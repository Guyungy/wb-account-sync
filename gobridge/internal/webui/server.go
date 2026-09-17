// Package webui 是本地浏览器界面。
//
// 与 Python 版的取舍：**前端一个字节都没重写**，整块用 go:embed 原样端出去。
// 两个理由——
//  1. 界面是已经调好的（含一键同步那套状态机），重写一遍只会引入新 bug；
//  2. 前端与后端是 HTTP + JSON 的边界，这里换实现对方不需要知道。
//
// tests/test_ui_page_parity.py 会断言两边的前端逐字节相同，
// 所以"复用"不是一次性动作，而是被锁住的契约。
package webui

import (
	"crypto/subtle"
	_ "embed"
	"encoding/json"
	"fmt"

	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/autosync"
	"github.com/Guyungy/wb-account-sync/gobridge/internal/bridge"
	"github.com/Guyungy/wb-account-sync/gobridge/internal/platform"
	"github.com/Guyungy/wb-account-sync/gobridge/internal/pyjson"
)

//go:embed static/index.html
var pageHTML string

// Page 是前端页面原文，供测试与调试读取。
func Page() string { return pageHTML }

// Server 持有一次界面会话的全部状态。
type Server struct {
	HomeA *bridge.Home
	HomeB *bridge.Home

	StateDir     string
	AutosyncRoot string
	Frozen       bool

	// Token 是本次启动的访问凭据，缺了或不对一律 403。
	// 每次启动重新生成，旧链接随即失效。
	Token string

	// NotifyHook 在"服务起来了但浏览器没打开"时被调用，用来把地址摊给用户。
	// 打包版没有终端，没有这个兜底用户看到的就是"双击没反应"。
	NotifyHook func(title, message string)

	mu          sync.Mutex
	planPath    string
	planID      string
	planDoc     map[string]any
	lastRunCode *int
	busy        sync.Mutex
}

// HomeFor 按客户端 key 取数据目录。
func (s *Server) HomeFor(key string) *bridge.Home {
	if key == "wb" {
		return s.HomeA
	}
	return s.HomeB
}

// --------------------------------------------------------------------------
// 输出助手
// --------------------------------------------------------------------------

func sendBytes(w http.ResponseWriter, body []byte, contentType string, code int) {
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Length", fmt.Sprint(len(body)))
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(code)
	w.Write(body)
}

func sendJSON(w http.ResponseWriter, payload any, code int) {
	// 与 Python 的 json.dumps(payload, ensure_ascii=False) 同义：
	// 中文原样输出，不转 \uXXXX。
	body := []byte(pyjson.MustMarshal(payload))
	sendBytes(w, body, "application/json; charset=utf-8", code)
}

func sendError(w http.ResponseWriter, msg string, code int) {
	sendJSON(w, map[string]any{"error": msg}, code)
}

// sseWriter 负责把事件按 Server-Sent Events 推给前端。
type sseWriter struct {
	w  http.ResponseWriter
	fl http.Flusher
	mu sync.Mutex
}

func (s *sseWriter) open() {
	s.w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	s.w.Header().Set("Cache-Control", "no-store")
	s.w.Header().Set("Connection", "keep-alive")
	s.w.WriteHeader(200)
	if f, ok := s.w.(http.Flusher); ok {
		s.fl = f
	}
	s.flush()
}

func (s *sseWriter) send(payload map[string]any) {
	// SSE 用换行分隔帧，payload 里带 \r 会把帧切断。
	line := strings.ReplaceAll(pyjson.MustMarshal(payload), "\r", " ")
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, err := fmt.Fprintf(s.w, "data: %s\n\n", line); err != nil {
		return
	}
	s.flush()
}

func (s *sseWriter) flush() {
	if s.fl != nil {
		s.fl.Flush()
	}
}

// --------------------------------------------------------------------------
// 路由
// --------------------------------------------------------------------------

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		s.handleGet(w, r)
	case http.MethodPost:
		s.handlePost(w, r)
	default:
		sendError(w, "不支持的方法 "+r.Method, 405)
	}
}

func (s *Server) tokenOK(r *http.Request) bool {
	given := r.URL.Query().Get("t")
	if given == "" {
		given = r.Header.Get("X-WB-Token")
	}
	// 常数时间比较：token 是唯一的访问闸门，别给计时侧信道留口子。
	return subtle.ConstantTimeCompare([]byte(given), []byte(s.Token)) == 1
}

