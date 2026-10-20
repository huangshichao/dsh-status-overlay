#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-status-overlay / tools/test_scroll.py

回归测试：悬浮框不能自己「一直滚」。

曾经的 bug：`render_detail()` 每 200ms 无条件 `set_text` + `scroll_to_mark(底部)`，
连收起时也照做。于是鼠标一放上去，正文每 200ms 被重建一次、滚动位置被重置到底部 ——
手动滚上去也会立刻被弹回来，看起来就是「一直在触发滚动」。

现在的规则：
  · 收起时不碰 buffer
  · 内容没变不碰 buffer
  · 只有「刚展开」或「本来就停在底部」才自动跟随

用法：/usr/bin/python3 tools/test_scroll.py   退出码 0 = 通过
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
from gi.repository import Gtk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OVERLAY = os.environ.get('DSH_OVERLAY_PATH') or os.path.join(HERE, '..', 'lib', 'overlay.py')


def load_overlay():
    spec = importlib.util.spec_from_file_location('overlay_scroll_test', OVERLAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    ov = load_overlay()
    work = tempfile.mkdtemp(prefix='dsh-overlay-scroll-')
    state_path = os.path.join(work, 'state.json')
    now = int(__import__('time').time() * 1000)
    entry = {
        'id': 'a', 'status': 'running', 'task': '滚动回归测试',
        'lastActivity': now, 'phase': 'thinking', 'tool': None,
        'toolLabel': '', 'toolHint': '', 'thinking': 'x' * 500,
        'streamText': '', 'steps': [], 'startedAt': now, 'doneAt': 0,
        'doneKind': '', 'error': '', 'computerUse': False, 'isSubagent': False,
    }
    with open(state_path, 'w') as handle:
        json.dump({'v': 1, 'updatedAt': now, 'pid': os.getpid(),
                   'primary': 'a', 'sessions': {'a': entry}}, handle)

    overlay = ov.Overlay(state_path, os.path.join(work, 'lock'))
    overlay.pointer_inside = lambda: overlay.expanded

    counts = {'set_text': 0, 'scroll': 0}
    buffer = overlay.view.get_buffer()

    def fake_set_text(*args, **kwargs):
        counts['set_text'] += 1

    def fake_scroll(*args, **kwargs):
        counts['scroll'] += 1

    buffer.set_text = fake_set_text
    overlay.view.scroll_to_mark = fake_scroll

    failures = []

    def expect(label, want_text, want_scroll):
        ok = counts['set_text'] == want_text and counts['scroll'] == want_scroll
        print('  %-44s set_text=%d scroll=%d  %s'
              % (label, counts['set_text'], counts['scroll'], '✓' if ok else
                 '✗ 期望 %d/%d' % (want_text, want_scroll)))
        if not ok:
            failures.append('%s：set_text=%d scroll=%d，期望 %d/%d'
                            % (label, counts['set_text'], counts['scroll'],
                               want_text, want_scroll))

    # 1) 收起态：无论渲染多少次都不该碰 buffer
    overlay.state = {'v': 1, 'updatedAt': now, 'pid': os.getpid(),
                     'primary': 'a', 'sessions': {'a': dict(entry)}}
    overlay.expanded = False
    for _ in range(5):
        overlay.render()
    expect('收起态渲染 5 次', 0, 0)

    # 2) 刚展开：重建一次并跟到底部
    overlay.set_expanded(True)
    overlay.render()
    expect('刚展开（首次跟随）', 1, 1)

    # 3) 内容没变：再渲染 5 次都不该重写 buffer（这是「一直滚」的直接原因）
    for _ in range(5):
        overlay.render()
    expect('内容不变，再渲染 5 次', 1, 1)

    # 4) 用户往上滚了（不在底部）：内容变了也不许抢滚动条
    overlay.at_bottom = lambda: False
    changed = dict(entry)
    changed['thinking'] = 'y' * 600
    overlay.state['sessions']['a'] = changed
    overlay.render()
    expect('用户上滚 + 内容变化（不跟随）', 2, 1)

    # 5) 用户回到顶部后内容再变，仍然不跟随
    overlay.render()
    expect('再次渲染（内容已变过）', 2, 1)

    # 6) 重新展开 → 强制重建并跟到底部
    overlay.set_expanded(False)
    overlay.set_expanded(True)
    overlay.render()
    expect('收起再展开（强制跟随）', 3, 2)

    overlay.win.destroy()
    print()
    if failures:
        for item in failures:
            print('✗ %s' % item)
        return 1
    print('✓ 收起/无变化时不滚；只有刚展开或停在底部才自动跟随')
    return 0


if __name__ == '__main__':
    sys.exit(main())
