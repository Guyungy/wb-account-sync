// Command wb-bridge 是 wb-account-sync 的 Go 实现入口。
//
// 目标是把原 Python 版的"底座"整块替掉：不依赖解释器、单文件可执行、
// 交叉编译出 mac/Windows/Linux 三端。
//
// 当前进度和 Python 版逐项对齐；已实现的部分可以通过 plan_id 直接对账——
// 同一份数据两边算出的 plan_id 相同，就说明计划内容逐字节等价。
package main

import (
	"sort"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/bridge"
	"github.com/Guyungy/wb-account-sync/gobridge/internal/platform"
)

const version = "0.3.0a2-go"

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(argv []string) int {
	if len(argv) == 0 {
		usage()
		return 2
	}
	switch argv[0] {
	case "--version", "version":
		fmt.Printf("wb-bridge %s\n", version)
		return 0
	case "-h", "--help", "help":
		usage()
		return 0
	case "status":
		return cmdStatus(argv[1:])
	case "quit-clients":
		return cmdQuitClients(argv[1:])
	case "survey":
		return cmdSurvey(argv[1:])
	case "plan":
		return cmdPlan(argv[1:])
	case "dbg-rows":
		return cmdDbgRows(argv[1:])
	default:
		fmt.Fprintf(os.Stderr, "未知子命令：%s\n\n", argv[0])
		usage()
		return 2
	}
}

func usage() {
	fmt.Fprint(os.Stderr, `wb-bridge —— WorkBuddy / WorkBuddy AI 数据目录打通（Go 版）

用法：
  wb-bridge status        [--json]                       客户端运行状态与数据目录
  wb-bridge quit-clients  [--json] [--force] [--wait 秒]  退出两个客户端并等进程消失
  wb-bridge survey        [--json] [--home-a 路径] [--home-b 路径]
  wb-bridge plan          [--json] [--home-a 路径] [--home-b 路径] [选项]

plan 选项（默认只开 changes / skills / memory / claw）：
  --no-changes --no-skills --no-memory --no-claw
  --include-plugins --include-automations --include-storage
  --include-connectors --overwrite-assets
`)
}

// --------------------------------------------------------------------------
// 参数解析
// --------------------------------------------------------------------------

type flags struct {
	json    bool
	force   bool
	wait    time.Duration
	homeA   string
	homeB   string
	options map[string]any
}

func parseFlags(argv []string) (*flags, error) {
	f := &flags{options: bridge.DefaultOptions()}
	for i := 0; i < len(argv); i++ {
		arg := argv[i]
		next := func() (string, error) {
			if i+1 >= len(argv) {
				return "", fmt.Errorf("%s 需要一个取值", arg)
			}
			i++
			return argv[i], nil
		}
		switch {
		case arg == "--json":
			f.json = true
		case arg == "--force":
			f.force = true
		case arg == "--wait":
			v, err := next()
			if err != nil {
				return nil, err
			}
			secs, err := time.ParseDuration(v + "s")
			if err != nil {
				return nil, fmt.Errorf("--wait 需要秒数：%v", err)
			}
			f.wait = secs
		case arg == "--home-a" || strings.HasPrefix(arg, "--home-a="):
			v, err := valueOf(arg, next)
			if err != nil {
				return nil, err
			}
			f.homeA = v
		case arg == "--home-b" || strings.HasPrefix(arg, "--home-b="):
			v, err := valueOf(arg, next)
			if err != nil {
				return nil, err
			}
			f.homeB = v
		case arg == "--no-changes":
			f.options["include_changes"] = false
		case arg == "--no-skills":
			f.options["include_skills"] = false
		case arg == "--no-memory":
			f.options["include_memory"] = false
		case arg == "--no-claw":
			f.options["include_claw"] = false
		case arg == "--include-plugins":
			f.options["include_plugins"] = true
		case arg == "--include-automations":
			f.options["include_automations"] = true
		case arg == "--include-storage":
			f.options["include_storage"] = true
		case arg == "--include-connectors":
			f.options["include_connectors"] = true
		case arg == "--overwrite-assets":
			f.options["overwrite_assets"] = true
		default:
			return nil, fmt.Errorf("未知参数：%s", arg)
		}
	}
	return f, nil
}

func valueOf(arg string, next func() (string, error)) (string, error) {
	if _, v, ok := strings.Cut(arg, "="); ok {
		return v, nil
	}
	return next()
}

// homes 按平台探测两个数据目录，显式指定优先。
func homes(f *flags) (*bridge.Home, *bridge.Home, error) {
	build := func(key, explicit string) *bridge.Home {
		spec := platform.ByKey[key]
		path := explicit
		if path == "" {
			path, _ = platform.ClientHome(spec)
		} else {
			path = platform.ExpandPath(path)
		}
		return &bridge.Home{
			Label: spec.Display,
			Slug:  spec.Key,
			Path:  path,
			App:   appLabel(spec),
		}
	}
	a := build("wb", f.homeA)
	b := build("wb_ai", f.homeB)
	if a.Path == b.Path {
		return nil, nil, &bridge.Error{Msg: "两个 home 不能是同一个目录。"}
	}
	return a, b, nil
}

func appLabel(spec platform.Spec) string {
	switch {
	case platform.IsWin:
		return spec.WinImage
	case platform.IsMac:
		return spec.MacApp
	}
	return spec.LinuxImage
}

func emitJSON(payload any) int {
	enc := json.NewEncoder(os.Stdout)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(payload); err != nil {
		fmt.Fprintf(os.Stderr, "输出失败：%v\n", err)
		return 2
	}
	return 0
}

func fail(err error) int {
	fmt.Fprintf(os.Stderr, "错误：%v\n", err)
	return 2
}

