"""比例尺物理换算和绘制层的回归测试。"""

from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory

from PIL import Image

from nd2_roi_locator import (
    ROIResult,
    ScaleBarOverlay,
    draw_roi_geometry,
    draw_roi_labels,
    draw_rois,
    draw_scale_bars,
    layout_roi_labels,
    scale_bar_label_size,
    save_export_image,
)
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

    def test_hidden_text_removes_label_from_scale_bar_bounds(self) -> None:
        shown = scale_bar_label_size(10, (1200, 800), font_size_px=48, show_text=True)
        hidden = scale_bar_label_size(10, (1200, 800), font_size_px=48, show_text=False)
        self.assertGreater(shown[0], 0)
        self.assertGreater(shown[1], 0)
        self.assertEqual(hidden, (0, 0))

    def test_text_size_changes_rendered_label_dimensions(self) -> None:
        small = scale_bar_label_size(10, (1200, 800), font_size_px=14)
        large = scale_bar_label_size(10, (1200, 800), font_size_px=48)
        self.assertGreater(large[0], small[0])
        self.assertGreater(large[1], small[1])


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

    def test_bottom_right_preset_uses_maximum_allowed_coordinates(self) -> None:
        state = ScaleBarState(
            key="overview",
            scope_name="10X overview",
            bounds=(0, 0, 1000, 600),
            pixel_size_um=0.5,
        )
        limits = self.viewer._scale_bar_limits(state)
        self.assertIsNotNone(limits)
        assert limits is not None
        self.assertTrue(self.viewer._place_scale_bar(state, "bottom-right"))
        self.assertEqual((state.center_x_px, state.bar_y_px), (limits[1], limits[3]))


class FixedROILabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.image = Image.new("RGB", (800, 500), (12, 18, 24))
        self.roi = ROIResult(
            center_x_px=400,
            center_y_px=250,
            width_px=160,
            height_px=100,
            dx_um=0,
            dy_um=0,
            dx_px=0,
            dy_px=0,
            offset_x_um=0,
            offset_y_um=0,
        )
        self.items = [(self.roi, "sample.nd2\nObjective 100X | Zoom 2×", (255, 218, 56))]

    def test_split_geometry_and_label_layers_match_combined_render(self) -> None:
        geometry = draw_roi_geometry(self.image, self.items)
        placements = layout_roi_labels(self.image, self.items)
        split = draw_roi_labels(geometry, placements)
        combined = draw_rois(self.image, self.items)
        self.assertEqual(split.tobytes(), combined.tobytes())

class ExportQualityTests(unittest.TestCase):
    def test_jpeg_quality_changes_encoded_file_size(self) -> None:
        image = Image.effect_noise((320, 240), 80).convert("RGB")
        with TemporaryDirectory() as directory:
            maximum = save_export_image(image, f"{directory}/maximum.jpg", jpeg_quality=100)
            compact = save_export_image(image, f"{directory}/compact.jpg", jpeg_quality=75)
            self.assertGreater(maximum.stat().st_size, compact.stat().st_size)

    def test_invalid_jpeg_quality_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            save_export_image(Image.new("RGB", (10, 10)), "unused.jpg", jpeg_quality=0)


if __name__ == "__main__":
    unittest.main()
