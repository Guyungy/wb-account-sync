package bridge

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/pyjson"
)

// 各表的主键列，用于把"插了哪些行"记进 undo，回滚时按主键删回去。
//
// buddy_snapshots 写的是 "id"，而它真正的主键列叫 snapshot_id——
// 这是从 Python 版原样带过来的。实践中该表恒为 0 行，所以没暴露过；
// 保留原值是为了两边行为一致，真要修得两边一起改（见 restore.go 的注释）。
var undoPKColumn = map[string]string{
	"sessions":                 "id",
	"automations":              "id",
	"session_usage":            "session_id",
	"workspaces":               "path",
	"buddy_snapshots":          "id",
	"automation_runs":          "thread_id",
	"automation_runtime_state": "automation_id",
}

// ApplyOptions 是 apply 需要的全部输入。
type ApplyOptions struct {
	PlanPath string
	Confirm  string
	StateDir string
}

// ApplyResult 汇总一次执行的结果，供 UI/CLI 打印。
type ApplyResult struct {
	PlanID       string
	RunDir       string
	JournalPath  string
	UndoPath     string
	DBInserted   map[string]int // 键为 "side/table"
	CreatedFiles int
	MemoryAdded  map[string]int
	ClawAdded    map[string][]string
}

// Apply 执行计划。
//
// 这里最关键的一条：**执行前重新生成一次计划并比对 plan_id**。
// 计划是"源数据在某一刻的快照"，如果生成计划之后源侧又写了新会话，
// 直接按老计划插入就会漏数据——而且是不报错地漏。
// 所以宁可整个拒绝，让用户重新生成、重新审阅。
func Apply(a, b *Home, opts ApplyOptions, log func(string), warn func(string)) (*ApplyResult, error) {
	savedRaw, err := os.ReadFile(opts.PlanPath)
	if err != nil {
		return nil, errf("读取计划失败：%v", err)
	}
	saved, err := jsonToMap(savedRaw)
	if err != nil {
		return nil, errf("解析计划失败：%v", err)
	}
	savedPlanID := asString(saved["plan_id"])
	if opts.Confirm != savedPlanID {
		return nil, errf("确认串不匹配。--confirm 必须是计划的完整 plan_id，不支持短前缀。\n"+
			"计划 plan_id: %s", savedPlanID)
	}

	savedHomes, _ := saved["homes"].(map[string]any)
	homePath := func(key string) string {
		if m, ok := savedHomes[key].(map[string]any); ok {
			return asString(m["path"])
		}
		return ""
	}
	if homePath("a") != a.Path || homePath("b") != b.Path {
		return nil, errf("计划记录的两个 home 与当前参数不一致。")
	}

	// 用计划里记录的执行选项重建，避免 apply 与 plan 选项不一致导致误判漂移
	options, _ := saved["options"].(map[string]any)
	if options == nil {
		options = DefaultOptions()
	}
	for _, key := range []string{"include_plugins", "include_automations", "include_storage",
		"include_connectors", "overwrite_assets"} {
		options[key] = truthy(options[key])
	}

	fresh, err := BuildPlan(a, b, options)
	if err != nil {
		return nil, err
	}
	if fresh.PlanID != savedPlanID {
		return nil, errf("计划已漂移（源数据或账号快照在生成计划后发生了变化），拒绝执行。\n"+
			"原计划: %s\n当前值: %s\n"+
			"请重新生成计划并重新审阅。", savedPlanID, fresh.PlanID)
	}

	summary, _ := fresh.Summary["totals"].(map[string]any)
	approx := asInt64(summary["approx_bytes"])
	if err := RequireSpace(a.Path, approx, "执行迁移", int64(1024*1024*1024)); err != nil {
		return nil, err
	}

	stateDir, err := StateDirGuard(opts.StateDir)
	if err != nil {
		return nil, err
	}
	runDir := filepath.Join(stateDir, "runs", savedPlanID)
	if err := os.MkdirAll(runDir, 0o700); err != nil {
		return nil, err
	}
	journalPath := filepath.Join(runDir, "journal.jsonl")
	undoPath := filepath.Join(runDir, "undo.json")

	undo, err := loadOrInitUndo(undoPath, savedPlanID)
	if err != nil {
		return nil, err
	}
	if _, err := os.Stat(undoPath); err == nil {
		warn(fmt.Sprintf("注意：该计划已有运行记录，将按幂等语义继续（%s）。", runDir))
	}

	journal, err := os.OpenFile(journalPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600)
	if err != nil {
		return nil, err
	}
	defer journal.Close()

	logRecord := func(rec map[string]any) {
		rec["at"] = time.Now().Format("2006-01-02T15:04:05-0700")
		journal.WriteString(pyjson.MustMarshal(rec) + "\n")
	}
	saveUndo := func() error { return saveUndoFile(undo, undoPath) }

	result := &ApplyResult{
		PlanID:      savedPlanID,
		RunDir:      runDir,
		JournalPath: journalPath,
		UndoPath:    undoPath,
		DBInserted:  map[string]int{},
		MemoryAdded: map[string]int{},
		ClawAdded:   map[string][]string{},
	}

	// ---------------- 1) 数据库行 ----------------
	log("== 数据库行 ==")
	sideHomes := map[string]*Home{"a2b": b, "b2a": a}
	for _, side := range []string{"a2b", "b2a"} {
		target := sideHomes[side]
		tables := fresh.Rows[side]
		tableNames := make([]string, 0, len(tables))
		for name := range tables {
			tableNames = append(tableNames, name)
		}
		sort.Strings(tableNames)
		for _, table := range tableNames {
			rows := tables[table]
			pk, hasPK := undoPKColumn[table]
			if len(rows) > 0 && hasPK {
				ids := make([]string, 0, len(rows))
				for _, row := range rows {
					if v := row[pk]; v != nil {
						ids = append(ids, asString(v))
					}
				}
				if len(ids) > 0 {
					appendUndoDB(undo, target.Path, table, pk, ids)
				}
			}
			n, err := InsertRows(target, table, rows, warn)
			if err != nil {
				return nil, err
			}
			result.DBInserted[side+"/"+table] = n
			if len(rows) > 0 {
				log(fmt.Sprintf("  [%s] %s: 计划 %d 行，实插 %d 行", side, table, len(rows), n))
				logRecord(map[string]any{
					"op": "db_insert", "side": side, "home": target.Path,
					"table": table, "planned": len(rows), "inserted": n,
				})
			}
		}
		if err := saveUndo(); err != nil {
			return nil, err
		}
	}

	// ---------------- 2) 文件资产 ----------------
	log("== 文件资产 ==")
	overwrite := truthy(fresh.Options["overwrite_assets"])
	createdFiles := 0
	for _, entry := range fresh.Entries {
		switch entry.Mode {
		case "copy_if_missing":
			if !isFile(entry.Src) {
				continue
			}
			existed := pathExists(entry.Dst)
			status, cerr := CopyFile(entry.Src, entry.Dst, overwrite)
			if cerr != nil {
				warn(fmt.Sprintf("  ! 复制失败 %s：%v", entry.Src, cerr))
				continue
			}
			if status == "create" || status == "overwrite" {
				createdFiles++
				if !existed {
					appendUndoStrings(undo, "files", entry.Dst)
				}
				logRecord(map[string]any{"op": "file", "status": status, "dst": entry.Dst})
			}
		case "copy_tree_if_missing":
			if !isDir(entry.Src) {
				continue
			}
			existed := isDir(entry.Dst)
			status, cerr := CopyTree(entry.Src, entry.Dst, overwrite, warn)
			if cerr != nil {
				warn(fmt.Sprintf("  ! 复制目录失败 %s：%v", entry.Src, cerr))
				continue
			}
			if !existed {
				appendUndoStrings(undo, "trees", entry.Dst)
			}
			logRecord(map[string]any{"op": "tree", "status": status, "dst": entry.Dst})
		case "merge_tree":
			if !isDir(entry.Src) {
				continue
			}
			created, skipped, merr := MergeTree(entry.Src, entry.Dst, overwrite, warn)
			if merr != nil {
				warn(fmt.Sprintf("  ! 合并目录失败 %s：%v", entry.Src, merr))
				continue
			}
			createdFiles += created
			appendUndoStrings(undo, "merged_dirs", entry.Dst)
			logRecord(map[string]any{"op": "merge_tree", "dst": entry.Dst,
				"created": created, "skipped": skipped})
		}
	}
	if err := saveUndo(); err != nil {
		return nil, err
	}
	result.CreatedFiles = createdFiles
	log(fmt.Sprintf("  新增文件 %d 个", createdFiles))

	// ---------------- 3) 记忆合并 ----------------
	if truthy(fresh.Options["include_memory"]) {
		log("== 长期记忆 ==")
		for _, p := range []struct {
			side     string
			src, dst *Home
		}{{"a2b", a, b}, {"b2a", b, a}} {
			uid, uerr := p.dst.CurrentUID()
			if uerr != nil {
				warn(fmt.Sprintf("  [%s] 读不到目标账号 uid，跳过记忆合并：%v", p.side, uerr))
				continue
			}
			added, status, created, mErr := mergeHomeMemory(p.src, p.dst, uid)
			if mErr != nil {
				return nil, mErr
			}
			if status != "merged" {
				log(fmt.Sprintf("  [%s] %s", p.side, status))
				continue
			}
			if created {
				// 目标本来没有记忆文件，是我们新建的 → 记进 undo 才能回滚掉。
				appendUndoStrings(undo, "files", filepath.Join(p.dst.Path, "memory", uid+"_memory.md"))
			}
			if err := saveUndo(); err != nil {
				return nil, err
			}
			result.MemoryAdded[p.side] = added
			log(fmt.Sprintf("  [%s] 记忆合并完成，新增 %d 段", p.side, added))
			logRecord(map[string]any{"op": "memory", "side": p.side, "status": status, "added": added})
		}
	}

	// ---------------- 4) claw.users 渠道绑定 ----------------
	if truthy(fresh.Options["include_claw"]) {
		for _, p := range []struct {
			side     string
			src, dst *Home
		}{{"a2b", a, b}, {"b2a", b, a}} {
			sp := filepath.Join(p.src.Path, "settings.json")
			dp := filepath.Join(p.dst.Path, "settings.json")
			if !isFile(sp) || !isFile(dp) {
				continue
			}
			added, cerr := MergeClawUsers(sp, dp)
			if cerr != nil {
				warn(fmt.Sprintf("  [%s] settings.json 渠道绑定合并失败：%v", p.side, cerr))
				continue
			}
			if len(added) == 0 {
				continue
			}
			result.ClawAdded[p.side] = added
			log(fmt.Sprintf("  [%s] settings.json 渠道绑定：新增 %d 个账号条目", p.side, len(added)))
			logRecord(map[string]any{"op": "claw_users", "side": p.side, "dst": dp, "added": added})
		}
	}

	setUndoString(undo, "finished_at", time.Now().Format("2006-01-02T15:04:05-0700"))
	if err := saveUndo(); err != nil {
		return nil, err
	}
	log(fmt.Sprintf("\n完成。运行目录：%s", runDir))
	return result, nil
}

