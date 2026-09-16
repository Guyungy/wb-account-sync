// Package autosync 管自动同步代理的状态根与路径布局。
//
// 目前只做到"读"：界面要显示代理装没装、暂停没、上次跑得怎么样。
// 安装/暂停/卸载（launchd plist、systemd timer、任务计划程序）属于下一步，
// 但路径布局必须先在这里定下来——否则界面与代理进程会各认一个目录。
package autosync

import (
	"encoding/json"
	"os"
	"path/filepath"

	"github.com/Guyungy/wb-account-sync/gobridge/internal/platform"
)

// Label 是 launchd 代理的标识。
const Label = "com.workbuddy.home-bridge-autosync"

// DefaultStateRoot 与界面的默认 state-dir 一致；可用 WB_AUTOSYNC_ROOT 单独覆盖。
const DefaultStateRoot = "~/.wb-home-bridge"

// Root 解析状态根（展开 ~、保证是绝对路径）。
func Root(explicit string) string {
	if explicit == "" {
		explicit = os.Getenv("WB_AUTOSYNC_ROOT")
	}
	if explicit == "" {
		explicit = DefaultStateRoot
	}
	return platform.ExpandPath(explicit)
}

// Dir 是自动同步自己的子目录。与界面的 --state-dir 共用根，但内容分开：
// 代理是独立进程，把它的 lock / log / status 混进界面目录会互相踩。
func Dir(root string) string { return filepath.Join(Root(root), "autosync") }

// StatusPath 是代理写下的状态快照。
func StatusPath(root string) string { return filepath.Join(Dir(root), "status.json") }

// LogPath 是代理运行日志。
func LogPath(root string) string { return filepath.Join(Dir(root), "autosync.log") }

// PausedPath 存在即表示暂停。
func PausedPath(root string) string { return filepath.Join(Dir(root), "PAUSED") }

// PlistTarget 是 macOS launchd 代理的落点。
func PlistTarget() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, "Library", "LaunchAgents", Label+".plist")
}

// ReadStatus 读代理状态快照；读不到或解析失败都返回空表——
// 界面只是展示，不该因为一个日志文件缺失就报错。
func ReadStatus(root string) map[string]any {
	raw, err := os.ReadFile(StatusPath(root))
	if err != nil {
		return map[string]any{}
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		return map[string]any{}
	}
	return out
}

// Summary 是给界面用的自动同步概览，字段名与 Python 版逐一对应。
//
// 非 macOS 直接给出"不支持"的说明，而不是报模块不可用：
// 自动同步靠 launchd，Windows / Linux 上功能确实不存在，
// 但盘点、计划、备份、执行、回滚都不受影响，界面要能如实这么讲。
func Summary(root string, installAvailable bool, installHint string) map[string]any {
	if !platform.IsMac {
		return map[string]any{
			"available": false,
			"supported": false,
			"note": "自动同步依赖 macOS 的 launchd，当前系统（" +
				platform.PlatformLabel() + "）暂不支持。" +
				"盘点、计划、备份、执行、回滚都不受影响，仍可手动操作。",
		}
	}
	plist := PlistTarget()
	_, installedErr := os.Stat(plist)
	_, pausedErr := os.Stat(PausedPath(root))
	return map[string]any{
		"available":         true,
		"supported":         true,
		"installed":         installedErr == nil,
		"paused":            pausedErr == nil,
		"state_root":        Root(root),
		"log":               LogPath(root),
		"plist":             plist,
		"status":            ReadStatus(root),
		"install_available": installAvailable,
		"install_hint":      installHint,
	}
}
