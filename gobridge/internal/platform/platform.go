// Package platform 是 Python 版 tools/wb_platform.py 的 Go 移植：客户端进程探测、
// 数据目录探测、退出客户端。只依赖标准库。
//
// 移植时保留了原版两个刻意的设计，它们都是踩过坑的结论：
//
//   - **不按进程名匹配**。macOS 上客户端主进程的可执行文件叫 Electron，
//     按名字匹配会静默失效；所以按 bundle 路径匹配。
//   - **退出分两段**。requestQuit 只把请求发出去，quitClients 再轮询确认
//     进程真的消失。合并成一步会返回"我发出去了"，而用户需要的是"退干净了"。
package platform

import (
	"encoding/csv"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"
)

// Version 与 Python 侧保持同号，方便对账。
const Version = "0.1.0"

// DBName 是判定"这个目录确实是客户端数据目录"的锚点。
const DBName = "workbuddy.db"

// QuitGrace 是优雅退出的宽限期。客户端退出时要落盘状态，秒退会被记成崩溃。
const QuitGrace = 25 * time.Second

// pollInterval 是等待进程消失时的轮询间隔。
const pollInterval = 400 * time.Millisecond

// 平台判定。刻意用 runtime.GOOS 而不是看文件系统，交叉编译时也正确。
var (
	IsMac   = runtime.GOOS == "darwin"
	IsWin   = runtime.GOOS == "windows"
	IsLinux = runtime.GOOS == "linux"
)

// Spec 描述一个客户端：怎么找它的进程、它的数据目录可能在哪。
type Spec struct {
	Key            string
	Display        string
	MacApp         string
	WinImage       string
	LinuxImage     string
	HomeCandidates []string
	Hint           string
}

// Clients 是已知的两个客户端。顺序即 UI 里的左右顺序。
var Clients = []Spec{
	{
		Key:        "wb",
		Display:    "WorkBuddy",
		MacApp:     "WorkBuddy.app",
		WinImage:   "WorkBuddy.exe",
		LinuxImage: "workbuddy",
		HomeCandidates: []string{
			"~/.workbuddy",
			"%APPDATA%/WorkBuddy",
			"%APPDATA%/workbuddy",
		},
		Hint: "WorkBuddy 客户端的数据目录",
	},
	{
		Key:        "wb_ai",
		Display:    "WorkBuddy AI",
		MacApp:     "WorkBuddy AI.app",
		WinImage:   "WorkBuddy AI.exe",
		LinuxImage: "workbuddy-ai",
		HomeCandidates: []string{
			"~/.workbuddy-ai",
			"%APPDATA%/WorkBuddy AI",
			"%APPDATA%/workbuddy-ai",
		},
		Hint: "WorkBuddy AI 客户端的数据目录",
	},
}

// ByKey 按 key 取客户端规格。
var ByKey = func() map[string]Spec {
	m := make(map[string]Spec, len(Clients))
	for _, s := range Clients {
		m[s.Key] = s
	}
	return m
}()

// Process 是一条命中的客户端进程。
type Process struct {
	PID int    `json:"pid"`
	Cmd string `json:"cmd"`
}

// Status 是一个客户端的运行状态 + 数据目录。
type Status struct {
	Key           string    `json:"key"`
	Display       string    `json:"display"`
	Hint          string    `json:"hint"`
	Running       bool      `json:"running"`
	Processes     []Process `json:"processes"`
	Home          string    `json:"home"`
	HomeConfirmed bool      `json:"home_confirmed"`
	HomeExists    bool      `json:"home_exists"`
	HomeNote      string    `json:"home_note"`
}

