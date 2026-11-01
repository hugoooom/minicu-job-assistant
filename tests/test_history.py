import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs as app
from job_history import job_identity, load_history


def card(cdp, node):
    return (f'<li><a class="job-name" href="/job_detail/job{node}.html?lid=changing">岗位{node}</a>'
            '<span class="boss-name">示例公司</span><span class="job-salary">150元/天</span></li>')


def identity(node):
    return job_identity({"title": f"岗位{node}", "company": "示例公司"})


class HistoryTests(unittest.TestCase):
    def test_restore_fallback_ids_across_keywords_and_partial_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, jobs in (("a", [{"id": "a"}, {"id": "b", "description_status": "error"}]),
                               ("b", [{"id": "a"}, {"id": "c", "description_status": "pending"}])):
                (root / name).mkdir()
                app.save_jobs(root / name / "jobs.json", {"keyword": name, "jobs": jobs})
            self.assertEqual(load_history(root), ({("id", "a"), ("id", "b"), ("id", "c")}, 2))

    def test_name_normalization_merges_new_ids_and_formatting_changes(self):
        a = {"id": "old", "title": " Ｐｙｔｈｏｎ　 AI 开发 ", "company": "示例　公司"}
        b = {"id": "new", "title": "python  ai 开发", "company": "示例 公司"}
        self.assertEqual(job_identity(a), job_identity(b))

    def test_different_titles_or_companies_remain_distinct(self):
        base = {"title": "Python 开发", "company": "甲公司"}
        self.assertNotEqual(job_identity(base), job_identity({**base, "company": "乙公司"}))
        self.assertNotEqual(job_identity(base), job_identity({**base, "title": "Java 开发"}))

    def test_missing_company_uses_id_instead_of_merging_titles(self):
        self.assertNotEqual(job_identity({"id": "a", "title": "Python 开发"}),
                            job_identity({"id": "b", "title": "Python 开发"}))

    def test_changed_link_is_skipped_by_historical_title_and_company(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old"
            old.mkdir()
            app.save_jobs(old / "jobs.json", {"jobs": [
                {"id": "previous-id", "title": "岗位1", "company": "示例公司"}
            ]})
            excluded, _ = load_history(root)
            new = root / "new"
            new.mkdir()
            payload = {"requested_count": 1, "jobs": []}
            with patch.object(app, "wait_cards", return_value=[1, 2]), \
                    patch.object(app, "outer_html", side_effect=card):
                app.collect_list(Mock(), payload, new, new / "jobs.json", "url", True, 3, excluded)
            self.assertEqual([job["id"] for job in payload["jobs"]], ["job2"])

    def test_same_name_company_with_different_ids_is_deduplicated_within_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "outer_html", side_effect=[card(None, 1), card(None, 2).replace("岗位2", "岗位1")]):
                jobs = app.read_list(Mock(), [1, 2], Path(directory))
            self.assertEqual([job["id"] for job in jobs], ["job1"])

    def test_cli_restores_history_and_include_seen_bypasses_it(self):
        for include_seen in (False, True):
            with self.subTest(include_seen=include_seen), tempfile.TemporaryDirectory() as directory:
                output = Path(directory)
                old = output / "runs" / "old"
                old.mkdir(parents=True)
                app.save_jobs(old / "jobs.json", {"city": "广州", "jobs": [
                    {"id": "old", "title": "岗位1", "company": "示例公司"}
                ]})
                argv = ["collect_jobs.py", "--limit", "1"] + (["--include-seen"] if include_seen else [])
                with patch("sys.argv", argv), patch.object(app, "OUTPUT", output), \
                        patch.object(app, "check_ocr_runtime"), patch.object(app, "ensure_browser"), patch.object(app, "open_task_tab", return_value={
                            "id": "task", "webSocketDebuggerUrl": "ws://test"}), \
                        patch.object(app.websocket, "create_connection"), patch.object(app, "RawCDP"), \
                        patch.object(app, "navigate"), patch.object(app, "require_login"), \
                        patch.object(app, "close_task_tab"), \
                        patch.object(app, "collect_list", return_value="no_new_results_after_scroll") as collect:
                    self.assertEqual(app.main(), 1)
                self.assertEqual(collect.call_args.args[-1], set() if include_seen else {identity(1)})

    def test_history_is_isolated_by_selected_search_city(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, city, title in (("gz", "广州", "岗位1"), ("sz", "深圳", "岗位2"),
                                      ("unknown", None, "岗位3")):
                folder = root / name
                folder.mkdir()
                app.save_jobs(folder / "jobs.json", {"city": city, "jobs": [
                    {"id": name, "title": title, "company": "示例公司"}
                ]})
            self.assertEqual(load_history(root, "广州"), ({identity(1)}, 1))
            self.assertEqual(load_history(root, "深圳"), ({identity(2)}, 1))

    def test_same_name_company_in_other_city_does_not_block_current_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "gz"
            old.mkdir()
            app.save_jobs(old / "jobs.json", {"city": "广州", "jobs": [
                {"id": "previous", "title": "岗位1", "company": "示例公司"}
            ]})
            excluded, _ = load_history(root, "深圳")
            new = root / "sz"
            new.mkdir()
            payload = {"requested_count": 1, "jobs": []}
            with patch.object(app, "wait_cards", return_value=[1]), \
                    patch.object(app, "outer_html", side_effect=card):
                app.collect_list(Mock(), payload, new, new / "jobs.json", "url", True, 3, excluded)
            self.assertEqual([job["id"] for job in payload["jobs"]], ["job1"])

    def test_missing_history_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_history(Path(directory) / "missing"), (set(), 0))

    def test_corrupt_history_is_reported_instead_of_silently_repeating(self):
        for content in ('{"jobs":', '{"jobs": {}}', '{"jobs": [{"title": "no id"}]}'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory) / "old"
                folder.mkdir()
                (folder / "jobs.json").write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "--include-seen"):
                    load_history(Path(directory))

    def test_two_saved_batches_are_disjoint_and_limit_counts_new_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batches = []
            for name in ("first", "second"):
                excluded, _ = load_history(root)
                folder = root / name
                folder.mkdir()
                payload = {"requested_count": 2, "jobs": []}
                with patch.object(app, "wait_cards", return_value=[1, 2, 3, 4]), \
                        patch.object(app, "outer_html", side_effect=card):
                    reason = app.collect_list(Mock(), payload, folder, folder / "jobs.json",
                                              "url", True, 3, excluded)
                self.assertEqual(reason, "requested_limit_reached")
                batches.append({j["id"] for j in payload["jobs"]})
            self.assertEqual(batches, [{"job1", "job2"}, {"job3", "job4"}])
            saved = json.loads((root / "second" / "jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["historical_skipped_count"], 2)

    def test_more_than_three_historical_screens_do_not_stop_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            payload = {"requested_count": 1, "jobs": []}
            with patch.object(app, "wait_cards", side_effect=[[1], [2], [3], [4], [5]]), \
                    patch.object(app, "outer_html", side_effect=card), \
                    patch.object(app, "scroll_list") as scroll, patch.object(app.time, "sleep"):
                reason = app.collect_list(Mock(), payload, folder, folder / "jobs.json", "url", True, 5,
                                          {identity(n) for n in (1, 2, 3, 4)})
            self.assertEqual(reason, "requested_limit_reached")
            self.assertEqual(payload["jobs"][0]["id"], "job5")
            self.assertEqual(payload["historical_skipped_count"], 4)
            self.assertEqual(scroll.call_count, 4)

    def test_exhausted_history_stops_with_shortfall(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            payload = {"requested_count": 2, "jobs": []}
            with patch.object(app, "wait_cards", return_value=[1]), \
                    patch.object(app, "outer_html", side_effect=card), \
                    patch.object(app, "scroll_list"), patch.object(app.time, "sleep"):
                reason = app.collect_list(Mock(), payload, folder, folder / "jobs.json", "url", True, 10,
                                          {identity(1)})
            app.update_report(payload)
            self.assertEqual(reason, "no_new_results_after_scroll")
            self.assertEqual(payload["collected_count"], 0)
            self.assertEqual(payload["missing_count"], 2)

    def test_historical_cards_are_skipped_before_ocr_and_file_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            hidden_salary = card(None, 1).replace("150元/天", "\ue031元/天")
            with patch.object(app, "outer_html", return_value=hidden_salary), \
                    patch.object(app, "salary_screenshot") as screenshot:
                self.assertEqual(app.read_list(Mock(), [1], folder, skip_keys={identity(1)}), [])
            screenshot.assert_not_called()
            self.assertEqual(list(folder.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
