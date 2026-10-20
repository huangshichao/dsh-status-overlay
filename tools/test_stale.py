#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-status-overlay / tools/test_stale.py

回归测试：「已断开」不能误报。

曾经的症状：长时间执行时悬浮框显示 `DSH · 已断开（宿主无响应）`，但其实一切正常。
原因是判断只看 `updatedAt` 是否超过 60s —— 而长时间工具调用期间没有任何会话事件，
`updatedAt` 会停更。

现在的规则：静默超过 STALE_SILENT_MS **且** state.json 里的宿主 pid 确实不存在，
才判定断开。

用法：/usr/bin/python3 tools/test_stale.py   退出码 0 = 通过
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
    spec = importlib.util.spec_from_file_location('overlay_stale_test', OVERLAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    ov = load_overlay()
    work = tempfile.mkdtemp(prefix='dsh-overlay-stale-')
    state_path = os.path.join(work, 'state.json')
    with open(state_path, 'w') as handle:
        json.dump({'v': 1, 'updatedAt': 0, 'primary': 'a', 'sessions': {}}, handle)

    overlay = ov.Overlay(state_path, os.path.join(work, 'lock'))
    overlay.pointer_inside = lambda: overlay.expanded
    failures = []

    now = int(time.time() * 1000)

    def session():
        return {
            'id': 'a', 'status': 'running', 'task': '一个跑了很久的任务',
            'lastActivity': now, 'phase': 'tool', 'tool': 'bash',
            'toolLabel': '执行命令', 'toolHint': 'long job', 'thinking': '',
            'streamText': '', 'steps': [], 'startedAt': now - 600000,
            'doneAt': 0, 'doneKind': '', 'error': '', 'computerUse': False,
            'isSubagent': False,
        }

    def check(label, updated_at, pid, want_stale):
        overlay.state = {
            'updatedAt': updated_at, 'pid': pid, 'primary': 'a',
            'sessions': {'a': session()},
        }
        overlay.render()
        title = overlay.title.get_text()
        is_stale = '已断开' in title
        ok = is_stale == want_stale
        print('  %-52s -> %-28s %s' % (label, title, '✓' if ok else '✗'))
        if not ok:
            failures.append('%s：得到 %r，期望 stale=%s' % (label, title, want_stale))

    # 拿一个**确定已经死掉**的 pid：起一个 /bin/true 等它退出，它的 pid 就是死的。
    def dead_pid():
        child = subprocess.Popen(['/bin/true'])
        child.wait()
        try:
            os.kill(child.pid, 0)
        except ProcessLookupError:
            return child.pid
        # 极少数情况下 pid 立刻被复用，那就再找一个
        for candidate in range(4194303, 4194000, -1):
            try:
                os.kill(candidate, 0)
            except ProcessLookupError:
                return candidate
            except Exception:
                continue
        return 4194303

    dead = dead_pid()
    print('   （用一个确实不存在的 pid 做负例：%d）' % dead)

    # 1) 文件停了 10 分钟，但宿主 pid 还在 —— 绝不能报断开（这正是用户遇到的场景）
    check('静默10分钟 + 宿主仍存活', now - 600000, os.getpid(), False)

    # 2) 文件停了 10 分钟，宿主 pid 确实不存在 —— 这时应当报断开
    check('静默10分钟 + 宿主已不存在', now - 600000, dead, True)

    # 3) 刚更新过，pid 已死 —— 宽限期内先不报断开
    check('刚更新 + 宿主已不存在', now, dead, False)

    overlay.win.destroy()
    print()
    if failures:
        for item in failures:
            print('✗ %s' % item)
        return 1
    print('✓ 长时间静默不再误报断开；只有宿主确实不在了才报')
    return 0


if __name__ == '__main__':
    sys.exit(main())
