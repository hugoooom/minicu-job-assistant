import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs
from extract_first_job import CardParser, needs_ocr
from inspect_raw_cdp import RawCDP

CARD = '''<li class="job-card-box">
<a class="job-name" href="/job_detail/example.html">Python <b>实习生</b></a>
<span class="job-salary">150–200 元/天</span>
<img src="example.png"><span class="boss-name">示例公司</span>
<span class="company-location">广州</span></li>'''


class CoreTests(unittest.TestCase):
    def test_card_fields_and_nested_title(self):
        parser = CardParser()
        parser.feed(CARD)
        fields = parser.fields()
        self.assertEqual(fields["title"], "Python 实习生")
        self.assertEqual(fields["company"], "示例公司")
        self.assertEqual(fields["location"], "广州")

    def test_salary_fallback(self):
        self.assertTrue(needs_ocr(""))
        self.assertTrue(needs_ocr("\ue037-\ue038K"))
        self.assertFalse(needs_ocr("150–200 元/天"))

    def test_export_omits_raw_salary(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(collect_jobs, "outer_html", return_value=CARD):
                with patch.object(collect_jobs, "RapidOCR") as engine:
                    jobs = collect_jobs.read_list(Mock(), [1], Path(folder))
            engine.assert_not_called()
        self.assertNotIn("salary_raw", jobs[0])
        self.assertEqual(jobs[0]["salary"], "150-200元/天")
        self.assertEqual(jobs[0]["salary_source"], "DOM")

    def test_ocr_export_omits_confused_characters(self):
        card = CARD.replace("150–200 元/天", "\ue031\ue032元/天")
        result = Mock(txts=("150-200元/天",), scores=(0.99,))
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(collect_jobs, "outer_html", return_value=card):
                with patch.object(collect_jobs, "salary_screenshot"):
                    with patch.object(collect_jobs, "RapidOCR") as engine:
                        engine.return_value.return_value = result
                        jobs = collect_jobs.read_list(Mock(), [1], Path(folder))
        self.assertEqual(jobs[0]["salary"], "150-200元/天")
        self.assertEqual(jobs[0]["salary_source"], "OCR")
        self.assertNotIn("salary_raw", jobs[0])
        self.assertNotIn("\ue031", json.dumps(jobs, ensure_ascii=False))

    def test_salary_ocr_with_adjacent_title_ellipsis_is_saved(self):
        card = CARD.replace("150–200 元/天", "\ue032\ue031-\ue032\ue036K")
        result = Mock(txts=(".. 10-15K",), scores=(0.96884,))
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(collect_jobs, "outer_html", return_value=card), \
                patch.object(collect_jobs, "salary_screenshot"), \
                patch.object(collect_jobs, "RapidOCR") as engine:
            engine.return_value.return_value = result
            jobs = collect_jobs.read_list(Mock(), [1], Path(folder))
        self.assertEqual(jobs[0]["salary"], "10-15K")
        self.assertEqual(jobs[0]["salary_status"], "ok")
        self.assertNotIn("salary_error", jobs[0])

    def test_rendered_text_excludes_hidden_parent(self):
        strings = ["#document", "P", "#text", "Hello", "hidden", "world",
                   "block", "visible", "1", "16px", "none", "SPAN"]
        data = {"strings": strings, "documents": [{
            "nodes": {"backendNodeId": [1, 2, 3, 4, 5, 6],
                      "parentIndex": [-1, 0, 1, 1, 3, 1],
                      "nodeName": [0, 1, 2, 11, 2, 2]},
            "layout": {"nodeIndex": [1, 2, 3, 4, 5],
                       "styles": [[6, 7, 8, 9], [6, 7, 8, 9], [10, 7, 8, 9],
                                  [6, 7, 8, 9], [6, 7, 8, 9]],
                       "bounds": [[0, 0, 100, 20]] * 5,
                       "text": [0, 3, 0, 4, 5]}}]}
        self.assertEqual(collect_jobs.rendered_text(data, 2), "Helloworld")

    def test_login_wall_stops_extraction(self):
        with self.assertRaises(collect_jobs.AccessGate):
            collect_jobs.check_gate("登录查看完整内容")

    def test_cdp_ignores_events_while_waiting_for_reply(self):
        connection = Mock()
        connection.recv.side_effect = [
            json.dumps({"method": "Page.frameNavigated"}),
            json.dumps({"id": 1, "result": {"ok": True}}),
        ]
        self.assertEqual(RawCDP(connection).call("Page.enable"), {"ok": True})

    def test_cdp_reports_protocol_error(self):
        connection = Mock()
        connection.recv.return_value = json.dumps({"id": 1, "error": {"message": "test"}})
        with self.assertRaises(RuntimeError):
            RawCDP(connection).call("Page.enable")


if __name__ == "__main__":
    unittest.main()
