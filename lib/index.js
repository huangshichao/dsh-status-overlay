/**
 * dsh-status-overlay —— 把「我正在干什么」放到桌面上。
 *
 * 解决的问题：DSH 在浏览器里，可一旦 Agent 开始 computer use（切窗口、点、拖），
 * 浏览器就被别的窗口盖住了 —— 你既看不见进度，也不知道它是不是还在动。
 *
 * 本插件只做两件事：
 *   1. 在宿主进程里**只读**地订阅公开事件，把实时状态折成一份 JSON（lib/store.js）；
 *   2. 确保桌面悬浮框进程活着（lib/spawn.js），由它渲染右下角的置顶窗口
 *      —— 悬浮可展开看逐 token 的思考进展，回合结束弹完成提示（lib/overlay.py）。
 *
 * 它**不改**任何 DSH 状态、不 append 任何会话事件、不注册工具、不注入提示词。
 * 卸载它，DSH 的一切行为保持不变，只是桌面上少一个盒子。
 *
 * @module dsh-status-overlay
 */

import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { StatusStore } from './store.js';
import { spawnOverlay } from './spawn.js';
import { SKILL_NAME, SKILL_DESCRIPTION, SKILL_WHEN_TO_USE, SKILL_CONTENT } from './skill.js';

/** 稳定插件名（也是 cordis.patch.yml 里的 loader entry id）。 */
export const name = 'dsh-status-overlay';

/** 不依赖任何服务：只用到 cordis 核心的 ctx.on / ctx.logger。 */
export const inject = [];

const here = dirname(fileURLToPath(import.meta.url));

/** 悬浮框看门狗：被杀掉后最多 20 秒自己回来。 */
const WATCHDOG_MS = 20000;

/**
 * 读一个本地开关。
 *
 * 优先级：环境变量 > `$DSH_HOME/status-overlay/config.json` > 默认关。
 * 这样「默认保护隐私」和「本机想要就打开」可以同时成立，
 * 而且不需要把任何私人偏好写进仓库。
 *
 * @param {string} key config.json 里的字段名。
 * @param {string} envName 对应的环境变量名。
 * @returns {boolean}
 */
function localFlag(key, envName) {
  if (process.env[envName] === '1') return true;
  try {
    const home = process.env.DSH_HOME || join(homedir(), '.dsh');
    const raw = readFileSync(join(home, 'status-overlay', 'config.json'), 'utf8');
    return JSON.parse(raw)?.[key] === true;
  } catch {
    return false;
  }
}

/**
 * 插件入口。
 * @param {import('@deepseek-ai/cordis').Context} ctx 插件作用域。
 * @returns {() => void} 卸载函数。
 */
export function apply(ctx) {
  let store = null;
  const info = (message) => {
    try {
      ctx.logger?.info?.(`dsh-status-overlay: ${message}`);
    } catch { /* logger 可选 */ }
    store?.debugLine(message);
  };

  store = new StatusStore({
    info,
    // 思考流默认**不采集**：那属于模型私有推理，公开版本不替用户决定要不要落盘。
    // 本机想要就设 DSH_OVERLAY_THINKING=1，或在 config.json 里写
    // { "captureReasoning": true }。
    captureReasoning: localFlag('captureReasoning', 'DSH_OVERLAY_THINKING'),
  });

  /** 统一包一层：监听器抛异常绝不能影响对话。 */
  const on = (eventName, handler) => {
    const disposer = ctx.on(eventName, (...args) => {
      try {
        handler(...args);
      } catch (error) {
        store.logError(error);
      }
    });
    return disposer;
  };

  const disposers = [
    on('session/event', (session, event) => store.sessionEvent(session, event)),
    on('agent/assistant-stream', (payload) => store.assistantStream(payload)),
    on('agent/status', (payload) => store.agentStatus(payload)),
    on('agent/created', (payload) => store.agentCreated(payload)),
    // 会话被恢复/打开时，历史事件不会重放 —— 补种一次，任务栏才不会空着。
    on('session/created', (payload) => store.seedSession(payload?.session || payload)),
    on('subagent/start', () => store.subagentEvent(1)),
    on('subagent/end', () => store.subagentEvent(-1)),
  ];

  // 补种：热加载 / DSH 重启后，会话里已有的事件不会重放，否则「当前任务」是空的。
  // 整块包在 try 里：apply() 抛异常会让整个插件——乃至 `dsh web` 的启动——失败，
  // dsh-desktop-gui 就是这么把宿主搞挂的，这里不能再犯。
  let seedFiber = null;
  try {
    seedFiber = ctx.inject(['sessions'], (sessionCtx) => {
      sessionCtx.effect(() => {
        try {
          for (const session of sessionCtx.sessions.list()) store.seedSession(session);
        } catch (error) {
          store.logError(error);
        }
        return () => {};
      }, 'dsh-status-overlay:seed');
    });
  } catch (error) {
    store.logError(error);
  }

  // 把 playbook 注册成 DSH 技能；没有 skills 服务的 profile 就安静跳过，
  // 注册失败也绝不影响悬浮框。
  let skillFiber = null;
  try {
    skillFiber = ctx.inject(['skills'], (skillCtx) => {
      skillCtx.effect(() => {
        try {
          return skillCtx.skills.register({
            name: SKILL_NAME,
            description: SKILL_DESCRIPTION,
            whenToUse: SKILL_WHEN_TO_USE,
            content: SKILL_CONTENT,
            source: 'bundled',
          });
        } catch (error) {
          store.logError(error);
          return () => {};
        }
      }, 'dsh-status-overlay:skill');
    });
  } catch (error) {
    store.logError(error);
  }

  const ensureOverlay = () => {
    try {
      spawnOverlay({
        pluginDir: here,
        stateDir: store.dir,
        statePath: store.statePath,
        info,
      });
    } catch (error) {
      store.logError(error);
    }
  };

  ensureOverlay();
  const watchdog = setInterval(() => {
    try { ensureOverlay(); } catch { /* 看门狗自己不能死 */ }
    try { store.heartbeat(); } catch { /* ignore */ }
  }, WATCHDOG_MS);
  watchdog.unref?.();

  info(`已挂载，状态文件 ${store.statePath}`);

  return () => {
    clearInterval(watchdog);
    try { seedFiber?.dispose?.(); } catch { /* ignore */ }
    try { skillFiber?.dispose?.(); } catch { /* ignore */ }
    for (const dispose of disposers.reverse()) {
      try { dispose?.(); } catch { /* 卸载尽力而为 */ }
    }
    try { store.dispose(); } catch { /* ignore */ }
  };
}
