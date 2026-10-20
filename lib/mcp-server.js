#!/usr/bin/env node
/**
 * dsh-status-overlay / lib/mcp-server.js
 *
 * 一个**零依赖、跨平台**的 MCP 服务（stdio 传输）：把悬浮框的状态文件
 * 暴露成 MCP 工具，任何 MCP 客户端（DSH、Claude Desktop、IDE 插件……）都能查
 * 「DSH 现在在做什么」。
 *
 * 为什么单独做成纯 Node：
 *   · 悬浮框本体依赖 GTK3 + X11，只能在 Linux/类 Unix 桌面跑；
 *   · 但「读一份 JSON 报告状态」这件事与操作系统、CPU 架构完全无关。
 *   所以 MCP 这一层刻意不 import 任何原生模块、不碰 X11、不要求 python，
 *   只要有 Node ≥ 18 就能在任何平台跑。
 *
 * 协议：MCP 的 stdio 传输 —— 一行一个 JSON-RPC 2.0 消息。
 * 只实现需要的四个方法：initialize / tools/list / tools/call / ping。
 *
 * 用法（客户端配置里）：
 *   { "command": "node", "args": ["/path/to/lib/mcp-server.js"] }
 * 或安装后：{ "command": "dsh-status-mcp" }
 */

import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const PROTOCOL_VERSION = '2024-11-05';
const SERVER_INFO = { name: 'dsh-status-overlay', version: '0.2.0' };

/** 状态文件位置：本机可用 DSH_OVERLAY_STATE 直接指定。 */
function statePath() {
  if (process.env.DSH_OVERLAY_STATE) return process.env.DSH_OVERLAY_STATE;
  const home = process.env.DSH_HOME || join(homedir(), '.dsh');
  return join(home, 'status-overlay', 'state.json');
}

/** 读状态；读不到返回 null（不是错误 —— 可能只是还没开始任务）。 */
function readState() {
  try {
    return JSON.parse(readFileSync(statePath(), 'utf8'));
  } catch {
    return null;
  }
}

/** 宿主进程是否还在（纯 Node，Windows/macOS/Linux 都支持）。 */
function hostAlive(pid) {
  const value = Number.parseInt(pid, 10);
  if (!Number.isInteger(value) || value <= 0) return null;
  try {
    process.kill(value, 0);
    return true;
  } catch (error) {
    return error?.code === 'EPERM' ? true : false;
  }
}

function elapsedSeconds(entry, now = Date.now()) {
  const started = entry?.startedAt || 0;
  if (!started) return 0;
  const ended = entry?.doneAt || now;
  return Math.max(0, Math.round((ended - started) / 1000));
}

/** 把一条会话压成给模型看的摘要。 */
function summarize(entry, now) {
  return {
    id: entry.id,
    phase: entry.phase,
    status: entry.status,
    task: entry.task || '',
    tool: entry.tool || null,
    tool_label: entry.toolLabel || null,
    tool_hint: entry.toolHint || null,
    elapsed_seconds: elapsedSeconds(entry, now),
    started_at: entry.startedAt || 0,
    done_at: entry.doneAt || 0,
    done_kind: entry.doneKind || '',
    error: entry.error || '',
    computer_use: entry.computerUse === true,
    is_subagent: entry.isSubagent === true,
  };
}

const TOOLS = [
  {
    name: 'get_status',
    description: '读取 DSH 当前正在执行的任务：状态（thinking/tool/done/error/idle）、'
      + '正在执行的工具及参数摘要、已用时间，以及并行会话数量。'
      + '返回的是**只读快照**，不改任何东西。',
    inputSchema: { type: 'object', properties: {}, additionalProperties: false },
  },
  {
    name: 'list_sessions',
    description: '列出状态文件里记录的所有会话（含已完成的和子代理），按最近活动排序。'
      + '用来判断「还有几个任务在跑」「刚才那个任务结束了没有」。',
    inputSchema: {
      type: 'object',
      properties: {
        only_running: { type: 'boolean', description: '只看正在运行的会话，默认 false' },
      },
      additionalProperties: false,
    },
  },
  {
    name: 'wait_for_idle',
    description: '等待所有会话停下来（没有 running 的），或超时。'
      + '适合在派发任务后确认「干完了没有」，避免反复轮询。',
    inputSchema: {
      type: 'object',
      properties: {
        timeoutSeconds: { type: 'number', description: '最长等待秒数，默认 60，上限 300' },
        pollMilliseconds: { type: 'number', description: '轮询间隔毫秒，默认 500' },
      },
      additionalProperties: false,
    },
  },
];

const text = (value) => [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }];

