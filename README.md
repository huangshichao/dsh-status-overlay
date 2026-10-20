# dsh-status-overlay

[English](#english) | 中文

给 [DeepSeek Harness](https://github.com/deepseek-ai)（DSH）用的桌面状态悬浮框：
**屏幕右下角一个置顶小窗，实时显示 DSH 此刻在做什么。**

## 为什么需要它

DSH 的界面在浏览器里。可一旦 Agent 开始 **computer use**（切窗口、点击、拖拽、
操作 Mind+、操作安卓设备…），浏览器就被目标窗口盖住了 —— 你既看不见进度，
也判断不了它是在思考、在动、还是已经卡住。

这个插件把状态放到**桌面上**，于是它不会被任何窗口盖住。

## 效果

| 状态 | 收起态 | 鼠标移上去（向上或向下展开） |
| --- | --- | --- |
| 思考中 | 蓝灯 + `DSH · 正在思考` + 计时 | 可见输出 / （可选）思考流 / 最近动作 |
| 执行中 | 橙灯 + `正在执行 · 操作桌面窗口` + 参数摘要 | 同上 + 动作列表 |
| 已完成 | 绿灯 + `DSH · 已完成` + 总耗时 | 同上，并弹一条系统通知 |
| 出错 | 红灯 + 错误摘要 | 完整错误 + 通知 |
| 宿主失联 | 灰灯 + `DSH · 已断开（宿主无响应）` | —— |

- **可以拖动**：按住药丸或展开后的文字区拖到任意位置，**位置会记住**（`ui.json`），
  重启后回到原地。
- **自动选方向**：拖到屏幕下方就向上展开、拖到上方就向下展开，被拖动的那个小条
  **永远待在指针底下不动**。
- 右上角 `✕` 隐藏，下次任务开始时自动回来。
- **多个会话并行时**：**只显示正在执行的那个** —— 有任务在跑就一定显示在跑的那个，
  完成的旧任务不会占位置；状态行会标出还有几个会话在跑。
  多个同时在跑时，把本机 `config.json` 的 `rotateSeconds` 设成秒数（如 `6`）就会在它们
  之间轮换；**鼠标移上去时轮换会冻结**，方便读完。
- **正文不会自己滚**：只在内容变化时刷新；你手动往上翻之后不会被弹回底部，重新展开才回到最新。
- **不会误报「断开」**：长时间执行时状态文件可能一时不更新，所以判定要**同时**满足
  「静默超过 90s」和「`state.json` 里的宿主进程确实不在了」，才显示 `已断开`。
- 窗口标题同步写着状态，任务切换器 / 无障碍工具都能读到。

## 适用环境

在本机实测通过，同时也是发布时的目标环境：

| 项 | 值 |
| --- | --- |
| 操作系统 | **统信 UOS Desktop 20 Pro（UnionTech OS）**，基于 Deepin |
| 架构 | **arm64 / aarch64**（代码本身与架构无关，x86_64 同样可用） |
| 内核 | 4.19.71-arm64-desktop（出厂内核） |
| 桌面 | D-UOS / Deepin，Wayland 会话 + **XWayland**（`DISPLAY=:0`） |
| 运行时 | Node.js ≥ 20（DSH 自带）、**系统 `/usr/bin/python3` + PyGObject(GTK3)**、`notify-send`、`libnotify` |

**要点：GTK 必须走 X11 后端。** Wayland 下 GTK 客户端自己声明的 `keep_above`
不算数；本机有 XWayland，所以 `overlay.py` 会自己 `setdefault('GDK_BACKEND','x11')`。

检查依赖：

```bash
/usr/bin/python3 -c "import gi; gi.require_version('Gtk','3.0'); from gi.repository import Gtk; print('GTK3 ok')"
which notify-send
```

> 注意：`which python3` 很可能指向不带 `gi` 的 conda/自建解释器。
> 自启项里写死 `/usr/bin/python3` 就是为了避开这个坑。

## 安装

```bash
git clone https://github.com/huangshichao/dsh-status-overlay.git
cd dsh-status-overlay
./install.sh                # 装插件（走 profile 的 live patch 热加载）
./install.sh --autostart    # 顺带装开机自启（不依赖 DSH 也会显示悬浮框）
```

需要写 `~/.dsh/profiles/web/` 与（可选）`~/.config/autostart/`。

卸载：

```bash
./uninstall.sh
```

## 不装插件，只想看效果

```bash
python3 tools/demo.py --state /tmp/dsh/state.json --phase thinking
GDK_BACKEND=x11 /usr/bin/python3 lib/overlay.py --state /tmp/dsh/state.json --lock /tmp/dsh/overlay.lock
python3 tools/demo.py --state /tmp/dsh/state.json --phase tool    # 换阶段：执行中
python3 tools/demo.py --state /tmp/dsh/state.json --phase done    # 完成 + 通知
python3 tools/demo.py --state /tmp/dsh/state.json --cycle         # 自动循环演示
```

## 架构

```
DSH 宿主进程
 └── dsh-status-overlay 插件（只读订阅）
       · session/event            持久事件：用户消息 / 工具调用 / 轮次起止
       · agent/assistant-stream   逐 token 的 text-delta（可选 reasoning-delta）
       · agent/status             running / idle
       · agent/created            区分主会话与子代理
       │  折成状态 JSON（节流 200ms、原子写）
       ▼
 $DSH_HOME/status-overlay/state.json   (0600)
       ▲  200ms 轮询
       │
  桌面悬浮框 overlay.py（系统 python3 + GTK3，X11 后端）
```

插件**只读**：不 append 任何会话事件、不注册工具、不注入提示词、不改 DSH 状态。
卸载它，DSH 行为完全不变，只是桌面上少一个盒子。

本仓库同时把使用说明注册成 DSH 技能（`status-overlay`，正文见 [`SKILL.md`](SKILL.md)），
所以它能出现在技能目录里按需加载。

## 隐私

- **默认不采集模型思考流**（`reasoning-delta`）。思考属于模型私有推理，可能夹带
  路径、代码片段和对话内容，公开版本不替用户决定要不要落盘。
  需要时显式打开：

  ```bash
  DSH_OVERLAY_THINKING=1    # 给 dsh 进程设置这个环境变量
  ```

- 采集到的东西**只写本地文件**，不上传任何地方：
  - `$DSH_HOME/status-overlay/state.json`（权限 0600，目录 0700）——当前任务文本、
    可见输出、工具名与参数摘要、状态；
  - `$DSH_HOME/status-overlay/plugin.log`、`overlay.log`——排查用，超 256KB 自动截断。
- 悬浮框**不联网**，只读本地文件、画窗口、调 `notify-send`。
- 插件日志里不会出现会话内容；要给别人看截图前，记得先关掉 `DSH_OVERLAY_THINKING`。

## 排查

| 症状 | 原因 / 处置 |
| --- | --- |
| 悬浮框不出现 | 看 `~/.dsh/status-overlay/overlay.log`；确认 `/usr/bin/python3 -c "import gi"` 可用 |
| 状态文件不更新 | 看 `~/.dsh/status-overlay/plugin.log`；确认补丁层里有挂载行，必要时重启 `dsh` |
| 窗口不置顶 | 必须是 X11 后端（`GDK_BACKEND=x11`）且 `DISPLAY` 可用 |
| 位置不对 | 删掉 `~/.dsh/status-overlay/ui.json`，框回到右下角 |
| 想彻底清掉 | `rm -rf ~/.dsh/status-overlay ~/.dsh/profiles/web/node_modules/dsh-status-overlay` |

## 自检

```bash
/usr/bin/python3 tools/test_geometry.py    # 展开/收起几何回归（不依赖真实鼠标）
```

断言：药丸在屏幕上**完全不动**（收起/展开/再展开三种状态一致）、整框不出工作区、
`poll()/tick()` 抛异常后仍返回 `True`（GLib 会移除抛异常的定时源，那会让框永久冻结）、
`leave-notify` 不再改变展开状态。

## 踩过的坑

这些都是实际调出来的，写在这里给下一个做桌面挂件的人省点时间：

- **别给窗口接 `enter-notify`/`leave-notify`**。`move()` 窗口时 X 会补发交叉事件，
  收/展每秒来回十几次 —— 表现是「框一直往上飘 + 光标闪烁」。
- **位置没变就不要 `move()`**。`size-allocate → place() → move()` 会变成 6 次/秒的
  死循环，合成器反复重判光标。
- **`get_size()` 在 `size-allocate` 之前返回旧值**，展开时要用「要求的高度」兜底，
  否则窗口会往下长到屏幕外。
- **定时回调必须吞异常并返回 `True`**，否则 GLib 直接移除定时源。
- **单例锁先 `flock` 再截断写 pid**，否则并发时会把赢家的 pid 清空。
- **自启项要用 `/usr/bin/python3` 全路径**：`PATH` 里的 `python3` 常常不带 `gi`。

## 变更记录

逐版本的改动见 [CHANGELOG.md](CHANGELOG.md)。

## License
MIT，见 [LICENSE](LICENSE)。

---

<a id="english"></a>
## English (short)

An always-on-top GTK3 desktop overlay for **DeepSeek Harness**: a small window in the
corner of the screen showing the running task, the tool being executed, and a completion
notification. It solves one specific problem — during *computer use* the agent covers the
browser, so you cannot see whether it is still working.

A read-only DSH plugin folds public host events into a JSON state file; a detached
Python/GTK3 process renders it. The plugin changes nothing about DSH and registers no
tools.

**Tested on UnionTech OS Desktop 20 Pro / Deepin, aarch64, with XWayland
(`GDK_BACKEND=x11` is required for `keep_above` to work on a Wayland session).**

Model reasoning (`reasoning-delta`) is **not captured by default**; opt in with
`DSH_OVERLAY_THINKING=1`. Everything stays on the local machine. MIT licensed.
