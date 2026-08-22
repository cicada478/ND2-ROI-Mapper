# ND2 ROI Mapper

ND2 ROI Mapper 是一个面向显微成像实验的 Windows/macOS 桌面工具。它读取 Nikon
`.nd2` 文件中的载物台中心坐标和物理像素尺寸，将一个或多个 60X/100X
高倍视野映射到对应的 10X 总览图，并导出带 ROI 标注的 JPG 或 PNG。

程序只进行基于物理坐标的几何映射，不使用图像特征匹配或 AI 图像识别。

## 主要功能

- 选择或拖入一张 10X overview ND2。
- 一次选择或拖入多张高倍 ND2。
- 显示物镜、扫描 Zoom、拍摄时间、XYZ 坐标、像素尺寸、图像尺寸和通道信息。
- 支持正置与倒置显微镜的不同 X/Y 方向。
- 根据高倍图像真实视野尺寸计算 ROI 矩形大小。
- 使用不同颜色区分多个 ROI，并自动避让相邻标签。
- 支持 10X 图像的适应窗口、缩放、平移和像素坐标查看。
- 支持可拖动的 10X 比例尺，以及限制在各自 ROI 内的 Zoom in 比例尺。
- 比例尺默认白色、10 µm；右键可选择 1000/100/10/1 µm、颜色、线宽和位置。
- 导出全分辨率 JPG（默认）或 PNG。

## 系统要求

- Windows 10（1809 或更高版本）或 Windows 11，64 位系统；或者受支持的 64 位
  macOS。macOS Release 分为 Apple Silicon 与 Intel 两种架构。
- 使用 Release 便携版时，不需要安装 Python，也不需要联网安装依赖。
- 从源码运行时需要 Python 3.10 或更高版本。

## 下载与运行

v1.1.0 GitHub Release 不再提供 Windows Setup EXE，提供以下压缩包：

### Windows Portable

下载 `ND2-ROI-Mapper-Windows-Portable-v1.1.0.zip`，完整解压后双击其中的：

```text
ND2 ROI Mapper.exe
```

请保留 `ND2 ROI Mapper.exe` 与同目录的 `_internal` 文件夹，不要只复制 EXE。

### macOS

根据 Mac 芯片选择：

- Apple Silicon（M1/M2/M3/M4 等）：`ND2-ROI-Mapper-macOS-Apple-Silicon-v1.1.0.zip`
- Intel：`ND2-ROI-Mapper-macOS-Intel-v1.1.0.zip`

完整解压后，将 `ND2 ROI Mapper.app` 拖入 `Applications`，再打开应用。当前 macOS
应用尚未使用 Apple Developer ID 签名或公证；首次运行可能需要在 Finder 中按住
Control 点击应用，选择 `Open` 并确认。

当前 Release 未进行商业代码签名。Windows 可能显示 SmartScreen，macOS 可能显示
Gatekeeper 提示；应仅从项目官方 GitHub Release 下载，并核对发布页提供的 SHA-256。

## 从源码运行

Windows 克隆或下载源码后，可以双击：

```text
run_nd2_roi_locator.vbs
```

该启动器会在项目目录中创建 `.venv`、安装缺失的依赖，并通过 `pythonw.exe`
启动程序，因此不会保留命令提示符窗口。首次运行需要联网下载依赖，可能等待一段
时间；创建环境、安装依赖或启动失败时会显示明确的消息框。

macOS/Linux 从源码运行：

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python nd2_roi_locator.py
```

## 使用流程

1. 在 `10X OVERVIEW` 区域选择或拖入一张 10X ND2。
2. 等待 metadata 和图像预览加载完成。
3. 在 `HIGH MAGNIFICATION` 区域选择或拖入一张或多张高倍 ND2。
4. 选择显微镜方向：
   - `Upright`：`X_SIGN = 1`，`Y_SIGN = -1`
   - `Inverted`：`X_SIGN = -1`，`Y_SIGN = 1`
5. 点击 `Calculate & Preview ROIs`。
6. 检查 ROI 位置、标签、metadata 和越界警告。
7. 可点击 Viewer 顶部的 `10X scale bar` 或 `Zoom in scale bar`：
   - 拖动比例尺改变位置；10X 比例尺限制在 overview 内，Zoom in 比例尺限制在
     各自 ROI 内。
   - 右键比例尺可设置物理长度、颜色、线宽或位置预设。
   - 选中比例尺后也可用 `Shift + 方向键` 微调位置。
8. 点击 `Export Annotated Image`，保存为 JPG 或 PNG。

移除 10X overview 时，程序会同时清空依赖该底图的高倍文件列表和 ROI 预览，
避免把不同采集批次的数据混合映射。

## 坐标模型

程序假设 `frame_metadata(0)` 中的 Stage X/Y 是图像中心的物理坐标。高倍图像
中心相对 10X 中心的物理偏移，除以 10X 的 X/Y 物理像素尺寸后得到像素偏移。
高倍图像的实际视野大小由其像素尺寸和图像宽高计算，因此拍摄时使用额外 Zoom
后，ROI 框大小仍能随 ND2 中记录的 `µm/px` 正确变化。

显示层的 Fit、Zoom 和 Pan 只改变查看方式，不参与坐标换算，也不会改变导出图像。
比例尺同样保存为独立显示层：物理长度使用 `length_um / 10X_pixel_size_x_um`
换算成最终 10X 图像像素。Zoom in 比例尺虽然限制在对应 ROI 内，仍使用 10X 像素
尺寸绘制，因为 ROI overlay 与最终导出图像都处于同一个 10X 像素坐标系。

## 校准参数

校准参数位于 `nd2_roi_locator.py` 顶部：

```python
MICROSCOPE_SIGNS = {
    "upright": (1, -1),
    "inverted": (-1, 1),
}