// mergeHomeMemory 把源 home 里"最像当前用户记忆"的那份并进目标。
//
// 源目录里可能同时存在多个 *_memory.md（换过账号就会留档），
// 取体积最大的那份——体积大意味着历史上写得更多，是最接近"完整记忆"的近似。
//
// 返回 (新增段数, 状态词, 目标文件是否为本工具新建)。第三个返回值决定
// 调用方要不要把它记进 undo：原本就存在的文件被改写时要留 .before-bridge-*
// 备份，而新建的文件回滚时应该直接删掉，两者是相反的处置。
func mergeHomeMemory(src, dst *Home, dstUID string) (int, string, bool, error) {
	srcDir := filepath.Join(src.Path, "memory")
	dstDir := filepath.Join(dst.Path, "memory")
	if err := os.MkdirAll(dstDir, 0o755); err != nil {
		return 0, "", false, err
	}
	entries, err := os.ReadDir(srcDir)
	if err != nil {
		return 0, "源无记忆文件，跳过", false, nil
	}
	best := ""
	bestSize := int64(-1)
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), "_memory.md") {
			continue
		}
		info, ierr := e.Info()
		if ierr != nil {
			continue
		}
		if info.Size() > bestSize {
			bestSize = info.Size()
			best = filepath.Join(srcDir, e.Name())
		}
	}
	if best == "" {
		return 0, "源无记忆文件，跳过", false, nil
	}
	if bestSize == 0 {
		return 0, "源记忆为空，跳过", false, nil
	}

	dstFile := filepath.Join(dstDir, dstUID+"_memory.md")
	created := !isFile(dstFile)
	if !created {
		// 已被合并改写的记忆原文无从恢复，留时间戳备份是唯一的后悔药。
		if err := copy2(dstFile, dstFile+".before-bridge-"+strconv.FormatInt(time.Now().Unix(), 10)); err != nil {
			return 0, "", false, err
		}
	}
	_, added, err := MemoryMerge(best, dstFile, dstUID)
	if err != nil {
		return 0, "", false, err
	}
	return added, "merged", created, nil
}

