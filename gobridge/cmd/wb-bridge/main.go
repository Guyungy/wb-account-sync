// Command wb-bridge 是 wb-account-sync 的 Go 实现入口。
//
// 目标是把原 Python 版的"底座"整块替掉：不依赖解释器、单文件可执行、
// 交叉编译出 mac/Windows/Linux 三端。
//
// 当前进度和 Python 版逐项对齐；已实现的部分可以通过 plan_id 直接对账——
// 同一份数据两边算出的 plan_id 相同，就说明计划内容逐字节等价。
package main

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"os"
	"os/signal"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/bridge"
	"github.com/Guyungy/wb-account-sync/gobridge/internal/platform"
	"github.com/Guyungy/wb-account-sync/gobridge/internal/pyjson"
	"github.com/Guyungy/wb-account-sync/gobridge/internal/webui"
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
	case "survey", "doctor":
		return cmdSurvey(argv[1:])
	case "plan":
		return cmdPlan(argv[1:])
	case "serve", "ui":
		return cmdServe(argv[1:])
	case "apply":
		return cmdApply(argv[1:])
	case "backup":
		return cmdBackup(argv[1:])
	case "verify":
		return cmdVerify(argv[1:])
	case "restore":
		return cmdRestore(argv[1:])
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
  wb-bridge plan          [--json] [--output 文件] [--home-a 路径] [--home-b 路径] [选项]
  wb-bridge serve         [--port 端口] [--no-open] [--token 字符串] [--handshake]
                          启动本地浏览器界面（只绑 127.0.0.1，必须带 token 访问）
  wb-bridge apply         --plan 文件 --state-dir 目录 --confirm plan_id
  wb-bridge verify        --plan 文件
  wb-bridge backup        --dest 目录 [--label 标签] [--include-heavy]
  wb-bridge restore       --run-dir 目录 --confirm plan_id

plan 选项（默认只开 changes / skills / memory / claw）：
  --no-changes --no-skills --no-memory --no-claw
  --include-plugins --include-automations --include-storage
  --include-connectors --overwrite-assets