func (s *Server) handleGet(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Path
	// 页面本身不含任何数据，所以与 Python 版一致地不做 token 校验；
	// 所有 /api/* 都要 token——数据只从这里出去。
	if path == "/" || path == "/index.html" {
		sendBytes(w, []byte(pageHTML), "text/html; charset=utf-8", 200)
		return
	}
	if !s.tokenOK(r) {
		sendError(w, "token 无效或缺失，请从终端给出的链接进入。", 403)
		return
	}
	switch path {
	case "/api/state":
		s.apiState(w)
	case "/api/survey":
		s.apiSurvey(w)
	case "/api/apply":
		s.apiApply(w, r)
	case "/api/accounts":
		// 账号面板目前只有 Python 实现提供。显式回 501 而不是 404，
		// 前端才能把"这个实现没有"和"接口写错了"区分开。
		sendError(w, "账号与用量面板目前只有 Python 实现提供，请用 python3 tools/wb_ui.py 启动。", 501)
	default:
		sendError(w, "未知端点 "+path, 404)
	}
}

func (s *Server) handlePost(w http.ResponseWriter, r *http.Request) {
	if !s.tokenOK(r) {
		sendError(w, "token 无效或缺失。", 403)
		return
	}
	body := readJSONBody(r)
	switch r.URL.Path {
	case "/api/plan":
		s.apiPlan(w, body)
	case "/api/verify":
		s.apiVerify(w)
	case "/api/restore":
		s.apiRestore(w, body)
	case "/api/backup":
		s.apiBackup(w, body)
	case "/api/quit-clients":
		s.apiQuitClients(w, body)
	default:
		sendError(w, "未知端点 "+r.URL.Path, 404)
	}
}

func readJSONBody(r *http.Request) map[string]any {
	if r.Body == nil {
		return map[string]any{}
	}
	var out map[string]any
	dec := json.NewDecoder(r.Body)
	dec.UseNumber()
	if err := dec.Decode(&out); err != nil {
		return map[string]any{}
	}
	if out == nil {
		return map[string]any{}
	}
	return out
}

// --------------------------------------------------------------------------
// 端点实现
// --------------------------------------------------------------------------

func (s *Server) apiState(w http.ResponseWriter) {
	statuses, err := platform.ClientStatuses()
	if err != nil {
		sendError(w, "进程探测失败："+err.Error(), 400)
		return
	}
	clients := make([]any, 0, len(statuses))
	runningNames := []string{}
	for _, st := range statuses {
		home := s.HomeFor(st.Key)
		dbExists := isFile(filepath.Join(home.Path, bridge.DBName))
		note := ""
		if !dbExists {
			note = "该目录下没有 workbuddy.db"
		}
		procs := make([]any, 0, len(st.Processes))
		for _, p := range st.Processes {
			procs = append(procs, map[string]any{"pid": p.PID, "cmd": p.Cmd})
		}
		clients = append(clients, map[string]any{
			"key":            st.Key,
			"display":        st.Display,
			"hint":           st.Hint,
			"running":        st.Running,
			"processes":      procs,
			"home":           home.Path,
			"home_confirmed": true,
			"home_exists":    isDir(home.Path),
			"db_exists":      dbExists,
			"home_note":      note,
		})
		if st.Running {
			runningNames = append(runningNames, st.Display)
		}
	}

	s.mu.Lock()
	hasPlan := s.planPath != ""
	lastCode := s.lastRunCode
	s.mu.Unlock()

	var last any
	if lastCode != nil {
		last = *lastCode
	}
	sendJSON(w, map[string]any{
		"platform":      platform.PlatformLabel(),
		"state_dir":     s.StateDir,
		"clients":       clients,
		"running_names": runningNames,
		"all_stopped":   len(runningNames) == 0,
		"has_plan":      hasPlan,
		"last_run_code": last,
		"autosync":      autosync.Summary(s.AutosyncRoot, !s.Frozen, autosyncHint()),
	}, 200)
}

func autosyncHint() string {
	return "wb-bridge autosync install"
}

