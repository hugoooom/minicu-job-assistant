import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs as app
from history_repairs import load_missing_jobs, valid_job_url
from job_db import turn_db


def job(name, salary="150元/天", description="有效原文"):
    return {"id": name, "title": name, "company": "公司", "salary": salary,
            "salary_status": "ok" if salary else "error", "salary_source": "DOM",
            "description": description, "description_status": "ok" if description else "error",
            "description_source": "CDP_DOMSnapshot" if description else None,
            "url": f"https://www.zhipin.com/job_detail/{name}.html"}


class HistoryRepairTests(unittest.TestCase):
    def seed(self, folder, jobs, keyword="Python", city="广州"):
        source = folder / (keyword + city + ".json")
        app.save_jobs(source, {"city": city, "keyword": keyword, "jobs": jobs})
        turn_db(source, folder / "jobs.db")

    def test_repairs_only_missing_fields_preserves_keywords_ids_and_new_job_count(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            first = job("salary-missing", salary=None)
            first.update(description_source="OCR", description_ocr_review_required=True)
            self.seed(folder, [first, job("description-missing", description=None), job("complete")])
            self.seed(folder, [job("both-missing", salary=None, description=None)], keyword="Java")
            self.seed(folder, [job("other-city", salary=None)], city="深圳")
            with closing(sqlite3.connect(folder / "jobs.db")) as con:
                before = con.execute("SELECT id,title FROM jobs ORDER BY id").fetchall()
            data = {"city": "广州", "keyword": "Python", "requested_count": 200, "jobs": []}
            groups = load_missing_jobs(folder / "jobs.db", "广州")
            self.assertEqual({key: len(value) for key, value in groups.items()}, {"Python": 2, "Java": 1})
            def description(cdp, current, *args, **kwargs):
                current["description_source"] = "CDP_DOMSnapshot"
                return "补齐正文"
            with patch.object(app, "read_detail_salary", return_value={"salary": "200元/天", "salary_status": "ok", "salary_source": "OCR"}) as salary, \
                    patch.object(app, "read_description_with_login", side_effect=description) as desc, patch.object(app.time, "sleep"):
                app.repair_missing_history(Mock(), groups, data, folder, folder / "run.json", folder / "jobs.db")
            self.assertEqual(salary.call_count, 2)
            self.assertEqual(desc.call_count, 2)
            self.assertEqual(data["jobs"], [])
            self.assertEqual(data["requested_count"], 200)
            self.assertEqual(data["history_repair"]["repaired"], 3)
            calls = {call.args[1]["id"]: call.kwargs["navigate_page"] for call in desc.call_args_list}
            self.assertEqual(calls, {"description-missing": True, "both-missing": False})
            self.assertEqual(load_missing_jobs(folder / "jobs.db", "广州"), {})
            with closing(sqlite3.connect(folder / "jobs.db")) as con:
                self.assertEqual(con.execute("SELECT id,title FROM jobs ORDER BY id").fetchall(), before)
                self.assertEqual(con.execute("SELECT description,description_ocr_review_required FROM jobs WHERE title='salary-missing'").fetchone(), ("有效原文", 1))
                self.assertEqual(con.execute("SELECT keyword FROM jobs WHERE title='both-missing'").fetchone()[0], "Java")

    def test_failure_and_invalid_url_do_not_destroy_existing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            invalid = job("invalid", description=None)
            invalid["url"] = "https://example.test/job_detail/invalid.html"
            self.seed(folder, [job("failed", description=None), invalid])
            data = {"city": "广州", "keyword": "Python", "requested_count": 200, "jobs": []}
            with patch.object(app, "read_description_with_login", side_effect=TimeoutError("岗位已下架")) as read, \
                    patch.object(app, "read_detail_salary") as salary, patch.object(app.time, "sleep"):
                app.repair_missing_history(Mock(), load_missing_jobs(folder / "jobs.db", "广州"), data,
                                           folder, folder / "run.json", folder / "jobs.db")
            read.assert_called_once()
            salary.assert_not_called()
            self.assertEqual(data["history_repair"]["failed"], 1)
            self.assertEqual(data["history_repair"]["skipped"], 1)
            with closing(sqlite3.connect(folder / "jobs.db")) as con:
                self.assertEqual(con.execute("SELECT salary,description FROM jobs WHERE title='failed'").fetchone(), ("150元/天", None))
            self.assertEqual(sum(map(len, load_missing_jobs(folder / "jobs.db", "广州").values())), 2)

    def test_gate_stops_further_requests_but_keeps_salary_already_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.seed(folder, [job("first", salary=None, description=None), job("second", description=None)])
            data = {"city": "广州", "keyword": "Python", "requested_count": 200, "jobs": []}
            with patch.object(app, "read_detail_salary", return_value={"salary": "200元/天", "salary_status": "ok"}), \
                    patch.object(app, "read_description_with_login", side_effect=app.AccessGate("验证页面")) as read:
                with self.assertRaises(app.AccessGate):
                    app.repair_missing_history(Mock(), load_missing_jobs(folder / "jobs.db", "广州"), data,
                                               folder, folder / "run.json", folder / "jobs.db")
            self.assertEqual(read.call_count, 1)
            with closing(sqlite3.connect(folder / "jobs.db")) as con:
                self.assertEqual(con.execute("SELECT salary FROM jobs WHERE title='first'").fetchone()[0], "200元/天")

    def test_detail_salary_uses_dom_or_ocr_after_identity_and_auth_checks(self):
        for raw in ("180-220元/天", "\ue031-\ue032元/天"):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as directory:
                current, cdp = job("test", salary=None), Mock()
                cdp.call.return_value = {"node": {"backendNodeId": 3}}
                with patch.object(app, "navigate"), patch.object(app, "document", return_value={"nodeId": 1, "documentURL": current["url"]}), \
                        patch.object(app, "snapshot"), patch.object(app, "rendered_text", side_effect=["正常页面", raw]), \
                        patch.object(app, "authentication_state", return_value="authenticated"), patch.object(app, "query", return_value=2), \
                        patch.object(app, "salary_screenshot") as screenshot:
                    engine = Mock(return_value=Mock(txts=("180-220元/天",), scores=(0.99,)))
                    fields = app.read_detail_salary(cdp, current, Path(directory), [engine])
                self.assertEqual(fields["salary"], "180-220元/天")
                self.assertEqual(fields["salary_source"], "OCR" if "\ue031" in raw else "DOM")
                self.assertEqual(screenshot.call_count, int("\ue031" in raw))

    def test_missing_database_and_foreign_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_missing_jobs(Path(directory) / "missing.db", "广州"), {})
        for bad in (None, "http://www.zhipin.com/job_detail/a.html", "https://evil.test/job_detail/a.html", "https://[bad"):
            self.assertFalse(valid_job_url(bad))

    def test_transient_detail_node_failure_is_relocated(self):
        current, cdp = job("test", salary=None), Mock()
        cdp.call.return_value = {"node": {"backendNodeId": 3}}
        root = {"nodeId": 1, "documentURL": current["url"]}
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "navigate"), \
                patch.object(app, "document", side_effect=[RuntimeError("node replaced"), root, root]) as document, \
                patch.object(app, "snapshot"), patch.object(app, "rendered_text", side_effect=["正常页面", "180-220元/天"]), \
                patch.object(app, "authentication_state", return_value="authenticated"), \
                patch.object(app, "query", return_value=2), patch.object(app.time, "sleep"):
            fields = app.read_detail_salary(cdp, current, Path(directory), [None])
        self.assertEqual(fields["salary"], "180-220元/天")
        self.assertEqual(document.call_count, 3)

    def test_startup_repairs_before_new_search_without_counting_as_new_job(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            current = job("old", description=None)
            self.seed(folder, [current])
            def collect(cdp, data, target, path, url, no_prompt, max_scrolls, excluded):
                self.assertEqual(data["jobs"], [])
                self.assertIn(app.job_identity(current), excluded)
                with closing(sqlite3.connect(folder / "jobs.db")) as con:
                    self.assertEqual(con.execute("SELECT description FROM jobs").fetchone()[0], "补齐正文")
                return "no_new_results_after_scroll"
            with patch("sys.argv", ["collect_jobs.py", "--citywide", "--limit", "1"]), \
                    patch.object(app, "OUTPUT", folder), patch.object(app, "check_ocr_runtime"), \
                    patch.object(app, "ensure_browser"), patch.object(app, "open_task_tab", return_value={"id":"task", "webSocketDebuggerUrl":"ws://test"}), \
                    patch.object(app.websocket, "create_connection"), patch.object(app, "RawCDP"), \
                    patch.object(app, "navigate"), patch.object(app, "require_login"), patch.object(app.time, "sleep"), \
                    patch.object(app, "read_description_with_login", return_value="补齐正文"), \
                    patch.object(app, "collect_list", side_effect=collect):
                self.assertEqual(app.main(), 1)  # 修复成功不能伪装成新增目标已达到。
            saved = json.loads(next((folder / "runs").glob("*/jobs.json")).read_text(encoding="utf-8"))
            self.assertEqual(saved["history_repair"]["repaired"], 1)
            self.assertEqual(saved["collected_count"], 0)


if __name__ == "__main__":
    unittest.main()
