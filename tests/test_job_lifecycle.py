import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs as app
from history_repairs import load_missing_jobs
from job_db import turn_db
from job_lifecycle import JobUnavailable, delete_unavailable_job, terminal_reason


def job(name, *, title=None, description=None, salary="150元/天"):
    return {"id": name, "title": title or name, "company": "公司", "salary": salary,
            "salary_status": "ok" if salary else "error", "description": description,
            "description_status": "ok" if description else "error",
            "url": f"https://www.zhipin.com/job_detail/{name}.html"}


class LifecycleTests(unittest.TestCase):
    def seed(self, folder, jobs, keyword="Python", city="广州"):
        path = folder / f"{keyword}-{city}.json"
        app.save_jobs(path, {"city": city, "keyword": keyword, "jobs": jobs})
        turn_db(path, folder / "jobs.db")
        return path

    def test_terminal_signals_are_scoped_to_the_target_job(self):
        url = job("target")["url"]
        self.assertIsNotNone(terminal_reason(url, url, "该职位已关闭", ""))
        self.assertIsNotNone(terminal_reason(url, "https://www.zhipin.com/404.html", "", "抱歉，您访问的页面不存在"))
        for actual, status, error, body, cards in [
                (job("other")["url"], "该职位已关闭", "", False, False),
                (url, "招聘中", "该职位已关闭", True, True),
                (url, "未停止招聘", "", False, False),
                (url, "", "负责处理职位已关闭后的归档", True, False),
                (url, "", "如何处理职位不存在的情况", False, False),
                ("https://www.zhipin.com/", "", "职位不存在", False, False),
                ("https://example.test/404.html", "", "页面不存在", False, False),
                (url, "", "加载中", False, False)]:
            with self.subTest(status=status, error=error, actual=actual):
                self.assertIsNone(terminal_reason(url, actual, status, error, body, cards))

    def test_closed_job_deleted_next_job_repaired_and_old_import_cannot_revive_it(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            closed = job("closed", title="同名岗位")
            sources = [self.seed(folder, [closed], keyword="Java"),
                       self.seed(folder, [job("active", title="同名岗位", description="有效正文"), job("next")]),
                       self.seed(folder, [closed], keyword="Go"),
                       self.seed(folder, [closed], city="深圳")]
            data = {"city": "广州", "keyword": "Python", "requested_count": 200, "jobs": []}
            def read(cdp, current, *args, **kwargs):
                if current["id"] == "closed":
                    raise JobUnavailable({"matched_text": "该职位已关闭", "kind": "closed"})
                return "补齐的有效正文"
            with patch.object(app, "read_description_with_login", side_effect=read) as reader, patch.object(app.time, "sleep"):
                app.repair_missing_history(Mock(), load_missing_jobs(folder / "jobs.db", "广州"), data,
                                           folder, folder / "run.json", folder / "jobs.db")
            self.assertEqual(reader.call_count, 2)  # 同 URL 跨关键词只访问一次。
            self.assertEqual(data["history_repair"]["deleted"], 2)
            self.assertEqual(data["history_repair"]["repaired"], 1)
            report = turn_db(sources, folder / "jobs.db")
            self.assertEqual(report["suppressed_deleted"], 2)
            with closing(sqlite3.connect(folder / "jobs.db")) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE city='广州' AND platform_job_id='closed'").fetchone()[0], 0)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE platform_job_id='active'").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE city='深圳' AND platform_job_id='closed'").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT description FROM jobs WHERE platform_job_id='next'").fetchone()[0], "补齐的有效正文")
                backup = json.loads(con.execute("SELECT original_rows_json FROM deleted_jobs").fetchone()[0])
                self.assertEqual(len(backup), 2)
            self.assertEqual(load_missing_jobs(folder / "jobs.db", "广州"), {})

    def test_salary_path_deletion_failure_keeps_row_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.seed(folder, [job("closed", salary=None, description="原有正文"), job("next")])
            data = {"city": "广州", "keyword": "Python", "requested_count": 200, "jobs": []}
            with patch.object(app, "read_detail_salary", side_effect=JobUnavailable({"matched_text": "职位已下架"})), \
                    patch.object(app, "delete_unavailable_job", side_effect=sqlite3.OperationalError("locked")), \
                    patch.object(app, "read_description_with_login", return_value="补齐正文") as read, patch.object(app.time, "sleep"):
                app.repair_missing_history(Mock(), load_missing_jobs(folder / "jobs.db", "广州"), data,
                                           folder, folder / "run.json", folder / "jobs.db")
            self.assertEqual(data["history_repair"]["deleted"], 0)
            self.assertEqual(data["history_repair"]["failed"], 1)
            read.assert_called_once()
            with closing(sqlite3.connect(folder / "jobs.db")) as con:
                self.assertEqual(con.execute("SELECT description FROM jobs WHERE platform_job_id='closed'").fetchone()[0], "原有正文")

    def test_closed_signal_must_be_confirmed_twice_and_security_gate_wins(self):
        current = job("target")
        root = {"nodeId": 1, "documentURL": current["url"]}
        selector = ".job-banner .job-status, .info-primary .job-status"
        for second, expected in [("该职位已关闭", JobUnavailable), ("招聘中", None), ("请完成安全验证", app.AccessGate)]:
            with self.subTest(second=second), tempfile.TemporaryDirectory() as directory:
                cdp, folder = Mock(), Path(directory)
                cdp.call.return_value = {"node": {"backendNodeId": 10}}
                def rendered(data, backend=None):
                    return second if data == "fresh" else "该职位已关闭"
                with patch.object(app, "document", return_value=root), patch.object(app, "snapshot", return_value="fresh"), \
                        patch.object(app, "rendered_text", side_effect=rendered), \
                        patch.object(app, "query", side_effect=lambda c,r,s: 10 if s == selector else 0), patch.object(app.time, "sleep"):
                    if expected:
                        with self.assertRaises(expected):
                            app.check_job_unavailable(cdp, current, folder, root, "old", "该职位已关闭")
                    else:
                        app.check_job_unavailable(cdp, current, folder, root, "old", "该职位已关闭")
                self.assertEqual((folder / "target-unavailable.json").exists(), expected is JobUnavailable)

    def test_delete_and_deletion_record_roll_back_together_on_database_error(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, current = Path(directory), job("closed")
            self.seed(folder, [current])
            with closing(sqlite3.connect(folder / "jobs.db")) as con:
                con.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON jobs BEGIN SELECT RAISE(ABORT, 'blocked'); END")
                con.commit()
            with self.assertRaises(sqlite3.DatabaseError):
                delete_unavailable_job(folder / "jobs.db", "广州", current, {"matched_text": "职位已关闭"})
            with closing(sqlite3.connect(folder / "jobs.db")) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM deleted_jobs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
