import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import collect_jobs as app
import description_ocr as ocr


def line(text, y, score=0.99):
    return {"text": text, "score": score, "x": 0, "y": y, "width": 200, "height": 16}


class DescriptionOCRTests(unittest.TestCase):
    def test_overlap_is_merged_but_repeated_paragraphs_are_preserved(self):
        items = [line("需要掌握 Python 开发", 50), line("需要掌握 Python 开发", 52),
                 line("需要掌握 Python 开发", 150)]
        result = ocr.merge_lines(items)
        self.assertEqual(len(result), 2)
        self.assertEqual([item["y"] for item in result], [50, 150])

    def test_scroll_container_bounds_limit_capture(self):
        cdp = Mock()
        cdp.call.return_value = {
            "strings": ["visible", "hidden", "auto"],
            "documents": [{"nodes": {"backendNodeId": [1, 2, 3], "parentIndex": [-1, 0, 1]},
                           "layout": {"nodeIndex": [0, 1, 2], "styles": [[], [1, 2], [0, 0]],
                                      "bounds": [[0, 0, 800, 1000], [10, 120, 600, 300], [10, 120, 600, 900]]}}],
        }
        viewport = {"clientWidth": 800, "clientHeight": 700, "pageX": 0, "pageY": 100}
        self.assertEqual(ocr.visible_bounds(cdp, 3, viewport), (10, 20, 610, 320))

    def test_multiline_ocr_spans_multiple_tiles(self):
        cdp = Mock()
        def call(method, params=None):
            if method == "DOM.describeNode":
                return {"node": {"backendNodeId": 10}}
            if method == "Page.getLayoutMetrics":
                return {"cssLayoutViewport": {"clientWidth": 800, "clientHeight": 800, "pageX": 0, "pageY": 0}}
            if method == "Page.captureScreenshot":
                return {"data": base64.b64encode(b"fake-image-for-mocked-ocr").decode()}
            return {}
        cdp.call.side_effect = call
        geometry = Mock(side_effect=[
            {"viewport_rect": {"x": 10, "y": 100, "width": 500, "height": 700}, "page_scroll": {"x": 0, "y": 0}},
            {"viewport_rect": {"x": 10, "y": 100, "width": 500, "height": 700}, "page_scroll": {"x": 0, "y": 0}},
            {"viewport_rect": {"x": 10, "y": -300, "width": 500, "height": 700}, "page_scroll": {"x": 0, "y": 400}},
        ])
        first, second = "岗位职责：负责后端开发与接口设计。" * 3, "任职要求：掌握数据库与测试方法。" * 3
        boxes = [[[0, 100], [400, 100], [400, 140], [0, 140]]]
        engine = Mock(side_effect=[Mock(txts=(first,), scores=(0.99,), boxes=boxes),
                                   Mock(txts=(second,), scores=(0.98,), boxes=boxes)])
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch.object(ocr, "_engine", engine):
                with patch.object(ocr, "visible_bounds", return_value=(0, 0, 800, 800)):
                    text, metadata = ocr.extract_description(cdp, 1, folder, "job", geometry)
            self.assertIn(first, text)
            self.assertIn(second, text)
            self.assertEqual(len(metadata["description_ocr_images"]), 2)
            self.assertTrue(metadata["description_ocr_review_required"])
            self.assertTrue((folder / "job-description-ocr.json").exists())
        for call_args in engine.call_args_list:
            self.assertTrue(call_args.kwargs["use_det"])

    def test_confused_dom_uses_ocr_instead_of_access_gate(self):
        cdp = Mock()
        cdp.call.return_value = {"node": {"backendNodeId": 5}}
        job = {"id": "test", "url": "https://www.zhipin.com/job_detail/test.html"}
        clean = "职位描述：负责应用开发、测试和维护，能够与团队合作解决实际问题。" * 2
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "navigate"), patch.object(app, "document", return_value={"nodeId": 1, "documentURL": job["url"]}), \
                    patch.object(app, "query", return_value=2), patch.object(app, "snapshot"), \
                    patch.object(app, "rendered_text", side_effect=["页面", "\ue031"]), \
                    patch.object(app, "authentication_state", return_value="authenticated"), \
                    patch.object(ocr, "extract_description", return_value=(clean, {"description_source": "OCR"})) as fallback:
                self.assertEqual(app.read_description(cdp, job, Path(directory)), clean)
                fallback.assert_called_once()
        self.assertEqual(job["description_source"], "OCR")

    def test_ocr_failure_is_item_error_not_batch_access_gate(self):
        cdp = Mock()
        cdp.call.return_value = {"node": {"backendNodeId": 5}}
        job = {"id": "test", "url": "https://www.zhipin.com/job_detail/test.html"}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(app, "navigate"), patch.object(app, "document", return_value={"nodeId": 1, "documentURL": job["url"]}), \
                    patch.object(app, "query", return_value=2), patch.object(app, "snapshot"), \
                    patch.object(app, "rendered_text", side_effect=["页面", "\ue031" * 45]), \
                    patch.object(app, "authentication_state", return_value="authenticated"), \
                    patch.object(ocr, "extract_description", side_effect=ValueError("OCR为空")):
                with self.assertRaises(ValueError):
                    app.read_description(cdp, job, Path(directory))


if __name__ == "__main__":
    unittest.main()
