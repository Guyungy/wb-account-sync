package pyjson

import (
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
)

// 保序 JSON 文档。
//
// 为什么不能直接用 map[string]any 往返一遍 settings.json：
//
//  1. Go 的 map 没有键序。Python 的 dict 保留 JSON 解析时的出现顺序，
//     所以 `json.load` → 改一处 → `json.dump` 之后，文件里除了改动点
//     以外逐行不变。换成 Go map 再编码，整个文件会被重排成字典序——
//     一个"加了一个渠道绑定"的操作会让 settings.json 全文件变红。
//  2. 数字。json.Unmarshal 到 any 会把所有数字变成 float64，
//     超过 2^53 的整数直接丢精度（时间戳、配额、id 都可能中招）。
//     这里保留原始数字文本，一个字节都不动。
//
// 唯一的已知差异：Python 会把 `1E5` 规范化成 `100000.0`，这里原样保留。
// 对 JS 的 JSON.stringify 产物（客户端实际写法）两者输出相同。
type Node struct {
	Kind NodeKind
	Str  string
	Num  string
	Bool bool
	Arr  []*Node
	Obj  []Member
}

type Member struct {
	Key string
	Val *Node
}

type NodeKind int

const (
	KindNull NodeKind = iota
	KindBool
	KindNumber
	KindString
	KindArray
	KindObject
)

// ParseOrdered 解析 JSON 并保留键序与数字原文。
func ParseOrdered(raw []byte) (*Node, error) {
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.UseNumber()
	node, err := decodeNode(dec)
	if err != nil {
		return nil, err
	}
	// 与 Python json.load 一致：尾部多余内容视为错误。
	if _, err := dec.Token(); err != io.EOF {
		return nil, fmt.Errorf("JSON 尾部有多余内容")
	}
	return node, nil
}

func decodeNode(dec *json.Decoder) (*Node, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	return nodeFromToken(dec, tok)
}

func nodeFromToken(dec *json.Decoder, tok json.Token) (*Node, error) {
	switch t := tok.(type) {
	case nil:
		return &Node{Kind: KindNull}, nil
	case bool:
		return &Node{Kind: KindBool, Bool: t}, nil
	case string:
		return &Node{Kind: KindString, Str: t}, nil
	case json.Number:
		return &Node{Kind: KindNumber, Num: t.String()}, nil
	case json.Delim:
		switch t {
		case '{':
			obj := &Node{Kind: KindObject, Obj: []Member{}}
			for dec.More() {
				keyTok, err := dec.Token()
				if err != nil {
					return nil, err
				}
				key, ok := keyTok.(string)
				if !ok {
					return nil, fmt.Errorf("对象键不是字符串")
				}
				val, err := decodeNode(dec)
				if err != nil {
					return nil, err
				}
				obj.Obj = append(obj.Obj, Member{Key: key, Val: val})
			}
			if _, err := dec.Token(); err != nil { // 吃掉 '}'
				return nil, err
			}
			return obj, nil
		case '[':
			arr := &Node{Kind: KindArray, Arr: []*Node{}}
			for dec.More() {
				item, err := decodeNode(dec)
				if err != nil {
					return nil, err
				}
				arr.Arr = append(arr.Arr, item)
			}
			if _, err := dec.Token(); err != nil { // 吃掉 ']'
				return nil, err
			}
			return arr, nil
		}
	}
	return nil, fmt.Errorf("无法识别的 JSON token：%v", tok)
}

// Get 按键取子节点，找不到返回 nil。
func (n *Node) Get(key string) *Node {
	if n == nil || n.Kind != KindObject {
		return nil
	}
	for _, m := range n.Obj {
		if m.Key == key {
			return m.Val
		}
	}
	return nil
}

// PlainString 把字符串节点取成 Go 字符串；非字符串返回空串。
// undo 记录里的都是字符串，用它可以省掉一层类型断言。
func (n *Node) PlainString() string {
	if n == nil || n.Kind != KindString {
		return ""
	}
	return n.Str
}

// Set 就地写入；键已存在则替换，否则**追加到末尾**——
// 与 Python `d[k] = v` 的落点一致。
func (n *Node) Set(key string, val *Node) {
	for i := range n.Obj {
		if n.Obj[i].Key == key {
			n.Obj[i].Val = val
			return
		}
	}
	n.Obj = append(n.Obj, Member{Key: key, Val: val})
}

// NewObject 造一个空对象节点。
func NewObject() *Node { return &Node{Kind: KindObject, Obj: []Member{}} }

// Keys 返回顶层键序。
func (n *Node) Keys() []string {
	if n == nil || n.Kind != KindObject {
		return nil
	}
	out := make([]string, 0, len(n.Obj))
	for _, m := range n.Obj {
		out = append(out, m.Key)
	}
	return out
}

// EncodeOrdered 按给定缩进输出，键序原样保留。
func EncodeOrdered(n *Node, indent int) string {
	var sb strings.Builder
	encodeNodeOrdered(&sb, n, indent, 0)
	return sb.String()
}

func encodeNodeOrdered(sb *strings.Builder, n *Node, indent, depth int) {
	if n == nil {
		sb.WriteString("null")
		return
	}
	switch n.Kind {
	case KindNull:
		sb.WriteString("null")
	case KindBool:
		if n.Bool {
			sb.WriteString("true")
		} else {
			sb.WriteString("false")
		}
	case KindNumber:
		sb.WriteString(n.Num)
	case KindString:
		encodeString(sb, n.Str)
	case KindArray:
		if len(n.Arr) == 0 {
			sb.WriteString("[]")
			return
		}
		sb.WriteByte('[')
		for i, item := range n.Arr {
			if i > 0 {
				sb.WriteByte(',')
			}
			pad(sb, indent, depth+1)
			encodeNodeOrdered(sb, item, indent, depth+1)
		}
		pad(sb, indent, depth)
		sb.WriteByte(']')
	case KindObject:
		if len(n.Obj) == 0 {
			sb.WriteString("{}")
			return
		}
		sb.WriteByte('{')
		for i, m := range n.Obj {
			if i > 0 {
				sb.WriteByte(',')
			}
			pad(sb, indent, depth+1)
			encodeString(sb, m.Key)
			sb.WriteString(": ")
			encodeNodeOrdered(sb, m.Val, indent, depth+1)
		}
		pad(sb, indent, depth)
		sb.WriteByte('}')
	}
}

// ToPlain 转成便于做等值比较的普通值（键序无关）。仅用于测试与展示。
func (n *Node) ToPlain() any {
	if n == nil {
		return nil
	}
	switch n.Kind {
	case KindNull:
		return nil
	case KindBool:
		return n.Bool
	case KindNumber:
		return json.Number(n.Num)
	case KindString:
		return n.Str
	case KindArray:
		out := make([]any, len(n.Arr))
		for i, item := range n.Arr {
			out[i] = item.ToPlain()
		}
		return out
	case KindObject:
		out := map[string]any{}
		for _, m := range n.Obj {
			out[m.Key] = m.Val.ToPlain()
		}
		return out
	}
	return nil
}

// SortedKeys 便于日志与断言时给键排序。
func (n *Node) SortedKeys() []string {
	keys := n.Keys()
	sort.Strings(keys)
	return keys
}
