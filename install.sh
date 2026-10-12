#!/usr/bin/env bash
# 把 dsh-status-overlay 装进 DSH profile（幂等，可反复执行）。
#
#   ./install.sh                 只装插件 + 悬浮框
#   ./install.sh --autostart     再装一个开机自启项（不依赖 DSH 也会显示悬浮框）
#   ./install.sh --uninstall     卸载（等价于 ./uninstall.sh）
#
# 需要写 ~/.dsh/profiles/web/ 与（可选）~/.config/autostart/；
# 若被文件沙箱拦住，请带 danger-full-access 重跑。
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PROFILE="${DSH_PROFILE_DIR:-$HOME/.dsh/profiles/web}"
PATCH="$PROFILE/cordis.patch.yml"
PLUGIN_ID="dsh-status-overlay"
AUTOSTART=0

for arg in "$@"; do
  case "$arg" in
    --autostart) AUTOSTART=1 ;;
    --uninstall) exec "$SRC/uninstall.sh" ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

[ -f "$PROFILE/package.json" ] || { echo "找不到 profile: $PROFILE"; exit 2; }
[ -f "$PATCH" ] || { echo "找不到 profile 补丁层: $PATCH"; exit 2; }

echo "① 复制插件到 $PROFILE/node_modules/$PLUGIN_ID"
rm -rf "$PROFILE/node_modules/$PLUGIN_ID"
mkdir -p "$PROFILE/node_modules/$PLUGIN_ID"
cp -r "$SRC/lib" "$SRC/package.json" "$SRC/cordis.patch.yml" "$PROFILE/node_modules/$PLUGIN_ID/"
cp "$SRC/README.md" "$SRC/SKILL.md" "$SRC/LICENSE" "$PROFILE/node_modules/$PLUGIN_ID/" 2>/dev/null || true
rm -rf "$PROFILE/node_modules/$PLUGIN_ID/lib/__pycache__"
chmod -R u+rwX,go+rX "$PROFILE/node_modules/$PLUGIN_ID" 2>/dev/null || true

echo "② 在 $PATCH 里挂载（幂等）"
[ -f "$PATCH.bak-status-overlay" ] || cp "$PATCH" "$PATCH.bak-status-overlay" 2>/dev/null || true
python3 - "$PATCH" "$PLUGIN_ID" <<'PY'
import pathlib, re, sys
path, plugin = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding='utf-8')
if plugin in text:
    print('   已存在，跳过')
else:
    block = "- insert:\n    - id: %s\n      name: '%s'\n" % (plugin, plugin)
    text = text.rstrip() + "\n\n" + block
    path.write_text(text, encoding='utf-8')
    print('   已写入')
PY

echo "③ 校验 YAML"
python3 - "$PATCH" <<'PY'
import sys
try:
    import yaml
except ImportError:
    print('   （没有 pyyaml，跳过校验）'); raise SystemExit
data = yaml.safe_load(open(sys.argv[1], encoding='utf-8'))
assert isinstance(data, list), '补丁层必须是顶层数组'
found = any('dsh-status-overlay' in str(entry) for entry in data)
assert found, '挂载行没写对'
print('   ✓ 合法，共 %d 条补丁项' % len(data))
PY

if [ "$AUTOSTART" = "1" ]; then
  echo "④ 安装开机自启项"
  mkdir -p "$HOME/.config/autostart"
  STATE_DIR="${DSH_HOME:-$HOME/.dsh}/status-overlay"
  # 必须用 /usr/bin/python3 全路径：PATH 里的 python3 很可能是不带 gi(GTK) 的
  # conda 解释器，那样自启项会静默失败。用 env 把 GDK_BACKEND 也钉死。
  cat > "$HOME/.config/autostart/dsh-status-overlay.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DSH 状态悬浮框
Comment=显示 DeepSeek Harness 正在执行的任务
Exec=env GDK_BACKEND=x11 /usr/bin/python3 $PROFILE/node_modules/$PLUGIN_ID/lib/overlay.py --state $STATE_DIR/state.json --lock $STATE_DIR/overlay.lock
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
  chmod +x "$PROFILE/node_modules/$PLUGIN_ID/lib/overlay.py"
  echo "   已写入 ~/.config/autostart/dsh-status-overlay.desktop"
fi

echo
echo "完成。插件通过 profile 的 live patch 热加载；"
echo "若 ~/.dsh/status-overlay/state.json 迟迟不出现，重启一次 dsh 即可。"
echo "回滚：  $SRC/uninstall.sh"
