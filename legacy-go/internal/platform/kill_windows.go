//go:build windows

package platform

import (
	"os/exec"
	"strconv"
	"syscall"
)

// CreateNoWindow / HideWindow 常量，避免在 GUI 程序里弹出控制台黑框。
const createNoWindow = 0x08000000

// killPid 在 Windows 上用 taskkill。/T 连子进程一起收，
// 客户端会派出 helper 子进程，只杀父进程会留下孤儿。
func killPid(pid int, force bool) error {
	args := []string{"/PID", strconv.Itoa(pid), "/T"}
	if force {
		args = append(args, "/F")
	}
	cmd := exec.Command("taskkill", args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: createNoWindow}
	return cmd.Run()
}

// hideWindow 让子进程不闪黑框。
func hideWindow(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.HideWindow = true
	cmd.SysProcAttr.CreationFlags = createNoWindow
}
