package bridge

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/platform"
	"github.com/Guyungy/wb-account-sync/gobridge/internal/pyjson"
)

// BackupDirs 是迁移相关、值得留档的目录。
var BackupDirs = []string{
	"projects", "tasks", "blobs", "changes-detail", "changes-index",
	"file-history", "artifact-index", "memory", "skills", "connectors",
	"storage", "local_storage", "workspace", "plans", "projects-state",
}

// HeavyDirs 体积大且与迁移无关：默认不备份，需要时显式加入。
var HeavyDirs = []string{"app", "logs", "traces", "shell-snapshots", "cache", "changes-tmp"}

// BackupFiles 是根目录下的单文件状态，体积小、排障时很关键。
var BackupFiles = []string{
	"settings.json", "mcp.json", "models.json", "user-state.json",
	"workspace-state.json", "workspace-display-names.json",
	"last-launch.json", "qimei-cache.json",
}

// BackupOptions 是 backup 的输入。
type BackupOptions struct {
	Dest         string
	Label        string
	IncludeHeavy bool
}

// Backup 做一份手工全量快照。
//
// 注意它是"手工快照"，不是 undo journal：整库回滚要靠 restore 的 journal，
// 这里只是出事之后有东西可查、可手工恢复。
func Backup(homes []*Home, opts BackupOptions, log func(string)) (string, error) {
	dest := platform.ExpandPath(opts.Dest)
	if err := os.MkdirAll(dest, 0o755); err != nil {
		return "", err
	}
	label := opts.Label
	if label == "" {
		label = time.Now().Format("20060102-150405")
	}
	root := filepath.Join(dest, label+"-home-bridge")
	if pathExists(root) {
		return "", errf("备份目标已存在，不覆盖：%s", root)
	}
	if err := os.MkdirAll(root, 0o700); err != nil {
		return "", err
	}

	dirs := append([]string{}, BackupDirs...)
	if opts.IncludeHeavy {
		dirs = append(dirs, HeavyDirs...)
	}

	var est int64
	for _, home := range homes {
		if err := home.RequireValid(); err != nil {
			return "", err
		}
		if info, err := os.Stat(home.DBPath()); err == nil {
			est += info.Size()
		}
		for _, name := range dirs {
			est += DirSize(filepath.Join(home.Path, name))
		}
	}
	if err := RequireSpace(dest, est, "创建备份", 2*1024*1024*1024); err != nil {
		return "", err
	}

	for _, home := range homes {
		if err := home.RequireValid(); err != nil {
			return "", err
		}
		target := filepath.Join(root, home.Slug)
		if err := os.MkdirAll(target, 0o700); err != nil {
			return "", err
		}
		log(fmt.Sprintf("备份 %s → %s", home.Label, target))

		if err := snapshotDB(home.DBPath(), filepath.Join(target, DBName)); err != nil {
			return "", err
		}
		for _, name := range dirs {
			src := filepath.Join(home.Path, name)
			if !isDir(src) {
				continue
			}
			dst := filepath.Join(target, name)
			if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
				return "", err
			}
			if err := CopyTreeFull(src, dst); err != nil {
				return "", errf("复制 %s 失败：%v", src, err)
			}
		}
		for _, name := range BackupFiles {
			src := filepath.Join(home.Path, name)
			if !isFile(src) {
				continue
			}
			if err := copy2(src, filepath.Join(target, name)); err != nil {
				return "", errf("复制 %s 失败：%v", src, err)
			}
		}
		log(fmt.Sprintf("  完成：%s", Human(float64(DirSize(target)))))
	}

	excluded := []any{}
	if !opts.IncludeHeavy {
		for _, name := range HeavyDirs {
			excluded = append(excluded, name)
		}
	}
	manifest := pyjson.OrderedMap{}
	appendKV := func(k string, v any) { manifest = append(manifest, pyjson.KV{Key: k, Val: v}) }
	appendKV("created_at", time.Now().Format("2006-01-02T15:04:05-0700"))
	appendKV("tool", "wb-home-bridge "+Version)
	appendKV("include_heavy", opts.IncludeHeavy)
	appendKV("excluded_by_default", excluded)
	homeList := make([]any, 0, len(homes))
	for _, home := range homes {
		uid, _ := home.CurrentUID()
		homeList = append(homeList, pyjson.OrderedMap{
			{Key: "label", Val: home.Label},
			{Key: "path", Val: home.Path},
			{Key: "uid", Val: uid},
		})
	}
	appendKV("homes", homeList)

	if err := os.WriteFile(filepath.Join(root, "backup-manifest.json"),
		[]byte(pyjson.MustMarshalIndent(manifest, 2)), 0o600); err != nil {
		return "", err
	}

	log(fmt.Sprintf("\n备份根目录：%s", root))
	if !opts.IncludeHeavy {
		log(fmt.Sprintf("已排除（体积大且与迁移无关）：%s", strings.Join(HeavyDirs, ", ")))
		log("需要连日志一起留档时，追加 --include-heavy 重跑。")
	}
	log("注意：这是手工快照，不是本工具的 undo journal；整库回滚需手工恢复。")
	return root, nil
}

// snapshotDB 用 VACUUM INTO 做一致性快照。
//
// 为什么不能用文件复制：数据库处于 WAL 模式，只拷 .db 会丢掉还在 WAL 里的
// 已提交事务——备份看着成功了，恢复出来却少一截。VACUUM INTO 走的是
// SQLite 自己的读事务，拿到的是自洽快照，顺带还把碎片压实了。
func snapshotDB(srcPath, dstPath string) error {
	db, err := openRaw(srcPath)
	if err != nil {
		return err
	}
	defer db.Close()
	quoted := "'" + strings.ReplaceAll(dstPath, "'", "''") + "'"
	if _, err := db.Exec("VACUUM INTO " + quoted); err != nil {
		return errf("快照数据库失败（%s）：%v", srcPath, err)
	}
	return nil
}
