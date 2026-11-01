import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs as app
import runtime_check


class RuntimeCheckTests(unittest.TestCase):
    def test_native_runtime_is_imported_and_providers_checked(self):
        module = Mock()
        module.get_available_providers.return_value = ["CPUExecutionProvider"]
        with patch.object(runtime_check.importlib, "import_module", return_value=module) as load:
            runtime_check.check_ocr_runtime()
        load.assert_called_once_with("onnxruntime")
        module.get_available_providers.assert_called_once()

    def test_missing_package_and_native_import_failure_have_actionable_messages(self):
        for error in (ModuleNotFoundError("No module named 'onnxruntime'"), ImportError("DLL load failed")):
            with self.subTest(error=error), patch.object(runtime_check.importlib, "import_module", side_effect=error):
                with self.assertRaises(RuntimeError) as raised:
                    runtime_check.check_ocr_runtime()
                message = str(raised.exception)
                self.assertIn(sys.executable, message)
                self.assertIn("-m pip install -r", message)
                self.assertIn("requirements.txt", message)
                self.assertIn(str(error), message)

    def test_runtime_without_providers_is_not_accepted(self):
        module = Mock()
        module.get_available_providers.return_value = []
        with patch.object(runtime_check.importlib, "import_module", return_value=module):
            with self.assertRaisesRegex(RuntimeError, "没有可用的推理提供程序"):
                runtime_check.check_ocr_runtime()

    def test_missing_runtime_stops_before_browser_or_run_directory_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with patch("sys.argv", ["collect_jobs.py"]), patch.object(app, "OUTPUT", output), \
                    patch.object(runtime_check.importlib, "import_module", side_effect=ModuleNotFoundError("onnxruntime")), \
                    patch.object(app, "ensure_browser") as browser, patch.object(app, "open_task_tab") as tab, \
                    contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(app.main(), 1)
            browser.assert_not_called()
            tab.assert_not_called()
            self.assertFalse(output.exists())
            self.assertIn("OCR 环境检查失败", printed.getvalue())

    def test_help_does_not_require_native_backend(self):
        with patch("sys.argv", ["collect_jobs.py", "--help"]), \
                patch.object(app, "check_ocr_runtime") as check, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                app.main()
        self.assertEqual(raised.exception.code, 0)
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
