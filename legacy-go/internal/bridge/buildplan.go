package bridge

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// DefaultOptions 是一键同步默认使用的范围，与 Python 版 UI 的默认勾选一致。
func DefaultOptions() map[string]any {
	return map[string]any{
		"include_changes":     true,
		"include_skills":      true,
		"include_plugins":     false,
		"include_automations": false,
		"include_storage":     false,
		"include_connectors":  false,
		"include_claw":        true,
		"include_memory":      true,
		"overwrite_assets":    false,
	}
}

// BuildPlan 扫两边数据，产出一份冻结了指纹的计划。
func BuildPlan(a, b *Home, options map[string]any) (*Plan, error) {
	if err := a.RequireValid(); err != nil {
		return nil, err
	}
	if err := b.RequireValid(); err != nil {
		return nil, err
	}
	uidA, err := a.CurrentUID()
	if err != nil {
		return nil, err
	}
	uidB, err := b.CurrentUID()
	if err != nil {
		return nil, err
	}

	plan := &Plan{
		Version: Version,
		HomeA:   a.Path,
		HomeB:   b.Path,
		LabelA:  a.Label,
		LabelB:  b.Label,
		UIDA:    uidA,
		UIDB:    uidB,
		// Python 的 strftime("%Y-%m-%dT%H:%M:%S%z") 等价写法。
		CreatedAt:   time.Now().Format("2006-01-02T15:04:05-0700"),
		Options:     options,
		Rows:        map[string]map[string][]map[string]any{},
		Skipped:     map[string]map[string]int{},
		Fingerprint: map[string]map[string]string{},
		Summary:     map[string]any{},
	}

	var (
		approx        int64
		totalSessions int
	)
	for _, dir := range []struct {
		side   string
		src    *Home
		dst    *Home
		srcUID string
		dstUID string
	}{
		{"a2b", a, b, uidA, uidB},
		{"b2a", b, a, uidB, uidA},
	} {
		rows, skipped, fingerprints, err := CollectRows(dir.src, dir.dst, dir.dstUID, options)
		if err != nil {
			return nil, err
		}
		plan.Rows[dir.side] = rows
		plan.Skipped[dir.side] = skipped
		plan.Fingerprint[dir.side] = fingerprints

		cids := make([]string, 0, len(rows["sessions"]))
		for _, r := range rows["sessions"] {
			cids = append(cids, asString(r["id"]))
		}
		totalSessions += len(cids)

		entries, stats, err := BuildDirectionEntries(dir.src, dir.dst, dir.srcUID, dir.dstUID, cids, options)
		if err != nil {
			return nil, err
		}
		fileEntries, treeEntries := 0, 0
		for _, e := range entries {
			switch e.Kind {
			case "file":
				fileEntries++
				if st, err := os.Stat(e.Src); err == nil && !st.IsDir() {
					approx += st.Size()
				}
			case "tree":
				treeEntries++
				approx += DirSize(e.Src)
			}
		}
		plan.Entries = append(plan.Entries, entries...)

		plan.Summary[dir.side] = map[string]any{
			"from":             dir.src.Label,
			"to":               dir.dst.Label,
			"sessions_to_copy": len(cids),
			"sessions_skipped": skipped["sessions"],
			"file_entries":     fileEntries,
			"tree_entries":     treeEntries,
			"missing_assets":   stats["missing_assets"],
			"cwd_invalid":      stats["cwd_invalid"],
		}
	}

	plan.Summary["totals"] = map[string]any{
		"sessions_to_copy": totalSessions,
		"approx_bytes":     approx,
		"approx_human":     Human(float64(approx)),
	}
	plan.Finalize()
	return plan, nil
}

