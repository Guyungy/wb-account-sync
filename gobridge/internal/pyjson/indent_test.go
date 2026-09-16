package pyjson

import "testing"

// 这些字符串是用 CPython 跑
// json.dumps(v, ensure_ascii=False, sort_keys=True, indent=2) 抓下来的原文，
// 一个空格都不能差 —— undo.json 与记忆块的 RAW_JSON 段靠它逐字节还原。
func TestMarshalIndentMatchesCPython(t *testing.T) {
	cases := []struct {
		name string
		val  any
		want string
	}{
		{"empty_dict", map[string]any{}, `{}`},
		{"empty_list", []any{}, `[]`},
		{
			"nested_empty",
			map[string]any{"a": map[string]any{}, "b": []any{}, "c": []any{map[string]any{}}},
			"{\n  \"a\": {},\n  \"b\": [],\n  \"c\": [\n    {}\n  ]\n}",
		},
		{
			"flat",
			map[string]any{"uid": "u1", "memoryBlock": "x", "updatedAt": "2026-01-01T00:00:00+08:00"},
			"{\n  \"memoryBlock\": \"x\",\n  \"uid\": \"u1\",\n  \"updatedAt\": \"2026-01-01T00:00:00+08:00\"\n}",
		},
		{
			"undo",
			map[string]any{
				"plan_id": "abc",
				"db": []any{map[string]any{
					"home": "/h", "table": "sessions", "pk": "id", "ids": []any{"a", "b"},
				}},
				"files":       []any{"/x"},
				"trees":       []any{},
				"merged_dirs": []any{"/m"},
			},
			"{\n" +
				"  \"db\": [\n    {\n      \"home\": \"/h\",\n      \"ids\": [\n        \"a\",\n        \"b\"\n      ],\n      \"pk\": \"id\",\n      \"table\": \"sessions\"\n    }\n  ],\n" +
				"  \"files\": [\n    \"/x\"\n  ],\n" +
				"  \"merged_dirs\": [\n    \"/m\"\n  ],\n" +
				"  \"plan_id\": \"abc\",\n" +
				"  \"trees\": []\n" +
				"}",
		},
		{
			"unicode",
			map[string]any{"中文标题": "值", "emoji": "🎯"},
			"{\n  \"emoji\": \"🎯\",\n  \"中文标题\": \"值\"\n}",
		},
		{
			"liststr",
			map[string]any{"ids": []string{"a", "b", "c"}},
			"{\n  \"ids\": [\n    \"a\",\n    \"b\",\n    \"c\"\n  ]\n}",
		},
	}
	for _, c := range cases {
		got, err := MarshalIndent(c.val, 2)
		if err != nil {
			t.Fatalf("%s: 编码失败：%v", c.name, err)
		}
		if got != c.want {
			t.Errorf("%s 与 CPython 不一致：\n got=%q\nwant=%q", c.name, got, c.want)
		}
	}
}

// 浮点走的是同一套 repr 规则，缩进模式下不能换成别的写法。
func TestMarshalIndentFloatRuleShared(t *testing.T) {
	got, err := MarshalIndent(map[string]any{"big": 1e16, "f": 0.1}, 2)
	if err != nil {
		t.Fatal(err)
	}
	want := "{\n  \"big\": 1e+16,\n  \"f\": 0.1\n}"
	if got != want {
		t.Errorf("got=%q want=%q", got, want)
	}
}
