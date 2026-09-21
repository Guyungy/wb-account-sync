package bridge

import (
	"database/sql"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/pyjson"
)

// PlanEntry 是一条待落地的文件/目录动作。
type PlanEntry struct {
	Kind string // file | tree
	Src  string
	Dst  string
	Mode string // copy_if_missing | copy_tree_if_missing | merge_tree
	Note string
}

// AsDict 决定 entries_fingerprint 的内容。
//
// 必须返回普通 map：Python 的 sort_keys=True 是**递归**生效的，
// entry 字典内部的键也按字典序排（dst/kind/mode/note/src）。
// 早先这里用了保序结构按 kind/mode/src/dst 输出，哈希就对不上了——
// 字段相同、顺序不同，SHA-256 完全是另一串。
func (e PlanEntry) AsDict() map[string]any {
	d := map[string]any{
		"kind": e.Kind,
		"mode": e.Mode,
		"src":  e.Src,
		"dst":  e.Dst,
	}
	if e.Note != "" {
		d["note"] = e.Note
	}
	return d
}

// Plan 是一份冻结了数据指纹的迁移计划。
type Plan struct {
	Version     string
	HomeA       string
	HomeB       string
	LabelA      string
	LabelB      string
	UIDA        string
	UIDB        string
	CreatedAt   string
	Options     map[string]any
	Rows        map[string]map[string][]map[string]any
	Skipped     map[string]map[string]int
	Fingerprint map[string]map[string]string
	Entries     []PlanEntry
	Summary     map[string]any
	PlanID      string
}

// Body 是参与 plan_id 计算的正文。**改这里等于改哈希契约**，
// 任何字段增删都必须同时在两边改。
func (p *Plan) Body() map[string]any {
	entryDicts := make([]any, 0, len(p.Entries))
	for _, e := range p.Entries {
		entryDicts = append(entryDicts, e.AsDict())
	}
	fp := map[string]any{}
	for side, m := range p.Fingerprint {
		inner := map[string]any{}
		for k, v := range m {
			inner[k] = v
		}
		fp[side] = inner
	}
	return map[string]any{
		"version":             p.Version,
		"home_a":              p.HomeA,
		"home_b":              p.HomeB,
		"uid_a":               p.UIDA,
		"uid_b":               p.UIDB,
		"options":             p.Options,
		"source_fingerprints": fp,
		"entries_fingerprint": pyjson.HashJSON(entryDicts),
	}
}

// Finalize 算出 plan_id。
func (p *Plan) Finalize() { p.PlanID = pyjson.HashJSON(p.Body()) }

// AsDict 是写入磁盘 / 回给界面的完整计划文档。
//
// 注意 entries 这里要还原成**普通对象**：AsDict 只是给外部消费的文档，
// 用 OrderedMap 会让 Go 的 encoding/json 序列化成 [{Key,Val}] 数组，
// 而 do_apply 读的是对象。保序只在算 entries_fingerprint 时有意义。
func (p *Plan) AsDict() map[string]any {
	entries := make([]any, 0, len(p.Entries))
	for _, e := range p.Entries {
		obj := map[string]any{
			"kind": e.Kind,
			"mode": e.Mode,
			"src":  e.Src,
			"dst":  e.Dst,
		}
		if e.Note != "" {
			obj["note"] = e.Note
		}
		entries = append(entries, obj)
	}
	fp := map[string]any{}
	for side, m := range p.Fingerprint {
		inner := map[string]any{}
		for k, v := range m {
			inner[k] = v
		}
		fp[side] = inner
	}
	rows := map[string]any{}
	for side, tables := range p.Rows {
		inner := map[string]any{}
		for table, list := range tables {
			items := make([]any, 0, len(list))
			for _, r := range list {
				items = append(items, r)
			}
			inner[table] = items
		}
		rows[side] = inner
	}
	skipped := map[string]any{}
	for side, m := range p.Skipped {
		inner := map[string]any{}
		for k, v := range m {
			inner[k] = v
		}
		skipped[side] = inner
	}
	uid := func(a, b string) string { return a }
	_ = uid
	return map[string]any{
		"plan_id":    p.PlanID,
		"version":    p.Version,
		"created_at": p.CreatedAt,
		"homes": map[string]any{
			"a": map[string]any{"label": p.LabelA, "path": p.HomeA, "uid": p.UIDA},
			"b": map[string]any{"label": p.LabelB, "path": p.HomeB, "uid": p.UIDB},
		},
		"options":             p.Options,
		"summary":             p.Summary,
		"source_fingerprints": fp,
		"rows":                rows,
		"skipped":             skipped,
		"entries":             entries,
	}
}

