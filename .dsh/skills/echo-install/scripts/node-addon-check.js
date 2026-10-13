// =====================================================================
// node-addon-check.js - 标准版 harness 装完后的「原生模块」自检
//
// 谁在调它：harness-install-local.ps1（装完/复用已有安装时各一次）
//   用法： node node-addon-check.js <标准版安装目录>
//         例如 node node-addon-check.js C:\ECHO\dsh\app
//   退出码：0 = 全部通过；1 = 有原生模块加载不了（stderr 说明哪个、为什么）
//
// 为什么必须有这一步（2026-10-13 同事新机器实测，见 AGENTS.md）：
//   只在 CLI 入口上冒烟（`node bin.js --version`）**抓不到** koffi —— 那条链不 require 它。
//   而 koffi 是这些包的原生依赖，正是跑 pwsh / 跑子进程的必经之路：
//     @deepseek-ai/dsh-fs、dsh-subprocess(-local)、dsh-session-persistence-jsonl、
//     dsh-sandbox-windows-acl、dsh-win32-process、dsh-host-directory-picker-*
//   典型故障：npm 装到 koffi 时它的 install 脚本调 `node ./cnoke.cjs` 现场编译，
//   而子进程的 PATH 里没有 node -> "'node' 不是内部或外部命令" -> 整次安装失败，
//   脚本退到 npx 缓存复制一棵"看起来能用"的树 -> 旧脚本照样打印「已就绪」。
//   本文件就是那句话的判据：**真 require 一次**，加载不了就别报成功。
//
// 一个踩过的坑（写这份自检时实测）：**不能**用
//   createRequire(...).resolve('koffi/package.json')
//   去判断"包在不在" —— koffi 的 package.json 里有 `exports` 字段且没暴露 './package.json'，
//   于是明明装着也会抛 ERR_PACKAGE_PATH_NOT_EXPORTED，把好树误判成坏的。
//   所以下面一律：读版本走文件路径（node_modules 的布局本来就是公开契约），
//   判断可加载才走 createRequire().resolve(包名)。
// =====================================================================
'use strict';

const { createRequire } = require('node:module');
const { join } = require('node:path');
const { existsSync, readFileSync } = require('node:fs');

const target = process.argv[2];
if (!target) {
  console.error('用法：node node-addon-check.js <标准版安装目录>');
  process.exit(2);
}

// 从「安装目录/package.json」建 require —— 解析路径就等同于**运行时**的解析路径。
let req;
try {
  req = createRequire(join(target, 'package.json'));
} catch (e) {
  console.error(`建 require 失败（${target}）：${e.message}`);
  process.exit(1);
}

// 包在不在：直接看 node_modules 布局（不受 exports 限制）。
function pkgDir(name) {
  const dir = join(target, 'node_modules', ...name.split('/'));
  return existsSync(join(dir, 'package.json')) ? dir : null;
}

// 读版本号（读不到也不影响"能不能加载"的判定）。
function pkgVersion(dir) {
  try {
    return JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8')).version || '?';
  } catch {
    return '?';
  }
}

const failures = [];
const passed = [];

// --- 1. koffi：本次事故的主角 ---
const koffiDir = pkgDir('koffi');
if (!koffiDir) {
  console.log('跳过 koffi（这份安装里没有它）');
} else {
  try {
    // 加载 koffi 就会去取 **预构建二进制**
    // （node_modules/@koromix/koffi-<平台>-<架构>/<平台>_<架构>/koffi.node）。
    // 这一步失败 = 预构建没就位、而现场编译也没成功 —— 正是要抓的情况。
    const koffi = req('koffi');
    const version = pkgVersion(koffiDir);
    if (typeof koffi.load !== 'function') {
      // 能 require 但没有 API = 拿到的不是能用的那份（残包 / 版本错配）
      failures.push(`koffi@${version} 能加载但没有 load()，这份不可用`);
    } else {
      // 顺带报出预构建包在不在：出问题时一眼看出是"没拉预构建"还是"ABI 不匹配"
      const prebuilt = pkgDir(`@koromix/koffi-${process.platform}-${process.arch}`);
      passed.push(`koffi@${version}（预构建：${prebuilt ? '有' : '无，用的可能是编译产物'}）`);
    }
  } catch (e) {
    failures.push(`koffi 加载失败：${e && e.message ? e.message.split('\n')[0] : e}`);
  }
}

// --- 2. node-pty：CLI 冒烟那边也只是"文件在不在"，这里真加载一遍 ---
const ptyDir = pkgDir('node-pty');
if (ptyDir) {
  try {
    req('node-pty');
    passed.push(`node-pty@${pkgVersion(ptyDir)}`);
  } catch (e) {
    // node-pty 是终端功能用的；加载不了不一定致命，但必须**说出来**，不能静默。
    failures.push(`node-pty 加载失败：${e && e.message ? e.message.split('\n')[0] : e}`);
  }
}

if (failures.length > 0) {
  for (const f of failures) console.error(`[fail] ${f}`);
  process.exit(1);
}

console.log(`原生模块自检通过（${passed.length} 项：${passed.join('、')}）`);
process.exit(0);
