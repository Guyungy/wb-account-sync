package bridge

import (
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/pyjson"
)

// 记忆文件是给人看的 Markdown + 给机器读的 RAW_JSON 段混排。
// 合并时只能动正文那部分，RAW_JSON 必须按目标账号重写，否则新 home 会
// 读到旧 uid 的记忆。
// 注意第一个 sub **没有** MULTILINE：它只削掉文件最开头那一行 `# ...`，
// 正文中间出现 `#` 的行是正文，不能碰。
var (
	reRawJSON    = regexp.MustCompile(`(?s)RAW_JSON_START(.*?)RAW_JSON_END`)
	reLeadHash   = regexp.MustCompile(`^#[^\n]*\n`)
	reQuoteLine  = regexp.MustCompile(`(?m)^>[^\n]*$`)
	reMemoryHead = regexp.MustCompile(`(?m)^##\s*Memory Block\s*$`)
)

// memoryLines 抽出正文里的非空行。
func memoryLines(raw string) []string {
	body := raw
	if m := reRawJSON.FindStringIndex(raw); m != nil {
		body = raw[:m[0]]
	}
	body = reLeadHash.ReplaceAllString(body, "")
	body = reQuoteLine.ReplaceAllString(body, "")
	body = reMemoryHead.ReplaceAllString(body, "")
	body = strings.ReplaceAll(body, "---", "")

	out := []string{}
	for _, ln := range splitLines(body) {
		if s := strings.TrimSpace(ln); s != "" {
			out = append(out, s)
		}
	}
	return out
}

// splitLines 近似 Python str.splitlines()。
// 只按 \n 切是不够的：个别编辑器会把记忆文件写成 CRLF，
// 那样行尾会留一个 \r，去重时同一行会被算成两行。
func splitLines(s string) []string {
	out := []string{}
	var sb strings.Builder
	for i := 0; i < len(s); {
		r, size := decodeRune(s[i:])
		switch r {
		case '\n', '\v', '\f', 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029:
			out = append(out, sb.String())
			sb.Reset()
			i += size
		case '\r':
			out = append(out, sb.String())
			sb.Reset()
			i += size
			if i < len(s) && s[i] == '\n' {
				i++
			}
		default:
			sb.WriteString(s[i : i+size])
			i += size
		}
	}
	if sb.Len() > 0 {
		out = append(out, sb.String())
	}
	return out
}

// MemoryMerge 把源记忆并入目标记忆：非空行去重并集，改写 uid，保持文件结构。
//
// 返回新增段数。目标文件会被整体重写为统一模板——这是刻意的：
// 两个 App 各自维护的记忆文件模板可能来自不同版本，保留谁的模板都会
// 让另一方下次解析出错，统一成当前模板最稳。
func MemoryMerge(srcFile, dstFile, dstUID string) (string, int, error) {
	srcRaw, err := os.ReadFile(srcFile)
	if err != nil {
		return "", 0, err
	}
	dstRaw := ""
	if data, rerr := os.ReadFile(dstFile); rerr == nil {
		dstRaw = string(data)
	}

	merged := memoryLines(dstRaw)
	seen := map[string]bool{}
	for _, ln := range merged {
		seen[ln] = true
	}
	added := 0
	for _, ln := range memoryLines(string(srcRaw)) {
		if !seen[ln] {
			merged = append(merged, ln)
			seen[ln] = true
			added++
		}
	}

	body := strings.Join(merged, "\n\n")
	now := time.Now().Format("2006-01-02T15:04:05") + "+08:00"
	version := 0
	if len(merged) > 0 {
		version = 1
	}

	// 键序必须是 uid → memoryBlock → updatedAt。
	// Python 侧是 json.dumps(不带 sort_keys)，吃的是 dict 的插入序；
	// 这里如果用 map 就会被按字典序排成 memoryBlock 打头，两边文件对不上。
	rawJSON := pyjson.MustMarshalIndent(pyjson.OrderedMap{
		{Key: "uid", Val: dstUID},
		{Key: "memoryBlock", Val: body},
		{Key: "updatedAt", Val: now},
	}, 2)

	var sb strings.Builder
	sb.WriteString("# User Memory Profile\n")
	sb.WriteString("> Last updated: " + now + "\n")
	sb.WriteString("> Version: " + strconv.Itoa(version) + "\n")
	sb.WriteString("\n## Memory Block\n\n")
	sb.WriteString(body + "\n\n\n---\n\n<!-- RAW_JSON_START\n")
	sb.WriteString(rawJSON)
	sb.WriteString("\nRAW_JSON_END -->\n")

	if err := os.MkdirAll(dirOf(dstFile), 0o755); err != nil {
		return "", 0, err
	}
	if err := os.WriteFile(dstFile, []byte(sb.String()), 0o644); err != nil {
		return "", 0, err
	}
	return "merged", added, nil
}

