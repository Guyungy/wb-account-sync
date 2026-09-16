// Package pyjson 复刻 CPython json.dumps 的默认输出，用来算跨实现可比的哈希。
//
// 为什么不能直接用 encoding/json：plan_id 是 Python 侧对
// `json.dumps(body, ensure_ascii=False, sort_keys=True)` 做 SHA-256 得到的。
// Go 的 encoding/json 有两个不可调和的差异——
//
//  1. 分隔符。CPython 默认是 `", "` 和 `": "`（带空格），Go 一个空格都不给。
//  2. 浮点。CPython 用 repr 规则（-4 <= exp < 16 用定点，否则科学计数），
//     Go 的 strconv 'g' 在指数 >= 15 就切科学计数，1e15 会写成 `1e+15`。
//
// 哈希差一个字节就是完全不同的一串，所以这里按 CPython 的规则自己编码。
// 只要两个实现对同一份数据吐出同一个 plan_id，"移植正确"就是可证的，
// 而不是"看起来差不多"。
package pyjson

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Marshal 等价于 Python 的 json.dumps(v, ensure_ascii=False, sort_keys=True)。
func Marshal(v any) (string, error) {
	var sb strings.Builder
	if err := encode(&sb, v); err != nil {
		return "", err
	}
	return sb.String(), nil
}

// MustMarshal 供内部调用；出错直接 panic（调用方已保证结构可控）。
func MustMarshal(v any) string {
	s, err := Marshal(v)
	if err != nil {
		panic(err)
	}
	return s
}

// SHA256Text 与 Python 侧 `sha256(text.encode("utf-8")).hexdigest()` 一致。
func SHA256Text(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])
}

// HashJSON 把值按 CPython 规则编码后取 SHA-256。
func HashJSON(v any) string { return SHA256Text(MustMarshal(v)) }

func encode(sb *strings.Builder, v any) error {
	switch t := v.(type) {
	case nil:
		sb.WriteString("null")
	case bool:
		if t {
			sb.WriteString("true")
		} else {
			sb.WriteString("false")
		}
	case string:
		encodeString(sb, t)
	case int:
		sb.WriteString(strconv.Itoa(t))
	case int64:
		sb.WriteString(strconv.FormatInt(t, 10))
	case float64:
		sb.WriteString(Float(t))
	case float32:
		sb.WriteString(Float(float64(t)))
	case []byte:
		// CPython 遇到 bytes 会抛 TypeError（说明真实数据里没有非空 BLOB），
		// 这里退化成原始字符串，保证不因一个意外列整份计划生成失败。
		encodeString(sb, string(t))
	case []any:
		sb.WriteByte('[')
		for i, item := range t {
			if i > 0 {
				sb.WriteString(", ")
			}
			if err := encode(sb, item); err != nil {
				return err
			}
		}
		sb.WriteByte(']')
	case map[string]any:
		return encodeMap(sb, t)
	case OrderedMap:
		return encodeOrdered(sb, t)
	default:
		return fmt.Errorf("pyjson: 不支持的类型 %T", v)
	}
	return nil
}

// OrderedMap 用于需要固定键序的场景（普通 map 一律按键排序，与 sort_keys 一致）。
type OrderedMap []KV

// KV 是 OrderedMap 的一项。
type KV struct {
	Key string
	Val any
}

func encodeMap(sb *strings.Builder, m map[string]any) error {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	// Python 的 sort_keys 按码点排序；UTF-8 的字节序与码点序一致。
	sort.Strings(keys)
	sb.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			sb.WriteString(", ")
		}
		encodeString(sb, k)
		sb.WriteString(": ")
		if err := encode(sb, m[k]); err != nil {
			return err
		}
	}
	sb.WriteByte('}')
	return nil
}

func encodeOrdered(sb *strings.Builder, m OrderedMap) error {
	sb.WriteByte('{')
	for i, kv := range m {
		if i > 0 {
			sb.WriteString(", ")
		}
		encodeString(sb, kv.Key)
		sb.WriteString(": ")
		if err := encode(sb, kv.Val); err != nil {
			return err
		}
	}
	sb.WriteByte('}')
	return nil
}

// encodeString 复刻 CPython 的 py_encode_basestring（ensure_ascii=False）：
// 只转义 `"`、`\` 和 C0 控制字符，非 ASCII 原样输出。
func encodeString(sb *strings.Builder, s string) {
	sb.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			sb.WriteString(`\"`)
		case '\\':
			sb.WriteString(`\\`)
		case '\n':
			sb.WriteString(`\n`)
		case '\r':
			sb.WriteString(`\r`)
		case '\t':
			sb.WriteString(`\t`)
		case '\b':
			sb.WriteString(`\b`)
		case '\f':
			sb.WriteString(`\f`)
		default:
			if r < 0x20 {
				sb.WriteString(fmt.Sprintf(`\u%04x`, r))
			} else if r == utf8.RuneError {
				// 保留原始的非法字节序列，与 Python 的 surrogateescape 行为一致。
				sb.WriteRune(r)
			} else {
				sb.WriteRune(r)
			}
		}
	}
	sb.WriteByte('"')
}

// Float 复刻 CPython 的 float repr。
//
// 两边取"最短可回环十进制"的数字是同一套（Gay 算法），所以先借 Go 拿到这个
// 数字，再按 CPython 的定点/科学计数切换规则排版。
func Float(f float64) string {
	if math.IsNaN(f) {
		return "NaN"
	}
	if math.IsInf(f, 1) {
		return "Infinity"
	}
	if math.IsInf(f, -1) {
		return "-Infinity"
	}
	// 先走整数，避免 1.0 被写成 "1.0" 之外的东西。
	if f == 0 {
		if math.Signbit(f) {
			return "-0.0"
		}
		return "0.0"
	}
	// 科学计数法拿最短尾数：-1.234e+05
	sci := strconv.FormatFloat(f, 'e', -1, 64)
	mant, expStr, _ := strings.Cut(sci, "e")
	exp, err := strconv.Atoi(expStr)
	if err != nil {
		return sci
	}
	neg := strings.HasPrefix(mant, "-")
	mant = strings.TrimPrefix(mant, "-")
	digits := strings.Replace(mant, ".", "", 1)

	// CPython：-4 <= exp < 16 用定点，其余用科学计数。
	if exp >= -4 && exp < 16 {
		return sign(neg) + fixed(digits, exp)
	}
	head := digits[:1]
	rest := digits[1:]
	if rest != "" {
		return sign(neg) + head + "." + rest + "e" + expPart(exp)
	}
	return sign(neg) + head + "e" + expPart(exp)
}

func sign(neg bool) string {
	if neg {
		return "-"
	}
	return ""
}

// expPart 生成 `+05` / `-05` 形式的指数，Python 至少两位。
func expPart(exp int) string {
	s := "+"
	if exp < 0 {
		s = "-"
		exp = -exp
	}
	if exp < 10 {
		return s + "0" + strconv.Itoa(exp)
	}
	return s + strconv.Itoa(exp)
}

// fixed 把 `1234` + exp=5（即 1.234e5）排成定点形式 `123400.0`。
func fixed(digits string, exp int) string {
	if exp < 0 {
		return "0." + strings.Repeat("0", -exp-1) + digits
	}
	if exp+1 >= len(digits) {
		return digits + strings.Repeat("0", exp+1-len(digits)) + ".0"
	}
	return digits[:exp+1] + "." + digits[exp+1:]
}