// --------------------------------------------------------------------------
// 子命令
// --------------------------------------------------------------------------

func cmdStatus(argv []string) int {
	f, err := parseFlags(argv)
	if err != nil {
		return fail(err)
	}
	statuses, err := platform.ClientStatuses()
	if err != nil {
		return fail(err)
	}
	running := []string{}
	for _, s := range statuses {
		if s.Running {
			running = append(running, s.Display)
		}
	}
	if f.json {
		return emitJSON(map[string]any{
			"platform":     platform.PlatformLabel(),
			"clients":      statuses,
			"running_names": running,
			"all_stopped":  len(running) == 0,
		})
	}
	fmt.Printf("平台：%s\n", platform.PlatformLabel())
	for _, s := range statuses {
		mark := "已退出"
		if s.Running {
			mark = "运行中"
		}
		fmt.Printf("\n[%s] %s\n  数据目录：%s\n", s.Display, mark, s.Home)
		if s.HomeNote != "" {
			fmt.Printf("  ! %s\n", s.HomeNote)
		}
		for i, p := range s.Processes {
			if i >= 5 {
				break
			}
			fmt.Printf("  pid %d  %s\n", p.PID, p.Cmd)
		}
	}
	fmt.Println()
	if len(running) == 0 {
		fmt.Println("全部客户端已退出。")
	} else {
		fmt.Println("仍有客户端在运行。")
	}
	return 0
}

func cmdQuitClients(argv []string) int {
	f, err := parseFlags(argv)
	if err != nil {
		return fail(err)
	}
	wait := platform.QuitGrace
	if f.wait > 0 {
		wait = f.wait
	}
	result, err := platform.QuitClients(wait, 10*time.Second, f.force)
	if err != nil {
		return fail(err)
	}
	if f.json {
		return emitJSON(result)
	}
	if len(result.Requested) == 0 {
		fmt.Println("没有客户端在运行。")
		return 0
	}
	for _, r := range result.Requested {
		fmt.Printf("已请求退出：%s（信号发出=%v）\n", r.Display, r.Signalled)
	}
	for _, key := range result.Forced {
		fmt.Printf("强制结束：%s\n", platform.ByKey[key].Display)
	}
	if result.OK {
		fmt.Println("两个客户端都已退出。")
		return 0
	}
	names := []string{}
	for _, r := range result.Remaining {
		names = append(names, r.Display)
	}
	fmt.Printf("仍在运行：%s\n", strings.Join(names, "、"))
	return 1
}

func cmdSurvey(argv []string) int {
	f, err := parseFlags(argv)
	if err != nil {
		return fail(err)
	}
	a, b, err := homes(f)
	if err != nil {
		return fail(err)
	}
	infoA, err := a.Survey()
	if err != nil {
		return fail(err)
	}
	infoB, err := b.Survey()
	if err != nil {
		return fail(err)
	}
	if f.json {
		return emitJSON(map[string]any{"homes": []any{infoA, infoB}})
	}
	for _, info := range []map[string]any{infoA, infoB} {
		fmt.Printf("[%v] %v\n  %v\n", info["label"], info["path"], info["counts"])
	}
	return 0
}

// cmdDbgRows 是跨实现对账工具：按行打印编码后的哈希，
// 用来回答"plan_id 不一致时到底是哪一行不一样"。
func cmdDbgRows(argv []string) int {
	// 位置参数（a / b）先摘出来，剩下的才交给 flag 解析。
	positional := ""
	rest := make([]string, 0, len(argv))
	for _, arg := range argv {
		if !strings.HasPrefix(arg, "-") && positional == "" {
			positional = arg
			continue
		}
		rest = append(rest, arg)
	}
	f, err := parseFlags(rest)
	if err != nil {
		return fail(err)
	}
	spec := platform.ByKey["wb"]
	if positional == "b" || positional == "wb_ai" {
		spec = platform.ByKey["wb_ai"]
	}
	path, _ := platform.ClientHome(spec)
	if f.homeA != "" {
		path = platform.ExpandPath(f.homeA)
	}
	home := &bridge.Home{Label: spec.Display, Slug: spec.Key, Path: path, App: appLabel(spec)}

	perRow, allHash, err := home.RowHashes()
	if err != nil {
		return fail(err)
	}
	cols, err := home.Columns("sessions")
	if err != nil {
		return fail(err)
	}
	fmt.Printf("# home=%s rows=%d sessions_all=%s\n", path, len(perRow), allHash)
	fmt.Printf("# columns=%s\n", strings.Join(cols, ","))
	ids := make([]string, 0, len(perRow))
	for id := range perRow {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		fmt.Printf("%s\t%s\n", id, perRow[id])
	}
	return 0
}

func cmdPlan(argv []string) int {
	f, err := parseFlags(argv)
	if err != nil {
		return fail(err)
	}
	a, b, err := homes(f)
	if err != nil {
		return fail(err)
	}
	plan, err := bridge.BuildPlan(a, b, f.options)
	if err != nil {
		return fail(err)
	}
	if f.json {
		return emitJSON(plan.AsDict())
	}
	totals, _ := plan.Summary["totals"].(map[string]any)
	fmt.Printf("plan_id: %v\n", plan.PlanID)
	for _, side := range []string{"a2b", "b2a"} {
		s, _ := plan.Summary[side].(map[string]any)
		fmt.Printf("%s：%v → %v  待复制 %v 条，已存在跳过 %v 条\n",
			side, s["from"], s["to"], s["sessions_to_copy"], s["sessions_skipped"])
	}
	fmt.Printf("合计：%v 条会话，约 %v\n", totals["sessions_to_copy"], totals["approx_human"])
	return 0
}
