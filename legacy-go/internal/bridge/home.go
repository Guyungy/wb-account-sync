// Package bridge 是 Python 版 tools/wb_home_bridge.py 的 Go 移植：
// 盘点、构建迁移计划、执行、备份、核验、回滚。
//
// 与 Python 版的硬约定：**同一份数据必须算出同一个 plan_id**。
// plan_id 是对规范化后的计划正文取 SHA-256，所以它同时充当
// "移植是否逐字节等价"的可执行证据——只要两边 plan_id 相同，
// 计划内容就必然一致，不需要人工逐字段比对。
package bridge

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	_ "modernc.org/sqlite" // 纯 Go 驱动：交叉编译不需要 C 工具链
)

// Version 与 Python 侧同号，便于对账。
const Version = "0.1.0"

// DBName 是客户端数据库文件名。
const DBName = "workbuddy.db"

// AccountSnapshot 相对 home 的账号快照路径。
const AccountSnapshot = "storage/skeleton/account-snapshot.json"

// 按会话 id 归档的内容资产。
var perSessionTrees = []struct {
	Template     string
	NeedsChanges bool
}{
	{"tasks/{cid}", false},
	{"changes-detail/{cid}", true},
	{"changes-index/{cid}", true},
	{"file-history/{cid}", true},
}

var perSessionFiles = []string{"artifact-index/{cid}.json"}

var projectSessionSuffixes = []string{".jsonl", ".meta.json", ".file-rollback.ndjson"}

// 跨 home 直接并集的目录（内容寻址或纯新增）。
var baseUnionTrees = []string{"blobs"}

var optionalUnionTrees = []struct {
	Name string
	Flag string
}{
	{"skills", "include_skills"},
	{"plugins/cache", "include_plugins"},
}

// 每个方向的指纹与行传输覆盖的表。
var (
	dbSessionTables  = []string{"sessions", "session_usage"}
	dbSideTables     = []string{"workspaces", "buddy_snapshots"}
	automationTables = []string{"automations", "automation_runs", "automation_runtime_state"}
)

var connectorSharedFiles = []string{"connector-states.json", "mcp.json"}

// Error 是受控失败：参数错误、安全检查拒绝、状态漂移。
type Error struct{ Msg string }

func (e *Error) Error() string { return e.Msg }

func errf(format string, args ...any) error { return &Error{Msg: fmt.Sprintf(format, args...)} }

// Home 是一个客户端的数据目录。
type Home struct {
	Label string
	Slug  string
	Path  string
	App   string
}

// DBPath 返回数据库路径。
func (h *Home) DBPath() string { return filepath.Join(h.Path, DBName) }

// SnapshotPath 返回账号快照路径。
func (h *Home) SnapshotPath() string { return filepath.Join(h.Path, AccountSnapshot) }

// RequireValid 在数据目录或数据库缺失时报错。
func (h *Home) RequireValid() error {
	info, err := os.Stat(h.Path)
	if err != nil || !info.IsDir() {
		return errf("[%s] 数据目录不存在：%s", h.Label, h.Path)
	}
	if st, err := os.Stat(h.DBPath()); err != nil || st.IsDir() {
		return errf("[%s] 缺少数据库：%s", h.Label, h.DBPath())
	}
	return nil
}

func (h *Home) snapshot() (map[string]any, error) {
	raw, err := os.ReadFile(h.SnapshotPath())
	if err != nil {
		return nil, errf("[%s] 缺少账号快照：%s\n请先启动该客户端并完成登录，再运行本工具。",
			h.Label, h.SnapshotPath())
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return nil, errf("[%s] 无法解析账号快照：%v", h.Label, err)
	}
	return doc, nil
}

// CurrentUID 从账号快照里取 primary.uid。
func (h *Home) CurrentUID() (string, error) {
	doc, err := h.snapshot()
	if err != nil {
		return "", err
	}
	primary, _ := doc["primary"].(map[string]any)
	uid, _ := primary["uid"].(string)
	if uid == "" {
		return "", errf("[%s] 账号快照中没有 primary.uid", h.Label)
	}
	return uid, nil
}

// Nickname 取账号昵称；缺失时返回空串而不是报错。
func (h *Home) Nickname() string {
	doc, err := h.snapshot()
	if err != nil {
		return ""
	}
	primary, _ := doc["primary"].(map[string]any)
	name, _ := primary["nickname"].(string)
	return name
}

// Connect 打开数据库并设置 busy_timeout。
func (h *Home) Connect() (*sql.DB, error) {
	db, err := sql.Open("sqlite", h.DBPath())
	if err != nil {
		return nil, errf("[%s] 无法打开数据库：%v", h.Label, err)
	}
	// 单连接：客户端在写时会占锁，多连接只会让 SQLITE_BUSY 更容易出现。
	db.SetMaxOpenConns(1)
	if _, err := db.Exec("PRAGMA busy_timeout=60000"); err != nil {
		db.Close()
		return nil, errf("[%s] 无法设置 busy_timeout：%v", h.Label, err)
	}
	return db, nil
}

// --------------------------------------------------------------------------
// 基础工具
// --------------------------------------------------------------------------

