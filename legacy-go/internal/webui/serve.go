package webui

import (
	"fmt"
	"net"

	"net/http"
	"os/exec"

	"time"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/platform"
)

// PickPort 挑一个可用的 127.0.0.1 端口；preferred 为 0 时交给内核分配。
//
// 注意必须回读真实端口：`bind(":0")` 一定会成功，0 的含义是"随便给一个"。
// 直接把传进来的 0 当结果用会拼出 `http://127.0.0.1:0/` 这种无效地址，
// 浏览器打不开，用户看到的就是"双击没反应"——这个坑 Python 版踩过一次。
func PickPort(preferred int) (int, error) {
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", preferred))
	if err == nil {
		defer ln.Close()
		return ln.Addr().(*net.TCPAddr).Port, nil
	}
	if preferred != 0 {
		ln, err2 := net.Listen("tcp", "127.0.0.1:0")
		if err2 != nil {
			return 0, err2
		}
		defer ln.Close()
		return ln.Addr().(*net.TCPAddr).Port, nil
	}
	return 0, err
}

// URL 拼出带 token 的访问地址。
func (s *Server) URL(port int) string {
	return fmt.Sprintf("http://127.0.0.1:%d/?t=%s", port, s.Token)
}

// Listen 绑定端口并把服务挂上去。返回的 net.Listener 用于拿真实端口。
//
// 只绑 127.0.0.1：这是个能改用户数据的本地工具，不该出现在局域网上。
func (s *Server) Listen(preferred int) (net.Listener, int, error) {
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", preferred))
	if err != nil {
		return nil, 0, err
	}
	port := ln.Addr().(*net.TCPAddr).Port
	srv := &http.Server{
		Handler:      s,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 0, // SSE 是长连接，写超时会把流掐断
	}
	go srv.Serve(ln)
	return ln, port, nil
}

// OpenBrowser 打开默认浏览器。
//
// macOS 走 /usr/bin/open 而不是 Go 自己的 open 封装：后者在打包成 .app 后
// 同样会碰到 AppleEvent 超时，而 open 经 LaunchServices 是可靠的。
func OpenBrowser(url string) bool {
	var cmd *exec.Cmd
	switch {
	case platform.IsMac:
		cmd = exec.Command("/usr/bin/open", url)
	case platform.IsWin:
		cmd = exec.Command("rundll32", "url.dll,FileProtocolHandler", url)
	default:
		cmd = exec.Command("xdg-open", url)
	}
	if err := cmd.Start(); err != nil {
		return false
	}
	// 不 Wait：浏览器是长期进程，等它退出等于把界面卡死。
	go cmd.Wait()
	return true
}

// OpenAndNotify 启动后自动开浏览器，**失败时把地址摊给用户**。
//
// 打包版没有终端，而带 token 的地址只打印在 stdout 里——浏览器一旦没打开，
// 用户既看不到界面、也拿不到地址，现象与"双击没反应"完全一样。
func (s *Server) OpenAndNotify(url string) bool {
	if OpenBrowser(url) {
		return true
	}
	if s.NotifyHook != nil {
		s.NotifyHook("wb-account-sync",
			"服务已经启动，但没能自动打开浏览器。\n\n"+
				"请把下面这个地址复制到浏览器打开（其中包含本次访问的令牌，"+
				"少了它打不开数据）：\n\n"+url)
	}
	return false
}
