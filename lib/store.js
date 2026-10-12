/**
 * dsh-status-overlay / lib/store.js
 *
 * 把 DSH 会话的实时事件折成一份「人看的」状态 JSON，交给桌面悬浮框。
 *
 * 数据来源（全部是宿主进程里已经存在的公开事件，不碰任何内部状态）：
 *   · `session/event`            —— 持久事件：用户消息、工具调用、轮次起止
 *   · `agent/assistant-stream`   —— 逐 token 的模型流：reasoning-delta / text-delta
 *   · `agent/status`             —— running / idle
 *
 * 设计取舍：
 *   · **写盘节流**。token 级事件每秒几十次，直接写盘没有意义 —— 悬浮框 200ms 轮询一次，
 *     所以这里最多 ~8 次/秒落盘，且只写变化。
 *   · **原子写**。先写 .tmp 再 rename，悬浮框永远读不到半截 JSON。
 *   · **只读不写会话**。本插件绝不 append 任何 session 事件，绝不改 DSH 状态；
 *     最坏情况只是状态文件停更。
 *   · **绝不抛**。监听器里的任何异常都吞掉并记进 plugin.log，否则会顺着
 *     cordis 的事件派发冒上去，影响整轮对话。
 * @module dsh-status-overlay/store
 */

import { appendFileSync, mkdirSync, renameSync, statSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

/** 工具名 → 中文动作标签。悬浮框上「正在执行 X」直接读这个。 */
const TOOL_LABELS = Object.freeze({
  bash: '执行命令',
  read: '读取文件',
  write: '写入文件',
  edit: '修改文件',
  glob: '查找文件',
  grep: '搜索内容',
  web_search: '联网搜索',
  web_fetch: '抓取网页',
  present: '交付文件',
  todo_write: '更新任务清单',
  ask_user_question: '向你提问',
  android_screenshot: '截取安卓屏幕',
  android_find_text: '识别安卓屏幕文字',
  android_ui_tree: '读取安卓界面',
  android_interact: '操作安卓设备',
  android_tap_text: '点击安卓界面',
  android_tap_element: '点击安卓控件',
  android_launch_app: '打开安卓应用',
  android_logs: '读取安卓日志',
  subagent: '派出子任务',
  subagent_fork: '派出子任务',
  workflow: '编排工作流',
  skill: '加载技能',
  present_files: '交付文件',
});

/** 判定「computer use」类工具：桌面窗口自动化 + 安卓设备自动化。 */
const COMPUTER_USE = /^(gui_|android_|mindplus)|^gui\b|computer/i;

const MAX_THINKING = 24000;
const MAX_STREAM = 12000;
const MAX_STEPS = 14;
const MAX_TASK = 4000;

/** 从 assistant/user 消息的 content 数组里取纯文本。 */
function blocksToText(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  const parts = [];
  for (const block of content) {
    if (!block || typeof block !== 'object') continue;
    if (block.type === 'text' && typeof block.text === 'string') parts.push(block.text);
    else if (block.type === 'reasoning' && typeof block.text === 'string') parts.push(block.text);
  }
  return parts.join('');
}

/** 单行化 + 截断，悬浮框只显示一行。 */
function oneLine(text, max = MAX_TASK) {
  const flat = String(text ?? '').replace(/\s+/g, ' ').trim();
  return flat.length > max ? `${flat.slice(0, max)}…` : flat;
}

/** 工具调用的参数摘要（尽量短、尽量可读）。 */
function toolHint(name, rawArguments) {
  let args = rawArguments;
  if (typeof args === 'string') {
    try { args = JSON.parse(args); } catch { return ''; }
  }
  if (!args || typeof args !== 'object') return '';
  const pick = (...keys) => {
    for (const key of keys) if (args[key] !== undefined && args[key] !== null && args[key] !== '') return String(args[key]);
    return '';
  };
  switch (name) {
    case 'bash': return oneLine(pick('description', 'command'), 120);
    case 'read': case 'write': case 'edit': return oneLine(pick('file_path', 'path'), 120);
    case 'glob': return oneLine(pick('pattern'), 80);
    case 'grep': return oneLine(pick('pattern'), 80);
    case 'web_search': return oneLine(pick('queries', 'query'), 100);
    case 'web_fetch': return oneLine(pick('url'), 100);
    case 'gui_click': case 'gui_dblclick': return `(${pick('x')}, ${pick('y')})`;
    case 'gui_drag': return `(${pick('x1')}, ${pick('y1')}) → (${pick('x2')}, ${pick('y2')})`;
    case 'gui_ocr': case 'gui_wait_for': return oneLine(pick('query', 'text'), 80);
    case 'gui_type': return oneLine(pick('text'), 80);
    case 'gui_key': return pick('key');
    case 'gui_focus': return oneLine(pick('title'), 60);
    case 'android_interact': return oneLine(pick('action', 'text', 'direction', 'name'), 60);
    case 'android_launch_app': return oneLine(pick('packageName', 'name'), 60);
    default: break;
  }
  const first = Object.values(args).find((v) => typeof v === 'string' && v.length > 0);
  return oneLine(first ?? '', 80);
}

/** 人类可读的工具标签。 */
export function toolLabel(name) {
  if (TOOL_LABELS[name]) return TOOL_LABELS[name];
  if (/^android_/.test(name)) return '操作安卓设备';
  if (/^gui_/.test(name)) return '操作桌面窗口';
  return name;
}

/** 一次会话（= 一个 Agent）的实时状态。 */
function blankSession(id) {
  return {
    id,
    cwd: '',
    title: '',
    task: '',
    phase: 'idle',
    status: 'idle',
    isSubagent: false,
    sawHuman: false,
    tool: null,
    toolHint: '',
    toolLabel: '',
    thinking: '',
    streamText: '',
    steps: [],
    computerUse: false,
    turn: null,
    startedAt: 0,
    lastActivity: 0,
    doneAt: 0,
    doneKind: '',
    error: '',
  };
}

/** 把会话状态压成给悬浮框的最小 JSON。 */
function wireSession(entry) {
  return {
    id: entry.id,
    cwd: entry.cwd,
    title: entry.title,
    task: entry.task,
    phase: entry.phase,
    status: entry.status,
    isSubagent: entry.isSubagent,
    tool: entry.tool,
    toolHint: entry.toolHint,
    toolLabel: entry.toolLabel,
    thinking: entry.thinking,
    streamText: entry.streamText,
    steps: entry.steps,
    computerUse: entry.computerUse,
    turn: entry.turn,
    startedAt: entry.startedAt,
    lastActivity: entry.lastActivity,
    doneAt: entry.doneAt,
    doneKind: entry.doneKind,
    error: entry.error,
  };
}

export class StatusStore {
  /**
   * @param {object} [options]
   * @param {string} [options.dir] 状态目录，默认 `$DSH_HOME/status-overlay`。
   * @param {(message: string) => void} [options.info] 走宿主日志。
   * @param {boolean} [options.captureReasoning] 是否采集模型的思考流（默认 **否**）。
   *   思考内容属于模型私有推理，可能夹带用户路径、代码、对话片段；默认不采集、
   *   不落盘，需要的人显式打开 `DSH_OVERLAY_THINKING=1`。
   */
  constructor({ dir, info, captureReasoning } = {}) {
    const home = process.env.DSH_HOME || join(homedir(), '.dsh');
    this.dir = dir || join(home, 'status-overlay');
    this.captureReasoning = captureReasoning === true;
    this.statePath = join(this.dir, 'state.json');
    this.logPath = join(this.dir, 'plugin.log');
    this.tmpPath = `${this.statePath}.tmp`;
    this.info = info || (() => {});
    this.sessions = new Map();
    this.primaryId = '';
    this.subagentRuns = 0;
    this.timer = null;
    this.dirty = false;
    this.disposed = false;
    this.lastWriteAt = 0;
    this.writeCount = 0;
    try {
      mkdirSync(this.dir, { recursive: true, mode: 0o700 });
    } catch { /* 目录不可写时后面 flush 会自己再试 */ }
  }

  /**
   * 追加一行日志。超过上限就截断重来 —— 这个文件由看门狗反复写，
   * 不设上限的话能长到几百 MB。
   */
  appendLog(line) {
    try {
      const MAX = 256 * 1024;
      try {
        if (statSync(this.logPath).size > MAX) writeFileSync(this.logPath, '', { encoding: 'utf8' });
      } catch { /* 文件还不存在 */ }
      appendFileSync(this.logPath, line);
    } catch { /* 日志失败无关紧要 */ }
  }

  /** 追加一行普通日志。 */
  debugLine(message) {
    this.appendLog(`[${new Date().toISOString()}] ${message}\n`);
  }

  /** 追加一行错误日志。 */
  logError(error) {
    this.appendLog(`[${new Date().toISOString()}] ${error?.stack || error}\n`);
  }

  /** 取或建一条会话状态。 */
  entry(id) {
    let found = this.sessions.get(id);
    if (!found) {
      found = blankSession(id);
      this.sessions.set(id, found);
    }
    return found;
  }

  /** 标记需要落盘，并安排一次节流写。 */
  touch(entry, at) {
    if (entry) entry.lastActivity = at || Date.now();
    this.dirty = true;
    if (this.timer || this.disposed) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.flush();
    }, 200);
    this.timer.unref?.();
  }

  /** 原子落盘。任何时候失败都只记日志。 */
  flush() {
    if (!this.dirty || this.disposed) return;
    this.dirty = false;
    try {
      const now = Date.now();
      const sessions = {};
      let best = null;
      let activeRoot = 0;
      for (const [id, entry] of this.sessions) {
        sessions[id] = wireSession(entry);
        if (!entry.isSubagent && entry.sawHuman) {
          if (!best || entry.lastActivity > best.lastActivity) best = entry;
        }
        if (entry.status === 'running') activeRoot += 1;
      }
      const primary = best || [...this.sessions.values()].sort((a, b) => b.lastActivity - a.lastActivity)[0];
      this.primaryId = primary?.id ?? '';
      const payload = {
        v: 1,
        updatedAt: now,
        pid: process.pid,
        primary: this.primaryId,
        activeCount: activeRoot,
        subagentRuns: this.subagentRuns,
        sessions,
      };
      writeFileSync(this.tmpPath, JSON.stringify(payload), { encoding: 'utf8', mode: 0o600 });
      renameSync(this.tmpPath, this.statePath);
      this.lastWriteAt = now;
      this.writeCount += 1;
      if (this.writeCount === 1) this.info(`状态文件已建立：${this.statePath}`);
    } catch (error) {
      this.logError(error);
    }
  }

  /** session/event —— 持久事件流。 */
  sessionEvent(session, event) {
    const id = session?.id || session?.sessionId;
    if (!id || !event) return;
    const entry = this.entry(id);
    if (!entry.cwd && session?.meta?.cwd) entry.cwd = String(session.meta.cwd);
    if (session?.meta?.origin === 'subagent') entry.isSubagent = true;
    const data = event.data || {};
    switch (event.type) {
      case 'user/message': {
        // 只认人发的话；插件注入的快照（source.kind=plugin）不算任务。
        if (data.source?.kind !== 'user') break;
        const text = oneLine(blocksToText(data.content));
        if (!text) break;
        entry.sawHuman = true;
        entry.isSubagent = false;
        entry.task = text;
        entry.thinking = '';
        entry.streamText = '';
        entry.steps = [];
        entry.computerUse = false;
        entry.doneAt = 0;
        entry.doneKind = '';
        entry.error = '';
        break;
      }
      case 'session/title': {
        const title = data.title || data.value || '';
        if (title) entry.title = oneLine(title, 120);
        break;
      }
      case 'turn/start': {
        entry.turn = data.turn ?? null;
        entry.status = 'running';
        entry.phase = 'thinking';
        entry.startedAt = event.time || Date.now();
        entry.doneAt = 0;
        entry.doneKind = '';
        entry.error = '';
        entry.computerUse = false;
        break;
      }
      case 'turn/end': {
        const reason = data.reason || {};
        entry.status = 'idle';
        entry.phase = reason.kind === 'error' ? 'error' : 'done';
        entry.doneKind = reason.kind || 'completed';
        entry.error = reason.error?.message ? oneLine(reason.error.message, 300) : '';
        entry.doneAt = event.time || Date.now();
        if (entry.doneKind !== 'error') entry.error = '';
        break;
      }
      case 'tool/call': {
        entry.status = 'running';
        entry.phase = 'tool';
        entry.tool = data.name || '';
        entry.toolLabel = toolLabel(entry.tool);
        entry.toolHint = toolHint(entry.tool, data.arguments);
        if (COMPUTER_USE.test(entry.tool || '')) entry.computerUse = true;
        const step = { name: entry.tool, label: entry.toolLabel, hint: entry.toolHint, at: event.time || Date.now() };
        entry.steps = [...entry.steps, step].slice(-MAX_STEPS);
        break;
      }
      case 'tool/result': {
        if (entry.phase === 'tool') {
          entry.phase = 'thinking';
          const failed = data.message?.content?.[0]?.isError === true;
          if (failed) {
            const last = entry.steps[entry.steps.length - 1];
            if (last) last.failed = true;
          }
        }
        break;
      }
      case 'assistant/message': {
        // 一轮 step 的完整回复：作为「可见输出」的兜底，流式事件缺失时也有内容。
        const text = oneLine(blocksToText(data.message?.content), MAX_STREAM);
        if (text) entry.streamText = text;
        break;
      }
      default:
        break;
    }
    // 用事件自己的时间戳：补种历史事件时，lastActivity 的先后才正确。
    this.touch(entry, event.time);
  }

  /** agent/assistant-stream —— 逐 token 的实时流，这是「思考进展」的来源。 */
  assistantStream(payload) {
    const agent = payload?.agent;
    const frame = payload?.frame;
    const id = agent?.id || agent?.sessionId;
    if (!id || !frame) return;
    const entry = this.entry(id);
    switch (frame.type) {
      case 'start': {
        entry.status = 'running';
        entry.phase = 'thinking';
        entry.thinking = '';
        entry.streamText = '';
        if (!entry.startedAt) entry.startedAt = Date.now();
        break;
      }
      case 'chunk': {
        const chunk = frame.chunk || {};
        switch (chunk.type) {
          case 'reasoning-delta':
            // 默认不采集思考内容（见构造函数注释）。
            if (chunk.text && this.captureReasoning) {
              entry.thinking = (entry.thinking + chunk.text).slice(-MAX_THINKING);
            }
            entry.phase = 'thinking';
            break;
          case 'text-delta':
            if (chunk.text) entry.streamText = (entry.streamText + chunk.text).slice(-MAX_STREAM);
            entry.phase = 'thinking';
            break;
          case 'tool-call-delta':
            if (!entry.tool) entry.phase = 'thinking';
            break;
          case 'block-start':
            if (chunk.blockType && String(chunk.blockType).includes('tool')) entry.phase = 'tool';
            break;
          default:
            break;
        }
        break;
      }
      default:
        break;
    }
    this.touch(entry, frame.time);
  }

  /**
   * 补种历史：插件热加载或 DSH 重启后，会话里**已经存在**的事件不会重放给监听器，
   * 「当前任务」会空着。这里把每个活着的会话最近一段日志重新折一遍。
   */
  seedSession(session) {
    const id = session?.id || session?.sessionId;
    if (!id) return;
    const end = typeof session.seq === 'number' ? session.seq : 0;
    const from = Math.max(0, end - 200);
    for (let seq = from; seq < end; seq += 1) {
      let event;
      try {
        event = session.eventAt(seq);
      } catch {
        continue;
      }
      if (event) this.sessionEvent(session, event);
    }
    // 不要在这里用 Date.now() 覆盖 lastActivity —— 那会把各会话的事件时间抹平，
    // 导致「谁是主会话」选错。事件自己的时间戳才是有序的依据。
    const entry = this.entry(id);
    if (!entry.lastActivity) entry.lastActivity = Date.now();
    this.dirty = true;
  }

  /** agent/status —— running / idle。 */
  agentStatus(payload) {
    const id = payload?.agent?.id || payload?.agent?.sessionId;
    if (!id) return;
    const entry = this.entry(id);
    const status = payload.status;
    if (status === 'running') {
      entry.status = 'running';
      if (entry.phase === 'idle' || entry.phase === 'done' || entry.phase === 'error') entry.phase = 'thinking';
      if (!entry.startedAt) entry.startedAt = Date.now();
    } else if (status === 'idle') {
      entry.status = 'idle';
      // turn/end 才是权威的收尾信号；这里只在没有它时兜底。
      if (entry.phase !== 'done' && entry.phase !== 'error') {
        entry.phase = 'done';
        entry.doneKind = 'completed';
        entry.doneAt = Date.now();
      }
    }
    this.touch(entry);
  }

  /** agent/created —— 记住子代理身份（它不是人开的会话）。 */
  agentCreated(payload) {
    const id = payload?.agent?.id;
    if (!id) return;
    const entry = this.entry(id);
    if (payload?.parentAgent || payload?.parent) entry.isSubagent = true;
    this.touch(entry);
  }

  /** 子任务计数（subagent/start、subagent/end 是纯计数事件）。 */
  subagentEvent(delta) {
    this.subagentRuns = Math.max(0, this.subagentRuns + delta);
    if (this.primaryId) {
      const entry = this.sessions.get(this.primaryId);
      if (entry) this.touch(entry);
    }
  }

  /**
   * 心跳：有会话在跑的时候刷新 updatedAt，让悬浮框能分清
   * 「任务卡住了」和「DSH 已经没了」。
   *
   * 只在真有 running 会话时才标脏，所以 DSH 空闲时不会平白写盘。
   */
  heartbeat() {
    for (const entry of this.sessions.values()) {
      if (entry.status === 'running') {
        this.dirty = true;
        return;
      }
    }
  }

  /** 供调试：把一次事件的形状写进日志。 */
  debugShape(label, value) {
    this.appendLog(`[${new Date().toISOString()}] ${label} ${JSON.stringify(value)?.slice(0, 400)}\n`);
  }

  /** 卸载：清定时器并把最后状态落盘。 */
  dispose() {
    this.disposed = true;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.dirty = true;
    try { this.flush(); } catch { /* ignore */ }
  }
}
