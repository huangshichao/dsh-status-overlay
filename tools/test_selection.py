#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-status-overlay / tools/test_selection.py

「显示哪个会话」的回归测试。

规则（用户明确要求过）：**正在执行的任务才轮询**。
  · 有任务在跑 → 一定显示在跑的那个（即使 primary 指向已完成的会话）
  · 多个在跑 + rotateSeconds>0 → 轮换；展开时冻结
  · 一个都没在跑 → 回到 primary，让「已完成」留在眼前

用法：/usr/bin/python3 tools/test_selection.py   退出码 0 = 通过
"""

import importlib.util
import json
import os
import sys
import tempfile
import time

os.environ.setdefault('GDK_BACKEND', 'x11')

import gi  # noqa: E402

gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OVERLAY = os.environ.get('DSH_OVERLAY_PATH') or os.path.join(HERE, '..', 'lib', 'overlay.py')


def load_overlay():
    spec = importlib.util.spec_from_file_location('overlay_sel_test', OVERLAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def session(sid, status, task, last_activity):
    return {
        'id': sid, 'status': status, 'task': task, 'lastActivity': last_activity,
        'phase': 'tool' if status == 'running' else 'done',
        'tool': 'bash' if status == 'running' else None,
        'thinking': '', 'streamText': '', 'steps': [], 'startedAt': 0,
        'doneAt': 0, 'doneKind': '', 'error': '', 'isSubagent': False,
    }


def main():
    ov = load_overlay()
    work = tempfile.mkdtemp(prefix='dsh-overlay-sel-')
    state = os.path.join(work, 'state.json')
    with open(state, 'w') as handle:
        json.dump({'v': 1, 'updatedAt': 2 ** 40, 'primary': None, 'sessions': {}}, handle)

    overlay = ov.Overlay(state, os.path.join(work, 'lock'))
    overlay.pointer_inside = lambda: overlay.expanded
    failures = []

    def setup(primary, sessions, rotate=0):
        overlay.state = {'primary': primary, 'sessions': sessions}
        overlay.rotate_seconds = rotate
        overlay.expanded = False

    def expect(label, want):
        got = overlay.current().get('id')
        ok = got == want
        print('  %-46s -> %-6s %s' % (label, got, '✓' if ok else '✗ 期望 %s' % want))
        if not ok:
            failures.append('%s: 得到 %s，期望 %s' % (label, got, want))

    # 1) 只有一个在跑，且 primary 指向已完成的会话 —— 必须显示在跑的那个
    setup('done1', {
        'done1': session('done1', 'idle', '已完成的旧任务', 5000),
        'run1': session('run1', 'running', '正在跑的任务', 1000),
    })
    expect('primary=已完成，另有一个在跑', 'run1')

    # 2) 两个在跑、不轮换 —— 显示最近活跃的那个
    setup('runA', {
        'runA': session('runA', 'running', '任务甲', 9000),
        'runB': session('runB', 'running', '任务乙', 1000),
    })
    expect('两个在跑、rotate=0 → 最近活跃的', 'runA')

    # 3) 两个在跑、开轮换 —— 随时间在两个之间切换
    setup('runA', {
        'runA': session('runA', 'running', '任务甲', 9000),
        'runB': session('runB', 'running', '任务乙', 1000),
    }, rotate=2)
    real_time = time.time
    try:
        overlay.rotate_seconds = 2
        seen = set()
        for t in (10, 12, 14, 16):
            ov.time.time = lambda t=t: t
            seen.add(overlay.current().get('id'))
        print('  %-46s -> %s  %s' % ('rotate=2，t=10/12/14/16 的取值',
                                     sorted(seen), '✓' if seen == {'runA', 'runB'} else '✗'))
        if seen != {'runA', 'runB'}:
            failures.append('轮换没有覆盖两个会话：%s' % sorted(seen))

        # 4) 展开时冻结
        overlay.expanded = True
        got = set()
        for t in (10, 12, 14, 16):
            ov.time.time = lambda t=t: t
            got.add(overlay.current().get('id'))
        print('  %-46s -> %s  %s' % ('展开时（应冻结成同一个）', sorted(got),
                                     '✓' if len(got) == 1 else '✗'))
        if len(got) != 1:
            failures.append('展开时轮换没有冻结：%s' % sorted(got))
    finally:
        ov.time.time = real_time

    # 5) 一个都没在跑 —— 回到 primary
    setup('done1', {
        'done1': session('done1', 'idle', '已完成的任务', 5000),
        'done2': session('done2', 'idle', '更早完成的任务', 1000),
    })
    expect('没有在跑 → 回到 primary', 'done1')

    overlay.win.destroy()
    print()
    if failures:
        for item in failures:
            print('✗ %s' % item)
        return 1
    print('✓ 只显示正在执行的任务；轮换仅在多个并行时生效，且展开即冻结')
    return 0


if __name__ == '__main__':
    sys.exit(main())