func (s *Server) apiSurvey(w http.ResponseWriter) {
	for _, home := range []*bridge.Home{s.HomeA, s.HomeB} {
		if err := home.RequireValid(); err != nil {
			sendError(w, err.Error(), 400)
			return
		}
	}
	body, err := s.surveyBody()
	if err != nil {
		sendError(w, err.Error(), 400)
		return
	}
	sendJSON(w, body, 200)
}

func (s *Server) surveyBody() (map[string]any, error) {
	infoA, err := s.HomeA.Survey()
	if err != nil {
		return nil, err
	}
	infoB, err := s.HomeB.Survey()
	if err != nil {
		return nil, err
	}
	// 关键一步：Survey 返回的是 Go 的原生类型（[]string、map[string]int64），
	// 不是编码器认识的 any 形状。不先归一化，下面按 []any / map[string]any
	// 取值会**静默取空**——技能对比全是 0、体积全不显示，而 JSON 照样 200。
	// 这类"不报错的空"是最难发现的，所以统一走 Normalize。
	infos := make([]map[string]any, 0, 2)
	for _, raw := range []map[string]any{infoA, infoB} {
		norm, nerr := pyjson.Normalize(raw)
		if nerr != nil {
			return nil, nerr
		}
		info, ok := norm.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("survey 结果不是对象")
		}
		infos = append(infos, info)
	}

	for _, info := range infos {
		sizes, _ := info["sizes"].(map[string]any)
		humanSizes := map[string]any{}
		for name, size := range sizes {
			n := toFloat(size)
			if n == 0 {
				continue
			}
			humanSizes[name] = bridge.Human(n)
		}
		info["sizes_human"] = humanSizes
	}

	setA := stringSet(infos[0]["skills"])
	setB := stringSet(infos[1]["skills"])
	return map[string]any{
		"homes": infos,
		"skills": map[string]any{
			"only_a": difference(setA, setB),
			"only_b": difference(setB, setA),
			"shared": intersection(setA, setB),
		},
	}, nil
}

func (s *Server) apiPlan(w http.ResponseWriter, body map[string]any) {
	opts, _ := body["options"].(map[string]any)
	if opts == nil {
		opts = map[string]any{}
	}
	options := bridge.DefaultOptions()
	// 界面传的是"要不要搬"的正向开关，引擎用的是同一套键名。
	for key, def := range map[string]bool{
		"include_changes": true, "include_skills": true,
		"include_memory": true, "include_claw": true,
	} {
		if v, ok := opts[key]; ok {
			options[key] = truthy(v)
		} else {
			options[key] = def
		}
	}
	for _, key := range []string{"include_plugins", "include_automations",
		"include_storage", "include_connectors", "overwrite_assets"} {
		options[key] = truthy(opts[key])
	}

	plan, err := bridge.BuildPlan(s.HomeA, s.HomeB, options)
	if err != nil {
		sendError(w, err.Error(), 400)
		return
	}
	doc := plan.AsDict()

	planDir := filepath.Join(s.StateDir, "plans")
	if err := os.MkdirAll(planDir, 0o700); err != nil {
		sendError(w, "无法创建计划目录："+err.Error(), 400)
		return
	}
	short := plan.PlanID
	if len(short) > 16 {
		short = short[:16]
	}
	path := filepath.Join(planDir, short+".json")
	text, merr := pyjson.MarshalIndent(doc, 2)
	if merr != nil {
		sendError(w, merr.Error(), 400)
		return
	}
	if werr := os.WriteFile(path, []byte(text), 0o600); werr != nil {
		sendError(w, "无法写入计划文件："+werr.Error(), 400)
		return
	}
	os.Chmod(path, 0o600)

	s.mu.Lock()
	s.planPath, s.planID, s.planDoc = path, plan.PlanID, doc
	s.lastRunCode = nil
	s.mu.Unlock()

	sendJSON(w, planView(doc), 200)
}

