import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collect_jobs as app
from turn_csv import COLUMNS, export_csv


class CsvExportTests(unittest.TestCase):
    def test_same_name_export_preserves_chinese_multiline_and_missing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "本轮岗位.json"
            jobs = [{"title": "Python 开发", "company": "示例公司", "description": '职责：开发,测试\n要求：掌握"Python"',
                     "description_status": "ok"}, {"title": "失败岗位", "description_status": "error"}]
            app.save_jobs(path, {"jobs": jobs})
            output = export_csv(path)
            self.assertEqual(output, path.with_suffix(".csv"))
            self.assertTrue(output.read_bytes().startswith(b"\xef\xbb\xbf"))
            with output.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["description"], jobs[0]["description"])
            self.assertEqual(rows[1]["company"], "")
            self.assertEqual(rows[1]["description_status"], "error")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["jobs"], jobs)

    def test_empty_batch_and_explicit_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            output = Path(directory) / "custom.csv"
            app.save_jobs(path, {"jobs": []})
            self.assertEqual(export_csv(path, output), output)
            with output.open(encoding="utf-8-sig", newline="") as f:
                self.assertEqual(list(csv.reader(f)), [COLUMNS])

    def test_json_cannot_be_overwritten_by_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            app.save_jobs(path, {"jobs": []})
            with self.assertRaises(ValueError):
                export_csv(path, path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"jobs": []})

    def test_collection_exports_final_records_and_preserves_json_on_export_failure(self):
        for case, close_tab in [(case, close_tab)
                                for case in ("complete", "stopped", "export_failure", "missing_ocr", "interrupted")
                                for close_tab in (False, True)]:
            with self.subTest(case=case, close_tab=close_tab), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)

                def collect(cdp, payload, *args):
                    payload["jobs"].append({"id": "a", "title": "Python 开发", "company": "示例公司",
                                            "salary": "150元/天", "salary_status": "ok", "description_status": "pending"})
                    if case == "stopped":
                        raise app.AccessGate("访问过于频繁")
                    if case == "missing_ocr":
                        raise ImportError("onnxruntime is not installed.")
                    if case == "interrupted":
                        raise KeyboardInterrupt()
                    return "requested_limit_reached"

                with patch("sys.argv", ["collect_jobs.py", "--limit", "1"] + (["--close-tab"] if close_tab else [])), \
                        patch.object(app, "OUTPUT", output), patch.object(app, "check_ocr_runtime"), patch.object(app, "ensure_browser"), \
                        patch.object(app, "open_task_tab", return_value={"id": "task", "webSocketDebuggerUrl": "ws://test"}), \
                        patch.object(app.websocket, "create_connection"), patch.object(app, "RawCDP"), \
                        patch.object(app, "navigate"), patch.object(app, "require_login"), \
                        patch.object(app, "close_task_tab") as close, \
                        patch.object(app, "collect_list", side_effect=collect), \
                        patch.object(app, "read_description_with_login", return_value="岗位正文"), \
                        patch.object(app, "export_csv", side_effect=PermissionError("Excel 占用") if case == "export_failure" else export_csv):
                    result = app.main()
                self.assertEqual(result, 0 if case == "complete" else 1)
                if close_tab:
                    close.assert_called_once_with("task")
                else:
                    close.assert_not_called()
                path = next((output / "runs").glob("*/jobs.json"))
                report = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "stopped" if case in ("stopped", "missing_ocr", "interrupted") else "complete")
                if case == "export_failure":
                    self.assertFalse(path.with_suffix(".csv").exists())
                else:
                    with path.with_suffix(".csv").open(encoding="utf-8-sig", newline="") as f:
                        row = next(csv.DictReader(f))
                    self.assertEqual(row["description_status"], "not_processed" if case in ("stopped", "missing_ocr", "interrupted") else "ok")


if __name__ == "__main__":
    unittest.main()