// --------------------------------------------------------------------------
// 数据库写入
// --------------------------------------------------------------------------

// InsertRows 只插入缺的行，已存在的一律忽略（幂等）。
//
// 用 BEGIN IMMEDIATE 而不是默认的 DEFERRED：客户端可能正在后台跑，
// 先拿写锁能在"发现冲突"和"改了半截"之间留出明确的边界——
// DEFERRED 会在第一条写语句处才升锁，那时事务已经开始了。
func InsertRows(home *Home, table string, rows []map[string]any, warn func(string)) (int, error) {
	if len(rows) == 0 {
		return 0, nil
	}
	db, err := home.Connect()
	if err != nil {
		return 0, err
	}
	defer db.Close()

	ctx := context.Background()
	if _, err := db.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return 0, errf("[%s] 无法开启写事务：%v", home.Label, err)
	}
	inserted := 0
	fail := func(err error) (int, error) {
		db.ExecContext(ctx, "ROLLBACK")
		return 0, errf("[%s] 写入 %s 失败：%v", home.Label, table, err)
	}

	for _, row := range rows {
		cols := make([]string, 0, len(row))
		for c := range row {
			cols = append(cols, c)
		}
		// 列顺序必须稳定：同一份计划两次执行要生成同样的 SQL，
		// 否则 undo/日志对不上（值是按列名绑的，顺序本身不影响结果）。
		sort.Strings(cols)
		quoted := make([]string, len(cols))
		marks := make([]string, len(cols))
		vals := make([]any, len(cols))
		for i, c := range cols {
			quoted[i] = `"` + c + `"`
			marks[i] = "?"
			vals[i] = row[c]
		}
		stmt := fmt.Sprintf(`INSERT OR IGNORE INTO "%s" (%s) VALUES (%s)`,
			table, strings.Join(quoted, ","), strings.Join(marks, ","))
		res, ierr := db.ExecContext(ctx, stmt, vals...)
		if ierr != nil {
			if isConstraintError(ierr) {
				warn(fmt.Sprintf("  ! %s 插入被拒（%v）", table, ierr))
				continue
			}
			return fail(ierr)
		}
		if n, aerr := res.RowsAffected(); aerr == nil && n > 0 {
			inserted += int(n)
		}
	}
	if _, err := db.ExecContext(ctx, "COMMIT"); err != nil {
		return fail(err)
	}
	return inserted, nil
}

