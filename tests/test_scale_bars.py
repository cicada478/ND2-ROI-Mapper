"""比例尺物理换算和绘制层的回归测试。"""

from __future__ import annotations

import unittest

from PIL import Image

from nd2_roi_locator import ScaleBarOverlay, draw_scale_bars
from nd2_roi_ui import ImageViewer, ScaleBarState


class ScaleBarOverlayTests(unittest.TestCase):
    def test_physical_length_is_converted_with_overview_pixel_size(self) -> None:
        bar = ScaleBarOverlay(
            center_x_px=100,
            bar_y_px=80,
            length_um=10,
            pixel_size_um=0.5,
        )
        self.assertEqual(bar.length_px, 20)

    def test_drawing_returns_copy_and_preserves_image_size(self) -> None:
        source = Image.new("RGB", (400, 240), (12, 18, 24))
        original_pixel = source.getpixel((100, 200))
        result = draw_scale_bars(
            source,
            [
                ScaleBarOverlay(
                    center_x_px=100,
                    bar_y_px=200,
                    length_um=10,
                    pixel_size_um=0.5,
                    color=(255, 255, 255),
                    line_width_px=4,
                )
            ],
        )
        self.assertIsNot(result, source)
        self.assertEqual(result.size, source.size)
        self.assertEqual(source.getpixel((100, 200)), original_pixel)
        self.assertNotEqual(result.getpixel((100, 200)), original_pixel)

    def test_invalid_pixel_size_is_rejected(self) -> None:
        bar = ScaleBarOverlay(100, 80, length_um=10, pixel_size_um=0)
        with self.assertRaisesRegex(ValueError, "pixel size"):
            _ = bar.length_px


class ScaleBarBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.viewer = ImageViewer.__new__(ImageViewer)
        self.viewer._image = Image.new("RGB", (1000, 600))

    def test_overview_bar_limits_keep_label_and_line_inside_bounds(self) -> None:
        state = ScaleBarState(
            key="overview",
            scope_name="10X overview",
            bounds=(0, 0, 1000, 600),
            pixel_size_um=0.5,
        )
        limits = self.viewer._scale_bar_limits(state)
        self.assertIsNotNone(limits)
        min_x, max_x, min_y, max_y = limits
        self.assertLess(min_x, max_x)
        self.assertLess(min_y, max_y)

    def test_scale_bar_that_is_too_long_for_roi_is_rejected(self) -> None:
        state = ScaleBarState(
            key="zoom-1",
            scope_name="Zoom in ROI 01",
            bounds=(100, 100, 200, 180),
            pixel_size_um=0.1,
            length_um=100,
        )
        self.assertIsNone(self.viewer._scale_bar_limits(state))


if __name__ == "__main__":
    unittest.main()