function missingState() {
  return {
    content: text('没有找到状态文件：' + statePath()
      + '\n（需要先运行 dsh-status-overlay 插件；或把 DSH_OVERLAY_STATE 指向已有文件。）'),
    isError: false,
  };
}

async function callTool(name, args = {}) {
  const now = Date.now();

  if (name === 'get_status') {
    const state = readState();
    if (!state) return missingState();
    const sessions = state.sessions || {};
    const primary = sessions[state.primary]
      || Object.values(sessions).sort((a, b) => (b.lastActivity || 0) - (a.lastActivity || 0))[0];
    const alive = hostAlive(state.pid);
    return {
      content: text({
        // 注意：在容器/沙箱等独立 PID 命名空间里，活着的宿主也可能探测不到，
        // 所以这里只报「pid 可达性」，不当成权威的「DSH 在不在」。
        host_pid: state.pid ?? null,
        host_pid_alive: alive,
        state_age_seconds: state.updatedAt ? Math.round((now - state.updatedAt) / 1000) : null,
        running_sessions: state.activeCount ?? 0,
        subagent_runs: state.subagentRuns ?? 0,
        current: primary ? summarize(primary, now) : null,
      }),
      isError: false,
    };
  }

  if (name === 'list_sessions') {
    const state = readState();
    if (!state) return missingState();
    const all = Object.values(state.sessions || {})
      .sort((a, b) => (b.lastActivity || 0) - (a.lastActivity || 0));
    const rows = args.only_running === true
      ? all.filter((s) => s.status === 'running')
      : all;
    return {
      content: text({
        running_sessions: state.activeCount ?? 0,
        returned: rows.length,
        sessions: rows.map((s) => summarize(s, now)),
      }),
      isError: false,
    };
  }

  if (name === 'wait_for_idle') {
    const timeout = Math.min(300, Math.max(1, Number(args.timeoutSeconds) || 60)) * 1000;
    const poll = Math.max(100, Math.min(5000, Number(args.pollMilliseconds) || 500));
    const deadline = Date.now() + timeout;
    for (;;) {
      const state = readState();
      if (state && (state.activeCount ?? 0) === 0) {
        return {
          content: text({ idle: true, waited_seconds: Math.round((timeout - (deadline - Date.now())) / 1000) }),
          isError: false,
        };
      }
      if (Date.now() >= deadline) {
        return {
          content: text({ idle: false, reason: 'timeout', waited_seconds: Math.round(timeout / 1000) }),
          isError: false,
        };
      }
      await new Promise((resolve) => setTimeout(resolve, poll));
    }
  }

  return { content: text(`未知工具：${name}`), isError: true };
}

/** 处理一条 JSON-RPC 消息；返回 response 对象，通知返回 null。 */
async function handle(message) {
  const { id, method, params } = message || {};
  const isNotification = id === undefined || id === null;

  try {
    if (method === 'initialize') {
      return {
        jsonrpc: '2.0',
        id,
        result: {
          protocolVersion: PROTOCOL_VERSION,
          capabilities: { tools: {} },
          serverInfo: SERVER_INFO,
        },
      };
    }

    if (method === 'ping') return { jsonrpc: '2.0', id, result: {} };

    if (method === 'tools/list') {
      return { jsonrpc: '2.0', id, result: { tools: TOOLS } };
    }

    if (method === 'tools/call') {
      const name = params?.name;
      const args = params?.arguments || {};
      const result = await callTool(name, args);
      return { jsonrpc: '2.0', id, result };
    }

    // 通知（例如 notifications/initialized）不需要回包，也不该报错
    if (isNotification) return null;

    return {
      jsonrpc: '2.0',
      id,
      error: { code: -32601, message: `Method not found: ${method}` },
    };
  } catch (error) {
    if (isNotification) return null;
    return {
      jsonrpc: '2.0',
      id,
      error: { code: -32603, message: String(error?.message || error) },
    };
  }
}

function send(payload) {
  process.stdout.write(JSON.stringify(payload) + '\n');
}

// ── stdio 主循环：一行一条消息 ────────────────────────────────────────────
let queue = Promise.resolve();
let buffer = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buffer += chunk;
  let index = buffer.indexOf('\n');
  while (index >= 0) {
    const line = buffer.slice(0, index).trim();
    buffer = buffer.slice(index + 1);
    index = buffer.indexOf('\n');
    if (!line) continue;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } });
      continue;
    }
    // 串行处理，避免多个 tools/call 交叉输出
    queue = queue.then(async () => {
      const response = await handle(message);
      if (response) send(response);
    });
  }
});

process.stdin.on('end', () => {
  queue.then(() => process.exit(0));
});
