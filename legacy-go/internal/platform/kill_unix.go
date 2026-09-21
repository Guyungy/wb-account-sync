//go:build !windows

package platform

import (
	"os/exec"
	"syscall"
)

// killPid 在类 Unix 上直接发信号。force=true 用 SIGKILL。
func killPid(pid int, force bool) error {
	sig := syscall.SIGTERM
	if force {
		sig = syscall.SIGKILL
	}
	return syscall.Kill(pid, sig)
}

// hideWindow 在类 Unix 上是空操作。
func hideWindow(*exec.Cmd) {}