OBJECTIVE_OFFSET = {
    "60X": (355.0, 145.0),
    "100X": (-12.0, 3.0),
}
```

`OBJECTIVE_OFFSET` 的格式为 `(x, y)`，单位为 µm。该值与显微镜、物镜和实验配置
有关，应通过拍摄同一明显结构进行人工校准，不应自动猜测。

## 命令行模式

需要可重复的批处理流程时，可以直接运行核心脚本：

```powershell
.\.venv\Scripts\python.exe .\nd2_roi_locator.py `
  --low .\overview-10X.nd2 `
  --high .\roi-01-60X.nd2 .\roi-02-100X.nd2 `
  --microscope upright `
  --output .\overview-10X_ROI_map.jpg
```

`--low`、`--high` 和 `--output` 必须同时提供。`--high` 支持一个或多个路径。

## 项目结构

```text
.
├── nd2_roi_locator.py       # ND2 读取、metadata、坐标映射、绘图和 CLI
├── nd2_roi_ui.py            # Tkinter GUI、状态管理、预览和导出
├── run_nd2_roi_locator.vbs  # Windows 无控制台启动器
├── requirements.txt         # 运行依赖
├── requirements-build.txt   # Windows/macOS 打包依赖
├── nd2_roi_mapper.spec      # Windows/macOS PyInstaller 独立应用配置
├── build_release.ps1        # 构建 Windows Portable ZIP
├── build_macos_release.sh   # 构建并验证 macOS App ZIP
├── .github/workflows/       # 原生 Windows、Apple Silicon 与 Intel 构建
├── tests/                   # 比例尺物理换算和范围约束测试
├── installer/               # v1.0.0 的旧 Inno Setup 配置，不用于 v1.1.0
├── packaging/               # 打包 hook 与 Windows 版本资源
├── pyproject.toml           # Python 格式化规则
├── .editorconfig            # 编辑器编码、缩进和换行规则
├── .gitattributes           # Git 文本与二进制文件规则
└── .gitignore               # 本地环境、ND2 数据和导出文件忽略规则
```

## 输出与数据

- 未点击 Export 时，预览图只存在于内存中；关闭程序后自动释放，不产生临时图片。
- 点击 Export 后才会在用户选择的位置写入 JPG 或 PNG。
- ND2 原始文件不会被修改。
- `.nd2`、本地虚拟环境和默认 ROI 导出文件已加入 `.gitignore`，避免误提交大型或
  可能包含实验信息的数据。

## 已知限制

- 当前使用第一个位置、时间点和 Z 平面生成预览。
- 假设 Stage X/Y 表示图像中心。
- 不校正旋转、X/Y 轴交换、剪切和非线性畸变。
- 各通道独立进行百分位归一化后，使用 ND2 显示颜色合成预览；该预览不适用于
  定量荧光强度分析。
- 不同 Nikon/NIS-Elements 版本的 metadata 结构可能不同。必要字段缺失时，GUI
  会显示可读错误；命令行模式会输出完整 metadata 结构，便于进一步适配。
- 比例尺当前为水平方向，物理长度使用 10X 的 X 向像素尺寸。
- macOS 应用尚未使用 Apple Developer ID 签名或 Apple notarization。

## 构建 Release

Windows 构建机需要 Windows 10/11 x64 和 Python 3.13：

```powershell
.\build_release.ps1 -Version 1.1.0 -PythonVersion 3.13
```

脚本会在项目内创建隔离的 `.build-venv`，安装构建依赖，并使用 PyInstaller 生成：

```text
release/ND2-ROI-Mapper-Windows-Portable-v1.1.0.zip
```

macOS 必须在对应架构的 Mac 上原生构建：

```bash
bash ./build_macos_release.sh 1.1.0 Apple-Silicon
```

项目的 GitHub Actions 会分别在 Windows x64、macOS Apple Silicon 和 macOS Intel
runner 上运行测试、PyInstaller 构建、冻结应用启动检查及压缩。PyInstaller 不能在
Windows 上交叉生成可信的 macOS `.app`，因此不要手工把 Windows 产物改名为 macOS 包。

`build/`、`dist/`、`release/`、`.build-venv/` 和项目内的 `.tools/` 均为本地构建
内容，不提交到 Git。上传 GitHub Release 前，应验证 ZIP 完整性并生成 SHA-256。

## 开发约定

- 源码和文档使用 UTF-8。
- Python 使用 4 空格缩进，Black 行宽为 100。
- Python、Markdown、TOML、TXT、PowerShell 和 Spec 使用 LF；VBS 与 Inno Setup
  脚本使用 Windows CRLF。
- 科学计算逻辑与 GUI 显示变换保持分离。

## License

本项目以 [MIT License](LICENSE) 发布。
