"""根据 Nikon ND2 中的载物台坐标，把高倍视野框画到低倍图像中。

重要假设：本程序把 ``frame_metadata`` 中的 stage X/Y 当作图像中心的物理坐标。
如果实际 ND2 中记录的是左上角、扫描起点或经过其他变换的坐标，那么仅修改正负号
仍然无法得到正确结果。排查时请重点查看本文件顶部的校准参数，以及
``calculate_roi_position()`` 中逐行标出的坐标公式。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import nd2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


# ============================================================================
# 第一部分：显微镜坐标校准参数（坐标不对时，优先检查这里）
# ============================================================================

# 当前程序没有自动读取/应用 cameraTransformationMatrix，也没有处理 X/Y 轴互换。
# 它使用的简化公式是：
#     图像像素偏移 = 载物台物理偏移 / 10X像素尺寸 × 方向符号
#
# X_SIGN 和 Y_SIGN 只能取 1 或 -1：
#   1  = stage 数值增大时，图像像素坐标也增大
#  -1  = stage 数值增大时，图像像素坐标反而减小
#
# 注意：图像坐标原点在左上角，像素 X 向右增加，像素 Y 向下增加。
# X_SIGN/Y_SIGN 仅作为非 GUI/旧接口的默认值，默认采用正置显微镜。
# GUI 会根据显微镜类型同时覆盖两个方向。
X_SIGN = 1
Y_SIGN = -1

MICROSCOPE_SIGNS: dict[str, tuple[int, int]] = {
    # 值的顺序是 (X_SIGN, Y_SIGN)。
    "upright": (1, -1),
    "inverted": (-1, 1),
}

# 不同物镜切换后可能存在固定的 XY 偏心（parcentric offset）。
# 字典中的值是 (X偏移, Y偏移)，单位均为 µm；它们会先加到高倍 stage 坐标上。
OBJECTIVE_OFFSET: dict[str, tuple[float, float]] = {
    "60X": (355.0, 145.0),
    "100X": (-12.0, 3.0),
}


# ============================================================================
# 第二部分：程序内部使用的数据结构
# ============================================================================


@dataclass(frozen=True)
class ND2Metadata:
    """从一个 ND2 文件中整理出的、参与定位和显示的字段。

    所有物理长度统一使用 µm；像素相关数值使用 px。
    """

    # ND2 文件路径。
    path: Path
    # frame_metadata(0) 中读取的载物台坐标。
    stage_x_um: float
    stage_y_um: float
    # Z 坐标仅用于 UI/诊断展示，不参与 XY ROI 映射；metadata 缺失时为 None。
    stage_z_um: float | None
    # X/Y 方向各自的物理像素尺寸，单位 µm/px；不强制假设二者相等。
    pixel_size_x_um: float
    pixel_size_y_um: float
    # 原始图像宽度和高度。
    width_px: int
    height_px: int
    # 物镜倍率和名称；metadata 缺失时允许为 None。
    magnification: float | None
    objective_name: str | None
    # AX/confocal 的 Scan Area Zoom；优先从 text_info 的 Scan Area 段读取。
    scan_zoom: float | None
    # 第一帧的绝对拍摄时间；由 ND2 的 Julian day timestamp 换算为本机时区。
    acquisition_time: datetime | None
    # 通道名称及 ND2 中保存的显示颜色。
    channel_names: tuple[str, ...]
    channel_colors: tuple[tuple[int, int, int], ...]

    @property
    def objective_label(self) -> str | None:
        """把 100.0 转换成用于字典查找和标注的 ``100X``。"""
        if self.magnification is None:
            return None
        rounded = round(self.magnification)
        value = (
            str(rounded) if math.isclose(self.magnification, rounded) else f"{self.magnification:g}"
        )
        return f"{value}X"


@dataclass(frozen=True)
class ROIResult:
    """一次坐标映射的完整中间结果，便于在控制台中逐项核对。"""

    # 高倍视野中心在低倍图中的像素坐标。
    center_x_px: float
    center_y_px: float
    # 高倍真实 FOV 换算到低倍图后的框宽和框高。
    width_px: float
    height_px: float
    # 应用物镜固定偏移后，高倍中心相对低倍中心的物理位移。
    dx_um: float
    dy_um: float
    # 上述物理位移换算得到的低倍像素位移。
    dx_px: float
    dy_px: float
    # 本次实际使用的物镜固定偏移，单独保留用于调试。
    offset_x_um: float
    offset_y_um: float

    @property
    def box(self) -> tuple[float, float, float, float]:
        """返回矩形的 (左, 上, 右, 下)，允许暂时超出图像边界。"""
        return (
            self.center_x_px - self.width_px / 2,
            self.center_y_px - self.height_px / 2,
            self.center_x_px + self.width_px / 2,
            self.center_y_px + self.height_px / 2,
        )


class MetadataError(RuntimeError):
    """找不到定位所必需的 ND2 metadata 字段时抛出的可读异常。"""


# ============================================================================
# 第三部分：把未知结构的 metadata 转成可打印的诊断文本
# ============================================================================


def _plain_object(value: Any, depth: int = 0, seen: set[int] | None = None) -> Any:
    """把 nd2/dataclass 对象递归转换成可由 JSON 打印的普通对象。

    这个函数不参与坐标计算，只在 metadata 缺失时帮助观察实际文件结构。
    ``depth`` 防止层级过深，``seen`` 防止对象循环引用造成死循环。
    """
    if depth > 12:
        return "<maximum depth reached>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.generic):
        return value.item()
    if seen is None:
        seen = set()
    value_id = id(value)
    if value_id in seen:
        return "<recursive reference>"
    seen.add(value_id)
    try:
        if dataclasses.is_dataclass(value):
            return {
                field.name: _plain_object(getattr(value, field.name), depth + 1, seen)
                for field in dataclasses.fields(value)
            }
        if isinstance(value, dict):
            return {str(k): _plain_object(v, depth + 1, seen) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_plain_object(v, depth + 1, seen) for v in value]
        if hasattr(value, "_asdict"):
            return _plain_object(value._asdict(), depth + 1, seen)
        if hasattr(value, "__dict__"):
            return {
                str(k): _plain_object(v, depth + 1, seen)
                for k, v in vars(value).items()
                if not str(k).startswith("_")
            }
        return repr(value)
    finally:
        seen.discard(value_id)


def _metadata_dump(nd2_file: nd2.ND2File) -> str:
    """汇总 nd2 包公开的主要 metadata 接口，生成完整诊断字符串。"""

    # sizes/metadata/experiment 通常都能直接读取。
    payload: dict[str, Any] = {
        "sizes": dict(nd2_file.sizes),
        "metadata": _plain_object(nd2_file.metadata),
        "experiment": _plain_object(nd2_file.experiment),
    }
    try:
        payload["frame_metadata(0)"] = _plain_object(nd2_file.frame_metadata(0))
    # frame_metadata 和 voxel_size 本身也可能读取失败；诊断流程不能覆盖原异常。
    except Exception as exc:
        payload["frame_metadata(0)_error"] = repr(exc)
    try:
        payload["voxel_size"] = _plain_object(nd2_file.voxel_size())
    except Exception as exc:
        payload["voxel_size_error"] = repr(exc)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=repr)


def _iter_named_values(value: Any, path: str = "", depth: int = 0) -> Iterable[tuple[str, Any]]:
    """遍历未知 metadata 结构，逐个返回 ``字段路径, 叶子值``。

    当不同 nd2 版本的固定属性路径失效时，后面的 fallback 会用它搜索字段名。
    """
    if depth > 12 or value is None:
        return
    if isinstance(value, (str, int, float, bool, np.generic)):
        yield path, value
        return
    if dataclasses.is_dataclass(value):
        for field in dataclasses.fields(value):
            yield from _iter_named_values(
                getattr(value, field.name), f"{path}.{field.name}", depth + 1
            )
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from _iter_named_values(child, f"{path}.{key}", depth + 1)
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _iter_named_values(child, f"{path}[{index}]", depth + 1)
    elif hasattr(value, "__dict__"):
        for key, child in vars(value).items():
            if not str(key).startswith("_"):
                yield from _iter_named_values(child, f"{path}.{key}", depth + 1)


def get_stage_position(nd2_file: nd2.ND2File) -> tuple[float, float]:
    """读取 stage X/Y，单位 µm。

    首选 nd2 0.11 的公开路径：
    ``frame_metadata(0).channels[0].position.stagePositionUm``。
    这里的 ``0`` 表示第一个 frame，``channels[0]`` 表示第一个通道；同一帧的
    各通道通常共享 stage 位置。如果文件是多位置或多时间序列，当前版本仍只取第一个。
    """

    # 注意：程序当前直接把下面两个数解释为“图像中心”的 stage 坐标。
    # 如果 Nikon 实际把它定义为扫描起点，这里就是最需要修改的位置。
    frame_meta = nd2_file.frame_metadata(0)
    try:
        stage = frame_meta.channels[0].position.stagePositionUm
        if stage.x is not None and stage.y is not None:
            return float(stage.x), float(stage.y)
    except (AttributeError, IndexError, TypeError):
        pass

    # 固定路径失败时，不立即报错：遍历 frame metadata，搜索路径中含有
    # stagePosition 且字段名以 .x/.y 结尾的数值。
    found: dict[str, float] = {}
    for path, value in _iter_named_values(frame_meta):
        lower = path.lower()
        if "stageposition" not in lower or not isinstance(value, (int, float, np.number)):
            continue
        if lower.endswith(".x"):
            found.setdefault("x", float(value))
        elif lower.endswith(".y"):
            found.setdefault("y", float(value))
    if "x" in found and "y" in found:
        return found["x"], found["y"]
    raise MetadataError("缺少 stage X/Y position（已检查 frame_metadata(0)）")


def get_stage_z(nd2_file: nd2.ND2File) -> float | None:
    """尽力读取 stage Z（µm），仅用于显示，不影响现有 XY 定位公式。"""

    frame_meta = nd2_file.frame_metadata(0)
    try:
        stage = frame_meta.channels[0].position.stagePositionUm
        if stage.z is not None:
            return float(stage.z)
    except (AttributeError, IndexError, TypeError, ValueError):
        pass

    for path, value in _iter_named_values(frame_meta):
        lower = path.lower()
        if (
            "stageposition" in lower
            and lower.endswith(".z")
            and isinstance(value, (int, float, np.number))
        ):
            return float(value)
    return None


def get_pixel_size(nd2_file: nd2.ND2File) -> tuple[float, float]:
    """读取 X/Y 物理像素尺寸，单位 µm/px。"""

    # 首选 nd2 包已经整理好的 voxel_size()。
    try:
        voxel = nd2_file.voxel_size()
        x, y = float(voxel.x), float(voxel.y)
        if x > 0 and y > 0:
            return x, y
    except (AttributeError, TypeError, ValueError):
        pass

    # 兼容路径：metadata 第一个通道的 axesCalibration 通常依次为 X/Y/Z。
    try:
        calibration = nd2_file.metadata.channels[0].volume.axesCalibration
        x, y = float(calibration[0]), float(calibration[1])
        if x > 0 and y > 0:
            return x, y
    except (AttributeError, IndexError, TypeError, ValueError):
        pass
    raise MetadataError("缺少有效的 physical pixel size（已检查 voxel_size 和 axesCalibration）")


def _objective_info(nd2_file: nd2.ND2File) -> tuple[float | None, str | None]:
    """读取物镜倍率和名称；失败不会中止定位，只返回 None。"""

    try:
        microscope = nd2_file.metadata.channels[0].microscope
        mag = getattr(microscope, "objectiveMagnification", None)
        name = getattr(microscope, "objectiveName", None)
        return (float(mag) if mag is not None else None, str(name) if name else None)
    except (AttributeError, IndexError, TypeError, ValueError):
        return None, None


def get_scan_zoom(nd2_file: nd2.ND2File) -> float | None:
    """读取扫描区域的 Zoom 倍率，例如 ``Scan Area -> Zoom: 2.16``。

    ``text_info`` 中的 Scan Area Zoom 最接近采集界面里设置的 zoom in。
    如果文本字段不存在，再尝试结构化的 ``zoomMagnification``。后者在部分
    Nikon 配置中可能代表显微镜光路 zoom，而不是扫描 zoom，因此只作 fallback。
    """

    try:
        text_info = nd2_file.text_info or {}
    except Exception:
        text_info = {}

    # capturing 通常比 description 简短，并且同样包含 Scan Area 设置。
    for key in ("capturing", "description"):
        text = str(text_info.get(key, ""))
        match = re.search(
            r"Scan\s+Area\s*:.*?\bZoom\s*:\s*([0-9]+(?:\.[0-9]+)?)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            zoom = float(match.group(1))
            if zoom > 0:
                return zoom

    # 文本中找不到时，兼容 nd2 包提供的结构化字段。
    try:
        zoom = nd2_file.metadata.channels[0].microscope.zoomMagnification
        if zoom is not None and float(zoom) > 0:
            return float(zoom)
    except (AttributeError, IndexError, TypeError, ValueError):
        pass

    return None


def _channel_info(
    nd2_file: nd2.ND2File,
) -> tuple[tuple[str, ...], tuple[tuple[int, int, int], ...]]:
    """读取各荧光通道名称和 RGB 显示颜色。"""

    names: list[str] = []
    colors: list[tuple[int, int, int]] = []
    try:
        for index, item in enumerate(nd2_file.metadata.channels):
            channel = item.channel
            names.append(str(channel.name or f"Channel {index + 1}"))
            color = channel.color
            colors.append((int(color.r), int(color.g), int(color.b)))
    except (AttributeError, TypeError):
        pass
    return tuple(names), tuple(colors)


def get_acquisition_time(nd2_file: nd2.ND2File) -> datetime | None:
    """读取第一帧的绝对拍摄时间，并转换成本机时区。

    Nikon/``nd2`` 将时间保存在 frame metadata 的
    ``absoluteJulianDayNumber`` 字段中。这里不使用文件创建/修改时间作为后备值，
    以免文件复制后把文件系统时间误显示成显微镜拍摄时间。
    """

    try:
        julian_day = float(nd2_file.frame_metadata(0).channels[0].time.absoluteJulianDayNumber)
        if not math.isfinite(julian_day) or julian_day <= 0:
            return None

        # Julian day 2440587.5 对应 Unix epoch 1970-01-01 00:00:00 UTC。
        seconds_since_epoch = (julian_day - 2440587.5) * 86_400.0
        acquired = datetime.fromtimestamp(seconds_since_epoch, timezone.utc).astimezone()
        # 明显不合理的日期视为 metadata 缺失/损坏，而不是展示误导性结果。
        if not 1900 <= acquired.year <= 2200:
            return None
        return acquired
    except (AttributeError, IndexError, OSError, OverflowError, TypeError, ValueError):
        return None


def read_nd2_metadata(path: str | Path) -> ND2Metadata:
    """打开一个 ND2，集中读取定位需要的全部 metadata。

    任一必要字段缺失时，会先把当前能读到的 metadata 结构打印到控制台，
    然后重新抛出原异常。这样可以针对不同 NIS-Elements 版本修改上述读取函数。
    """

    path = Path(path)
    # 使用 with 确保 ND2 文件句柄在函数结束时关闭。
    with nd2.ND2File(path) as nd2_file:
        try:
            # sizes 是类似 {'C': 3, 'Y': 2048, 'X': 2048} 的有序映射。
            sizes = dict(nd2_file.sizes)
            if "X" not in sizes or "Y" not in sizes:
                raise MetadataError(f"缺少图像 X/Y dimensions；当前 dimensions: {sizes}")
            # 以下字段是坐标计算的直接输入，建议排查时逐个与 NIS-Elements 对照。
            stage_x, stage_y = get_stage_position(nd2_file)
            stage_z = get_stage_z(nd2_file)
            pixel_x, pixel_y = get_pixel_size(nd2_file)
            magnification, objective_name = _objective_info(nd2_file)
            scan_zoom = get_scan_zoom(nd2_file)
            acquisition_time = get_acquisition_time(nd2_file)
            channel_names, channel_colors = _channel_info(nd2_file)
            return ND2Metadata(
                path=path,
                stage_x_um=stage_x,
                stage_y_um=stage_y,
                stage_z_um=stage_z,
                pixel_size_x_um=pixel_x,
                pixel_size_y_um=pixel_y,
                width_px=int(sizes["X"]),
                height_px=int(sizes["Y"]),
                magnification=magnification,
                objective_name=objective_name,
                scan_zoom=scan_zoom,
                acquisition_time=acquisition_time,
                channel_names=channel_names,
                channel_colors=channel_colors,
            )
        except Exception:
            # 诊断信息输出到 stderr；命令行模式下可直接查看完整结构。
            print(f"\n无法完整读取 metadata: {path}", file=sys.stderr)
            print("当前可读取的 metadata 结构：", file=sys.stderr)
            print(_metadata_dump(nd2_file), file=sys.stderr)
            raise


# ============================================================================
# 第四部分：读取 ND2 像素并生成用于查看的 RGB composite
# 这一部分只影响底图亮度和颜色，不参与 ROI 坐标计算。
# ============================================================================


def _first_cyx_plane(nd2_file: nd2.ND2File) -> np.ndarray:
    """从 ND2 取第一张图，并统一转成 (C, Y, X) 轴顺序。

    对 T（时间）、Z（层面）、P（位置）等额外维度统一取索引 0。
    """

    # asarray() 返回的轴顺序与 nd2_file.sizes.keys() 对应。
    array = np.asarray(nd2_file.asarray())
    axes = list(nd2_file.sizes.keys())
    # 某些 nd2 版本会自动挤掉长度为 1 的轴，因此这里同步清理 axes。
    if array.ndim != len(axes):
        array = np.squeeze(array)
        axes = [axis for axis, size in nd2_file.sizes.items() if size != 1]
    # C/Y/X 三个轴全部保留；其余轴只取第 0 张。
    index: list[Any] = [slice(None) if axis in {"C", "Y", "X"} else 0 for axis in axes]
    array = array[tuple(index)]
    kept_axes = [axis for axis in axes if axis in {"C", "Y", "X"}]
    # 单通道 ND2 可能没有显式 C 轴，人为补一个长度为 1 的 C 轴。
    if "C" not in kept_axes:
        array = np.expand_dims(array, 0)
        kept_axes.insert(0, "C")
    if "Y" not in kept_axes or "X" not in kept_axes:
        raise ValueError(f"无法从 dimensions {dict(nd2_file.sizes)} 提取 Y/X 图像")
    # 无论源文件轴顺序如何，返回值固定为 C,Y,X。
    return np.transpose(array, [kept_axes.index(axis) for axis in ("C", "Y", "X")])


def _normalise_channel(channel: np.ndarray) -> np.ndarray:
    """把一个荧光通道拉伸到 0~1，仅用于显示。

    默认用 1% 和 99.8% 分位点做上下限，减少极少量异常亮点的影响。
    该归一化会改变显示强度，不能用于定量荧光分析。
    """

    # 丢弃 NaN/Inf 后再计算分位点。
    finite = channel[np.isfinite(channel)]
    if finite.size == 0:
        return np.zeros(channel.shape, dtype=np.float32)
    low, high = np.percentile(finite, (1.0, 99.8))
    # 图像几乎为常数时，改用真正的最小值/最大值。
    if high <= low:
        high = float(finite.max())
        low = float(finite.min())
    if high <= low:
        return np.zeros(channel.shape, dtype=np.float32)
    return np.clip((channel.astype(np.float32) - low) / (high - low), 0.0, 1.0)


def read_nd2_image(path: str | Path) -> Image.Image:
    """读取 ND2 并生成简单的加法 RGB 荧光合成图。"""

    with nd2.ND2File(path) as nd2_file:
        cyx = _first_cyx_plane(nd2_file)
        _, colors = _channel_info(nd2_file)
    # metadata 没有颜色时，按蓝、绿、红、白循环分配默认颜色。
    fallback = ((0, 0, 255), (0, 255, 0), (255, 0, 0), (255, 255, 255))
    # rgb 使用浮点数累加，最后裁剪到 0~1。
    rgb = np.zeros((cyx.shape[1], cyx.shape[2], 3), dtype=np.float32)
    for index, channel in enumerate(cyx):
        color = colors[index] if index < len(colors) else fallback[index % len(fallback)]
        color_vector = np.asarray(color, dtype=np.float32) / 255.0
        # 单通道灰度强度乘以该通道的 RGB 颜色，再加到 composite 中。
        rgb += _normalise_channel(channel)[..., None] * color_vector
    return Image.fromarray(np.uint8(np.clip(rgb, 0.0, 1.0) * 255), mode="RGB")


def _offset_for(metadata: ND2Metadata) -> tuple[float, float]:
    """按照 ``60X``/``100X`` 标签取得对应的物镜固定偏移。"""

    label = metadata.objective_label
    # 物镜倍率缺失或字典中没有该倍率时，默认不加任何偏移。
    return OBJECTIVE_OFFSET.get(label, (0.0, 0.0)) if label else (0.0, 0.0)


# ============================================================================
# 第五部分：核心坐标映射（框位置不对时，最重要的排查位置）
# ============================================================================


def calculate_roi_position(
    low: ND2Metadata,
    high: ND2Metadata,
    x_sign: int = X_SIGN,
    y_sign: int = Y_SIGN,
) -> ROIResult:
    """用 stage 坐标和像素尺寸计算高倍视野在低倍图中的矩形。

    当前模型只包含“分别缩放 X/Y + 分别反向 + 固定平移”，不包含：

    1. X/Y 轴互换；
    2. 任意角度旋转；
    3. shear（剪切）或非线性畸变；
    4. 低倍和高倍使用不同 detector/scan area 后产生的额外坐标变换；
    5. stagePositionUm 不是图像中心时所需的半个 FOV 修正。

    如果修改 X_SIGN/Y_SIGN 和 OBJECTIVE_OFFSET 后仍无法对齐，应把模型升级为
    2×2 仿射矩阵，而不是继续盲调正负号。
    """

    # 第 1 步：根据高倍物镜倍率取得人工校准的固定偏移（单位 µm）。
    offset_x, offset_y = _offset_for(high)

    # 第 2 步：高倍 stage 中心减去低倍 stage 中心，得到物理位移。
    # 公式：dx_um = (Xhigh + objective_offset_x) - Xlow
    #       dy_um = (Yhigh + objective_offset_y) - Ylow
    # 注意：这里隐含“两个 stagePositionUm 都代表图像中心”的关键假设。
    dx_um = high.stage_x_um + offset_x - low.stage_x_um
    dy_um = high.stage_y_um + offset_y - low.stage_y_um

    # 第 3 步：把 µm 位移换算为 10X 图中的像素位移。
    # 这里使用低倍像素尺寸，因为最终矩形画在低倍图上。
    # 若框在相反方向，修改顶部 X_SIGN/Y_SIGN；若 X 位移跑到了 Y 方向，说明要交换轴。
    if x_sign not in (-1, 1) or y_sign not in (-1, 1):
        raise ValueError(f"x_sign/y_sign 只能是 -1 或 1，当前值为 ({x_sign}, {y_sign})")
    dx_px = x_sign * dx_um / low.pixel_size_x_um
    dy_px = y_sign * dy_um / low.pixel_size_y_um

    # 第 4 步：低倍图中心 + 像素位移 = 高倍中心在低倍图中的像素坐标。
    # 图像宽/高除以 2 表示把低倍 stage 坐标放在低倍图几何中心。
    center_x_px = low.width_px / 2 + dx_px
    center_y_px = low.height_px / 2 + dy_px

    # 第 5 步：先算高倍图真实物理 FOV，再除以低倍像素尺寸。
    # 例如：高倍宽度(px) × 高倍像素尺寸(µm/px) = 高倍真实宽度(µm)。
    roi_width_px = high.width_px * high.pixel_size_x_um / low.pixel_size_x_um
    roi_height_px = high.height_px * high.pixel_size_y_um / low.pixel_size_y_um

    return ROIResult(
        center_x_px=center_x_px,
        center_y_px=center_y_px,
        width_px=roi_width_px,
        height_px=roi_height_px,
        dx_um=dx_um,
        dy_um=dy_um,
        dx_px=dx_px,
        dy_px=dy_px,
        offset_x_um=offset_x,
        offset_y_um=offset_y,
    )


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """按 Windows/Linux 顺序寻找字体；都找不到时使用 Pillow 默认字体。"""

    candidates = (
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _intersection_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    """计算两个矩形的重叠面积；不相交时返回 0。"""

    width = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def _draw_arrow(
    draw: ImageDraw.ImageDraw,
    label_box: tuple[float, float, float, float],
    roi: ROIResult,
    color: tuple[int, int, int],
    line_width: int,
) -> None:
    """从标签边缘画一根箭头，箭头尖端落在 ROI 矩形边缘。"""

    label_cx = (label_box[0] + label_box[2]) / 2
    label_cy = (label_box[1] + label_box[3]) / 2
    roi_cx, roi_cy = roi.center_x_px, roi.center_y_px

    # 连线从标签矩形上最靠近 ROI 中心的点出发。
    start_x = min(max(roi_cx, label_box[0]), label_box[2])
    start_y = min(max(roi_cy, label_box[1]), label_box[3])

    # 从 ROI 中心沿“指向标签”的方向求与 ROI 边缘的交点。
    toward_label_x = label_cx - roi_cx
    toward_label_y = label_cy - roi_cy
    half_width = max(1.0, roi.width_px / 2)
    half_height = max(1.0, roi.height_px / 2)
    tx = half_width / abs(toward_label_x) if abs(toward_label_x) > 1e-9 else float("inf")
    ty = half_height / abs(toward_label_y) if abs(toward_label_y) > 1e-9 else float("inf")
    scale = min(tx, ty)
    end_x = roi_cx + toward_label_x * scale
    end_y = roi_cy + toward_label_y * scale

    draw.line((start_x, start_y, end_x, end_y), fill=color, width=line_width)

    # 在 ROI 边缘绘制实心三角箭头。
    direction_x = end_x - start_x
    direction_y = end_y - start_y
    length = math.hypot(direction_x, direction_y)
    if length <= 1e-9:
        return
    unit_x, unit_y = direction_x / length, direction_y / length
    arrow_length = max(10, line_width * 4)
    arrow_half_width = max(5, line_width * 2)
    base_x = end_x - unit_x * arrow_length
    base_y = end_y - unit_y * arrow_length
    perpendicular_x, perpendicular_y = -unit_y, unit_x
    draw.polygon(
        (
            (end_x, end_y),
            (
                base_x + perpendicular_x * arrow_half_width,
                base_y + perpendicular_y * arrow_half_width,
            ),
            (
                base_x - perpendicular_x * arrow_half_width,
                base_y - perpendicular_y * arrow_half_width,
            ),
        ),
        fill=color,
    )


def draw_rois(
    image: Image.Image,
    items: list[tuple[ROIResult, str, tuple[int, int, int]]],
) -> Image.Image:
    """统一绘制多个 ROI，并自动避让相邻的框和标签。

    每个 item 是 ``(ROI结果, 两行标签, RGB颜色)``。程序先画全部框，再按顺序
    选择标签位置。默认位置发生遮挡时，会尝试框的下、左、右及更远的对角位置，
    并从移动后的标签画箭头指回对应 ROI。
    """

    output = image.copy().convert("RGB")
    draw = ImageDraw.Draw(output)
    line_width = max(3, round(min(output.size) / 350))
    cross = max(6, line_width * 2)
    font = _font(max(16, round(min(output.size) / 65)))
    text_spacing = max(2, line_width)
    margin = max(6, line_width * 2)
    near_gap = max(12, line_width * 3)
    far_gap = max(36, round(min(output.size) / 45))

    # 第一步：先画完所有矩形和中心十字，避免后画的框覆盖已放置标签。
    roi_obstacles: list[tuple[float, float, float, float]] = []
    for roi, _label, color in items:
        left, top, right, bottom = roi.box
        visible_box = (
            max(0, left),
            max(0, top),
            min(output.width - 1, right),
            min(output.height - 1, bottom),
        )
        if visible_box[0] <= visible_box[2] and visible_box[1] <= visible_box[3]:
            draw.rectangle(visible_box, outline=color, width=line_width)
        cx, cy = roi.center_x_px, roi.center_y_px
        draw.line((cx - cross, cy, cx + cross, cy), fill=color, width=line_width)
        draw.line((cx, cy - cross, cx, cy + cross), fill=color, width=line_width)
        roi_obstacles.append((left - margin, top - margin, right + margin, bottom + margin))

    # 第二步：为每个标签计算多个候选位置，优先选择不遮挡任何框/标签的位置。
    placed_labels: list[tuple[float, float, float, float]] = []
    placements: list[
        tuple[ROIResult, str, tuple[int, int, int], float, float, float, float, bool]
    ] = []

    for item_index, (roi, label, color) in enumerate(items):
        measured = draw.multiline_textbbox(
            (0, 0), label, font=font, spacing=text_spacing, stroke_width=1
        )
        text_width = measured[2] - measured[0]
        text_height = measured[3] - measured[1]
        left, top, right, bottom = roi.box

        # 第一个候选是原来的“紧贴框上方”；其余候选逐渐远离框。
        candidates = (
            (left, top - text_height - near_gap),
            (left, bottom + near_gap),
            (right + far_gap, roi.center_y_px - text_height / 2),
            (left - text_width - far_gap, roi.center_y_px - text_height / 2),
            (right + far_gap, top - text_height - far_gap),
            (left - text_width - far_gap, top - text_height - far_gap),
            (right + far_gap, bottom + far_gap),
            (left - text_width - far_gap, bottom + far_gap),
        )

        best: tuple[float, int, float, float, tuple[float, float, float, float]] | None = None
        for candidate_index, (raw_x, raw_y) in enumerate(candidates):
            # 把候选位置限制在图像内部。
            x = min(max(margin, raw_x), max(margin, output.width - text_width - margin))
            y = min(max(margin, raw_y), max(margin, output.height - text_height - margin))
            background_box = (
                x - 4,
                y - 4,
                x + text_width + 5,
                y + text_height + 5,
            )

            # 遮挡 ROI 的代价最高，其次是遮挡已经放置的标签。
            roi_overlap = sum(_intersection_area(background_box, box) for box in roi_obstacles)
            label_overlap = sum(_intersection_area(background_box, box) for box in placed_labels)
            distance = math.hypot(x - left, y - (top - text_height - near_gap))
            score = roi_overlap * 1000 + label_overlap * 2000 + distance + candidate_index
            choice = (score, candidate_index, x, y, background_box)
            if best is None or choice < best:
                best = choice

        assert best is not None
        _score, candidate_index, text_x, text_y, background_box = best
        placed_labels.append(background_box)
        # 多图时，标签只要不在原始紧邻位置，就用箭头明确对应关系。
        use_arrow = len(items) > 1 and candidate_index != 0
        placements.append((roi, label, color, text_x, text_y, text_width, text_height, use_arrow))

    # 第三步：箭头先画，标签黑色背景后画，这样连线不会穿过文字。
    for roi, _label, color, text_x, text_y, text_width, text_height, use_arrow in placements:
        if use_arrow:
            label_box = (
                text_x - 4,
                text_y - 4,
                text_x + text_width + 5,
                text_y + text_height + 5,
            )
            _draw_arrow(draw, label_box, roi, color, line_width)

    for _roi, label, color, text_x, text_y, text_width, text_height, _use_arrow in placements:
        draw.rectangle(
            (text_x - 4, text_y - 4, text_x + text_width + 5, text_y + text_height + 5),
            fill=(0, 0, 0),
        )
        draw.multiline_text(
            (text_x, text_y),
            label,
            font=font,
            fill=color,
            spacing=text_spacing,
            stroke_width=1,
            stroke_fill=(0, 0, 0),
        )

    return output


def draw_roi(
    image: Image.Image,
    roi: ROIResult,
    label: str,
    color: tuple[int, int, int] = (255, 255, 0),
) -> Image.Image:
    """保留单 ROI 绘图接口；内部调用统一的多 ROI 排版函数。"""

    return draw_rois(image, [(roi, label, color)])


def _print_metadata(title: str, metadata: ND2Metadata) -> None:
    """把单个 ND2 的定位输入打印到控制台，方便与 NIS-Elements 核对。"""

    print(f"\n{title}:")
    print(f"file = {metadata.path}")
    print(f"stage X = {metadata.stage_x_um:.6f} um")
    print(f"stage Y = {metadata.stage_y_um:.6f} um")
    print(
        f"stage Z = {metadata.stage_z_um:.6f} um"
        if metadata.stage_z_um is not None
        else "stage Z = not available"
    )
    print(f"pixel size X = {metadata.pixel_size_x_um:.9f} um/px")
    print(f"pixel size Y = {metadata.pixel_size_y_um:.9f} um/px")
    print(f"image width = {metadata.width_px} px")
    print(f"image height = {metadata.height_px} px")
    objective = metadata.objective_label or "not available"
    objective_name = metadata.objective_name or "name unavailable"
    print(f"objective = {objective} ({objective_name})")
    print(
        f"scan zoom = {metadata.scan_zoom:g}x"
        if metadata.scan_zoom is not None
        else "scan zoom = not available"
    )
    print(
        f"acquisition time = {metadata.acquisition_time.isoformat(sep=' ', timespec='seconds')}"
        if metadata.acquisition_time is not None
        else "acquisition time = not available"
    )


def _print_result(
    roi: ROIResult,
    x_sign: int = X_SIGN,
    y_sign: int = Y_SIGN,
) -> None:
    """把所有核心中间计算值打印到控制台。"""

    print("\nCalculated:")
    print(f"X_SIGN = {x_sign}")
    print(f"Y_SIGN = {y_sign}")
    print(f"objective offset = ({roi.offset_x_um:.6f}, {roi.offset_y_um:.6f}) um")
    print(f"dx_um = {roi.dx_um:.6f}")
    print(f"dy_um = {roi.dy_um:.6f}")
    print(f"dx_px = {roi.dx_px:.3f}")
    print(f"dy_px = {roi.dy_px:.3f}")
    print(f"ROI center = ({roi.center_x_px:.3f}, {roi.center_y_px:.3f}) px")
    print(f"ROI width = {roi.width_px:.3f} px")
    print(f"ROI height = {roi.height_px:.3f} px")
    print(f"ROI box = ({roi.box[0]:.3f}, {roi.box[1]:.3f}, {roi.box[2]:.3f}, {roi.box[3]:.3f}) px")


# ============================================================================
# 第六部分：串联“读取 → 计算 → 绘图 → 保存”的完整工作流
# ============================================================================


def locate_rois(
    low_path: str | Path,
    high_paths: Iterable[str | Path],
    output_path: str | Path,
    x_sign: int = X_SIGN,
    y_sign: int = Y_SIGN,
) -> Path:
    """把多个高倍 ND2 视野定位到同一张低倍图，并返回输出绝对路径。"""

    # 立即转换为列表，既允许调用者传入生成器，也便于检查是否为空。
    high_path_list = [Path(path) for path in high_paths]
    if not high_path_list:
        raise ValueError("至少需要选择一个 60X/100X ND2 文件。")

    # 低倍 metadata 和底图只读取一次，随后依次叠加所有高倍 ROI。
    low = read_nd2_metadata(low_path)
    _print_metadata("10X / low magnification", low)

    base = read_nd2_image(low_path)
    drawing_items: list[tuple[ROIResult, str, tuple[int, int, int]]] = []
    colors = (
        (255, 255, 0),  # 黄
        (0, 255, 255),  # 青
        (255, 80, 255),  # 品红
        (255, 160, 0),  # 橙
        (80, 255, 80),  # 绿
        (120, 180, 255),  # 蓝
    )

    for index, high_path in enumerate(high_path_list, start=1):
        high = read_nd2_metadata(high_path)
        _print_metadata(f"High magnification [{index}/{len(high_path_list)}]", high)

        roi = calculate_roi_position(low, high, x_sign=x_sign, y_sign=y_sign)
        _print_result(roi, x_sign=x_sign, y_sign=y_sign)
        if not (0 <= roi.center_x_px < low.width_px and 0 <= roi.center_y_px < low.height_px):
            print(
                f"WARNING: ROI centre for {high_path.name} lies outside the 10X image. "
                "Check X_SIGN/Y_SIGN, file pairing, and objective offset."
            )

        objective = high.objective_label or "High-mag"
        zoom_text = f"{high.scan_zoom:g}×" if high.scan_zoom is not None else "N/A"
        # 第一行显示当前高倍文件名，第二行显示该文件自己的物镜和 Zoom。
        label = f"{high_path.name}\nObjective {objective} | Zoom {zoom_text}"
        color = colors[(index - 1) % len(colors)]
        drawing_items.append((roi, label, color))

    # 所有 ROI 坐标齐备后一次性排版，才能让标签避开其他框和标签。
    annotated = draw_rois(base, drawing_items)

    # 自动创建输出父目录。JPG 使用 quality=95，PNG 使用 Pillow 默认无损参数。
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs = {"quality": 95} if output.suffix.lower() in {".jpg", ".jpeg"} else {}
    annotated.save(output, **save_kwargs)
    print(f"\nCompleted.\nOutput saved to:\n{output.resolve()}")
    return output.resolve()


def locate_roi(
    low_path: str | Path,
    high_path: str | Path,
    output_path: str | Path,
    x_sign: int = X_SIGN,
    y_sign: int = Y_SIGN,
) -> Path:
    """保留原单高倍文件接口；内部转交给多 ROI 工作流。"""

    return locate_rois(
        low_path,
        [high_path],
        output_path,
        x_sign=x_sign,
        y_sign=y_sign,
    )


def _default_output(low_path: str, extension: str) -> Path:
    """根据低倍文件名生成默认输出名，例如 10X-1_ROI_map.png。"""

    low = Path(low_path)
    return low.with_name(f"{low.stem}_ROI_map.{extension.lower()}")


# ============================================================================
# 第七部分：Tkinter 图形界面
# GUI 只负责收集路径和显示消息，不包含任何坐标计算公式。
# ============================================================================


def main_gui() -> None:
    """延迟加载独立 UI 层，使命令行模式无需初始化 Tkinter。"""

    from nd2_roi_ui import launch_app

    launch_app()


# ============================================================================
# 第八部分：命令行参数和程序入口
# ============================================================================


def _parse_args() -> argparse.Namespace:
    """定义可选的命令行模式参数。"""

    parser = argparse.ArgumentParser(
        description=(
            "Map one or more high-magnification Nikon ND2 fields onto a 10X overview "
            "using stage coordinates and physical pixel sizes."
        )
    )
    parser.add_argument("--low", help="10X/low-magnification ND2")
    parser.add_argument(
        "--high",
        nargs="+",
        help="One or more 60X/100X high-magnification ND2 files",
    )
    parser.add_argument(
        "--microscope",
        choices=("upright", "inverted"),
        default="upright",
        help=(
            "Microscope orientation: upright uses X_SIGN=1/Y_SIGN=-1; "
            "inverted uses X_SIGN=-1/Y_SIGN=1"
        ),
    )
    parser.add_argument("--output", help="Annotated .png/.jpg output")
    return parser.parse_args()


def main() -> None:
    """有命令行参数时直接处理；没有参数时启动 GUI。"""

    args = _parse_args()
    supplied = [args.low, args.high, args.output]
    # 三个参数中只要提供了任意一个，就进入 CLI 模式，并要求三个必须齐全。
    if any(supplied):
        if not all(supplied):
            raise SystemExit("CLI mode requires --low, --high, and --output together.")
        selected_x_sign, selected_y_sign = MICROSCOPE_SIGNS[args.microscope]
        locate_rois(
            args.low,
            args.high,
            args.output,
            x_sign=selected_x_sign,
            y_sign=selected_y_sign,
        )
    else:
        main_gui()


# 直接执行 ``python nd2_roi_locator.py`` 时才调用 main()；
# 其他脚本 import 本模块时不会自动打开 GUI。
if __name__ == "__main__":
    main()