// ExpandPath 展开 `~` 与 `%VAR%`，返回绝对路径。
func ExpandPath(path string) string {
	path = os.Expand(path, func(name string) string { return os.Getenv(name) })
	if strings.HasPrefix(path, "~") {
		if home, err := os.UserHomeDir(); err == nil {
			path = filepath.Join(home, strings.TrimPrefix(strings.TrimPrefix(path, "~"), string(os.PathSeparator)))
		}
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	return abs
}

// --------------------------------------------------------------------------
// 进程扫描
// --------------------------------------------------------------------------

func isSelfNoise(cmd string) bool {
	if strings.Contains(cmd, "/Frameworks/") {
		return true
	}
	lowered := strings.ToLower(cmd)
	for _, token := range []string{"wb_platform", "wb_home_bridge", "wb_ui", "grep workbuddy"} {
		if strings.Contains(lowered, token) {
			return true
		}
	}
	return false
}

func output(name string, args ...string) (string, error) {
	cmd := exec.Command(name, args...)
	raw, err := cmd.Output()
	if err != nil {
		if _, ok := err.(*exec.ExitError); !ok {
			return "", fmt.Errorf("无法执行 %s：%w", name, err)
		}
	}
	return string(raw), nil
}

func scanMacOS(found map[string][]Process) error {
	out, err := output("ps", "-Ao", "pid=,command=")
	if err != nil {
		return err
	}
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || isSelfNoise(line) {
			continue
		}
		pidStr, cmd, ok := strings.Cut(line, " ")
		if !ok {
			continue
		}
		pid, err := strconv.Atoi(pidStr)
		if err != nil {
			continue
		}
		for _, spec := range Clients {
			// 带斜杠前缀保证 "WorkBuddy.app" 不会误命中 "WorkBuddy AI.app"。
			if strings.Contains(cmd, "/"+spec.MacApp+"/Contents/MacOS/") {
				found[spec.Key] = append(found[spec.Key], Process{PID: pid, Cmd: truncate(cmd, 200)})
				break
			}
		}
	}
	return nil
}

func scanWindows(found map[string][]Process) error {
	out, err := output("tasklist", "/FO", "CSV", "/NH")
	if err != nil {
		return err
	}
	byImage := make(map[string]string, len(Clients))
	for _, spec := range Clients {
		byImage[strings.ToLower(spec.WinImage)] = spec.Key
	}
	reader := csv.NewReader(strings.NewReader(out))
	reader.FieldsPerRecord = -1
	reader.LazyQuotes = true
	for {
		record, err := reader.Read()
		if err != nil {
			break
		}
		if len(record) < 2 {
			continue
		}
		key, ok := byImage[strings.ToLower(strings.TrimSpace(record[0]))]
		if !ok {
			continue
		}
		pid, err := strconv.Atoi(strings.TrimSpace(record[1]))
		if err != nil {
			continue
		}
		found[key] = append(found[key], Process{PID: pid, Cmd: strings.TrimSpace(record[0])})
	}
	return nil
}

func scanLinux(found map[string][]Process) error {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return fmt.Errorf("无法读取 /proc：%w", err)
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		pid, err := strconv.Atoi(entry.Name())
		if err != nil {
			continue
		}
		raw, err := os.ReadFile(filepath.Join("/proc", entry.Name(), "cmdline"))
		if err != nil || len(raw) == 0 {
			continue
		}
		argv0 := strings.Split(string(raw), "\x00")[0]
		base := strings.ToLower(filepath.Base(argv0))
		for _, spec := range Clients {
			// 用相等而非包含，避免 "workbuddy" 命中 "workbuddy-ai"。
			if spec.LinuxImage != "" && base == spec.LinuxImage {
				found[spec.Key] = append(found[spec.Key], Process{PID: pid, Cmd: truncate(argv0, 200)})
				break
			}
		}
	}
	return nil
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func scanProcesses() (map[string][]Process, error) {
	found := make(map[string][]Process, len(Clients))
	switch {
	case IsMac:
		return found, scanMacOS(found)
	case IsWin:
		return found, scanWindows(found)
	case IsLinux:
		return found, scanLinux(found)
	}
	return found, nil
}

// --------------------------------------------------------------------------
// home 探测
// --------------------------------------------------------------------------

