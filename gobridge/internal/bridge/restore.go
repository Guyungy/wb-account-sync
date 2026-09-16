package bridge

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/platform"
	"github.com/Guyungy/wb-account-sync/gobridge/internal/pyjson"
	_ "modernc.org/sqlite"
)

// openRaw 打开任意路径上的 SQLite 库，不要求它属于某个合法 Home。
//
// restore 与 backup 都要处理"用户给的路径"——回滚时目标 home 可能已经被
// 卸载或改名，拿不到合法 Home 也得能把记录里的行删掉。
func openRaw(dbPath string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, errf("无法打开数据库 %s：%v", dbPath, err)
	}
	db.SetMaxOpenConns(1)
	if _, err := db.Exec("PRAGMA busy_timeout=60000"); err != nil {
		db.Close()
		return nil, errf("无法设置 busy_timeout：%v", err)
	}
	return db, nil
}

// RestoreResult 汇总回滚结果。
type RestoreResult struct {
	RemovedFiles int
	RemovedRows  int
	RunDir       string
}

// Restore 撤销一次运行写入的行与文件。
//
// 明确**不**负责的部分（与 Python 版一致，也是刻意的）：
//   - 并集目录（blobs / skills / connectors-skills）里的新增文件不删。
//     这些目录是内容寻址的，删一个文件可能同时影响另一侧已有的引用，
//     自动化删除的风险高于收益，留给人工检查。
//   - 被合并改写的记忆原文不还原，只留 .before-bridge-* 备份。
func Restore(runDir, confirm string, log func(string), warn func(string)) (*RestoreResult, error) {
	runDir = platform.ExpandPath(runDir)
	undoPath := filepath.Join(runDir, "undo.json")
	if _, err := os.ReadFile(undoPath); err != nil {
		return nil, errf("找不到 undo 记录：%s", undoPath)
	}
	undo, err := loadOrInitUndo(undoPath, "")
	if err != nil {
		return nil, err
	}
	if confirm != undo.Get("plan_id").PlainString() {
		return nil, errf("确认串不匹配。--confirm 必须是该 run 的完整 plan_id。")
	}

	result := &RestoreResult{RunDir: runDir}

	// 文件：先删路径最深的。父目录被先删掉之后，子路径的 os.remove 会 ENOENT，
	// 于是明明该删的文件被跳过、还不报错。
	files := nodeStringList(undo.Get("files"))
	sort.SliceStable(files, func(i, j int) bool { return len(files[i]) > len(files[j]) })
	for _, path := range files {
		if !isFile(path) {
			continue
		}
		if isBackupArtifact(path) {
			// .before-bridge-* 是唯一的后悔药，回滚不该把它一起抹掉。
			continue
		}
		if err := os.Remove(path); err != nil {
			warn(fmt.Sprintf("  ! 无法删除 %s：%v", path, err))
			continue
		}
		result.RemovedFiles++
	}

	trees := nodeStringList(undo.Get("trees"))
	sort.SliceStable(trees, func(i, j int) bool { return len(trees[i]) > len(trees[j]) })
	for _, path := range trees {
		if isDir(path) {
			os.RemoveAll(path)
		}
	}

	dbRecs := undo.Get("db")
	if dbRecs != nil && dbRecs.Kind == pyjson.KindArray {
		for _, rec := range dbRecs.Arr {
			homePath := rec.Get("home").PlainString()
			table := rec.Get("table").PlainString()
			pk := rec.Get("pk").PlainString()
			ids := nodeStringList(rec.Get("ids"))
			if len(ids) == 0 || homePath == "" || table == "" || pk == "" {
				continue
			}
			n, derr := deleteRows(filepath.Join(homePath, DBName), table, pk, ids)
			if derr != nil {
				warn(fmt.Sprintf("  ! 回滚 %s %s 失败：%v", homePath, table, derr))
				continue
			}
			result.RemovedRows += n
			log(fmt.Sprintf("  回滚 %s %s: 删除 %d 行", homePath, table, n))
		}
	}

	log(fmt.Sprintf("\n已回滚 %d 个新建文件。", result.RemovedFiles))
	log("并集目录（blobs/skills/connectors-skills）中的新增文件未自动删除，请手工检查。")
	log("记忆文件已被合并改写，已保留 .before-bridge-* 备份，可手工还原。")
	return result, nil
}

// isBackupArtifact 判断是不是本工具留下的 .before-bridge-<ts> 备份。
func isBackupArtifact(path string) bool {
	idx := strings.LastIndex(path, ".before-bridge-")
	if idx < 0 {
		return false
	}
	// 后缀必须是纯数字时间戳，否则只是文件名里恰好含有这段。
	return allDigits(path[idx+len(".before-bridge-"):])
}

func allDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

func deleteRows(dbPath, table, pk string, ids []string) (int, error) {
	db, err := openRaw(dbPath)
	if err != nil {
		return 0, err
	}
	defer db.Close()

	ctx := context.Background()
	if _, err := db.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return 0, err
	}
	marks := make([]string, len(ids))
	args := make([]any, len(ids))
	for i, id := range ids {
		marks[i] = "?"
		args[i] = id
	}
	stmt := fmt.Sprintf(`DELETE FROM "%s" WHERE "%s" IN (%s)`,
		table, pk, strings.Join(marks, ","))
	res, err := db.ExecContext(ctx, stmt, args...)
	if err != nil {
		db.ExecContext(ctx, "ROLLBACK")
		return 0, err
	}
	n, _ := res.RowsAffected()
	if _, err := db.ExecContext(ctx, "COMMIT"); err != nil {
		return 0, err
	}
	return int(n), nil
}

// nodeStringList 取一个字符串数组节点的内容。
func nodeStringList(n *pyjson.Node) []string {
	if n == nil || n.Kind != pyjson.KindArray {
		return nil
	}
	out := make([]string, 0, len(n.Arr))
	for _, item := range n.Arr {
		if s := item.PlainString(); s != "" {
			out = append(out, s)
		}
	}
	return out
}
