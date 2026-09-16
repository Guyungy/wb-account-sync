# 桌面应用（macOS / Windows）

把工具打成原生应用，**用户不需要装 Python**，双击图标即用。

界面显示在应用**自己的窗口**里（macOS 走 WKWebView、Windows 走 WebView2，
都是系统自带组件，不用另外装运行时），不会往外跳浏览器。

---

## 下载

从 [Releases](https://github.com/Guyungy/wb-account-sync/releases) 取：

| 平台 | 文件 | 得到什么 |
|---|---|---|
| macOS | `wb-account-sync-macos.zip` | 解压出 `wb-account-sync.app`，拖进「应用程序」 |
| Windows | `wb-account-sync-windows.zip` | 解压出 `wb-account-sync.exe`，双击运行 |

包体积：macOS 约 **39 MB**（内含 Python 运行时与 pywebview 的 WebView 绑定），
运行时不依赖任何外部程序。

---

## 首次打开会被系统拦一下

这两个产物都**没有代码签名证书**，两个系统都会拦一次。这是预期行为，不是文件损坏。

### macOS：Gatekeeper

会提示「无法打开，因为 Apple 无法检查其是否包含恶意软件」。
任选一种放行：

- 在「应用程序」里**右键点图标 → 打开 → 再点「打开」**（只需一次）
- 或执行：`xattr -dr com.apple.quarantine /Applications/wb-account-sync.app`

构建时做的是 **ad-hoc 签名**（`codesign -s -`），它能保证二进制没被改动过，
但不构成 Apple 认可的开发者身份。要彻底消除这个提示，需要 Apple Developer
账号（$99/年）做真正的签名 + 公证。

### Windows：SmartScreen

会提示「Windows 已保护你的电脑」。点**「更多信息」→「仍要运行」**。
要免掉这一步需要购买代码签名证书（EV 证书可即时建立信誉）。

---

## 怎么用

双击图标后：

1. 弹出应用自己的窗口，内部服务起在 `127.0.0.1` 的一个随机端口上。
   页面本身要凭一次性 token 访问，token 每次启动重新生成、只绑本机；
2. 界面上实时显示两个客户端是否**已完全退出**；
3. **只有退出后「执行」按钮才会解锁。**

**关掉窗口就等于退出应用**，内部服务随之停止。

### 窗口起不来时会怎样

WebView 在极少数环境下会不可用（比如系统缺 WebView2、或在没有图形会话的
远程 shell 里）。这时应用会**自动退回浏览器**，并用系统弹窗把带 token 的
完整地址摆出来让你复制 —— 不会出现"双击了但什么都没有"。

想强制走浏览器（不用自带窗口），加 `--no-window`。

命令行运行时默认仍是浏览器方式，加 `--window` 才用自带窗口。

macOS 上如果想让它在 Launchpad 里更好找，从「应用程序」拖到 Dock 即可。

---

## 「双击没反应」怎么查

窗口模式没有终端，异常不会打在任何地方。用自检模式拿诊断信息：

```bash
# macOS
/Applications/wb-account-sync.app/Contents/MacOS/wb-account-sync --selftest

# Windows
wb-account-sync.exe --selftest
```

输出是一段 JSON，`"ok": true` 表示打包完整（模块都收进去了、客户端探测正常）。
`ok: false` 时看 `modules` 里哪一项是 `FAILED`。

还要看 **`"webview"`** 这一项 —— 它决定界面是**弹自己的窗口**还是**退回浏览器**：

| 值 | 含义 |
|---|---|
| `"ok"` | WebView 可用，双击后弹的是应用自己的窗口 |
| `"MISSING: ..."` | pywebview 没打进去，双击后会走浏览器路径（功能仍可用，但会跳浏览器） |

这个开关是为打包产物准备的：`wb_autosync` 在界面里是延迟 import、pywebview 的
后端是**按平台动态挑**的，静态分析都追不到，而漏掉之后的现象刚好就是"没反应"
或者"怎么跳浏览器了"。

---

## 平台功能对照

| 功能 | macOS | Windows |
|---|---|---|
| 客户端进程探测与数据目录定位 | ✅ 已实测 | ⚠️ 未实测 |
| 盘点 / 计划 / 备份 / 执行 / 核验 / 回滚 | ✅ | ⚠️ 逻辑同一套，未实测 |
| 自带窗口（WKWebView / WebView2） | ✅ | ⚠️ 需 WebView2 运行时 |
| 退回浏览器的兜底 | ✅ | ✅ |
| 自动同步（机会式代理） | ✅ launchd | ❌ 需改用任务计划程序，**尚未实现** |

**Windows 侧从未在真机上跑过。** 具体来说：

- `%APPDATA%\WorkBuddy`、`%APPDATA%\WorkBuddy AI` 这两个数据目录是**推断值**，
  没有在装了 Windows 客户端的环境里核对过。探测不到时界面会提示"未找到数据目录"，
  此时用 `--home-a` / `--home-b` 显式指定（或用 `tools/ui.bat --home-a ...`）。
- 进程检测按镜像名 `WorkBuddy.exe` / `WorkBuddy AI.exe` 匹配，同样未经核对。
  如果 Windows 客户端的进程名不是这个，界面会一直显示"已退出"——**这是危险方向
  的错误**（明明在运行却以为退出了），所以在 Windows 上首次使用前，
  请先用任务管理器确认这两个进程名。
- 打包版**不含**自动同步安装器（它依赖 `tools/wb_autosync.py` 的路径）。
  macOS 上要用自动同步，请用源码运行方式安装。

---

## 自己构建

**PyInstaller 不支持交叉编译。** macOS 的 `.app` 只能在 macOS 上构建，
Windows 的 `.exe` 只能在 Windows 上构建。

本机构建（在仓库根目录）：

```bash
python -m pip install pyinstaller
python -m PyInstaller packaging/wb-account-sync.spec --noconfirm
```

产物在 `dist/`：macOS 是 `wb-account-sync.app`，Windows 是 `wb-account-sync.exe`。

打包完先自检：

```bash
dist/wb-account-sync.app/Contents/MacOS/wb-account-sync --selftest
```

CI 构建见 [`.github/workflows/app-build.yml`](../.github/workflows/app-build.yml)：
矩阵跑 `macos-latest` + `windows-latest`，构建后自检、ad-hoc 签名、
用 `ditto` 打包（保留符号链接与权限位），推 `v*` 标签时自动附加到同名 Release。

---

## 已知限制

- **Windows 需要 WebView2 运行时**才弹得出自带窗口。Windows 11 和
  Windows 10 1803+ 一般已预装；更老的系统需要先装
  [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。
  没装也不会坏 —— 会自动退回浏览器并弹窗给出地址。
- **没有应用图标**，显示的是系统默认图标。
- **未签名、未公证**，首次打开都要手动放行一次（见上）。
- 打包版**不能安装自动同步代理**，需要在源码方式下装。
- 产物是 onedir（macOS `.app` 天然是目录）。Windows 是单文件，
  首次启动要解压到临时目录，比后续启动慢一两秒。