func isConstraintError(err error) bool {
	if err == nil {
		return false
	}
	msg := strings.ToLower(err.Error())
	return strings.Contains(msg, "constraint") || strings.Contains(msg, "unique")
}

// --------------------------------------------------------------------------
// undo 记录
// --------------------------------------------------------------------------

func loadOrInitUndo(path, planID string) (*pyjson.Node, error) {
	if raw, err := os.ReadFile(path); err == nil {
		node, perr := pyjson.ParseOrdered(raw)
		if perr != nil {
			return nil, errf("解析已有 undo 记录失败（%s）：%v", path, perr)
		}
		return node, nil
	}
	undo := pyjson.NewObject()
	// 键序与 Python 侧一致：plan_id, db, files, trees, merged_dirs
	undo.Set("plan_id", strNode(planID))
	undo.Set("db", &pyjson.Node{Kind: pyjson.KindArray, Arr: []*pyjson.Node{}})
	undo.Set("files", &pyjson.Node{Kind: pyjson.KindArray, Arr: []*pyjson.Node{}})
	undo.Set("trees", &pyjson.Node{Kind: pyjson.KindArray, Arr: []*pyjson.Node{}})
	undo.Set("merged_dirs", &pyjson.Node{Kind: pyjson.KindArray, Arr: []*pyjson.Node{}})
	return undo, nil
}

