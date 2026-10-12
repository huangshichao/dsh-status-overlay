/**
 * dsh-status-overlay / lib/skill.js
 *
 * 把这份 playbook 通过 `ctx.skills.register()` 注册成 DSH 技能，
 * 这样它在技能目录里可见、可被按需加载。
 *
 * 正文放在仓库根的 SKILL.md，不内联成模板字符串 —— 那份内容通篇是行内代码
 * （反引号），内联进 JS 模板字符串会被反引号提前截断。放在 .md 里还能直接
 * 复制成 `~/.dsh/skills/<name>/SKILL.md` 的独立技能。
 *
 * 注册是**防御式**的：没有 skills 服务的 profile 依然能加载插件，只是不广播这份
 * playbook；注册失败也绝不能连累悬浮框本身。
 * @module dsh-status-overlay/skill
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

/** kebab-case 技能 id。 */
export const SKILL_NAME = 'status-overlay';

export const SKILL_DESCRIPTION =
  '桌面右下角有一个置顶悬浮框在实时显示 DSH 当前在做什么（任务 / 正在执行的工具 / '
  + '完成与否）。用户问「你在干嘛 / 还在跑吗」、或做 computer use 期间需要确认进度时用它，'
  + '不需要截图。也说明怎么开关思考内容显示、怎么复位位置。';

export const SKILL_WHEN_TO_USE =
  '需要确认 DSH 此刻是否还在工作、正在执行哪一步；用户问进度；'
  + '长时间 computer use 期间浏览器被盖住时向用户说明进度；'
  + '或者悬浮框不出现 / 不置顶 / 位置不对需要排查。';

/** playbook 正文，从仓库根的 SKILL.md 读入。 */
export const SKILL_CONTENT = readFileSync(join(here, '..', 'SKILL.md'), 'utf8');

export const SKILL_SOURCE = 'bundled';