// Human 复刻 Python 的 human()：1024 进制、B 不带小数、其余一位小数。
func Human(n float64) string {
	units := []string{"B", "KB", "MB", "GB"}
	for _, unit := range units {
		if n < 1024 || unit == "GB" {
			if unit == "B" {
				return fmt.Sprintf("%dB", int64(n))
			}
			return fmt.Sprintf("%.1f%s", n, unit)
		}
		n /= 1024.0
	}
	return fmt.Sprintf("%.1fGB", n)
}

// DirSize 递归统计文件字节数。
//
// 与 Python 的 os.walk 保持同一口径：符号链接指向的目录**不递归**、
// 也不计入大小；指向文件的符号链接按目标大小计入。两边算出来的
// approx_bytes 必须一致，否则计划正文就对不上了。
func DirSize(path string) int64 {
	info, err := os.Stat(path) // 跟随符号链接，等价于 os.path.isdir
	if err != nil || !info.IsDir() {
		return 0
	}
	var total int64
	var walk func(string)
	walk = func(dir string) {
		entries, err := os.ReadDir(dir)
		if err != nil {
			return
		}
		for _, e := range entries {
			full := filepath.Join(dir, e.Name())
			if e.IsDir() {
				walk(full)
				continue
			}
			st, err := os.Stat(full)
			if err == nil && !st.IsDir() {
				total += st.Size()
			}
		}
	}
	walk(path)
	return total
}

// TreeManifest 返回相对路径 → 文件大小。目录顺序与文件顺序都排过序。
func TreeManifest(path string) map[string]int64 {
	out := map[string]int64{}
	info, err := os.Stat(path)
	if err != nil || !info.IsDir() {
		return out
	}
	var walk func(string)
	walk = func(dir string) {
		entries, err := os.ReadDir(dir)
		if err != nil {
			return
		}
		names := make([]string, 0, len(entries))
		for _, e := range entries {
			names = append(names, e.Name())
		}
		sort.Strings(names)
		for _, name := range names {
			full := filepath.Join(dir, name)
			if st, err := os.Stat(full); err == nil && st.IsDir() {
				walk(full)
				continue
			}
			if st, err := os.Stat(full); err == nil {
				rel, err := filepath.Rel(path, full)
				if err != nil {
					continue
				}
				out[rel] = st.Size()
			}
		}
	}
	walk(path)
	return out
}

// ProjectSlugFor 找出含该会话正文的项目桶（可能多于一个）。
func ProjectSlugFor(h *Home, cid string) []string {
	base := filepath.Join(h.Path, "projects")
	entries, err := os.ReadDir(base)
	if err != nil {
		return nil
	}
	slugs := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		if st, err := os.Stat(filepath.Join(base, e.Name(), cid+".jsonl")); err == nil && !st.IsDir() {
			slugs = append(slugs, e.Name())
		}
	}
	sort.Strings(slugs)
	return slugs
}

// ConversationsMissingDBRow 返回有正文文件但数据库无对应会话行的会话 id。
func ConversationsMissingDBRow(h *Home) ([]string, error) {
	base := filepath.Join(h.Path, "projects")
	entries, err := os.ReadDir(base)
	if err != nil {
		return nil, nil
	}
	db, err := h.Connect()
	if err != nil {
		return nil, err
	}
	defer db.Close()
	known, err := idSet(db, "SELECT id FROM sessions")
	if err != nil {
		return nil, err
	}
	orphans := map[string]bool{}
	for _, slug := range entries {
		if !slug.IsDir() {
			continue
		}
		names, err := os.ReadDir(filepath.Join(base, slug.Name()))
		if err != nil {
			continue
		}
		for _, n := range names {
			if !strings.HasSuffix(n.Name(), ".jsonl") {
				continue
			}
			id := strings.TrimSuffix(n.Name(), ".jsonl")
			if !known[id] {
				orphans[id] = true
			}
		}
	}
	out := make([]string, 0, len(orphans))
	for id := range orphans {
		out = append(out, id)
	}
	sort.Strings(out)
	return out, nil
}

// SkillsOf 返回 skills/ 下的技能目录名（不含隐藏目录）。
func SkillsOf(h *Home) []string {
	base := filepath.Join(h.Path, "skills")
	entries, err := os.ReadDir(base)
	if err != nil {
		return nil
	}
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		if !e.IsDir() || strings.HasPrefix(e.Name(), ".") {
			continue
		}
		out = append(out, e.Name())
	}
	sort.Strings(out)
	return out
}

// idSet 把单列文本查询收成集合。
func idSet(db *sql.DB, query string) (map[string]bool, error) {
	rows, err := db.Query(query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]bool{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out[id] = true
	}
	return out, rows.Err()
}

// TableCount 返回表行数；表不存在时返回 (-1, nil) 等价于 Python 的 None。
func TableCount(db *sql.DB, table string) (int64, bool) {
	var n int64
	if err := db.QueryRow(fmt.Sprintf(`SELECT COUNT(*) FROM "%s"`, table)).Scan(&n); err != nil {
		return 0, false
	}
	return n, true
}

// TargetColumns 返回目标表实际存在的列；表不存在时返回空。
func TargetColumns(db *sql.DB, table string) []string {
	rows, err := db.Query(fmt.Sprintf(`PRAGMA table_info("%s")`, table))
	if err != nil {
		return nil
	}
	defer rows.Close()
	var cols []string
	for rows.Next() {
		var (
			cid     int
			name    string
			ctype   string
			notnull int
			dflt    sql.NullString
			pk      int
		)
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			return cols
		}
		cols = append(cols, name)
	}
	return cols
}