写操作（apply / backup）默认要求两个客户端都已退出；确知无写入冲突时
可用 --allow-client-running 绕过，风险自负。
`)
}

// --------------------------------------------------------------------------
// 参数解析
// --------------------------------------------------------------------------

type flags struct {
	json         bool
	force        bool
	wait         time.Duration
	homeA        string
	homeB        string
	options      map[string]any
	output       string
	planPath     string
	stateDir     string
	confirm      string
	dest         string
	label        string
	includeHeavy bool
	runDir       string
	allowRunning bool
	port         int
	noOpen       bool
	token        string
	handshake    bool
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
		str := func() (string, error) { return valueOf(arg, next) }
		switch {
		case arg == "--json":
			f.json = true
		case arg == "--force":
			f.force = true
		case arg == "--allow-client-running":
			f.allowRunning = true
		case arg == "--include-heavy":
			f.includeHeavy = true
		case arg == "--no-open":
			f.noOpen = true
		case arg == "--handshake":
			f.handshake = true
		case arg == "--port" || strings.HasPrefix(arg, "--port="):
			v, err := str()
			if err != nil {
				return nil, err
			}
			n, cerr := strconv.Atoi(v)
			if cerr != nil {
				return nil, fmt.Errorf("--port 需要整数：%v", cerr)
			}
			f.port = n
		case arg == "--token" || strings.HasPrefix(arg, "--token="):
			v, err := str()
			if err != nil {
				return nil, err
			}
			f.token = v
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
			v, err := str()
			if err != nil {
				return nil, err
			}
			f.homeA = v
		case arg == "--home-b" || strings.HasPrefix(arg, "--home-b="):
			v, err := str()
			if err != nil {
				return nil, err
			}
			f.homeB = v
		case arg == "--output" || strings.HasPrefix(arg, "--output="):
			v, err := str()
			if err != nil {
				return nil, err
			}
			f.output = v
		case arg == "--plan" || strings.HasPrefix(arg, "--plan="):
			v, err := str()
			if err != nil {
				return nil, err
			}
			f.planPath = v
		case arg == "--state-dir" || strings.HasPrefix(arg, "--state-dir="):
			v, err := str()
			if err != nil {
				return nil, err
			}
			f.stateDir = v
		case arg == "--confirm" || strings.HasPrefix(arg, "--confirm="):
			v, err := str()
			if err != nil {
				return nil, err
			}
			f.confirm = v
		case arg == "--dest" || strings.HasPrefix(arg, "--dest="):
			v, err := str()
			if err != nil {
				return nil, err
			}
			f.dest = v
		case arg == "--label" || strings.HasPrefix(arg, "--label="):
			v, err := str()
			if err != nil {
				return nil, err
			}
			f.label = v
		case arg == "--run-dir" || strings.HasPrefix(arg, "--run-dir="):
			v, err := str()
			if err != nil {
				return nil, err
			}
			f.runDir = v
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
			"platform":      platform.PlatformLabel(),
			"clients":       statuses,
			"running_names": running,
			"all_stopped":   len(running) == 0,
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
	data := plan.AsDict()
	if f.output != "" {
		out := platform.ExpandPath(f.output)
		if _, serr := os.Stat(out); serr == nil {
			return fail(fmt.Errorf("输出文件已存在，不覆盖：%s", out))
		}
		text, merr := pyjson.MarshalIndent(data, 2)
		if merr != nil {
			return fail(merr)
		}
		if werr := os.WriteFile(out, []byte(text), 0o600); werr != nil {
			return fail(werr)
		}
		os.Chmod(out, 0o600)
	}
	if f.json {
		return emitJSON(data)
	}
	totals, _ := plan.Summary["totals"].(map[string]any)
	fmt.Printf("plan_id: %v\n", plan.PlanID)
	for _, side := range []string{"a2b", "b2a"} {
		s, _ := plan.Summary[side].(map[string]any)
		fmt.Printf("%s：%v → %v  待复制 %v 条，已存在跳过 %v 条\n",
			side, s["from"], s["to"], s["sessions_to_copy"], s["sessions_skipped"])
	}
	fmt.Printf("合计：%v 条会话，约 %v\n", totals["sessions_to_copy"], totals["approx_human"])
	if f.output != "" {
		fmt.Printf("计划已写入：%s\n", platform.ExpandPath(f.output))
	}
	return 0
}

// logLine / warnLine 是给引擎用的两个输出通道。
// 引擎本身不打印，是为了让 CLI 与 HTTP UI 能用不同的方式消费同一份事件流。
func logLine(msg string)  { fmt.Println(msg) }
func warnLine(msg string) { fmt.Fprintln(os.Stderr, msg) }

// requireClientsStopped 是写操作的前置守卫。
//
// 客户端在运行时持有数据库写锁与内存中的状态副本：此时写入要么被锁挡住，
// 要么写进去了但被客户端的退出 flush 覆盖掉——后者更糟，因为不报错。
func requireClientsStopped(allowRunning bool) error {
	if allowRunning {
		return nil
	}
	statuses, err := platform.ClientStatuses()
	if err != nil {
		return err
	}
	names := []string{}
	for _, s := range statuses {
		if s.Running {
			names = append(names, s.Display)
		}
	}
	if len(names) > 0 {
		return fmt.Errorf("%s 仍在运行。请先完全退出客户端（或加 --allow-client-running，风险自负）。",
			strings.Join(names, "、"))
	}
	return nil
}

// defaultStateDir 与 Python 版的 DEFAULT_STATE_DIR 一致。
const defaultStateDir = "~/.wb-home-bridge"

// newToken 生成 32 字节的 URL 安全随机串。
//
// 这个 token 是本地界面的唯一访问闸门：绑的是 127.0.0.1，但同机上的任何
// 进程/页面都能访问到回环地址，所以随机性与不可预测性是必要的。
// 用 crypto/rand 而不是 math/rand —— 后者可预测，等于没有闸门。
func newToken() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(buf), nil
}

func cmdServe(argv []string) int {
	f, err := parseFlags(argv)
	if err != nil {
		return fail(err)
	}
	a, b, err := homes(f)
	if err != nil {
		return fail(err)
	}
	if a.Path == b.Path {
		return fail(fmt.Errorf("两个 home 不能是同一个目录。"))
	}

	token := f.token
	if token == "" {
		token, err = newToken()
		if err != nil {
			return fail(fmt.Errorf("无法生成访问令牌：%v", err))
		}
	}
	stateDir := f.stateDir
	if stateDir == "" {
		stateDir = defaultStateDir
	}

	srv := &webui.Server{
		HomeA:      a,
		HomeB:      b,
		StateDir:   platform.ExpandPath(stateDir),
		Token:      token,
		Frozen:     false, // 纯 Go 二进制不需要 Python 脚本，安装器可用
		NotifyHook: notifyUser,
	}

	ln, port, err := srv.Listen(f.port)
	if err != nil {
		return fail(fmt.Errorf("无法绑定端口：%v", err))
	}
	defer ln.Close()
	url := srv.URL(port)

	fmt.Printf("wb-home-bridge UI %s\n", version)
	fmt.Printf("  平台     : %s\n", platform.PlatformLabel())
	fmt.Printf("  左侧目录 : %s\n", a.Path)
	fmt.Printf("  右侧目录 : %s\n", b.Path)
	fmt.Printf("  状态目录 : %s\n", srv.StateDir)
	fmt.Printf("  地址     : %s\n\n", url)
	fmt.Println("本服务只监听 127.0.0.1，且所有接口都要求上面链接里的 token。")
	fmt.Println("按 Ctrl-C 停止。执行写操作前必须先完全退出两个客户端。")

	if f.handshake {
		// 宿主程序读这一行来拿地址：必须是单行 JSON，且在用户可读日志之后。
		fmt.Println("WBUI_READY " + pyjson.MustMarshal(map[string]any{
			"port": port, "token": token, "url": url,
			"home_a": a.Path, "home_b": b.Path,
		}))
	}
	// 有终端时 stdout 是行缓冲，但重定向到文件就不是了；显式刷一次，
	// 否则用户从日志里拿不到带 token 的地址。
	os.Stdout.Sync()

	if !f.noOpen {
		// 稍等一下再开浏览器：立刻开有可能在服务还没进入 accept 循环时
		// 就发请求，浏览器会看到连接被拒。
		time.AfterFunc(400*time.Millisecond, func() { srv.OpenAndNotify(url) })
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	fmt.Println("\n已停止。")
	return 0
}

// notifyUser 是"服务起来了但浏览器没打开"时的兜底提示。
// 命令行模式下终端本来就有地址，所以只在非交互场景（无 TTY）弹窗打扰。
func notifyUser(title, message string) {
	if isTerminal() {
		return
	}
	platform.Notify(title, message)
}

func isTerminal() bool {
	info, err := os.Stdout.Stat()
	if err != nil {
		return false
	}
	return info.Mode()&os.ModeCharDevice != 0
}

func cmdApply(argv []string) int {
	f, err := parseFlags(argv)
	if err != nil {
		return fail(err)
	}
	if f.planPath == "" || f.stateDir == "" || f.confirm == "" {
		return fail(fmt.Errorf("apply 需要 --plan、--state-dir、--confirm 三个参数"))
	}
	if err := requireClientsStopped(f.allowRunning); err != nil {
		return fail(err)
	}
	a, b, err := homes(f)
	if err != nil {
		return fail(err)
	}
	res, err := bridge.Apply(a, b, bridge.ApplyOptions{
		PlanPath: platform.ExpandPath(f.planPath),
		Confirm:  f.confirm,
		StateDir: f.stateDir,
	}, logLine, warnLine)
	if err != nil {
		return fail(err)
	}
	fmt.Println("请重启两个 App 后再查看（客户端有内存缓存，不重启看不到新会话）。")
	if f.json {
		return emitJSON(res)
	}
	return 0
}

func cmdBackup(argv []string) int {
	f, err := parseFlags(argv)
	if err != nil {
		return fail(err)
	}
	if f.dest == "" {
		return fail(fmt.Errorf("backup 需要 --dest 指定备份根目录"))
	}
	if err := requireClientsStopped(f.allowRunning); err != nil {
		return fail(err)
	}
	a, b, err := homes(f)
	if err != nil {
		return fail(err)
	}
	root, err := bridge.Backup([]*bridge.Home{a, b}, bridge.BackupOptions{
		Dest:         f.dest,
		Label:        f.label,
		IncludeHeavy: f.includeHeavy,
	}, logLine)
	if err != nil {
		return fail(err)
	}
	if f.json {
		return emitJSON(map[string]any{"root": root})
	}
	return 0
}

func cmdVerify(argv []string) int {
	f, err := parseFlags(argv)
	if err != nil {
		return fail(err)
	}
	if f.planPath == "" {
		return fail(fmt.Errorf("verify 需要 --plan 指定计划文件"))
	}
	a, b, err := homes(f)
	if err != nil {
		return fail(err)
	}
	results, ok, err := bridge.Verify(a, b, platform.ExpandPath(f.planPath), logLine)
	if err != nil {
		return fail(err)
	}
	if f.json {
		emitJSON(map[string]any{"ok": ok, "results": results})
	}
	if !ok {
		// 3 与 Python 版一致：核验未通过是"有结论的失败"，不是参数错误（2）。
		return 3
	}
	return 0
}

func cmdRestore(argv []string) int {
	f, err := parseFlags(argv)
	if err != nil {
		return fail(err)
	}
	if f.runDir == "" || f.confirm == "" {
		return fail(fmt.Errorf("restore 需要 --run-dir 与 --confirm"))
	}
	res, err := bridge.Restore(f.runDir, f.confirm, logLine, warnLine)
	if err != nil {
		return fail(err)
	}
	if f.json {
		return emitJSON(res)
	}
	return 0
}
