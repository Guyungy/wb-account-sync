package platform

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// fakeProcs 是可控制的假进程表；用来在不碰真实进程的前提下测退出逻辑。
type fakeProcs struct {
	running       map[string]bool
	gracefulWorks bool
	forceWorks    bool
	calls         [][2]any // (key, force)
	scanErr       error
}

func newFake(keys ...string) *fakeProcs {
	f := &fakeProcs{running: map[string]bool{}, gracefulWorks: true, forceWorks: true}
	for _, k := range keys {
		f.running[k] = true
	}
	return f
}

func (f *fakeProcs) scan() (map[string][]Process, error) {
	if f.scanErr != nil {
		return nil, f.scanErr
	}
	out := map[string][]Process{}
	for _, spec := range Clients {
		if f.running[spec.Key] {
			out[spec.Key] = []Process{{PID: 101, Cmd: "/Applications/" + spec.MacApp + "/Contents/MacOS/Electron"}}
		} else {
			out[spec.Key] = []Process{}
		}
	}
	return out, nil
}

func (f *fakeProcs) request(spec Spec, force bool) bool {
	f.calls = append(f.calls, [2]any{spec.Key, force})
	works := f.gracefulWorks
	if force {
		works = f.forceWorks
	}
	if works {
		f.running[spec.Key] = false
	}
	return true
}

// withFake 在测试期间替换注入点，结束后恢复。
func withFake(t *testing.T, f *fakeProcs) {
	t.Helper()
	oldScan, oldQuit := scanFn, requestQuitFn
	scanFn, requestQuitFn = f.scan, f.request
	t.Cleanup(func() { scanFn, requestQuitFn = oldScan, oldQuit })
}

func TestQuitClientsNothingRunning(t *testing.T) {
	f := newFake()
	withFake(t, f)
	got, err := QuitClients(time.Millisecond, time.Millisecond, false)
	if err != nil {
		t.Fatalf("意外错误：%v", err)
	}
	if !got.OK || len(got.Remaining) != 0 || len(f.calls) != 0 {
		t.Fatalf("没有客户端在跑时不该有任何动作，得到 %+v", got)
	}
}

func TestQuitClientsGracefulSucceeds(t *testing.T) {
	f := newFake("wb")
	withFake(t, f)
	got, err := QuitClients(2*time.Second, time.Second, false)
	if err != nil {
		t.Fatalf("意外错误：%v", err)
	}
	if !got.OK || len(got.Forced) != 0 {
		t.Fatalf("应优雅退出成功，得到 %+v", got)
	}
	if len(f.calls) != 1 || f.calls[0][0] != "wb" || f.calls[0][1] != false {
		t.Fatalf("应只发一次优雅退出请求，实际 %v", f.calls)
	}
}

func TestQuitClientsStubbornIsReportedNotForced(t *testing.T) {
	// 没拿到强杀授权时，宁可如实报告失败，也不越过那条线。
	f := newFake("wb")
	f.gracefulWorks = false
	f.forceWorks = false
	withFake(t, f)
	got, err := QuitClients(10*time.Millisecond, time.Millisecond, false)
	if err != nil {
		t.Fatalf("意外错误：%v", err)
	}
	if got.OK {
		t.Fatal("客户端没退出，不该报成功")
	}
	if len(got.Remaining) != 1 || got.Remaining[0].Key != "wb" {
		t.Fatalf("remaining 应含 wb，得到 %+v", got.Remaining)
	}
	if len(got.Forced) != 0 {
		t.Fatalf("未授权强杀，forced 必须为空，得到 %v", got.Forced)
	}
	for _, call := range f.calls {
		if call[1] == true {
			t.Fatalf("未授权时不得发出强杀，实际 %v", f.calls)
		}
	}
}

func TestQuitClientsForceIsUsedAndReported(t *testing.T) {
	f := newFake("wb")
	f.gracefulWorks = false
	f.forceWorks = true
	withFake(t, f)
	got, err := QuitClients(10*time.Millisecond, 2*time.Second, true)
	if err != nil {
		t.Fatalf("意外错误：%v", err)
	}
	if !got.OK || len(got.Forced) != 1 || got.Forced[0] != "wb" {
		t.Fatalf("应强杀成功并记录，得到 %+v", got)
	}
	if len(f.calls) != 2 || f.calls[1][1] != true {
		t.Fatalf("第二次调用应是 force，实际 %v", f.calls)
	}
}

