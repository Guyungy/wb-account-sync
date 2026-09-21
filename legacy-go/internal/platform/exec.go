package platform

import (
	"context"
	"os/exec"
	"time"
)

// runCheck 跑一条外部命令，只关心它是否成功返回 0。
// 超时或命令不存在都算失败——调用方据此决定是否退回到下一种手段。
func runCheck(timeout time.Duration, name string, args ...string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Stdout = nil
	cmd.Stderr = nil
	cmd.Stdin = nil
	hideWindow(cmd)
	if err := cmd.Run(); err != nil {
		return false
	}
	return true
}
