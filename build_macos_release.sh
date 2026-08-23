#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-1.1.2}"
ARCH_LABEL="${2:-Apple-Silicon}"
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$PROJECT_ROOT/build"
DIST_DIR="$PROJECT_ROOT/dist"
RELEASE_DIR="$PROJECT_ROOT/release"
STAGING_DIR="$PROJECT_ROOT/.release-staging"

case "$VERSION" in
  [0-9]*.[0-9]*.[0-9]*) ;;
  *) echo "Version must use MAJOR.MINOR.PATCH format." >&2; exit 2 ;;
esac

for target in "$BUILD_DIR" "$DIST_DIR" "$RELEASE_DIR" "$STAGING_DIR"; do
  case "$target" in
    "$PROJECT_ROOT"/*) rm -rf -- "$target" ;;
    *) echo "Refusing to remove path outside project: $target" >&2; exit 2 ;;
  esac
done

python -m pip install --disable-pip-version-check --quiet -r "$PROJECT_ROOT/requirements-build.txt"
python -m unittest discover -s "$PROJECT_ROOT/tests" -v
python -c "from tkinterdnd2 import TkinterDnD; root=TkinterDnD.Tk(); root.withdraw(); root.update(); root.destroy()"

export ND2_ROI_MAPPER_VERSION="$VERSION"
python -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "$DIST_DIR" \
  --workpath "$BUILD_DIR" \
  "$PROJECT_ROOT/nd2_roi_mapper.spec"

APP_PATH="$DIST_DIR/ND2 ROI Mapper.app"
APP_EXECUTABLE="$APP_PATH/Contents/MacOS/ND2 ROI Mapper"
if [[ ! -x "$APP_EXECUTABLE" ]]; then
  echo "macOS application executable is missing: $APP_EXECUTABLE" >&2
  exit 1
fi

"$APP_EXECUTABLE" --version
file "$APP_EXECUTABLE"
codesign --verify --deep --strict "$APP_PATH"

# 启动冻结后的 GUI 数秒；若 Tk/tkdnd 资源缺失，进程会提前退出并使构建失败。
"$APP_EXECUTABLE" >"$PROJECT_ROOT/macos-gui-smoke.log" 2>&1 &
APP_PID=$!
sleep 5
if ! kill -0 "$APP_PID" 2>/dev/null; then
  wait "$APP_PID" || true
  cat "$PROJECT_ROOT/macos-gui-smoke.log" >&2
  echo "Frozen macOS GUI exited during smoke test." >&2
  exit 1
fi
kill "$APP_PID"
wait "$APP_PID" || true
rm -f -- "$PROJECT_ROOT/macos-gui-smoke.log"

PACKAGE_NAME="ND2-ROI-Mapper-macOS-${ARCH_LABEL}-v${VERSION}"
PACKAGE_ROOT="$STAGING_DIR/$PACKAGE_NAME"
mkdir -p "$PACKAGE_ROOT" "$RELEASE_DIR"
cp -R "$APP_PATH" "$PACKAGE_ROOT/"
cp "$PROJECT_ROOT/README.md" "$PROJECT_ROOT/LICENSE" "$PACKAGE_ROOT/"

OUTPUT_ZIP="$RELEASE_DIR/${PACKAGE_NAME}.zip"
ditto -c -k --sequesterRsrc --keepParent "$PACKAGE_ROOT" "$OUTPUT_ZIP"
test -f "$OUTPUT_ZIP"
echo "macOS release artifact created: $OUTPUT_ZIP"
