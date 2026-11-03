import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import collect_jobs as app
from search_regions import search_url, selected_districts


def identity(n):
    return app.job_identity({"title": f"岗位{n}", "company": "公司"})


def card(cdp, n):
    return (f'<a class="job-name" href="/job_detail/id{n}.html">岗位{n}</a>'
            '<span class="boss-name">公司</span><span class="job-salary">150元/天</span>')


class DistrictTests(unittest.TestCase):
    def test_region_urls_and_validation(self):
        query = parse_qs(urlsplit(search_url("广州", "Python AI", "荔湾区")).query)
        self.assertEqual(query, {"city": ["101280100"], "query": ["Python AI"], "multiBusinessDistrict": ["440103"]})
        self.assertEqual(len(selected_districts("广州")), 11)
        self.assertEqual(selected_districts("广州", ["天河区", "天河区", "荔湾区"]), ["天河区", "荔湾区"])
        with self.assertRaises(ValueError):
            selected_districts("广州", ["罗湖区"])

    def test_cross_district_deduplication_global_limit_and_history_union(self):
        data = {"city": "广州", "keyword": "Python", "requested_count": 3, "jobs": [], "historical_skipped_count": 0}
        active, visited = [None], []
        def navigate(cdp, url):
            active[0] = parse_qs(urlsplit(url).query)["multiBusinessDistrict"][0]
            visited.append(active[0])
        def nodes(*args):
            return [1, 2, 5] if active[0] == "440103" else [2, 5, 3, 4]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(app, "navigate", side_effect=navigate), patch.object(app, "require_login"), \
                    patch.object(app, "wait_search_scope"), patch.object(app, "wait_cards", side_effect=nodes), \
                    patch.object(app, "outer_html", side_effect=card), patch.object(app.time, "sleep"):
                reason = app.collect_districts(Mock(), data, folder, folder / "jobs.json",
                                              ["荔湾区", "白云区", "天河区"], True, 0, {identity(5)})
            self.assertEqual(reason, "requested_limit_reached")
            self.assertEqual(visited, ["440103", "440111"])
            self.assertEqual([job["id"] for job in data["jobs"]], ["id1", "id2", "id3"])
            self.assertEqual([job["search_district"] for job in data["jobs"]], ["荔湾区", "荔湾区", "白云区"])
            self.assertEqual(data["historical_skipped_count"], 1)
            self.assertEqual([row["collected_count"] for row in data["district_runs"]], [2, 1])
            saved = json.loads((folder / "jobs.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["jobs"]), 3)

    def test_scope_timeout_is_recorded_but_gate_stops_all_districts(self):
        for error, should_stop in [(TimeoutError("未确认范围"), False), (app.AccessGate("验证页面"), True)]:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                data = {"city": "广州", "keyword": "Python", "requested_count": 3, "jobs": []}
                folder = Path(directory)
                with patch.object(app, "navigate") as navigate, patch.object(app, "require_login"), \
                        patch.object(app, "wait_search_scope", side_effect=[error, None]), \
                        patch.object(app, "collect_list", return_value="no_new_results_after_scroll"), patch.object(app.time, "sleep"):
                    if should_stop:
                        with self.assertRaises(app.AccessGate):
                            app.collect_districts(Mock(), data, folder, folder / "jobs.json", ["荔湾区", "白云区"], True, 1, set())
                    else:
                        self.assertEqual(app.collect_districts(Mock(), data, folder, folder / "jobs.json",
                            ["荔湾区", "白云区"], True, 1, set()), "districts_finished_with_errors")
                self.assertEqual(navigate.call_count, 1 if should_stop else 2)
                self.assertEqual(data["district_runs"][0]["status"], "blocked" if should_stop else "error")

    def test_default_cli_uses_all_districts_and_single_task_tab(self):
        for flags, expected in [([], selected_districts("广州")), (["--district", "白云区", "天河区"], ["白云区", "天河区"])]:
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                with patch("sys.argv", ["collect_jobs.py"] + flags), patch.object(app, "OUTPUT", folder), \
                        patch.object(app, "check_ocr_runtime"), patch.object(app, "ensure_browser"), \
                        patch.object(app, "load_history", return_value=(set(), 0)), \
                        patch.object(app, "open_task_tab", return_value={"id": "task", "webSocketDebuggerUrl": "ws://test"}) as tab, \
                        patch.object(app.websocket, "create_connection"), patch.object(app, "RawCDP"), \
                        patch.object(app, "collect_districts", return_value="districts_exhausted") as collect, \
                        patch.object(app, "close_task_tab") as close:
                    self.assertEqual(app.main(), 0)
                self.assertEqual(collect.call_args.args[4], expected)
                tab.assert_called_once_with()
                close.assert_not_called()
                saved = json.loads(next((folder / "runs").glob("*/jobs.json")).read_text(encoding="utf-8"))
                self.assertEqual(saved["search_mode"], "districts")
                self.assertEqual(saved["districts"], expected)
                self.assertEqual(saved["requested_count"], 1000)
                self.assertTrue(saved["finish_district"])
                self.assertEqual(saved["database"]["status"], "ok")

    def test_scope_requires_matching_url_and_visible_city_and_district(self):
        for correct_url, city, district, success in [
                (True, "广州", "荔湾区(全)", True),
                (False, "广州", "荔湾区(全)", False),
                (True, "深圳", "荔湾区(全)", False),
                (True, "广州", "天河区(全)", False),
                (True, "广州", "荔湾区", False)]:
            with self.subTest(city=city, district=district, correct_url=correct_url):
                clock = [0]
                def sleep(seconds): clock[0] += seconds
                root = {"nodeId": 1, "documentURL": search_url("广州", "Python", "荔湾区") if correct_url else search_url("广州", "Python", "白云区")}
                with patch.object(app, "document", return_value=root), patch.object(app, "query", side_effect=lambda c,r,s: 2 if s == '.cur-city-label' else 3), \
                        patch.object(app, "outer_html", side_effect=lambda c,n: city if n == 2 else district), \
                        patch.object(app, "snapshot"), patch.object(app, "rendered_text", return_value="正常页面"), \
                        patch.object(app.time, "monotonic", side_effect=lambda: clock[0]), patch.object(app.time, "sleep", side_effect=sleep):
                    if success:
                        app.wait_search_scope(Mock(), "广州", "Python", "荔湾区", timeout=1)
                    else:
                        with self.assertRaises(TimeoutError):
                            app.wait_search_scope(Mock(), "广州", "Python", "荔湾区", timeout=1)


if __name__ == "__main__":
    unittest.main()
