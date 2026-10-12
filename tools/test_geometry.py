#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-status-overlay / tools/test_geometry.py

悬浮框展开/收起的回归测试 —— 不依赖真实鼠标位置。

守的是用户报过的几个症状：
  ① 鼠标放上去，**提示框自己往上飘**（连指针下面的东西都换了）；
  ② 移开后**收不回来**（卡在展开态）；
  ③ 拖到屏幕上方后，展开/收起**原地闪动**。

核心不变量：**药丸（常驻那条）在屏幕上必须一动不动** —— 它就是指针底下那个东西。
窗口向上长时药丸贴底、向下长时药丸贴顶，两种情况都要成立。

用法：
    /usr/bin/python3 tools/test_geometry.py                        # 应通过
    DSH_TEST_OLD_LAYOUT=1 /usr/bin/python3 tools/test_geometry.py   # 复现旧布局，应失败
退出码 0 = 通过。
"""

import importlib.util
import json
import os
import sys
import tempfile

os.environ.setdefault('GDK_BACKEND', 'x11')

import gi  # noqa: E402

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OVERLAY = os.environ.get('DSH_OVERLAY_PATH') or os.path.join(HERE, '..', 'lib', 'overlay.py')


def load_overlay():
    spec = importlib.util.spec_from_file_location('overlay_under_test', OVERLAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    ov = load_overlay()
    work = tempfile.mkdtemp(prefix='dsh-overlay-geom-')
    state = os.path.join(work, 'state.json')
    with open(state, 'w') as handle:
        json.dump({'v': 1, 'updatedAt': 2 ** 40, 'primary': 's', 'activeCount': 1,
                   'sessions': {'s': {'id': 's', 'phase': 'thinking', 'status': 'running',
                                      'task': '几何回归测试', 'thinking': 'x' * 200,
                                      'steps': [], 'startedAt': 0, 'lastActivity': 2 ** 40}}},
                  handle)

    overlay = ov.Overlay(state, os.path.join(work, 'lock'))
    # 打桩：让 poll 的指针校准成为「不改状态」的空操作，测试与真实鼠标彻底解耦。
    overlay.pointer_inside = lambda: overlay.expanded

    if os.environ.get('DSH_TEST_OLD_LAYOUT') == '1':
        overlay.pill.get_parent().reorder_child(overlay.pill, 0)
        overlay._grow_up = False
        print('  （已把药丸挪回顶部，复现旧布局）')

    area = overlay.workarea()
    failures = []
    results = {}

    def record(phase, tag):
        width, height = overlay.win.get_size()
        x, y = overlay.win.get_position()
        alloc = overlay.pill.get_allocation()
        results[(phase, tag)] = (width, height, x, y, y + alloc.y)
        print('  [%s] %-9s 窗口 %dx%d @ %d,%d  底边=%d  药丸屏幕 y=%d'
              % (phase, tag, width, height, x, y, y + height, y + alloc.y))

    def check_phase(phase, label):
        col = results.get((phase, 'collapsed'))
        exp = results.get((phase, 'expanded'))
        exp2 = results.get((phase, 'expanded2'))
        if not col or not exp or not exp2:
            failures.append('[%s] 没拿到全部几何数据' % phase)
            return
        if exp[1] <= col[1]:
            failures.append('[%s] 展开后没有变高：%d -> %d' % (phase, col[1], exp[1]))
        # 核心不变量：药丸在屏幕上不许动。
        if len({col[4], exp[4], exp2[4]}) != 1:
            failures.append('[%s] %s：药丸在屏幕上移动了 收起=%d 展开=%d 再展开=%d'
                            % (phase, label, col[4], exp[4], exp2[4]))
        if exp != exp2:
            failures.append('[%s] 再次展开的几何不稳定：%s vs %s' % (phase, exp, exp2))
        # 整框必须留在工作区内，否则就是「被顶住之后原地跳」。
        for tag, r in (('collapsed', col), ('expanded', exp)):
            if r[3] < area.y or r[3] + r[1] > area.y + area.height + 1:
                failures.append('[%s] %s 超出工作区：y=%d h=%d workarea=[%d,%d]'
                                % (phase, tag, r[3], r[1], area.y, area.y + area.height))

    # ── 阶段一：默认右下角（向上长，药丸贴底）──
    def phase1_expand():
        overlay.set_expanded(True)
        GLib.timeout_add(400, phase1_after_expand)
        return False

    def phase1_after_expand():
        record('bottom', 'expanded')
        overlay.set_expanded(False)
        GLib.timeout_add(400, phase1_after_collapse)
        return False

    def phase1_after_collapse():
        record('bottom', 'collapsed')
        overlay.set_expanded(True)
        GLib.timeout_add(400, phase1_after_reexpand)
        return False

    def phase1_after_reexpand():
        record('bottom', 'expanded2')
        check_phase('bottom', '右下角')
        # ── 阶段二：拖到屏幕上方（应改成向下长，药丸贴顶且不动）──
        overlay.set_expanded(False)
        overlay.anchor_x = area.x + area.width - 200
        overlay.anchor_y = area.y + 260
        overlay._grow_up = True          # 让 place() 重新判断方向
        GLib.timeout_add(300, phase2_expand)
        return False

    def phase2_expand():
        overlay.set_expanded(True)
        GLib.timeout_add(400, phase2_after_expand)
        return False

    def phase2_after_expand():
        record('top', 'expanded')
        overlay.set_expanded(False)
        GLib.timeout_add(400, phase2_after_collapse)
        return False

    def phase2_after_collapse():
        record('top', 'collapsed')
        overlay.set_expanded(True)
        GLib.timeout_add(400, phase2_after_reexpand)
        return False

    def phase2_after_reexpand():
        record('top', 'expanded2')
        check_phase('top', '屏幕上方')
        final_checks()
        Gtk.main_quit()
        return False

    def final_checks():
        # 定时回调必须「打不死」：GLib 在回调抛异常时会移除定时源，悬浮框会永久冻结。
        good_render = overlay.render
        overlay.render = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
        survived_poll = overlay.poll()
        survived_tick = overlay.tick()
        overlay.render = good_render
        if survived_poll is not True:
            failures.append('poll() 抛异常时没有返回 True —— GLib 会移除定时源，框会冻结')
        if survived_tick is not True:
            failures.append('tick() 抛异常时没有返回 True —— 计时会冻结')

        # 守卫：不能再挂 enter-notify / leave-notify 处理器。
        overlay.set_expanded(True)
        try:
            overlay.win.emit('leave-notify-event', Gdk.Event.new(Gdk.EventType.LEAVE_NOTIFY))
        except Exception:
            pass
        if not overlay.expanded:
            failures.append('leave-notify 仍会改变展开状态 —— enter/leave 处理器又被接回来了')

    GLib.timeout_add(500, phase1_expand)
    Gtk.main()

    print()
    if failures:
        for item in failures:
            print('✗ %s' % item)
        return 1
    print('✓ 右下角向上长、屏幕上方向下长；两种情况药丸都不动，且不出工作区')
    return 0


if __name__ == '__main__':
    sys.exit(main())
