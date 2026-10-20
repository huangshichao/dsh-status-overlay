/**
 * dsh-status-overlay / tools/test_mcp.mjs
 *
 * MCP 服务的协议级回归测试：真的起一个子进程、真的走 stdio、真的发 JSON-RPC。
 * 不用 mock —— 验的就是「别人的 MCP 客户端连上来能不能用」。
 *
 * 覆盖：
 *   initialize / tools/list / tools/call(get_status) / tools/call(list_sessions)
 *   / tools/call(wait_for_idle) / 状态文件缺失 / 未知方法
 *
 * 用法：node tools/test_mcp.mjs   退出码 0 = 通过
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const SERVER = join(HERE, '..', 'lib', 'mcp-server.js');

let failures = 0;
function check(label, condition, detail) {
  if (condition) {
    console.log(`  ✓ ${label}`);
  } else {
    failures += 1;
    console.log(`  ✗ ${label}${detail ? `  → ${detail}` : ''}`);
  }
}

/** 起一个服务进程，返回 { request, close }。 */
function startServer(env) {
  const child = spawn(process.execPath, [SERVER], {
    env: { ...process.env, ...env },
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  const pending = new Map();
  let buffer = '';

  child.stdout.setEncoding('utf8');
  child.stdout.on('data', (chunk) => {
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
        continue;
      }
      const entry = pending.get(message.id);
      if (entry) {
        pending.delete(message.id);
        entry.resolve(message);
      }
    }
  });

  let nextId = 1;
  function request(method, params) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`超时：${method}`));
      }, 8000);
      pending.set(id, {
        resolve: (message) => { clearTimeout(timer); resolve(message); },
      });
      child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }

  function notify(method) {
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method, params: {} }) + '\n');
  }

  return { request, notify, close: () => child.kill() };
}

const payload = (response) => {
  const text = response?.result?.content?.[0]?.text ?? '';
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
};

const work = mkdtempSync(join(tmpdir(), 'dsh-mcp-'));
const stateFile = join(work, 'state.json');
const now = Date.now();

writeFileSync(stateFile, JSON.stringify({
  v: 1,
  updatedAt: now,
  pid: process.pid,
  primary: 's1',
  activeCount: 0,
  subagentRuns: 0,
  sessions: {
    s1: {
      id: 's1', status: 'running', phase: 'tool', task: 'MCP 测试任务',
      lastActivity: now, tool: 'bash', toolLabel: '执行命令', toolHint: 'sleep 1',
      thinking: '', streamText: '', steps: [], startedAt: now - 5000,
      doneAt: 0, doneKind: '', error: '', computerUse: false, isSubagent: false,
    },
    s2: {
      id: 's2', status: 'idle', phase: 'done', task: '已完成任务',
      lastActivity: now - 1000, tool: null, toolLabel: '', toolHint: '',
      thinking: '', streamText: '', steps: [], startedAt: now - 9000,
      doneAt: now - 2000, doneKind: 'completed', error: '',
      computerUse: false, isSubagent: false,
    },
  },
}));

const server = startServer({ DSH_OVERLAY_STATE: stateFile });

try {
  console.log('initialize');
  const init = await server.request('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'test-client', version: '0' },
  });
  check('返回协议版本', init.result?.protocolVersion === '2024-11-05', JSON.stringify(init));
  check('声明 tools 能力', init.result?.capabilities?.tools !== undefined);
  check('返回 serverInfo', init.result?.serverInfo?.name === 'dsh-status-overlay');
  server.notify('notifications/initialized');

  console.log('tools/list');
  const list = await server.request('tools/list', {});
  const names = (list.result?.tools || []).map((tool) => tool.name);
  check('三个工具都在', ['get_status', 'list_sessions', 'wait_for_idle'].every((n) => names.includes(n)),
    names.join(','));
  check('每个工具都有 inputSchema',
    (list.result?.tools || []).every((tool) => tool.inputSchema?.type === 'object'));
  check('inputSchema 显式声明 additionalProperties',
    (list.result?.tools || []).every((tool) => tool.inputSchema.additionalProperties === false));

  console.log('tools/call get_status');
  const status = await server.request('tools/call', { name: 'get_status', arguments: {} });
  const body = payload(status);
  check('返回当前任务', body?.current?.task === 'MCP 测试任务', JSON.stringify(body));
  check('带上正在执行的工具', body?.current?.tool === 'bash');
  check('报告会话数', body?.running_sessions === 0);
  check('探测宿主 pid', body?.host_pid_alive === true && body?.host_pid === process.pid);

  console.log('tools/call list_sessions');
  const all = payload(await server.request('tools/call', { name: 'list_sessions', arguments: {} }));
  check('列出两个会话', all?.returned === 2, JSON.stringify(all));
  const onlyRunning = payload(await server.request('tools/call', {
    name: 'list_sessions', arguments: { only_running: true },
  }));
  check('only_running 过滤生效', onlyRunning?.returned === 1, JSON.stringify(onlyRunning));

  console.log('tools/call wait_for_idle');
  const started = Date.now();
  const idle = payload(await server.request('tools/call', {
    name: 'wait_for_idle', arguments: { timeoutSeconds: 10, pollMilliseconds: 100 },
  }));
  check('activeCount=0 时立即返回 idle', idle?.idle === true, JSON.stringify(idle));
  check('没有白等', Date.now() - started < 2000, `${Date.now() - started}ms`);

  console.log('未知方法');
  const unknown = await server.request('tools/unknown', {});
  check('返回 -32601', unknown.error?.code === -32601, JSON.stringify(unknown));

  server.close();

  console.log('状态文件缺失');
  const missing = startServer({ DSH_OVERLAY_STATE: join(work, 'nope.json') });
  const emptyInit = await missing.request('initialize', { protocolVersion: '2024-11-05', capabilities: {} });
  check('缺文件也能初始化', emptyInit.result?.protocolVersion === '2024-11-05');
  const missingStatus = await missing.request('tools/call', { name: 'get_status', arguments: {} });
  const missingBody = missingStatus.result?.content?.[0]?.text ?? '';
  check('缺文件时给出可读提示', missingBody.includes('没有找到状态文件'), missingBody);
  check('缺文件不算错误', missingStatus.result?.isError === false);
  missing.close();
} catch (error) {
  failures += 1;
  console.log(`  ✗ 异常：${error?.message || error}`);
} finally {
  server.close();
  rmSync(work, { recursive: true, force: true });
}

console.log();
if (failures) {
  console.log(`✗ ${failures} 项失败`);
  process.exit(1);
}
console.log('✓ MCP 服务通过：初始化、工具列表、三个工具、缺文件、未知方法');
process.exit(0);
