#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-status-overlay / lib/overlay.py

桌面右下角的置顶悬浮框：显示 DSH 此刻正在执行的任务。
  · 收起态：一个 380×72 的小条 —— 状态灯 + 状态词 + 正在执行的动作 + 计时。
  · 鼠标移上去：向上展开，显示逐 token 的思考进展 / 可见输出 / 最近动作。
  · 一轮结束：变成绿色「已完成」，并发一条系统通知。
  · 按住拖动可换位置（记住右下角锚点，所以展开时向上长，不会跑出屏幕）。

它只读 state.json（由 dsh-status-overlay 插件写），不做任何决策，也不碰 DSH。
单实例：用 flock 抢 --lock；抢不到就安静退出 —— 这让插件可以随时重复拉起。

刻意用 X11(GDK_BACKEND=x11) 跑：Wayland 下 GTK 客户端自己说的 keep_above 不算数，
而本机有 XWayland(DISPLAY=:0)，走 X11 才能真的置顶。
"""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import time

# 必须在 import gi 之前：GTK 在初始化时读 GDK_BACKEND。谁拉起它都要走 X11 ——
# Wayland 下 GTK 客户端自己声明的 keep_above 不算数，而本机有 XWayland。
# 放在这里而不是只靠调用方设环境变量，是为了让「开机自启」「插件拉起」「手动跑」
# 三条路径行为一致。
os.environ.setdefault('GDK_BACKEND', 'x11')

import gi

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

POLL_MS = 200          # state.json 轮询间隔
TICK_MS = 1000         # 计时刷新
MARGIN_X = 24
MARGIN_Y = 24
WIDTH = 400
COLLAPSED_H = 100   # 收起态的真实内容高度（实测），want_h 要和它一致才不会摆错
EXPANDED_H = 460
# 状态文件多久没动静才「怀疑」宿主失联。这只是第一道闸门：
# 真正判定还要再确认宿主 pid 已经不在了，否则长时间工具调用会被误报成断开。
STALE_SILENT_MS = 90000

# 排查用的开关：DSH_OVERLAY_DEBUG=1 时把定位决策写进日志。
DEBUG = os.environ.get('DSH_OVERLAY_DEBUG') == '1'
DEBUG_LOG = os.environ.get('DSH_OVERLAY_DEBUG_LOG', '/tmp/dsh-overlay-debug.log')


def dbg(message):
    if not DEBUG:
        return
    try:
        with open(DEBUG_LOG, 'a') as handle:
            handle.write('[%s] %s\n' % (time.strftime('%H:%M:%S'), message))
    except Exception:
        pass

# 每种阶段一套配色：(主色, 状态词)
THEMES = {
    'thinking': ('#4c8dff', '正在思考'),
    'tool':     ('#f0a020', '正在执行'),
    'running':  ('#4c8dff', '正在工作'),
    'done':     ('#35c26a', '已完成'),
    'error':    ('#ff5d5d', '出错'),
    'idle':     ('#7a8291', '空闲'),
}

CSS_TMPL = """
.card {{
  background-color: rgba(22, 24, 30, 0.95);
  border-radius: 13px;
  border: 1px solid alpha({accent}, 0.45);
}}
.brand {{ color: #8b93a7; font-size: 10px; font-weight: bold; }}
.title {{ color: {accent}; font-size: 13px; font-weight: bold; }}
.task  {{ color: #e8ecf4; font-size: 12px; }}
.meta  {{ color: #8b93a7; font-size: 11px; }}
.hint  {{ color: #6d7488; font-size: 10px; }}
.close {{ color: #6d7488; font-size: 12px; padding: 0 2px; }}
.close:hover {{ color: #ff5d5d; }}
.detail-title {{ color: {accent}; font-size: 11px; font-weight: bold; }}
.detail {{ color: #c8cfdd; font-size: 11px; background-color: rgba(0,0,0,0); }}
"""


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return '%ds' % seconds
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return '%dm%02ds' % (minutes, sec)
    hours, minutes = divmod(minutes, 60)
    return '%dh%02dm' % (hours, minutes)


def ellipsize(text, limit):
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit] + '…'


class Overlay(object):
    def __init__(self, state_path, lock_path):
        self.state_path = state_path
        self.lock_path = lock_path
        self.state = {}
        self.config = self.load_config()
        self.rotate_seconds = float(self.config.get('rotateSeconds') or 0)
        self.sig = None
        self.expanded = False
        self.hidden = False
        self.anchor_x = None      # 窗口右下角的横坐标
        self.anchor_y = None      # 窗口右下角的纵坐标
        self.want_h = COLLAPSED_H  # 我们「要求」的高度，不依赖可能滞后的 get_size()
        self._grow_up = True       # 当前是向上长（药丸贴底）还是向下长（药丸贴顶）
        self.drag = None
        self.moved = False
        self.last_done_at = 0
        self.first_render = True
        self.last_error = ''
        self.was_running = False

        self.win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.win.set_title('DSH 状态')
        self.win.set_decorated(False)
        self.win.set_resizable(False)
        self.win.set_keep_above(True)
        self.win.set_skip_taskbar_hint(True)
        self.win.set_skip_pager_hint(True)
        self.win.set_focus_on_map(False)
        self.win.set_accept_focus(False)
        try:
            self.win.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        except Exception:
            pass
        self.win.set_app_paintable(True)
        screen = self.win.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self.win.set_visual(visual)
        self.win.add_events(Gdk.EventMask.BUTTON_PRESS_MASK |
                            Gdk.EventMask.BUTTON_RELEASE_MASK |
                            Gdk.EventMask.POINTER_MOTION_MASK |
                            Gdk.EventMask.ENTER_NOTIFY_MASK |
                            Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.win.connect('destroy', Gtk.main_quit)
        # 刻意**不**接 enter-notify/leave-notify。
        # place() 会 move() 窗口，而窗口在静止的指针底下移动时 X 会补发
        # Enter/Leave 交叉事件；于是 on_leave 收起 → 窗口缩小 → on_enter 展开
        # → …… 每秒来回十几次。表现就是「框一直往上飘 + 光标一直在变」。
        # 悬浮状态只由指针坐标轮询决定（每 200ms 一次，见 poll()）。
        self.win.connect('button-press-event', self.on_press)
        self.win.connect('button-release-event', self.on_release)
        self.win.connect('motion-notify-event', self.on_motion)

        self.css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            screen, self.css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.apply_theme('#4c8dff')

        self.build()

        self.win.connect('realize', lambda *_: self.apply_cursor())
        self.win.show_all()
        self.detail.hide()
        self.sep.hide()
        self.win.set_size_request(WIDTH, COLLAPSED_H)
        self.load_anchor()
        self.place()
        self.win.connect('size-allocate', lambda *_: self.place())
        # 子控件（TextView 等）的 GdkWindow 要等 show_all 之后才存在，那时再钉一次光标。
        GLib.timeout_add(300, lambda: (self.apply_cursor(), False)[1])

        GLib.timeout_add(POLL_MS, self.poll)
        GLib.timeout_add(TICK_MS, self.tick)

    # ---------- UI ----------

    def build(self):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.get_style_context().add_class('card')
        card.set_border_width(11)
        self.win.add(card)

        # ── 布局顺序是关键 ────────────────────────────────────────────────
        # 详情在上、常驻的「药丸」在下。窗口锚在右下角、展开时向上长，
        # 药丸放在最下面它就**原地不动**，指针始终停在同一个东西上。
        # 反过来（药丸在上）时，展开会把药丸往上顶 360px：指针下面从药丸
        # 变成 TextView，用户看到的就是「框飘到上面去了 + 光标一直在变」。
        self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.detail_title = Gtk.Label(label='思考进展')
        self.detail_title.get_style_context().add_class('detail-title')
        self.detail_title.set_xalign(0.0)
        self.detail.pack_start(self.detail_title, False, False, 0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_size_request(-1, EXPANDED_H - 210)
        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_can_focus(False)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_left_margin(2)
        self.view.set_right_margin(2)
        self.view.get_style_context().add_class('detail')
        # TextView 自己会在 realize 时把光标设成 I 形；在它 realize/map 之后再钉一次，
        # 指针滑到文字上才不会变成「输入指针」。
        self.view.connect('realize', lambda *_: self.apply_cursor())
        self.view.connect('map', lambda *_: self.apply_cursor())
        # 展开后整块文字区也能拖动（否则只有底部那一条能拖，用户会以为拖不动）。
        # TextView 的默认处理是 RUN_LAST，所以这里的 connect 先跑；
        # on_press 返回 True 把事件吃掉，顺带也不会触发文本选择。
        self.view.connect('button-press-event', self.on_press)
        self.view.connect('button-release-event', self.on_release)
        self.view.connect('motion-notify-event', self.on_motion)
        scroll.add(self.view)
        self.detail.pack_start(scroll, True, True, 0)

        self.footer = Gtk.Label(label='')
        self.footer.get_style_context().add_class('hint')
        self.footer.set_xalign(0.0)
        self.detail.pack_start(self.footer, False, False, 0)
        card.pack_start(self.detail, True, True, 0)

        self.sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        card.pack_start(self.sep, False, False, 6)

        # ── 药丸（常驻，永远贴在右下角）──────────────────────────────────
        self.pill = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.dot = Gtk.Label(label='●')
        self.title = Gtk.Label(label='DSH')
        self.title.get_style_context().add_class('title')
        self.spinner = Gtk.Spinner()
        self.meta = Gtk.Label(label='')
        self.meta.get_style_context().add_class('meta')
        self.close_btn = Gtk.Label(label='✕')
        self.close_btn.get_style_context().add_class('close')
        self.close_btn.set_tooltip_text('隐藏（下次开始任务时自动回来）')
        close_ev = Gtk.EventBox()
        close_ev.add(self.close_btn)
        close_ev.connect('button-press-event', self.on_close)
        header.pack_start(self.dot, False, False, 0)
        header.pack_start(self.title, False, False, 0)
        header.pack_start(self.spinner, False, False, 0)
        header.pack_start(self.meta, True, True, 0)
        header.pack_end(close_ev, False, False, 0)
        self.pill.pack_start(header, False, False, 0)

        self.task = Gtk.Label(label='等待 DSH 活动…')
        self.task.get_style_context().add_class('task')
        self.task.set_xalign(0.0)
        self.task.set_ellipsize(Pango.EllipsizeMode.END)
        self.task.set_max_width_chars(44)
        self.task.set_line_wrap(False)
        self.pill.pack_start(self.task, False, False, 4)

        self.tool = Gtk.Label(label='')
        self.tool.get_style_context().add_class('hint')
        self.tool.set_xalign(0.0)
        self.tool.set_ellipsize(Pango.EllipsizeMode.END)
        self.tool.set_max_width_chars(48)
        self.pill.pack_start(self.tool, False, False, 1)

        card.pack_start(self.pill, False, False, 0)

    def apply_cursor(self):
        """把窗口（含 TextView）的光标钉成普通箭头。

        不设置的话，指针滑到 TextView 上会变成文本 I 形光标 —— 这正是
        用户报的「鼠标的光标一直在变动」。
        """
        try:
            display = self.win.get_display()
            cursor = Gdk.Cursor.new_from_name(display, 'default')
            for widget in (self.win, self.view, self.task, self.tool, self.pill):
                window = widget.get_window()
                if window is not None:
                    window.set_cursor(cursor)
        except Exception as error:
            dbg('apply_cursor failed: %s' % error)

    def apply_theme(self, accent):
        self.css.load_from_data(CSS_TMPL.format(accent=accent).encode('utf-8'))

    # ---------- 位置 ----------

    def workarea(self):
        screen = self.win.get_screen()
        display = screen.get_display()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        return monitor.get_workarea()

    def ui_config_path(self):
        """和 state.json 放一起；那个目录一定是可写的（锁文件就写在那里）。"""
        return os.path.join(os.path.dirname(os.path.abspath(self.state_path)), 'ui.json')

    def load_anchor(self):
        """恢复上次拖到的位置（右下角锚点），这样「放到哪里」重启后还算数。"""
        try:
            with open(self.ui_config_path(), 'r') as handle:
                data = json.load(handle)
            x, y = data.get('anchor_x'), data.get('anchor_y')
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                self.anchor_x, self.anchor_y = int(x), int(y)
                dbg('anchor restored: %s,%s' % (self.anchor_x, self.anchor_y))
        except Exception:
            pass

    def save_anchor(self):
        try:
            path = self.ui_config_path()
            tmp = path + '.tmp'
            with open(tmp, 'w') as handle:
                json.dump({'anchor_x': self.anchor_x, 'anchor_y': self.anchor_y}, handle)
            os.replace(tmp, path)
            dbg('anchor saved: %s,%s' % (self.anchor_x, self.anchor_y))
        except Exception as error:
            dbg('save_anchor failed: %s' % error)

    def reorder_pill(self, grow_up):
        """决定药丸在卡片里的位置：向上长时贴底，向下长时贴顶。

        药丸必须永远挨着**不动的那条边**。它是指针底下那个东西；让它跟着窗口跑，
        就又回到了「框飘走了 + 光标乱变」。
        """
        try:
            card = self.pill.get_parent()
            if card is None:
                return
            if grow_up:
                card.reorder_child(self.detail, 0)
                card.reorder_child(self.sep, 1)
                card.reorder_child(self.pill, 2)
            else:
                card.reorder_child(self.pill, 0)
                card.reorder_child(self.sep, 1)
                card.reorder_child(self.detail, 2)
            dbg('reorder_pill grow_up=%s' % grow_up)
        except Exception as error:
            dbg('reorder_pill failed: %s' % error)

    def clamp_anchor(self, area, width):
        """把锚点收进工作区，保证**收起态**也完整可见。

        锚点是收起态的右下角。如果它离顶边太近，place() 里的兜底 clamp 会把窗口
        挪下来，实际位置和锚点就对不上了 —— 展开/收起时又会原地跳一下。
        所以约束加在锚点身上，而不是事后修位置。
        """
        self.anchor_x = max(area.x + width, min(self.anchor_x, area.x + area.width))
        self.anchor_y = max(area.y + COLLAPSED_H, min(self.anchor_y, area.y + area.height))

    def place(self):
        area = self.workarea()
        if self.anchor_x is None:
            self.anchor_x = area.x + area.width - MARGIN_X
            self.anchor_y = area.y + area.height - MARGIN_Y
        width, height = self.win.get_size()
        if width <= 1:
            return
        # get_size() 在 size-allocate 之前返回的还是旧高度。收起方向直接用旧值会把
        # 窗口先停在展开位置、下一帧再往下跳；所以收起取 want_h，展开取两者较大者。
        height = self.want_h if self.want_h < height else max(height, self.want_h)
        self.clamp_anchor(area, width)

        # 向上长需要上方留得下整个展开高度；留不下就反过来向下长。
        # 不这么分方向的话，把框拖到屏幕上方时会「想往上长但被工作区顶住」，
        # 于是展开/收起每次都在原地跳一大截 —— 看着就是闪动。
        grow_up = (self.anchor_y - EXPANDED_H) >= area.y
        if grow_up:
            y = self.anchor_y - height          # 底边钉住，向上长
        else:
            y = self.anchor_y - COLLAPSED_H     # 顶边钉住，向下长
        x = int(self.anchor_x - width)
        y = int(y)
        y = max(area.y, min(y, area.y + area.height - height))

        if self.expanded and grow_up != self._grow_up:
            self._grow_up = grow_up
            self.reorder_pill(grow_up)

        dbg('place size=%dx%d want_h=%d grow_up=%s anchor=(%s,%s) -> (%d,%d)' % (
            width, height, self.want_h, grow_up, self.anchor_x, self.anchor_y, x, y))
        # 只有位置真的变了才 move()。
        # move() 会触发 size-allocate → 又回到 place()，位置不变时就是一个死循环
        # （实测约 6 次/秒）。窗口被反复重新配置，合成器就会反复重新判定「指针下
        # 是哪个控件」，光标于是在箭头和 I 形之间来回跳 —— 用户看到的就是光标闪烁。
        current = self.win.get_position()
        if (x, y) != (int(current[0]), int(current[1])):
            self.win.move(x, y)

    # ---------- 悬浮 / 拖动 / 隐藏 ----------

    def pointer_state(self):
        """返回 (px, py, wx, wy, ww, wh)；取不到返回 None。给判定和排查共用。"""
        try:
            display = self.win.get_display()
            pointer = display.get_default_seat().get_pointer()
            _screen, px, py = pointer.get_position()
            wx, wy = self.win.get_position()
            ww, wh = self.win.get_size()
            return (px, py, wx, wy, ww, wh)
        except Exception:
            return None

    def pointer_inside(self):
        """指针是否在窗口矩形内。

        XWayland 下 enter/leave 事件的投递并不可靠（尤其是从 Wayland 原生窗口
        移回来的时候），所以悬浮状态以**真实指针坐标**为准，每 200ms 校准一次；
        enter/leave 只作为即时响应，不作为唯一依据。
        """
        state = self.pointer_state()
        if state is None:
            return False
        px, py, wx, wy, ww, wh = state
        return wx <= px < wx + ww and wy <= py < wy + wh

    def button_held(self):
        """当前是否真的按着鼠标键。

        `on_press` / `on_release` 不保证配对：松开的事件可能落在别的窗口上，
        那样 `self.drag` 会永久为真，而它一为真就关掉了「移开即收起」。
        所以每轮都拿真实按键状态校对。

        GTK3 里拿设备状态要用 `Gdk.Window.get_device_position()`；
        `Gdk.Device.get_state()` 在这个版本并不存在（踩过，异常被吞掉后
        这个函数会永远返回 False，看着像能用其实没有）。
        """
        try:
            gdk_window = self.win.get_window()
            if gdk_window is None:
                return False
            pointer = self.win.get_display().get_default_seat().get_pointer()
            _win, _x, _y, mask = gdk_window.get_device_position(pointer)
            buttons = (Gdk.ModifierType.BUTTON1_MASK
                       | Gdk.ModifierType.BUTTON2_MASK
                       | Gdk.ModifierType.BUTTON3_MASK)
            return bool(mask & buttons)
        except Exception as error:
            dbg('button_held failed: %s' % error)
            return False

    def set_expanded(self, expanded):
        if expanded == self.expanded:
            return
        self.expanded = expanded
        if expanded:
            self.detail.show_all()
            self.sep.show()
            self.want_h = EXPANDED_H
        else:
            self.detail.hide()
            self.sep.hide()
            self.want_h = COLLAPSED_H
        dbg('set_expanded(%s) want_h=%d size_before=%s' % (
            expanded, self.want_h, self.win.get_size()))
        self.win.set_size_request(WIDTH, self.want_h)
        self.place()
        # 分配是在本轮事件循环之后才发生的：那时再定位一次，否则会按旧高度摆窗，
        # 结果就是展开的窗口从原来的顶边往**下**长到屏幕外。
        GLib.idle_add(self.place)

    def on_press(self, _widget, event):
        if event.button == 1:
            self.drag = (event.x_root, event.y_root, self.win.get_position(), self.anchor_x, self.anchor_y)
            self.moved = False
            return True   # 吃掉按下事件：不让 TextView 开始选字，拖动才顺
        return False

    def on_motion(self, _widget, event):
        if not self.drag:
            return False
        start_x, start_y, pos, anchor_x, anchor_y = self.drag
        dx = event.x_root - start_x
        dy = event.y_root - start_y
        if abs(dx) > 3 or abs(dy) > 3:
            self.moved = True
        self.win.move(int(pos[0] + dx), int(pos[1] + dy))
        self.anchor_x = anchor_x + dx
        self.anchor_y = anchor_y + dy
        return False

    def on_release(self, _widget, event):
        if self.drag and not self.moved and event.button == 1:
            self.set_expanded(not self.expanded)
        if self.drag and self.moved:
            self.save_anchor()
        self.drag = None
        return False

    def on_close(self, _widget, _event):
        self.hidden = True
        self.win.hide()
        return True

    # ---------- 状态读取 ----------

    def poll(self):
        # 整个回调必须吞掉任何异常并**始终返回 True**：GLib 在回调抛异常时会
        # 直接移除这个定时源，悬浮框从此永久冻结 —— 实测卡在展开态就是这么来的。
        try:
            try:
                stat = os.stat(self.state_path)
                sig = (stat.st_mtime_ns, stat.st_size)
                if sig != self.sig:
                    self.sig = sig
                    with open(self.state_path, 'r') as handle:
                        self.state = json.load(handle)
            except (IOError, OSError, ValueError):
                pass
            try:
                self.render()
            except Exception as error:
                dbg('render failed: %s' % error)
                self.title.set_text('DSH')
                self.meta.set_text(str(error)[:60])
            # 用真实按键状态校对拖拽：松开事件一旦丢失，self.drag 会永久为真，
            # 而它一为真，「移开即收起」就再也不会发生。
            if self.drag and not self.button_held():
                dbg('clearing stale drag flag')
                self.drag = None
            # 悬浮状态以真实指针位置为准（每 200ms 校准），不依赖 enter/leave。
            self._poll_n = getattr(self, '_poll_n', 0) + 1
            if not self.drag and not self.hidden:
                inside = self.pointer_inside()
                # 每 ~2.4s 记一次判定依据，排查「为什么不收起」时不用猜。
                if self._poll_n % 12 == 0:
                    dbg('poll pointer_state=%s inside=%s expanded=%s'
                        % (self.pointer_state(), inside, self.expanded))
                if inside != self.expanded:
                    dbg('hover inside=%s -> expand=%s' % (inside, inside))
                    self.set_expanded(inside)
                # 每隔 ~2s 再钉一次光标，兜住任何一次被 GTK 重置的情况。
                if self._poll_n % 10 == 0:
                    self.apply_cursor()
            elif self._poll_n % 12 == 0:
                dbg('hover check SKIPPED: drag=%s hidden=%s button_held=%s'
                    % (bool(self.drag), self.hidden, self.button_held()))
        except Exception as error:
            dbg('poll failed: %s' % error)
        return True

    def load_config(self):
        """读和插件共用的 config.json（可选项都在这里，不进仓库）。

        目前支持：
          captureReasoning  插件读：要不要采集思考流
          rotateSeconds     悬浮框读：多个会话并行时每隔几秒轮换显示
        """
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(self.state_path)), 'config.json')
            with open(path, 'r') as handle:
                return json.load(handle) or {}
        except Exception:
            return {}

    def running_sessions(self):
        """有任务文本、且正在跑的会话，按最近活动排序。"""
        sessions = self.state.get('sessions') or {}
        running = [s for s in sessions.values()
                   if s.get('status') == 'running' and (s.get('task') or '').strip()]
        running.sort(key=lambda s: s.get('lastActivity') or 0, reverse=True)
        return running

    def current(self):
        sessions = self.state.get('sessions') or {}
        running = self.running_sessions()

        # 铁律：**只要有任务在跑，就显示正在执行的那个**。
        # 完成的旧任务不许占位置 —— 即使 primary 指向的是它。
        if running:
            # 多个并行时才轮换；展开时冻结，方便你读完再换。
            if self.rotate_seconds > 0 and not self.expanded and len(running) > 1:
                index = int(time.time() // self.rotate_seconds) % len(running)
                return running[index]
            return running[0]          # running 已按最近活动排序

        # 一个都没在跑：回到 primary，让「已完成」留在眼前。
        primary = self.state.get('primary')
        entry = sessions.get(primary) if primary else None
        if entry is None and sessions:
            entry = max(sessions.values(), key=lambda item: item.get('lastActivity') or 0)
        return entry or {}

    def host_alive(self):
        """state.json 里记录的宿主 pid 是否还在。

        - `True`  进程存在（可能没权限发信号，也算存在）
        - `False` 进程确实没了
        - `None`  拿不到 pid，不做判断

        用杀 0 号信号探测，不会真的影响进程。
        """
        try:
            pid = int(self.state.get('pid'))
        except (TypeError, ValueError):
            return None
        if pid <= 0:
            return None
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except Exception:
            return None

    def render(self):
        entry = self.current()
        phase = entry.get('phase') or 'idle'
        accent, word = THEMES.get(phase, THEMES['idle'])
        self.apply_theme(accent)

        running = entry.get('status') == 'running'

        # 「断开」只在**确认真连不上**时才说。
        # 只按 updatedAt 判断会误报：长时间工具调用期间没有任何会话事件，
        # 插件可能不写盘，文件停更但宿主好好地跑着。
        # 所以先看静默时长，再拿 state.json 里的宿主 pid 做存在性检查。
        updated_at = self.state.get('updatedAt') or 0
        silent_ms = (time.time() * 1000 - updated_at) if updated_at else 0
        stale = bool(running and silent_ms > STALE_SILENT_MS and self.host_alive() is False)
        if stale:
            running = False
            accent, word = THEMES['idle']

        # 新一轮开始：如果用户之前点了隐藏，把它请回来。
        if running and not self.was_running and self.hidden:
            self.hidden = False
            self.win.show_all()
            if not self.expanded:
                self.detail.hide()
            self.place()
        self.was_running = running

        if stale:
            headline = 'DSH · 已断开（宿主无响应）'
        elif phase == 'tool':
            label = entry.get('toolLabel') or entry.get('tool') or '工具'
            headline = '正在执行 · %s' % label
        else:
            headline = 'DSH · %s' % word
        self.title.set_text(headline)
        # 同时写进 X11 窗口标题：任务切换器/无障碍工具能看到，自动化测试也能读回来。
        self.win.set_title('DSH 状态 — %s' % headline)

        if running:
            self.spinner.start()
        else:
            self.spinner.stop()

        started = entry.get('startedAt') or 0
        done_at = entry.get('doneAt') or 0
        if running and started:
            elapsed = (time.time() * 1000 - started) / 1000.0
        elif done_at and started:
            elapsed = (done_at - started) / 1000.0
        else:
            elapsed = 0
        bits = []
        if elapsed:
            bits.append(fmt_duration(elapsed))
        if not running and done_at:
            bits.append(time.strftime('%H:%M', time.localtime(done_at / 1000.0)))
        if entry.get('computerUse'):
            bits.append('桌面操作')
        active = self.state.get('activeCount') or 0
        if active > 1:
            bits.append('%d 个会话' % active)
        self.meta.set_text('  '.join(bits))

        task = entry.get('task') or ''
        if not task:
            task = '等待你的下一条指令'
        self.task.set_text(ellipsize(task, 90))

        tool_name = entry.get('tool') or ''
        hint = entry.get('toolHint') or ''
        if phase == 'tool' and tool_name:
            self.tool.set_text('▸ %s  %s' % (entry.get('toolLabel') or tool_name, ellipsize(hint, 60)))
        elif phase == 'error' and entry.get('error'):
            self.tool.set_text('⚠ ' + ellipsize(entry.get('error'), 80))
        elif phase == 'done':
            self.tool.set_text('✓ ' + ellipsize(entry.get('task') or '任务已完成', 70))
        else:
            self.tool.set_text('')

        self.render_detail(entry)

        # 完成通知：doneAt 变了就报一次。
        # 首次渲染除外 —— 否则每次悬浮框/DSH 重启，都会把上一轮的「已完成」
        # 当成新完成再弹一次。
        if done_at and done_at != self.last_done_at:
            self.last_done_at = done_at
            if not self.first_render:
                self.notify(entry, phase)
        self.first_render = False

    def render_detail(self, entry):
        thinking = (entry.get('thinking') or '').strip()
        stream = (entry.get('streamText') or '').strip()
        steps = entry.get('steps') or []

        parts = []
        if entry.get('task'):
            parts.append('【任务】\n' + entry['task'])
        if thinking:
            parts.append('【思考】\n' + thinking)
        if stream:
            parts.append('【输出】\n' + stream)
        if steps:
            lines = []
            for index, step in enumerate(steps[-8:], 1):
                mark = '✗' if step.get('failed') else '·'
                suffix = ('  ' + step['hint']) if step.get('hint') else ''
                lines.append('%d. %s %s%s' % (index, mark, step.get('label') or step.get('name'), suffix))
            parts.append('【最近动作】\n' + '\n'.join(lines))
        if not parts:
            parts = ['（还没有活动。开始一个任务后，这里会实时显示思考与动作。）']

        body = '\n\n'.join(parts)
        buffer = self.view.get_buffer()
        buffer.set_text(body)
        end = buffer.get_end_iter()
        self.view.scroll_to_mark(buffer.create_mark(None, end, False), 0.0, False, 0.0, 0.0)

        self.detail_title.set_text('思考进展' if thinking else '执行进展')
        total = len(thinking) + len(stream)
        self.footer.set_text('思考 %d 字 · 动作 %d 步%s' % (
            total, len(steps),
            '' if self.was_running else '（已结束）'))

    def notify(self, entry, phase):
        ok = phase != 'error'
        title = 'DSH 任务完成' if ok else 'DSH 任务出错'
        body = ellipsize(entry.get('task') or '', 140)
        if not ok and entry.get('error'):
            body = ellipsize(entry['error'], 140)
        icon = 'dialog-information' if ok else 'dialog-error'
        try:
            subprocess.Popen(
                ['notify-send', '-a', 'DSH', '-u', 'normal', '-i', icon, title, body or ' '],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def tick(self):
        # 计时需要每秒刷新，即使 state.json 没变。同样绝不能抛出去。
        try:
            if self.state.get('sessions'):
                self.render()
        except Exception as error:
            dbg('tick failed: %s' % error)
        return True


def acquire_lock(path):
    """抢单例锁，并把 pid 写进锁文件。

    顺序很关键：**先 flock，抢到了才截断并写 pid**。
    如果像以前那样用 `open(path, 'w')`，截断发生在抢锁之前 —— 并发的第二个实例
    会先把赢家写好的 pid 清空，然后抢锁失败退出，留下一个**空的锁文件**。
    而锁文件正是 spawn.js 判断「悬浮框还活着吗」和 uninstall.sh 结束进程的依据，
    空文件会让两者都失效（实测确实出现过 0 字节）。
    """
    handle = open(path, 'a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main():
    parser = argparse.ArgumentParser(description='DSH 状态悬浮框')
    parser.add_argument('--state', default=os.path.expanduser('~/.dsh/status-overlay/state.json'))
    parser.add_argument('--lock', default=os.path.expanduser('~/.dsh/status-overlay/overlay.lock'))
    args = parser.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.state)), exist_ok=True)
    lock = acquire_lock(args.lock)
    if lock is None:
        return 0  # 已经有一个在跑

    overlay = Overlay(args.state, args.lock)  # noqa: F841
    Gtk.main()
    return 0


if __name__ == '__main__':
    sys.exit(main())
