"""ND2 ROI Mapper 的桌面界面。

本模块只负责工作流、状态、预览和导出。ND2 解析、stage 坐标换算、物镜偏移
以及 ROI 几何计算仍由 :mod:`nd2_roi_locator` 提供，避免显示层改变科学数据。
"""

from __future__ import annotations

import queue
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk
from tkinterdnd2 import COPY, DND_FILES, TkinterDnD

from nd2_roi_locator import (
    MICROSCOPE_SIGNS,
    MetadataError,
    ND2Metadata,
    ROIResult,
    ScaleBarOverlay,
    _default_output,
    _print_metadata,
    _print_result,
    calculate_roi_position,
    draw_scale_bars,
    draw_rois,
    format_scale_bar_length,
    read_nd2_image,
    read_nd2_metadata,
    scale_bar_font_size,
    scale_bar_label_size,
)


T = TypeVar("T")


# 颜色来自显微成像工作站的视觉语义，而不是装饰性品牌配色。
COLORS = {
    "app_bg": "#E8EDF1",
    "panel": "#F7F9FA",
    "panel_alt": "#EEF2F4",
    "viewer": "#0E151A",
    "viewer_toolbar": "#182229",
    "header": "#142129",
    "border": "#C8D1D7",
    "border_dark": "#2B3942",
    "text": "#17252E",
    "muted": "#5B6B75",
    "inverse": "#F4F8FA",
    "inverse_muted": "#AFC0CA",
    "accent": "#087F8C",
    "accent_hover": "#066C77",
    "accent_soft": "#D9EEF0",
    "success": "#237A57",
    "warning": "#A56212",
    "error": "#B33A3A",
    "disabled": "#9AA7AE",
}

ROI_COLORS = (
    (255, 218, 56),
    (49, 214, 232),
    (246, 112, 221),
    (255, 145, 61),
    (104, 224, 118),
    (119, 164, 255),
)

SCALE_BAR_LENGTHS_UM = (1000.0, 100.0, 10.0, 1.0)
SCALE_BAR_COLORS = {
    "White": (255, 255, 255),
    "Yellow": (255, 218, 56),
    "Cyan": (49, 214, 232),
    "Magenta": (246, 112, 221),
    "Black": (0, 0, 0),
}
SCALE_BAR_WIDTHS_PX = (2, 4, 6, 8)


@dataclass(frozen=True)
class HighMagItem:
    """UI 中一条高倍文件记录；metadata 本身仍是核心模块的数据结构。"""

    path: Path
    metadata: ND2Metadata


@dataclass
class ScaleBarState:
    """Viewer 中一个可交互比例尺；所有位置仍使用 10X 原图像素坐标。"""

    key: str
    scope_name: str
    bounds: tuple[float, float, float, float]
    pixel_size_um: float
    center_x_px: float = 0.0
    bar_y_px: float = 0.0
    length_um: float = 10.0
    color_name: str = "White"
    line_width_px: int = 4

    def overlay(self) -> ScaleBarOverlay:
        return ScaleBarOverlay(
            center_x_px=self.center_x_px,
            bar_y_px=self.bar_y_px,
            length_um=self.length_um,
            pixel_size_um=self.pixel_size_um,
            color=SCALE_BAR_COLORS[self.color_name],
            line_width_px=self.line_width_px,
        )


class MetadataPanel(ttk.Frame):
    """以固定两列展示关键采集元数据。"""

    ROWS = (
        ("Source file", "source"),
        ("Objective", "objective"),
        ("Scan zoom", "zoom"),
        ("Captured", "captured"),
        ("Stage X", "x"),
        ("Stage Y", "y"),
        ("Stage Z", "z"),
        ("Pixel size X", "pixel_x"),
        ("Pixel size Y", "pixel_y"),
        ("Image size", "image_size"),
        ("Channels", "channels"),
    )

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, style="Panel.TFrame", padding=(10, 8))
        self.columnconfigure(1, weight=1)
        self._values: dict[str, ttk.Label] = {}
        for row, (label, key) in enumerate(self.ROWS):
            ttk.Label(self, text=label, style="MetaKey.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=2
            )
            value = ttk.Label(
                self,
                text="—",
                style=(
                    "MetaTextValue.TLabel" if key in {"source", "channels"} else "MetaValue.TLabel"
                ),
                anchor="e",
                justify="right",
                # 文件名、单位和通道列表不使用固定宽度强制换行。
                # 只有可能很长的物镜完整名称允许在 metadata 区域内换行。
                wraplength=238 if key == "objective" else 0,
            )
            value.grid(row=row, column=1, sticky="ew", pady=2)
            self._values[key] = value

    def show_metadata(self, metadata: ND2Metadata | None) -> None:
        if metadata is None:
            for label in self._values.values():
                label.configure(text="—")
            return

        objective = metadata.objective_label or "Not available"
        if metadata.objective_name:
            objective = f"{objective} · {metadata.objective_name}"
        values = {
            "source": metadata.path.name,
            "objective": objective,
            "zoom": (
                f"{metadata.scan_zoom:g}×" if metadata.scan_zoom is not None else "Not available"
            ),
            "captured": (
                metadata.acquisition_time.strftime("%Y-%m-%d %H:%M:%S")
                if metadata.acquisition_time is not None
                else "Not available"
            ),
            "x": f"{metadata.stage_x_um:,.3f} µm",
            "y": f"{metadata.stage_y_um:,.3f} µm",
            "z": (
                f"{metadata.stage_z_um:,.3f} µm"
                if metadata.stage_z_um is not None
                else "Not available"
            ),
            "pixel_x": f"{metadata.pixel_size_x_um:.6f} µm/px",
            "pixel_y": f"{metadata.pixel_size_y_um:.6f} µm/px",
            "image_size": f"{metadata.width_px:,} × {metadata.height_px:,} px",
            "channels": (
                ", ".join(metadata.channel_names) if metadata.channel_names else "Not available"
            ),
        }
        for key, value in values.items():
            self._values[key].configure(text=value)