// planView 只挑前端要用的字段。
//
// rows 里可能有上百条完整会话行，全塞给前端会让响应膨胀到几 MB，
// 而界面真正展示的只有计数与前 30 条标题。
func planView(doc map[string]any) map[string]any {
	rows, _ := doc["rows"].(map[string]any)
	sample := map[string]any{}
	counts := map[string]any{}
	for _, side := range []string{"a2b", "b2a"} {
		sideRows, _ := rows[side].(map[string]any)
		tableCounts := map[string]any{}
		for table, items := range sideRows {
			tableCounts[table] = len(anyList(items))
		}
		counts[side] = tableCounts

		list := anyList(sideRows["sessions"])
		if len(list) > 30 {
			list = list[:30]
		}
		items := make([]any, 0, len(list))
		for _, raw := range list {
			row, _ := raw.(map[string]any)
			title := asStr(row["title"])
			if title == "" {
				title = "(无标题)"
			}
			items = append(items, map[string]any{
				"id":     row["id"],
				"title":  title,
				"cwd":    row["cwd"],
				"status": row["status"],
			})
		}
		sample[side] = items
	}
	return map[string]any{
		"plan_id":    doc["plan_id"],
		"created_at": doc["created_at"],
		"homes":      doc["homes"],
		"options":    doc["options"],
		"summary":    doc["summary"],
		"counts":     counts,
		"sample":     sample,
	}
}

func (s *Server) apiVerify(w http.ResponseWriter) {
	s.mu.Lock()
	planPath := s.planPath
	s.mu.Unlock()
	if planPath == "" {
		sendError(w, "尚未生成计划。", 400)
		return
	}
	var captured strings.Builder
	results, ok, err := bridge.Verify(s.HomeA, s.HomeB, planPath, func(msg string) {
		captured.WriteString(msg + "\n")
	})
	code := 0
	if err != nil {
		captured.WriteString("\n错误：" + err.Error() + "\n")
		code = 2
	} else if !ok {
		code = 3
	}
	_ = results
	sendJSON(w, map[string]any{"code": code, "log": captured.String()}, 200)
}

func (s *Server) apiRestore(w http.ResponseWriter, body map[string]any) {
	s.mu.Lock()
	planID := s.planID
	s.mu.Unlock()

	confirm := asStr(body["confirm"])
	if confirm == "" {
		confirm = planID
	}
	if planID == "" {
		sendError(w, "本会话还没有计划，无法回滚。", 400)
		return
	}
	if !s.busy.TryLock() {
		sendError(w, "已有操作在执行中，请等待完成。", 409)
		return
	}
	defer s.busy.Unlock()

	runDir := filepath.Join(s.StateDir, "runs", planID)
	if !isDir(runDir) {
		sendError(w, "找不到运行记录目录："+runDir, 400)
		return
	}
	var captured strings.Builder
	_, err := bridge.Restore(runDir, confirm,
		func(msg string) { captured.WriteString(msg + "\n") },
		func(msg string) { captured.WriteString(msg + "\n") })
	code := 0
	if err != nil {
		captured.WriteString("\n错误：" + err.Error() + "\n")
		code = 2
	}
	sendJSON(w, map[string]any{"code": code, "log": captured.String()}, 200)
}

func (s *Server) apiBackup(w http.ResponseWriter, body map[string]any) {
	dest := asStr(body["dest"])
	if dest == "" {
		dest = filepath.Join(s.StateDir, "backups")
	}
	if !s.busy.TryLock() {
		sendError(w, "已有操作在执行中，请等待完成。", 409)
		return
	}
	defer s.busy.Unlock()

	sse := &sseWriter{w: w}
	sse.open()
	sse.send(map[string]any{"type": "start"})
	_, err := bridge.Backup([]*bridge.Home{s.HomeA, s.HomeB}, bridge.BackupOptions{
		Dest: dest, Label: "",
	}, func(msg string) { sse.send(map[string]any{"type": "log", "text": msg + "\n"}) })
	code := 0
	if err != nil {
		sse.send(map[string]any{"type": "log", "text": "\n错误：" + err.Error() + "\n"})
		code = 2
	}
	sse.send(map[string]any{"type": "done", "code": code, "what": "备份"})
}

