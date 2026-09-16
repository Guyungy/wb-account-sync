package platform

import (
	"os/exec"
	"strings"
)

// Notify 弹一个原生提示框。
//
// 只在"没有终端可看"的场景用它：打包版没法把信息写到用户眼前，
// 而带 token 的地址又必须让用户拿到。有终端时调用方应该直接打印。
//
// 实现方式是尽量少依赖：macOS 用 osascript（系统自带，支持中文），
// Windows 用 PowerShell 的 MessageBox，Linux 尽力用 zenity。
// 弹不出来不算致命——调用方本来就是在做"尽力而为"的兜底。
func Notify(title, message string) bool {
	var cmd *exec.Cmd
	switch {
	case IsMac:
		// 用 argv 传参而不是拼 AppleScript 源码：消息里可能有引号、换行，
		// 拼进去会变成语法错误，而这里恰恰是"出问题时"才走的路径。
		cmd = exec.Command("osascript",
			"-e", "on run argv",
			"-e", "display dialog (item 2 of argv) with title (item 1 of argv) buttons {\"好\"} default button 1",
			"-e", "end run",
			title, message)
	case IsWin:
		cmd = exec.Command("powershell", "-NoProfile", "-Command",
			"Add-Type -AssemblyName PresentationFramework;"+
				"[System.Windows.MessageBox]::Show($args[1],$args[0])",
			title, message)
	default:
		if _, err := exec.LookPath("zenity"); err != nil {
			return false
		}
		cmd = exec.Command("zenity", "--info",
			"--title="+title, "--text="+strings.ReplaceAll(message, "%", "%%"))
	}
	if err := cmd.Run(); err != nil {
		return false
	}
	return true
}