// ClientHome 返回 (路径, 是否已确认)。
//
// 优先返回确实含 workbuddy.db 的候选；否则退回第一个存在的目录；
// 都不存在时返回首选候选并标记未确认。
func ClientHome(spec Spec) (string, bool) {
	var fallback string
	for _, cand := range spec.HomeCandidates {
		path := ExpandPath(cand)
		if info, err := os.Stat(filepath.Join(path, DBName)); err == nil && !info.IsDir() {
			return path, true
		}
		if fallback == "" {
			if info, err := os.Stat(path); err == nil && info.IsDir() {
				fallback = path
			}
		}
	}
	if fallback != "" {
		return fallback, false
	}
	return ExpandPath(spec.HomeCandidates[0]), false
}

// ClientStatuses 返回两个客户端的运行状态与数据目录。
func ClientStatuses() ([]Status, error) {
	procs, err := scanFn()
	if err != nil {
		return nil, err
	}
	out := make([]Status, 0, len(Clients))
	for _, spec := range Clients {
		hits := procs[spec.Key]
		if hits == nil {
			hits = []Process{}
		}
		home, confirmed := ClientHome(spec)
		info, statErr := os.Stat(home)
		exists := statErr == nil && info.IsDir()
		note := ""
		if !confirmed {
			if exists {
				note = "目录存在但未找到 workbuddy.db"
			} else {
				note = "未找到数据目录"
			}
		}
		out = append(out, Status{
			Key:           spec.Key,
			Display:       spec.Display,
			Hint:          spec.Hint,
			Running:       len(hits) > 0,
			Processes:     hits,
			Home:          home,
			HomeConfirmed: confirmed,
			HomeExists:    exists,
			HomeNote:      note,
		})
	}
	return out, nil
}

// RunningClients 只返回在跑的客户端。
func RunningClients() ([]Status, error) {
	all, err := ClientStatuses()
	if err != nil {
		return nil, err
	}
	out := make([]Status, 0, len(all))
	for _, item := range all {
		if item.Running {
			out = append(out, item)
		}
	}
	return out, nil
}

// ClientsAllStopped 探测失败时返回 false——不假装安全。
func ClientsAllStopped() bool {
	running, err := RunningClients()
	if err != nil {
		return false
	}
	return len(running) == 0
}

// PlatformLabel 是给人看的平台描述。
func PlatformLabel() string {
	switch {
	case IsMac:
		return fmt.Sprintf("macOS %s · %s", macVersion(), machine())
	case IsWin:
		return fmt.Sprintf("Windows · %s", machine())
	case IsLinux:
		return fmt.Sprintf("Linux · %s", machine())
	}
	return fmt.Sprintf("%s · %s", runtime.GOOS, machine())
}

func macVersion() string {
	out, err := output("sw_vers", "-productVersion")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}

func machine() string { return runtime.GOARCH }

// --------------------------------------------------------------------------
// 测试注入点
// --------------------------------------------------------------------------

// 退出客户端的测试必须能替换掉"真实扫描进程"和"真实发信号"这两步，
// 否则跑一次单测就会把开发者的 WorkBuddy 关掉。生产路径下这些变量
// 就是下面的真实实现，行为与直接调用完全一致。
var (
	scanFn        = scanProcesses
	requestQuitFn = RequestQuit
)

// --------------------------------------------------------------------------
// 退出客户端
// --------------------------------------------------------------------------

func pidsOf(key string) []int {
	procs, err := scanFn()
	if err != nil {
		return nil
	}
	pids := make([]int, 0, len(procs[key]))
	for _, p := range procs[key] {
		pids = append(pids, p.PID)
	}
	return pids
}

func signalPids(pids []int, force bool) bool {
	sent := false
	for _, pid := range pids {
		if err := killPid(pid, force); err == nil {
			sent = true
		}
	}
	return sent
}

