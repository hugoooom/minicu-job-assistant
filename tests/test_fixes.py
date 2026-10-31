import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs as app
from extract_first_job import CardParser
from salary import normalize_salary


class LoginTests(unittest.TestCase):
    def test_guest_cards_are_not_treated_as_login(self):
        cdp = Mock()
        cdp.call.return_value = {"nodeIds": [1, 2]}
        with patch.object(app, "document", return_value={"nodeId": 1}):
            with patch.object(app, "authentication_state", return_value="unconfirmed"):
                with self.assertRaises(app.LoginRequired):
                    app.wait_cards(cdp)

    def test_no_prompt_stops_before_guest_collection(self):
        with patch.object(app, "wait_authenticated", return_value=False):
            with patch("builtins.input") as prompt:
                with self.assertRaises(app.LoginRequired):
                    app.require_login(Mock(), "https://www.zhipin.com/", no_prompt=True)
            prompt.assert_not_called()

    def test_enter_without_login_does_not_continue(self):
        with patch.object(app, "wait_authenticated", side_effect=[False, False]):
            with patch.object(app, "navigate"):
                with patch("builtins.input", return_value="") as prompt:
                    with self.assertRaises(app.LoginRequired):
                        app.require_login(Mock(), "https://www.zhipin.com/")
                prompt.assert_called_once()

    def test_login_is_rechecked_after_prompt(self):
        with patch.object(app, "wait_authenticated", side_effect=[False, True]):
            with patch.object(app, "navigate") as navigate:
                with patch("builtins.input", return_value=""):
                    app.require_login(Mock(), "https://www.zhipin.com/")
                navigate.assert_called_once()

    def test_expired_detail_login_resumes_same_job(self):
        with patch.object(app, "read_description", side_effect=[app.LoginRequired("expired"), "完整描述"]):
            with patch.object(app, "require_login") as login:
                text = app.read_description_with_login(Mock(), {"url": "https://www.zhipin.com/job_detail/test.html"}, Path("."))
        self.assertEqual(text, "完整描述")
        login.assert_called_once()

    def test_account_ui_must_be_visible(self):
        cdp = Mock()
        cdp.call.return_value = {"model": {"width": 0, "height": 0}}
        with patch.object(app, "query", side_effect=[4, 5]):
            state = app.authentication_state(cdp, {"nodeId": 1}, "岗位内容")
        self.assertEqual(state, "unconfirmed")

    def test_visible_account_and_logout_confirm_login(self):
        cdp = Mock()
        cdp.call.return_value = {"model": {"width": 48, "height": 36}}
        with patch.object(app, "query", side_effect=[4, 5]):
            state = app.authentication_state(cdp, {"nodeId": 1}, "账户菜单")
        self.assertEqual(state, "authenticated")


class SalaryTests(unittest.TestCase):
    def test_restore_k_from_dom(self):
        result = normalize_salary("8-10", "\ue037-\ue038K")
        self.assertEqual(result["salary"], "8-10K")
        self.assertEqual(result["salary_unit"], "K")
        self.assertIsNone(result["salary_period"])

    def test_restore_pay_month_suffix(self):
        result = normalize_salary("10-15K·13", "\ue031-\ue032K·13薪")
        self.assertEqual(result["salary"], "10-15K·13薪")
        self.assertEqual(result["salary_pay_months"], 13)

    def test_restore_daily_unit(self):
        result = normalize_salary("120-180", "\ue031\ue032-\ue033\ue034元/天")
        self.assertEqual(result["salary"], "120-180元/天")
        self.assertEqual(result["salary_period"], "天")

    def test_missing_units_are_not_invented(self):
        result = normalize_salary("120-180", "\ue031-\ue032")
        self.assertEqual(result["salary_status"], "unit_unknown")
        self.assertIsNone(result["salary_unit"])

    def test_negotiable_and_decimal_salary(self):
        self.assertEqual(normalize_salary("薪资面议")["salary_status"], "negotiable")
        self.assertEqual(normalize_salary("1.5–2万/月")["salary"], "1.5-2万/月")


class CompletenessTests(unittest.TestCase):
    def test_requirement_tags_are_preserved(self):
        parser = CardParser()
        parser.feed('<ul class="tag-list"><li>1-3年</li><li>本科</li></ul>')
        fields = parser.fields()
        self.assertEqual(fields["tags"], ["1-3年", "本科"])
        self.assertEqual(fields["education"], "本科")
        self.assertEqual(fields["experience"], "1-3年")

    def test_summary_includes_salary_failure_and_shortfall(self):
        payload = {"requested_count": 3, "jobs": [
            {"description_status": "ok", "salary_status": "error"},
            {"description_status": "ok", "salary_status": "ok"},
        ]}
        app.update_report(payload)
        self.assertEqual(payload["description_success_count"], 2)
        self.assertEqual(payload["fully_extracted_count"], 1)
        self.assertEqual(payload["missing_count"], 1)

    def test_scroll_loads_new_jobs_and_deduplicates(self):
        def html(cdp, node):
            return (f'<li><a class="job-name" href="/job_detail/job{node}.html">岗位{node}</a>'
                    '<span class="job-salary">150元/天</span></li>')
        payload = {"requested_count": 2, "jobs": []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            with patch.object(app, "wait_cards", side_effect=[[1], [1, 2]]):
                with patch.object(app, "outer_html", side_effect=html):
                    with patch.object(app, "scroll_list") as scroll:
                        with patch.object(app.time, "sleep"):
                            reason = app.collect_list(Mock(), payload, path, path / "jobs.json", "url", True, 3)
            self.assertEqual(reason, "requested_limit_reached")
            self.assertEqual([job["id"] for job in payload["jobs"]], ["job1", "job2"])
            scroll.assert_called_once()


if __name__ == "__main__":
    unittest.main()
