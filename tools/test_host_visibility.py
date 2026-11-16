#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-status-overlay / tools/test_host_visibility.py

回归测试：**DSH 主服务不在时，悬浮框不应该显示**。

规则：
  · state.json 里的宿主 pid 确实不存在 → 整个框隐藏（不是显示「已断开」）
  · state.json 根本不存在且已超过 3 秒 → 隐藏
  · 宿主回来后 → 自动重新显示
  · 拿不到 pid（standalone/演示）→ 保持显示，不误伤

用法：/usr/bin/python3 tools/test_host_visibility.py   退出码 0 = 通过
"""

import importlib.util
import json
import os
import subprocess
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
    spec = importlib.util.spec_from_file_location('overlay_vis_test', OVERLAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dead_pid():
    """拿一个确实已经退出的 pid。"""
    child = subprocess.Popen(['/bin/true'])
    child.wait()
    try:
        os.kill(child.pid, 0)
    except ProcessLookupError:
        return child.pid
    return 4194303


def session():
    now = int(time.time() * 1000)
    return {
        'id': 'a', 'status': 'running', 'task': '可见性测试',
        'lastActivity': now, 'phase': 'tool', 'tool': 'bash',
        'toolLabel': '执行命令', 'toolHint': '', 'thinking': '',
        'streamText': '', 'steps': [], 'startedAt': now, 'doneAt': 0,
        'doneKind': '', 'error': '', 'computerUse': False, 'isSubagent': False,
    }


def main():
    ov = load_overlay()
    work = tempfile.mkdtemp(prefix='dsh-overlay-vis-')
    state_path = os.path.join(work, 'state.json')
    with open(state_path, 'w') as handle:
        json.dump({'v': 1, 'updatedAt': int(time.time() * 1000), 'pid': None,
                   'primary': 'a', 'sessions': {'a': session()}}, handle)

    overlay = ov.Overlay(state_path, os.path.join(work, 'lock'))
    overlay.pointer_inside = lambda: overlay.expanded
    overlay.hide_exit_seconds = 0          # 测试里不要因为超时退进程

    failures = []
    dead = dead_pid()
    print('  （负例 pid：%d）' % dead)

    def check(label, want_visible):
        overlay.render()
        visible = overlay.win.get_visible()
        ok = visible == want_visible
        print('  %-46s 可见=%-5s %s' % (label, visible, '✓' if ok else '✗ 期望 %s' % want_visible))
        if not ok:
            failures.append('%s：可见=%s，期望 %s' % (label, visible, want_visible))

    def set_state(pid, fresh=True):
        overlay.state = {
            'v': 1,
            'updatedAt': int(time.time() * 1000) if fresh else 0,
            'pid': pid,
            'primary': 'a',
            'sessions': {'a': session()},
        }

    # 1) 宿主 pid 已死 → 应隐藏
    set_state(dead)
    check('宿主 pid 已不存在', False)

    # 2) 宿主 pid 活着 → 应显示
    set_state(os.getpid())
    check('宿主 pid 存活', True)

    # 3) 先隐藏，再让宿主回来 → 应自动重新显示
    set_state(dead)
    check('再次判定宿主已死', False)
    set_state(os.getpid())
    check('宿主回来后自动恢复', True)

    # 4) state.json 不存在，3 秒内 → 仍显示（给启动留窗口）
    os.remove(state_path)
    overlay.state = {}
    overlay._no_state_since = time.time()
    check('状态文件刚消失（<3s）', True)

    # 5) state.json 长时间不存在 → 隐藏
    overlay._no_state_since = time.time() - 10
    check('状态文件消失超过 3s', False)

    # 6) 拿不到 pid（standalone/演示）→ 不误伤，保持显示
    with open(state_path, 'w') as handle:
        json.dump({'v': 1, 'updatedAt': int(time.time() * 1000), 'pid': None,
                   'primary': 'a', 'sessions': {'a': session()}}, handle)
    overlay.state = {'v': 1, 'updatedAt': int(time.time() * 1000), 'pid': None,
                     'primary': 'a', 'sessions': {'a': session()}}
    overlay._no_state_since = 0
    check('拿不到宿主 pid（不判断）', True)

    overlay.win.destroy()
    print()
    if failures:
        for item in failures:
            print('✗ %s' % item)
        return 1
    print('✓ 主服务不在时隐藏、回来时恢复，未知 pid 不误伤')
    return 0


if __name__ == '__main__':
    sys.exit(main())
