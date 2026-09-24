import importlib.util
import sys
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT / "tools" / "修复宝玉助手连接.py"
_spec = importlib.util.spec_from_file_location("port_patch", PATCH_PATH)
port_patch = importlib.util.module_from_spec(_spec)
sys.modules["port_patch"] = port_patch
_spec.loader.exec_module(port_patch)


class ProcessDetectionTests(unittest.TestCase):
    @patch("port_patch.subprocess.run")
    def test_allows_patch_when_game_process_is_absent(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "INFO: No tasks are running...\r\n", "")

        port_patch.assert_game_stopped()

        self.assertEqual(run.call_args.args[0][1:], [
            "/FI", "IMAGENAME eq Genesis.exe", "/FO", "CSV", "/NH",
        ])

    @patch("port_patch.subprocess.run")
    def test_refuses_patch_when_game_is_running(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, '"Genesis.exe","1234","Console","1","10,000 K"\r\n', "")

        with self.assertRaisesRegex(RuntimeError, "Genesis.exe"):
            port_patch.assert_game_stopped()

    @patch("port_patch.subprocess.run", side_effect=OSError("missing"))
    def test_refuses_patch_if_windows_process_check_fails(self, _run):
        with self.assertRaisesRegex(RuntimeError, "拒绝修改"):
            port_patch.assert_game_stopped()

    @patch("port_patch.subprocess.run")
    def test_refuses_patch_if_tasklist_returns_error(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "access denied")

        with self.assertRaisesRegex(RuntimeError, "access denied"):
            port_patch.assert_game_stopped()


if __name__ == "__main__":
    unittest.main()
