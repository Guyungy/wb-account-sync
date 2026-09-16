"""客户端退出逻辑的隔离测试。

这些测试**不碰真实进程**：``_scan_processes`` 被替换成假扫描器，
``request_quit`` 被替换成记账函数。否则跑一次测试就会把开发者的
WorkBuddy 关掉——那才是真正的破坏性副作用。
"""

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
    def test_macos_prefers_osascript_then_falls_back(self):
        spec = platform.CLIENTS_BY_KEY["wb"]
        # osascript 成功：不再发信号。
        with mock.patch.object(platform, "_run_check", return_value=True) as run:
            with mock.patch.object(platform, "_pids_of") as pids:
                self.assertTrue(platform.request_quit(spec, force=False))
        run.assert_called_once()
        self.assertIn('tell application "WorkBuddy" to quit', run.call_args[0][0][2])
        pids.assert_not_called()

    def test_macos_falls_back_to_signal_when_osascript_fails(self):
        spec = platform.CLIENTS_BY_KEY["wb"]
        with mock.patch.object(platform, "_run_check", return_value=False):
            with mock.patch.object(platform, "_pids_of", return_value=[4242]) as pids:
                with mock.patch.object(platform, "_signal_pids", return_value=True) as sig:
                    self.assertTrue(platform.request_quit(spec, force=False))
        pids.assert_called_once_with("wb")
        self.assertFalse(sig.call_args[0][1])

    def test_force_skips_osascript(self):
        spec = platform.CLIENTS_BY_KEY["wb_ai"]
        with mock.patch.object(platform, "_run_check") as run:
            with mock.patch.object(platform, "_pids_of", return_value=[7]):
                with mock.patch.object(platform, "_signal_pids", return_value=True) as sig:
                    platform.request_quit(spec, force=True)
        run.assert_not_called()
        self.assertTrue(sig.call_args[0][1])


if __name__ == "__main__":
    unittest.main()
