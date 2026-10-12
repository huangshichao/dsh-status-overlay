#!/usr/bin/env bash
# 卸载 dsh-status-overlay：摘掉挂载行、删掉插件目录、关掉自启项、结束悬浮框进程。
set -euo pipefail

PROFILE="${DSH_PROFILE_DIR:-$HOME/.dsh/profiles/web}"
PATCH="$PROFILE/cordis.patch.yml"
PLUGIN_ID="dsh-status-overlay"
STATE_DIR="${DSH_HOME:-$HOME/.dsh}/status-overlay"

echo "① 从 $PATCH 里摘掉挂载行"
if [ -f "$PATCH" ]; then
  python3 - "$PATCH" "$PLUGIN_ID" <<'PY'
import pathlib, re, sys
path, plugin = pathlib.Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding='utf-8')
# 安装时写入的是自成一体的单条块：
#     - insert:
#         - id: <plugin>
#           name: '<plugin>'
# 注意 id 行前面有 "- "。漏掉它正则会一次都匹配不上 —— 这里真的踩过一次，
# 结果是「卸载了但补丁层还留着挂载行」，DSH 下次启动会去找一个已删除的插件。
pattern = re.compile(
    r"\n- insert:\n[ \t]+- id: ['\"]?" + re.escape(plugin) + r"['\"]?\n(?:[ \t]+.*\n)*"
)
new, count = pattern.subn("\n", text)
if count:
    path.write_text(new.rstrip() + "\n", encoding='utf-8')
print('   已移除 %d 处' % count)
try:
    import yaml
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    left = [e for e in (data or []) if plugin in str(e)]
    print('   校验：补丁层里%s' % ('仍有残留！' if left else '已无残留 ✓'))
except ImportError:
    print('   （没有 pyyaml，跳过校验）')
PY
fi

echo "② 删除插件目录"
rm -rf "$PROFILE/node_modules/$PLUGIN_ID"

echo "③ 关闭自启项"
rm -f "$HOME/.config/autostart/dsh-status-overlay.desktop"

echo "④ 结束悬浮框"
# 优先用锁文件里的 pid —— 实测 wmctrl -c 关不掉这个无焦点的置顶窗口。
KILLED=0
if [ -f "$STATE_DIR/overlay.lock" ]; then
  PID="$(tr -dc '0-9' < "$STATE_DIR/overlay.lock" || true)"
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null && KILLED=1
    sleep 0.5
    kill -9 "$PID" 2>/dev/null || true
  fi
fi
pkill -f "status-overlay/lib/overla[y].py" 2>/dev/null || true
echo "   已结束悬浮框（pid 方式生效：$KILLED）"

echo
echo "已卸载。状态文件保留在 $STATE_DIR（想彻底清掉：rm -rf $STATE_DIR）。"
