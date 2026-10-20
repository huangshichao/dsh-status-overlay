# 变更记录

本项目的所有重要改动都记在这里。

## [0.1.0] — 2026-10-12

首个版本：DSH 的桌面状态悬浮框。

### 新增

- **DSH 插件**（`lib/index.js`）：只读订阅 `session/event`、`agent/assistant-stream`、
  `agent/status`、`agent/created`，把实时状态折成一份 JSON；不注册工具、不改 DSH 状态、
  不 append 会话事件。卸载后 DSH 行为完全不变。
- **状态折叠**（`lib/store.js`）：节流 + 原子写（`.tmp` → rename），200ms 一次。
- **悬浮框**（`lib/overlay.py`）：Python + GTK3，置顶、无边框、跳过任务栏；
  收起态是一颗「药丸」，悬停向上/向下展开；完成与出错发系统通知。
- **进程托管**（`lib/spawn.js`）：detached 拉起 + `flock` 单例，看门狗每 20s 自愈。
- **开机自启**：`install.sh --autostart` 写入 XDG autostart 项。
- **技能**：`SKILL.md` + `lib/skill.js`，通过 `ctx.skills.register()` 注册为
  `status-overlay`。
- **演示与测试**：`tools/demo.py`（不依赖 DSH 的仿真状态）、`tools/test_geometry.py`。

### 已知取舍

- 强制 `GDK_BACKEND=x11`：Wayland 下 GTK 客户端声明的 `keep_above` 不生效，
  必须走 XWayland 才能真置顶。
- 模型思考流（`reasoning-delta`）**默认不采集**；需要时用 `DSH_OVERLAY_THINKING=1`
  或 `config.json` 的 `captureReasoning` 显式打开。

## [0.1.0-fix.1] — 2026-10-20

实测反馈修的一批问题。

### 修复

- **光标闪烁 / 框往上飘**：窗口在静止指针底下 `move()` 时，X 会补发
  Enter/Leave 交叉事件，`on_leave` 收起 → 窗口缩小 → `on_enter` 展开 → 每秒十几次。
  去掉了 `enter-notify`/`leave-notify` 处理器，悬浮改为纯指针坐标轮询。
- **移动死循环**：`place()` 位置没变也调 `move()`，触发 `size-allocate` → 回到
  `place()`，约 6 次/秒。改为位置变了才移动。
- **药丸被顶离指针**：药丸原本排在卡片顶部，展开时被顶走 360px，指针下面从药丸变成
  文字区（光标也变成 I 形）。改为药丸永远贴着「不动的那条边」。
- **拖到屏幕上方后原地跳**：锚点算出的 y 被工作区 clamp，展开/收起每次跳一大截。
  改为上方空间不足时自动向下展开，并把约束加在锚点上。
- **单例锁竞态**：`open(path,'w')` 在 `flock` 之前截断，并发时会把赢家的 pid 清空。
  改为先抢锁再截断写 pid。
- **文件描述符泄漏**：`spawn.js` 的日志 fd 从不关闭，看门狗每 20s 漏一个。
  改为 `finally` 里关闭，并先检查 pid 存活再决定要不要拉起。
- **卸载残留**：`uninstall.sh` 的正则少了一个 `- `，一次都匹配不上，卸载后挂载行还在。
  修正并加 YAML 校验；改用锁文件里的 pid 结束进程（`wmctrl -c` 关不掉这种窗口）。

### 新增

- **拖动 + 位置记忆**：按住药丸或展开后的文字区都能拖，位置写进 `ui.json`，
  重启后恢复。
- **方向自适应**：拖到屏幕下方向上展开、拖到上方向下展开。
- **定时器健壮性**：`poll()`/`tick()` 吞掉所有异常并始终返回 `True`
  （GLib 会移除抛异常的定时源，那会让悬浮框永久冻结）。
- **`config.json` 本机开关**：`captureReasoning`、`rotateSeconds`，
  优先级为 环境变量 > config.json > 默认值。

### 测试

- `test_geometry.py` 扩展为两个方向，断言药丸屏幕坐标在收起/展开/再展开下完全一致、
  整框不出工作区，并守住「不能再接 enter/leave 处理器」。

## [0.1.0-fix.2] — 2026-10-20

### 新增

- **多会话轮换**：`config.json` 的 `rotateSeconds`，收起态下每 N 秒在正在运行的会话之间
  轮换；展开时冻结。默认 0（关闭）。

### 修复

- **显示了已完成的任务**：原来只要存在 running 会话就走轮换分支，但「只有一个在跑」时
  会落回 `primary` —— 而 `primary` 可能是刚完成的会话。
  现在：**有任务在跑就一定显示在跑的那个**；一个都没在跑才回到 `primary`，
  让「已完成」留在眼前。
- 新增 `test_selection.py` 钉住这条规则（5 个场景）。

## [0.1.0-fix.3] — 2026-10-20

### 修复

- **长时间执行误报「已断开」**，两个原因叠在一起：
  1. 插件心跳只写 `this.dirty = true`，**没有调度 flush**。长时间工具调用期间没有
     任何会话事件，`updatedAt` 停在最后一个事件上 → 60s 后被判失联。
     抽出 `schedule()`，`touch()` 与 `heartbeat()` 都走它。
  2. 悬浮框只看 `updatedAt` 超过 60s。文件不动可能只是插件没写盘，宿主活得好好的。
     改为：静默 >90s **且** `state.json` 里的宿主 `pid` 确实不存在
     （`os.kill(pid, 0)` 探活）才判定断开。
- 新增 `test_heartbeat.mjs`（含反向验证）与 `test_stale.py`
  （覆盖「静默 10 分钟 + 宿主仍存活」这一实际场景）。

### 文档

- README / SKILL.md 修正「超过 60s 不动说明 DSH 已经不在跑了」这句错误说明。

## [0.1.0-fix.4] — 2026-10-20

### 修复

- **鼠标一放上去就「一直在滚」**：`render_detail()` 每 200ms 无条件重建正文 buffer 并
  `scroll_to_mark(底部)`，连收起时也照做。于是手动滚动会被立刻弹回，看起来像一直在
  触发滚动。现在：
  - 收起时不碰 buffer；
  - 内容没变不碰 buffer（不再每 200ms 重建一次）；
  - 只有「刚展开」或「本来就停在底部」才自动跟随 —— 用户往上翻之后不再抢滚动条。
- 新增 `tools/test_scroll.py`，用 `set_text` / `scroll_to_mark` 的调用计数把这三条钉住
  （6 个场景：收起、首次展开、无变化、上滚+变化、再渲染、收起再展开）。
