"""客户端退出逻辑的隔离测试。

这些测试**不碰真实进程**：``_scan_processes`` 被替换成假扫描器，
``request_quit`` 被替换成记账函数。否则跑一次测试就会把开发者的
WorkBuddy 关掉——那才是真正的破坏性副作用。
"""

import io
import os
import sys
import unittest
from unittest import mock

TOOLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"
)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

import wb_platform as platform  # noqa: E402


def _proc(pid: int = 101, app: str = "WorkBuddy.app") -> dict:
    return {"pid": pid, "cmd": f"/Applications/{app}/Contents/MacOS/Electron"}


class _FakeProcs:
    """假的进程表；可控制"退出请求是否真的生效"。"""

    def __init__(self, running=(), graceful_works=True, force_works=True):
        self.running = set(running)
        self.graceful_works = graceful_works
        self.force_works = force_works
        self.calls = []

    def scan(self) -> dict:
        return {
            key: ([_proc()] if key in self.running else [])
            for key in ("wb", "wb_ai")
        }

    def request(self, spec, force=False):
        self.calls.append((spec.key, force))
        works = self.force_works if force else self.graceful_works
        if works:
            self.running.discard(spec.key)
        return True


class QuitClientsTest(unittest.TestCase):
    def setUp(self):
        # 别让轮询真的按 0.4 秒睡。
        self._poll = mock.patch.object(platform, "POLL_SECONDS", 0.01)
        self._poll.start()
        self.addCleanup(self._poll.stop)

    def _run(self, fake, **kwargs):
        with mock.patch.object(platform, "_scan_processes", fake.scan):
            with mock.patch.object(platform, "request_quit", fake.request):
                return platform.quit_clients(**kwargs)

    def test_nothing_running_is_a_noop(self):
        fake = _FakeProcs(running=())
        result = self._run(fake, wait=0.05)
        self.assertTrue(result["ok"])
        self.assertEqual(result["remaining"], [])
        self.assertEqual(fake.calls, [])

    def test_graceful_quit_succeeds(self):
        fake = _FakeProcs(running=("wb",), graceful_works=True)
        result = self._run(fake, wait=2.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["remaining"], [])
        self.assertEqual(result["forced"], [])
        # 只发过优雅退出请求，没动强杀。
        self.assertEqual(fake.calls, [("wb", False)])
        self.assertEqual([r["key"] for r in result["requested"]], ["wb"])

    def test_stubborn_client_is_reported_not_forced(self):
        """没拿到强杀授权时，宁可如实报告失败，也不越过那条线。"""
        fake = _FakeProcs(running=("wb",), graceful_works=False, force_works=False)
        result = self._run(fake, wait=0.05, allow_force=False)
        self.assertFalse(result["ok"])
        self.assertEqual([r["key"] for r in result["remaining"]], ["wb"])
        self.assertEqual(result["forced"], [])
        # 重发过优雅请求，但一次 force 都没有。
        self.assertTrue(fake.calls)
        self.assertTrue(all(not force for _, force in fake.calls))

    def test_force_kills_and_reports(self):
        fake = _FakeProcs(running=("wb",), graceful_works=False, force_works=True)
        result = self._run(fake, wait=0.05, allow_force=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["forced"], ["wb"])
        self.assertEqual(fake.calls, [("wb", False), ("wb", True)])

    def test_only_running_clients_are_targeted(self):
        fake = _FakeProcs(running=("wb_ai",), graceful_works=True)
        result = self._run(fake, wait=2.0)
        self.assertTrue(result["ok"])
        self.assertEqual(fake.calls, [("wb_ai", False)])


