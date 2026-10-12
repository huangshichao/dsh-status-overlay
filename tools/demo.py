#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dsh-status-overlay / tools/demo.py

不依赖 DSH，往 state.json 里写一份仿真状态，用来单独测试 / 演示悬浮框。

用法：
    python3 tools/demo.py --state /tmp/dsh/state.json --phase thinking
    python3 tools/demo.py --state /tmp/dsh/state.json --phase tool
    python3 tools/demo.py --state /tmp/dsh/state.json --phase done
    python3 tools/demo.py --state /tmp/dsh/state.json --cycle     # 自动走一遍

阶段：thinking / tool / done / error / idle
"""

import argparse
import json
import os
import time


def build(phase, now_ms):
    thinking = ("用户要的是：在桌面右下角放一个置顶的悬浮框，显示当前正在执行的任务，"
                "悬浮时能看到思考输出的进展，完成了要有提示。\n"
                "先确认数据源 —— 宿主进程里有 agent/assistant-stream 这个公开事件，"
                "逐 token 的 reasoning-delta 和 text-delta 都在里面，不需要改 DSH 内部状态。\n"
                "再确认渲染 —— 系统 python3 有 gi(GTK3)，Deepin 是 Wayland 会话但 XWayland 在，"
                "显式走 X11 后端才能保证 keep_above 生效。")
    stream = "好的，我来做一个桌面悬浮框：它订阅 DSH 的实时事件，在右下角显示正在执行的任务。"
    entry = {
        "id": "session-demo",
        "cwd": "/home/user/projects",
        "title": "示例任务",
        "task": "做一个桌面悬浮框，显示正在执行的任务，悬浮看思考进展，完成有提示",
        "phase": "thinking",
        "status": "running",
        "isSubagent": False,
        "tool": None, "toolHint": "", "toolLabel": "",
        "thinking": thinking,
        "streamText": stream,
        "steps": [
            {"name": "bash", "label": "执行命令", "hint": "检查系统 python 与 GTK 绑定", "at": now_ms - 8000},
            {"name": "gui_ocr", "label": "识别屏幕文字", "hint": "状态", "at": now_ms - 5000},
            {"name": "write", "label": "写入文件", "hint": "dsh-status-overlay/lib/overlay.py", "at": now_ms - 2000},
        ],
        "computerUse": True,
        "turn": 1,
        "startedAt": now_ms - 9000,
        "lastActivity": now_ms,
        "doneAt": 0, "doneKind": "", "error": "",
    }
    if phase == "tool":
        entry.update({
            "phase": "tool", "status": "running",
            "tool": "gui_drag", "toolLabel": "操作桌面窗口",
            "toolHint": "(1200, 800) → (1600, 900)",
        })
        entry["steps"].append({"name": "gui_drag", "label": "操作桌面窗口", "hint": "(1200, 800) → (1600, 900)", "at": now_ms})
    elif phase == "done":
        entry.update({"phase": "done", "status": "idle", "doneKind": "completed", "doneAt": now_ms, "tool": None})
    elif phase == "error":
        entry.update({"phase": "error", "status": "idle", "doneKind": "error", "doneAt": now_ms,
                      "error": "ERR_LLM_TIMEOUT: 模型请求超时（600s）"})
    elif phase == "idle":
        entry.update({"phase": "idle", "status": "idle", "thinking": "", "streamText": "",
                      "startedAt": 0, "steps": [{"name": "bash", "label": "执行命令", "hint": "pwd", "at": now_ms}]})
        entry["task"] = ""

    return {
        "v": 1, "updatedAt": now_ms, "pid": os.getpid(),
        "primary": entry["id"], "activeCount": 1, "subagentRuns": 0,
        "sessions": {entry["id"]: entry},
    }


def write(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True)
    parser.add_argument("--phase", default="thinking",
                        choices=["thinking", "tool", "done", "error", "idle"])
    parser.add_argument("--cycle", action="store_true", help="每 4 秒换一个阶段，循环")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.state)), exist_ok=True)

    if not args.cycle:
        write(args.state, build(args.phase, int(time.time() * 1000)))
        print("wrote %s (%s)" % (args.state, args.phase))
        return

    order = ["thinking", "tool", "done", "idle"]
    index = 0
    while True:
        phase = order[index % len(order)]
        write(args.state, build(phase, int(time.time() * 1000)))
        print("phase -> %s" % phase, flush=True)
        index += 1
        time.sleep(4)


if __name__ == "__main__":
    main()