// --------------------------------------------------------------------------
// 行读取
// --------------------------------------------------------------------------

// queryDicts 把一次查询收成 []map[string]any（保持行的先后顺序）。
func queryDicts(db *sql.DB, query string, args ...any) ([]map[string]any, error) {
	rows, err := db.Query(query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	cols, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	var out []map[string]any
	for rows.Next() {
		holders := make([]any, len(cols))
		for i := range holders {
			var v any
			holders[i] = &v
		}
		if err := rows.Scan(holders...); err != nil {
			return nil, err
		}
		item := make(map[string]any, len(cols))
		for i, c := range cols {
			item[c] = deref(holders[i])
		}
		out = append(out, item)
	}
	return out, rows.Err()
}

func deref(p any) any {
	ptr, ok := p.(*any)
	if !ok {
		return nil
	}
	v := *ptr
	if b, ok := v.([]byte); ok {
		// 驱动对 TEXT 与 BLOB 都回 []byte；文本列统一成 string 才能与 Python 对齐。
		return string(b)
	}
	return v
}

// CollectRows 收集要 INSERT 到 dst 的行 + 源侧完整指纹 + 跳过计数。
//
// `sessions_all` 指纹是计划漂移检测的核心：源侧任何一行变化都会换掉 plan_id。
func CollectRows(src, dst *Home, dstUID string, options map[string]any) (
	map[string][]map[string]any, map[string]int, map[string]string, error,
) {
	rows := map[string][]map[string]any{}
	skipped := map[string]int{}
	fingerprints := map[string]string{}

	srcDB, err := src.Connect()
	if err != nil {
		return nil, nil, nil, err
	}
	defer srcDB.Close()
	dstDB, err := dst.Connect()
	if err != nil {
		return nil, nil, nil, err
	}
	defer dstDB.Close()

	sessionList, err := queryDicts(srcDB, "SELECT * FROM sessions WHERE deleted_at IS NULL")
	if err != nil {
		return nil, nil, nil, errf("[%s] 读取 sessions 失败：%v", src.Label, err)
	}
	srcSessions := map[string]any{}
	for _, row := range sessionList {
		srcSessions[asString(row["id"])] = row
	}
	fingerprints["sessions_all"] = pyjson.HashJSON(srcSessions)

	dstIDs, err := idSet(dstDB, "SELECT id FROM sessions")
	if err != nil {
		return nil, nil, nil, errf("[%s] 读取 sessions 失败：%v", dst.Label, err)
	}
	toCopy := make([]string, 0, len(srcSessions))
	for sid := range srcSessions {
		if !dstIDs[sid] {
			toCopy = append(toCopy, sid)
		}
	}
	sort.Strings(toCopy)
	skipped["sessions"] = len(srcSessions) - len(toCopy)

	rows["sessions"] = []map[string]any{}
	copied := map[string]bool{}
	for _, sid := range toCopy {
		original, _ := srcSessions[sid].(map[string]any)
		clone := make(map[string]any, len(original))
		for k, v := range original {
			clone[k] = v
		}
		clone["user_id"] = dstUID
		rows["sessions"] = append(rows["sessions"], clone)
		copied[sid] = true
	}

	// session_usage 跟随
	dstUsage, err := idSet(dstDB, "SELECT session_id FROM session_usage")
	if err != nil {
		return nil, nil, nil, errf("[%s] 读取 session_usage 失败：%v", dst.Label, err)
	}
	usage, err := queryDicts(srcDB, "SELECT * FROM session_usage")
	if err != nil {
		return nil, nil, nil, errf("[%s] 读取 session_usage 失败：%v", src.Label, err)
	}
	rows["session_usage"] = []map[string]any{}
	for _, r := range usage {
		sid := asString(r["session_id"])
		if copied[sid] && !dstUsage[sid] {
			rows["session_usage"] = append(rows["session_usage"], r)
		}
	}

	// workspaces 并集
	dstWs, err := idSet(dstDB, "SELECT path FROM workspaces")
	if err != nil {
		return nil, nil, nil, errf("[%s] 读取 workspaces 失败：%v", dst.Label, err)
	}
	ws, err := queryDicts(srcDB, "SELECT * FROM workspaces")
	if err != nil {
		return nil, nil, nil, errf("[%s] 读取 workspaces 失败：%v", src.Label, err)
	}
	rows["workspaces"] = []map[string]any{}
	for _, r := range ws {
		if !dstWs[asString(r["path"])] {
			rows["workspaces"] = append(rows["workspaces"], r)
		}
	}

	// buddy_snapshots：只搬被引用且目标缺失的
	rows["buddy_snapshots"] = []map[string]any{}
	snapCols := TargetColumns(dstDB, "buddy_snapshots")
	if contains(snapCols, "snapshot_id") {
		dstSnap, err := idSet(dstDB, "SELECT snapshot_id FROM buddy_snapshots")
		if err != nil {
			return nil, nil, nil, errf("[%s] 读取 buddy_snapshots 失败：%v", dst.Label, err)
		}
		referenced := map[string]bool{}
		for _, r := range rows["sessions"] {
			if v := asString(r["buddy_snapshot_id"]); v != "" {
				referenced[v] = true
			}
		}
		if len(referenced) > 0 {
			snaps, err := queryDicts(srcDB, "SELECT * FROM buddy_snapshots")
			if err != nil {
				return nil, nil, nil, errf("[%s] 读取 buddy_snapshots 失败：%v", src.Label, err)
			}
			for _, r := range snaps {
				sid := asString(r["snapshot_id"])
				if referenced[sid] && !dstSnap[sid] {
					rows["buddy_snapshots"] = append(rows["buddy_snapshots"], r)
				}
			}
		}
	}

	if truthy(options["include_automations"]) {
		autoAll, err := queryDicts(srcDB, "SELECT * FROM automations WHERE deleted_at IS NULL")
		if err != nil {
			return nil, nil, nil, errf("[%s] 读取 automations 失败：%v", src.Label, err)
		}
		list := make([]any, 0, len(autoAll))
		for _, r := range autoAll {
			list = append(list, r)
		}
		fingerprints["automations_all"] = pyjson.HashJSON(list)

		dstAuto, err := idSet(dstDB, "SELECT id FROM automations")
		if err != nil {
			return nil, nil, nil, errf("[%s] 读取 automations 失败：%v", dst.Label, err)
		}
		rows["automations"] = []map[string]any{}
		ids := map[string]bool{}
		for _, r := range autoAll {
			id := asString(r["id"])
			if dstAuto[id] {
				continue
			}
			clone := make(map[string]any, len(r))
			for k, v := range r {
				clone[k] = v
			}
			clone["owner_user_id"] = dstUID
			clone["owner_status"] = "confirmed"
			clone["status"] = "PAUSED" // 防两个 App 各跑一遍
			clone["next_run_at"] = nil
			rows["automations"] = append(rows["automations"], clone)
			ids[id] = true
		}

		dstRuns, err := idSet(dstDB, "SELECT thread_id FROM automation_runs")
		if err != nil {
			return nil, nil, nil, errf("[%s] 读取 automation_runs 失败：%v", dst.Label, err)
		}
		runs, err := queryDicts(srcDB, "SELECT * FROM automation_runs")
		if err != nil {
			return nil, nil, nil, errf("[%s] 读取 automation_runs 失败：%v", src.Label, err)
		}
		rows["automation_runs"] = []map[string]any{}
		for _, r := range runs {
			if ids[asString(r["automation_id"])] && !dstRuns[asString(r["thread_id"])] {
				rows["automation_runs"] = append(rows["automation_runs"], r)
			}
		}

		dstState, err := idSet(dstDB, "SELECT automation_id FROM automation_runtime_state")
		if err != nil {
			return nil, nil, nil, errf("[%s] 读取 automation_runtime_state 失败：%v", dst.Label, err)
		}
		states, err := queryDicts(srcDB, "SELECT * FROM automation_runtime_state")
		if err != nil {
			return nil, nil, nil, errf("[%s] 读取 automation_runtime_state 失败：%v", src.Label, err)
		}
		rows["automation_runtime_state"] = []map[string]any{}
		for _, r := range states {
			aid := asString(r["automation_id"])
			if ids[aid] && !dstState[aid] {
				clone := make(map[string]any, len(r))
				for k, v := range r {
					clone[k] = v
				}
				clone["running"] = int64(0)
				rows["automation_runtime_state"] = append(rows["automation_runtime_state"], clone)
			}
		}
	}

	// 裁剪到目标库实际存在的列
	cleaned := map[string][]map[string]any{}
	for table, tableRows := range rows {
		cols := TargetColumns(dstDB, table)
		if len(cols) == 0 {
			continue
		}
		out := make([]map[string]any, 0, len(tableRows))
		for _, r := range tableRows {
			item := make(map[string]any)
			for _, c := range cols {
				if v, ok := r[c]; ok {
					item[c] = v
				}
			}
			out = append(out, item)
		}
		cleaned[table] = out
	}
	if _, ok := cleaned["sessions"]; !ok {
		cleaned["sessions"] = []map[string]any{}
	}
	return cleaned, skipped, fingerprints, nil
}

// BuildDirectionEntries 生成一个方向的全部文件/目录动作。
//
// **顺序是哈希契约的一部分**：Python 侧按同样的次序 append，
// 换了顺序 plan_id 就不同了，所以这里的循环结构刻意与之一一对应。
func BuildDirectionEntries(src, dst *Home, srcUID, dstUID string, sessionIDs []string,
	options map[string]any) ([]PlanEntry, map[string]int, error) {
	entries := []PlanEntry{}
	stats := map[string]int{"sessions": 0, "missing_assets": 0, "cwd_invalid": 0}

	addFile := func(srcRel, dstRel, note string) {
		s := filepath.Join(src.Path, srcRel)
		if _, err := os.Stat(s); err == nil {
			entries = append(entries, PlanEntry{
				Kind: "file", Src: s, Dst: filepath.Join(dst.Path, dstRel),
				Mode: "copy_if_missing", Note: note,
			})
			return
		}
		stats["missing_assets"]++
	}
	addTree := func(srcRel, dstRel, note string) {
		s := filepath.Join(src.Path, srcRel)
		if info, err := os.Stat(s); err == nil && info.IsDir() {
			entries = append(entries, PlanEntry{
				Kind: "tree", Src: s, Dst: filepath.Join(dst.Path, dstRel),
				Mode: "copy_tree_if_missing", Note: note,
			})
		}
	}

	db, err := src.Connect()
	if err != nil {
		return nil, nil, err
	}
	meta := map[string]string{}
	{
		rows, err := db.Query("SELECT id, cwd FROM sessions WHERE deleted_at IS NULL")
		if err != nil {
			db.Close()
			return nil, nil, errf("[%s] 读取 sessions 失败：%v", src.Label, err)
		}
		for rows.Next() {
			var id, cwd sql.NullString
			if err := rows.Scan(&id, &cwd); err != nil {
				continue
			}
			meta[id.String] = cwd.String
		}
		rows.Close()
	}
	db.Close()

	unique := map[string]bool{}
	for _, cid := range sessionIDs {
		unique[cid] = true
	}
	sorted := make([]string, 0, len(unique))
	for cid := range unique {
		sorted = append(sorted, cid)
	}
	sort.Strings(sorted)

	for _, cid := range sorted {
		cwd, ok := meta[cid]
		if !ok {
			continue
		}
		stats["sessions"]++
		if info, err := os.Stat(cwd); err != nil || !info.IsDir() {
			stats["cwd_invalid"]++
		}

		for _, slug := range ProjectSlugFor(src, cid) {
			for _, suffix := range projectSessionSuffixes {
				rel := filepath.Join("projects", slug, cid+suffix)
				addFile(rel, rel, "conversation")
			}
			rel := filepath.Join("projects", slug, cid)
			addTree(rel, rel, "tool-results")
		}

		for _, item := range perSessionTrees {
			if item.NeedsChanges && !truthy(options["include_changes"]) {
				continue
			}
			rel := strings.ReplaceAll(item.Template, "{cid}", cid)
			addTree(rel, rel, "session asset")
		}
		for _, tmpl := range perSessionFiles {
			rel := strings.ReplaceAll(tmpl, "{cid}", cid)
			addFile(rel, rel, "session asset")
		}
	}

	// 全局并集
	for _, name := range baseUnionTrees {
		addTree(name, name, "content-addressed union")
	}
	for _, item := range optionalUnionTrees {
		if truthy(options[item.Flag]) {
			addTree(item.Name, item.Name, "union")
		}
	}

	// 连接器技能定义
	if truthy(options["include_connectors"]) {
		addTree("connectors/skills", "connectors/skills", "connector skills")
	}

	// 账号个人存储：源 uid 目录 → 目标 uid 目录
	if truthy(options["include_storage"]) {
		for _, suffix := range []string{"", "-personal"} {
			addTree("storage/user-"+srcUID+suffix, "storage/user-"+dstUID+suffix,
				"account storage (merge-if-missing)")
		}
	}

	// 连接器：只并状态与 mcp 配置，绝不搬凭据
	if truthy(options["include_connectors"]) {
		for _, name := range connectorSharedFiles {
			addFile(filepath.Join("connectors", srcUID, name),
				filepath.Join("connectors", dstUID, name),
				"connector state (no credentials)")
		}
	}

	return entries, stats, nil
}

func contains(list []string, want string) bool {
	for _, item := range list {
		if item == want {
			return true
		}
	}
	return false
}

func truthy(v any) bool {
	b, ok := v.(bool)
	return ok && b
}

func asString(v any) string {
	switch t := v.(type) {
	case string:
		return t
	case []byte:
		return string(t)
	}
	return ""
}
