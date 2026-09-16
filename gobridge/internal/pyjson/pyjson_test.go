package pyjson

import "testing"

// 期望值全部由 CPython 3.13 的
// `json.dumps(v, ensure_ascii=False, sort_keys=True)` 生成，不是手抄的。
// 任何一条不过，都说明 Go 侧的 plan_id 会和 Python 侧对不上。
func TestMarshalMatchesCPython(t *testing.T) {
	cases := []struct {
		name string
		in   any
		want string
	}{
		{"nil", nil, `null`},
		{"true", true, `true`},
		{"false", false, `false`},
		{"intzero", 0, `0`},
		{"intneg", -42, `-42`},
		{"int64", int64(9007199254740993), `9007199254740993`},
		{"strsimple", "abc", `"abc"`},
		{"strquote", `a"b\c`, `"a\"b\\c"`},
		{"strnl", "line1\nline2\ttab", `"line1\nline2\ttab"`},
		{"stru0001", "\u0001", `"\u0001"`},
		{"strip", "a\rb", `"a\rb"`},
		{"strbs", "a\bb\fc", `"a\bb\fc"`},
		{"strcn", "中文 OK", `"中文 OK"`},
		{"stremoji", "🎯", `"🎯"`},
		{"float0", 0.0, `0.0`},
		{"floatneg0", negZero(), `-0.0`},
		{"float01", 0.1, `0.1`},
		{"float1e15", 1e15, `1000000000000000.0`},
		{"float1e16", 1e16, `1e+16`},
		{"float1e-4", 1e-4, `0.0001`},
		{"float1e-5", 1e-5, `1e-05`},
		{"float15e16", 1.5e16, `1.5e+16`},
		{"float123456789", 123456.789, `123456.789`},
		{"float1e100", 1e100, `1e+100`},
		{"float5em324", 5e-324, `5e-324`},
		{"floatcost", 0.009999999999999998, `0.009999999999999998`},
		{
			"dict",
			map[string]any{"b": 1, "a": "x"},
			`{"a": "x", "b": 1}`,
		},
		{
			"dictunicode",
			map[string]any{"中文": 1, "a": 2},
			`{"a": 2, "中文": 1}`,
		},
		{
			"list",
			[]any{1, "a", nil, true},
			`[1, "a", null, true]`,
		},
		{
			"nested",
			map[string]any{"z": []any{1, map[string]any{"y": nil}}, "a": map[string]any{"k": 2.5}},
			`{"a": {"k": 2.5}, "z": [1, {"y": null}]}`,
		},
		{
			"empties",
			map[string]any{"e": map[string]any{}, "l": []any{}},
			`{"e": {}, "l": []}`,
		},
		{
			"ordered",
			OrderedMap{{"kind", "file"}, {"mode", "copy_if_missing"}},
			`{"kind": "file", "mode": "copy_if_missing"}`,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Marshal(tc.in)
			if err != nil {
				t.Fatalf("Marshal 出错：%v", err)
			}
			if got != tc.want {
				t.Fatalf("\n got: %s\nwant: %s", got, tc.want)
			}
		})
	}
}

func negZero() float64 {
	z := 0.0
	return -z
}

func TestSHA256TextMatchesEmptyString(t *testing.T) {
	// sha256("") 的已知值，用来钉住编码方式（hex 小写、UTF-8）。
	const want = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
	if got := SHA256Text(""); got != want {
		t.Fatalf("got %s want %s", got, want)
	}
}

// 中文必须按 UTF-8 原始字节参与哈希，而不是 \uXXXX 转义形式。
func TestSHA256TextUsesUTF8Bytes(t *testing.T) {
	cn := SHA256Text("中文")
	escaped := SHA256Text(`\u4e2d\u6587`)
	if cn == escaped {
		t.Fatal("中文 与 \\u4e2d\\u6587 的哈希不应相同——说明按转义形式编码了")
	}
}
