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
    _default_output,
    _print_metadata,
    _print_result,
    calculate_roi_position,
    draw_rois,
    read_nd2_image,
    read_nd2_metadata,
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


@dataclass(frozen=True)
class HighMagItem:
    """UI 中一条高倍文件记录；metadata 本身仍是核心模块的数据结构。"""

    path: Path
    metadata: ND2Metadata


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

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, style="Viewer.TFrame")
        self._image: Image.Image | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._image_item: int | None = None
        self._empty_item: int | None = None
        self._scale = 1.0
        self._offset_x = 0.0
        self._offset_y = 0.0
        self._fit_mode = True
        self._drag_origin: tuple[int, int, float, float] | None = None
        self._resize_job: str | None = None

        toolbar = tk.Frame(self, background=COLORS["viewer_toolbar"], height=44)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_propagate(False)
        tk.Label(
            toolbar,
            text="10X VIEWER",
            background=COLORS["viewer_toolbar"],
            foreground=COLORS["inverse_muted"],
            font=("Segoe UI Semibold", 9),
        ).pack(side="left", padx=(14, 18))
        self._toolbar_button(toolbar, "Fit", self.fit_to_window).pack(
            side="left", padx=(0, 6), pady=7
        )
        self._toolbar_button(toolbar, "100%", self.actual_size).pack(side="left", pady=7)
        self._toolbar_button(toolbar, "−", lambda: self._zoom_from_center(1 / 1.15)).pack(
            side="left", padx=(6, 0), pady=7
        )
        self._toolbar_button(toolbar, "+", lambda: self._zoom_from_center(1.15)).pack(
            side="left", padx=(6, 0), pady=7
        )
        self.zoom_text = tk.StringVar(value="—")
        self.cursor_text = tk.StringVar(value="x —   y —")
        tk.Label(
            toolbar,
            textvariable=self.cursor_text,
            background=COLORS["viewer_toolbar"],
            foreground=COLORS["inverse_muted"],
            font=("Cascadia Mono", 9),
        ).pack(side="right", padx=(8, 14))
        tk.Label(
            toolbar,
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
        self.canvas.bind("<Motion>", self._cursor_motion)
        self.canvas.bind("<Leave>", lambda _event: self.cursor_text.set("x —   y —"))
        self.canvas.bind("<KeyPress-plus>", lambda _event: self._zoom_from_center(1.15))
        self.canvas.bind("<KeyPress-equal>", lambda _event: self._zoom_from_center(1.15))
        self.canvas.bind("<KeyPress-minus>", lambda _event: self._zoom_from_center(1 / 1.15))
        self.canvas.bind("<KeyPress-Left>", lambda _event: self._keyboard_pan(40, 0))
        self.canvas.bind("<KeyPress-Right>", lambda _event: self._keyboard_pan(-40, 0))
        self.canvas.bind("<KeyPress-Up>", lambda _event: self._keyboard_pan(0, 40))
        self.canvas.bind("<KeyPress-Down>", lambda _event: self._keyboard_pan(0, -40))
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
        self._drag_origin = (event.x, event.y, self._offset_x, self._offset_y)
        self.canvas.configure(cursor="fleur")

    def _pan(self, event: tk.Event) -> None:
        if self._drag_origin is None:
            return
        start_x, start_y, image_x, image_y = self._drag_origin
        self._offset_x = image_x + event.x - start_x
        self._offset_y = image_y + event.y - start_y
        self._fit_mode = False
        if self._image_item is not None:
            self.canvas.coords(self._image_item, self._offset_x, self._offset_y)

    def _end_pan(self, _event: tk.Event) -> None:
        self._drag_origin = None
        self.canvas.configure(cursor="crosshair")

    def _keyboard_pan(self, dx: float, dy: float) -> None:
        """为拖拽平移提供键盘等价操作。"""

        if self._image is None:
            return
        self._offset_x += dx
        self._offset_y += dy
        self._fit_mode = False
        if self._image_item is not None:
            self.canvas.coords(self._image_item, self._offset_x, self._offset_y)

    def _cursor_motion(self, event: tk.Event) -> None:
        point = self._image_coordinate(event.x, event.y)
        self.cursor_text.set(
            f"x {point[0]:7.1f}   y {point[1]:7.1f}" if point is not None else "x —   y —"
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
        self.viewer = ImageViewer(viewer_shell)
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
            text="Wheel / + −: zoom   Drag / arrow keys: pan",
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
        image = self.annotated_image.copy()
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