// Survey 是只读盘点：行数、目录体积、技能、孤立会话等。
func (h *Home) Survey() (map[string]any, error) {
	if err := h.RequireValid(); err != nil {
		return nil, err
	}
	counts := map[string]any{}
	sizes := map[string]any{}
	warnings := []any{}
	info := map[string]any{
		"label":       h.Label,
		"app":         h.App,
		"path":        h.Path,
		"uid":         nil,
		"nickname":    "",
		"counts":      counts,
		"sizes":       sizes,
		"invalid_cwd": []any{},
		"warnings":    warnings,
	}

	if uid, err := h.CurrentUID(); err == nil {
		info["uid"] = uid
	} else {
		warnings = append(warnings, err.Error())
	}
	info["nickname"] = h.Nickname()

	db, err := h.Connect()
	if err != nil {
		return nil, err
	}
	tables := append([]string{}, dbSessionTables...)
	tables = append(tables, dbSideTables...)
	tables = append(tables, automationTables...)
	for _, table := range tables {
		if n, ok := TableCount(db, table); ok {
			counts[table] = n
		}
	}

	byUser := map[string]any{}
	badCwd := map[string]int{}
	order := []string{}
	rows, err := db.Query("SELECT cwd, user_id FROM sessions WHERE deleted_at IS NULL")
	if err != nil {
		db.Close()
		return nil, errf("[%s] 读取 sessions 失败：%v", h.Label, err)
	}
	for rows.Next() {
		var cwd, userID string
		if err := rows.Scan(&cwd, &userID); err != nil {
			continue
		}
		if n, ok := byUser[userID].(int); ok {
			byUser[userID] = n + 1
		} else {
			byUser[userID] = 1
		}
		if !isDir(cwd) {
			if badCwd[cwd] == 0 {
				order = append(order, cwd)
			}
			badCwd[cwd]++
		}
	}
	rows.Close()
	db.Close()
	info["sessions_by_user"] = byUser

	// Python 是 sorted(bad_cwd.items(), key=lambda kv: -kv[1])，计数相同者保持
	// 首次出现的顺序（dict 插入序）；这里显式还原同一口径。
	rank := map[string]int{}
	for i, cwd := range order {
		rank[cwd] = i
	}
	sortedCwd := append([]string{}, order...)
	sort.SliceStable(sortedCwd, func(i, j int) bool {
		if badCwd[sortedCwd[i]] != badCwd[sortedCwd[j]] {
			return badCwd[sortedCwd[i]] > badCwd[sortedCwd[j]]
		}
		return rank[sortedCwd[i]] < rank[sortedCwd[j]]
	})
	invalid := make([]any, 0, len(sortedCwd))
	for _, cwd := range sortedCwd {
		invalid = append(invalid, []any{cwd, badCwd[cwd]})
	}
	info["invalid_cwd"] = invalid

	for _, name := range []string{"projects", "tasks", "traces", "blobs", "changes-detail",
		"changes-index", "file-history", "artifact-index", "memory", "skills",
		"connectors", "storage"} {
		sizes[name] = DirSize(filepath.Join(h.Path, name))
	}

	base := filepath.Join(h.Path, "projects")
	if entries, err := os.ReadDir(base); err == nil {
		slugs, convs := 0, 0
		for _, e := range entries {
			if !e.IsDir() {
				continue
			}
			slugs++
			names, err := os.ReadDir(filepath.Join(base, e.Name()))
			if err != nil {
				continue
			}
			for _, n := range names {
				if strings.HasSuffix(n.Name(), ".jsonl") {
					convs++
				}
			}
		}
		counts["projects_buckets"] = slugs
		counts["conversations"] = convs
	}

	orphans, err := ConversationsMissingDBRow(h)
	if err != nil {
		return nil, err
	}
	if len(orphans) > 0 {
		info["orphan_conversations"] = orphans
		warnings = append(warnings, fmt.Sprintf(
			"%d 段对话有正文文件但数据库无对应会话行，本工具不处理。", len(orphans)))
	}
	info["warnings"] = warnings
	info["skills"] = SkillsOf(h)
	return info, nil
}

func isDir(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}