func TestQuitClientsOnlyTargetsRunning(t *testing.T) {
	f := newFake("wb_ai")
	withFake(t, f)
	if _, err := QuitClients(2*time.Second, time.Second, false); err != nil {
		t.Fatalf("意外错误：%v", err)
	}
	if len(f.calls) != 1 || f.calls[0][0] != "wb_ai" {
		t.Fatalf("只该动在跑的那个，实际 %v", f.calls)
	}
}

func TestClientsAllStoppedFailsClosedOnScanError(t *testing.T) {
	// 探测失败时不假装安全。
	f := newFake()
	f.scanErr = os.ErrPermission
	old := scanFn
	scanFn = f.scan
	t.Cleanup(func() { scanFn = old })
	if ClientsAllStopped() {
		t.Fatal("扫描失败时必须返回 false")
	}
}

func TestClientHomePrefersConfirmedCandidate(t *testing.T) {
	dir := t.TempDir()
	spec := Spec{
		Key:            "t",
		Display:        "T",
		HomeCandidates: []string{filepath.Join(dir, "empty"), filepath.Join(dir, "real")},
	}
	if err := os.MkdirAll(filepath.Join(dir, "empty"), 0o755); err != nil {
		t.Fatal(err)
	}
	real := filepath.Join(dir, "real")
	if err := os.MkdirAll(real, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(real, DBName), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	// 首选候选是存在的目录但没库：应继续往后找到真正带库的那个。
	home, confirmed := ClientHome(spec)
	if !confirmed || home != real {
		t.Fatalf("应确认到 %s，得到 %s confirmed=%v", real, home, confirmed)
	}
}

func TestClientHomeFallsBackToExistingDirUnconfirmed(t *testing.T) {
	dir := t.TempDir()
	empty := filepath.Join(dir, "empty")
	if err := os.MkdirAll(empty, 0o755); err != nil {
		t.Fatal(err)
	}
	spec := Spec{Key: "t", HomeCandidates: []string{filepath.Join(dir, "missing"), empty}}
	home, confirmed := ClientHome(spec)
	if confirmed {
		t.Fatal("没有 workbuddy.db 时不该确认")
	}
	if home != empty {
		t.Fatalf("应退回第一个存在的目录 %s，得到 %s", empty, home)
	}
}

func TestClientHomeReturnsFirstCandidateWhenNothingExists(t *testing.T) {
	dir := t.TempDir()
	spec := Spec{Key: "t", HomeCandidates: []string{filepath.Join(dir, "nope"), filepath.Join(dir, "nope2")}}
	home, confirmed := ClientHome(spec)
	if confirmed {
		t.Fatal("全部不存在时不该确认")
	}
	if home != ExpandPath(spec.HomeCandidates[0]) {
		t.Fatalf("应退回首选候选，得到 %s", home)
	}
}

// macOS 上客户端主进程的可执行文件名是 Electron，按进程名匹配会静默失效。
// 这里钉住扫描器认的是 bundle 路径，而且不会把 AI 版错认成普通版。
func TestScanMacOSMatchesBundlePathNotProcessName(t *testing.T) {
	if !IsMac {
		t.Skip("仅在 macOS 上验证 bundle 路径匹配")
	}
	found, err := scanProcesses()
	if err != nil {
		t.Fatalf("扫描失败：%v", err)
	}
	for key, procs := range found {
		for _, p := range procs {
			spec := ByKey[key]
			if wantPath := "/" + spec.MacApp + "/Contents/MacOS/"; !contains(p.Cmd, wantPath) {
				t.Fatalf("%s 的进程 %q 不含 %q", key, p.Cmd, wantPath)
			}
			if key == "wb" && contains(p.Cmd, "/WorkBuddy AI.app/") {
				t.Fatalf("普通版命中了 AI 版进程：%q", p.Cmd)
			}
		}
	}
}

func contains(haystack, needle string) bool {
	return len(needle) == 0 || (len(haystack) >= len(needle) && indexOf(haystack, needle) >= 0)
}

func indexOf(h, n string) int {
	for i := 0; i+len(n) <= len(h); i++ {
		if h[i:i+len(n)] == n {
			return i
		}
	}
	return -1
}

func TestIsSelfNoiseFiltersHelpers(t *testing.T) {
	if !isSelfNoise("/Applications/WorkBuddy.app/Contents/Frameworks/Electron Helper") {
		t.Fatal("helper 进程应被过滤")
	}
	if !isSelfNoise("python3 tools/wb_home_bridge.py") {
		t.Fatal("本次探测自身的命令行应被过滤")
	}
	if isSelfNoise("/Applications/WorkBuddy.app/Contents/MacOS/Electron") {
		t.Fatal("客户端主进程不该被过滤")
	}
}