func (s *Server) apiApply(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	planPath, planID := s.planPath, s.planID
	s.mu.Unlock()

	if planPath == "" || planID == "" {
		sendError(w, "尚未生成计划。", 400)
		return
	}
	// 这一步是**防陈旧**：请求里带的必须是本会话当前的那个计划，
	// 否则说明前端拿的是旧计划，拒绝。
	if r.URL.Query().Get("plan_id") != planID {
		sendError(w, "请求里的 plan_id 与当前计划不一致，请重新生成计划。", 400)
		return
	}
	if !s.busy.TryLock() {
		sendError(w, "已有操作在执行中，请等待完成。", 409)
		return
	}
	defer s.busy.Unlock()

	statuses, serr := platform.ClientStatuses()
	if serr != nil {
		sendError(w, "进程探测失败："+serr.Error(), 400)
		return
	}
	names := []string{}
	for _, st := range statuses {
		if st.Running {
			names = append(names, st.Display)
		}
	}
	if len(names) > 0 {
		sendError(w, strings.Join(names, "、")+
			" 仍在运行。请先完全退出客户端，或勾选「自动退出两个客户端」。", 409)
		return
	}

	// confirm 由服务端用自己的 plan_id 填。引擎那条「--confirm 必须等于 plan_id」
	// 的校验本意是拦人手抄短前缀；界面已经把 id 完整持有，再让用户手打一遍
	// 只是仪式。真正的闸门是上面的 plan_id 一致性检查 + 界面上的一次显式确认点击。
	sse := &sseWriter{w: w}
	sse.open()
	sse.send(map[string]any{"type": "start"})
	emit := func(msg string) {
		sse.send(map[string]any{"type": "log", "text": msg + "\n"})
	}
	_, err := bridge.Apply(s.HomeA, s.HomeB, bridge.ApplyOptions{
		PlanPath: planPath,
		Confirm:  planID,
		StateDir: s.StateDir,
	}, emit, emit)
	code := 0
	if err != nil {
		emit("\n错误：" + err.Error() + "\n")
		code = 2
	}

	s.mu.Lock()
	s.lastRunCode = &code
	s.mu.Unlock()

	sse.send(map[string]any{"type": "done", "code": code, "what": "迁移"})
}

func (s *Server) apiQuitClients(w http.ResponseWriter, body map[string]any) {
	if !s.busy.TryLock() {
		sendError(w, "已有操作在执行中，请等待完成。", 409)
		return
	}
	defer s.busy.Unlock()

	allowForce := truthy(body["allow_force"])
	wait := platform.QuitGrace
	if v, ok := body["wait"]; ok {
		secs := toFloat(v)
		if secs < 1 {
			secs = 1
		}
		if secs > 120 {
			secs = 120
		}
		wait = time.Duration(secs * float64(time.Second))
	}
	result, err := platform.QuitClients(wait, 10*time.Second, allowForce)
	if err != nil {
		sendError(w, "进程探测失败："+err.Error(), 400)
		return
	}
	sendJSON(w, result, 200)
}

// --------------------------------------------------------------------------
// 小工具
// --------------------------------------------------------------------------

func isDir(p string) bool {
	info, err := os.Stat(p)
	return err == nil && info.IsDir()
}

func isFile(p string) bool {
	info, err := os.Stat(p)
	return err == nil && info.Mode().IsRegular()
}

func asStr(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case json.Number:
		return t.String()
	case bool:
		if t {
			return "true"
		}
		return "false"
	}
	return fmt.Sprint(v)
}

func truthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != "" && t != "0" && t != "false"
	case json.Number:
		f, _ := t.Float64()
		return f != 0
	case float64:
		return t != 0
	}
	return true
}

func toFloat(v any) float64 {
	switch t := v.(type) {
	case nil:
		return 0
	case float64:
		return t
	case int:
		return float64(t)
	case int64:
		return float64(t)
	case json.Number:
		f, _ := t.Float64()
		return f
	}
	return 0
}

func anyList(v any) []any {
	if list, ok := v.([]any); ok {
		return list
	}
	return nil
}

func stringSet(v any) map[string]bool {
	out := map[string]bool{}
	if list, ok := v.([]any); ok {
		for _, item := range list {
			if s, ok := item.(string); ok {
				out[s] = true
			}
		}
	}
	return out
}

func sortedKeys(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func difference(a, b map[string]bool) []string {
	out := map[string]bool{}
	for k := range a {
		if !b[k] {
			out[k] = true
		}
	}
	return sortedKeys(out)
}

func intersection(a, b map[string]bool) []string {
	out := map[string]bool{}
	for k := range a {
		if b[k] {
			out[k] = true
		}
	}
	return sortedKeys(out)
}
