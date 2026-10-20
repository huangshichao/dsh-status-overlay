# SKILL: status-overlay

有一个桌面悬浮框在实时显示「DSH 此刻在做什么」。这份 playbook 讲清楚它是什么、
怎么确认它在工作、以及怎么按需打开/关闭思考内容显示。

## 它是什么

一个 DSH 插件 + 一个 GTK3 小窗口：

- 插件在宿主进程里**只读**订阅 `session/event`、`agent/assistant-stream`、`agent/status`；
- 折成一份状态 JSON（`$DSH_HOME/status-overlay/state.json`）；
- 悬浮框 200ms 轮询它，显示任务 / 工具 / 进度。

插件不注册工具、不改任何 DSH 状态、不 append 会话事件。**卸载后 DSH 行为完全不变。**

## 什么时候用得上

- 用户问「你现在在干嘛 / 还在跑吗」——去看悬浮框的窗口标题（`DSH 状态 — 正在执行 · …`），
  比截图快得多。
- 做 computer use（切窗口、点、拖）时，浏览器会被盖住，用户看不到进度；
  这个框是置顶的，正好补上。
- 长任务收尾——完成/出错会发系统通知。

## 确认它在工作（不要靠截图猜）

窗口标题就是状态，直接读：

```bash
xdotool search --name "DSH 状态" | while read w; do xdotool getwindowname "$w"; done
```

状态文件更新得比 UI 还勤，看它最准：

```bash
python3 - <<'PY'
import json, time
d = json.load(open(__import__('os').path.expanduser('~/.dsh/status-overlay/state.json')))
e = d['sessions'][d['primary']]
print('%.1fs 前更新 | phase=%s | tool=%s' % ((time.time()*1000-d['updatedAt'])/1000, e['phase'], e['tool']))
PY
```

- `phase`：`thinking` / `tool` / `done` / `error` / `idle`
- `updatedAt` 由插件心跳维持（20s 一次）。但**判断宿主还在不在不能只看这个时间戳**：
  长时间工具调用期间没有任何会话事件，文件可能停更，宿主其实好好地跑着 ——
  只看它就会误报「已断开」。悬浮框的规则是：静默超过 90s **且** `state.json` 里的
  宿主 `pid` 确实不存在，才显示 `DSH · 已断开（宿主无响应）`。

## 跨平台边界（重要）

- **悬浮框本体**：Linux 桌面 + GTK3 + X11。实测 UOS Desktop 20 Pro / Deepin、arm64。
- **MCP 服务**（`lib/mcp-server.js`）：纯 Node、零依赖、不碰 X11、不要求 python ——
  任何平台的 MCP 客户端都能接。它只读同一份 `state.json`，提供
  `get_status` / `list_sessions` / `wait_for_idle` 三个只读工具。

接入（任意 MCP 客户端）：

```json
{ "mcpServers": { "dsh-status": { "command": "node",
  "args": ["/绝对路径/lib/mcp-server.js"] } } }
```

## 打开 / 关闭

```bash
./install.sh                # 装插件（profile 的 live patch 热加载）
./install.sh --autostart    # 顺带装开机自启
./uninstall.sh              # 卸载
```

## 思考内容默认是关的

**默认不采集模型思考流**（`reasoning-delta`）。那是模型私有推理，可能夹带用户路径、
代码片段和对话内容，公开版本不替用户决定要不要落盘。

需要看的话显式打开：

```bash
DSH_OVERLAY_THINKING=1   # 给 dsh 进程设置这个环境变量
```

打开后思考原文会写进 `$DSH_HOME/status-overlay/state.json`（权限 0600，目录 0700）。
**这是本地文件，不会上传任何地方**；但截图给别人看之前记得先关掉。

## 排查

| 症状 | 处置 |
| --- | --- |
| 框不出现 | `~/.dsh/status-overlay/overlay.log` 看 python 报错；`/usr/bin/python3 -c "import gi"` 是否可用 |
| 状态文件不更新 | `~/.dsh/status-overlay/plugin.log` 看插件异常；确认补丁层里有挂载行，必要时重启 dsh |
| 不置顶 | 必须是 X11 后端：`GDK_BACKEND=x11`（overlay.py 自己会设默认值） |
| 想复位位置 | 删掉 `~/.dsh/status-overlay/ui.json`，框回到右下角 |

## 别踩的坑（都是实测踩出来的）

- **不要给这个窗口接 enter-notify/leave-notify**。窗口在静止指针底下 `move()` 时
  X 会补发交叉事件，收/展会每秒来回十几次。
- **位置没变就不要调 `move()`**，否则 `size-allocate` 会把 `place()` 变成死循环。
- **`get_size()` 在 `size-allocate` 之前返回旧值**，定位要用「要求的高度」兜底。
- **定时回调必须吞掉异常并返回 True**：GLib 会移除抛异常的定时源，框会永久冻结。
- **单例锁要先 flock 再截断写 pid**，否则并发时会把赢家的 pid 清空。