// saveUndoFile 先写临时文件再原子替换：undo 是回滚的唯一依据，
// 半截写入比没有更危险（会让人以为已经记录完整）。
func saveUndoFile(undo *pyjson.Node, path string) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, []byte(pyjson.EncodeOrdered(undo, 2)), 0o600); err != nil {
		return err
	}
	if err := os.Chmod(tmp, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func appendUndoStrings(undo *pyjson.Node, key, value string) {
	arr := undo.Get(key)
	if arr == nil || arr.Kind != pyjson.KindArray {
		arr = &pyjson.Node{Kind: pyjson.KindArray, Arr: []*pyjson.Node{}}
		undo.Set(key, arr)
	}
	arr.Arr = append(arr.Arr, strNode(value))
}

func appendUndoDB(undo *pyjson.Node, home, table, pk string, ids []string) {
	arr := undo.Get("db")
	if arr == nil || arr.Kind != pyjson.KindArray {
		arr = &pyjson.Node{Kind: pyjson.KindArray, Arr: []*pyjson.Node{}}
		undo.Set("db", arr)
	}
	rec := pyjson.NewObject()
	rec.Set("home", strNode(home))
	rec.Set("table", strNode(table))
	rec.Set("pk", strNode(pk))
	idArr := &pyjson.Node{Kind: pyjson.KindArray, Arr: make([]*pyjson.Node, 0, len(ids))}
	for _, id := range ids {
		idArr.Arr = append(idArr.Arr, strNode(id))
	}
	rec.Set("ids", idArr)
	arr.Arr = append(arr.Arr, rec)
}

func setUndoString(undo *pyjson.Node, key, value string) {
	undo.Set(key, strNode(value))
}

func strNode(s string) *pyjson.Node {
	return &pyjson.Node{Kind: pyjson.KindString, Str: s}
}

// --------------------------------------------------------------------------
// 小工具
// --------------------------------------------------------------------------

func jsonToMap(raw []byte) (map[string]any, error) {
	var out map[string]any
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.UseNumber()
	if err := dec.Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}

func asInt64(v any) int64 {
	switch t := v.(type) {
	case nil:
		return 0
	case int64:
		return t
	case int:
		return int64(t)
	case float64:
		return int64(t)
	case json.Number:
		n, _ := t.Int64()
		return n
	case string:
		n, _ := strconv.ParseInt(t, 10, 64)
		return n
	}
	return 0
}

func pathExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

func isFile(p string) bool {
	info, err := os.Stat(p)
	return err == nil && info.Mode().IsRegular()
}
