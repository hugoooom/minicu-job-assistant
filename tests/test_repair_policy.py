import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs as app
from history_repairs import load_missing_jobs
from job_db import turn_db
from repair_policy import RepairPolicy


def job(name="same", salary=None, description=None):
    return {"id": name, "title": name, "company": "公司", "salary": salary,
            "salary_status": "ok" if salary else "error", "description": description,
            "description_status": "ok" if description else "error",
            "url": f"https://www.zhipin.com/job_detail/{name}.html"}


class RepairPolicyTests(unittest.TestCase):
    def seed(self, folder, groups):
        for keyword, jobs in groups.items():
            source = folder / f"{keyword}.json"
            app.save_jobs(source, {"city": "广州", "keyword": keyword, "jobs": jobs})
            turn_db(source, folder / "jobs.db")

    def run_repair(self, folder, groups):
        data = {"city": "广州", "keyword": "Python", "requested_count": 200, "jobs": []}
        app.repair_missing_history(Mock(), groups, data, folder, folder / "run.json", folder / "jobs.db")
        return data

    def test_failed_salary_reuses_open_page_for_description_and_address(self):
        with tempfile.TemporaryDirectory() as directory:
            folder, current = Path(directory), job()
            current["location_refresh_required"] = True
            with patch.object(app, "read_detail_salary", side_effect=ValueError("OCR 未识别")) as salary, \
                    patch.object(app, "read_description_with_login", side_effect=TimeoutError("正文暂未加载")) as description, \
                    patch.object(app, "read_detail_location", side_effect=TimeoutError("地址未加载")) as location, patch.object(app.time, "sleep"):
                self.run_repair(folder, {"Python": [current]})
            salary.assert_called_once()
            self.assertFalse(description.call_args.kwargs["navigate_page"])
            self.assertFalse(location.call_args.kwargs["navigate_page"])

    def test_duplicate_url_success_is_reused_across_keywords(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            groups = {"Python": [job()], "Java": [job()]}
            self.seed(folder, groups)
            with patch.object(app, "read_detail_salary", return_value={"salary": "200元/天", "salary_status": "ok"}) as salary, \
                    patch.object(app, "read_description_with_login", return_value="完整正文") as description, patch.object(app.time, "sleep"):
                data = self.run_repair(folder, load_missing_jobs(folder / "jobs.db", "广州"))
            self.assertEqual(salary.call_count, 1)
            self.assertEqual(description.call_count, 1)
            self.assertEqual(data["history_repair"]["reused_urls"], 1)
            with closing(sqlite3.connect(folder / "jobs.db")) as connection:
                self.assertEqual(connection.execute("SELECT salary,description FROM jobs ORDER BY id").fetchall(),
                                 [("200元/天", "完整正文"), ("200元/天", "完整正文")])

    def test_duplicate_url_failure_is_not_retried_by_another_keyword(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(app, "read_description_with_login", side_effect=TimeoutError("读取失败")) as reader, patch.object(app.time, "sleep"):
                data = self.run_repair(folder, {"Python": [job(salary="200元/天")], "Java": [job(salary="200元/天")]})
            self.assertEqual(reader.call_count, 1)
            self.assertEqual(data["history_repair"]["failed"], 2)
            selected, report = RepairPolicy(folder / "jobs.db", "广州").select(load_missing_jobs(folder / "jobs.db", "广州"))
            self.assertEqual(selected, {})
            self.assertEqual(report["cooldown_deferred"], 2)

    def test_existing_complementary_fields_need_no_browser_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            groups = {"Python": [job(salary="200元/天")], "Java": [job(description="已保存正文")]}
            self.seed(folder, groups)
            with patch.object(app, "read_detail_salary") as salary, patch.object(app, "read_description_with_login") as description, patch.object(app.time, "sleep"):
                self.run_repair(folder, groups)
            salary.assert_not_called()
            description.assert_not_called()
            self.assertEqual(load_missing_jobs(folder / "jobs.db", "广州"), {})

    def test_field_cooldown_persists_and_expires_without_blocking_other_fields_or_cities(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1000.0]
            path, current = Path(directory) / "jobs.db", job()
            policy = RepairPolicy(path, "广州", clock=lambda: clock[0])
            policy.record(current, "salary")
            restarted = RepairPolicy(path, "广州", clock=lambda: clock[0])
            selected, _ = restarted.select({"Python": [current]})
            self.assertEqual(selected["Python"][0]["_repair_fields"], ["description"])
            elsewhere, _ = RepairPolicy(path, "深圳", clock=lambda: clock[0]).select({"Python": [job()]})
            self.assertEqual(set(elsewhere["Python"][0]["_repair_fields"]), {"salary", "description"})
            clock[0] += 86401
            selected, _ = restarted.select({"Python": [job()]})
            self.assertEqual(set(selected["Python"][0]["_repair_fields"]), {"salary", "description"})

    def test_unpublished_address_waits_seven_days_and_success_clears_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1000.0]
            current = job(salary="200元/天", description="正文")
            current["location_refresh_required"] = True
            policy = RepairPolicy(Path(directory) / "jobs.db", "广州", clock=lambda: clock[0])
            policy.record(current, "location", "not_provided")
            clock[0] += 86401
            self.assertEqual(policy.select({"Python": [current]})[0], {})
            clock[0] += 6 * 86400
            self.assertEqual(policy.select({"Python": [current]})[0]["Python"][0]["_repair_fields"], ["location"])
            policy.record(current, "location", "ok")
            with closing(sqlite3.connect(policy.database)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM repair_attempts").fetchone()[0], 0)

    def test_budget_counts_unique_urls_not_keyword_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            groups = {"Python": [job(str(i)) for i in range(32)], "Java": [job("0")]}
            selected, report = RepairPolicy(Path(directory) / "jobs.db", "广州").select(groups)
            self.assertEqual(report["unique_urls"], 30)
            self.assertEqual(report["budget_deferred"], 2)
            self.assertEqual(sum(map(len, selected.values())), 31)

    def test_empty_location_queue_does_not_open_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["collect_jobs.py", "--update-locations"]), \
                    patch.object(app, "OUTPUT", Path(directory)), patch.object(app, "check_ocr_runtime"), \
                    patch.object(app, "ensure_browser") as browser, patch.object(app, "open_task_tab") as tab:
                self.assertEqual(app.main(), 0)
            browser.assert_not_called()
            tab.assert_not_called()

    def test_login_resume_does_not_navigate_a_second_time(self):
        with patch.object(app, "read_description", side_effect=[app.LoginRequired("登录"), "正文"]) as read, \
                patch.object(app, "require_login") as login:
            self.assertEqual(app.read_description_with_login(Mock(), job(), Path("unused")), "正文")
        login.assert_called_once()
        self.assertFalse(read.call_args.kwargs["navigate_page"])


if __name__ == "__main__":
    unittest.main()
