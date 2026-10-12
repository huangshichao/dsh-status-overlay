/**
 * dsh-status-overlay / lib/spawn.js
 *
 * 保证桌面悬浮框那个进程活着。
 *
 * 关键点：
 *   · **幂等且便宜**。先读锁文件里的 pid，活着就什么都不做；真的需要拉起时才
 *     spawn。悬浮框自己也用 flock 抢锁，所以即使并发拉起也只会活一个。
 *   · **不留文件描述符**。日志 fd 在 spawn 返回后必须关掉 —— 子进程已经 dup
 *     走了自己那一份，父进程留着只会一路泄漏（这个看门狗 20s 跑一次，不关的话
 *     几小时就能把宿主的 fd 用光）。
 *   · **detached + unref**。它是桌面上的独立窗口，不该随插件卸载或 DSH 重启而死，
 *     也不该让宿主因为它卡住。
 *   · **显式 GDK_BACKEND=x11**。Deepin 是 Wayland 会话，但 XWayland 在
 *     （DISPLAY=:0）。走 X11 后端 `set_keep_above` 才真正生效。
 * @module dsh-status-overlay/spawn
 */

import { spawn } from 'node:child_process';
import { closeSync, mkdirSync, openSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

/** 候选解释器：系统 python3 才有 gi(GTK)；conda 那个没有。 */
function pythonCandidates() {
  if (process.env.DSH_OVERLAY_PYTHON) return [process.env.DSH_OVERLAY_PYTHON];
  return ['/usr/bin/python3', '/usr/bin/python3.7', 'python3'];
}

/**
 * 悬浮框是否已经在跑（读锁文件里的 pid）。
 * @param {string} lockPath 悬浮框写入自己 pid 的锁文件。
 * @returns {boolean}
 */
function overlayAlive(lockPath) {
  try {
    const pid = Number.parseInt(readFileSync(lockPath, 'utf8').trim(), 10);
    if (!Number.isInteger(pid) || pid <= 0) return false;
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM = 进程存在但没权限发信号，仍然算活着。
    return error?.code === 'EPERM';
  }
}

/**
 * 拉起悬浮框（已在跑则什么都不做）。
 * @param {object} options
 * @param {string} options.pluginDir 插件目录（overlay.py 在 lib/ 下）。
 * @param {string} options.stateDir 状态目录（日志与锁放这里）。
 * @param {string} options.statePath 状态 JSON 绝对路径。
 * @param {(m: string) => void} [options.info] 宿主日志。
 * @returns {boolean} 是否真的发起了一次 spawn。
 */
export function spawnOverlay({ pluginDir, stateDir, statePath, info = () => {} }) {
  try {
    mkdirSync(stateDir, { recursive: true, mode: 0o700 });
  } catch { /* 已存在即可 */ }
  const script = join(pluginDir, 'overlay.py');
  const lock = join(stateDir, 'overlay.lock');
  const logFile = join(stateDir, 'overlay.log');

  if (overlayAlive(lock)) return false;

  let out = null;
  try {
    out = openSync(logFile, 'a');
  } catch { /* 没有日志文件也要能跑 */ }

  try {
    let lastError = null;
    for (const python of pythonCandidates()) {
      try {
        const child = spawn(python, [script, '--state', statePath, '--lock', lock], {
          detached: true,
          stdio: out === null ? 'ignore' : ['ignore', out, out],
          env: {
            ...process.env,
            GDK_BACKEND: process.env.DSH_OVERLAY_GDK_BACKEND || 'x11',
            DISPLAY: process.env.DISPLAY || ':0',
          },
        });
        child.on('error', (error) => info(`悬浮框启动失败(${python})：${error.message}`));
        child.unref();
        info(`已拉起悬浮框：${python} ${script}`);
        return true;
      } catch (error) {
        lastError = error;
      }
    }
    info(`悬浮框启动失败：${lastError?.message ?? '没有可用的 python3'}`);
    return false;
  } finally {
    // 子进程已经 dup 走了自己的那一份；父进程这份必须关，否则每次调用漏一个 fd。
    if (out !== null) {
      try { closeSync(out); } catch { /* ignore */ }
    }
  }
}