class RequestQuitTest(unittest.TestCase):
    """``request_quit`` 的分支选择。

    **这几条必须显式声明自己模拟哪个平台。** ``request_quit`` 读的是模块级
    ``IS_MAC`` / ``IS_WIN``，依赖宿主平台的话：在 Linux 上 osascript 分支
    整个被跳过，断言直接失败；而在 macOS 上"通过"也只是因为宿主恰好是 mac，
    并没有真正断言到分支选择。显式 patch 之后，任何平台上跑的都是同一套逻辑。
    """

    @staticmethod
    def _as(platform_name: str):
        """把 ``request_quit`` 眼中的平台固定成 platform_name。"""
        return mock.patch.multiple(
            platform,
            IS_MAC=(platform_name == "mac"),
            IS_WIN=(platform_name == "win"),
            IS_LINUX=(platform_name == "linux"),
        )

    def test_macos_prefers_osascript_then_falls_back(self):
        spec = platform.CLIENTS_BY_KEY["wb"]
        # osascript 成功：不再发信号。
        with self._as("mac"):
            with mock.patch.object(platform, "_run_check", return_value=True) as run:
                with mock.patch.object(platform, "_pids_of") as pids:
                    self.assertTrue(platform.request_quit(spec, force=False))
        run.assert_called_once()
        self.assertIn('tell application "WorkBuddy" to quit', run.call_args[0][0][2])
        pids.assert_not_called()

    def test_macos_falls_back_to_signal_when_osascript_fails(self):
        spec = platform.CLIENTS_BY_KEY["wb"]
        with self._as("mac"):
            with mock.patch.object(platform, "_run_check", return_value=False):
                with mock.patch.object(platform, "_pids_of", return_value=[4242]) as pids:
                    with mock.patch.object(platform, "_signal_pids", return_value=True) as sig:
                        self.assertTrue(platform.request_quit(spec, force=False))
        pids.assert_called_once_with("wb")
        self.assertFalse(sig.call_args[0][1])

    def test_force_skips_osascript(self):
        spec = platform.CLIENTS_BY_KEY["wb_ai"]
        with self._as("mac"):
            with mock.patch.object(platform, "_run_check") as run:
                with mock.patch.object(platform, "_pids_of", return_value=[7]):
                    with mock.patch.object(platform, "_signal_pids", return_value=True) as sig:
                        platform.request_quit(spec, force=True)
        run.assert_not_called()
        self.assertTrue(sig.call_args[0][1])

    def test_windows_uses_taskkill(self):
        """Windows 分支此前没有测试覆盖，而这条路径只在真机 Windows 上跑过 ——
        显式模拟平台之后，CI 上也能守住它。"""
        spec = platform.CLIENTS_BY_KEY["wb"]
        # force=True 时 taskkill 要带 /F
        with self._as("win"):
            with mock.patch.object(platform, "_run_check", return_value=True) as run:
                self.assertTrue(platform.request_quit(spec, force=True))
        run.assert_called_once_with(["taskkill", "/IM", spec.win_image, "/F"])

        with self._as("win"):
            with mock.patch.object(platform, "_run_check", return_value=True) as run:
                platform.request_quit(spec, force=False)
        run.assert_called_once_with(["taskkill", "/IM", spec.win_image])

    def test_linux_signals_pids_without_osascript(self):
        """Linux 上没有 AppleEvent 也没有 taskkill，只能发信号。"""
        spec = platform.CLIENTS_BY_KEY["wb"]
        with self._as("linux"):
            with mock.patch.object(platform, "_run_check") as run:
                with mock.patch.object(platform, "_pids_of", return_value=[11]) as pids:
                    with mock.patch.object(platform, "_signal_pids", return_value=True) as sig:
                        self.assertTrue(platform.request_quit(spec, force=False))
        run.assert_not_called()
        pids.assert_called_once_with("wb")
        self.assertFalse(sig.call_args[0][1])


class LinuxScanTest(unittest.TestCase):
    """``_scan_linux`` 按 ``/proc/<pid>/cmdline`` 里 ``argv[0]`` 的文件名**精确相等**匹配。

    为什么要单独测这条：它在 macOS 上根本跑不到（读的是 ``/proc``）。
    而 ``test_go_ui_parity`` 里「真的有客户端在跑」这个前提，在 Linux CI 上
    正是靠放一个同名真进程来满足的 —— 匹配逻辑一旦变了，那条测试不是变成
    「跳过」，而是**直接挂到超时**（旧写法就是这么红的）。
    所以这里用假的 ``/proc`` 把匹配口径钉死，不必依赖真 Linux 环境。
    """

    PIDS = ("4242", "4243", "4244")

    def _scan(self, cmdlines: dict[str, bytes]) -> dict:
        found = {spec.key: [] for spec in platform.CLIENTS}

        def fake_listdir(path):
            self.assertEqual("/proc", path)
            # 混入非数字条目：内核线程与统计文件都必须被跳过
            return list(self.PIDS) + ["self", "meminfo", "sys"]

        def fake_open(path, mode="r", *args, **kwargs):
            for pid, raw in cmdlines.items():
                if path == f"/proc/{pid}/cmdline":
                    return io.BytesIO(raw)
            raise FileNotFoundError(path)

        with mock.patch.object(platform.os, "listdir", side_effect=fake_listdir):
            with mock.patch.object(platform, "open", side_effect=fake_open, create=True):
                platform._scan_linux(found)
        return found

    def test_matches_exact_basename_only(self):
        found = self._scan({
            "4242": b"/opt/workbuddy\x00",
            "4243": b"/opt/workbuddy-ai\x00",
            # 包含关系不算命中："workbuddy" 不能把 "workbuddy-helper" 也吃掉
            "4244": b"/opt/workbuddy-helper\x00",
        })
        self.assertEqual([p["pid"] for p in found["wb"]], [4242])
        self.assertEqual([p["pid"] for p in found["wb_ai"]], [4243])

    def test_takes_basename_of_full_path(self):
        found = self._scan({"4242": b"/Applications/workbuddy\x00--flag\x00"})
        self.assertEqual([p["pid"] for p in found["wb"]], [4242])

    def test_keyword_arguments_are_not_treated_as_names(self):
        # argv[0] 之外的参数里出现同样的词，不能误判
        found = self._scan({"4242": b"/usr/bin/sleep\x00workbuddy\x00"})
        self.assertEqual(found["wb"], [])

    def test_empty_cmdline_is_skipped(self):
        # 内核线程的 cmdline 是空的，不能因此炸掉整个探测
        found = self._scan({"4242": b""})
        self.assertEqual(found["wb"], [])
        self.assertEqual(found["wb_ai"], [])


if __name__ == "__main__":
    unittest.main()
