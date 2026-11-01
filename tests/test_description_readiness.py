import itertools
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs as app
import description_ocr as ocr


class DescriptionReadinessTests(unittest.TestCase):
    def read_texts(self, texts, *, authenticated=True, wrong_url=False):
        clock = [0.0]
        values = itertools.chain(texts, itertools.repeat(texts[-1]))
        job = {"id": "short", "url": "https://www.zhipin.com/job_detail/short.html"}
        root = {"nodeId": 1, "documentURL": job["url"] if not wrong_url else "about:blank"}
        cdp = Mock()
        cdp.call.return_value = {"node": {"backendNodeId": 5}}

        def rendered(data, backend=None):
            return next(values) if backend is not None else "正常页面"

        def sleep(seconds):
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(app, "navigate"), patch.object(app, "document", return_value=root), \
                patch.object(app, "query", return_value=2), patch.object(app, "snapshot"), \
                patch.object(app, "rendered_text", side_effect=rendered), \
                patch.object(app, "authentication_state", return_value="authenticated" if authenticated else "unconfirmed"), \
                patch.object(app.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(app.time, "sleep", side_effect=sleep), \
                patch.object(ocr, "extract_description") as fallback:
            folder = Path(directory)
            result = app.read_description(cdp, job, folder, timeout=4)
            self.assertEqual((folder / "short-description.txt").read_text(encoding="utf-8"), result)
            fallback.assert_not_called()
        return result, clock[0], job

    def test_real_short_description_is_saved_after_stabilizing(self):
        text = "有量化经验，从事过股票基金交易行业。"
        self.assertLess(len(text), 40)
        result, elapsed, job = self.read_texts([text])
        self.assertEqual(result, text)
        self.assertGreaterEqual(elapsed, 1)
        self.assertEqual(job["description_source"], "CDP_DOMSnapshot")

    def test_changing_short_text_waits_for_latest_content(self):
        full = "有量化经验，从事过股票基金交易行业。"
        result, elapsed, _ = self.read_texts(["有量化经验", full])
        self.assertEqual(result, full)
        self.assertGreaterEqual(elapsed, 1.4)

    def test_loading_placeholders_are_not_accepted(self):
        for text in ("加载中...", "正在加载…", "Loading..."):
            with self.subTest(text=text), self.assertRaisesRegex(TimeoutError, "正文仍在加载"):
                self.read_texts([text])

    def test_loading_then_short_description(self):
        result, _, _ = self.read_texts(["加载中...", "有量化经验。"])
        self.assertEqual(result, "有量化经验。")

    def test_short_text_does_not_bypass_login_check(self):
        with self.assertRaises(app.LoginRequired):
            self.read_texts(["有量化经验。"], authenticated=False)

    def test_short_text_does_not_bypass_job_url_check(self):
        with self.assertRaisesRegex(TimeoutError, "URL 与目标岗位不一致"):
            self.read_texts(["有量化经验。"], wrong_url=True)


if __name__ == "__main__":
    unittest.main()
