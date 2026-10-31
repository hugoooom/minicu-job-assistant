import unittest
from unittest.mock import Mock, patch

from collect_jobs import screenshot_geometry, stable_screenshot_geometry


def metrics(x=0, y=0):
    return {"cssLayoutViewport": {"pageX": x, "pageY": y, "clientWidth": 1000, "clientHeight": 700}}


class ScreenshotTests(unittest.TestCase):
    def test_scrolled_viewport_coordinates_become_document_coordinates(self):
        result = screenshot_geometry([20, 638, 120, 638, 120, 662, 20, 662], metrics(y=2724))
        self.assertEqual(result["clip"], {"x": 12, "y": 3354, "width": 116, "height": 40, "scale": 2})

    def test_horizontal_scroll_is_included(self):
        result = screenshot_geometry([20, 30, 120, 30, 120, 54, 20, 54], metrics(x=200, y=100))
        self.assertEqual(result["clip"]["x"], 212)
        self.assertEqual(result["clip"]["y"], 122)

    def test_padding_near_document_origin_is_clamped_without_widening(self):
        result = screenshot_geometry([1, 1, 45, 1, 45, 25, 1, 25], metrics())
        self.assertEqual(result["clip"], {"x": 0, "y": 0, "width": 53, "height": 33, "scale": 2})

    def test_empty_element_is_rejected(self):
        with self.assertRaises(ValueError):
            screenshot_geometry([0] * 8, metrics())

    def test_capture_waits_for_consistent_scroll_and_bounds(self):
        cdp = Mock()
        box = {"model": {"border": [20, 30, 120, 30, 120, 54, 20, 54]}}
        # 第一轮测量期间还在滚动；接下来三轮稳定后才能截图。
        cdp.call.side_effect = [metrics(y=10), box, metrics(y=20)] + [metrics(y=20), box, metrics(y=20)] * 3
        with patch("collect_jobs.time.sleep"):
            result = stable_screenshot_geometry(cdp, 1)
        self.assertEqual(result["clip"]["y"], 42)
        self.assertEqual(cdp.call.call_count, 12)


if __name__ == "__main__":
    unittest.main()
