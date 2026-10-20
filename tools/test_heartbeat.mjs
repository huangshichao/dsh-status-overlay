/**
 * dsh-status-overlay / tools/test_heartbeat.mjs
 *
 * 回归测试：心跳必须真的把 `updatedAt` 推进。
 *
 * 曾经的 bug：heartbeat() 只写 `this.dirty = true`，**没有调度 flush**。
 * 于是长时间工具调用期间没有任何会话事件，`flush()` 永远不触发，
 * `updatedAt` 停在最后一个事件上 —— 悬浮框 60s 后误报「宿主已断开」。
 *
 * 用法：node tools/test_heartbeat.mjs   退出码 0 = 通过
 */

import { readFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

import { StatusStore } from '../lib/store.js';

const dir = join(tmpdir(), `dsh-overlay-hb-${process.pid}`);
rmSync(dir, { recursive: true, force: true });

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const readState = () => JSON.parse(readFileSync(join(dir, 'state.json'), 'utf8'));

const store = new StatusStore({ dir, info: () => {} });

// 进入 running，写一次盘
store.sessionEvent({ id: 'a' }, { type: 'turn/start', time: Date.now(), data: { turn: 1 } });
store.flush();
const before = readState().updatedAt;

// 模拟「长时间工具调用」：之后不再有任何事件，只靠心跳。
await sleep(250);
store.heartbeat();
await sleep(400);
const after = readState().updatedAt;

let failed = false;

if (!(after > before)) {
  console.error(`✗ 心跳没有推进 updatedAt（${before} -> ${after}）—— 长时间任务会被误判为断开`);
  failed = true;
} else {
  console.log(`✓ 心跳推进了 updatedAt：${before} -> ${after}（+${after - before}ms）`);
}

// 空闲时不该平白写盘
const idleStore = new StatusStore({ dir: join(dir, 'idle'), info: () => {} });
idleStore.sessionEvent({ id: 'b' }, { type: 'turn/start', time: Date.now(), data: { turn: 1 } });
idleStore.sessionEvent({ id: 'b' }, { type: 'turn/end', time: Date.now(), data: { reason: { kind: 'completed' } } });
idleStore.flush();
const idleBefore = JSON.parse(readFileSync(join(dir, 'idle', 'state.json'), 'utf8')).updatedAt;
idleStore.heartbeat();          // 没有 running 会话
await sleep(300);
const idleAfter = JSON.parse(readFileSync(join(dir, 'idle', 'state.json'), 'utf8')).updatedAt;
if (idleAfter !== idleBefore) {
  console.error('✗ 空闲时心跳仍然写盘（没必要，会一直磨磁盘）');
  failed = true;
} else {
  console.log('✓ 空闲时心跳不写盘');
}

rmSync(dir, { recursive: true, force: true });
process.exit(failed ? 1 : 0);