class ScrollablePanel(ttk.Frame):
    """让紧凑参数轨在较小窗口中仍能完整访问。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, style="Panel.TFrame")
        """参数栏 Sidebar / Control Panel"""
        self.canvas = tk.Canvas(
            self,
            background=COLORS["panel"],
            borderwidth=0,
            highlightthickness=0,
            width=560,
        )
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.content = ttk.Frame(self.canvas, style="Panel.TFrame", padding=(14, 12, 12, 18))
        self._window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.content.bind("<Configure>", self._sync_scrollregion)
        self.canvas.bind("<Configure>", self._sync_width)
        self.canvas.bind(
            "<Enter>", lambda _event: self.canvas.bind_all("<MouseWheel>", self._wheel)
        )
        self.canvas.bind("<Leave>", lambda _event: self.canvas.unbind_all("<MouseWheel>"))

    def _sync_scrollregion(self, _event: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_width(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _wheel(self, event: tk.Event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class ImageViewer(ttk.Frame):
    """保持科研坐标独立的 fit/zoom/pan 图像查看器。"""

    def __init__(
        self,
        master: tk.Misc,
        on_scale_bar_changed: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(master, style="Viewer.TFrame")
        self._on_scale_bar_changed = on_scale_bar_changed
        self._image: Image.Image | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._image_item: int | None = None
        self._empty_item: int | None = None
        self._scale = 1.0
        self._offset_x = 0.0
        self._offset_y = 0.0
        self._fit_mode = True
        self._drag_origin: tuple[int, int, float, float] | None = None
        self._scale_drag_origin: tuple[str, int, int, float, float] | None = None
        self._active_scale_key: str | None = None
        self._scale_bars: dict[str, ScaleBarState] = {}
        self._overview_scale_enabled = False
        self._zoom_scale_enabled = False
        self._resize_job: str | None = None

        toolbar = tk.Frame(self, background=COLORS["viewer_toolbar"], height=78)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)
        primary_toolbar = tk.Frame(toolbar, background=COLORS["viewer_toolbar"], height=42)
        primary_toolbar.pack(fill="x")
        primary_toolbar.pack_propagate(False)
        scale_toolbar = tk.Frame(toolbar, background="#111C22", height=36)
        scale_toolbar.pack(fill="x")
        scale_toolbar.pack_propagate(False)
        tk.Label(
            primary_toolbar,
            text="10X VIEWER",
            background=COLORS["viewer_toolbar"],
            foreground=COLORS["inverse_muted"],
            font=("Segoe UI Semibold", 9),
        ).pack(side="left", padx=(14, 18))
        self._toolbar_button(primary_toolbar, "Fit", self.fit_to_window).pack(
            side="left", padx=(0, 6), pady=7
        )
        self._toolbar_button(primary_toolbar, "100%", self.actual_size).pack(side="left", pady=7)
        self._toolbar_button(
            primary_toolbar, "−", lambda: self._zoom_from_center(1 / 1.15)
        ).pack(
            side="left", padx=(6, 0), pady=7
        )
        self._toolbar_button(primary_toolbar, "+", lambda: self._zoom_from_center(1.15)).pack(
            side="left", padx=(6, 0), pady=7
        )
        self.overview_scale_button = self._toolbar_button(
            scale_toolbar, "10X scale bar", self._toggle_overview_scale
        )
        self.overview_scale_button.configure(padx=8)
        self.overview_scale_button.pack(side="left", padx=(8, 0), pady=4)
        self.zoom_scale_button = self._toolbar_button(
            scale_toolbar, "Zoom in scale bar", self._toggle_zoom_scale
        )
        self.zoom_scale_button.configure(padx=8)
        self.zoom_scale_button.pack(side="left", padx=(4, 0), pady=4)
        self.overview_scale_button.configure(state="disabled")
        self.zoom_scale_button.configure(state="disabled")
        self.zoom_text = tk.StringVar(value="—")
        self.cursor_text = tk.StringVar(value="x —   y —")
        tk.Label(
            primary_toolbar,
            textvariable=self.cursor_text,
            background=COLORS["viewer_toolbar"],
            foreground=COLORS["inverse_muted"],
            font=("Cascadia Mono", 9),
        ).pack(side="right", padx=(8, 14))
        tk.Label(
            primary_toolbar,
            textvariable=self.zoom_text,
            background=COLORS["viewer_toolbar"],
            foreground=COLORS["inverse"],
            font=("Cascadia Mono Semibold", 9),
        ).pack(side="right", padx=8)

        self.canvas = tk.Canvas(
            self,
            background=COLORS["viewer"],
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=COLORS["viewer"],
            highlightcolor=COLORS["accent"],
            cursor="crosshair",
            takefocus=True,
        )
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<ButtonPress-1>", self._start_pan)
        self.canvas.bind("<B1-Motion>", self._pan)
        self.canvas.bind("<ButtonRelease-1>", self._end_pan)
        self.canvas.bind("<Button-3>", self._show_scale_bar_menu)
        self.canvas.bind("<Button-2>", self._show_scale_bar_menu)
        self.canvas.bind("<Control-Button-1>", self._show_scale_bar_menu)
        self.canvas.bind("<Motion>", self._cursor_motion)
        self.canvas.bind("<Leave>", lambda _event: self.cursor_text.set("x —   y —"))
        self.canvas.bind("<KeyPress-plus>", lambda _event: self._zoom_from_center(1.15))
        self.canvas.bind("<KeyPress-equal>", lambda _event: self._zoom_from_center(1.15))
        self.canvas.bind("<KeyPress-minus>", lambda _event: self._zoom_from_center(1 / 1.15))
        self.canvas.bind("<KeyPress-Left>", lambda _event: self._keyboard_pan(40, 0))
        self.canvas.bind("<KeyPress-Right>", lambda _event: self._keyboard_pan(-40, 0))
        self.canvas.bind("<KeyPress-Up>", lambda _event: self._keyboard_pan(0, 40))
        self.canvas.bind("<KeyPress-Down>", lambda _event: self._keyboard_pan(0, -40))
        self.canvas.bind("<Shift-KeyPress-Left>", lambda _event: self._keyboard_move_scale(-5, 0))
        self.canvas.bind("<Shift-KeyPress-Right>", lambda _event: self._keyboard_move_scale(5, 0))
        self.canvas.bind("<Shift-KeyPress-Up>", lambda _event: self._keyboard_move_scale(0, -5))
        self.canvas.bind("<Shift-KeyPress-Down>", lambda _event: self._keyboard_move_scale(0, 5))
        self._show_empty()

    @staticmethod
    def _toolbar_button(master: tk.Misc, text: str, command: Callable[[], None]) -> tk.Button:
        return tk.Button(
            master,
            text=text,
            command=command,
            background="#24333C",
            activebackground="#31434E",
            foreground=COLORS["inverse"],
            activeforeground=COLORS["inverse"],
            disabledforeground=COLORS["disabled"],
            relief="flat",
            borderwidth=0,
            padx=12,
            pady=3,
            font=("Segoe UI", 9),
            cursor="hand2",
        )

    @staticmethod
    def _set_toggle_button_style(button: tk.Button, active: bool) -> None:
        """使用稳定的颜色状态表达比例尺开关，不改变按钮尺寸。"""

        button.configure(
            background=COLORS["accent"] if active else "#24333C",
            activebackground=COLORS["accent_hover"] if active else "#31434E",
        )

    def set_scale_bar_control_state(self, has_overview: bool, has_zoom: bool, busy: bool) -> None:
        """根据应用 workflow 控制比例尺按钮是否可用。"""

        self.overview_scale_button.configure(
            state="normal" if has_overview and not busy else "disabled"
        )
        self.zoom_scale_button.configure(state="normal" if has_zoom and not busy else "disabled")

    def configure_overview_scale_bar(self, pixel_size_um: float) -> None:
        """为当前 10X 图像建立一个默认 10 µm、白色比例尺。"""

        if self._image is None:
            return
        self._scale_bars.clear()
        self._overview_scale_enabled = False
        self._zoom_scale_enabled = False
        state = ScaleBarState(
            key="overview",
            scope_name="10X overview",
            bounds=(0.0, 0.0, float(self._image.width), float(self._image.height)),
            pixel_size_um=pixel_size_um,
        )
        self._scale_bars[state.key] = state
        self._place_scale_bar(state, "bottom-left")
        self._active_scale_key = state.key
        self._update_scale_bar_buttons()
        self._render_scale_bars()

    def configure_zoom_scale_bars(
        self,
        rois: list[ROIResult],
        pixel_size_um: float,
    ) -> None:
        """为每个可见高倍 ROI 建立一个限制在自身矩形内的比例尺。"""

        if self._image is None:
            return
        for key in [key for key in self._scale_bars if key.startswith("zoom-")]:
            del self._scale_bars[key]
        self._zoom_scale_enabled = False
        for index, roi in enumerate(rois, start=1):
            left, top, right, bottom = roi.box
            bounds = (
                max(0.0, left),
                max(0.0, top),
                min(float(self._image.width), right),
                min(float(self._image.height), bottom),
            )
            if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
                continue
            state = ScaleBarState(
                key=f"zoom-{index}",
                scope_name=f"Zoom in ROI {index:02d}",
                bounds=bounds,
                pixel_size_um=pixel_size_um,
            )
            self._scale_bars[state.key] = state
            self._place_scale_bar(state, "bottom-left")
        self._update_scale_bar_buttons()
        self._render_scale_bars()

    def clear_zoom_scale_bars(self) -> None:
        """ROI 映射失效时只清除高倍比例尺，保留 10X 比例尺设置。"""

        for key in [key for key in self._scale_bars if key.startswith("zoom-")]:
            del self._scale_bars[key]
        self._zoom_scale_enabled = False
        if self._active_scale_key not in self._scale_bars:
            self._active_scale_key = "overview" if "overview" in self._scale_bars else None
        self._update_scale_bar_buttons()
        self._render_scale_bars()

    def clear_scale_bars(self) -> None:
        """移除 overview 时清除全部比例尺和交互状态。"""

        self._scale_bars.clear()
        self._overview_scale_enabled = False
        self._zoom_scale_enabled = False
        self._active_scale_key = None
        self._update_scale_bar_buttons()
        self.canvas.delete("scale-bar")

    def compose_scale_bars(self, image: Image.Image | None = None) -> Image.Image | None:
        """按当前交互状态生成全分辨率导出图像。"""

        source = image if image is not None else self._image
        if source is None:
            return None
        return draw_scale_bars(source, [state.overlay() for state in self._visible_scale_bars()])

    def _toggle_overview_scale(self) -> None:
        if "overview" not in self._scale_bars:
            return
        self._overview_scale_enabled = not self._overview_scale_enabled
        self._active_scale_key = "overview"
        self._update_scale_bar_buttons()
        self._render_scale_bars()
        self._notify_scale_bar_change(
            "10X scale bar enabled" if self._overview_scale_enabled else "10X scale bar hidden"
        )

    def _toggle_zoom_scale(self) -> None:
        zoom_states = [state for key, state in self._scale_bars.items() if key.startswith("zoom-")]
        if not zoom_states:
            return
        fitting = [state for state in zoom_states if self._scale_bar_limits(state) is not None]
        if not fitting:
            self._show_scale_bar_warning(
                "The default 10 µm scale bar does not fit inside any mapped ROI. "
                "Right-click after choosing a shorter length."
            )
            return
        self._zoom_scale_enabled = not self._zoom_scale_enabled
        self._active_scale_key = fitting[0].key
        self._update_scale_bar_buttons()
        self._render_scale_bars()
        suffix = "enabled" if self._zoom_scale_enabled else "hidden"
        skipped = len(zoom_states) - len(fitting)
        message = f"Zoom in scale bars {suffix}"
        if self._zoom_scale_enabled and skipped:
            message += f" · {skipped} ROI(s) too small for 10 µm"
        self._notify_scale_bar_change(message)

    def _update_scale_bar_buttons(self) -> None:
        self._set_toggle_button_style(self.overview_scale_button, self._overview_scale_enabled)
        self._set_toggle_button_style(self.zoom_scale_button, self._zoom_scale_enabled)

    def _visible_scale_bars(self) -> list[ScaleBarState]:
        states: list[ScaleBarState] = []
        if self._overview_scale_enabled and "overview" in self._scale_bars:
            states.append(self._scale_bars["overview"])
        if self._zoom_scale_enabled:
            states.extend(
                state
                for key, state in self._scale_bars.items()
                if key.startswith("zoom-") and self._scale_bar_limits(state) is not None
            )
        return states

    def _scale_bar_limits(
        self,
        state: ScaleBarState,
        length_um: float | None = None,
        line_width_px: int | None = None,
    ) -> tuple[float, float, float, float] | None:
        """计算中心 X 和 bar Y 的合法范围；放不下时明确返回 None。"""

        if self._image is None:
            return None
        length = state.length_um if length_um is None else length_um
        width = state.line_width_px if line_width_px is None else line_width_px
        length_px = length / state.pixel_size_um
        text_width, text_height = scale_bar_label_size(length, self._image.size)
        half_span = max(length_px, float(text_width)) / 2
        margin = max(4.0, min(self._image.size) / 400)
        gap = max(4.0, width + 1.0)
        left, top, right, bottom = state.bounds
        min_center = left + margin + half_span
        max_center = right - margin - half_span
        min_bar_y = top + margin + text_height + gap
        max_bar_y = bottom - margin - width / 2
        if min_center > max_center or min_bar_y > max_bar_y:
            return None
        return min_center, max_center, min_bar_y, max_bar_y

    def _clamp_scale_bar(self, state: ScaleBarState) -> bool:
        limits = self._scale_bar_limits(state)
        if limits is None:
            return False
        min_center, max_center, min_y, max_y = limits
        state.center_x_px = min(max(state.center_x_px, min_center), max_center)
        state.bar_y_px = min(max(state.bar_y_px, min_y), max_y)
        return True

    def _place_scale_bar(self, state: ScaleBarState, preset: str) -> bool:
        limits = self._scale_bar_limits(state)
        if limits is None:
            return False
        min_center, max_center, min_y, max_y = limits
        positions = {
            "bottom-left": (min_center, max_y),
            "bottom-right": (max_center, max_y),
            "top-left": (min_center, min_y),
            "top-right": (max_center, min_y),
            "center": ((min_center + max_center) / 2, (min_y + max_y) / 2),
        }
        state.center_x_px, state.bar_y_px = positions[preset]
        return True

    def _notify_scale_bar_change(self, message: str) -> None:
        if self._on_scale_bar_changed is not None:
            self._on_scale_bar_changed(message)

    def _show_scale_bar_warning(self, message: str) -> None:
        messagebox.showwarning("Scale bar does not fit", message, parent=self.winfo_toplevel())
        self._notify_scale_bar_change(message)

    def _show_empty(self) -> None:
        self.canvas.delete("all")
        self._empty_item = self.canvas.create_text(
            max(1, self.canvas.winfo_width()) / 2,
            max(1, self.canvas.winfo_height()) / 2,
            text="Select a 10X ND2 overview\nto render the scientific preview",
            fill="#82939D",
            font=("Segoe UI", 13),
            justify="center",
        )

    def set_image(self, image: Image.Image | None) -> None:
        self._image = image
        self._fit_mode = True
        if image is None:
            self.zoom_text.set("—")
            self._show_empty()
        else:
            self.fit_to_window()

    def fit_to_window(self) -> None:
        if self._image is None:
            return
        canvas_width = max(100, self.canvas.winfo_width())
        canvas_height = max(100, self.canvas.winfo_height())
        self._scale = min(
            (canvas_width - 28) / self._image.width,
            (canvas_height - 28) / self._image.height,
        )
        self._scale = max(0.01, self._scale)
        self._offset_x = canvas_width / 2
        self._offset_y = canvas_height / 2
        self._fit_mode = True
        self._render()

    def actual_size(self) -> None:
        if self._image is None:
            return
        self._scale = 1.0
        self._offset_x = self.canvas.winfo_width() / 2
        self._offset_y = self.canvas.winfo_height() / 2
        self._fit_mode = False
        self._render()

    def _render(self) -> None:
        if self._image is None:
            return
        width = max(1, round(self._image.width * self._scale))
        height = max(1, round(self._image.height * self._scale))
        resized = self._image.resize((width, height), Image.Resampling.LANCZOS)
        self._photo = ImageTk.PhotoImage(resized)
        if self._image_item is None or not self.canvas.find_withtag(self._image_item):
            self.canvas.delete("all")
            self._image_item = self.canvas.create_image(
                self._offset_x, self._offset_y, image=self._photo, anchor="center"
            )
        else:
            self.canvas.itemconfigure(self._image_item, image=self._photo)
            self.canvas.coords(self._image_item, self._offset_x, self._offset_y)
        self.zoom_text.set(f"{self._scale * 100:.0f}%")
        self._render_scale_bars()

    def _render_scale_bars(self) -> None:
        """把比例尺作为 Canvas overlay 绘制；坐标仍从原图像素实时换算。"""

        self.canvas.delete("scale-bar")
        if self._image is None:
            return
        image_left = self._offset_x - self._image.width * self._scale / 2
        image_top = self._offset_y - self._image.height * self._scale / 2
        font_px = max(8, round(scale_bar_font_size(self._image.size) * self._scale))

        for state in self._visible_scale_bars():
            overlay = state.overlay()
            length_px = overlay.length_px * self._scale
            center_x = image_left + state.center_x_px * self._scale
            bar_y = image_top + state.bar_y_px * self._scale
            left = center_x - length_px / 2
            right = center_x + length_px / 2
            width = max(1, round(state.line_width_px * self._scale))
            color = "#%02x%02x%02x" % SCALE_BAR_COLORS[state.color_name]
            red, green, blue = SCALE_BAR_COLORS[state.color_name]
            luminance = 0.2126 * red + 0.7152 * green + 0.0722 * blue
            contrast = "#000000" if luminance >= 140 else "#FFFFFF"
            tags = ("scale-bar", f"scale-bar:{state.key}")
            self.canvas.create_line(
                left,
                bar_y,
                right,
                bar_y,
                fill=contrast,
                width=width + 2,
                tags=tags,
            )
            self.canvas.create_line(
                left,
                bar_y,
                right,
                bar_y,
                fill=color,
                width=width,
                tags=tags,
            )
            gap = max(4, width + 1)
            self.canvas.create_text(
                center_x,
                bar_y - gap,
                text=format_scale_bar_length(state.length_um),
                fill=color,
                font=("Segoe UI Semibold", -font_px),
                anchor="s",
                tags=tags,
            )

    def _scale_key_at(self, canvas_x: int, canvas_y: int) -> str | None:
        """返回指针下最上层比例尺 key，用于拖动和右键菜单。"""

        for item in reversed(self.canvas.find_overlapping(
            canvas_x - 4, canvas_y - 4, canvas_x + 4, canvas_y + 4
        )):
            for tag in self.canvas.gettags(item):
                if tag.startswith("scale-bar:"):
                    return tag.split(":", 1)[1]
        return None

    def _on_resize(self, _event: tk.Event) -> None:
        if self._image is None:
            if self._empty_item is not None:
                self.canvas.coords(
                    self._empty_item,
                    max(1, self.canvas.winfo_width()) / 2,
                    max(1, self.canvas.winfo_height()) / 2,
                )
            return
        if not self._fit_mode:
            return
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(100, self.fit_to_window)

    def _image_coordinate(self, canvas_x: float, canvas_y: float) -> tuple[float, float] | None:
        if self._image is None:
            return None
        left = self._offset_x - self._image.width * self._scale / 2
        top = self._offset_y - self._image.height * self._scale / 2
        x = (canvas_x - left) / self._scale
        y = (canvas_y - top) / self._scale
        if 0 <= x < self._image.width and 0 <= y < self._image.height:
            return x, y
        return None

    def _on_wheel(self, event: tk.Event) -> None:
        if self._image is None:
            return
        factor = 1.15 if event.delta > 0 else 1 / 1.15
        self._zoom(factor, event.x, event.y)

    def _zoom_from_center(self, factor: float) -> None:
        """按钮/键盘缩放，以 viewer 中心作为稳定锚点。"""

        self._zoom(factor, self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2)

    def _zoom(self, factor: float, anchor_x: float, anchor_y: float) -> None:
        if self._image is None:
            return
        image_point = self._image_coordinate(anchor_x, anchor_y)
        old_scale = self._scale
        # 限制显示位图的最大边，避免高倍滚轮缩放瞬间分配数百 MB 内存。
        max_scale = min(8.0, 6000 / max(self._image.width, self._image.height))
        self._scale = min(max_scale, max(0.02, self._scale * factor))
        if self._scale == old_scale:
            return
        if image_point is not None:
            x, y = image_point
            self._offset_x = anchor_x - (x - self._image.width / 2) * self._scale
            self._offset_y = anchor_y - (y - self._image.height / 2) * self._scale
        self._fit_mode = False
        self._render()

    def _start_pan(self, event: tk.Event) -> None:
        if self._image is None:
            return
        self.canvas.focus_set()
        scale_key = self._scale_key_at(event.x, event.y)
        if scale_key is not None and scale_key in self._scale_bars:
            state = self._scale_bars[scale_key]
            self._active_scale_key = scale_key
            self._scale_drag_origin = (
                scale_key,
                event.x,
                event.y,
                state.center_x_px,
                state.bar_y_px,
            )
            self.canvas.configure(cursor="hand2")
            return
        self._drag_origin = (event.x, event.y, self._offset_x, self._offset_y)
        self.canvas.configure(cursor="fleur")

    def _pan(self, event: tk.Event) -> None:
        if self._scale_drag_origin is not None:
            key, start_x, start_y, center_x, bar_y = self._scale_drag_origin
            state = self._scale_bars.get(key)
            if state is None:
                return
            state.center_x_px = center_x + (event.x - start_x) / self._scale
            state.bar_y_px = bar_y + (event.y - start_y) / self._scale
            self._clamp_scale_bar(state)
            self._render_scale_bars()
            return
        if self._drag_origin is None:
            return
        start_x, start_y, image_x, image_y = self._drag_origin
        self._offset_x = image_x + event.x - start_x
        self._offset_y = image_y + event.y - start_y
        self._fit_mode = False
        if self._image_item is not None:
            self.canvas.coords(self._image_item, self._offset_x, self._offset_y)
        self._render_scale_bars()

    def _end_pan(self, _event: tk.Event) -> None:
        moved_scale = self._scale_drag_origin is not None
        self._scale_drag_origin = None
        self._drag_origin = None
        self.canvas.configure(cursor="crosshair")
        if moved_scale and self._active_scale_key in self._scale_bars:
            state = self._scale_bars[self._active_scale_key]
            self._notify_scale_bar_change(f"{state.scope_name} scale bar position updated")

    def _keyboard_pan(self, dx: float, dy: float) -> None:
        """为拖拽平移提供键盘等价操作。"""

        if self._image is None:
            return
        self._offset_x += dx
        self._offset_y += dy
        self._fit_mode = False
        if self._image_item is not None:
            self.canvas.coords(self._image_item, self._offset_x, self._offset_y)
        self._render_scale_bars()

    def _keyboard_move_scale(self, dx_px: float, dy_px: float) -> str:
        """Shift+方向键移动最后选中的比例尺，作为拖动的键盘替代。"""

        if self._active_scale_key is None:
            return "break"
        state = self._scale_bars.get(self._active_scale_key)
        if state is None or state not in self._visible_scale_bars():
            return "break"
        state.center_x_px += dx_px
        state.bar_y_px += dy_px
        self._clamp_scale_bar(state)
        self._render_scale_bars()
        self._notify_scale_bar_change(f"{state.scope_name} scale bar position updated")
        return "break"

    def _show_scale_bar_menu(self, event: tk.Event) -> str:
        """在指针下比例尺打开长度、颜色、宽度和位置预设菜单。"""

        key = self._scale_key_at(event.x, event.y)
        if key is None or key not in self._scale_bars:
            return "break"
        state = self._scale_bars[key]
        self._active_scale_key = key
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label=state.scope_name, state="disabled")
        menu.add_separator()

        length_menu = tk.Menu(menu, tearoff=False)
        length_var = tk.DoubleVar(menu, value=state.length_um)
        for length_um in SCALE_BAR_LENGTHS_UM:
            length_menu.add_radiobutton(
                label=format_scale_bar_length(length_um),
                variable=length_var,
                value=length_um,
                command=lambda value=length_um, target=key: self._set_scale_bar_length(
                    target, value
                ),
            )
        menu.add_cascade(label="Length", menu=length_menu)

        color_menu = tk.Menu(menu, tearoff=False)
        color_var = tk.StringVar(menu, value=state.color_name)
        for color_name in SCALE_BAR_COLORS:
            color_menu.add_radiobutton(
                label=color_name,
                variable=color_var,
                value=color_name,
                command=lambda value=color_name, target=key: self._set_scale_bar_color(
                    target, value
                ),
            )
        menu.add_cascade(label="Color", menu=color_menu)

        width_menu = tk.Menu(menu, tearoff=False)
        width_var = tk.IntVar(menu, value=state.line_width_px)
        for width_px in SCALE_BAR_WIDTHS_PX:
            width_menu.add_radiobutton(
                label=f"{width_px} px",
                variable=width_var,
                value=width_px,
                command=lambda value=width_px, target=key: self._set_scale_bar_width(
                    target, value
                ),
            )
        menu.add_cascade(label="Line width", menu=width_menu)

        position_menu = tk.Menu(menu, tearoff=False)
        for label, preset in (
            ("Top left", "top-left"),
            ("Top right", "top-right"),
            ("Bottom left", "bottom-left"),
            ("Bottom right", "bottom-right"),
            ("Center", "center"),
        ):
            position_menu.add_command(
                label=label,
                command=lambda value=preset, target=key: self._set_scale_bar_position(
                    target, value
                ),
            )
        menu.add_cascade(label="Position", menu=position_menu)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _set_scale_bar_length(self, key: str, length_um: float) -> None:
        state = self._scale_bars[key]
        if self._scale_bar_limits(state, length_um=length_um) is None:
            self._show_scale_bar_warning(
                f"{format_scale_bar_length(length_um)} does not fit inside "
                f"{state.scope_name}. Choose a shorter length."
            )
            return
        state.length_um = length_um
        self._clamp_scale_bar(state)
        self._render_scale_bars()
        self._notify_scale_bar_change(
            f"{state.scope_name} scale bar length set to {format_scale_bar_length(length_um)}"
        )

    def _set_scale_bar_color(self, key: str, color_name: str) -> None:
        state = self._scale_bars[key]
        state.color_name = color_name
        self._render_scale_bars()
        self._notify_scale_bar_change(f"{state.scope_name} scale bar color set to {color_name}")

    def _set_scale_bar_width(self, key: str, width_px: int) -> None:
        state = self._scale_bars[key]
        if self._scale_bar_limits(state, line_width_px=width_px) is None:
            self._show_scale_bar_warning(
                f"A {width_px} px scale bar does not fit inside {state.scope_name}."
            )
            return
        state.line_width_px = width_px
        self._clamp_scale_bar(state)
        self._render_scale_bars()
        self._notify_scale_bar_change(
            f"{state.scope_name} scale bar line width set to {width_px} px"
        )

    def _set_scale_bar_position(self, key: str, preset: str) -> None:
        state = self._scale_bars[key]
        if not self._place_scale_bar(state, preset):
            self._show_scale_bar_warning(
                f"The current scale bar does not fit inside {state.scope_name}."
            )
            return
        self._render_scale_bars()
        self._notify_scale_bar_change(f"{state.scope_name} scale bar position updated")

    def _cursor_motion(self, event: tk.Event) -> None:
        point = self._image_coordinate(event.x, event.y)
        self.cursor_text.set(
            f"x {point[0]:7.1f}   y {point[1]:7.1f}" if point is not None else "x —   y —"
        )
        if self._scale_drag_origin is None and self._drag_origin is None:
            self.canvas.configure(
                cursor="hand2" if self._scale_key_at(event.x, event.y) else "crosshair"
            )


class ND2ROIMapperApp:
    """ND2 ROI Mapper 主窗口；科学计算函数保持在核心模块中。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ND2 ROI Mapper")
        self.root.geometry("2560x1560")  # 默认窗口大小
        self.root.minsize(1040, 680)
        self.root.configure(background=COLORS["app_bg"])
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nd2-roi")
        self._events: queue.Queue[str] = queue.Queue()
        self._busy = False

        self.low_metadata: ND2Metadata | None = None
        self.low_image: Image.Image | None = None
        self.high_items: list[HighMagItem] = []
        self.roi_results: list[ROIResult] = []
        self.annotated_image: Image.Image | None = None
        self.last_export_path: Path | None = None

        self.microscope_var = tk.StringVar(value="upright")
        self.format_var = tk.StringVar(value="JPG")
        self.status_var = tk.StringVar(value="Ready · Select a 10X overview to begin")
        self.status_kind = "neutral"

        self._configure_styles()
        self._build_layout()
        self._refresh_state()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=COLORS["app_bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Viewer.TFrame", background=COLORS["viewer"])
        style.configure(
            "Section.TLabelframe",
            background=COLORS["panel"],
            bordercolor=COLORS["border"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "Section.TLabelframe.Label",
            background=COLORS["panel"],
            foreground=COLORS["muted"],
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "DropActive.TLabelframe",
            background=COLORS["accent_soft"],
            bordercolor=COLORS["accent"],
            relief="solid",
            borderwidth=2,
        )
        style.configure(
            "DropActive.TLabelframe.Label",
            background=COLORS["accent_soft"],
            foreground=COLORS["accent"],
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI", 9)
        )
        style.configure("Muted.TLabel", foreground=COLORS["muted"], font=("Segoe UI", 9))
        style.configure("Filename.TLabel", foreground=COLORS["text"], font=("Segoe UI Semibold", 9))
        style.configure(
            "StatusOK.TLabel", foreground=COLORS["success"], font=("Segoe UI Semibold", 9)
        )
        style.configure(
            "StatusWait.TLabel", foreground=COLORS["warning"], font=("Segoe UI Semibold", 9)
        )
        style.configure("MetaKey.TLabel", foreground=COLORS["muted"], font=("Segoe UI", 8))
        style.configure("MetaValue.TLabel", foreground=COLORS["text"], font=("Cascadia Mono", 8))
        # 文件名和通道名用比例字体，紧凑且更适合连续文本；数值仍使用等宽字体。
        style.configure("MetaTextValue.TLabel", foreground=COLORS["text"], font=("Segoe UI", 8))
        style.configure("TButton", padding=(10, 6), font=("Segoe UI Semibold", 9))
        style.configure(
            "Secondary.TButton",
            background="#E4EAED",
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
        )
        style.map("Secondary.TButton", background=[("active", "#D7E0E4"), ("disabled", "#EDF1F3")])
        style.configure(
            "Primary.TButton",
            background=COLORS["accent"],
            foreground="#FFFFFF",
            bordercolor=COLORS["accent"],
            padding=(14, 8),
        )
        style.map(
            "Primary.TButton",
            background=[("active", COLORS["accent_hover"]), ("disabled", COLORS["disabled"])],
            foreground=[("disabled", "#E7ECEF")],
        )
        style.configure(
            "TRadiobutton",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=("Segoe UI", 9),
        )
        style.map("TRadiobutton", background=[("active", COLORS["panel"])])
        style.configure("TCombobox", padding=4)
        style.configure(
            "Treeview",
            background="#FFFFFF",
            fieldbackground="#FFFFFF",
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            rowheight=25,
            font=("Segoe UI", 8),
        )
        style.configure(
            "Treeview.Heading",
            background=COLORS["panel_alt"],
            foreground=COLORS["muted"],
            font=("Segoe UI Semibold", 8),
        )
        style.map(
            "Treeview",
            background=[("selected", COLORS["accent_soft"])],
            foreground=[("selected", COLORS["text"])],
        )
        style.configure("TNotebook", background=COLORS["panel"], borderwidth=0)
        style.configure("TNotebook.Tab", padding=(10, 5), font=("Segoe UI Semibold", 8))
        style.map(
            "TNotebook.Tab", background=[("selected", COLORS["panel_alt"]), ("active", "#E4EAED")]
        )

    def _build_layout(self) -> None:
        header = tk.Frame(self.root, background=COLORS["header"], height=62)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        title_block = tk.Frame(header, background=COLORS["header"])
        title_block.pack(side="left", padx=18, pady=10)
        tk.Label(
            title_block,
            text="ND2 ROI Mapper",
            background=COLORS["header"],
            foreground=COLORS["inverse"],
            font=("Segoe UI Semibold", 16),
        ).pack(anchor="w")
        tk.Label(
            title_block,
            text="Stage-coordinate mapping for high-magnification fields",
            background=COLORS["header"],
            foreground=COLORS["inverse_muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="w")
        self.header_state = tk.Label(
            header,
            text="INPUT REQUIRED",
            background="#263640",
            foreground=COLORS["inverse_muted"],
            font=("Segoe UI Semibold", 8),
            padx=11,
            pady=5,
        )
        self.header_state.pack(side="right", padx=18)

        paned = ttk.Panedwindow(self.root, orient="horizontal")
        paned.grid(row=1, column=0, sticky="nsew")
        self.sidebar = ScrollablePanel(paned)
        viewer_shell = ttk.Frame(paned, style="Viewer.TFrame", padding=(1, 1, 1, 0))
        paned.add(self.sidebar, weight=0)
        paned.add(viewer_shell, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)

        self._build_sidebar(self.sidebar.content)
        self.viewer = ImageViewer(viewer_shell, self._scale_bar_changed)
        self.viewer.pack(fill="both", expand=True)

        status = tk.Frame(self.root, background="#DCE3E7", height=28)
        status.grid(row=2, column=0, sticky="ew")
        status.grid_propagate(False)
        self.status_dot = tk.Label(
            status, text="●", background="#DCE3E7", foreground=COLORS["muted"], font=("Segoe UI", 8)
        )
        self.status_dot.pack(side="left", padx=(12, 6))
        self.status_label = tk.Label(
            status,
            textvariable=self.status_var,
            background="#DCE3E7",
            foreground=COLORS["muted"],
            font=("Segoe UI", 8),
            anchor="w",
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        tk.Label(
            status,
            text=(
                "Wheel / + −: zoom   Drag / arrows: pan   "
                "Scale bar: drag · right-click · Shift+arrows"
            ),
            background="#DCE3E7",
            foreground=COLORS["muted"],
            font=("Segoe UI", 8),
        ).pack(side="right", padx=12)

    def _build_sidebar(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        ttk.Label(parent, text="WORKFLOW", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.step_labels: list[tk.Label] = []
        step_bar = tk.Frame(parent, background=COLORS["panel"])
        step_bar.grid(row=1, column=0, sticky="ew", pady=(5, 12))
        for index, text in enumerate(("Overview", "High-mag", "Map", "Export"), start=1):
            cell = tk.Frame(step_bar, background=COLORS["panel"])
            cell.pack(side="left", expand=True, fill="x")
            badge = tk.Label(
                cell,
                text=str(index),
                width=2,
                background="#DDE4E8",
                foreground=COLORS["muted"],
                font=("Segoe UI Semibold", 8),
                pady=2,
            )
            badge.pack()
            tk.Label(
                cell,
                text=text,
                background=COLORS["panel"],
                foreground=COLORS["muted"],
                font=("Segoe UI", 7),
            ).pack(pady=(2, 0))
            self.step_labels.append(badge)

        self.overview_drop_area = ttk.Labelframe(
            parent, text="  10X OVERVIEW  ", style="Section.TLabelframe", padding=10
        )
        self.overview_drop_area.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.overview_drop_area.columnconfigure(0, weight=1)
        low_buttons = ttk.Frame(self.overview_drop_area, style="Panel.TFrame")
        low_buttons.grid(row=0, column=0, sticky="ew")
        low_buttons.columnconfigure(0, weight=1)
        self.low_button = ttk.Button(
            low_buttons,
            text="Select or Drop 10X ND2",
            style="Secondary.TButton",
            command=self.select_low,
        )
        self.low_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.remove_low_button = ttk.Button(
            low_buttons,
            text="Remove",
            width=8,
            style="Secondary.TButton",
            command=self.remove_low,
        )
        self.remove_low_button.grid(row=0, column=1)
        self.low_name = ttk.Label(
            self.overview_drop_area,
            text="No overview selected",
            style="Filename.TLabel",
            wraplength=285,
        )
        self.low_name.grid(row=1, column=0, sticky="w", pady=(8, 2))
        self.low_state = ttk.Label(
            self.overview_drop_area,
            text="Click or drop one .nd2 file",
            style="Muted.TLabel",
        )
        self.low_state.grid(row=2, column=0, sticky="w")

        self.high_drop_area = ttk.Labelframe(
            parent, text="  HIGH MAGNIFICATION  ", style="Section.TLabelframe", padding=10
        )
        self.high_drop_area.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        self.high_drop_area.columnconfigure(0, weight=1)
        buttons = ttk.Frame(self.high_drop_area, style="Panel.TFrame")
        buttons.grid(row=0, column=0, sticky="ew")
        buttons.columnconfigure(0, weight=1)
        self.add_high_button = ttk.Button(
            buttons,
            text="Add/Drop ND2",
            style="Secondary.TButton",
            command=self.select_high,
        )
        self.add_high_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.remove_high_button = ttk.Button(
            buttons,
            text="Remove",
            width=8,
            style="Secondary.TButton",
            command=self.remove_selected_high,
        )
        self.remove_high_button.grid(row=0, column=1)
        self.high_tree = ttk.Treeview(
            self.high_drop_area,
            columns=("index", "file", "objective", "zoom"),
            show="headings",
            height=5,
            selectmode="extended",
        )
        self.high_tree.heading("index", text="#")
        self.high_tree.heading("file", text="File")
        self.high_tree.heading("objective", text="Obj.")
        self.high_tree.heading("zoom", text="Zoom")
        self.high_tree.column("index", width=28, stretch=False, anchor="center")
        self.high_tree.column("file", width=176, stretch=True)
        self.high_tree.column("objective", width=48, stretch=False, anchor="center")
        self.high_tree.column("zoom", width=60, stretch=False, anchor="center")
        self.high_tree.grid(row=1, column=0, sticky="ew", pady=(8, 4))
        self.high_tree.bind("<<TreeviewSelect>>", self._high_selection_changed)
        self.high_summary = ttk.Label(
            self.high_drop_area,
            text="Click Add or drop one or more .nd2 files",
            style="Muted.TLabel",
        )
        self.high_summary.grid(row=2, column=0, sticky="w")
        self._register_drop_targets(
            self.overview_drop_area,
            (
                self.overview_drop_area,
                low_buttons,
                self.low_button,
                self.remove_low_button,
                self.low_name,
                self.low_state,
            ),
            self._drop_low,
        )
        self._register_drop_targets(
            self.high_drop_area,
            (
                self.high_drop_area,
                buttons,
                self.add_high_button,
                self.remove_high_button,
                self.high_tree,
                self.high_summary,
            ),
            self._drop_high,
        )

        mapping = ttk.Labelframe(
            parent, text="  MAPPING  ", style="Section.TLabelframe", padding=10
        )
        mapping.grid(row=4, column=0, sticky="ew", pady=(0, 10))
        mapping.columnconfigure(0, weight=1)
        ttk.Label(mapping, text="Microscope orientation", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        radios = ttk.Frame(mapping, style="Panel.TFrame")
        radios.grid(row=1, column=0, sticky="ew", pady=(4, 1))
        self.upright_radio = ttk.Radiobutton(
            radios,
            text="Upright  X +1 · Y −1",
            variable=self.microscope_var,
            value="upright",
            command=self.mapping_setting_changed,
        )
        self.upright_radio.pack(anchor="w")
        self.inverted_radio = ttk.Radiobutton(
            radios,
            text="Inverted  X −1 · Y +1",
            variable=self.microscope_var,
            value="inverted",
            command=self.mapping_setting_changed,
        )
        self.inverted_radio.pack(anchor="w", pady=(2, 6))
        self.map_button = ttk.Button(
            mapping, text="Calculate & Preview ROIs", style="Primary.TButton", command=self.map_rois
        )
        self.map_button.grid(row=2, column=0, sticky="ew")

        metadata_group = ttk.Labelframe(
            parent, text="  ACQUISITION METADATA  ", style="Section.TLabelframe", padding=4
        )
        metadata_group.grid(row=5, column=0, sticky="ew", pady=(0, 10))
        notebook = ttk.Notebook(metadata_group)
        notebook.pack(fill="x", expand=True)
        self.low_metadata_panel = MetadataPanel(notebook)
        self.high_metadata_panel = MetadataPanel(notebook)
        notebook.add(self.low_metadata_panel, text="10X overview")
        notebook.add(self.high_metadata_panel, text="Selected ROI")

        export = ttk.Labelframe(parent, text="  EXPORT  ", style="Section.TLabelframe", padding=10)
        export.grid(row=6, column=0, sticky="ew")
        export.columnconfigure(1, weight=1)
        ttk.Label(export, text="Format", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.format_combo = ttk.Combobox(
            export,
            textvariable=self.format_var,
            values=("JPG", "PNG"),
            state="readonly",
            width=8,
        )
        self.format_combo.grid(row=0, column=1, sticky="w")
        self.export_button = ttk.Button(
            export,
            text="Export Annotated Image",
            style="Primary.TButton",
            command=self.export_image,
        )
        self.export_button.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(9, 0))

    def _register_drop_targets(
        self,
        area: ttk.Labelframe,
        widgets: tuple[tk.Misc, ...],
        callback: Callable[[object], str],
    ) -> None:
        """把一个输入区及其子控件注册为 Windows/macOS/Linux 文件拖放目标。"""

        for widget in widgets:
            widget.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            widget.dnd_bind(  # type: ignore[attr-defined]
                "<<DropEnter>>",
                lambda _event, target=area: self._drop_enter(target),
            )
            widget.dnd_bind(  # type: ignore[attr-defined]
                "<<DropLeave>>",
                lambda _event, target=area: self._drop_leave(target),
            )
            widget.dnd_bind(  # type: ignore[attr-defined]
                "<<Drop>>",
                lambda event, target=area, handler=callback: self._finish_drop(
                    target, handler, event
                ),
            )

    @staticmethod
    def _drop_enter(area: ttk.Labelframe) -> str:
        """拖入时用边框和标题颜色明确显示可放置区域。"""

        area.configure(style="DropActive.TLabelframe")
        return COPY

    @staticmethod
    def _drop_leave(area: ttk.Labelframe) -> str:
        area.configure(style="Section.TLabelframe")
        return COPY

    def _finish_drop(
        self,
        area: ttk.Labelframe,
        callback: Callable[[object], str],
        event: object,
    ) -> str:
        area.configure(style="Section.TLabelframe")
        return callback(event)

    def _dropped_nd2_paths(self, event: object) -> list[Path] | None:
        """安全解析系统拖入的 Tcl 文件列表，并拒绝目录或非 ND2 文件。"""

        data = str(getattr(event, "data", ""))
        try:
            raw_paths = self.root.tk.splitlist(data)
        except tk.TclError:
            raw_paths = ()
        paths = [Path(raw_path) for raw_path in raw_paths]
        invalid = [path for path in paths if not path.is_file() or path.suffix.lower() != ".nd2"]
        if not paths or invalid:
            details = (
                "\n".join(f"• {path}" for path in invalid) if invalid else "No files were detected."
            )
            message = "Only Nikon .nd2 files can be dropped here.\n\n" f"Rejected:\n{details}"
            self._set_status("Drop rejected · select valid .nd2 file(s)", "error")
            messagebox.showwarning("Invalid file drop", message, parent=self.root)
            return None
        return paths

    def _drop_low(self, event: object) -> str:
        """10X 区只接受单个 ND2，避免不明确地选择其中一个文件。"""

        if self._busy:
            self._set_status("Please wait for the current ND2 operation to finish", "warning")
            return COPY
        paths = self._dropped_nd2_paths(event)
        if paths is None:
            return COPY
        if len(paths) != 1:
            self._set_status("10X drop rejected · drop exactly one .nd2 file", "error")
            messagebox.showwarning(
                "10X overview requires one file",
                "Drop exactly one 10X .nd2 file into the 10X Overview area.",
                parent=self.root,
            )
            return COPY
        self.load_low_path(paths[0])
        return COPY

    def _drop_high(self, event: object) -> str:
        """高倍区接受单个或多个 ND2，并沿用现有的去重和后台读取逻辑。"""

        if self._busy:
            self._set_status("Please wait for the current ND2 operation to finish", "warning")
            return COPY
        if self.low_metadata is None or self.low_image is None:
            self._set_status(
                "Load the 10X overview before adding high-magnification files", "warning"
            )
            messagebox.showwarning(
                "10X overview required",
                "Select or drop the 10X overview before adding high-magnification ND2 files.",
                parent=self.root,
            )
            return COPY
        paths = self._dropped_nd2_paths(event)
        if paths is not None:
            self.load_high_paths(paths)
        return COPY

    def _set_status(self, message: str, kind: str = "neutral") -> None:
        self.status_var.set(message)
        self.status_kind = kind
        colors = {
            "neutral": COLORS["muted"],
            "busy": COLORS["warning"],
            "success": COLORS["success"],
            "warning": COLORS["warning"],
            "error": COLORS["error"],
        }
        color = colors.get(kind, COLORS["muted"])
        self.status_dot.configure(foreground=color)
        self.status_label.configure(
            foreground=color if kind in {"error", "warning"} else COLORS["muted"]
        )

    def _scale_bar_changed(self, message: str) -> None:
        """比例尺变更只使上次导出失效，不重新计算任何 ROI 几何。"""

        self.last_export_path = None
        self._set_status(message + " · export again to save the change", "neutral")
        self._refresh_state()

    def _refresh_state(self) -> None:
        has_low = self.low_metadata is not None and self.low_image is not None
        has_high = bool(self.high_items)
        has_map = self.annotated_image is not None
        normal = "normal" if not self._busy else "disabled"
        self.low_button.configure(state=normal)
        self.remove_low_button.configure(
            state="normal" if has_low and not self._busy else "disabled"
        )
        self.add_high_button.configure(state="normal" if has_low and not self._busy else "disabled")
        self.remove_high_button.configure(
            state=(
                "normal"
                if has_high and self.high_tree.selection() and not self._busy
                else "disabled"
            )
        )
        self.map_button.configure(
            state="normal" if has_low and has_high and not self._busy else "disabled"
        )
        self.export_button.configure(state="normal" if has_map and not self._busy else "disabled")
        radio_state = "normal" if not self._busy else "disabled"
        self.upright_radio.configure(state=radio_state)
        self.inverted_radio.configure(state=radio_state)
        self.format_combo.configure(state="readonly" if not self._busy else "disabled")
        self.viewer.set_scale_bar_control_state(has_low, has_map, self._busy)

        completed = [has_low, has_high, has_map, self.last_export_path is not None]
        current = 0 if not has_low else 1 if not has_high else 2 if not has_map else 3
        for index, badge in enumerate(self.step_labels):
            if completed[index]:
                badge.configure(background=COLORS["success"], foreground="#FFFFFF")
            elif index == current:
                badge.configure(background=COLORS["accent"], foreground="#FFFFFF")
            else:
                badge.configure(background="#DDE4E8", foreground=COLORS["muted"])
        if self._busy:
            self.header_state.configure(
                text="PROCESSING", background="#6F4C20", foreground="#FFF1D7"
            )
        elif self.last_export_path is not None:
            self.header_state.configure(
                text="EXPORT COMPLETE", background="#1F684B", foreground="#E4FFF3"
            )
        elif has_map:
            self.header_state.configure(
                text="READY TO EXPORT", background="#1F684B", foreground="#E4FFF3"
            )
        elif has_low or has_high:
            self.header_state.configure(
                text="IN PROGRESS", background="#264955", foreground="#DDF6F8"
            )
        else:
            self.header_state.configure(
                text="INPUT REQUIRED", background="#263640", foreground=COLORS["inverse_muted"]
            )

    def _run_background(
        self,
        initial_status: str,
        work: Callable[[Callable[[str], None]], T],
        on_success: Callable[[T], None],
    ) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_status(initial_status, "busy")
        self._refresh_state()

        def report(message: str) -> None:
            self._events.put(message)

        future = self._executor.submit(work, report)
        self._poll_future(future, on_success)

    def _poll_future(self, future: Future[T], on_success: Callable[[T], None]) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            self._set_status(event, "busy")
        if not future.done():
            self.root.after(60, lambda: self._poll_future(future, on_success))
            return
        self._busy = False
        try:
            result = future.result()
        except Exception as exc:
            traceback.print_exc()
            message = self._friendly_error(exc)
            self._set_status(message.splitlines()[0], "error")
            self._refresh_state()
            messagebox.showerror("ND2 processing failed", message, parent=self.root)
            return
        on_success(result)
        self._refresh_state()

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        lower = text.lower()
        if isinstance(exc, MetadataError) and ("stage x/y" in lower or "stage" in lower):
            return (
                "Unable to determine XY stage coordinates from this ND2 file.\n\n"
                f"Details: {text}\n\n"
                "Run the file in command-line mode for full metadata diagnostics."
            )
        if isinstance(exc, MetadataError) and ("pixel" in lower or "physical" in lower):
            return (
                "Unable to determine physical pixel size from this ND2 file.\n\n"
                f"Details: {text}\n\n"
                "Run the file in command-line mode for full metadata diagnostics."
            )
        return (
            f"The operation could not be completed.\n\nDetails: {text}\n\n"
            "Run the application in command-line mode for additional diagnostics."
        )

    def select_low(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Select 10X overview ND2",
            filetypes=[("Nikon ND2", "*.nd2"), ("All files", "*.*")],
        )
        if path:
            self.load_low_path(Path(path))

    def load_low_path(self, path: Path) -> None:
        """加载入口单独暴露，方便 UI smoke test 使用真实 ND2。"""

        self.low_name.configure(text=path.name)
        self.low_state.configure(text="Reading metadata…", style="StatusWait.TLabel")

        def work(report: Callable[[str], None]) -> tuple[ND2Metadata, Image.Image]:
            report(f"Reading metadata · {path.name}")
            metadata = read_nd2_metadata(path)
            _print_metadata("10X / low magnification", metadata)
            report(f"Rendering 10X preview · {path.name}")
            image = read_nd2_image(path)
            return metadata, image

        def accept(result: tuple[ND2Metadata, Image.Image]) -> None:
            self.low_metadata, self.low_image = result
            self.high_items.clear()
            self._rebuild_high_tree()
            self.low_metadata_panel.show_metadata(self.low_metadata)
            self.high_metadata_panel.show_metadata(None)
            self.low_name.configure(text=path.name)
            self.low_state.configure(
                text="Metadata loaded · Preview ready", style="StatusOK.TLabel"
            )
            self._invalidate_mapping(reset_to_base=False)
            self.viewer.set_image(self.low_image)
            self.viewer.configure_overview_scale_bar(self.low_metadata.pixel_size_x_um)
            self._set_status(f"10X overview ready · {path.name}", "success")

        self._run_background("Reading 10X metadata…", work, accept)

    def remove_low(self) -> None:
        """移除 10X overview，并清空所有依赖该底图的 ROI 状态。"""

        if self._busy or self.low_metadata is None:
            return

        self.low_metadata = None
        self.low_image = None
        self.high_items.clear()
        self.low_metadata_panel.show_metadata(None)
        self.high_metadata_panel.show_metadata(None)
        self.low_name.configure(text="No overview selected")
        self.low_state.configure(text="Click or drop one .nd2 file", style="Muted.TLabel")
        self._rebuild_high_tree()
        self._invalidate_mapping(reset_to_base=False)
        self.viewer.clear_scale_bars()
        self.viewer.set_image(None)
        self._set_status("10X overview removed · dependent ROI list cleared", "neutral")
        self._refresh_state()

    def select_high(self) -> None:
        selected = filedialog.askopenfilenames(
            parent=self.root,
            title="Add high-magnification ND2 files",
            filetypes=[("Nikon ND2", "*.nd2"), ("All files", "*.*")],
        )
        if selected:
            self.load_high_paths([Path(path) for path in selected])

    def load_high_paths(self, paths: list[Path]) -> None:
        known = {item.path.resolve() for item in self.high_items}
        new_paths = [path for path in paths if path.resolve() not in known]
        if not new_paths:
            self._set_status(
                "All selected high-magnification files are already in the list", "warning"
            )
            return

        def work(report: Callable[[str], None]) -> list[HighMagItem]:
            items: list[HighMagItem] = []
            for index, path in enumerate(new_paths, start=1):
                report(f"Reading high-mag metadata {index}/{len(new_paths)} · {path.name}")
                metadata = read_nd2_metadata(path)
                _print_metadata(f"High magnification [{index}/{len(new_paths)}]", metadata)
                items.append(HighMagItem(path=path, metadata=metadata))
            return items

        def accept(items: list[HighMagItem]) -> None:
            self.high_items.extend(items)
            self._rebuild_high_tree(select_last=True)
            self._invalidate_mapping()
            self._set_status(
                f"Metadata loaded · {len(self.high_items)} high-magnification file(s)", "success"
            )

        self._run_background("Reading high-magnification metadata…", work, accept)

    def _rebuild_high_tree(self, select_last: bool = False) -> None:
        self.high_tree.delete(*self.high_tree.get_children())
        for index, item in enumerate(self.high_items, start=1):
            objective = item.metadata.objective_label or "—"
            zoom = f"{item.metadata.scan_zoom:g}×" if item.metadata.scan_zoom is not None else "—"
            # 列表用 ROI 编号建立对应关系；颜色只用于图像 overlay，避免低对比文字。
            self.high_tree.insert(
                "",
                "end",
                iid=f"high-{index - 1}",
                values=(f"{index:02d}", item.path.name, objective, zoom),
            )
        count = len(self.high_items)
        self.high_summary.configure(
            text=(
                f"{count} file{'s' if count != 1 else ''} · select a row to inspect metadata"
                if count
                else "Click Add or drop one or more .nd2 files"
            )
        )
        if select_last and count:
            iid = f"high-{count - 1}"
            self.high_tree.selection_set(iid)
            self.high_tree.see(iid)
            self.high_metadata_panel.show_metadata(self.high_items[-1].metadata)
        self._refresh_state()

    def _high_selection_changed(self, _event: tk.Event | None = None) -> None:
        selection = self.high_tree.selection()
        if selection:
            index = int(selection[0].split("-")[-1])
            if 0 <= index < len(self.high_items):
                self.high_metadata_panel.show_metadata(self.high_items[index].metadata)
        else:
            self.high_metadata_panel.show_metadata(None)
        self._refresh_state()

    def remove_selected_high(self) -> None:
        indices = sorted(
            (int(iid.split("-")[-1]) for iid in self.high_tree.selection()), reverse=True
        )
        for index in indices:
            if 0 <= index < len(self.high_items):
                del self.high_items[index]
        self._rebuild_high_tree(select_last=bool(self.high_items))
        self._invalidate_mapping()
        self._set_status("High-magnification list updated · recalculate the preview", "neutral")

    def mapping_setting_changed(self) -> None:
        if self.annotated_image is not None:
            self._invalidate_mapping()
            self._set_status("Microscope orientation changed · recalculate the preview", "warning")

    def _invalidate_mapping(self, reset_to_base: bool = True) -> None:
        self.roi_results = []
        self.annotated_image = None
        self.last_export_path = None
        self.viewer.clear_zoom_scale_bars()
        if reset_to_base and self.low_image is not None:
            self.viewer.set_image(self.low_image)
        self._refresh_state()

    def map_rois(self) -> None:
        if self.low_metadata is None or self.low_image is None or not self.high_items:
            return
        low = self.low_metadata
        base = self.low_image
        high_items = list(self.high_items)
        self.last_export_path = None
        microscope = self.microscope_var.get()
        x_sign, y_sign = MICROSCOPE_SIGNS[microscope]

        def work(report: Callable[[str], None]) -> tuple[Image.Image, list[ROIResult], list[str]]:
            drawing_items: list[tuple[ROIResult, str, tuple[int, int, int]]] = []
            results: list[ROIResult] = []
            warnings: list[str] = []
            for index, item in enumerate(high_items, start=1):
                report(f"Calculating ROI {index}/{len(high_items)} · {item.path.name}")
                roi = calculate_roi_position(low, item.metadata, x_sign=x_sign, y_sign=y_sign)
                _print_result(roi, x_sign=x_sign, y_sign=y_sign)
                objective = item.metadata.objective_label or "High-mag"
                zoom = (
                    f"{item.metadata.scan_zoom:g}×"
                    if item.metadata.scan_zoom is not None
                    else "N/A"
                )
                label = f"{item.path.name}\nObjective {objective} | Zoom {zoom}"
                drawing_items.append((roi, label, ROI_COLORS[(index - 1) % len(ROI_COLORS)]))
                results.append(roi)
                left, top, right, bottom = roi.box
                if left < 0 or top < 0 or right > low.width_px or bottom > low.height_px:
                    warnings.append(f"ROI {index:02d} extends beyond the 10X image")
            report("Rendering annotated preview…")
            return draw_rois(base, drawing_items), results, warnings

        def accept(result: tuple[Image.Image, list[ROIResult], list[str]]) -> None:
            self.annotated_image, self.roi_results, warnings = result
            self.viewer.set_image(self.annotated_image)
            self.viewer.configure_zoom_scale_bars(
                self.roi_results,
                low.pixel_size_x_um,
            )
            if warnings:
                self._set_status("Preview ready with warning · " + "; ".join(warnings), "warning")
            else:
                self._set_status(
                    f"Preview ready · {len(self.roi_results)} ROI(s) mapped", "success"
                )

        self._run_background("Calculating stage-coordinate mapping…", work, accept)

    def export_image(self) -> None:
        if self.annotated_image is None or self.low_metadata is None:
            return
        extension = "jpg" if self.format_var.get() == "JPG" else "png"
        suggested = _default_output(str(self.low_metadata.path), extension)
        output = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export annotated image",
            initialdir=str(suggested.parent),
            initialfile=suggested.name,
            defaultextension=f".{extension}",
            filetypes=[(self.format_var.get(), f"*.{extension}")],
        )
        if not output:
            return
        composed = self.viewer.compose_scale_bars(self.annotated_image)
        if composed is None:
            return
        image = composed
        output_path = Path(output)

        def work(report: Callable[[str], None]) -> Path:
            report(f"Exporting {output_path.name}…")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            kwargs = {"quality": 95} if output_path.suffix.lower() in {".jpg", ".jpeg"} else {}
            image.save(output_path, **kwargs)
            return output_path.resolve()

        def accept(result: Path) -> None:
            self.last_export_path = result
            self._set_status(f"Export complete · {result}", "success")
            messagebox.showinfo(
                "Export complete", f"Annotated image saved to:\n{result}", parent=self.root
            )

        self._run_background("Exporting annotated image…", work, accept)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def _enable_windows_high_dpi() -> None:
    """启用系统 DPI 感知；失败时回退到 Tk 默认缩放。"""

    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


def launch_app() -> None:
    """创建并运行 ND2 ROI Mapper。"""

    _enable_windows_high_dpi()
    root = TkinterDnD.Tk()
    ND2ROIMapperApp(root)
    root.mainloop()
