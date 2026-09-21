package bridge

import (
	"sort"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/pyjson"
)

// RowHashes 是给跨实现对账用的调试入口：返回
// `会话 id → sha256(pyjson(row))`，以及整份集合的哈希。
//
// 存在的意义：Go 与 Python 算出的 plan_id 一旦不一致，用它能立刻定位到
// **是哪一行**的编码不同，而不是对着一整份计划猜。
func (h *Home) RowHashes() (map[string]string, string, error) {
	db, err := h.Connect()
	if err != nil {
		return nil, "", err
	}
	defer db.Close()
	list, err := queryDicts(db, "SELECT * FROM sessions WHERE deleted_at IS NULL")
	if err != nil {
		return nil, "", err
	}
	perRow := map[string]string{}
	all := map[string]any{}
	for _, row := range list {
		id := asString(row["id"])
		perRow[id] = pyjson.HashJSON(row)
		all[id] = row
	}
	return perRow, pyjson.HashJSON(all), nil
}

// Columns 返回 sessions 表的列名，用于排查"两边列集合不同"这类差异。
func (h *Home) Columns(table string) ([]string, error) {
	db, err := h.Connect()
	if err != nil {
		return nil, err
	}
	defer db.Close()
	cols := TargetColumns(db, table)
	sort.Strings(cols)
	return cols, nil
}

// RowDetail 返回某一行的规范化 JSON（含每个字段的类型名），
// 用于看清"值一样但类型不同"这种最容易漏的差异。
func (h *Home) RowDetail(id string) (string, map[string]string, error) {
	db, err := h.Connect()
	if err != nil {
		return "", nil, err
	}
	defer db.Close()
	list, err := queryDicts(db, "SELECT * FROM sessions WHERE id = ?", id)
	if err != nil {
		return "", nil, err
	}
	if len(list) == 0 {
		return "", nil, nil
	}
	row := list[0]
	types := map[string]string{}
	for k, v := range row {
		types[k] = typeName(v)
	}
	return pyjson.MustMarshal(row), types, nil
}

func typeName(v any) string {
	switch v.(type) {
	case nil:
		return "NoneType"
	case string:
		return "str"
	case int64:
		return "int"
	case float64:
		return "float"
	case bool:
		return "bool"
	case []byte:
		return "bytes"
	}
	return "other"
}