// RequestQuit 向客户端发出退出请求；返回「请求是否成功发出」。
//
// 返回值**不代表进程已消失**——quitClients 会轮询确认。
func RequestQuit(spec Spec, force bool) bool {
	if IsMac && !force {
		// 优先走 AppleEvent 优雅退出，客户端才有机会落盘。
		app := strings.TrimSuffix(spec.MacApp, ".app")
		if runCheck(10*time.Second, "/usr/bin/osascript", "-e", fmt.Sprintf(`tell application %q to quit`, app)) {
			return true
		}
		// osascript 失败（自动化权限被拒等）时退回信号，不假装已请求。
	}
	if IsWin {
		args := []string{"/IM", spec.WinImage}
		if force {
			args = append(args, "/F")
		}
		return runCheck(15*time.Second, "taskkill", args...)
	}
	return signalPids(pidsOf(spec.Key), force)
}

// WaitStopped 等待到没有客户端在跑；返回超时后仍在运行的客户端 key。
func WaitStopped(timeout time.Duration) ([]string, error) {
	deadline := time.Now().Add(timeout)
	for {
		running, err := RunningClients()
		if err != nil {
			return nil, err
		}
		if len(running) == 0 {
			return nil, nil
		}
		if !time.Now().Before(deadline) {
			keys := make([]string, 0, len(running))
			for _, item := range running {
				keys = append(keys, item.Key)
			}
			return keys, nil
		}
		time.Sleep(pollInterval)
	}
}

// QuitRequest 记录对某个客户端发了什么。
type QuitRequest struct {
	Key       string `json:"key"`
	Display   string `json:"display"`
	Signalled bool   `json:"signalled"`
}

// QuitResult 是 quitClients 的结果。
type QuitResult struct {
	OK        bool          `json:"ok"`
	Requested []QuitRequest `json:"requested"`
	Remaining []QuitRequest `json:"remaining"`
	Forced    []string      `json:"forced"`
}

// QuitClients 退出所有在跑的客户端。
//
// allowForce 为真时才在宽限期后强杀。强杀会让客户端来不及落盘，因此默认关闭，
// 由调用方（界面上的复选框）显式授权。
func QuitClients(wait, forceWait time.Duration, allowForce bool) (QuitResult, error) {
	before, err := RunningClients()
	if err != nil {
		return QuitResult{}, err
	}
	result := QuitResult{Requested: []QuitRequest{}, Remaining: []QuitRequest{}, Forced: []string{}}
	if len(before) == 0 {
		result.OK = true
		return result, nil
	}

	for _, item := range before {
		spec := ByKey[item.Key]
		result.Requested = append(result.Requested, QuitRequest{
			Key:       spec.Key,
			Display:   spec.Display,
			Signalled: requestQuitFn(spec, false),
		})
	}

	still, err := WaitStopped(wait)
	if err != nil {
		return QuitResult{}, err
	}
	if len(still) > 0 {
		if allowForce {
			for _, key := range still {
				if requestQuitFn(ByKey[key], true) {
					result.Forced = append(result.Forced, key)
				}
			}
			still, err = WaitStopped(forceWait)
			if err != nil {
				return QuitResult{}, err
			}
		} else {
			// 没拿到强杀授权：把请求重发一次（首次可能被权限弹窗挡住），再等一小轮。
			// 比直接放弃更贴近「我确实想让它退出」的意图，同时不越过强杀那条线。
			for _, key := range still {
				requestQuitFn(ByKey[key], false)
			}
			extra := wait / 4
			if extra > 5*time.Second {
				extra = 5 * time.Second
			}
			if extra < time.Second {
				extra = time.Second
			}
			still, err = WaitStopped(extra)
			if err != nil {
				return QuitResult{}, err
			}
		}
	}

	for _, key := range still {
		result.Remaining = append(result.Remaining, QuitRequest{
			Key:     key,
			Display: ByKey[key].Display,
		})
	}
	result.OK = len(result.Remaining) == 0
	return result, nil
}
