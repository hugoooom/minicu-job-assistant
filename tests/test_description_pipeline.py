import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs as app
import description_ocr as ocr
import retry_description_ocr as retry


def payload(count=3):
    return {"requested_count": count, "jobs": [
        {"id": str(i), "title": f"岗位{i}", "url": f"https://example.test/{i}",
         "description_status": "pending", "salary_status": "ok"}
        for i in range(count)
    ]}


class DescriptionPipelineTests(unittest.TestCase):
    def test_browser_continues_while_ocr_runs_and_only_main_thread_writes_jobs(self):
        main_thread = threading.get_ident()
        started, release = threading.Event(), threading.Event()
        worker_threads = []
        data = payload()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            def read(cdp, job, target, no_prompt, capture_only):
                self.assertEqual(threading.get_ident(), main_thread)
                self.assertTrue(capture_only)
                if job["id"] == "0":
                    return ocr.CapturedDescription("0", folder / "0.json")
                if job["id"] == "1":
                    self.assertTrue(started.wait(5), "OCR 未在后台开始")
                    self.assertEqual(data["jobs"][0]["description_status"], "ocr_pending")
                    self.assertEqual(data["description_success_count"], 0)
                    release.set()
                return f"正文{job['id']}"

            def recognize(captured):
                worker_threads.append(threading.get_ident())
                started.set()
                if not release.wait(5):
                    raise AssertionError("浏览器未能在 OCR 期间继续")
                return "正文0", {"description_source": "OCR"}

            actual_save = app.save_jobs
            def save(path, value):
                self.assertEqual(threading.get_ident(), main_thread)
                actual_save(path, value)

            with patch.object(app, "read_description_with_login", side_effect=read), \
                    patch.object(app, "DescriptionOCRWorker", return_value=recognize), \
                    patch.object(app, "save_jobs", side_effect=save), patch.object(app.time, "sleep"):
                try:
                    app.collect_details(Mock(), data, folder, folder / "jobs.json")
                finally:
                    release.set()
            self.assertTrue(worker_threads)
            self.assertNotIn(main_thread, worker_threads)
            self.assertEqual([j["description"] for j in data["jobs"]], ["正文0", "正文1", "正文2"])
            self.assertEqual(data["description_success_count"], 3)
            self.assertEqual(json.loads((folder / "jobs.json").read_text(encoding="utf-8"))["description_success_count"], 3)

    def test_gate_and_interrupt_drain_existing_ocr_without_visiting_next_job(self):
        for error in (app.AccessGate("验证页面"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                folder, data = Path(directory), payload()
                captured = ocr.CapturedDescription("0", folder / "0.json")
                with patch.object(app, "read_description_with_login", side_effect=[captured, error]) as read, \
                        patch.object(app, "DescriptionOCRWorker", return_value=lambda c: ("完整正文", {"description_source": "OCR"})), \
                        patch.object(app.time, "sleep"):
                    with self.assertRaises(type(error)):
                        app.collect_details(Mock(), data, folder, folder / "jobs.json")
                self.assertEqual(read.call_count, 2)
                self.assertEqual(data["jobs"][0]["description_status"], "ok")
                self.assertEqual(data["jobs"][2]["description_status"], "pending")
                if isinstance(error, app.AccessGate):
                    self.assertEqual(data["jobs"][1]["description_status"], "blocked")

    def test_one_worker_failure_does_not_drop_other_jobs(self):
        data = payload(2)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            def recognize(captured):
                if captured.job_id == "0":
                    raise ImportError("模型加载失败")
                return "第二个岗位正文", {"description_source": "OCR"}
            with patch.object(app, "read_description_with_login", side_effect=lambda c, j, *a, **k: ocr.CapturedDescription(j["id"], folder / f"{j['id']}.json")), \
                    patch.object(app, "DescriptionOCRWorker", return_value=recognize), patch.object(app.time, "sleep"):
                app.collect_details(Mock(), data, folder, folder / "jobs.json")
            self.assertEqual([j["description_status"] for j in data["jobs"]], ["error", "ok"])
            self.assertIn("模型加载失败", data["jobs"][0]["description_error"])

    def test_pending_queue_is_bounded_before_next_browser_visit(self):
        data, release = payload(4), threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            def read(cdp, job, *args, **kwargs):
                if int(job["id"]) >= 2:
                    self.assertEqual(data["jobs"][0]["description_status"], "ok")
                return ocr.CapturedDescription(job["id"], folder / f"{job['id']}.json")
            def recognize(captured):
                if captured.job_id == "0":
                    if not release.wait(5):
                        raise AssertionError("未等待已满队列")
                return f"正文{captured.job_id}", {"description_source": "OCR"}
            from concurrent.futures import Future
            original_result = Future.result
            def result(future, *args, **kwargs):
                release.set()  # 主线程已回收/等待任务，才释放第一个任务。
                return original_result(future, *args, **kwargs)
            with patch.object(app, "read_description_with_login", side_effect=read), \
                    patch.object(app, "DescriptionOCRWorker", return_value=recognize), \
                    patch.object(Future, "result", result), patch.object(app.time, "sleep"):
                app.collect_details(Mock(), data, folder, folder / "jobs.json")
            self.assertEqual(data["description_success_count"], 4)

    def test_login_retry_keeps_capture_mode_and_same_cdp(self):
        cdp, job = Mock(), {"url": "https://example.test/job"}
        with patch.object(app, "read_description", side_effect=[app.LoginRequired("登录"), "正文"]) as read, \
                patch.object(app, "require_login") as login:
            self.assertEqual(app.read_description_with_login(cdp, job, Path("."), capture_only=True), "正文")
        login.assert_called_once_with(cdp, job["url"], no_prompt=False)
        for call in read.call_args_list:
            self.assertIs(call.args[0], cdp)
            self.assertTrue(call.kwargs["capture_only"])

    def test_interrupt_while_waiting_full_queue_still_recovers_that_future(self):
        from concurrent.futures import Future
        original_result = Future.result
        release, interrupted = threading.Event(), [False]
        data = payload(3)
        def result(future, *args, **kwargs):
            if not interrupted[0]:
                interrupted[0] = True
                release.set()
                raise KeyboardInterrupt()
            return original_result(future, *args, **kwargs)
        def recognize(captured):
            if not release.wait(5):
                raise AssertionError("没有发生队列等待")
            return "完整正文", {"description_source": "OCR"}
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(app, "read_description_with_login", side_effect=lambda c, j, *a, **k: ocr.CapturedDescription(j["id"], folder / f"{j['id']}.json")) as read, \
                    patch.object(app, "DescriptionOCRWorker", return_value=recognize), \
                    patch.object(Future, "result", result), patch.object(app.time, "sleep"):
                with self.assertRaises(KeyboardInterrupt):
                    app.collect_details(Mock(), data, folder, folder / "jobs.json")
            self.assertEqual(read.call_count, 2)
            self.assertEqual([j["description_status"] for j in data["jobs"]], ["ok", "ok", "pending"])

    def test_main_exports_after_ocr_and_opens_only_one_task_tab(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            def collect(cdp, report, *args):
                report["jobs"] = payload(2)["jobs"]
                return "requested_limit_reached"
            def read(cdp, job, folder, *args, **kwargs):
                return ocr.CapturedDescription(job["id"], folder / f"{job['id']}.json")
            def export(path):
                report = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(report["status"], "complete")
                self.assertEqual([j["description_status"] for j in report["jobs"]], ["ok", "ok"])
                return path.with_suffix(".csv")
            with patch("sys.argv", ["collect_jobs.py", "--limit", "2"]), \
                    patch.object(app, "OUTPUT", output), patch.object(app, "load_history", return_value=(set(), 0)), \
                    patch.object(app, "check_ocr_runtime"), patch.object(app, "ensure_browser"), \
                    patch.object(app, "open_task_tab", return_value={"id": "task", "webSocketDebuggerUrl": "ws://test"}) as tab, \
                    patch.object(app.websocket, "create_connection"), patch.object(app, "RawCDP"), \
                    patch.object(app, "navigate"), patch.object(app, "require_login"), \
                    patch.object(app, "close_task_tab") as close, patch.object(app, "collect_list", side_effect=collect), \
                    patch.object(app, "read_description_with_login", side_effect=read), \
                    patch.object(app, "DescriptionOCRWorker", return_value=lambda c: ("本地识别正文", {"description_source": "OCR"})), \
                    patch.object(app, "export_csv", side_effect=export), patch.object(app.time, "sleep"):
                self.assertEqual(app.main(), 0)
            tab.assert_called_once_with()
            close.assert_called_once_with("task")

    def test_first_interrupt_during_final_drain_keeps_result(self):
        from concurrent.futures import Future
        original_result, interrupted = Future.result, [False]
        def result(future, *args, **kwargs):
            if not interrupted[0]:
                interrupted[0] = True
                raise KeyboardInterrupt()
            return original_result(future, *args, **kwargs)
        data = payload(1)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(app, "read_description_with_login", return_value=ocr.CapturedDescription("0", folder / "0.json")), \
                    patch.object(app, "DescriptionOCRWorker", return_value=lambda c: ("完整正文", {"description_source": "OCR"})), \
                    patch.object(Future, "result", result):
                with self.assertRaises(KeyboardInterrupt):
                    app.collect_details(Mock(), data, folder, folder / "jobs.json")
            saved = json.loads((folder / "jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["jobs"][0]["description_status"], "ok")


class LocalRecognitionTests(unittest.TestCase):
    def test_incomplete_capture_never_starts_ocr_or_publishes_manifest(self):
        import base64
        cdp = Mock()
        def call(method, params=None):
            if method == "DOM.describeNode":
                return {"node": {"backendNodeId": 1}}
            if method == "Page.getLayoutMetrics":
                return {"cssLayoutViewport": {"clientWidth": 800, "clientHeight": 800}}
            if method == "Page.captureScreenshot":
                return {"data": base64.b64encode(b"saved-part").decode()}
            return {}
        cdp.call.side_effect = call
        geometry = {"viewport_rect": {"x": 0, "y": 0, "width": 500, "height": 900},
                    "page_scroll": {"x": 0, "y": 0}}
        guard = Mock(side_effect=[None, app.LoginRequired("需要登录")])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(ocr, "visible_bounds", return_value=(0, 0, 800, 800)), patch.object(ocr, "RapidOCR") as engine:
                with self.assertRaises(app.LoginRequired):
                    ocr.extract_description(cdp, 1, folder, "job", Mock(return_value=geometry), guard)
            engine.assert_not_called()
            self.assertTrue((folder / "job-description-01.png").exists())
            self.assertFalse((folder / "job-description-captures.json").exists())

    def test_worker_creates_one_engine_for_multiple_jobs(self):
        worker = ocr.DescriptionOCRWorker()
        with patch.object(ocr, "RapidOCR") as engine, \
                patch.object(ocr, "recognize_description", side_effect=[("正文一", {}), ("正文二", {})]) as recognize:
            worker(ocr.CapturedDescription("one", Path("one.json")))
            worker(ocr.CapturedDescription("two", Path("two.json")))
        engine.assert_called_once()
        self.assertTrue(all(call.args[1] is engine.return_value for call in recognize.call_args_list))

    def test_retry_updates_json_from_local_manifest_without_browser(self):
        data = payload(2)
        data["jobs"][0].update(description_status="error", description_error="old error",
                               description_capture_manifest="0-description-captures.json")
        data["jobs"][1].update(description_status="ok", description="已有正文")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with patch.object(retry, "DescriptionOCRWorker", return_value=lambda c: ("重新识别正文", {"description_source": "OCR"})), \
                    patch.object(app, "ensure_browser") as browser:
                report = retry.retry_saved_descriptions(path)
            browser.assert_not_called()
            self.assertEqual(report["status"], "complete")
            self.assertNotIn("description_error", report["jobs"][0])
            self.assertEqual(report["jobs"][1]["description"], "已有正文")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["description_success_count"], 2)

    def test_short_text_and_saved_coordinate_offsets_without_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            manifest = folder / "short-description-captures.json"
            manifest.write_text(json.dumps({"job_id": "short", "complete": True, "capture_seconds": 0.1,
                "captures": [{"image": "tile.png", "relative_left": 7, "relative_top": 432,
                              "clip": {"scale": 2}}]}), encoding="utf-8")
            engine = Mock(return_value=Mock(txts=("有量化经验。",), scores=(0.99,),
                boxes=[[[10, 20], [110, 20], [110, 60], [10, 60]]]))
            text, metadata = ocr.recognize_description(ocr.CapturedDescription("short", manifest), engine)
            self.assertEqual(text, "有量化经验。")
            self.assertTrue(metadata["description_ocr_review_required"])
            lines = json.loads((folder / "short-description-ocr.json").read_text(encoding="utf-8"))["lines"]
            self.assertEqual((lines[0]["x"], lines[0]["y"]), (12, 452))
            for bad in ("", "加载中…", "\ue031"):
                engine.return_value.txts = (bad,)
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    ocr.recognize_description(ocr.CapturedDescription("short", manifest), engine)
            with self.assertRaisesRegex(ValueError, "标识不匹配"):
                ocr.recognize_description(ocr.CapturedDescription("wrong", manifest), engine)


if __name__ == "__main__":
    unittest.main()