func dirOf(p string) string {
	i := strings.LastIndexByte(p, '/')
	if i <= 0 {
		return "."
	}
	return p[:i]
}

// decodeRune 只用于 splitLines 的逐字符扫描；非法字节按单字节步进，
// 保证不会因为一个坏字节就把后面的内容全丢掉。
func decodeRune(s string) (rune, int) {
	if len(s) == 0 {
		return 0, 0
	}
	r, size := utf8.DecodeRuneInString(s)
	if r == utf8.RuneError && size <= 1 {
		return rune(s[0]), 1
	}
	return r, size
}

// --------------------------------------------------------------------------
// settings.json 渠道绑定（claw.users）
// --------------------------------------------------------------------------

// MergeClawUsers 把源 settings.json 里新增的账号条目并进目标。
//
// 只并 claw.users，绝不碰凭据类字段；整份文件走保序往返，
// 避免"加一条绑定"变成"重写用户整个配置文件"。
func MergeClawUsers(srcPath, dstPath string) ([]string, error) {
	srcRaw, err := os.ReadFile(srcPath)
	if err != nil {
		return nil, nil
	}
	dstRaw, err := os.ReadFile(dstPath)
	if err != nil {
		return nil, nil
	}
	srcDoc, err := pyjson.ParseOrdered(srcRaw)
	if err != nil {
		return nil, errf("解析 %s 失败：%v", srcPath, err)
	}
	dstDoc, err := pyjson.ParseOrdered(dstRaw)
	if err != nil {
		return nil, errf("解析 %s 失败：%v", dstPath, err)
	}

	srcUsers := ensureObjectPath(srcDoc, "claw", "users")
	dstUsers := ensureObjectPath(dstDoc, "claw", "users")
	if srcUsers == nil || dstUsers == nil {
		return nil, nil
	}

	added := []string{}
	for _, m := range srcUsers.Obj {
		if dstUsers.Get(m.Key) == nil {
			dstUsers.Set(m.Key, m.Val)
			added = append(added, m.Key)
		}
	}
	if len(added) == 0 {
		return nil, nil
	}

	// 先留一份带时间戳的备份再落盘：改写用户配置属于"出错很难查"的操作。
	if err := copy2(dstPath, dstPath+".before-bridge-"+strconv.FormatInt(time.Now().Unix(), 10)); err != nil {
		return nil, err
	}
	if err := os.WriteFile(dstPath, []byte(pyjson.EncodeOrdered(dstDoc, 2)), 0o644); err != nil {
		return nil, err
	}
	return added, nil
}

// ensureObjectPath 沿路径下钻，缺失的中间层级按 Python 的
// `d.setdefault(k, {})` 语义就地补出来（补在末尾，保持键序）。
func ensureObjectPath(n *pyjson.Node, path ...string) *pyjson.Node {
	cur := n
	for _, key := range path {
		next := cur.Get(key)
		if next == nil || next.Kind != pyjson.KindObject {
			if next != nil && next.Kind != pyjson.KindObject {
				// 存在的值不是对象：Python 的 setdefault 此时会原样返回该值，
				// 后续 .setdefault 会抛 AttributeError。这里选择不覆盖用户数据。
				return nil
			}
			next = pyjson.NewObject()
			cur.Set(key, next)
		}
		cur = next
	}
	return cur
}
