/* ECHO 控制面板前端逻辑（原生 JS，无构建链） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel, root = document) => [...(root || document).querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/** 候选项统一成 `{value,label}` 两种形态都吃：
 *  字符串（枚举，值即名字）与 `{value,label}`（值给程序、名字给人看 —— 例如输入设备的
 *  「3 · 耳机 (Realtek)」，那个字符串没法当 int 存回设置里）。 */
const optionPairs = (options) => (options || []).map((o) => (o && typeof o === "object")
  ? { value: o.value, label: o.label != null ? o.label : o.value }
  : { value: o, label: o });

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}
const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });

function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.add("hidden"), ms);
}

/** 保存设置后的提示：**联动没成功的要当场说出来**。
 *
 *  后端 `PUT /api/settings` 会把联动结果放在 `effects` 里。像「命令转写引擎改成了
 *  sherpa，但这台机器没装 sherpa_onnx」这种事，若只提示"已保存"，用户要等到说第一句
 *  命令时才发现——那时表现为"录音正常但没反应"，极难自己查到（2026-09-23 事故）。
 */
function toastSaved(r, fallback = "已保存") {
  const bad = (((r || {}).effects) || []).filter((e) => e && e.ok === false);
  if (bad.length) {
    toast("已保存，但有联动没成功：" + bad.map((e) => e.detail).join("；"), 8000);
    return;
  }
  toast(fallback);
}

/**
 * 自绘确认弹窗（替代浏览器原生 confirm）。
 *
 * 为什么不用原生 confirm：2026-09-12 用户反馈"结束录音的确认窗口显示不全"。
 * 边条是 450 逻辑像素宽的 WebView2，宿主窗口是 DPI 感知的 WinForms 窗体，原生
 * 对话框的尺寸/缩放由浏览器接管，窄窗 + 高 DPI 下会出现裁切且无法用 CSS 修正。
 * 自绘后用页面自己的布局：宽度 min(420px, 92vw)、按钮自动换行、长文本折行。
 *
 * @returns {Promise<boolean>} 确认=true，取消/ESC/点遮罩=false
 */
function confirmDialog(message, { okText = "确定", cancelText = "取消", danger = false } = {}) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "cdlg-mask";
    wrap.innerHTML = `
      <div class="cdlg" role="dialog" aria-modal="true">
        <div class="cdlg-msg">${esc(message)}</div>
        <div class="cdlg-actions">
          <button class="btn" data-act="cancel">${esc(cancelText)}</button>
          <button class="btn ${danger ? "danger" : "primary"}" data-act="ok">${esc(okText)}</button>
        </div>
      </div>`;
    const done = (answer) => {
      document.removeEventListener("keydown", onKey, true);
      wrap.remove();
      resolve(answer);
    };
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); done(false); }
      else if (e.key === "Enter") { e.preventDefault(); done(true); }
    };
    wrap.addEventListener("click", (e) => {
      const act = e.target.closest("[data-act]");
      if (act) { done(act.dataset.act === "ok"); return; }
      if (e.target === wrap) done(false);   // 点遮罩 = 取消
    });
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(wrap);
    const ok = wrap.querySelector('[data-act="ok"]');
    if (ok) ok.focus();
  });
}

/* ================= 视图切换 ================= */
/* 页签清单（2026-09-25 整合后）：新加页签要同时在 index.html 里加
   <button class="tab" data-view="…"> 和 <section id="view-…">，否则 switchView 找不到容器。
   同日「向导」也并进「常规」最后一张卡（默认收起），所以顶层只剩 7 个。 */
const _VIEWS = ["dashboard", "general", "business", "capability",
                "history", "meetings"];

/* 旧入口 → 新页签（深链/书签/别处硬编码的兼容层）：
   2026-09-26 用户定的设置信息架构（IA）= **三类 + 一个历史位**：

     1. ECHO 通用      —— 快捷键 · 打开/启动形式 · 运行状态（服务/端口/版本/重启停止）
     2. 业务配置       —— 会议 · 语音指令 · 朗读反馈 · 队列 · 工作区与归档
     3. 能力与智能体   —— 智能体选择 · 模型路由（合并卡）· 设备选择
                          ├ 已配对后端连接情况
                          ├ 本地能力部署运行情况（默认只展开"配置为要用的"）
                          └ 清理（先预览、后确认）
     4. 历史           —— 指令历史 · 会议历史（**这一轮只预留位置/空态**）

   原来的「常规 / 语音与设备 / 智能体 / 能力后端」四个设置页签就是被这三类**替掉**的
   （内容一项没少，只是重新分组 + 默认折叠，见 docs/设置项归属表.md）。
   深链折算： */
const VIEW_ALIASES = {
  settings: "general", boot: "general", wizard: "general",
  voice: "business", "语音与设备": "business", meetingsettings: "business",
  failover: "capability", model: "capability", models: "capability",
  capabilities: "capability", agent: "capability",
};
function normView(name) {
  const n = String(name || "");
  return VIEW_ALIASES[n] || n;
}

function switchView(name) {
  const view = _VIEWS.includes(normView(name)) ? normView(name) : "dashboard";
  $$(".view").forEach((v) => v.classList.add("hidden"));
  const host = $(`#view-${view}`);
  if (host) host.classList.remove("hidden");
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  if (view === "dashboard") { refreshDashboard(); loadTargets(); }
  // 三个设置页签共用一份设置数据：loadSettings() 一次把三页的卡片都画出来（切页不重拉）。
  // 「ECHO 通用」里还挂着启动状态与**安装向导**卡，所以那一页要多拉两样。
  if (view === "general") { loadSettings(); loadBoot(); loadBootLogs(); loadWizard(); }
  if (view === "business") { loadSettings(); loadQueueCard(); }
  if (view === "capability") { loadSettings(); loadCapabilities(); loadRouter(); }
  if (view === "history") loadHistory();
  if (view === "meetings") { loadMeetings(); refreshMeetingHeader(); }
}
$$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));

/** 打开「常规」页最后那张「安装向导」卡（原 `switchView("wizard")` 的去处）。
 *  三件事一起做，缺一样用户体验就是"点了没反应"：切到常规 → **展开**那张卡（并把它从
 *  localStorage 的折叠集合里去掉，否则下次重绘又被收起来）→ 拉一次向导数据 → 滚到它。 */
async function gotoWizard() {
  switchView("general");
  const card = $("#wizCard");
  if (card) {
    card.classList.remove("collapsed");
    card.querySelector(":scope > .card-title")?.setAttribute("aria-expanded", "true");
    const collapsed = _collapsedCards();
    if (collapsed.delete("wizard")) _saveCollapsedCards(collapsed);
  }
  try { await loadWizard(); } catch (e) { /* 拉不到就只展开；卡片自己会显示失败原因 */ }
  const after = $("#wizCard");
  if (after) after.scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ---------------- 安装状态横幅（技能优先，2026-09-21） ----------------
   安装现在多半是**助手按 echo-install 技能**在用户自己的 agent 里完成的
   （docs/安装-技能优先.md）。面板因此不再用"首装"当"进向导"的理由 —— 那条判据原本
   只有向导末页才写，于是技能装完打开面板还是进向导。改成：默认进仪表盘，顶部给一条
   **不打断**的横幅说清"登记了没有 / 还缺什么 / 下一步干什么"，向导降级为手动入口。 */
/* 横幅原来只在页面启动时拉一次 —— harness 后来起来了，它还写着"还有 1 项没就绪"，
   与智能体表格里的"可用"看着互相打脸（2026-09-22 用户实测反馈）。
   改成：组件状态一变就立刻重拉，否则最多每 60 秒重拉一次（别把它变成轮询负担）。 */
let _installNoticeAt = 0;
let _installNoticeSig = "";
function maybeRefreshInstallNotice(st) {
  const sig = JSON.stringify((st && st.components) || []);
  const now = Date.now();
  if (sig === _installNoticeSig && now - _installNoticeAt < 60000) return;
  _installNoticeSig = sig;
  _installNoticeAt = now;
  renderInstallNotice().catch(() => {});
}

async function renderInstallNotice() {
  let st = null;
  try { st = await api("/api/install/state"); } catch (e) { return; }   // 取不到就不打扰
  if (!st) return;
  let box = $("#installNotice");
  if (!box) {
    box = document.createElement("div");
    box.id = "installNotice";
    const main = document.querySelector("main");
    if (!main) return;
    main.insertBefore(box, main.firstChild);
  }
  const miss = st.missing || [];
  if (st.declared && !miss.length) {
    // 装好了就别占地方；只在"能力"页给一句可查的结论（那里才是补能力的地方）
    box.innerHTML = "";
    box.dataset.state = "ok";
    return;
  }
  const rep = st.report || {};
  const items = miss.slice(0, 4).map((m) =>
    `<li>${esc(m.feature || "")} —— ${esc(m.reason || "")}${m.fix ? `<span class="muted">（${esc(m.fix)}）</span>` : ""}</li>`).join("");
  const head = st.declared
    ? `安装已登记${rep.savedAt ? "（" + esc(rep.savedAt) + "）" : ""}，但还有 ${miss.length} 项没就绪`
    : `这台机器还没登记安装完成`;
  const how = st.declared
    ? `可以现在补：面板 → 能力；或让助手再跑一次 echo-install 技能（已装好的会跳过）。`
    : `推荐做法：把分享包里的 <code>echo-install</code> 文件夹交给你的 AI 助手，说「按 echo-install 这个技能给我装 ECHO」。<b>没有助手</b>也可以点下面的「手动向导」按界面走一遍。`;
  box.dataset.state = "warn";
  box.innerHTML = `<div class="install-notice">
      <div><b>${head}</b></div>
      <div class="muted">${how}</div>
      ${items ? `<ul>${items}</ul>` : ""}
      <div><button class="btn" id="installGoCap">看还缺什么</button>
           <button class="btn ghost" id="installGoWizard">手动向导</button></div>
    </div>`;
  const cap = $("#installGoCap", box);
  if (cap) cap.addEventListener("click", () => switchView("capability"));
  const wiz = $("#installGoWizard", box);
  // 「向导」不再是页签：切到常规 + 展开那张卡 + 滚过去（见 gotoWizard）
  if (wiz) wiz.addEventListener("click", () => gotoWizard());
}

/* ---------------- 启动自愈提示（A3，2026-09-22） ----------------
   后端启动时若发现上次被强杀留下的数据库日志（echo.db-wal / -shm）并做了归位，
   会把一句人话放进 /api/status 的 startupNotes。这里**只提示一次**（刷新后又出现会烦人），
   点「知道了」即收 —— 不打断、不阻断。 */
let _startupNotesSeen = false;
function renderStartupNotes(st) {
  if (_startupNotesSeen) return;
  const notes = (st && st.startupNotes) || [];
  if (!notes.length) return;
  _startupNotesSeen = true;
  let box = $("#startupNotes");
  if (!box) {
    box = document.createElement("div");
    box.id = "startupNotes";
    const main = document.querySelector("main");
    if (!main) return;
    main.insertBefore(box, main.firstChild);
  }
  box.dataset.state = "warn";
  box.innerHTML = `<div class="install-notice">
      <div><b>启动时自动处理了一处异常</b></div>
      <ul>${notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>
      <div class="muted">常见原因是上次 ECHO 不是正常退出的（关机 / 任务管理器结束进程 / 掉电）。
        已经处理好了，正常用即可。</div>
      <div><button class="btn ghost" id="startupNotesOk">知道了</button></div>
    </div>`;
  const ok = $("#startupNotesOk", box);
  if (ok) ok.addEventListener("click", () => box.remove());
}

/* ---------------- 可折叠卡片 ----------------
   给卡片加 class="collapsible" 与 data-collapse-id，点标题就能折叠/展开，状态记在
   localStorage（跨刷新/重开保持）。支持两种卡片：
     * `.card`  —— 标题 `.card-title`，内容 `.card-body`（静态卡片，如路由参数）；
     * `.mcard` —— 标题 `.mcard-head`，内容 `.mcard-body`（能力页签里动态渲染的卡片）。
   与设置页的分组折叠是**两套存储**：一个记分组、一个记卡片，互不影响。 */
const CARD_COLLAPSE_KEY = "echo.panel.collapsedCards";
const COLLAPSE_TITLE_SEL = ".card.collapsible > .card-title, .mcard.collapsible > .mcard-head";
const COLLAPSE_ANY_SEL = ".card.collapsible, .mcard.collapsible";
const COLLAPSE_TITLE_CHILD = ":scope > .card-title, :scope > .mcard-head";

function _collapsedCards() {
  try { return new Set(JSON.parse(localStorage.getItem(CARD_COLLAPSE_KEY) || "[]")); }
  catch (e) { return new Set(); }
}
function _saveCollapsedCards(set) {
  try { localStorage.setItem(CARD_COLLAPSE_KEY, JSON.stringify([...set])); } catch (e) { /* 忽略 */ }
}
/* 「默认收起」的卡片种子（2026-09-25，为「常规 → 安装向导」那张卡加的）：
   折叠状态是"在集合里 = 收起"，而"用户从没表态过"和"用户手动展开过"在集合里长得一样
   （都不在集合里）。所以用**一个一次性标记**记住"这些卡已经按默认值种子过了"：
   只在第一次见到 `data-collapse-default="closed"` 的卡时把它的 id 塞进折叠集合，
   之后完全由用户自己的点击决定（展开就是展开，下次打开还是展开）。 */
const COLLAPSE_SEEDED_KEY = "echo.panel.collapseDefaultsSeeded";
function _seedCollapseDefaults() {
  let seeded = null;
  try { seeded = new Set(JSON.parse(localStorage.getItem(COLLAPSE_SEEDED_KEY) || "[]")); }
  catch (e) { seeded = new Set(); }
  const want = $$('[data-collapse-default="closed"]')
    .map((card) => card.dataset.collapseId || card.id)
    .filter((id) => id && !seeded.has(id));
  if (!want.length) return;
  const collapsed = _collapsedCards();
  want.forEach((id) => { collapsed.add(id); seeded.add(id); });
  _saveCollapsedCards(collapsed);
  try { localStorage.setItem(COLLAPSE_SEEDED_KEY, JSON.stringify([...seeded])); } catch (e) { /* 忽略 */ }
}
function _cardTitleOf(el) {
  // 只认"直接子标题"：卡片里嵌套的其它标题（如设置页分组）不该触发卡片折叠
  return el.closest(COLLAPSE_TITLE_SEL) || null;
}
function toggleCollapsibleCard(card) {
  if (!card) return;
  const id = card.dataset.collapseId || card.id;
  if (!id) return;
  const nowCollapsed = card.classList.toggle("collapsed");
  const title = card.querySelector(COLLAPSE_TITLE_CHILD);
  if (title) title.setAttribute("aria-expanded", String(!nowCollapsed));
  const collapsed = _collapsedCards();
  if (nowCollapsed) collapsed.add(id); else collapsed.delete(id);
  _saveCollapsedCards(collapsed);
}
/** 应用已保存的折叠状态。**动态卡片每次重绘后都要调一次**（能力页签渲染完会调）。 */
function applyCollapsedCards(root) {
  _seedCollapseDefaults();                 // 「默认收起」的卡（安装向导）第一次先落进折叠集合
  const collapsed = _collapsedCards();
  $$(COLLAPSE_ANY_SEL, root).forEach((card) => {
    const id = card.dataset.collapseId || card.id;
    const isCollapsed = collapsed.has(id);
    card.classList.toggle("collapsed", isCollapsed);
    const title = card.querySelector(COLLAPSE_TITLE_CHILD);
    if (title) {
      title.setAttribute("role", "button");
      title.setAttribute("tabindex", "0");
      title.setAttribute("aria-expanded", String(!isCollapsed));
    }
  });
}
/** 「有内容才展开」的卡：默认收起（`data-collapse-default="closed"`），但某件事成立时
 *  **自动展开一次**，之后完全听用户的（他收起就是收起）。
 *
 *  目前只有一处用它：「已配对后端连接情况」—— 没配对时它没什么可看的（收着省地方），
 *  真配上了就该让人一眼看到健康状态（用户："下面附已配对后端连接情况"）。
 *  用一次性标记（localStorage）而不是每次渲染都展开：否则用户手动收起后，下一次轮询
 *  又把它弹开 —— 那种"界面自己动"的毛病以前踩过。 */
const AUTO_EXPAND_KEY = "echo.panel.autoExpanded";
function autoExpandOnce(collapseId, wantOpen) {
  if (!wantOpen) return;
  const card = document.querySelector('[data-collapse-id="' + collapseId + '"]');
  if (!card) return;
  let done = null;
  try { done = new Set(JSON.parse(localStorage.getItem(AUTO_EXPAND_KEY) || "[]")); }
  catch (e) { done = new Set(); }
  if (done.has(collapseId)) return;
  done.add(collapseId);
  try { localStorage.setItem(AUTO_EXPAND_KEY, JSON.stringify([...done])); } catch (e) { /* 忽略 */ }
  card.classList.remove("collapsed");
  const collapsed = _collapsedCards();
  if (collapsed.delete(collapseId)) _saveCollapsedCards(collapsed);
}
document.addEventListener("click", (e) => {
  // 「? 」浮窗：点一下开/关，点别处关掉（窄边条里悬停也可能点，两种都要能用）。
  // 这一条必须排在卡片折叠之前 —— 否则点 `?` 会顺带把整张卡收起来。
  const q = e.target.closest(".sq");
  $$(".sq.open").forEach((x) => { if (x !== q) x.classList.remove("open"); });
  if (q) { q.classList.toggle("open"); return; }
  const title = _cardTitleOf(e.target);
  if (title) toggleCollapsibleCard(title.parentElement);
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const title = _cardTitleOf(e.target);
  if (title) { e.preventDefault(); toggleCollapsibleCard(title.parentElement); }
});

/* ================= 仪表盘 ================= */
let _levelTimer = null;   // 录音电平轮询

/** 驱动麦克风电平条（5 根竖条，按相位错开形成波动感）。
 *  sel 可指定目标：会议录音用 #micLevel，语音命令收音用 #cmdLevel。 */
function setMicLevel(level, sel = "#micLevel") {
  const bars = $$(sel + " i");
  if (!bars.length) return;
  const v = Math.max(0, Math.min(1, level || 0));
  bars.forEach((b, idx) => {
    const phase = 0.5 + ((idx * 37) % 13) / 26;          // 0.5~1.0 相位差
    const h = v <= 0.02 ? 5 : Math.max(6, Math.min(100, v * 100 * phase));
    b.style.height = h + "%";
  });
}

/* "说话"按钮的进行态：点下去立刻有反馈，三个阶段文字/配色不同（用户 2026-09-12）。
   状态来自 /api/status 的 busy+busyPhase，所以热键/窄条发起的语音也会同步显示。 */
const CAPTURE_PHASE = {
  listening:    { text: "🎤 正在听…", cls: "listening", tip: "正在收音，说完停一下就会自动结束" },
  transcribing: { text: "✍ 转写中…",  cls: "working",   tip: "正在把语音转成文字" },
  running:      { text: "⏳ 处理中…",  cls: "working",   tip: "已发给 DSH，正在执行（详情看会话）" },
};

function renderCaptureBtn(phase) {
  const btn = $("#btnCapture");
  if (!btn) return;
  const s = phase ? (CAPTURE_PHASE[phase] || CAPTURE_PHASE.running) : null;
  btn.classList.toggle("listening", !!s && s.cls === "listening");
  btn.classList.toggle("working", !!s && s.cls === "working");
  btn.textContent = s ? s.text : "🎤 说话";
  btn.title = s ? s.tip : "点一下开始语音命令";
  btn.disabled = !!phase;
}

/* ================= 模型路由（仪表盘小卡片） ================= */
/** 健康表 → 一眼能读懂的组状态：当前请求会走哪个通道（通道1/通道2…）/ 全挂 / 未运行。
 *  通道号就是 member.priority。 */
function routerVerdict(d) {
  const nm = (m, i) => `通道${m.priority || i + 1} ${m.name}`;
  const g = (d.groups || []).find((x) => x.id === "echo-auto") || (d.groups || [])[0];
  if (!d.proxy_online) return { kind: "offline", text: "路由未运行", cls: "error", color: "--red",
                                tip: "模型路由进程没在跑：ECHO AUTO 会直接失败（重启 ECHO 可自动拉起）" };
  if (!g || !(g.members || []).some((m) => m.enabled !== false)) {
    return { kind: "idle", text: "无可用通道", cls: "idle", color: "--muted", tip: "模型组里没有启用的通道" };
  }
  const act = g.members.filter((m) => m.enabled !== false);
  const up = (m) => m.reachable !== false && m.state !== "open";
  const first = act[0];
  const alive = act.filter(up);
  if (up(first)) {
    return { kind: "first", text: nm(first, 0), cls: "online", color: "--green",
             tip: `${act.length} 个通道里，请求走 ${nm(first, 0)}` +
                  (alive.length > 1 ? `（后面还有 ${alive.length - 1} 个可用）` : "") };
  }
  if (alive.length) {
    const k = act.indexOf(alive[0]);
    return { kind: "fallback", text: nm(alive[0], k), cls: "idle", color: "--yellow",
             tip: `${nm(first, 0)} 当前不可用（${first.last_error || first.detail}），请求改走 ${nm(alive[0], k)}` };
  }
  return { kind: "down", text: "无可用通道", cls: "error", color: "--red",
           tip: act.map((m, i) => `${nm(m, i)}：${m.last_error || m.detail}`).join("；") };
}

/** 卡片第二行：只留每个通道各自的命中次数，末尾靠右补上次派发时刻。
 *  「失败/共」这类运营汇总不放在仪表盘，挪到模型路由页的「派发情况」卡片（#rtStats）。 */
function routerCounts(d) {
  const g = (d.groups || []).find((x) => x.id === "echo-auto") || (d.groups || [])[0] || {};
  const mem = (g.members || []).filter((m) => m.enabled !== false);
  const rt = d.routes || {};
  const out = mem.map((m, i) => `<span class="fo-k">通道${m.priority || i + 1} <b>${m.ok || 0}</b></span>`);
  out.push(`<span class="muted fo-time">${rt.last_route_at || "尚无请求"}</span>`);
  return out.join("");
}

async function refreshFailoverCard() {
  const card = $("#failoverCard");
  if (!card) return;
  try {
    const d = await api("/api/failover/health");
    const v = routerVerdict(d);
    // 状态做成会议录音那种椭圆徽章：文字+描边同色，一眼看出通道通不通
    const st = $("#foStateText");
    st.className = `badge ${v.cls}`;
    st.textContent = v.text;
    card.title = v.tip + "\n（点一下进模型路由页）";
    const c = $("#foCounts");
    if (c) c.innerHTML = routerCounts(d);
  } catch (e) {
    const st = $("#foStateText");
    st.className = "badge error";
    st.textContent = "ECHO 离线";
    const c = $("#foCounts");
    if (c) c.innerHTML = `<span class="muted fo-time">读不到路由状态：${esc(e.message)}</span>`;
  }
}

function gotoFailover() {
  switchView("agent");          // 模型路由的内容现在在「智能体」页签（2026-09-25 整合）
}

/* ================= 模型路由（管理页：成员/优先级/启停/注册） ================= */
let _rtMembers = [];        // 本地编辑副本（保存前的改动都在这里）
let _rtMeta = {};           // 组元信息
let _rtCands = [];          // 候选模型（来自 DSH 配置）
let _rtView = {};           // 最近一次 /api/router/status 的原始返回
let _rtDirty = false;
let _rtOpen = new Set();    // 展开了详情的行（按通道号）

const RT_STATE = {
  closed: { cls: "on", text: "可用" },
  open: { cls: "err", text: "熔断中" },
  down: { cls: "err", text: "不可达" },
  unknown: { cls: "", text: "未探测" },
};

/** 成员的健康 → 一个小圆点（颜色）+ 一句话（悬停可见） */
function _rtMarkDirty() {
  _rtDirty = true;
  const btn = $("#rtSave");
  if (btn) { btn.classList.add("primary"); btn.textContent = "保存 *"; }
}

function _rtHealth(m) {
  const h = m.health || {};
  const st = h.state === "open" ? RT_STATE.open
    : (h.reachable === false ? RT_STATE.down
      : (h.reachable === true ? RT_STATE.closed : RT_STATE.unknown));
  const why = h.state === "open" ? (h.detail || "连续失败，暂时跳过")
    : (h.reachable === false ? (h.last_error || h.detail || "连不上")
      : (h.detail || "正常"));
  const bits = [st.text, why];
  if (h.last_ttfb_ms != null) bits.push(`首字节 ${h.last_ttfb_ms}ms`);
  bits.push(`成功 ${h.ok || 0} / 失败 ${h.fail || 0}`);
  if (!m.has_key) bits.push("⚠ DSH 凭据库里没有这个引用，调用会失败");
  return { cls: st.cls, text: st.text, tip: bits.join(" · ") };
}

/** 单行：通道号（可点开详情）· 昵称 · 健康点 · 上移/下移/启停/移除 */
function _rtRow(m, i, n) {
  const h = _rtHealth(m);
  const open = _rtOpen.has(i);
  const nick = m.name || "";
  const info = [
    `模型 <code>${esc(m.model)}</code>`,
    `端点 <code>${esc(m.base_url)}</code>`,
    m.credential ? `凭据 <code>${esc(m.credential)}</code>${m.has_key ? "" : ` <span class="warn">（缺失）</span>`}` : "",
    (m.context_window || m.max_tokens)
      ? `能力 声明 ${Math.round((m.context_window || 0) / 1000)}K 上下文 / ${Math.round((m.max_tokens || 0) / 1000)}K 输出` : "",
    h.tip,
  ].filter(Boolean).join("<br>");
  return `<div class="rt-row${m.enabled ? "" : " off"}${open ? " open" : ""}" data-i="${i}">
    <button class="rt-no" data-act="toggle" title="通道 ${i + 1}（派发顺序）· 点开看详情">${i + 1}</button>
    <input class="rt-nick" value="${esc(nick)}" placeholder="给这条通道起个短名，如 大ep / 外4.1F" maxlength="24"
           title="通道昵称：DSH 里看不到，只在 ECHO 面板与路由日志里用">
    <span class="rt-dot ${h.cls}" title="${esc(m.enabled ? h.tip : "已停用")}"></span>
    <span class="rt-acts">
      <button data-act="up" ${i === 0 ? "disabled" : ""} title="上移（提高优先级）">↑</button>
      <button data-act="down" ${i === n - 1 ? "disabled" : ""} title="下移（降低优先级）">↓</button>
      <label class="rt-sw" title="${m.enabled ? "停用（保留配置与统计，不参与派发）" : "启用"}">
        <input type="checkbox" class="rt-en" ${m.enabled ? "checked" : ""}><i></i>
      </label>
      <button data-act="del" class="del" title="从模型组移除">✕</button>
    </span>
    <div class="rt-detail${open ? "" : " hidden"}">${open ? info : ""}</div>
  </div>`;
}

function renderRouterMembers() {
  const box = $("#rtMembers");
  if (!box) return;
  box.innerHTML = _rtMembers.length
    ? _rtMembers.map((m, i) => _rtRow(m, i, _rtMembers.length)).join("")
    : `<div class="empty">还没有成员：从下面选一个 DSH 里的模型加进来</div>`;
}

function renderRouterCandidates() {
  const sel = $("#rtCandSelect");
  if (!sel) return;
  const used = new Set(_rtMembers.map((m) => m.candidate_key).filter(Boolean));
  const opts = ['<option value="">从 DSH 的模型里选一个…</option>'];
  _rtCands.forEach((c, i) => {
    const dup = used.has(c.key) ? "（已加入）" : "";
    const noBase = c.base_url ? "" : "（无端点）";
    const noKey = c.has_key ? "" : " · 缺凭据";
    opts.push(`<option value="${i}" ${c.base_url ? "" : "disabled"}>` +
      `${esc(c.model_name)} · ${esc(c.provider_display)}${dup}${noBase}${noKey}</option>`);
  });
  sel.innerHTML = opts.join("");
}

/** 注册态文案：DSH 可能有两个家目录（桌面版 / 标准版 harness），**分别**说清谁注册了。
 *  同事不一定两个都装（2026-09-22）：只装一个时也要说得明白，别只报一句"已注册"。 */
function routerRegText(reg) {
  const homes = reg.homes || [];
  const label = (h) => esc(h.label || h.kind || "DSH");
  const on = homes.filter((h) => h.registered);
  const off = homes.filter((h) => !h.registered);
  if (!homes.length) {
    return reg.registered
      ? "<b>已注册</b>"
      : `<b style="color:var(--yellow)">未注册</b>`;
  }
  if (!on.length) {
    return `<b style="color:var(--yellow)">未注册</b>（${homes.map(label).join(" / ")}）`;
  }
  return `<b>已注册</b>（${on.map(label).join(" / ")}）` +
    (off.length
      ? ` · <span style="color:var(--yellow)">${off.map(label).join(" / ")} 未注册</span>`
      : "");
}

function renderRouterHead() {
  const v = _rtView || {};
  const reg = v.registration || {};
  const r = v.router || {};
  const badge = $("#rtBadge");
  const verdict = routerVerdict({
    proxy_online: r.online,
    groups: _rtMembers.length ? [{
      id: "echo-auto",
      members: _rtMembers.map((m, i) => ({
        priority: m.priority || i + 1,
        name: m.name || "未命名", enabled: m.enabled,
        reachable: (m.health || {}).reachable, state: (m.health || {}).state,
        detail: (m.health || {}).detail, last_error: (m.health || {}).last_error,
      })),
    }] : [],
  });
  badge.textContent = r.online ? verdict.text : "路由未运行";
  badge.className = "badge " + (r.online ? verdict.cls : "error");
  badge.title = verdict.tip;
  const g = v.group || {};
  const enabled = _rtMembers.filter((m) => m.enabled).length;
  // 一行说清「DSH 那边是什么、注册没注册」——成员细节在各行详情里，不在这里堆
  $("#rtReg").innerHTML =
    `DSH 模型 <b>${esc(g.id || "echo-auto")}</b>` +
    `（显示名 ${esc(g.display_name || "ECHO AUTO")}）· ` +
    `${enabled}/${_rtMembers.length} 启用 · ` +
    `${Math.round((g.context_window || 0) / 1000)}K 上下文 / ${Math.round((g.max_tokens || 0) / 1000)}K 输出 · ` +
    routerRegText(reg) +
    (r.online ? "" : ` · <span style="color:var(--red)">路由进程未运行：${esc(r.error || "")}</span>`);
  const rb = $("#rtRegister");
  rb.textContent = reg.registered ? "重新注册到 DSH" : "注册到 DSH";
  rb.classList.toggle("primary", !reg.registered);
  const sb = $("#rtSave");
  if (sb && !_rtDirty) sb.textContent = "保存";
  renderRouterStats();
}

/** 「派发情况」卡片（模型路由页第二张卡，在通道设置下面，标题无括号说明；2026-09-13 由「运营数据」改名）：
 *  上排 4 个指标块（累计派发 / 失败 / 成功率 / 最近命中），下排各通道命中条 + 占比条。
 *  仪表盘卡片上只放各通道命中次数，这些汇总只在这里显示。 */
function renderRouterStats() {
  const box = $("#rtStats");
  if (!box) return;
  const r = (_rtView || {}).router || {};
  const rt = r.routes || {};
  if (!r.online) {
    box.innerHTML = `<div class="rt-empty warn">路由进程没在运行，暂时读不到派发数据` +
                    (r.error ? `（${esc(r.error)}）` : "") + `</div>`;
    return;
  }
  const req = rt.requests || 0;
  const fail = rt.failed || 0;
  const ok = Math.max(0, req - fail);
  const rateNum = req ? ok / req : 0;
  const pct = Math.round(rateNum * 1000) / 10;   // 一位小数，顺手抹掉浮点噪声（95.00000000000001）
  const rate = req ? `${pct % 1 === 0 ? pct.toFixed(0) : pct.toFixed(1)}%` : "—";
  const rateCls = !req ? "" : (fail === 0 ? " ok" : (rateNum >= 0.95 ? "" : " warn"));
  const when = rt.last_route_at ? `<span class="t">${esc(rt.last_route_at)}</span>` : "";
  const last = rt.last_member
    ? `<b class="sm" title="${esc(`通道${rt.last_channel} ${rt.last_member}`.trim())}">${esc(rt.last_member)}</b>` + when
    : `<b class="sm muted">尚无请求</b>`;
  const mem = _rtMembers.filter((m) => m.enabled);
  const counts = mem.map((m) => (m.health || {}).ok || 0);
  const top = Math.max(1, ...counts);
  const rows = mem.map((m, i) => {
    const n = counts[i];
    return `<div class="rt-hit${n ? "" : " zero"}">` +
      `<span class="no">${m.priority != null ? m.priority : i + 1}</span>` +
      `<span class="nm" title="${esc(m.name || "")}">${esc(m.name || "未命名")}</span>` +
      `<span class="bar"><i style="width:${Math.round((n / top) * 100)}%"></i></span>` +
      `<b>${n}</b></div>`;
  }).join("");
  box.innerHTML =
    `<div class="rt-kpis">` +
      `<div class="rt-kpi"><span class="k">累计派发</span><b>${req}</b></div>` +
      `<div class="rt-kpi${fail ? " bad" : ""}"><span class="k">失败</span><b>${fail}</b></div>` +
      `<div class="rt-kpi${rateCls}"><span class="k">成功率</span><b>${rate}</b></div>` +
      `<div class="rt-kpi"><span class="k">最近命中</span>${last}</div>` +
    `</div>` +
    (rows ? `<div class="rt-hits-title">各通道命中</div><div class="rt-hits">${rows}</div>` : "");
}

/** 正在这一页上操作（改昵称、选下拉、点开关）时，别让 2 秒轮询把 DOM 换掉、抢走焦点。
 *  （2026-09-25：`#view-failover` 已并进「智能体」页；2026-09-26 IA 重构后那一页叫
 *   「能力与智能体」（id 仍是 `capability`），选择器跟着改。） */
function _rtBusy() {
  const a = document.activeElement;
  const sec = $("#view-capability");
  return !!(a && sec && a !== document.body && sec.contains(a));
}

async function loadRouter() {
  try {
    const v = await api("/api/router/status");
    _rtView = v;
    _rtMeta = v.group || {};
    _rtMembers = (v.members || []).map((m) => ({ ...m, name: m.name || "" }));
    _rtCands = v.candidates || [];
    _rtDirty = false;
    _rtOpen.clear();
    // 路由仪表盘链接的地址由后端给出（端口以 config.json 为准，不再写死）
    const rurl = (v.router || {}).url;
    if (rurl) {
      const a = document.getElementById("rtDashLink");
      if (a) a.href = rurl.replace(/\/+$/, "") + "/";
    }
    renderRouterHead();
    renderRouterMembers();
    renderRouterCandidates();
  } catch (e) { toast("加载模型路由失败：" + e.message); }
  // 语言模型那一块（用哪个实现 + 在线服务）也在这一页（智能体）；
  // 7 项路由参数由设置卡片渲染（同一个 pane 里，见 SET_CARDS.agent 的「通道设置」卡）
  loadRouterLlm();
}

/** 读一次设置（`_settingsCache`）。多个页签都要用（能力页签 / 模型路由页签 / 设置页），
 *  所以做成"没有才拉"的共享入口 —— 各拉一遍还会出现"一处改了另一处是旧值"。
 *  2026-09-25：**并进**已有缓存而不是整个换掉 —— 设置页 v3 会把 provider / 能力 / 智能体
 *  那些 hidden 行的元数据补进同一个缓存，换掉就等于把它们弄丢。 */
async function ensureSettings(force) {
  if (!force && _settingsCache.length) return _settingsCache;
  const r = await api("/api/settings");
  mergeSettingsRows(r.settings || []);
  _agentsCache = r.agents || _agentsCache;
  return _settingsCache;
}

/* 2026-09-25（第二版）：7 项路由参数回到**这张卡自己的「保存」**名下 —— 用户要求把
   「语言模型 / 通道成员 / 派发情况 / 通道设置」合成一张卡，卡内一处改就该一处存。
   所以 `#rtSetHost` 那 7 行由 renderSettingsPanes() 填，落库交给卡内的 `#rtSave`
   （见下面的 saveRouter），页签顶部的「保存」只管 pane 里那些设置卡片 —— 不会出现
   两个按钮都管同一件事。 */

/** 收集某个根节点下 `[data-key]` 的表单值（bool / list / int / float / 密钥的分支与既有实现一致）。
 *  页签顶部的「保存」与「模型路由」卡内的「保存」共用它，免得两处各写一份、哪天漂移。 */
function collectSettingValues(rootSel) {
  const values = {};
  $$(`${rootSel} [data-key]`).forEach((el) => {
    const key = el.dataset.key;
    const meta = _settingsCache.find((s) => s.key === key);
    if (!meta) return;
    if (meta.secret) {
      // 密钥：**只在这轮真的输入了新值时才提交**（空 = 不改）。服务端另有同样的闸。
      if (el.value && el.value.trim()) values[key] = el.value;
      return;
    }
    if (meta.value_type === "bool") values[key] = el.checked;
    else if (meta.value_type === "list") {
      values[key] = el.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    } else if (meta.value_type === "int") values[key] = parseInt(el.value, 10) || 0;
    else if (meta.value_type === "float") values[key] = parseFloat(el.value) || 0;
    else values[key] = el.value;
  });
  return values;
}

/** 「模型路由」卡内的「保存」：**这张卡里的改动一起落库**
 *  ① 成员 / 优先级 / 昵称 → `PUT /api/router/members`（热重载 + 同步注册到 DSH）；
 *  ② 「会议能力通道」那 3 项 + 「高级 → 路由参数」那 7 项 → `PUT /api/settings`
 *     （后端会写 `dsh-failover/config.json` 并热重载）。
 *  页签顶部那个「保存」只收 `[data-settab-pane]` 里的设置行（这两个 host 都不在其中），
 *  所以同一件事不会被两个按钮管。 */
async function saveRouter() {
  // ① 先存设置（有改动才发请求；没动就不打扰后端）
  const params = {};
  RT_SAVE_HOSTS.forEach((sel) => Object.assign(params, collectSettingValues(sel)));
  if (Object.keys(params).length) {
    try {
      const rs = await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: params }) });
      toastSaved(rs, `已保存 ${Object.keys(params).length} 项路由参数`);
    } catch (e) { toast("路由参数保存失败：" + e.message, 5000); return; }
  }
  // ② 再存成员与优先级
  const payload = _rtMembers.map((m, i) => ({
    priority: i + 1,
    name: (m.name || "").trim() || `通道${i + 1}`,
    enabled: !!m.enabled,
    candidate_key: m.candidate_key || "",
    base_url: m.base_url,
    model: m.model,
    credential: m.credential,
    headers: m.headers || {},
    body_mode: m.body_mode,
    context_window: m.context_window,
    max_tokens: m.max_tokens,
  }));
  try {
    const r = await api("/api/router/members", { method: "PUT", body: JSON.stringify({ members: payload }) });
    toast(r.message || "已保存", 4200);
    _rtDirty = false;
    const sb = $("#rtSave");
    if (sb) { sb.classList.remove("primary"); sb.textContent = "保存"; }
    await loadRouter();
    await loadSettings();          // 参数改完让卡片里的取值跟着刷新
  } catch (e) { toast("保存失败：" + e.message, 5000); }
}

/** 候选 → 一个短昵称建议（用户随手就能改） */
function _rtSuggest(c) {
  const p = c.provider_display || "";
  const prefix = /官方/.test(p) ? "外" : (/联通|内网/.test(p) ? "内" : "");
  let s = String(c.model_name || c.model || "").replace(/^BJ-Dp4-/, "").replace(/^DeepSeek-/, "");
  if (/^V?4\.?1/i.test(s)) s = "4.1" + s.replace(/^V?4\.?1/i, "");
  return (prefix + s).slice(0, 14);
}

function initRouterUI() {
  const box = $("#rtMembers");
  if (!box) return;

  box.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const i = Number(btn.closest(".rt-row").dataset.i);
    const act = btn.dataset.act;
    if (act === "toggle") {
      if (_rtOpen.has(i)) _rtOpen.delete(i); else _rtOpen.add(i);
      renderRouterMembers();
      return;
    }
    if (act === "del") {
      const m = _rtMembers[i];
      confirmDialog(`从模型组移除「${i + 1}-${m.name || "未命名"}」？`, { okText: "移除", danger: true }).then((yes) => {
        if (!yes) return;
        _rtMembers.splice(i, 1);
        _rtOpen.clear();
        _rtMarkDirty(); renderRouterMembers(); renderRouterHead();
      });
      return;
    }
    const j = act === "up" ? i - 1 : i + 1;
    if (j < 0 || j >= _rtMembers.length) return;
    [_rtMembers[i], _rtMembers[j]] = [_rtMembers[j], _rtMembers[i]];
    _rtOpen.clear();
    _rtMarkDirty(); renderRouterMembers();
  });

  // 昵称边打边存到本地副本（保存时才写文件）
  box.addEventListener("input", (e) => {
    if (!e.target.classList.contains("rt-nick")) return;
    const i = Number(e.target.closest(".rt-row").dataset.i);
    _rtMembers[i].name = e.target.value;
    _rtMarkDirty();
  });

  box.addEventListener("change", (e) => {
    if (!e.target.classList.contains("rt-en")) return;
    const i = Number(e.target.closest(".rt-row").dataset.i);
    _rtMembers[i].enabled = e.target.checked;
    _rtMarkDirty();
    e.target.closest(".rt-row").classList.toggle("off", !e.target.checked);
    renderRouterHead();
  });

  $("#rtAdd").addEventListener("click", () => {
    const sel = $("#rtCandSelect");
    if (sel.value === "") { toast("先选一个模型"); return; }
    const c = _rtCands[Number(sel.value)];
    if (!c || !c.base_url) { toast("这个候选没有可用端点"); return; }
    _rtMembers.push({
      priority: _rtMembers.length + 1,
      name: _rtSuggest(c),
      enabled: true,
      candidate_key: c.key,
      base_url: c.base_url,
      model: c.model,
      credential: c.credential,
      headers: c.headers || {},
      body_mode: c.body_mode,
      context_window: c.context_window,
      max_tokens: c.max_tokens,
      has_key: c.has_key,
      health: { state: "unknown", reachable: null, detail: "新增，待探测" },
    });
    _rtOpen.add(_rtMembers.length - 1);
    _rtMarkDirty();
    renderRouterMembers();
    renderRouterCandidates();
    renderRouterHead();
    sel.value = "";
    toast("已加入待保存列表：可改昵称、调顺序，然后点「保存」", 3600);
  });

  $("#rtSave").addEventListener("click", saveRouter);

  $("#rtProbe").addEventListener("click", async () => {
    const btn = $("#rtProbe");
    btn.disabled = true;
    try {
      await post("/api/router/probe");
      toast("已探测完成");
      await loadRouter();
    } catch (e) { toast("探测失败：" + e.message, 4500); }
    finally { btn.disabled = false; }
  });

  $("#rtReload").addEventListener("click", async () => {
    try {
      const r = await post("/api/router/reload");
      toast(r.message || "已重载");
      await loadRouter();
    } catch (e) { toast("重载失败：" + e.message, 4500); }
  });

  $("#rtRegister").addEventListener("click", async () => {
    try {
      const r = await post("/api/router/register");
      toast(r.message || "已注册到 DSH", 4500);
      await loadRouter();
    } catch (e) { toast("注册失败：" + e.message, 5000); }
  });
}
$("#failoverCard").addEventListener("click", (e) => {
  if (e.target.closest("a")) return;   // 让 "详情 ›" 链接走自己的 handler
  gotoFailover();
});
$$("[data-goto-link='agent']").forEach((a) =>
  a.addEventListener("click", (e) => { e.preventDefault(); gotoFailover(); }));

async function refreshDashboard() {
  refreshFailoverCard();            // 模型路由小卡片（独立容错，不阻塞主刷新）
  // 智能体 Web 界面小图标（独立 harness）：跟着仪表盘刷新一起更新，失败不阻塞
  refreshAgentsForDashboard().catch(() => {});
  try {
    const st = await api("/api/status");
    _statusCache = st;                                // 设置页「常规 → 服务」卡要用它
    document.body.classList.remove("echo-offline");   // 顶栏去掉常驻状态后，靠这个红标表示"连不上"
    renderLiveStatus(st);                             // 启动页"启动日志"标题右侧的在线时长 + 状态
    renderStartupNotes(st);                           // 启动期自愈留痕（只提示一次）
    maybeRefreshInstallNotice(st);                     // "还缺什么"横幅别停在旧结论上
    // 会议控制
    const mb = $("#meetingBadge");
    mb.textContent = st.meeting.active ? "录音中" : "空闲";
    mb.className = "badge " + (st.meeting.active ? "active" : "idle");
    $("#meetingInfo").textContent = st.meeting.active
      ? `正在录音：${st.meeting.folder}`
      : (st.meeting.error ? `上次错误：${st.meeting.error}` : "未在录音");
    $("#btnMeeting").textContent = st.meeting.active ? "停止录音" : "开始录音";
    $("#btnMeeting").className = "btn big " + (st.meeting.active ? "danger" : "");
    // 录音中：显示麦克风电平波动
    // 语音命令收音阶段（busyPhase=listening）同样显示波形 —— 后端复用录音器的电平回调，不开新采样流
    const micLevel = $("#micLevel");
    const cmdLevel = $("#cmdLevel");
    const listening = !!(st.busy && st.busyPhase === "listening");
    if (micLevel) micLevel.classList.toggle("hidden", !st.meeting.active);
    if (cmdLevel) cmdLevel.classList.toggle("hidden", !listening);
    if (st.meeting.active || listening) {
      if (!_levelTimer) {
        _levelTimer = setInterval(async () => {
          try {
            const lv = await api("/api/audio/level");
            setMicLevel(lv.level || 0, "#micLevel");
            setMicLevel(lv.level || 0, "#cmdLevel");
          } catch (e) { /* ignore */ }
        }, 120);
      }
    } else {
      clearInterval(_levelTimer);
      _levelTimer = null;
      setMicLevel(0, "#micLevel");
      setMicLevel(0, "#cmdLevel");
    }
    // "说话"按钮动效：点下去立刻有反馈；收音/转写/处理三个阶段文字与配色不同（用户 2026-09-12）
    renderCaptureBtn(st.busy ? st.busyPhase || "running" : null);
    // 近期命令（2026-09-12 用户要求：仪表盘只保留 2 条，把纵向空间让给会议录音卡）
    const cmds = await api("/api/commands?limit=2");
    // 点击任意一条 → 跳到「历史」页签并定位到这条的完整详情（issue #2）
    renderCmdList($("#recentCmds"), cmds.items, false, { click: "history" });
    // 近期会议（最近 5 条）
    const meets = await api("/api/meetings?limit=5");
    renderMeetingItems($("#recentMeetings"), meets.items);
  } catch (e) {
    // 顶栏不再有"运行时长 · 空闲"这类常驻状态；连不上时给整页加红色标记（正常时不可见），
    // 同时模型路由卡自己会显示"ECHO 离线"
    document.body.classList.add("echo-offline");
    renderLiveStatus(null);
    console.warn("ECHO 状态轮询失败：", e.message);
  }
}

// 会议时长 hh:mm 格式：满1小时显示 "H:MM"，不足1小时仅显分钟数（不含秒）
function fmtHM(sec) {
  const t = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60);
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}` : String(m);
}

/* ---------------- 在线时长 + 当前状态（顶栏已取消，改放启动页与折叠条） ----------------
   口径统一在这里：面板（启动日志标题右侧）用中文全称，折叠条用 compact 短格式（48 逻辑宽塞得下）。 */
function fmtUptime(s, compact) {
  const t = Math.max(0, Math.round(s || 0));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), ss = t % 60;
  if (compact) {
    if (h >= 100) return "99h+";
    return h > 0 ? `${h}h${m}m` : (m > 0 ? `${m}m` : `${ss}s`);
  }
  return h > 0 ? `${h}时${m}分` : (m > 0 ? `${m}分${ss}秒` : `${ss}秒`);
}

/** 当前状态：录音中 > 命令处理中 > 空闲；读不到状态就是离线。cls 用面板既有徽章配色 */
function liveState(st) {
  if (!st) return { text: "ECHO 离线", cls: "error", tip: "读不到 /api/status：ECHO 服务可能正在重启" };
  if (st.meeting && st.meeting.active) {
    return { text: "录音中", cls: "active", tip: "正在会议录音" + (st.meeting.folder ? `：${st.meeting.folder}` : "") };
  }
  if (st.busy) {
    return { text: "命令处理中", cls: "running", tip: "正在处理一条指令" + (st.busyPhase ? `（${st.busyPhase}）` : "") };
  }
  return { text: "空闲", cls: "idle", tip: "没有正在执行的任务" };
}

function renderLiveStatus(st) {
  const el = $("#bootLive");
  if (!el) return;
  const v = liveState(st);
  if (!st) {
    el.innerHTML = `<span class="badge ${v.cls}">${v.text}</span>`;
    el.title = v.tip;
    return;
  }
  const up = fmtUptime(st.uptime);
  // 只放"当前状态"：运行时长在「服务」卡里已经有了，同一页不重复第二遍（2026-09-25 整合）
  el.innerHTML = `<span class="badge ${v.cls}">${v.text}</span>`;
  el.title = `ECHO 服务连续在线 ${up}，当前${v.text} · ${v.tip}`;
}

const STATUS_TEXT = { online: "在线", offline: "离线", active: "工作中", idle: "空闲",
  error: "错误", disabled: "未启用", paused: "暂停", unknown: "未知",
  transcribing: "转写中", sent: "已发送", running: "执行中", done: "完成", failed: "失败",
  pending: "排队中", recording: "录音中", transcribed: "已完成", interrupted: "已中断",
  starting: "启动中",
  // 「你选了另一个智能体」——不是故障，所以既不算失败也不能渲染成错误徽章
  skipped: "未使用" };

/* 历史回复先给这么多字的预览，超出可展开全文（issue #2：以前硬截 300 字、后面看不到） */
const REPLY_PREVIEW = 300;

function renderCmdList(el, items, withReply, opts = {}) {
  if (!items.length) {
    el.innerHTML = `<div class="empty">暂无命令</div>`;
    return;
  }
  const jump = opts.click === "history";
  el.innerHTML = items.map((c) => {
    const stCls = ["done", "sent", "running"].includes(c.status) ? c.status
      : (c.status === "failed" ? "error" : "idle");
    const reply = (withReply && c.reply) ? String(c.reply) : "";
    const long = reply.length > REPLY_PREVIEW;
    return `<div class="cmd-item${jump ? " clickable" : ""}" data-id="${c.id}"${
      jump ? ` title="点击查看这条指令的完整历史"` : ""}>
      <div class="head"><span class="badge ${stCls}">${STATUS_TEXT[c.status] || c.status}</span>
        <span class="muted" style="font-size:12px">${esc(c.source)}</span>
        <span class="time">${esc(c.ts || "")}</span></div>
      <div class="text">${esc(c.text)}</div>
      ${reply ? `<div class="reply">↳ <span class="reply-short">${esc(reply.slice(0, REPLY_PREVIEW))}</span>${
        long ? `<span class="reply-full hidden">${esc(reply)}</span>` : ""}</div>` : ""}
      ${long ? `<a class="reply-toggle" data-expand data-len="${reply.length}">展开全文（${reply.length} 字）</a>` : ""}
      ${c.error ? `<div class="reply" style="color:var(--red)">⚠ ${esc(c.error)}</div>` : ""}
      ${(opts.sessions && c.session_id) ? `<div class="cmd-sess">
        <a class="sess-open" data-sess="${c.id}">看会话 ▸</a>
        <span class="muted sess-title" title="${esc(c.session_id)}">${esc(sessionLabel(c.session_id))}</span>
        <a class="sess-copy" data-sesscopy="${esc(c.session_id)}" title="复制会话 ID（DSH 里搜不到会话时用）">⧉</a>
      </div>
      <div class="sess-body hidden" data-sessbody="${c.id}"></div>` : ""}
    </div>`;
  }).join("");

  $$("[data-expand]", el).forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();   // 别被算成"整条点击"（仪表盘上整条是跳转）
    toggleReply(a);
  }));
  if (opts.sessions) {
    $$("[data-sess]", el).forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleCommandSession(a.dataset.sess);
    }));
    $$("[data-sesscopy]", el).forEach((a) => a.addEventListener("click", async (e) => {
      e.preventDefault();
      e.stopPropagation();
      try { await navigator.clipboard.writeText(a.dataset.sesscopy); toast("已复制会话 ID"); }
      catch (err) { toast("复制失败，请手动选择"); }
    }));
  }
  if (jump) {
    $$(".cmd-item", el).forEach((it) =>
      it.addEventListener("click", () => openCmdInHistory(it.dataset.id)));
  } else {
    // 历史页：整条也可点，等价于点"展开全文/收起"（只对长回复有反应）
    $$(".cmd-item", el).forEach((it) => it.addEventListener("click", () => {
      const link = it.querySelector("[data-expand]");
      if (link) toggleReply(link);
    }));
  }
}

/** 展开/收起某条历史的回复全文；force=true 时只展开。 */
function toggleReply(link, force) {
  const item = link.closest(".cmd-item");
  if (!item) return;
  const expand = force === true ? true : !item.classList.contains("expanded");
  item.classList.toggle("expanded", expand);
  const short = item.querySelector(".reply-short");
  const full = item.querySelector(".reply-full");
  if (short) short.classList.toggle("hidden", expand);
  if (full) full.classList.toggle("hidden", !expand);
  link.textContent = expand ? "收起" : `展开全文（${link.dataset.len || ""} 字）`;
}

/* ================= 仪表盘 → 历史某一条 ================= */

let _focusCmdId = null;   // 跨页签传参：loadHistory() 渲染完成后据此定位

function openCmdInHistory(id) {
  _focusCmdId = String(id);
  switchView("history");          // 内部会调用 loadHistory()
}

/** 定位 + 高亮某条历史；长回复顺手展开，保证落到的就是"完整详情"。 */
function focusCmd(id) {
  const item = $(`#historyList .cmd-item[data-id="${id}"]`);
  if (!item) return;
  item.scrollIntoView({ block: "center", behavior: "smooth" });
  const toggle = item.querySelector("[data-expand]");
  if (toggle && !item.classList.contains("expanded")) toggleReply(toggle, true);
  item.classList.add("cmd-focus");
  setTimeout(() => item.classList.remove("cmd-focus"), 2600);
}

/* ================= 命令目标（工作区/对话） ================= */
let _targets = { workspaces: [], sessions: [] };

function shortName(p) {
  const parts = String(p || "").replace(/\\/g, "/").split("/");
  return parts[parts.length - 1] || p;
}

/** 从 /api/settings 返回的列表里取值。 */
function settingFrom(list, key) {
  const row = (list || []).find((s) => s.key === key);
  return row && row.value != null ? String(row.value) : "";
}

/** 命令目标一次选中即持久化：语音（媒体键/唤醒/麦克风）与打字命令共用它。 */
async function saveCommandTarget() {
  const t = currentTarget();
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: {
      commandTargetWorkspace: t.workspace || "",
      commandTargetSession: t.session_id || "",
    } }) });
  } catch (e) { toast("命令目标保存失败：" + e.message); }
}

async function loadTargets() {
  let savedWs = "", savedSid = "";
  try {
    const [t, s] = await Promise.all([api("/api/dsh/targets"), api("/api/settings")]);
    _targets = t;
    savedWs = settingFrom(s.settings, "commandTargetWorkspace");
    savedSid = settingFrom(s.settings, "commandTargetSession");
  } catch (e) {
    _targets = { workspaces: [], sessions: [] };
  }
  const ws = $("#wsSelect");
  if (!ws) return;
  const prevWs = ws.dataset.val || savedWs || "";
  ws.innerHTML = `<option value="">默认（ECHO 固定会话）</option>` +
    _targets.workspaces.map((w) =>
      `<option value="${esc(w)}">${esc(shortName(w))}</option>`).join("");
  // 保存的工作区若已不存在（移除/改名）→ 退回默认项
  ws.value = [...ws.options].some((o) => o.value === prevWs) ? prevWs : "";
  ws.dataset.val = ws.value;
  renderSessSelect(savedSid);
}

function renderSessSelect(wantSid) {
  const wsSel = $("#wsSelect");
  const ss = $("#sessSelect");
  if (!wsSel || !ss) return;
  const ws = wsSel.value;
  const list = ws ? (_targets.sessions || []).filter((s) => s.cwd === ws) : [];
  const prev = (wantSid !== undefined ? wantSid : (ss.dataset.val || "")) || "";
  ss.innerHTML = `<option value="">自动（该工作区最近对话 / 新建）</option>` +
    list.map((s) =>
      `<option value="${esc(s.sessionId)}">${esc(s.title || shortName(s.sessionId))}${s.running ? " ●" : ""}</option>`).join("");
  ss.value = [...ss.options].some((o) => o.value === prev) ? prev : "";
  ss.dataset.val = ss.value;
}
$("#wsSelect").addEventListener("change", () => { renderSessSelect(); saveCommandTarget(); });
$("#sessSelect").addEventListener("change", saveCommandTarget);

function currentTarget() {
  const ws = $("#wsSelect").value || "";
  const sid = $("#sessSelect").value || "";
  return { workspace: ws || undefined, session_id: sid || undefined };
}

/* 仪表盘事件 */
$("#btnCmdSend").addEventListener("click", async () => {
  const text = $("#cmdInput").value.trim();
  if (!text) return;
  try {
    const t = currentTarget();
    const r = await post("/api/assistant/command", { text, source: "web", ...t });
    toast(r.message);
    $("#cmdInput").value = "";
    refreshDashboard();
  } catch (e) { toast("发送失败：" + e.message); }
});
$("#cmdInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#btnCmdSend").click(); });

$("#btnCapture").addEventListener("click", async () => {
  const btn = $("#btnCapture");
  btn.blur();
  if (btn.disabled) return;
  btn.disabled = true;
  renderCaptureBtn("listening");          // 先给本地反馈，不等后端轮询回来
  try { const r = await post("/api/assistant/capture", { source: "web" }); toast(r.message); }
  catch (e) { toast("失败：" + e.message); renderCaptureBtn(null); }
  finally { btn.disabled = false; refreshDashboard(); }
});

/* 会议录音按钮防误触：
   点击后立即失焦（blur），否则焦点留在按钮上，之后按回车/空格会再次触发
   click —— 此时按钮文字已是「停止录音」，就会误发 /api/meeting/stop 把录音停掉。
   再加 800ms 防抖，双击/连按只生效一次（服务端 start/stop 也已有原子锁）。
   停止录音前弹确认框，避免误停丢失录音。 */
let _meetingBusy = false;
$("#btnMeeting").addEventListener("click", async (e) => {
  e.currentTarget.blur();
  if (_meetingBusy) return;
  const active = $("#btnMeeting").textContent === "停止录音";
  if (active && !(await confirmDialog("确定要停止录音并开始转写吗？", { okText: "停止并转写", danger: true }))) return;
  _meetingBusy = true;
  try {
    const r = await post(active ? "/api/meeting/stop" : "/api/meeting/start", {});
    toast(r.message);
    refreshDashboard();
  } catch (e2) { toast("失败：" + e2.message); }
  finally { setTimeout(() => { _meetingBusy = false; }, 800); }
});

/* 仪表盘"全部"跳转 */
$("#gotoHistory").addEventListener("click", (e) => { e.preventDefault(); switchView("history"); });
$("#gotoMeetings").addEventListener("click", (e) => { e.preventDefault(); switchView("meetings"); });

/* 右下角箭头：把展开的面板收成屏幕右缘的折叠条（与折叠条底部的隐藏箭头互为反向操作）。
   走 WebView2 内置桥（宿主 Program.cs 的 WebMessageReceived），不经过 ECHO 的 HTTP；
   页面不在边条里运行时（整窗模式 / 浏览器直接打开）没有可收起的边条，按钮隐藏。 */
(() => {
  const btn = $("#btnRailCollapse");
  if (!btn) return;
  const bridge = window.chrome && window.chrome.webview;
  if (!bridge || typeof bridge.postMessage !== "function") { btn.classList.add("hidden"); return; }
  btn.addEventListener("click", () => bridge.postMessage("rail-collapse"));
})();

/* ================= 设置（2026-09-25 重设计 v3）=================

   规格：`docs/设置项归属表.md`（由 `app.config.DEFAULTS` 机械导出，106 项**逐条**给了
   页签 / 卡片 / 层级 / 处置）；视觉：`docs/设置页重设计-样张.html`。

   三条来自用户的设计规则，实现方式各自标注在下面：
     ① **边条优先**：面板最常以 ~360px 边条呈现 → 标签与控件同行（`.lbl` 84px、省略号），
        卡片标题独占一行，间距抄样张（见 app.css 的「设置区 v3」段）；
     ② **卡片可折叠**：卡片就是 `.card.collapsible`，直接复用既有的 `applyCollapsedCards`
        /`toggleCollapsibleCard`（localStorage 记状态）—— 标题里的 `?` 不触发折叠（见
        `_cardTitleOf` 那个全局委托里的 `.sq` 排除）；
     ③ **每卡两段**：`common`（常显）+ `adv`（高级设置区，默认收起）。哪些项进高级
        **完全按归属表的「层级」列**，不凭感觉。

   ⚠️ 这张表是**唯一**的落点声明：`SET_CARDS` 里没出现的可见设置项会被兜底渲染进
   「未归类」卡（`_fallbackCards`）—— 也就是"新加的设置项不会静默消失"，而不是靠人记。
   `SET_GROUP_ORDER` / `SET_GROUP_NAMES` / `SET_SUB_NAMES` 保留下来给兜底用（grp 词汇表）。 */
let _settingsCache = [];        // 设置元数据全集（可见行 + provider/能力/智能体的 hidden 行）
let _capView = null;            // GET /api/capability 的最近一次返回（含 hidden 的能力键与后端清单）
let _compsCache = null;         // GET /api/components（常规页那条"组件就绪 N/M"的摘要用）
let _routerView = null;         // GET /api/router/status（通道设置的状态行）
let _statusCache = null;        // GET /api/status（服务卡的运行信息）

/* 一级分组（后端 grp）展示顺序 = 业务相关性（只在兜底卡里用到）。 */
const SET_GROUP_ORDER = ["agent", "model", "voice", "wake", "meeting",
  "worklog", "panel", "paths", "router", "dsh"];
const SET_GROUP_NAMES = { agent: "智能体", model: "模型与引擎", voice: "语音命令",
  wake: "唤醒词", meeting: "会议", worklog: "纪要归档",
  panel: "面板与服务", paths: "存储路径", router: "模型路由", dsh: "DSH 服务" };

/* 二级小节（后端 sub）：**包含在**所属卡片里，不是与它并列的卡片。
   用户 2026-09-19 反馈："语音命令和 beep / command / speech 应该是包含不是并列"。
   在 v3 里它被用作**高级设置区里的小节标题**（没被 SET_ADV_SEC 显式指名的键就走它）——
   所以后端 `sub` 仍然是"面板分节"的一份真实输入，不是死字段。 */
const SET_SUB_ORDER = ["record", "command", "speech", "beep"];
const SET_SUB_NAMES = { record: "录音与转写", command: "命令与会话",
  speech: "朗读与反馈", beep: "提示音与通知" };

/* 3 个新页签（顺序即顶层页签顺序；第四位留给「历史」）。
   三个设置页签（2026-09-26 IA 重构后**就是顶层页签**）。`who` 是每页顶部那句"这一页管什么"，
   由 renderSettingsPanes() 填进 index.html 里那个空 `.sset-who`（单一出处，别在两处写标题）。 */
const SET_TABS = [
  { id: "general", who: "快捷键 · 打开/启动形式 · 运行状态（服务 / 端口 / 版本 / 重启停止）" },
  { id: "business", who: "会议 · 语音指令 · 朗读与反馈 · 队列 · 工作区与归档" },
  { id: "capability", who: "智能体选择 · 模型路由 · 设备选择；下面是已配对后端、本地能力与清理" },
];

/* 高级设置区里的小节标题：只给**没有后端 sub** 的键起名；
   有 sub 的键用 SET_SUB_NAMES（后端自己声明的分组）。
   键名一律是 DEFAULTS 的真实键 —— 写错就是"这一项从界面上消失"，所以有一条自检兜底。 */
const SET_ADV_SEC = {
  // 业务配置 → 语音指令
  wakeKeywords: "唤醒词与灵敏度", wakeAliases: "唤醒词与灵敏度",
  wakePaused: "唤醒词与灵敏度", wakeThreshold: "唤醒词与灵敏度",
  wakeCooldownSec: "唤醒词与灵敏度", wakeConfirmX: "唤醒词与灵敏度",
  wakeConfirmN: "唤醒词与灵敏度", wakeSilenceFloor: "唤醒词与灵敏度",
  device: "计算与唤醒", wakeEngine: "计算与唤醒",
  // 2026-09-26：声纹识别是标配（没有开关）；用户能决定的只有"改名要不要顺手入库"，
  // 所以这一节只剩它 + 两个阈值（认错人时唯一能调的东西）。见 docs/3.0-设计总览 §6.5。
  voiceprintAutoEnroll: "声纹入库",
  voiceprintThreshold: "声纹入库", voiceprintMargin: "声纹入库",
  providerAsr: "在线服务", providerAsrBaseUrl: "在线服务", providerAsrModel: "在线服务",
  providerAsrApiKey: "在线服务", providerLlm: "在线服务", providerLlmBaseUrl: "在线服务",
  providerLlmModel: "在线服务", providerLlmApiKey: "在线服务",
  // 业务配置 → 会议（保存策略）
  meetingAutoSummarize: "录音与产出", meetingKeepRawAudio: "录音与产出",
  meetingWorkspaceTitle: "录音与产出", capabilityPrivacy: "出网许可",
  // 业务配置 → 工作区与归档
  worklogEnabled: "纪要归档", worklogEnsureSessionAccess: "纪要归档",
  worklogVaultRoot: "纪要归档", worklogPrompt: "纪要归档",
  // ECHO 通用 → 快捷键与打开方式
  apiAuthEnabled: "面板鉴权",
};

/* 卡片结构（v3）。字段含义：
     id     卡片标识（折叠状态按它记：data-collapse-id="set-<id>"）
     title  卡片标题（**独占一行**，点它折叠整卡）
     hint   标题下一句说明（可选）
     help   标题右侧的 `?` 浮窗（可选）
     common 常用参数：键数组，或一个返回 HTML 的函数（需要状态/按钮的卡用它）
     adv    高级设置区的键数组（默认收起）
     advOrder 高级区里的小节顺序（缺省按声明顺序）
     advNote  高级区里某小节的补充说明：{小节名: html}
     note   卡片末尾的常显说明（可选） */
const SET_CARDS = {
  /* ---------- ① ECHO 通用：快捷键 · 打开/启动形式 · 运行状态 ---------- */
  general: [
    { id: "ui", title: "快捷键与打开方式",
      hint: "面板自己怎么打开、用哪个键、启动时什么样。",
      common: ["panelHotkey", "panelOpenMode", "panelAutoStart",
               "panelStartCollapsed", "panelAutoRefresh"],
      advOrder: ["面板鉴权"],
      adv: ["apiAuthEnabled"],
      advNote: {
        "面板鉴权": () => `<div class="snote info"><span>ⓘ</span><span>`
          + `语音指令的快捷键（唤醒热键 / 回退热键 / 媒体键）在「业务配置 → 语音指令」里 —— `
          + `它们属于"怎么说话"，不属于"怎么打开面板"。</span></div>`,
      } },
    { id: "svc", title: "运行状态",
      help: "这一台机器自己的运行态（服务 / 端口 / 版本 / 重启停止）。" +
            "组件与模型的就绪清单只在「能力与智能体」页渲染一处。",
      common: () => renderServiceCard(),
      advOrder: ["端口"],
      advDefault: "端口",
      adv: ["serverPort"] },
  ],
  /* ---------- ② 业务配置：会议 · 语音指令 · 朗读反馈 · 队列 · 工作区 ---------- */
  business: [
    { id: "meet", title: "会议",
      hint: "录音设备、分段时长、音频落点，以及转写走哪条路（下面那条单选）。",
      common: ["meetingInputDeviceId", "meetingSegmentMinutes", "meetingsDir"],
      // 转写走哪条路（3 个能力键）就在这张卡里 —— 用户的原话是"会议（… · 转写走哪条路）"。
      // 2026-09-26 之前那 3 个键散在「模型路由 → 会议能力通道」里，这里收口成一处。
      dynAfter: () => renderMeetingServiceCard(),
      covers: ["capabilityMeetingAsrBackend", "capabilityDiarizeBackend",
               "capabilityEmbedBackend"],
      advOrder: ["录音与产出", "出网许可"],
      adv: ["meetingAutoSummarize", "meetingKeepRawAudio", "meetingWorkspaceTitle",
            "meetingAutoCompressAudio", "capabilityPrivacy"] },
    { id: "cmd", title: "语音指令",
      hint: "从说一句话到出文字：唤醒 → 收音 → 转写。",
      common: ["wakeEnabled", "inputDeviceId", "sttModel"],
      dynAfter: () => renderCmdEngineStatus(),
      advOrder: ["唤醒词与灵敏度", "计算与唤醒", "录音与转写", "命令与会话",
                 "声纹入库", "在线服务"],
      adv: ["wakeKeywords", "wakeAliases", "wakePaused", "wakeThreshold", "wakeCooldownSec",
            "wakeConfirmX", "wakeConfirmN", "wakeSilenceFloor", "wakeEngine",
            "sttLanguage", "commandInputDeviceId", "silenceThreshold", "silenceHangoverMs",
            "noSpeechAbortMs", "maxRecordMs", "consumeMediaKey",
            "wakeHotkey", "fallbackHotkey", "triggerKeys",
            "commandWorkspaceTitle", "commandIdleRotateHours", "commandTargetWorkspace",
            "commandTargetSession", "userLocation", "sendEnvContext",
            "voiceprintAutoEnroll", "voiceprintThreshold", "voiceprintMargin",
            "providerAsr", "providerAsrBaseUrl", "providerAsrModel", "providerAsrApiKey",
            "providerLlm", "providerLlmBaseUrl", "providerLlmModel", "providerLlmApiKey"],
      advNote: {
        "录音与转写": () => `<div class="snote info"><span>ⓘ</span><span>`
          + `「允许使用虚拟/接力输入设备」（<code>allowVirtualInputDevice</code>）默认关闭，`
          + `而且**不在任何接口的下发范围内**（它标了 hidden、也不在 /api/capability 的 settings 里），`
          + `所以这里没有开关：确实要用（如回环测试）需直接改配置库。打开它可能把系统音频服务卡死，`
          + `这就是它默认拒绝的原因。</span></div>`,
        "声纹入库": () => `<div class="snote info"><span>ⓘ</span><span>`
          + `<b>识别是标配</b>：只要声纹库里已经有这个人，会议就会显示他的姓名 —— `
          + `没有开关（库里没人时它安静地什么都不做）。<b>要不要入库由你决定</b>：`
          + `这里的「改名即入库」是"随手存"，关着时一个字都不会写；`
          + `想精准控制就关掉它，在会议详情的「说话人管理」里逐个点「声纹入库」。`
          + `模板只存本机库，不出网。</span></div>`,
      } },
    { id: "fb", title: "朗读与反馈",
      hint: "播报与提示音：任务做完怎么告诉你。（播放设备池在「能力与智能体 → 设备选择」）",
      common: ["ttsEngine"],
      dynAfter: () => renderTtsStatus(),
      advOrder: ["朗读与反馈", "提示音与通知"],
      adv: ["voiceConfirm", "voiceBrief", "maxBriefChars", "minimalReply", "minimalReplyChars",
            "minimalReplyHint",
            "beepOnStart", "beepOnDone", "beepOnSend", "notifyOnSend"] },
    { id: "queue", title: "队列",
      hint: "正在处理的命令与最近几条；完整历史在「历史 → 指令历史」里。",
      common: () => renderQueueCard() },
    { id: "ws", title: "工作区与归档",
      help: "语音指令与会议纪要生成的会话会登记到这两个目录下 —— 智能体侧栏按目录分组，"
          + "所以「指令空间」和「会议工作区」会各聚成一个分组。目录不存在时会自动建；"
          + "换成自己的目录后，旧会话仍留在原处。",
      common: ["commandWorkspace", "meetingWorkspace"],
      advOrder: ["纪要归档"],
      adv: ["worklogEnabled", "worklogEnsureSessionAccess", "worklogVaultRoot",
            "worklogPrompt"] },
  ],
  /* ---------- ③ 能力与智能体：智能体选择 · 设备选择（模型路由是下面那张静态卡） ---------- */
  capability: [
    { id: "agent", title: "智能体",
      help: "命令与会议纪要交给哪个智能体执行。选中即启用；它的参数在「高级」里。",
      common: () => renderAgentCardCommon(),
      adv: () => renderAgentCardAdv(),
      // 这两块是**函数**渲染的（要状态/按钮），落点表看不见它们 → 键在这里显式声明，
      // 「未归类」兜底卡才不会把它们当成没人管的设置项
      // （纪要归档那一族 2026-09-26 挪到「业务配置 → 工作区与归档」，所以不在这里了）
      covers: ["agentBackend", "harnessHome", "dshBaseUrl", "agentCustomPath",
               "harnessCommand", "harnessPort", "harnessToken",
               "agentCodebuddyEnabled", "agentHarnessEnabled"] },
    { id: "dev", title: "设备选择（设备池与优先级）",
      help: "推理设备决定转写/分离在哪算；下面三条是播放设备池 —— 任务反馈 / 语音指令 / 会议"
          + "各自用哪个，留空 = 系统默认。",
      common: ["device", "outputDeviceIds", "commandOutputDeviceId",
               "meetingOutputDeviceId"] },
    // 「模型路由」那张卡**不在这里**：它是 index.html 里的静态卡（语言模型 / 通道成员 /
    // 派发情况 三个 host 要被 loadRouter 系列反复渲染，重绘会和并发请求互相覆盖）。
    // 卡里那 7 项路由参数由 renderSettingsPanes() 填进 `#rtSetHost`，落点记在
    // SET_PLACED_ELSEWHERE 里；卡内的「保存」= saveRouter()（成员 + 参数）。
    // 会议能力通道（转写/分离/声纹由谁做）**已挪到「业务配置 → 会议」**（一个实体一处状态）。
  ],
};

/* 落点表看不见、但确实有家的键（不是"漏了"，是**由别的渲染路径**画的）。
   `meetingSttModel`（会议转写引擎）就是这一条：归属表把它放在「能力后端 / 本机」，
   而它由「能力与智能体」页里原「能力」页签那块渲染（`capAsrLocal()` 的「会议转写」下拉，见
   `#capKindCards`）—— 所以本文件不再重复画一遍，也不该落进「未归类」兜底卡。
   `router*` 那 7 项同理：它们是「智能体」页那张**静态**「模型路由」卡「高级」里的行，
   由 renderSettingsPanes() 填进 `#rtSetHost`（`SET_CARDS` 里没有这张卡，见那边的注释）。
   注：其余 model/provider 键（sttModel / ttsEngine / wakeEngine / device / voiceprint*）在
   语音与设备页签的卡片里也有控件，那一处重叠是"四个非设置视图重做"要收口的遗留，见交接报告。 */
const SET_PLACED_ELSEWHERE = new Set([
  "meetingSttModel",
  "routerAutoRegister", "routerDisplayName", "routerProbeInterval",
  "routerFirstByteTimeout", "routerConnectTimeout",
  "routerBreakerThreshold", "routerBreakerCooldown",
  // 2026-09-26 IA 重构后由**非 SET_CARDS 的渲染路径**画的三项：
  "modelsDir",             // 「能力与智能体 → 本地能力」卡里（模型权重放哪）
  "modelCleanupDays",      // 「能力与智能体 → 清理」卡里（"近期"是多少天）
  "capabilityEchoServerUrl",   // 「能力与智能体 → 已配对后端连接情况」卡里
]);

/* 说明留在明面上的项（用户规则：只有"不可逆 / 会出网 / 生物特征"这类不平铺进 `?`）。
   其余项的 `description` 一律收进行尾的 `?` 浮窗。
   **明面上留的是短句**（SET_LOUD_NOTE），完整说明仍在 `?` 里 —— 平铺一整段正是用户要治的"啰嗦"。 */
const SET_LOUD_DESC = new Set([
  "apiAuthEnabled",              // 不可逆：开启后本地面板也连不上
  "allowVirtualInputDevice",     // 不可逆：可能把系统音频服务卡死
  "sendEnvContext",              // 出网：把环境上下文发给模型
  "providerAsrBaseUrl", "providerAsrModel", "providerAsrApiKey",   // 出网：音频
  "providerLlmBaseUrl", "providerLlmApiKey",                       // 出网：文本与密钥
  "capabilityPrivacy",           // 出网许可（约束上面三个后端）
  "ttsEngine",                   // edge-tts 会把文本发给服务商
  "meetingAutoSummarize",        // 转写全文会发给模型
  "voiceprintAutoEnroll",        // 生物特征：会往声纹库里写样本（识别本身没有开关了）
  "worklogVaultRoot",            // 会往你的笔记库里写东西
]);
const SET_LOUD_NOTE = {
  apiAuthEnabled: "开启后连本地面板也要令牌：开了就没法从面板关回来，确认了再点保存。",
  allowVirtualInputDevice: "虚拟/接力麦可能把系统音频服务卡死（macOS 实测过），默认拒绝。",
  sendEnvContext: "会把时间/地点这类环境信息一起发给模型。",
  providerAsrBaseUrl: "在线转写会把**音频**发到这个地址。",
  providerAsrModel: "在线转写会把**音频**发给所选服务商。",
  providerAsrApiKey: "密钥只存本机、接口永不回显；留空 = 不改。",
  providerLlmBaseUrl: "在线语言模型会把**文本**发到这个地址。",
  providerLlmApiKey: "密钥只存本机、接口永不回显；留空 = 不改。",
  capabilityPrivacy: "约束三个会议后端能去哪：不出机 / 内网 / 出网。",
  ttsEngine: "选「在线」会把要朗读的文本发给服务商；「本机离线」不出网。",
  meetingAutoSummarize: "开启后转写全文会发给模型服务商。",
  voiceprintAutoEnroll: "开启后**改名就把声纹存进库**（生物特征，只存本机）。关着时一个字都不写；"
    + "想入库就在会议详情里点「声纹入库」。",
  worklogVaultRoot: "纪要会写进这个目录（你的笔记库），确认路径再改。",
};

/* 行里的**短标签**：标签列固定 84px，`?` 还要占位，所以 6 个字以上的标签会被省略号截断。
   这里给长标签一份短名（样张里用的就是这套短词）—— 完整名字仍在 `title` 与 `?` 里，
   设置项的**取值一个字都没动**。 */
const SET_SHORT_LABELS = {
  serverPort: "面板端口", panelOpenMode: "打开方式", panelAutoRefresh: "自动刷新",
  panelAutoStart: "启动即显示", panelStartCollapsed: "显示即收起", apiAuthEnabled: "API 鉴权",
  minimalReplyHint: "极简文案", minimalReplyChars: "极简字数", minimalReply: "极简回复",
  commandWorkspace: "指令空间", commandWorkspaceTitle: "指令分组",
  commandIdleRotateHours: "空闲轮换", commandTargetWorkspace: "目标空间",
  commandTargetSession: "目标会话", meetingsDir: "音频存放", meetingWorkspace: "会议空间",
  meetingWorkspaceTitle: "会议分组", inputDeviceId: "收音设备",
  commandInputDeviceId: "指令麦克风", meetingInputDeviceId: "会议麦",
  outputDeviceIds: "扬声器", commandOutputDeviceId: "指令播报",
  meetingOutputDeviceId: "会议播报", sttModel: "转写引擎", meetingSttModel: "会议引擎",
  wakeEngine: "唤醒方式", device: "计算设备", wakeEnabled: "启用唤醒",
  wakePaused: "唤醒暂停", wakeAliases: "唤醒别名", wakeThreshold: "唤醒阈值",
  sttLanguage: "转写语言", wakeHotkey: "唤醒热键", fallbackHotkey: "回退热键",
  silenceThreshold: "静音阈值", silenceHangoverMs: "静音收尾", noSpeechAbortMs: "放弃毫秒",
  maxRecordMs: "最长录音", consumeMediaKey: "拦截媒体键", triggerKeys: "媒体键",
  voiceprintAutoEnroll: "改名即入库",
  voiceprintThreshold: "匹配阈值", voiceprintMargin: "歧义间隔",
  meetingSegmentMinutes: "分段时长", meetingAutoSummarize: "自动纪要",
  meetingKeepRawAudio: "保留音频",
  ttsEngine: "朗读", voiceConfirm: "复述确认", voiceBrief: "语音简报",
  maxBriefChars: "简报字数", sendEnvContext: "环境上下文",
  allowVirtualInputDevice: "虚拟/接力麦",
  worklogEnabled: "纪要归档", worklogEnsureSessionAccess: "归档权限",
  worklogVaultRoot: "笔记库", worklogPrompt: "归档模板",
  dshBaseUrl: "DSH 地址", harnessHome: "家目录", harnessPort: "端口",
  harnessCommand: "启动命令", harnessToken: "访问 token",
  providerAsr: "转写实现", providerLlm: "模型实现",
  providerAsrBaseUrl: "转写地址", providerAsrModel: "转写模型", providerAsrApiKey: "转写密钥",
  providerLlmBaseUrl: "LLM 地址", providerLlmModel: "LLM 模型", providerLlmApiKey: "LLM 密钥",
  capabilityEchoServerUrl: "后端地址", capabilityPrivacy: "出网许可",
  capabilityMeetingAsrBackend: "会议转写", capabilityDiarizeBackend: "说话人分离",
  capabilityEmbedBackend: "声纹提取",
  capabilityEchoServerToken: "手填令牌", capabilityEchoServerStaticToken: "静态令牌",
  routerAutoRegister: "启动注册", routerDisplayName: "组显示名",
  routerProbeInterval: "探测间隔", routerFirstByteTimeout: "首字节超时",
  routerConnectTimeout: "连接超时", routerBreakerThreshold: "熔断阈值",
  routerBreakerCooldown: "熔断冷却",
};
function sLabel(s) {
  return SET_SHORT_LABELS[s.key] || s.label || s.key;
}

/* 数值行的单位（样张里就是这么标的：标签 + 控件 + 单位）。 */
const SET_UNITS = {
  serverPort: "端口", panelAutoRefresh: "秒", meetingSegmentMinutes: "分钟",
  silenceHangoverMs: "毫秒", noSpeechAbortMs: "毫秒", maxRecordMs: "毫秒",
  maxBriefChars: "字", minimalReplyChars: "字", commandIdleRotateHours: "小时",
  wakeCooldownSec: "秒", wakeConfirmX: "帧", wakeConfirmN: "帧", harnessPort: "端口",
  routerProbeInterval: "秒", routerFirstByteTimeout: "秒", routerConnectTimeout: "秒",
  routerBreakerThreshold: "次", routerBreakerCooldown: "秒",
};

/* 下拉里的**短**选项文案（用户 2026-09-25：`sherpa-onnx 流式（推荐）`→`sherpa`、
   `SenseVoice 中文`→`SenseVoice`…）。**只改显示**：写回后端的 value 仍是原来的 id。
   2026-09-26：whisper 各档的文案删掉了 —— 权重已从本机删除、候选项里也不再有它们
   （老库里的值由 `config.RETIRED_VALUE_FALLBACKS` 折成 sherpa / qwen3asr）。 */
const SET_OPT_LABELS = {
  sttModel: { sensevoice: "SenseVoice", qwen3asr: "Qwen3-ASR", sherpa: "sherpa" },
  meetingSttModel: { sensevoice: "SenseVoice", qwen3asr: "Qwen3-ASR", sherpa: "sherpa" },
  device: { auto: "自动", cpu: "CPU", cuda: "GPU(CUDA)" },
  wakeEngine: { sherpa: "sherpa", kws: "KWS" },
  ttsEngine: { auto: "自动", "edge-tts": "在线", sapi: "本机离线", say: "本机离线",
    espeak: "本机离线", off: "关闭" },
  panelOpenMode: { sidebar: "边条", app: "独立应用", browser: "浏览器" },
  capabilityPrivacy: { none: "不出机", lan: "内网", wan: "出网" },
};

/* 会议转写/分离/声纹三个 hidden 键在界面上合成一个单选 + 一个「会议产出」下拉。 */
const SET_MEETING_BACKENDS = [
  { value: "echo-server", label: "ECHO 后端", hint: "配对好的 GPU 机器" },
  { value: "local", label: "本机", hint: "这台机器的转写引擎" },
  { value: "asr-provider", label: "网络服务商", hint: "外部/内网服务（适配器未实现）" },
];

/* "用哪个实现"相关的配置项：从「设置」页移出，统一由顶部「能力」页签承载
   （前端过滤，后端 grp 不动）——它们都是"每个能力用哪个实现/装没装"这一类问题。
   ttsEngine 也在其中：它是朗读的唯一开关（本地/在线/关闭），能力页签的"语音合成"卡承载它。
   2026-09-25：**设置页 v3 里它们同时也在「语音与设备」页签出现**（归属表把 model/provider
   这些键归到了那一页）；两处编辑入口的收口属于"四个非设置视图重做"那一步，见交接报告。 */
const MODEL_KEYS = new Set([
  "sttModel", "meetingSttModel", "device", "ttsEngine",
  "wakeEngine",
  "voiceprintAutoEnroll", "voiceprintThreshold", "voiceprintMargin",
]);
/* 路由参数（grp=router，7 项）：归属表把它们归到「设置 → 智能体 → 通道设置」，
   所以 v3 里它们由**设置页**渲染（高级区），「模型路由」页签只留一张指路卡。
   键名与 config.DEFAULTS 里 grp="router" 的项一一对应（tests 里钉住，防漂移）。 */
const ROUTER_KEYS = new Set([
  "routerAutoRegister", "routerDisplayName", "routerProbeInterval",
  "routerFirstByteTimeout", "routerConnectTimeout",
  "routerBreakerThreshold", "routerBreakerCooldown",
]);

/* 会议能力通道（谁跑 ASR / 谁跑分离 / 谁跑声纹）：3 个 hidden 键。
   **2026-09-26 IA 重构后它们由「业务配置 → 会议」卡渲染**（`renderMeetingServiceCard()`）——
   用户的原话是"会议（… · 转写走哪条路）"，路由卡只管语言模型与通道成员。
   这一份常量留给用例/文档做"3 个键在这里"的锚点，界面不再从它渲染。 */
const RT_CAP_KEYS = ["capabilityMeetingAsrBackend", "capabilityDiarizeBackend",
                     "capabilityEmbedBackend"];
/*: 路由卡里要跟着卡内「保存」落库的 host：只剩 7 项路由参数（`#rtSetHost`）。
   会议能力通道已挪走 → 别再加 `#rtCapHost`（那个 host 已经不存在了）。 */
const RT_SAVE_HOSTS = ["#rtSetHost"];

/* 智能体元信息（来自 /api/agents）：
   {name, displayName, vendor, description, configKey, enabled, active, available, reason, probe} */
let _agentsCache = [];
const _agentDirty = {};        // 展开区里改过、但还没点保存的值

/** 智能体状态徽标：三个信号要分开说（2026-09-22 实测反馈）。
 *
 *  原来只写"可用"：三行都是"可用"，用户读成"都在用/都正常"，而顶部横幅又在说
 *  "harness 没在运行" —— 看着自相矛盾。其实三个词是三件事：
 *    * available = 探测结论（这个产品在本机能不能用）；
 *    * active    = 当前 ECHO 用的是不是它（单选，看右边的开关）；
 *    * enabled   = 产品自己的启用开关。
 *  所以文案要同时说清"能不能用"与"在不在用"。
 */
function agentStatusChip(a) {
  if (!a) return "";
  let cls = "off", text = "未启用";
  if (a.active && a.available) { cls = "on"; text = "使用中"; }
  else if (a.active) { cls = "bad"; text = "已选但不可用"; }
  else if (a.enabled && a.available) { cls = "idle"; text = "可用（未使用）"; }
  else if (a.enabled) { cls = "bad"; text = "不可用"; }
  return `<span class="agent-chip ${cls}" title="${esc(a.reason || "")}">${esc(text)}</span>`;
}

/** 智能体那一块的重绘入口（v3 里整张卡由 `renderSettingsPanes()` 一次画完 ——
 *  这段保留是为了既有的调用点仍然有效，它现在等于"重画设置区的卡片"）。 */
function renderAgentTable() {
  renderSettingsPanes();
}

/** 当前选中智能体的设置行（v3：与设置页其它行同一套 `.srow` 长相）。
 *
 *  它自己的配置项来自 `/api/agents` 的 `settings` 字段（后端按适配器的 `settings_keys` 组装）——
 *  这些键在 `/api/settings` 里是 hidden 的：把"DSH 服务地址"摊在「面板与服务」里，用户根本
 *  不知道它跟谁有关（2026-09-19 实测反馈："下面的 dsh 没必要吧，或者把端口挪上去"）。
 *  现在它们长在对应智能体的卡片里，与"检测"、状态说明同处一地。 */
function agentDetailHtml() {
  const cur = (_agentsCache || []).find((a) => a.active);
  if (!cur) {
    return `<div class="snote warn"><span>⚠</span><span>没有选中的智能体 —— 在上面的下拉里选一个即可启用。</span></div>`;
  }
  const fields = (cur.settings || []).filter((s) => s.key !== "harnessHome").map((s) => {
    // harnessHome 被排除在这里、改在卡片**常用区**渲染（归属表把它标成「常用」）——
    // 同一行出现两个输入框会让「改的是哪个」说不清。
    const id = "agent-set-" + s.key;
    const curVal = _agentDirty[s.key] !== undefined ? _agentDirty[s.key] : s.value;
    let ctl;
    if (s.secret) {
      // 密钥：只显示"配没配"，值永不下发（后端也不回显）→ 密码框 + 留空 = 不改
      // （2026-09-19 用户实测问"我在哪里配置 key"：这类键原来被整条跳过，面板上没处填）
      ctl = `<input type="password" class="ctl" id="${id}" data-agent-field="${esc(s.key)}"
               data-setkey="${esc(s.key)}" data-secret="1" value="" autocomplete="new-password"
               placeholder="${s.hasValue ? "已配置（留空 = 不改）" : "未配置"}">
             <button type="button" class="btn" data-clear-secret="${esc(s.key)}"
               title="清空这个密钥">清除</button>`;
    } else if (s.value_type === "bool") {
      ctl = `<label class="schk"><input type="checkbox" class="ctl" id="${id}"
        data-agent-field="${esc(s.key)}" data-setkey="${esc(s.key)}" ${curVal ? "checked" : ""}>
        <span>${esc(s.label)}</span></label>`;
    } else if (s.options && s.options.length) {
      ctl = `<select class="ctl" id="${id}" data-agent-field="${esc(s.key)}"
          data-setkey="${esc(s.key)}">` +
        optionPairs(s.options).map((o) =>
          `<option value="${esc(o.value)}" ${String(o.value) === String(curVal) ? "selected" : ""}>`
          + `${esc(sOptLabel(s.key, o.label, o.value))}</option>`).join("") + `</select>`;
    } else {
      ctl = `<input class="ctl" id="${id}" data-agent-field="${esc(s.key)}"
        data-setkey="${esc(s.key)}" value="${esc(curVal || "")}" placeholder="${esc(s.key === "harnessCommand" ? "（自动：本地入口优先）" : "")}">`;
    }
    const loud = SET_LOUD_DESC.has(s.key);
    const help = s.description ? sHelp(s.description) : "";
    return `<div class="srow"><div class="lbl">
        <span class="lt" title="${esc(s.label)}">${esc(sLabel(s))}</span>${help}</div>
      <div class="sctl">${ctl}</div></div>`
      + (loud ? `<div class="sdesc warn">${richText(SET_LOUD_NOTE[s.key] || "")}</div>` : "");
  });
  const note = cur.reason
    ? `<div class="snote ${cur.available ? "info" : "warn"}"><span>${cur.available ? "ⓘ" : "⚠"}</span>`
      + `<span>${esc(cur.reason)}</span></div>`
    : "";
  const warn = cur.available ? "" :
    `<div class="snote warn"><span>⚠</span><span>探测未通过时无法用它执行命令；按上面的提示处理后点「检测」重试。</span></div>`;
  return `<div class="sset-hint">${esc(cur.description || "")}</div>`
    + fields.join("") + note + warn;
}

/** 拉取智能体可用性；probe=true 时做重探测（会真的执行 CLI 探测）。 */
async function loadAgents(probe = false) {
  try {
    const r = await api("/api/agents" + (probe ? "?probe=1" : ""));
    _agentsCache = r.agents || [];
    refreshAgentWebIcon();
    return r;
  } catch (e) { return null; }
}

/** 仪表盘「超级助理」名字后的小图标：当前智能体自带 Web 界面时才出现。
 *
 *  2026-09-19 用户要求："我选了独立 dsh 之后，在仪表盘超级助理名称后面增加一个小图标，
 *  让我点了之后能打开浏览器展示独立 dsh 的 web 端"。
 *  点击**不**在前端拼 URL —— harness 的 URL 里带 token（密钥），拼在前端等于把它写进
 *  浏览器历史；改成让服务端拼好并直接调系统浏览器（POST /api/harness/browser）。 */
function refreshAgentWebIcon() {
  const btn = $("#agentWebOpen");
  if (!btn) return;
  const cur = (_agentsCache || []).find((a) => a.active);
  const show = !!(cur && cur.webUi);
  btn.classList.toggle("hidden", !show);
  if (show) {
    btn.title = `在浏览器里打开「${cur.displayName}」的 Web 界面`;
  }
}

async function refreshAgentsForDashboard() {
  await loadAgents(false);
}

$("#agentWebOpen")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    const r = await api("/api/harness/browser", { method: "POST" });
    toast(r.message || (r.ok ? "已在浏览器打开" : "打开失败"));
  } catch (err) {
    toast("打开失败：" + err.message);
  } finally {
    btn.disabled = false;
  }
});

/* ---------------- 设置 → 智能体 ----------------
   开关/字段/检测的事件都在 v3 那一批委托里（见「设置页 v3：数据 / 渲染 / 交互」段里挂在
   `document` 上的 click/change 监听）—— 这里不再单独绑一遍，否则一次点击会跑两遍
   （「检测」会真的探测两次）。 */

/* ---- 能力页签（2026-09-19 合并：原「模型」页签 + 原「组件」页签 + 设置页的「能力 provider」卡片）----

   为什么合并：三处都在回答同一件事 —— "这台机器上，每个功能**由什么实现、装好了没**"。
     * 原「模型」页签：用哪个本地引擎 + 装没装 + 下载；
     * 原「组件」页签：装没装 + 怎么装（纯表格，没有动作）；
     * 设置页的 provider 卡片：用哪个（本地/在线/路由）+ 出网吗 + 在线服务地址密钥。
   用户直接指出这是设计重叠 → 现在**每个能力只在这一处看全**：先"用哪个"，再"装没装/怎么装"。

   页面结构：三个能力卡（语音转写 / 语言模型 / 语音合成）+ 其他功能卡（唤醒 / 说话人分离 /
   声纹 / 计算设备，复用 renderModelCard）+ 运行环境（运行时 / 加速 / 智能体后端）。

   数据面：
     /api/components?includeBlocked=true   装什么（有 model_id 的就绪判定问 modelinfo，单一判据）
     /api/models                           模型下载元数据与进度（按 model_id 关联）
     /api/providers[?ready=true]           实现清单（本地 / 在线 / 路由 + 出网 + 就绪）
     /api/providers/config|presets         在线服务的地址/模型/密钥（遮罩）与一键预设
     /api/settings                         每个能力的"用哪个"（sttModel/ttsEngine/wakeEngine…）
     /api/stt/status、/api/voiceprints     设备与声纹库（计算设备/声纹卡用）            */
let _capCache = { comps: null, prov: null, cfg: null, presets: null };
let _capPoll = null;

/* 能力页签上的能力种类。**语言模型不在其中**：它的"用哪个实现"与路由的上游配置是同一件事
   （ECHO AUTO 的成员就在「模型路由」里配），2026-09-19 用户要求整合到「模型路由」页签去。 */
const CAP_KINDS = ["asr", "tts"];
// CAP_META 只描述能力页签上的卡片；语言模型那块渲染在「模型路由」页签里（见 loadRouterLlm）
const CAP_META = {
  asr: { icon: "🎤", title: "语音转写", note: "命令口述与会议录音都走它；本地引擎与在线服务二选一" },
  tts: { icon: "🔊", title: "语音合成", note: "朗读复述确认、语音简报与提示语；off = 完全不朗读" },
};
// 注：TTS 候选项的显示名由 capTtsOptionLabel() 现算（名字 + 出网/就绪），
// 不在这里维护一份静态映射 —— 静态映射拿不到运行时的就绪状态，就会逼出第二行状态文字。

/** 某类能力的可装组件（本平台适用的）。 */
function capCompsOf(kind) {
  const items = ((_capCache.comps || {}).items) || [];
  return items.filter((c) => c.kind === kind && c.applicable);
}

/** TTS 的"当前实现"名字：由 ttsEngine 派生（它是朗读的唯一开关）。 */
function capTtsCurrentName() {
  const engine = String((settingByKey("ttsEngine") || {}).value || "auto");
  if (engine === "off") return "已关闭朗读";
  if (engine === "edge-tts") return "edge-tts（微软在线）";
  if (engine === "auto") return "自动（优先 edge-tts）";
  return "本机离线合成（" + engine + "）";
}

/** 组件状态徽标。 */
function capCompBadge(c) {
  const m = c.model_id ? modelById(c.model_id) : null;
  const job = (m && _modelJobsCache[m.id]) || {};
  if (job.status === "running") return modelBadge(`下载中 ${Math.round(job.percent || 0)}%`, "warn");
  if (c.ready === true) return modelBadge("✅ 已就绪", "ok");
  // 「本机服务」（DSH Desktop / 独立 harness）不是"没装"，是"没在跑" —— 措辞要准
  // 两个智能体是**二选一**：没选中的那条要说"未使用"，否则用户读成"坏了"（同事 2026-09-21 反馈）
  if (c.service) {
    if (c.active === false) return modelBadge("⏹ 未使用（你选的是另一个）", "idle");
    return modelBadge(c.ready === false ? "⏹ 未运行" : "未知", "warn");
  }
  // 「缺 pip 依赖」与「模型没下」是**两件事**：前者说"未安装"会把用户引去下载模型
  // （模型其实已经在了）—— 2026-09-23 实测事故就是这么被误导的。
  if (c.ready === false && c.readyKind === "python") return modelBadge("⚠ 缺依赖", "warn");
  if (c.ready === false) return modelBadge(c.model_id ? "⬇ 未安装" : "未安装", "warn");
  return modelBadge("未知", "idle");
}

/** 组件行的「获取」动作。
 *  * 模型类（有 model_id）：走 /api/models 的「下载 / 重新下载」等；
 *  * pip 类（有 command）：给「下载命令」——复制的是**后端拼好的、带本机解释器路径**的命令。
 *    为什么必须带解释器：裸 `pip install x` 会装到 PATH 上第一个 Python 里，
 *    ECHO 自己的 venv 看不到 → 面板永远显示"未安装"（用户实测问过"装哪个环境/哪个目录"）；
 *  * 其它（如 DSH Desktop 这种"装客户端"的）：不给按钮，说明放在行的 meta 里。
 */
function capCompActions(c) {
  const m = c.model_id ? modelById(c.model_id) : null;
  if (m) {
    const job = _modelJobsCache[m.id] || {};
    if (job.status === "running") {
      return `<span class="muted" style="font-size:12px">下载中 ${Math.round(job.percent || 0)}%</span>`;
    }
    // 模型类组件也可能缺 **pip 依赖**（如 sherpa：模型下好了、sherpa_onnx 没装）。
    // 这时光给「下载模型」是错的 —— 把后端拼好的安装命令一并摆出来。
    const needPip = c.readyKind === "python" && c.command;
    const pipBtn = needPip
      ? `<button type="button" class="btn mini" data-mcopy="${esc(c.command)}"
          title="先装引擎依赖（复制后粘进终端执行）：&#10;${esc(c.command)}"
          >复制安装命令</button>`
      : "";
    return modelActions(m) + pipBtn;
  }
  const btns = [];
  if (c.command) {
    // 标签由清单给：pip 类 = 「下载命令」；独立 harness = 「复制启动命令」
    const label = c.command_label || "下载命令";
    btns.push(`<button type="button" class="btn mini" data-mcopy="${esc(c.command)}"
      title="复制后粘进终端执行（cmd 与 PowerShell 都行，在哪个目录执行都行）：&#10;${esc(c.command)}"
      >${esc(label)}</button>`);
  }
  if (c.ref) btns.push(`<span class="muted" style="font-size:12px">${esc(c.ref)}</span>`);
  return btns.join(" ");
}

/** 一行模型的**使用情况**：占用 · 上次使用 · 次数 · 保留钉子 · 在用。
 *
 *  数据全部来自 `/api/models`（库里的 `model_usage` 账本，见 app/model_usage.py）——
 *  面板**不猜**，账本里没有记录就如实写"从未使用"。这是「清理」那张卡能成立的前提：
 *  用户要看到"哪一项多久没动过"，而不是一句"看起来不常用"。 */
function modelUsageBits(m) {
  if (!m) return "";
  const bits = [`占用 ${m.local_mb ? fmtMb(m.local_mb) : "0 MB"}`];
  bits.push(m.lastUsedAt ? `上次使用 ${m.lastUsedAt}` : "从未使用");
  bits.push(`用过 ${m.useCount || 0} 次`);
  if (m.inUse) bits.push("▶ 在用");
  if (m.pinned) bits.push("📌 已保留");
  return bits.join(" · ");
}

/** 组件清单：能力卡里的"装没装 / 怎么装"。`currentIds` 命中的行标「当前使用」。
 *
 *  2026-09-19 从**表格**改成**逐条行**（用户实测："这个内容布局不太好看，按钮字太多"）：
 *  窄边条里表格的「用途」列被压成一列竖排的汉字、按钮也竖着排。改成行之后：
 *    第一行 = 名称 + 状态徽标（当前用的标「▶ 当前」）
 *    第二行 = 体积 · 用途（说明整行展开，不再逐字换行）
 *    第三行 = 动作按钮（自动换行，不挤）
 *  宽屏（≥861px）下用 CSS 网格把动作挪到右侧，视觉上仍是紧凑两栏。
 */
function capCompTable(comps, currentIds) {
  if (!comps.length) return `<div class="muted" style="font-size:12px">本平台没有可装的组件。</div>`;
  const cur = new Set((currentIds || []).filter(Boolean));
  const rows = comps.map((c) => {
    const isCur = cur.has(c.model_id);
    // 体积为 0/未知（如"本机服务"类）时不显示 "0 MB"
    const meta = [c.size_mb ? c.size_mb + " MB" : "", c.purpose || ""].filter(Boolean).join(" · ");
    // 非模型组件把 how 也显示出来：它们的动作按钮可能没有（如 DSH 只装客户端），
    // 说明不能只藏在按钮的复制内容里（用户实测问过"这条命令该在哪执行"）。
    const how = (!c.model_id && c.how) ? `<div class="cap-comp-meta">${esc(c.how)}</div>` : "";
    // 模型行补一行**使用情况**（上次使用 / 次数 / 占用 / 保留 / 在用）——
    // 用户在「本地能力」区要看的就是"这一项多久没动过"，清理建议也照着它算。
    const usage = c.model_id ? modelUsageBits(modelById(c.model_id)) : "";
    return `<div class="cap-comp${isCur ? " cur" : ""}">
      <div class="cap-comp-main">
        ${isCur ? `<span class="cap-cur">▶ 当前</span>` : ""}
        <span class="cap-comp-name">${esc(c.name || c.id)}</span>
        <span class="cap-comp-badge">${capCompBadge(c)}</span>
      </div>
      <div class="cap-comp-meta">${esc(meta)}</div>
      ${usage ? `<div class="cap-comp-meta usage">${esc(usage)}</div>` : ""}
      ${c.readyReason ? `<div class="cap-comp-meta" style="color:var(--red)">${esc(c.readyReason)}</div>` : ""}
      ${c.readyNextStep ? `<div class="cap-comp-meta" style="color:var(--red)">下一步：${esc(c.readyNextStep)}</div>` : ""}
      ${how}
      <div class="cap-comp-acts">${capCompActions(c)}</div>
    </div>`;
  }).join("");
  return `<div class="cap-comps">${rows}</div>`;
}

/** 卡片里的**折叠区**（默认收起）：与卡片同一套折叠存储（`data-collapse-id` + localStorage）。
 *
 *  为什么要有它（用户 2026-09-26 原话）："界面设计的时候要默认折叠掉，现在的界面内容太多了，
 *  易用性太差"。所以"留着但不常用"的东西（whisper 三档、未安装的模型）默认收起来，
 *  点标题才展开 —— 而不是删掉它们，也不是把整页拉长。 */
function foldGroup(id, title, body, opts) {
  const o = opts || {};
  const badge = o.badge ? `<span class="sbadge ${o.badgeCls || ""}">${esc(o.badge)}</span>` : "";
  return `<div class="mcard fold collapsible${o.cls ? " " + o.cls : ""}"
      data-collapse-id="${esc(id)}" data-collapse-default="${o.open ? "open" : "closed"}">
    <div class="mcard-head"><span class="set-arrow">▶</span>
      <div class="mcard-title">${esc(title)}</div>${badge}</div>
    <div class="mcard-body">${body}</div>
  </div>`;
}

/** 在线服务的一个字段（地址/模型名/密钥）。密钥沿用服务端遮罩：空 = 不改。 */
function capCfgField(key) {
  const s = ((_capCache.cfg || {}).settings || []).find((x) => x.key === key);
  if (!s) return "";
  const id = "cap-" + key;
  if (s.secret) {
    return `<div class="set-row" style="margin:0">
      <label for="${id}">${esc(s.label)}</label>
      <div style="display:flex;gap:6px;align-items:center">
        <input type="password" class="ctl" id="${id}" data-key="${key}" data-secret="1" style="flex:1"
          value="" autocomplete="new-password"
          placeholder="${s.hasValue ? "已配置（留空 = 不改）" : "未配置"}">
        <button type="button" class="btn" data-clear-secret="${key}"
          style="flex:0 0 auto;padding:2px 8px;font-size:12px" title="清空这个密钥">清除</button>
      </div>
    </div>`;
  }
  return `<div class="set-row" style="margin:0">
    <label for="${id}">${esc(s.label)}</label>
    <input class="ctl" id="${id}" data-key="${key}" value="${esc(s.value || "")}">
    <div class="desc">${esc(s.description || "")}</div>
  </div>`;
}

/** 在线服务预设：一键把公开的地址与模型名填进字段（密钥仍要自己填）。 */
function capPresets(kind) {
  const all = (_capCache.presets || {}).presets || [];
  const mine = all.filter((p) => (p.kind || "llm") === kind);
  if (!mine.length) return "";
  return `<div class="set-row" style="margin:0"><label>在线服务预设</label>
      <div style="display:flex;flex-wrap:wrap;gap:6px">` +
    mine.map((p) => {
      const idx = all.indexOf(p);
      return `<button type="button" class="btn" data-preset-index="${idx}"
        style="padding:3px 10px;font-size:12px" title="${esc(p.note || "")}">${esc(p.name)}</button>`;
    }).join("") +
    `</div><div class="desc">点一下把<b>地址与模型名</b>填进上面的字段；密钥请手动填（接口永不回显）；` +
    `内网网关的地址属单位内部信息，需自己填。</div></div>`;
}

/** 下拉里那一项的显示名：名字 + （出网/本地 · 就绪）。

    状态**只在这一处说**：下面再挂一行"当前 XXX · 已就绪"是纯重复
    （2026-09-19 用户看着截图直接指出："下拉框下面的附属没有意义"）。
    选中的那一项在收起状态下就是一行文字，用户照样看得见状态。
 */
function capOptLabel(name, p) {
  if (!p) return name;
  const bits = [p.egress ? "出网" : "本地"];
  if (p.ready === true) bits.push("已就绪");
  else if (p.ready === false) bits.push("未就绪");
  return `${name}（${bits.join(" · ")}）`;
}

/** TTS 的候选项显示名。TTS 选的是 `ttsEngine`（不是 provider id），所以名字要自己拼：
    "离线引擎"这一档带上本机离线实现的名字，状态也带出来（否则它会没有任何状态显示）。 */
function capTtsOptionLabel(engine, provs) {
  const edge = provs.find((p) => p.id === "edge-tts");
  const offline = provs.find((p) => p.id !== "edge-tts");
  if (engine === "off") return "关闭朗读（不出声）";
  if (engine === "edge-tts") return capOptLabel("edge-tts（微软在线）", edge);
  if (engine === "auto") {
    return (edge && edge.ready === false)
      ? "自动（edge-tts 未就绪 → 走离线）"
      : "自动（优先 edge-tts，失败回退离线）";
  }
  return capOptLabel(`本机离线合成（${engine}）`, offline);
}

/** 当前选中的实现会不会出网：``{on, note}``（note = "发什么、给谁"）。
    判断只有这一处：卡片上那个黄色三角图标由它决定，弹不弹、说什么都在这里。 */
function capEgress(kind) {
  const provs = ((_capCache.prov || {}).providers || []).filter((p) => p.kind === kind);
  if (kind === "tts") {
    const engine = String((settingByKey("ttsEngine") || {}).value || "auto");
    const edge = provs.find((p) => p.id === "edge-tts");
    const on = engine === "edge-tts" || (engine === "auto" && edge && edge.ready !== false);
    return { on: !!on, note: (edge && edge.egress_note) || "被朗读的文本会发送到微软合成语音" };
  }
  const cur = provs.find((p) => p.active) || provs[0];
  return cur && cur.egress ? { on: true, note: cur.egress_note || "" } : { on: false, note: "" };
}

/** 出网提醒：黄色三角叹号图标，鼠标悬停用原生 title 浮出说明。
 *
 *  2026-09-19 用户要求："数据出网提醒改成找合适位置显示黄色三角叹号的标，鼠标放上去浮动显示提示内容"
 *  —— 原来它占一整行（一行"数据会出网：…"），三张卡各一行，卡片被撑长；而且这句话只在
 *  选中的实现确实会出网时才成立。现在收成一个图标，放在"用哪个实现"下拉的右边（贴着它所指的对象），
 *  说明进 title（与面板其它 30 多处提示同一套做法）。**不出网时什么都不显示**（选项里已写"本地"）。
 */
function capEgressIcon(kind) {
  const e = capEgress(kind);
  if (!e.on) return "";
  const tip = "数据会出网：" + (e.note || "内容会发到外部服务");
  return `<span class="cap-egress" title="${esc(tip)}" aria-label="${esc(tip)}" role="img">
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <path d="M12 2.6 22.4 21H1.6Z" fill="#f5b301" stroke="#8a6a00" stroke-width="1.2"
              stroke-linejoin="round"/>
        <path d="M12 9.2v5.4" stroke="#4a3800" stroke-width="2.1" stroke-linecap="round"/>
        <circle cx="12" cy="17.8" r="1.25" fill="#4a3800"/>
      </svg></span>`;
}

/** 「用哪个实现」区块：下拉（选中即生效）+ 出网图标 + 在线服务的地址/模型/密钥。
 *
 *  刻意只留这些：下拉自带「出网/本地 · 就绪」，再补一行"当前 …"、或再列一遍"两种实现"，
 *  都是同一份状态的第三、第四份拷贝（用户实测反馈）；出网说明也从一整行收成三角图标。
 */
function capProviderBlock(kind) {
  const provs = ((_capCache.prov || {}).providers || []).filter((p) => p.kind === kind);
  if (!provs.length) return "";
  const isTts = kind === "tts";
  const cur = isTts ? null : (provs.find((p) => p.active) || provs[0]);
  let select = "";
  if (isTts) {
    // TTS 的"用哪个"就是 ttsEngine（含 off）：选项从设置元数据来，避免再造一个开关
    const meta = settingByKey("ttsEngine");
    const opts = (meta && meta.options) || [];
    select = `<select class="ctl" data-tts-engine="1" title="朗读用哪个实现" aria-label="朗读用哪个实现">` +
      opts.map((o) =>
        `<option value="${esc(o)}" ${String(o) === String(meta && meta.value) ? "selected" : ""}>` +
        `${esc(capTtsOptionLabel(o, provs))}</option>`).join("") + `</select>`;
  } else {
    select = `<select class="ctl" data-provider-kind="${esc(kind)}" title="用哪个实现" aria-label="用哪个实现">` +
      provs.map((p) =>
        `<option value="${esc(p.id)}" ${p.active ? "selected" : ""}>` +
        `${esc(capOptLabel(p.name, p))}</option>`).join("") + `</select>`;
  }
  return `<div class="cap-prov">
      <div class="cap-pick">${select}${capEgressIcon(kind)}</div>
      ${capOnlineBlock(kind, cur)}
    </div>`;
}

/** 选中在线实现时就地展开它需要的地址/模型/密钥（+ 预设 + 保存）。 */
function capOnlineBlock(kind, cur) {
  const ONLINE = { asr: { prefix: "providerAsr", on: "openai-asr" },
                   llm: { prefix: "providerLlm", on: "openai-llm" } };
  const o = ONLINE[kind];
  if (!o || !cur || cur.id !== o.on) return "";
  return `<div class="cap-online">
      ${capCfgField(o.prefix + "BaseUrl")}${capCfgField(o.prefix + "Model")}${capCfgField(o.prefix + "ApiKey")}
      ${capPresets(kind)}
      <div style="display:flex;gap:8px;align-items:center;padding-top:4px">
        <button type="button" class="btn" data-cap-save="1">保存在线服务设置</button>
        <span class="muted" style="font-size:12px">密钥留空 = 不改；下拉选择是选中即生效</span>
      </div>
    </div>`;
}

/** 转写卡的本地面：三个引擎入口 + 本地模型分两堆（**配置为要用的** / 其余折叠）。
 *
 *  2026-09-26 IA 重构（用户原话："本地能力部署运行情况（仅展示配置了要用的本地模型，
 *  其它的折叠起来）… whisper 的三个你建议留着的留下，但是界面设计的时候要默认折叠掉"）：
 *  whisper tiny/base/small 的权重还在本机、但面板早就不再提供它们（退役），
 *  所以它们落在**默认收起的「其余本地引擎」**里 —— 留着，不占视野。
 */
function capAsrLocal() {
  const stt = settingByKey("sttModel");
  const mstt = settingByKey("meetingSttModel");
  const curIds = [engineModelId(stt && stt.value), engineModelId(mstt && mstt.value)];
  const cur = new Set(curIds.filter(Boolean));
  const comps = capCompsOf("stt");
  const used = comps.filter((c) => cur.has(c.model_id));
  const rest = comps.filter((c) => !cur.has(c.model_id));
  const restMb = rest.reduce((sum, c) => {
    const m = modelById(c.model_id);
    return sum + ((m && m.local_mb) || 0);
  }, 0);
  return `<div class="cap-sub">本地引擎（这几条就是「业务配置」里选中的那两台引擎）</div>
    <div class="cap-engine-row">
      <label>命令转写${_selectHtml("sttModel", stt && stt.options, stt && stt.value)}</label>
      <label>会议转写${_selectHtml("meetingSttModel", mstt && mstt.options, mstt && mstt.value)}</label>
    </div>
    <div class="cap-sub">配置为要用的（${used.length} 项）</div>
    ${used.length ? capCompTable(used, curIds)
                  : `<div class="muted" style="font-size:12px">两台引擎都走在线服务或 ECHO 后端，
                       本机没有必须装的转写引擎。</div>`}
    ${rest.length ? foldGroup("cap-engines-rest",
      `其余本地引擎（${rest.length} 项 · 留着但不常用）`,
      capCompTable(rest, curIds),
      { badge: restMb ? `占用 ${fmtMb(restMb)}` : "" }) : ""}`;
}

/** 一张能力卡：标题 + 说明 + 「用哪个」+ 各自的补充内容。可折叠（点标题）。
 *
 *  折叠是 2026-09-19 用户要求："能力下面的各卡片也增加折叠功能" ——
 *  这张页签内容长（转写那张有 8 个模型行），折叠状态记在 localStorage，重绘后仍保持。
 */
function capKindCard(kind) {
  const meta = CAP_META[kind] || { icon: "•", title: kind, note: "" };
  let body = capProviderBlock(kind);
  if (kind === "asr") body += capAsrLocal();
  if (kind === "llm") {
    // 只留一个入口按钮：成员与优先级的说明已经在下拉下面那行「会出网」里说过了
    body += `<div class="mcard-act"><button type="button" class="btn mini" data-goto="agent">去通道成员</button></div>`;
  }
  // TTS 不再单列「两种实现」：那两行的状态与下拉选项里的（出网/本地 · 就绪）是同一份信息，
  // 顶部概览条也已经各给了一个点 + 名字（2026-09-19 用户实测反馈：重复）。
  return `<div class="mcard collapsible" data-collapse-id="cap-${esc(kind)}">
    <div class="mcard-head"><span class="set-arrow">▶</span><div class="mcard-ic">${meta.icon}</div>
      <div class="mcard-title">${esc(meta.title)}</div></div>
    <div class="mcard-body">
      <div class="muted" style="font-size:12px;margin-bottom:6px">${esc(meta.note)}</div>
      ${body}
    </div></div>`;
}

/** 顶部一览：三个能力当前用的是什么 + 组件就绪计数。 */
function renderCapOverview() {
  const host = $("#capOverview");
  const items = ((_capCache.comps || {}).items) || [];
  // 就绪判据与「常规 → 启动状态」那条摘要共用 compsTally()：两处数字不会打架
  const tally = compsTally(items);
  const provs = ((_capCache.prov || {}).providers) || [];
  const dotOf = (ok) => (ok === true ? "green" : ok === false ? "yellow" : "idle");
  if (host) {
    host.innerHTML = CAP_KINDS.map((k) => {
      if (k === "tts") {
        const engine = String((settingByKey("ttsEngine") || {}).value || "auto");
        const edge = provs.find((p) => p.id === "edge-tts") || {};
        const ok = engine === "off" ? false : (engine === "auto" ? edge.ready !== false : true);
        return `<span class="ov-item"><span class="dot d-${dotOf(ok)}"></span>${esc(CAP_META[k].title)} ` +
          `<b>${esc(capTtsCurrentName())}</b></span>`;
      }
      const p = provs.find((x) => x.kind === k && x.active) || provs.find((x) => x.kind === k) || {};
      return `<span class="ov-item"><span class="dot d-${dotOf(p.ready)}"></span>${esc(CAP_META[k].title)} ` +
        `<b>${esc(p.name || "—")}</b></span>`;
    }).join("");
  }
  const sum = $("#capOvSummary");
  if (sum) sum.textContent = `组件就绪 ${tally.ready}/${tally.total}`;
}

/** 运行环境：装不了就得手装的 pypi 类组件（运行时 / 加速 / 智能体后端）。 */
function renderCapEnv() {
  const host = $("#capEnvHost");
  if (!host) return;
  const comps = capCompsOf("runtime").concat(capCompsOf("accel"), capCompsOf("agent"));
  const blocked = (((_capCache.comps || {}).items) || []).filter((c) => !c.applicable);
  const blockedHtml = blocked.length
    ? `<div class="muted" style="margin-top:8px;font-size:12px">本平台不适用（显示但不可选）：
         <ul style="margin:4px 0 0 18px">` +
      blocked.map((c) => `<li>${esc(c.name || c.id)}：${esc(c.blockedReason || "不适用")}</li>`).join("") +
      `</ul></div>`
    : "";
  const plat = ((_capCache.comps || {}).platform || "") +
    (((_capCache.comps || {}).osVersion) ? " " + _capCache.comps.osVersion : "");
  const envTally = compsTally(((_capCache.comps || {}).items) || []);
  host.innerHTML = `<div class="card collapsible" data-collapse-id="cap-env"
      data-collapse-default="closed">
    <div class="card-title"><span class="set-arrow">▶</span><span class="ic">🧱</span>运行环境（组件）
      <span class="muted">${esc(plat)}</span>
      <span class="muted" style="margin-left:auto">就绪 ${envTally.ready}/${envTally.total}</span>
    </div>
    <div class="card-body">
      ${capCompTable(comps, [])}
      <div class="muted" style="margin-top:6px;font-size:12px">
        这些是本机依赖与服务：pip 类的点「下载命令」复制到剪贴板后自己执行（命令里带的是本机解释器，
        在哪个目录、用 cmd 还是 PowerShell 都行）；服务类（DSH Desktop）装客户端并保持运行即可，
        独立 harness 则可以由 ECHO 随自己拉起（设置 → 智能体）。
      </div>
      ${blockedHtml}
    </div>
  </div>`;
}

/* ============ 「能力与智能体」第三页签的其后三块（2026-09-26 IA 重构） ============

   用户给的结构（原话）：
     ├─ 已配对后端连接情况（配对状态 · 健康 · 发授权/撤销入口）→ `#capRouteCard`（静态卡）
     ├─ 本地能力部署运行情况（**默认只展示"配置了要用的"**；其余默认折叠；每项带使用情况）
     └─ 清理（近期没再使用的模型 → **先预览、后确认**）

   这一块只讲**事实**（装没装 / 多大 / 多久没用过）与**动作**（下载 / 保留 / 删除）；
   "用哪个引擎"的选择在「业务配置」里 —— 一个实体只有一处状态（规则②）。 */

/** 模型目录（`modelsDir`）那一行：权重放哪。**只有这一处能改**（落点见 SET_PLACED_ELSEWHERE）。 */
function renderModelsDirRow() {
  const host = $("#capModelsDirHost");
  if (!host) return;
  host.innerHTML = `<div class="cap-sub">存储位置</div>`
    + renderSettingRows(settingRows(["modelsDir"]));
}

/* ---------- 清理（先预览、后确认） ---------- */
let _cleanupView = null;

async function loadCleanupCard() {
  const body = $("#cleanupBody");
  if (!body) return;
  try {
    _cleanupView = await api("/api/models/cleanup/preview");
  } catch (e) {
    body.innerHTML = `<div class="mcard-warn">读取失败：${esc(e.message)}</div>`;
    return;
  }
  const host = $("#cleanupDaysHost");
  // 阈值那一行（modelCleanupDays）跟着渲染；**正在输入时不重绘**（别抢焦点）
  if (host && !host.contains(document.activeElement)) {
    host.innerHTML = renderSettingRows(settingRows(["modelCleanupDays"]));
  }
  renderCleanupCard();
}

function cleanupUsageText(it) {
  const bits = [`占用 ${fmtMb(it.localMb)}`];
  bits.push(it.lastUsedAt ? `上次使用 ${it.lastUsedAt}` : "本机无使用记录");
  if (it.useCount) bits.push(`用过 ${it.useCount} 次`);
  if (it.idleDays != null) bits.push(`${it.idleDays} 天没有变动`);
  if (it.retired) bits.push("已退役（面板不再提供）");
  return bits.join(" · ");
}

function renderCleanupCard() {
  const body = $("#cleanupBody");
  if (!body) return;
  const v = _cleanupView || { items: [], suggested: [], suggestedMb: 0, days: 90 };
  const items = v.items || [];
  const sug = items.filter((it) => it.suggested);
  const badge = $("#cleanupBadge");
  if (badge) {
    badge.textContent = sug.length ? `${sug.length} 项建议 · 可释放 ${fmtMb(v.suggestedMb)}`
                                   : "暂无建议";
    badge.className = "badge " + (sug.length ? "warn" : "idle");
  }
  if (!items.length) { body.innerHTML = `<div class="muted">模型清单是空的。</div>`; return; }
  const rows = items.map((it) => {
    const picked = it.suggested;
    const box = picked
      ? `<input type="checkbox" data-cleanup-pick="${esc(it.id)}" checked
           title="勾上才会删（默认勾选的是建议项）">`
      : `<input type="checkbox" disabled title="${esc(it.protectReason || it.reason || "不列入建议")}">`;
    const state = picked ? modelBadge("建议清理", "warn")
      : (it.protected ? modelBadge("不动", "ok") : modelBadge("留着", "idle"));
    const pin = `<button type="button" class="btn mini" data-pin-toggle="${esc(it.id)}"
        title="钉住 = 永久不列入清理建议（钉子和使用记录都只存本机）">${it.pinned ? "取消保留" : "保留"}</button>`;
    const paths = (it.paths || []).filter((p) => p.exists)
      .map((p) => `<code>${esc(p.path)}</code>`).join("<br>");
    return `<div class="cap-comp${picked ? " cur" : ""}">
      <div class="cap-comp-main">${box}
        <span class="cap-comp-name">${esc(it.name || it.id)}</span>
        <span class="cap-comp-badge">${state}</span></div>
      <div class="cap-comp-meta">${esc(cleanupUsageText(it))}</div>
      <div class="cap-comp-meta">${esc(it.reason || "")}</div>
      ${paths ? `<div class="cap-comp-meta">${paths}</div>` : ""}
      <div class="cap-comp-acts">${pin}</div>
    </div>`;
  }).join("");
  const totalMb = sug.reduce((s, it) => s + (it.localMb || 0), 0);
  body.innerHTML = `
    <div class="muted" style="font-size:12px;line-height:1.6">
      判据是<b>本机的使用账本</b>（这个模型什么时候真的被加载/调用过），不是文件修改时间 ——
      所以"刚拷进来一次"不会被当成"刚用过"，"真用过一次"也不会被当成"从没用过"。
      「近期」= <b>${esc(v.days)} 天</b>没用过（下面可改）；<b>正在用的、当前配置选中的、
      打了「保留」钉子的一律不动</b>。删除会<b>同时覆盖两处缓存</b>：
      <code>models\\</code> 与本机 <code>~/.cache/modelscope/models</code>；
      删掉要重新下载才能再用。
    </div>
    ${rows}
    <div class="sacts" style="padding-top:6px">
      <button class="btn danger" id="btnCleanupRun" ${sug.length ? "" : "disabled"}
        title="先给你看这一张清单，确认之后才删">删除选中的 ${sug.length} 项（约 ${fmtMb(totalMb)}）</button>
      <button class="btn" id="btnCleanupReload">重新预览</button>
      <span class="muted" id="cleanupState" style="font-size:11px"></span>
    </div>`;
}

/** 真删：**先弹确认**（把清单原样写进去），删完**如实回报**（删了什么、释放多少、谁失败、为什么）。 */
async function runModelCleanup() {
  const picks = $$("#cleanupBody [data-cleanup-pick]")
    .filter((el) => el.checked && !el.disabled).map((el) => el.dataset.cleanupPick);
  if (!picks.length) { toast("先勾选要删的模型"); return; }
  const items = ((_cleanupView || {}).items || []).filter((it) => picks.includes(it.id));
  const plan = items.map((it) => `· ${it.name}（${fmtMb(it.localMb)}，`
    + (it.lastUsedAt ? `上次使用 ${it.lastUsedAt}` : "无使用记录") + `）`).join("\n");
  const go = await confirmDialog(
    `将删除下面这些本地模型并释放磁盘：\n\n${plan}\n\n`
    + `两处缓存都会清（models\\ 与本机 ModelScope 缓存）。删掉之后要重新下载才能再用。`,
    { okText: "删除", cancelText: "取消", danger: true });
  if (!go) return;
  const st = $("#cleanupState");
  if (st) st.textContent = "正在删除…";
  try {
    const r = await api("/api/models/cleanup", { method: "POST",
      body: JSON.stringify({ ids: picks }) });
    const lines = [];
    (r.removed || []).forEach((x) => lines.push(`✓ ${x.name}：释放 ${x.freedBytes} 字节`));
    (r.failed || []).forEach((x) => lines.push(`✗ ${x.id}：${x.reason}`));
    lines.unshift(`删除 ${(r.removed || []).length} 个，释放 ${fmtMb(r.freedMb)}`);
    toast(lines.join("\n"), 9000);
    if (st) st.textContent = lines.join(" · ");
    await loadCapabilities();                    // 占用与清单都变了，重拉一遍
  } catch (e) {
    toast("清理失败：" + e.message, 7000);
    if (st) st.textContent = "失败：" + e.message;
  }
}

async function toggleModelPin(id, pinned) {
  try {
    const r = await api("/api/models/pin", { method: "POST",
      body: JSON.stringify({ id: id, pinned: !!pinned }) });
    toast(r.message || "已更新");
    await loadCapabilities();
  } catch (e) { toast("设置保留失败：" + e.message); }
}

/* ---------- 业务配置 → 队列 ---------- */
let _queueCache = null;

function queueBodyHtml() {
  const st = _statusCache || {};
  const cmds = (_queueCache && _queueCache.items) || [];
  const busy = st.busy
    ? `<span class="sbadge warn">正在处理：${esc(st.busyOwner || "命令")}`
      + `${st.busyPhase ? " · " + esc(st.busyPhase) : ""}</span>`
    : `<span class="sbadge ok">空闲</span>`;
  const rows = cmds.length ? cmds.map((c) => `<div class="cmd-item">
      <div class="head"><span class="muted" style="font-size:12px">${esc(c.status || "")}</span>
        <span class="time">${esc(c.created_at || c.ts || "")}</span></div>
      <div class="text">${esc(String(c.text || "").slice(0, 160))}</div>
    </div>`).join("")
    : `<div class="muted" style="font-size:12px">队列里没有命令。</div>`;
  return `<div class="srow"><div class="lbl"><span class="lt">执行队列</span></div>
      <div class="sctl">${busy}</div></div>
    <div class="muted" style="font-size:12px">最近 ${cmds.length} 条（一次只跑一条）</div>
    <div class="cmd-list">${rows}</div>
    <div class="sacts" style="padding-top:6px">
      <button class="btn mini" data-goto="history">全部历史 ›</button>
      <button class="btn mini" data-goto="meetings">会议记录 ›</button>
    </div>`;
}

function renderQueueCard() {
  return `<div id="setQueueHost">${queueBodyHtml()}</div>`;
}

async function loadQueueCard() {
  const host = $("#setQueueHost");
  if (!host) return;
  try { _queueCache = await api("/api/commands?limit=3"); } catch (e) { _queueCache = null; }
  host.innerHTML = queueBodyHtml();
}


/** 能力页签的事件绑定（一个页面一次，重绘不用重绑）。 */
function bindCapCards() {
  const host = $("#view-capability");
  if (!host || host.dataset.bound) return;
  host.dataset.bound = "1";
  host.addEventListener("change", async (e) => {
    const el = e.target;
    let key, val;
    if (el.dataset.mset) { key = el.dataset.mset; val = el.value; }
    else if (el.dataset.mbool) { key = el.dataset.mbool; val = el.checked; }
    else if (el.dataset.mnum) { key = el.dataset.mnum; val = parseFloat(el.value) || 0; }
    else if (el.dataset.ttsEngine) { key = "ttsEngine"; val = el.value; }
    else return;
    try {
      const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: { [key]: val } }) });
      toastSaved(r, "已更新");
      const r2 = await api("/api/settings");
      _settingsCache = r2.settings || _settingsCache;
      loadCapabilities();
    } catch (err) { toast("更新失败：" + err.message); }
  });
  host.addEventListener("click", async (e) => {
    const save = e.target.closest("[data-cap-save]");
    if (save) {
      const values = {};
      $$("#view-capability [data-key]").forEach((el) => {
        const key = el.dataset.key;
        const meta = (((_capCache.cfg || {}).settings) || []).find((x) => x.key === key);
        if (!meta) return;
        if (meta.secret) { if (el.value && el.value.trim()) values[key] = el.value; return; }
        values[key] = el.value;
      });
      if (!Object.keys(values).length) { toast("没有需要保存的改动"); return; }
      try {
        const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ values }) });
        toastSaved(r, "已保存 " + Object.keys(values).length + " 项");
        loadCapabilities();
      } catch (err) { toast("保存失败：" + err.message); }
      return;
    }
    const dl = e.target.closest("[data-msdl]");
    if (dl) {
      dl.disabled = true;
      try {
        const r = await post("/api/models/download", { id: dl.dataset.msdl, force: dl.dataset.force === "1" });
        // 被"缺依赖"拒掉时：后端给的是**整段可展示的说明**（原因 + 安装命令 + 下一步），
        // 只弹一句"缺依赖"用户不知道该装什么、也不知道装完要回来再点一次下载
        // （2026-09-25 同事实测）。所以把详情落进该模型的任务状态 —— 卡片会据此渲染
        // 「复制安装命令」按钮（data-mcopy，处理器在下面同一处）。
        if (r && r.ok === false && r.detail) {
          _modelJobsCache[dl.dataset.msdl] = { status: "failed", message: r.detail,
                                               installCommand: r.installCommand || "",
                                               local: true };
          toast(r.reason || r.message || "下载没有开始");
        } else {
          toast(r.message || "已开始下载");
        }
      } catch (err) { toast("下载失败：" + err.message); }
      loadCapabilities();
      return;
    }
    const cp = e.target.closest("[data-mcopy]");
    if (cp) {
      try { await navigator.clipboard.writeText(cp.dataset.mcopy); toast("已复制"); }
      catch (err) { toast("复制失败，请手动选择"); }
      return;
    }
    const go = e.target.closest("[data-goto]");
    if (go) { switchView(go.dataset.goto); return; }
    const rl = e.target.closest("[data-cap-reload]");
    if (rl) { loadCapabilities(); return; }
    const preset = e.target.closest("[data-preset-index]");
    if (preset) { await capApplyPreset(preset.dataset.presetIndex); return; }
    // 清理：取消「保留」钉子 / 真删（真删走 runModelCleanup，里面有确认对话框）
    const pin = e.target.closest("[data-pin-toggle]");
    if (pin) {
      const id = pin.dataset.pinToggle;
      const it = ((_cleanupView || {}).items || []).find((x) => x.id === id) || {};
      await toggleModelPin(id, !it.pinned);
      return;
    }
    if (e.target.closest("#btnCleanupRun")) { await runModelCleanup(); return; }
    if (e.target.closest("#btnCleanupReload")) { await loadCleanupCard(); return; }
  });
}

/** 读取"用哪个实现"需要的三份数据（provider 清单 / 在线服务字段 / 预设）。
 *
 *  能力页签与「模型路由」页签（语言模型那一块）共用同一份缓存 —— 两处各拉一遍不仅浪费，
 *  还会出现"一处改了另一处还是旧状态"。`force=true` 时强制刷新。 */
async function ensureProviderData(force) {
  if (!force && _capCache.prov) return _capCache;
  const [provRes, cfgRes, presetRes] = await Promise.all([
    api("/api/providers?ready=true"),
    api("/api/providers/config").catch(() => ({ settings: [] })),
    api("/api/providers/presets").catch(() => ({ presets: [] })),
  ]);
  _capCache = Object.assign({}, _capCache, { prov: provRes, cfg: cfgRes, presets: presetRes });
  return _capCache;
}

/** 语言模型那块（渲染在「模型路由」页签里）：用哪个实现 + 在线服务地址/模型/密钥。 */
async function loadRouterLlm() {
  const host = $("#rtLlmHost");
  if (!host) return;
  try {
    await ensureProviderData(true);
    await ensureSettings(true);
    host.innerHTML = capProviderBlock("llm");
    bindRouterLlm(host);
  } catch (e) {
    host.innerHTML = `<div class="muted" style="font-size:12px">读取失败：${esc(e.message)}</div>`;
  }
}

/** 语言模型块的事件绑定（一次性，重绘不用重绑）。 */
function bindRouterLlm(host) {
  if (host.dataset.bound) return;
  host.dataset.bound = "1";
  host.addEventListener("click", async (e) => {
    const save = e.target.closest("[data-cap-save]");
    if (!save) return;
    const values = {};
    $$("#rtLlmHost [data-key]").forEach((el) => {
      const key = el.dataset.key;
      const meta = (((_capCache.cfg || {}).settings) || []).find((x) => x.key === key);
      if (!meta) return;
      if (meta.secret) { if (el.value && el.value.trim()) values[key] = el.value; return; }
      values[key] = el.value;
    });
    if (!Object.keys(values).length) { toast("没有需要保存的改动"); return; }
    try {
      const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ values }) });
      toastSaved(r, "已保存 " + Object.keys(values).length + " 项");
      loadRouterLlm();
    } catch (err) { toast("保存失败：" + err.message); }
  });
}

/** 某个"用哪个实现"选择变化后，刷新**当前正在看的**那一页。 */
function refreshAfterProviderChange() {
  const tab = $(".tab.active");
  const view = tab && tab.dataset ? normView(tab.dataset.view) : "";
  // 「能力与智能体」一页里既有语言模型块（路由卡）也有能力/本地能力块，两处一起刷
  if (view === "capability") { loadRouter(); loadCapabilities(); return; }
  loadSettings();
}

/* 选 provider：立即写配置（与"命令目标"下拉同一种交互：选中即持久化）。
   TTS 不走这里：它的开关是 ttsEngine（见 bindCapCards 的 data-tts-engine）。 */
document.addEventListener("change", async (e) => {
  const kind = e.target && e.target.dataset ? e.target.dataset.providerKind : "";
  if (!kind) return;
  const key = "provider" + kind.charAt(0).toUpperCase() + kind.slice(1);
  try {
    const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: { [key]: e.target.value } }) });
    toastSaved(r, "已切换到：" + e.target.value);
    const r2 = await api("/api/settings");
    _settingsCache = r2.settings || _settingsCache;
    refreshAfterProviderChange();
  } catch (err) { toast("切换失败：" + err.message); }
});

/** 在线服务预设：填进字段（不直接保存，让用户过一眼再点保存）；未选中在线实现时先切过去。 */
async function capApplyPreset(idx) {
  const all = ((_capCache.presets || {}).presets) || [];
  const p = all[Number(idx)];
  if (!p) return;
  const kind = p.kind === "asr" ? "asr" : "llm";
  const prefix = kind === "asr" ? "providerAsr" : "providerLlm";
  const onlineId = kind === "asr" ? "openai-asr" : "openai-llm";
  // 两处字段落点：语言模型块在「模型路由」卡（`#rtLlmHost`），语音转写块在「本地能力」区
  // （`#view-capability`）。按当前页签找 —— 2026-09-26 起这两块同在「能力与智能体」一页。
  const scope = ($(".tab.active") || {}).dataset?.view === "capability"
    ? (kind === "llm" ? "#rtLlmHost" : "#view-capability") : "#view-capability";
  const fieldEl = (key) => $(`${scope} [data-key="${key}"]`);
  if (!p.base_url && !p.model) { toast("这个预设需要你自己填地址（属单位内部信息）"); return; }
  if (!fieldEl(prefix + "BaseUrl")) {                 // 在线实现未选中 → 先切过去
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: { [prefix]: onlineId } }) });
    } catch (err) { toast("切换 provider 失败：" + err.message); return; }
    await refreshAfterProviderChange();
  }
  if (p.base_url && fieldEl(prefix + "BaseUrl")) fieldEl(prefix + "BaseUrl").value = p.base_url;
  if (p.model && fieldEl(prefix + "Model")) fieldEl(prefix + "Model").value = p.model;
  toast("已填入地址与模型名，请补密钥后点「保存在线服务设置」");
}

/** 读取能力页签的全部数据并渲染。失败时把模型相关设置退回设置页（唯一入口不能断）。 */
async function loadCapabilities() {
  const host = $("#capKindCards");
  if (!host) return;
  const wasOk = _capTabOk;
  try {
    const [setRes, modelsRes, compRes, sttRes, vpRes] = await Promise.all([
      api("/api/settings"),
      api("/api/models"),
      api("/api/components?includeBlocked=true"),
      api("/api/stt/status").catch(() => null),
      api("/api/voiceprints").catch(() => null),
    ]);
    await ensureProviderData(true);
    _capCache.comps = compRes;
    _settingsCache = setRes.settings || _settingsCache;
    _modelsCache = modelsRes.items || [];
    // 任务缓存：**服务端的为准**，但要保住「本地记下的下载被拒」（见 data-msdl 处理器：
    // 缺依赖时下载根本没开始，服务端不会有这个 id 的任务，整体覆盖会把那段说明和
    // 「复制安装命令」按钮一起冲掉）。规则：
    //   * 服务端报了这个 id（重试后 running/done/failed）→ 服务端覆盖本地记录；
    //   * 模型已经就绪 → 丢掉本地那条失败（别让"✅ 已就绪"旁边挂着旧失败）。
    const srvJobs = (modelsRes.jobs && modelsRes.jobs.items) || {};
    const localJobs = {};
    for (const [id, job] of Object.entries(_modelJobsCache || {})) {
      if (!job || !job.local) continue;
      const mm = _modelsCache.find((x) => x.id === id);
      if (mm && mm.ready) continue;
      localJobs[id] = job;
    }
    _modelJobsCache = Object.assign(localJobs, srvJobs);
    _sttCache = sttRes;
    _vpCache = vpRes;
    _capTabOk = true;
    renderCapOverview();
    host.innerHTML = CAP_KINDS.map(capKindCard).join("");
    const funcs = $("#capFuncCards");
    if (funcs) {
      // 唤醒 / 说话人分离 / 声纹这三张是"配置为要用的"（标配），**留在这里**；
      // `dev`（计算设备）已挪到「能力与智能体 → 设备选择」卡 —— 同一实体只有一处状态。
      const keep = ["wake", "diar", "vp"];
      const cards = modelFunctions().filter((f) => keep.indexOf(f.id) >= 0);
      funcs.innerHTML = cards.length
        ? `<div class="cap-sub">另外三件标配（唤醒 / 说话人分离 / 声纹）</div>`
          + cards.map(renderModelCard).join("")
        : "";
    }
    renderCapEnv();
    renderModelsDirRow();
    await loadCleanupCard();          // 「清理」是这一页的第三块（先预览，不改盘）
    bindCapCards();
    // GPU 后端卡（能力路由）。**排在主清单之后**：它是一次轻量请求，
    // 主清单失败也要能看到后端状态（反过来也一样，两边各自兜自己的错）。
    await loadCapabilityRouting();
    applyCollapsedCards($("#view-capability"));   // 应用上次的卡片折叠状态（动态卡片要重绘后应用）
    const active = modelsRes.jobs && modelsRes.jobs.active;
    if (active && !_capPoll) _capPoll = setInterval(loadCapabilities, 1500);
    else if (!active && _capPoll) { clearInterval(_capPoll); _capPoll = null; }
    if (!wasOk) loadSettings();        // 页签恢复：设置页里回退显示的那些项可以收起来了
  } catch (e) {
    host.innerHTML = `<div class="mcard bad">
      <div class="mcard-head"><div class="mcard-ic">⚠</div>
        <div class="mcard-title">能力清单加载失败</div>${modelBadge("不可用", "miss")}</div>
      <div class="mcard-body">
        <div class="mcard-warn">${esc(e.message)}</div>
        <div class="mcard-meta">模型/引擎相关设置已暂时回到「设置」页签，先在那儿改也可以；这里恢复后会自动收起。</div>
        <div class="mcard-act"><button type="button" class="btn mini" data-cap-reload="1">重试</button></div>
      </div></div>`;
    bindCapCards();
    _capTabOk = false;
    if (wasOk) { try { loadSettings(); } catch (_) { /* 忽略 */ } }
  }
}

const _btnCapReload = $("#btnCapReload");
if (_btnCapReload) _btnCapReload.addEventListener("click", () => loadCapabilities());
const _btnCapDlMissing = $("#btnCapDownloadMissing");
if (_btnCapDlMissing) _btnCapDlMissing.addEventListener("click", () => downloadMissingModels());


/* ================= GPU 后端（能力路由，3.0）=================

   为什么长在「能力」页签里、而不是单开一个页签：用户 2026-09-19 明确要求
   "每个能力只在这一处出现"（当时模型页签 / 组件页签 / 设置里的 provider 卡片三处
   都在讲同一件事，被合并成这一个页签）。"这个能力用哪个实现"与"用本机还是用那台
   GPU"是同一个问题的两半，再开一页就是走回那次合并之前。

   三条设计：
   1. **这里不算"会选中谁"** —— 那是 `router.plan` 的唯一职责（会议主链路用的就是它）。
      页签只显示事实：每个后端自己声明了什么、健不健康、配置选的是哪个。
      这里再算一遍，迟早出现"页签说会走 GPU、实际走了本机"。
   2. 设置行**由后端出**（`/api/capability` 的 `settings`，形状与设置页相同），
      用同一个 `renderSettingRow` 渲染 —— 那几项是 hidden，不出现在设置页。
   3. 配对失败**显示成一句人话**（后端也是这么回的），不是一串 HTTP 报错。 */
let _capRouteCache = null;

function capBackendBadge(b) {
  if (b.ready === true) return `<span class="badge online">可用</span>`;
  if (b.ready === false) return `<span class="badge offline">不可用</span>`;
  return `<span class="badge idle">未知</span>`;
}

function renderCapBackends(rows) {
  const host = $("#capBackendList");
  if (!host) return;
  if (!rows.length) { host.innerHTML = ""; return; }
  const src = { local: "这台机器", lan: "内网", wan: "公网" };
  host.innerHTML = rows.map((b) => {
    const slots = (b.slotsLabeled || []).map((s) => s.label).join("、") || "（什么都没声明）";
    const extra = b.capsError
      ? `<div class="cap-be-slots">读不到能力清单：${esc(b.capsError)}</div>` : "";
    return `<div class="cap-be-row">
      <span class="cap-be-name">${esc(b.label || b.backendId)}</span>
      ${capBackendBadge(b)}
      <span class="muted" style="font-size:12px">${esc(src[b.source] || b.source || "")}${b.serverName ? " · " + esc(b.serverName) : ""}</span>
      <div class="cap-be-slots">能做：${esc(slots)}</div>
      ${extra}
    </div>`;
  }).join("");
}

function renderCapPairState(pair) {
  const state = $("#capPairState");
  if (!state) return;
  if (!pair || !pair.paired) {
    state.textContent = "还没配对 —— 这台机器只用本机引擎。";
    return;
  }
  const token = pair.tokenFresh ? "令牌有效" : "下次调用时自动换令牌";
  state.textContent = `已配对：${pair.serverName || pair.baseUrl}（${pair.clientId}）· ${token}`;
}

async function loadCapabilityRouting(force) {
  const host = $("#capRouteSettings");
  if (!host) return;
  const badge = $("#capRouteBadge");
  try {
    const r = force
      ? await api("/api/capability/probe", { method: "POST" })
      : await api("/api/capability");
    _capRouteCache = r;
    // 这三项（转写/分离/声纹用哪个后端）是 hidden 键，值由本接口下发 —— 并进
    // `_settingsCache` 才能被 `settingByKey/settingValue` 读到（`loadCapabilities()`
    // 刚把缓存换成了 `/api/settings` 那一份，而那里**不含 hidden 键**）。
    mergeSettingsRows(r.settings || []);
    renderCapPairState(r.pair);
    renderCapBackends(r.backends || []);
    // 「已配对后端连接情况」默认收起，**真配上了自动展开一次**（见 autoExpandOnce）
    autoExpandOnce("cap-pair", !!(r.pair && r.pair.paired));
    // 配对输入框**不隐藏**：换一台后端（先解除配对、再配一次）与"第一次配对"
    // 是同一件事，藏起来只会让人找不到入口。
    const unpair = $("#btnCapUnpair");
    if (unpair) unpair.classList.toggle("hidden", !(r.pair && r.pair.paired));
    // 这一格放两样：**ECHO 后端的地址**（本机直连时用）+ **一句指路**。
    // 转写走哪条路 / 分离与声纹由谁做都收进了「业务配置 → 会议」卡（一个实体一处状态），
    // 所以这里不再摆第二套下拉 —— 那正是 2026-09-19 那次"同一个设置项两处能改"的老问题。
    host.innerHTML = renderSettingRows(settingRows(["capabilityEchoServerUrl"]))
      + `<div class="muted" style="font-size:12px;padding-top:6px">
      <b>会议转写走哪条路</b>（本机 / ECHO 后端 / 网络服务商）与<b>分离、声纹由谁做</b>都在
      「业务配置 → 会议」卡里改；「允许音频去哪」也在那张卡的「高级」里。
      这一页管<b>配对</b>与<b>后端清单</b>（配对状态 · 健康 · 刷新）。<br>
      <b>发授权（一次性配对串）与撤销客户端</b>在<b>管理员那台 ECHO 后端</b>上 ——
      客户端这侧只有"配对 / 解除配对"（忘掉本机凭据），服务端那本客户端清单不归这里管。
      排障用的两个令牌（<code>capabilityEchoServerToken</code> /
      <code>capabilityEchoServerStaticToken</code>）刻意不在这里：填了会盖过配对凭据。</div>`;
    const n = (r.backends || []).filter((b) => b.backendId !== "local").length;
    if (badge) {
      badge.textContent = n ? `${n} 个后端` : "只用本机";
      badge.className = "badge " + (n ? "online" : "idle");
    }
  } catch (e) {
    host.innerHTML = `<div class="muted" style="font-size:12px">读取失败：${esc(e.message)}</div>`;
    if (badge) { badge.textContent = "读取失败"; badge.className = "badge offline"; }
  }
}

/** 解析配对串 `echo://pair?host=…&code=…&fp=sha256:…`（设计 §7.5 ①）。
 *
 * 管理员那边给的就是**这一整串**（服务端 `--new-pairing-code` 打的），用户粘一次就够。
 * `fp=` 是防中间人的那一步：没有它，第一次连接只能 TOFU（第一次见谁信谁）。
 * 认不出的键忽略、也不猜 —— 配对是安全动作，宁可少填让人补，不可猜错。
 * 返回 null 表示"这压根不是配对串"（那就是普通地址，走老路）。 */
function parsePairString(text) {
  const m = /^echo:\/\/pair\b[^?]*\?(.*)$/i.exec(String(text || "").trim());
  if (!m) return null;
  const out = { url: "", code: "", fp: "" };
  m[1].split("&").forEach((kv) => {
    const i = kv.indexOf("=");
    if (i < 0) return;
    const k = decodeURIComponent(kv.slice(0, i)).trim().toLowerCase();
    const v = decodeURIComponent(kv.slice(i + 1).replace(/\+/g, " ")).trim();
    if (k === "host" || k === "url") out.url = v;
    else if (k === "code") out.code = v;
    else if (k === "fp" || k === "fingerprint") out.fp = v;
  });
  return out;
}

async function doCapabilityPair() {
  const raw = (($("#capPairUrl") || {}).value || "").trim();
  const boxCode = (($("#capPairCode") || {}).value || "").trim();
  const parsed = parsePairString(raw);
  const url = parsed ? parsed.url : raw;
  const code = (parsed && parsed.code) ? parsed.code : boxCode;
  const fp = parsed ? parsed.fp : "";
  const state = $("#capPairState");
  if (!url) { toast(parsed ? "配对串里没有 host —— 让管理员重发一张" : "先填后端地址"); return; }
  if (!code) { toast("先填配对码"); return; }
  if (state) state.textContent = fp ? "正在配对…（会按串里的指纹校验证书）" : "正在配对…";
  try {
    const r = await api("/api/capability/pair", {
      method: "POST",
      body: JSON.stringify({ base_url: url, code: code, fingerprint: fp }),
    });
    toast(r.message || "配对成功");
    const c = $("#capPairCode"); if (c) c.value = "";
    if (parsed) { const u = $("#capPairUrl"); if (u) u.value = url; }
    await loadCapabilityRouting(true);
  } catch (e) {
    // 400 的 body 是 {"detail": "一句人话"}（后端就是这么回的）。原样显示那条，
    // 而不是把 `HTTP 400: {...}` 糊到人脸上。
    let msg = e.message;
    const m = /^\s*HTTP \d+:\s*(\{.*\})$/s.exec(msg);
    if (m) { try { msg = JSON.parse(m[1]).detail || msg; } catch (_) { /* 原样 */ } }
    if (state) state.textContent = msg;
    toast(msg, 6000);
  }
}

async function doCapabilityUnpair() {
  if (!window.confirm("解除配对？\n\n这只是让这台机器忘掉后端凭据；"
    + "服务端那本客户端清单归管理员。再要用得让管理员重新发一张配对码。")) return;
  try {
    const r = await api("/api/capability/unpair", { method: "POST" });
    toast(r.message || "已解除配对");
    await loadCapabilityRouting(true);
  } catch (e) { toast("解除配对失败：" + e.message); }
}

const _btnCapPair = $("#btnCapPair");
if (_btnCapPair) _btnCapPair.addEventListener("click", doCapabilityPair);
const _btnCapUnpair = $("#btnCapUnpair");
if (_btnCapUnpair) _btnCapUnpair.addEventListener("click", doCapabilityUnpair);
// `#btnCapRouteSave` 不再有了：那几行设置已经收进「会议转写服务」卡，随页签顶部的「保存」落库
const _btnCapRouteProbe = $("#btnCapRouteProbe");
if (_btnCapRouteProbe) _btnCapRouteProbe.addEventListener("click", () => loadCapabilityRouting(true));



/* ---- 环境体检 + 迁移已有会议（2.0 / P1、D20、D21） ----
   只读展示四类根（ECHO/DATA/MEETINGS/MODELS）的存在、可写性与磁盘余量，并提供
   "迁移已有会议"。迁移的用法刻意设计成两步：**先改上面的「会议目录」并保存，再点迁移**——
   卡片在渲染时记下"当时的会议目录"作为 source，因为配置一旦保存，"当前目录"就已经是新值了，
   不显式传旧值的话服务端只能回答"无需迁移"（正确但不是用户想要的）。 */
async function renderEnvCheck(host) {
  host.innerHTML = `<div class="card collapsible" data-collapse-id="env-check"
      data-collapse-default="closed">
    <div class="card-title"><span class="set-arrow">▶</span><span>环境体检</span>
      <span class="sbadge" id="envCheckCount">…</span></div>
    <div class="card-body"><div id="envCheckBody" class="muted">读取中…</div></div>
  </div>`;
  let oldPath = null;                       // 渲染时的会议目录 = 迁移的源
  const post = (url, body) => fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }).then((r) => r.json());

  async function migrate() {
    const btn = $("#envMigrateBtn");
    if (btn) { btn.disabled = true; btn.textContent = "迁移中…"; }
    try {
      const dry = await post("/api/paths/migrate-meetings", { source: oldPath || "", dryRun: true });
      if (!dry.ok) { toast("无法迁移：" + (dry.error || "未知原因")); return; }
      const plan = `源：${dry.source}\n目标：${dry.target}\n将迁移 ${dry.moved.length} 个会议目录\n` +
        `跳过（目标已存在）${dry.skipped.length} 个\n不动（非会议目录）${(dry.others || []).length} 个` +
        (dry.moved.length ? `\n\n${dry.moved.slice(0, 8).join("\n")}${dry.moved.length > 8 ? "\n…" : ""}` : "");
      if (!dry.moved.length) { toast("没有需要迁移的会议目录（目标里可能已经搬过了）"); return; }
      if (!window.confirm(plan + "\n\n现在开始迁移？")) return;
      const done = await post("/api/paths/migrate-meetings", { source: oldPath || "", dryRun: false });
      if (done.ok) { toast(`已迁移 ${done.moved.length} 个会议目录`); refresh(); }
      else { toast("迁移未完成：" + (done.error || "未知原因")); }
    } catch (e) { toast("迁移失败：" + e.message); }
    finally { if (btn) { btn.disabled = false; btn.textContent = "迁移已有会议"; } }
  }

  async function refresh() {
    try {
      const d = await api("/api/paths/env");
      const rows = (d.roots || []).map((r) => `<tr>
        <td>${esc(r.name)}</td>
        <td style="word-break:break-all">${esc(r.path || "(无法解析)")}</td>
        <td>${r.exists ? "有" : "无"}</td>
        <td>${r.writable ? "可写" : "<b>不可写</b>"}</td>
        <td>${r.freeGB == null ? "-" : r.freeGB + " GB"}</td>
        <td>${esc(r.note || "")}</td></tr>`).join("");
      const meet = (d.roots || []).find((r) => r.name === "MEETINGS");
      if (oldPath === null) oldPath = meet ? (meet.path || "") : "";
      const cfg = d.configured || {};
      $("#envCheckBody").innerHTML = `
        <table class="muted" style="width:100%;font-size:12px">
          <thead><tr><th>根</th><th>路径</th><th>存在</th><th>写入</th><th>可用</th><th>备注</th></tr></thead>
          <tbody>${rows}</tbody></table>
        <div class="muted" style="margin:8px 0;font-size:12px">
          会议目录 ${d.meetingDirs} 个 · 面板端口 ${d.port || "-"} ·
          会议目录为自定义：${cfg.meetings ? "是" : "否"} · 模型目录为自定义：${cfg.models ? "是" : "否"}
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <button type="button" id="envMigrateBtn">迁移已有会议</button>
          <span class="muted" style="font-size:12px">
            用法：先把上面的「会议目录」改成新路径并**保存**，再点这里搬旧会议（先列出计划，确认后才动手）。
          </span>
        </div>`;
      $("#envCheckCount").textContent = (d.roots || []).length;
      $("#envMigrateBtn").addEventListener("click", migrate);
    } catch (e) {
      $("#envCheckBody").textContent = "读取失败：" + e.message;
    }
  }
  refresh();
}

/* ---------- 设置页 v3：数据 / 渲染 / 交互 ---------- */

/** 后端**没有**下发的三个 hidden 键（`agentBackend` / `agentCodebuddyEnabled` /
 *  `agentHarnessEnabled`）：它们的**值**来自 `/api/agents` 的 `active` 与 `configKey`/`enabled`
 *  两个派生态，元数据（标签、说明）任何接口都不给 —— `/api/settings` 只发可见项，
 *  `/api/agents` 只发各适配器 `settings_keys` 里那几条。
 *  所以这里给一份**面板侧的最小标题**：改的仍是原来的键，写回口径不变。
 *  （归属表把这 3 项归在「智能体 / 智能体」卡：见 renderAgentCardCommon/Adv。） */
const SET_META_PATCH = [
  { key: "agentBackend", grp: "agent", label: "执行智能体", value_type: "str",
    description: "ECHO 把命令与会议纪要交给哪个智能体执行。选中即启用该产品"
               + "（产品自己的开关在高级里，会自动一起打开）。" },
  { key: "agentCodebuddyEnabled", grp: "agent", label: "启用 CodeBuddy Code", value_type: "bool",
    description: "腾讯 CodeBuddy Code（WorkBuddy 内置同一引擎）的启用开关。" },
  { key: "agentHarnessEnabled", grp: "agent", label: "启用标准版 harness", value_type: "bool",
    description: "独立 DeepSeek Harness 的启用开关（不装 DSH Desktop 也能用）。" },
];

/** 把一批设置行并进 `_settingsCache`：**已有的键不覆盖**（可见行优先），缺的补上。
 *  设置页要渲染 provider / 能力 / 智能体那些 hidden 键，而它们走另外三个接口下发。 */
function mergeSettingsRows(rows) {
  (rows || []).forEach((s) => {
    if (!s || !s.key) return;
    if (!_settingsCache.some((x) => x.key === s.key)) _settingsCache.push(s);
  });
  return _settingsCache;
}
function settingValue(key, fallback = "") {
  const s = settingByKey(key);
  return s && s.value !== undefined && s.value !== null ? s.value : fallback;
}
function setMeta(key) {
  return settingByKey(key) || SET_META_PATCH.find((m) => m.key === key) || null;
}

/** `**粗体**` / `` `代码` `` 的轻量渲染（先按段落转义，杜绝插值注入）。 */
function richText(s) {
  return String(s ?? "").split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((p) => {
    if (/^\*\*[^*]+\*\*$/.test(p)) return "<b>" + esc(p.slice(2, -2)) + "</b>";
    if (/^`[^`]+`$/.test(p)) return "<code>" + esc(p.slice(1, -1)) + "</code>";
    return esc(p);
  }).join("");
}
/** 行尾的 `?` 浮窗：长说明都收在这里（不可逆/出网/生物特征那几项仍平铺，见 SET_LOUD_DESC）。 */
function sHelp(text, label) {
  if (!text) return "";
  return `<span class="sq" role="button" tabindex="0" aria-label="说明">?`
       + `<span class="spop">${richText(text)}</span></span>`;
}
/** 下拉选项的短文案（值不变，只改显示）。 */
function sOptLabel(key, label, value) {
  const m = SET_OPT_LABELS[key];
  return (m && m[value] != null) ? m[value] : label;
}
/** 高级设置区里的小节名：显式表 → 后端 `sub` → 卡片的缺省名。 */
function sAdvSection(s, card) {
  return SET_ADV_SEC[s.key] || (s.sub && SET_SUB_NAMES[s.sub]) || (card.advDefault || "其他");
}
function isPureBool(s) {
  return s && s.value_type === "bool" && !s.secret && !SET_LOUD_DESC.has(s.key);
}
function settingRows(keys) {
  return (keys || []).map((k) => settingByKey(k)).filter(Boolean);
}
/** 一批行：**连续的纯复选项 ≥2 个就一行摆两个**（用户规则②），其余按行渲染。 */
function renderSettingRows(items) {
  const out = [];
  let buf = [];
  const flush = () => {
    if (!buf.length) return;
    if (buf.length >= 2) {
      out.push(`<div class="schk2">` + buf.map((s) => schkCell(s)).join("") + `</div>`);
    } else {
      out.push(renderSettingRow(buf[0]));
    }
    buf = [];
  };
  (items || []).forEach((s) => {
    if (isPureBool(s)) { buf.push(s); return; }
    flush();
    out.push(renderSettingRow(s));
  });
  flush();
  return out.join("");
}
function schkCell(s) {
  const help = s.description ? sHelp(s.description) : "";
  return `<span class="schkcell"><label class="schk" title="${esc(s.label || s.key)}">`
       + `<input type="checkbox" data-key="${esc(s.key)}" data-setkey="${esc(s.key)}" `
       + `${s.value ? "checked" : ""}><span>${esc(sLabel(s))}</span></label>${help}</span>`;
}

/* ---- 「会议转写服务」：3 个 hidden 键 ↔ 界面上 1 个单选 ----
   2026-09-26（概念纠正）：**会议转写 = 转写 + 说话人分离 + 声纹识别，三件都是标配**
   （本地跑或走 ECHO 后端一样）。"分两次调用"是现有模型能力不足的实现细节，不是用户
   要理解的开关 —— 所以「会议产出（全部/说话人/只要文字）」那个三选一被**删掉**了：
   它让用户以为"不说话人"是一种正当选择，而真相是那场会议只是少了说话人（且没人告诉他）。

   这条单选只写 `capabilityMeetingAsrBackend`（会议转写走哪条路）。"分离/声纹**由谁做**"
   是三件独立的事，在「智能体 → 模型路由 → 会议能力通道」里按槽指定（见 `RT_CAP_KEYS`）。
   界面上一套控件只写它自己那个键 —— 不替别的槽做主（曾经"一个下拉同时写三个键"，
   表现是"选了后端也没说话人"）。 */
function capBackendValue() {
  const v = String(settingValue("capabilityMeetingAsrBackend", "echo-server") || "echo-server");
  return SET_MEETING_BACKENDS.some((b) => b.value === v) ? v : "echo-server";
}
async function saveMeetingBackend(sel) {
  try {
    const r = await api("/api/settings", { method: "PUT",
      body: JSON.stringify({ values: { capabilityMeetingAsrBackend: sel } }) });
    toastSaved(r, "会议转写走哪条路已更新");
    await loadSettings();
  } catch (e) { toast("保存失败：" + e.message); }
}

/* ---- 状态显示：**一律用后端给的字段**，面板不写死原因 ----
   2026-09-25 整合后，这一批渲染器只剩「会议转写服务」卡在用：
     * 后端可用性：/api/capability 的 backends[].ready / capsError / paired / auth；
     * 组件/模型的"装没装、缺什么、怎么装"由「能力与智能体」页原「能力」那套渲染
       （capCompTable / renderModelCard 直接用 readyReason / readyNextStep /
       installCommand / notReadyReason / nextStep —— 那是**唯一**一处）。 */
function backendRow(backendId) {
  return ((_capView || {}).backends || []).find((b) => b.backendId === backendId) || null;
}
function readyBadge(ready) {
  if (ready === true) return `<span class="sbadge ok">就绪</span>`;
  if (ready === false) return `<span class="sbadge err">不可用</span>`;
  return `<span class="sbadge">未知</span>`;
}

/** 常用参数里那种"带状态 + 动作"的一行（标签 + 控件 + 右侧按钮）。 */
function sStatusRow(label, help, ctlHtml, actsHtml) {
  return `<div class="srow"><div class="lbl"><span class="lt" title="${esc(label)}">${esc(label)}</span>`
       + `${help ? sHelp(help) : ""}</div><div class="sctl">${ctlHtml}`
       + `${actsHtml ? `<span class="sacts">${actsHtml}</span>` : ""}</div></div>`;
}

/* ---- 常规 → 服务：运行信息 + 重启（数据来自 /api/status） ---- */
function renderServiceCard() {
  const st = _statusCache || {};
  const srv = ((st.components || []).find((c) => c.name === "server")) || {};
  const meet = st.meeting || {};
  const port = String(settingValue("serverPort", ""));
  const info = [
    srv.pid ? `pid ${srv.pid}` : "",
    st.uptime ? `已运行 ${fmtUptime(st.uptime)}` : "",
    st.version ? `ECHO ${st.version}` : "",
    port ? `面板端口 ${port}` : "",
  ].filter(Boolean).join(" · ");
  const recWarn = meet.active
    ? `<div class="snote warn"><span>⚠</span><span><b>正在录音</b>（${esc(meet.folder || "会议")}）：
         现在别重启 —— 会打断这一场。等它录完再动。</span></div>`
    : `<div class="snote info"><span>ⓘ</span><span>当前没有在录音，可以安全重启。</span></div>`;
  // 端口改了要重启才生效 —— 而"现在这个页面"还是老端口，说清这一点，免得用户以为改坏了
  let portNote = "";
  try {
    if (port && location.port && String(location.port) !== port) {
      portNote = `<div class="snote warn"><span>⚠</span><span>配置里的端口（${esc(port)}）与当前页面
        （${esc(location.port)}）不一致：保存后要重启 ECHO，再用新端口打开面板。</span></div>`;
    }
  } catch (e) { /* 忽略 */ }
  return `<div class="srow"><div class="lbl"><span class="lt">运行状态</span></div>
      <div class="sctl"><span class="smono">${esc(info || "读取中…")}</span></div></div>
    ${recWarn}${portNote}
    <div class="sacts" style="padding-top:6px">
      <button class="btn danger" id="btnRestartEcho"
        title="停止并重新启动 ECHO 服务进程">重启 ECHO 服务</button>
      <button class="btn" data-scroll="#bootLogCard" title="滚到下面那张「启动日志」卡">启动日志 ↓</button>
      <span class="muted" id="restartState" style="font-size:11px"></span>
    </div>`;
}

/* ---- 语音与设备里的"引擎还没就绪"提示：**指路，不是第二份就绪清单** ----
   2026-09-25 用户要求把重复的就绪信息整合到一处（常规的启动状态 ↔ 能力后端的可装模型）。
   所以这里不再渲染就绪徽标/模型体积/缺失原因/下载/复制命令（那些只在「能力与智能体」页出现一次），
   只在**当前选中的引擎还没就绪**时冒一行警告 + 一个跳转 —— 目的是别让"选了没装的引擎"
   变成静默失败（2026-09-23 那次事故），而不是把清单抄一遍。 */
function enginePointerHtml(engineKey) {
  const engine = String(settingValue(engineKey));
  const m = modelById(engineModelId(engine));
  const ready = m ? m.ready === true : false;
  if (ready) return "";
  const what = m ? (m.name || m.id) : (engine || "（未选）");
  return `<div class="snote warn"><span>⚠</span><span>选中的「${esc(what)}」还没就绪 ——
      缺失原因、下一步与安装方式都在「能力与智能体」页。</span>
      <span class="sacts"><button class="btn" data-goto="capability">去能力与智能体 →</button></span></div>`;
}
const renderCmdEngineStatus = () => enginePointerHtml("sttModel");
/** 朗读同理：TTS 不是模型清单里的条目，看 `/api/status` 里那条组件在不在线。
 *  自己关掉（ttsEngine=off）不算"没就绪"，不打扰。 */
function renderTtsStatus() {
  if (String(settingValue("ttsEngine")) === "off") return "";
  const c = ((_statusCache || {}).components || []).find((x) => x.name === "tts");
  if (!c || c.status === "online") return "";
  return `<div class="snote warn"><span>⚠</span><span>朗读组件现在不是在线状态
      （${esc(c.detail || c.status || "")}）—— 实现与就绪情况在「能力与智能体」页的「语音合成」卡。</span>
      <span class="sacts"><button class="btn" data-goto="capability">去能力与智能体 →</button></span></div>`;
}

/* ---- 智能体卡（常用）：选中哪个 + 状态 + 家目录 ---- */
function agentByKey(key) {
  return SET_META_PATCH.find((m) => m.key === key) || { key: key, label: key };
}
function renderAgentCardCommon() {
  const cur = (_agentsCache || []).find((a) => a.active) || null;
  const opts = (_agentsCache || []).map((a) =>
    `<option value="${esc(a.name)}" ${a.active ? "selected" : ""}>${esc(a.displayName)}</option>`).join("");
  const chip = cur ? agentStatusChip(cur) : `<span class="agent-chip off">未选择</span>`;
  const acts = [`<button class="btn" data-agent-probe="${esc(cur ? cur.name : "")}"
      title="重新探测该智能体是否可用">检测</button>`];
  if (cur && cur.webUi) {
    acts.push(`<button class="btn" data-agent-web="1" title="由服务端打开它的 Web 界面（token 不下发页面）">浏览器打开</button>`);
  }
  const harness = (_agentsCache || []).find((a) => a.name === "harness") || null;
  const homeRow = (harness && (harness.settings || []).some((s) => s.key === "harnessHome"))
    ? `<div class="srow"><div class="lbl"><span class="lt" title="harness 数据目录（DSH_HOME）">家目录</span>
         ${sHelp("独立 harness 的家目录；留空 = 新部署 {echoBase}/dsh/home、老装机 {DATA}/harness。"
               + "刻意与 Desktop 的家目录分开（各用各的）：两边的会话与设置互不干扰。")}</div>
       <div class="sctl"><input class="ctl" data-agent-field="harnessHome" data-setkey="harnessHome"
         value="${esc(agentFieldValue("harnessHome"))}" placeholder="（默认）"
         title="只有「标准版 harness」用它"></div></div>`
    : "";
  return sStatusRow("执行智能体", agentByKey("agentBackend").description,
      `<select class="ctl" data-agent-select="1" data-setkey="agentBackend">${opts}</select>${chip}`,
      acts.join(""))
    + (cur && cur.reason ? `<div class="snote ${cur.available ? "info" : "warn"}"><span>${cur.available ? "ⓘ" : "⚠"}</span>`
        + `<span>${esc(cur.reason)}</span></div>` : "")
    + homeRow;
}
function agentFieldValue(key) {
  const cur = (_agentsCache || []).find((a) => a.active);
  const rows = (_agentsCache || []).flatMap((a) => a.settings || []);
  const hit = rows.find((s) => s.key === key);
  void cur;
  return _agentDirty[key] !== undefined ? _agentDirty[key] : (hit ? hit.value : "");
}

/* ---- 智能体卡（高级）：产品开关 + 当前智能体的参数 ----
   （纪要归档那一族 2026-09-26 挪到「业务配置 → 工作区与归档」：
     "纪要往哪写"是产出落点，不是"用哪个智能体"。） */
function renderAgentCardAdv() {
  const out = [];
  const switches = (_agentsCache || []).filter((a) => a.configKey);
  if (switches.length) {
    out.push(`<div class="ssec">产品开关</div>`);
    out.push(switches.map((a) => {
      const meta = agentByKey(a.configKey);
      return `<label class="schk" title="${esc(meta.description || "")}">
        <input type="checkbox" data-agent-enable="${esc(a.name)}" data-setkey="${esc(a.configKey)}"
          ${a.enabled ? "checked" : ""}><span>${esc(meta.label || a.displayName)}</span></label>`;
    }).join(""));
  }
  out.push(`<div class="ssec">当前智能体的参数</div>`);
  out.push(agentDetailHtml());
  out.push(`<div class="snote info"><span>ⓘ</span><span>DSH Desktop 2.x 由它自己的宿主进程托管，
    所以 <code>dshStartCommand</code> / <code>dshNodePath</code> / <code>dshPackageDir</code>
    这三项在配置里已标为**已过时**（任何接口都不再下发，面板也就没有它们的行）；
    要覆盖启动方式请用上面的 <code>harnessCommand</code>。</span></div>`);
  return out.join("");
}

/* 2026-09-25：原来那张「通道设置」卡（注册情况 + 通道状态 + 7 项参数）连同
   `renderChanStatus()` 一起并进了「智能体」页那张**静态**的「模型路由」卡：
   注册情况由 `#rtReg`（renderRouterHead）说，成员状态由 `#rtMembers` 说，
   7 项参数在它的「高级」里 —— 一页一张路由卡，不再各说一段。 */

/* ---- 业务配置 → 会议：**转写 + 分离 + 声纹由谁做**（4 个 hidden 键，一处改完） ----
   2026-09-26：这一段从「模型路由 → 会议能力通道」（`#rtCapHost`）挪到「会议」卡里。
   理由就是用户的原话 ——"会议（录音设备 · 分段 · 保存策略 · **转写走哪条路**）"：
   "开会时这些活谁干"是**会议**的配置，不是语言模型路由的配置。
   界面上每一行只写自己那个键（曾经"一个下拉同时写三个键"，表现是"选了后端也没说话人"）。 */
function renderMeetingServiceCard() {
  const sel = capBackendValue();
  const radios = SET_MEETING_BACKENDS.map((b) => {
    const be = backendRow(b.value);
    const badge = b.value === "asr-provider"
      ? `<span class="sbadge">未实现</span>` : readyBadge(be ? be.ready : undefined);
    return `<label class="schk" title="${esc(b.hint)}">`
         + `<input type="radio" name="setMeetingBackend" value="${esc(b.value)}" data-merge-backend="1"`
         + `${b.value === sel ? " checked" : ""}><span>${esc(b.label)}</span>${badge}</label>`;
  }).join("");
  const priv = String(settingValue("capabilityPrivacy", "lan") || "lan");
  let consistency = `<span class="sbadge ok">一致</span>`;
  if (priv === "none" && sel !== "local") {
    consistency = `<span class="sbadge warn" title="出网许可是「不出机」，而选中的后端不在本机：`
      + `调用会被策略挡住（后端会把原因报成 blocked）">许可不允许</span>`;
  } else if (priv === "lan" && sel === "asr-provider") {
    consistency = `<span class="sbadge warn" title="「内网」许可不覆盖公网服务商">许可只到内网</span>`;
  }
  const dia = String(settingValue("capabilityDiarizeBackend", "auto") || "auto");
  const diaWhere = (SET_MEETING_BACKENDS.find((b) => b.value === dia) || {}).label || "自动（按默认链挑）";
  return `<div class="ssec">转写走哪条路</div>
    <div class="sras" data-setkey="capabilityMeetingAsrBackend">
      ${radios}
      <div class="snote info" style="margin-top:4px"><span>ⓘ</span><span>
        <b>会议转写 = 转写 + 说话人分离 + 声纹识别</b>，三件一起，本地跑或走后端都一样
        （"分两次调用"只是实现细节）。这一条单选管<b>转写</b>；下面两条定
        <b>分离与声纹由谁做</b>（现在分离是 ${esc(diaWhere)}）。分离要是真跑不了，
        会议详情会明说「说话人分离未执行：&lt;原因&gt;」——不会安静地少掉说话人。</span></div>
    </div>
    <div class="srow"><div class="lbl"><span class="lt" title="许可一致性">调用许可</span>
      ${sHelp("「允许音频去哪」决定哪些后端根本不被考虑（见高级）。这里只做一致性提示。")}</div>
      <div class="sctl">${consistency}</div>
    </div>
    <div class="ssec">分离与声纹由谁做</div>
    ${renderSettingRows(settingRows(["capabilityDiarizeBackend", "capabilityEmbedBackend"]))}`;
}

/* 2026-09-25：原来的 `renderBackendCard()` / `renderEchoBackendFace()` / `renderLocalBackendFace()`
   / `renderLocalComponents()` / `renderProviderBackendFace()` 都删掉了 —— 「能力后端」**页签**
   里已经有原「能力」页签那一整块（`#capRouteCard` 的配对与后端清单、`#capKindCards` 的
   「用哪个实现 + 装没装」、`#capEnvHost` 的运行环境），它们用同一批后端字段
   （`capsError` / `readyReason` / `readyNextStep` / `installCommand`）显示真原因。
   再摆一张自绘的「后端面板」卡就是同一件事的第二个副本。 */

/* ---- 4 个页签的渲染 ---- */
function renderCard(card) {
  const body = [
    card.hint ? `<div class="sset-hint">${esc(card.hint)}</div>` : "",
    renderGroupBody(card),
  ].join("");
  const open = _sadvOpen.has(card.id) ? " open" : "";
  void open;
  return `<div class="card collapsible" id="setCard-${esc(card.id)}"
      data-collapse-id="set-${esc(card.id)}">
    <div class="card-title"><span class="set-arrow">▶</span><span>${esc(card.title)}</span>
      ${card.help ? sHelp(card.help) : ""}</div>
    <div class="card-body">${body}</div>
  </div>`;
}
/** 卡片内容 = 常用参数（常显）+ 高级设置区（默认收起；哪些项进高级**按归属表的层级列**）。 */
function renderGroupBody(card) {
  const commonHtml = typeof card.common === "function"
    ? card.common()
    : renderSettingRows(settingRows(card.common));
  const dyn = card.dynAfter ? card.dynAfter() : "";
  if (!card.adv) return commonHtml + dyn;
  const inner = typeof card.adv === "function"
    ? card.adv() : renderAdvSections(card, settingRows(card.adv));
  return commonHtml + dyn
    + `<div class="sadv${_sadvOpen.has(card.id) ? " open" : ""}">
         <button type="button" class="sadvbtn" aria-expanded="${_sadvOpen.has(card.id)}">
           <span class="set-arrow">▶</span>高级</button>
         <div class="sadvbody">${inner}</div>
       </div>`;
}
/** 高级区里的小节：只有 ≥2 个小节时才画小节标题（一节到底就别多一行头）。 */
function renderAdvSections(card, items) {
  const names = [];
  const bySec = {};
  items.forEach((s) => {
    const n = sAdvSection(s, card);
    if (!bySec[n]) { bySec[n] = []; names.push(n); }
    bySec[n].push(s);
  });
  const order = (card.advOrder || []).filter((n) => bySec[n]);
  const all = order.concat(names.filter((n) => !order.includes(n)));
  const showHead = all.length > 1;
  return all.map((n) => {
    const note = (card.advNote && card.advNote[n]) ? card.advNote[n]() : "";
    return (showHead ? `<div class="ssec">${esc(n)}</div>` : "")
         + renderSettingRows(bySec[n]) + note;
  }).join("");
}
/** 「未归类」兜底卡：`SET_CARDS` 没写到的设置项仍然会出现（新键不会静默消失）。
 *  正常情况下它应当是空的 —— 空的它不渲染。 */
function renderFallbackCards() {
  const placed = _placedKeys();
  const extra = _settingsCache.filter((s) => !s.deprecated && !placed.has(s.key));
  if (!extra.length) return "";
  const byGrp = {};
  extra.forEach((s) => { (byGrp[s.grp || "?"] = byGrp[s.grp || "?"] || []).push(s); });
  const grps = SET_GROUP_ORDER.filter((g) => byGrp[g])
    .concat(Object.keys(byGrp).filter((g) => !SET_GROUP_ORDER.includes(g)));
  return `<div class="card collapsible" data-collapse-id="set-unplaced">
    <div class="card-title"><span class="set-arrow">▶</span><span>未归类</span>
      <span class="sbadge warn">${extra.length} 项</span></div>
    <div class="card-body">
      <div class="snote warn"><span>⚠</span><span>这些设置项还没写进 <code>SET_CARDS</code> 的落点表
        （见 <code>docs/设置项归属表.md</code>）：先把它们摆出来，再去补归属，别让它们消失。</span></div>
      ${grps.map((g) => `<div class="ssec">${esc(SET_GROUP_NAMES[g] || g)}</div>`
        + renderSettingRows(byGrp[g])).join("")}
    </div></div>`;
}
/** 落点表声明覆盖的键（普通行 + 动态渲染的卡用 `covers` 显式列出 + 用说明代替控件的键）。 */
const SET_PLACED_BY_NOTE = new Set([
  "capabilityEchoServerToken", "capabilityEchoServerStaticToken",   // 排障令牌：只在说明里点名
  "allowVirtualInputDevice",                                        // 接口不下发：只在说明里点名
]);
function _placedKeys() {
  const out = new Set(SET_PLACED_BY_NOTE);
  SET_PLACED_ELSEWHERE.forEach((k) => out.add(k));
  Object.keys(SET_CARDS).forEach((tab) => (SET_CARDS[tab] || []).forEach((c) => {
    if (typeof c.common !== "function") (c.common || []).forEach((k) => out.add(k));
    if (c.adv && typeof c.adv !== "function") (c.adv || []).forEach((k) => out.add(k));
    (c.covers || []).forEach((k) => out.add(k));
  }));
  return out;
}

/** 渲染三个页签里的设置卡片（数据已在 `_settingsCache` / `_agentsCache` / `_capView` / …）。
 *
 *  2026-09-26 IA 重构后：三个 pane 分别长在 `#view-general / #view-business /
 *  #view-capability` 里，**没有子页签那一层** —— 一次 loadSettings() 把三页都画好，
 *  切页签只是显示/隐藏（数据不重拉）。
 *  会议能力通道（`#rtCapHost`）同日撤掉：转写/分离/声纹由谁做现在由
 *  `renderMeetingServiceCard()` 画在「业务配置 → 会议」里 —— 一个实体只有一处状态。 */
function renderSettingsPanes() {
  const html = {};
  SET_TABS.forEach((t) => {
    html[t.id] = (SET_CARDS[t.id] || []).map((c) => renderCard(c));
    const who = $(`#view-${t.id} .sset-who`);      // 每页顶部那句"这一页管什么"
    if (who) who.textContent = t.who || "";
  });
  html.general.push(renderFallbackCards());
  const hosts = {
    general: "#setPaneGeneral", business: "#setPaneBusiness",
    capability: "#setPaneCapability",
  };
  Object.keys(hosts).forEach((id) => {
    const host = $(hosts[id]);
    if (host) host.innerHTML = (html[id] || []).join("");
  });
  // 「模型路由」卡（index.html 里**静态**的）里的 7 项路由参数：
  // 卡本身不能由 JS 生成（它的 host 会被 loadRouter()/loadRouterLlm() 渲染，重绘会和
  // 并发请求互相覆盖），所以这里只把那些行填进它的 `#rtSetHost`；
  // 展开状态与动态卡共用 `_sadvOpen`。
  const rp = $("#rtSetHost");
  if (rp) rp.innerHTML = renderSettingRows(settingRows([...ROUTER_KEYS]));
  const radv = $("#rtMergeCard .sadv");
  if (radv) radv.classList.toggle("open", _sadvOpen.has("router"));

  // 环境体检（四类根 + 一键迁移）：不是设置项，静态 host 就在「能力与智能体」页
  // 本地能力区的末尾，每次重绘刷一遍数据（容器不重建）
  try {
    const host = $("#envCheckHost");
    if (host) renderEnvCheck(host);
  } catch (e) { /* 体检卡片失败不能拖垮设置页 */ }
  ["#view-general", "#view-business", "#view-capability"]
    .forEach((sel) => applyCollapsedCards($(sel)));
  _syncSettingsCollapseAll();
}

async function loadSettings() {
  const grab = (p) => p.catch(() => null);
  try {
    const [setRes, cfgRes, capRes, compsRes, modelsRes, rtRes, stRes] = await Promise.all([
      api("/api/settings"),
      grab(api("/api/providers/config")),
      grab(api("/api/capability")),
      // 常规页那条"组件与模型就绪 N/M"的摘要要用它（就绪清单本身只在能力后端页渲染）
      grab(api("/api/components?includeBlocked=true")),
      grab(api("/api/models")),
      grab(api("/api/router/status")),
      grab(api("/api/status")),
    ]);
    _settingsCache = (setRes && setRes.settings) || [];
    _agentsCache = (setRes && setRes.agents) || _agentsCache;
    // hidden 键的元数据：provider（/api/providers/config）、能力（/api/capability）、
    // 智能体（/api/agents 的 settings）。**值也一并进来**，于是 settingByKey() 对它们有效。
    mergeSettingsRows((cfgRes && cfgRes.settings) || []);
    mergeSettingsRows((capRes && capRes.settings) || []);
    (_agentsCache || []).forEach((a) => mergeSettingsRows(a.settings || []));
    _capView = capRes;
    _compsCache = compsRes;
    _modelsCache = (modelsRes && modelsRes.items) || [];
    _modelJobsCache = (modelsRes && modelsRes.jobs && modelsRes.jobs.items) || {};
    _routerView = rtRes;
    _statusCache = stRes;
    renderSettingsPanes();
    renderBootReadyNote();            // 摘要行用的就是 _compsCache，重绘后要跟着更新
  } catch (e) { toast("加载设置失败：" + e.message); }
}

/** 高级设置区的展开状态：会话内记住（默认收起 —— 规则③）。
 *  （原名 `toggleSetSub`，那是"二级小节折叠"；v3 里它是卡片内**高级区**的开关，与子页签无关，
 *   所以改名 `toggleSetAdv`，别和已经删掉的子页签切换逻辑混起来。） */
const _sadvOpen = new Set();
function toggleSetAdv(btn) {
  const box = btn.closest(".sadv");
  if (!box) return;
  const card = box.closest("[data-collapse-id]");
  const id = card ? card.dataset.collapseId : "";
  const nowOpen = box.classList.toggle("open");
  btn.setAttribute("aria-expanded", String(nowOpen));
  if (!id) return;
  if (nowOpen) _sadvOpen.add(id.replace(/^set-/, "")); else _sadvOpen.delete(id.replace(/^set-/, ""));
}

/* 设置区里的按钮/开关（一个委托，重绘后不用重绑；四个页签共用）：
   「▶ 高级」、复制命令、下载、去某页、页内跳转、重新探测、智能体检测/浏览器打开。 */
document.addEventListener("click", async (e) => {
  const adv = e.target.closest(".sadvbtn");
  if (adv) { toggleSetAdv(adv); return; }
  const cp = e.target.closest("[data-mcopy]");
  if (cp) {
    try { await navigator.clipboard.writeText(cp.dataset.mcopy); toast("已复制"); }
    catch (err) { toast("复制失败，请手动选择"); }
    return;
  }
  const dl = e.target.closest("[data-msdl]");
  if (dl) {
    dl.disabled = true;
    try {
      const r = await post("/api/models/download", { id: dl.dataset.msdl, force: dl.dataset.force === "1" });
      if (r && r.ok === false) {
        // 被"缺依赖"拒掉时后端给的是**整段可展示的说明**（原因 + 安装命令 + 下一步）
        _modelJobsCache[dl.dataset.msdl] = { status: "failed", message: r.detail || "",
          installCommand: r.installCommand || "", nextStep: r.nextStep || "", local: true };
        toast(r.reason || r.message || "下载没有开始");
      } else { toast(r.message || "已开始下载"); }
    } catch (err) { toast("下载失败：" + err.message); }
    await loadSettings();
    return;
  }
  const go = e.target.closest("[data-goto]");
  if (go) { switchView(go.dataset.goto); return; }
  // 页内跳转（页签整合后，同一页里上下两段互相指路用这个，不再切页签）
  const sc = e.target.closest("[data-scroll]");
  if (sc) {
    const target = document.querySelector(sc.dataset.scroll);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const probe = e.target.closest("[data-cap-probe]");
  if (probe) {
    probe.disabled = true;
    try {
      _capView = await post("/api/capability/probe", {});
      toast("已重新探测");
      renderSettingsPanes();
    } catch (err) { toast("探测失败：" + err.message); }
    return;
  }
  const web = e.target.closest("[data-agent-web]");
  if (web) {
    web.disabled = true;
    try {
      const r = await api("/api/harness/browser", { method: "POST" });
      toast(r.message || (r.ok ? "已在浏览器打开" : "打开失败"));
    } catch (err) { toast("打开失败：" + err.message); }
    finally { web.disabled = false; }
    return;
  }
});

/* 「会议转写服务」那条单选：只写 `capabilityMeetingAsrBackend`（见 saveMeetingBackend）。
   分离/声纹**由谁做**是另外两项设置，在「模型路由 → 会议能力通道」里改（`RT_CAP_KEYS`），
   不跟着这条单选走 —— 这正是"选了后端也没有说话人"那次事故的根因（一个控件替三个槽做主）。 */
document.addEventListener("change", async (e) => {
  const rb = e.target.closest("[data-merge-backend]");
  if (rb) {
    await saveMeetingBackend(rb.value);
    return;
  }
  const sel = e.target.closest("[data-agent-select]");
  if (sel) {
    /* 选中即启用：产品自带的"启用开关"（agentCodebuddyEnabled / agentHarnessEnabled）一并打开。
       否则会出现"开关已选中、状态却是未启用"的自相矛盾（2026-09-19 设置去重）。 */
    const name = sel.value;
    const cur = (_agentsCache || []).find((a) => a.active);
    if (cur && cur.name === name) return;
    const target = (_agentsCache || []).find((a) => a.name === name);
    const values = { agentBackend: name };
    if (target && target.configKey) values[target.configKey] = true;
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ values }) });
      await loadSettings();
      const now = (_agentsCache || []).find((a) => a.active);
      toast(`已切换到 ${now ? now.displayName : name}`);
    } catch (err) { toast("切换失败：" + err.message); }
    return;
  }
  const en = e.target.closest("[data-agent-enable]");
  if (en) {
    const a = (_agentsCache || []).find((x) => x.name === en.dataset.agentEnable);
    if (!a || !a.configKey) return;
    try {
      await api("/api/settings", { method: "PUT",
        body: JSON.stringify({ values: { [a.configKey]: en.checked } }) });
      await loadSettings();
      toast(`${a.displayName}：${en.checked ? "已启用" : "已停用"}`);
    } catch (err) { toast("保存失败：" + err.message); }
    return;
  }
  // 当前智能体的参数（家目录 / DSH 地址 / harness 参数 / 密钥）：先记下，随「保存」一起落库
  const field = e.target.closest("[data-agent-field]");
  if (field) {
    const cur = (_agentsCache || []).find((a) => a.active);
    const meta = cur ? (cur.settings || []).find((s) => s.key === field.dataset.agentField) : null;
    _agentDirty[field.dataset.agentField] = (meta && meta.value_type === "bool")
      ? field.checked : field.value;
  }
});

/* 展开区里的「检测」按钮 */
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-agent-probe]");
  if (!btn) return;
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "检测中…";
  const r = await loadAgents(true);
  btn.disabled = false;
  btn.textContent = old;
  if (!r) { toast("检测失败：服务未响应"); return; }
  renderSettingsPanes();
  const a = (_agentsCache || []).find((x) => x.name === btn.dataset.agentProbe);
  if (a) toast(a.available ? `${a.displayName} 可用` : `${a.displayName} 不可用：${a.reason || ""}`);
});

/* 全部折叠 / 全部展开（每页顶部那个双箭头图标按钮：方向表示点下去会发生什么，
   悬停说明也跟着变；2026-09-13 用户要求把原来的"折叠/展开"文字按钮换成双箭头）。
   2026-09-25：四个设置页签各有一条工具条，所以按钮改成按 `[data-set-collapse]` 委托，
   状态在**当前这一页**的卡片上算（折的仍是卡片，复用 toggleCollapsibleCard 那套 localStorage）。 */
function _setViewOf(el) {
  const view = el ? el.closest(".view") : null;
  return view && view.id ? view.id.replace(/^view-/, "") : "";
}
function _syncSettingsCollapseAll() {
  $$("[data-set-collapse]").forEach((btn) => {
    const view = _setViewOf(btn);
    const boxes = $$(`#view-${view} .card.collapsible`);
    const anyOpen = boxes.some((b) => !b.classList.contains("collapsed"));
    btn.classList.toggle("unfold", !anyOpen);
    const label = anyOpen ? "折叠全部卡片" : "展开全部卡片";
    btn.title = label;
    btn.setAttribute("aria-label", label);
  });
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-set-collapse]");
  if (!btn) return;
  const view = _setViewOf(btn);
  const boxes = $$(`#view-${view} .card.collapsible`);
  const anyOpen = boxes.some((b) => !b.classList.contains("collapsed"));
  const collapsed = _collapsedCards();
  boxes.forEach((b) => {
    const id = b.dataset.collapseId || b.id;
    b.classList.toggle("collapsed", anyOpen);
    b.querySelector(":scope > .card-title")?.setAttribute("aria-expanded", String(!anyOpen));
    if (anyOpen) collapsed.add(id); else collapsed.delete(id);
  });
  _saveCollapsedCards(collapsed);
  _syncSettingsCollapseAll();
});

/* ---------------- 设置 → 服务：重启 ECHO ----------------
   后端收到请求后立刻返回，真正的停/起由脱离进程组的 restart-echo.ps1 做（见 app/runtime.py）。
   这里负责：确认 → 置灰按钮 → 轮询 /api/status 等它回来（期间连接会被拒，属正常）。 */
async function waitForEchoBack(timeoutMs = 90000) {
  const t0 = Date.now();
  let down = false;
  while (Date.now() - t0 < timeoutMs) {
    try {
      const st = await api("/api/status");
      if (down || st) return st;          // 能应答即视为已恢复
    } catch (e) {
      down = true;                        // 服务正在重启：连接被拒
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  return null;
}

/* 重启按钮现在长在「常规 → 服务」卡里（卡片由 loadSettings 重绘），所以用**事件委托**
   而不是 `$("#btnRestartEcho").addEventListener` —— 后者在重绘后就失效了。 */
async function restartEcho(btn) {
  btn.blur();
  if (btn.disabled) return;
  if (!(await confirmDialog("确定重启 ECHO 服务？正在进行的录音或命令会中断。",
                            { okText: "重启", danger: true }))) return;
  btn.disabled = true;
  btn.classList.add("working");           // 复用"说话"按钮的进行态动效
  btn.textContent = "重启中…";
  const state = $("#restartState");
  try {
    const r = await post("/api/system/restart", {});
    if (!r.ok) { toast(r.message || "重启请求被拒绝"); return; }
    if (state) state.textContent = "服务重启中，等待重连…";
    toast(r.message || "正在重启…");
    const st = await waitForEchoBack();
    if (st) {
      if (state) state.textContent = "已重连（pid " + ((st.components || [])
        .filter((c) => c.name === "server")[0] || {}).pid + "）";
      toast("ECHO 已重启");
      loadSettings();                     // 拉一遍新进程的设置元数据
    } else {
      if (state) state.textContent = "90 秒内未重连，请看 data/logs/restart.log";
      toast("重启后 90 秒未重连，请查看 data\\logs\\restart.log");
    }
  } catch (err) {
    if (state) state.textContent = "请求失败：" + err.message;
    toast("重启请求失败：" + err.message);
  } finally {
    btn.disabled = false;
    btn.classList.remove("working");
    btn.textContent = "重启 ECHO 服务";
  }
}
document.addEventListener("click", (e) => {
  const btn = e.target.closest("#btnRestartEcho");
  if (btn) restartEcho(btn);
});

/* ---------------- 模型清单小工具 ---------------- */
function fmtMb(mb) {
  if (!mb) return "0 MB";
  return mb >= 1024 ? (mb / 1024).toFixed(1) + " GB" : mb + " MB";
}

/* ================= 模型功能卡（能力页签的「其他功能」区） =================
   配置来自 /api/settings（MODEL_KEYS 那批），就绪/获取来自 /api/models
   （app/modelinfo.py，含 ready/target/size/how/cmd），声纹来自 /api/voiceprints，
   已加载引擎来自 /api/stt/status。
   目的：一眼看清"每个功能用哪个模型、装没装、装在哪"，并当场切换/获取，
   避免"选了却跑不起来"（就绪会标红 + 给去下载/复制命令）。 */
let _modelsCache = [];        // /api/models items
let _modelJobsCache = {};     // /api/models → jobs.items（下载进度/失败原因）
let _vpCache = null;          // /api/voiceprints
let _sttCache = null;         // /api/stt/status
// 「能力」页签是否健康：ok 时设置页收起那些"用哪个实现"的项，加载失败时回退到设置页显示
// （否则页签一出错，界面上就再没有入口改回转写引擎/朗读实现/设备了）
let _capTabOk = true;

const _ENGINE_MODEL_ID = { sensevoice: "sensevoice", qwen3asr: "qwen3asr", sherpa: "sherpa" };

/** 引擎值 → 模型清单 id。
 *
 * 2026-09-26：whisper 各档从**候选项**里退役了（权重已从本机删除），但这一层仍然认它们 ——
 * 老库里还存着 whisper 值时（macOS 仍提供那些档），面板要能指到对应那条组件记录，
 * 而不是显示成"没有这个模型"。 */
function engineModelId(engine) {
  const e = String(engine || "").toLowerCase();
  if (_ENGINE_MODEL_ID[e]) return _ENGINE_MODEL_ID[e];
  if (["tiny", "base", "small", "medium", "large"].includes(e)) {
    return "whisper-" + (e === "large" ? "large-v3" : e);
  }
  return "";
}
function modelById(id) { return _modelsCache.find((m) => m.id === id) || null; }
function settingByKey(k) { return _settingsCache.find((s) => s.key === k) || null; }

/** 下拉里显示的人话名字（值仍是原样，避免改配置口径）。 */
function friendlyOption(key, v) {
  const s = String(v);
  if (key === "sttModel" || key === "meetingSttModel") {
    return { sensevoice: "SenseVoice 中文短命令", qwen3asr: "Qwen3-ASR 0.6B",
      sherpa: "sherpa 流式（中英）" }[s] || ("Whisper " + s);
  }
  if (key === "device") return { auto: "自动（有 GPU 就用）", cpu: "CPU", cuda: "CUDA（GPU）" }[s] || s;
  if (key === "wakeEngine") return { sherpa: "sherpa 流式识别", kws: "KWS 关键词 spotting" }[s] || s;
  if (key === "capabilityDiarizeBackend" || key === "capabilityEmbedBackend") {
    return { auto: "自动（按默认链挑）", "echo-server": "ECHO 后端", local: "本机" }[s] || s;
  }
  if (key === "capabilityMeetingAsrBackend") {
    return { "echo-server": "ECHO 后端", local: "本机", "asr-provider": "网络服务商" }[s] || s;
  }
  return s;
}

function modelBadge(text, kind) { return `<span class="mcard-badge ${kind}">${esc(text)}</span>`; }

/** 模型/组件的「获取」动作组：下载 / 复制命令 / 复制路径 / 官方链接。
 *
 *  **两档视觉层级**（2026-09-19 用户："已就绪的还用保留下载吗"）：
 *    * 未就绪 → 「下载」是**主按钮**（.btn.mini，一眼看到要做什么）；
 *    * 已就绪 → 不再摆一个显眼的下载按钮，全部降级成**小文字链接**
 *      （`重新下载 · 复制命令`，`.act-link`，灰字、悬停才亮）——
 *      已装好的行视觉上"安静"，但"重下"这条路还在（模型损坏/想刷新快照时用得上）。
 *  文字一律**短**（用户："按钮字太多"，窄边条里会被挤成竖排），完整含义进 title。
 */
function modelActions(m) {
  if (!m) return "";
  const ready = !!m.ready;
  const cls = ready ? "act-link" : "btn mini";          // 已就绪 → 文字链接；未就绪 → 按钮
  const btns = [];
  if (m.downloadable !== false && m.source !== "copy") {
    btns.push(`<button class="${cls}" data-msdl="${esc(m.id)}" data-force="${ready ? "1" : "0"}"
      title="${ready ? "重新下载（覆盖现有文件）" : "从上游下载到本机"}">${ready ? "重新下载" : "下载"}</button>`);
  }
  if (m.cmd) {
    btns.push(`<button class="${cls}" data-mcopy="${esc(m.cmd)}"
      title="${esc(m.cmd_label || "复制下载命令")}">复制命令</button>`);
  }
  if (m.source === "copy") {
    btns.push(`<button class="${cls}" data-mcopy="${esc(m.target)}"
      title="复制落地路径：${esc(m.target)}">复制路径</button>`);
  }
  for (const link of (m.links || [])) {
    if (String(link.url || "").startsWith("https://huggingface.co/"))
      btns.push(`<a class="${cls}" href="${esc(link.url)}" target="_blank" rel="noopener noreferrer"
        title="${esc(link.label || "官方页面")}">链接</a>`);
  }
  return btns.join("");
}

function _selectHtml(key, options, value) {
  return `<select class="ctl" data-mset="${esc(key)}">` +
    (options || []).map((o) => `<option value="${esc(o)}" ${String(o) === String(value) ? "selected" : ""}>` +
      `${esc(friendlyOption(key, o))}</option>`).join("") + `</select>`;
}

/** 六个功能卡片的数据模型。
 *
 * 2026-09-26（概念纠正）：「说话人分离」与「声纹」**不再是开关**——会议转写一律
 * 包含这两件事（本地或后端都一样），所以这两张卡上只有**状态与由谁做**，
 * 没有"启用"复选框：用户嫌的不是配置多，而是"要做没有信息量的决定"。
 * 声纹那张卡上唯一的开关是「改名即入库」（默认关，它往本地声纹库里写东西）。 */
function modelFunctions() {
  const stt = settingByKey("sttModel");
  const mstt = settingByKey("meetingSttModel");
  const wake = settingByKey("wakeEngine");
  const dev = settingByKey("device");
  return [
    { id: "stt", icon: "🎤", name: "命令转写", settingKey: "sttModel", options: stt && stt.options,
      value: stt && stt.value, catalogId: engineModelId(stt && stt.value) },
    { id: "mstt", icon: "📝", name: "会议转写", settingKey: "meetingSttModel", options: mstt && mstt.options,
      value: mstt && mstt.value, catalogId: engineModelId(mstt && mstt.value) },
    { id: "wake", icon: "🔔", name: "唤醒", settingKey: "wakeEngine", options: wake && wake.options,
      value: wake && wake.value, catalogId: "kws" },
    // 分离与声纹：会议标配，卡上只说"由谁做"（键 = capabilityDiarizeBackend）。
    { id: "diar", icon: "👥", name: "说话人分离", settingKey: "capabilityDiarizeBackend",
      options: ["auto", "echo-server", "local"], value: settingValue("capabilityDiarizeBackend", "auto"),
      catalogId: "pyannote", alwaysOn: true },
    { id: "vp", icon: "🧬", name: "声纹", special: "voiceprint" },
    { id: "dev", icon: "💻", name: "计算设备", settingKey: "device", options: dev && dev.options,
      value: dev && dev.value, special: "device" },
  ];
}

/* 状态：ok=就绪 / miss=红色（转写引擎缺依赖，会失败）/ warn=黄色（可选模型未装）/ idle=未启用 */
function _loadState(f) {
  if (f.special === "device") return { kind: "ok", text: "就绪" };
  if (f.special === "voiceprint") {
    // 识别是标配 → 这一格说"库里有什么"，而不是"开没开"。入库开关在卡体里单独说。
    const count = (_vpCache && Array.isArray(_vpCache.items)) ? _vpCache.items.length : 0;
    return { kind: "ok", text: count ? `${count} 位联系人` : "库为空" };
  }
  const m = modelById(f.catalogId);
  if (f.id === "diar") {
    // 分离由谁做决定"就绪"该怎么看：指定本机时看 pyannote 装没装；
    // 指定/默认走后端时看后端在不在（本机没装 pyannote 与它无关，不该报红）。
    if (String(f.value) === "local") {
      return m && m.ready ? { kind: "ok", text: "本机就绪" }
                          : { kind: "miss", text: "本机未装" };
    }
    const be = backendRow("echo-server");
    return (be && be.ready) ? { kind: "ok", text: "后端就绪" }
                            : { kind: "warn", text: "后端不可用" };
  }
  if (!m) return { kind: "idle", text: "—" };
  if (m.ready) return { kind: "ok", text: "就绪" };
  const critical = f.id === "stt" || f.id === "mstt";   // 转写引擎缺失会直接导致失败
  return critical ? { kind: "miss", text: "未就绪" } : { kind: "warn", text: "未安装" };
}


/** 单张功能卡。 */
function renderModelCard(f) {
  let badge = "", cls = "", body = "";

  if (f.special === "voiceprint") {
    const auto = !!(settingByKey("voiceprintAutoEnroll") || {}).value;
    const thr = settingByKey("voiceprintThreshold");
    const mar = settingByKey("voiceprintMargin");
    const count = (_vpCache && Array.isArray(_vpCache.items)) ? _vpCache.items.length : 0;
    badge = modelBadge(count ? `${count} 位联系人` : "库为空", "ok");
    body = `<label class="mcard-sw"><input type="checkbox" data-mbool="voiceprintAutoEnroll" ${auto ? "checked" : ""}><span>改名即入库</span></label>
      <div class="mcard-meta"><b>识别是标配</b>：库里已经有这个人，会议就会显示他的姓名
        （没有开关；库里没人时安静地什么都不做）。<br>
        这个开关只管<b>入库</b>：开着 = 在会议里给说话人改名时顺手存一份；
        关着 = 一个字都不写，想存就在会议详情「说话人管理」里逐个点「声纹入库」。</div>
      <div class="mcard-nums">
        <label>匹配阈值<input type="number" step="0.01" class="ctl" data-mnum="voiceprintThreshold" value="${esc(thr ? thr.value : 0.65)}"></label>
        <label>歧义间隔<input type="number" step="0.01" class="ctl" data-mnum="voiceprintMargin" value="${esc(mar ? mar.value : 0.05)}"></label>
      </div>
      <div class="mcard-meta">声纹库：<b>${count}</b> 位联系人 · 模板只存本机 data 目录，不出网</div>`;
  } else if (f.special === "device") {
    const stt = _sttCache || {};
    badge = modelBadge("✅ " + esc(f.value || stt.device || "—"), "ok");
    const loaded = (stt.loaded || []).map((e) => e.key || `${e.engine}:${e.model}`).join("、");
    body = _selectHtml(f.settingKey, f.options, f.value) +
      `<div class="mcard-meta">用于全部转写/分离${loaded ? " · 已加载 " + esc(loaded) : ""}` +
      `${stt.cuda ? " · CUDA 可用" : " · 本机无 CUDA"}</div>`;
  } else {
    const m = modelById(f.catalogId);
    const st = _loadState(f);
    const job = (m && _modelJobsCache[m.id]) || {};
    const running = job.status === "running";
    const failed = job.status === "failed";
    if (running) { badge = modelBadge(`下载中 ${job.percent || 0}%`, "warn"); cls = " warn"; }
    else if (st.kind === "ok") { badge = modelBadge("✅ 已就绪", "ok"); cls = " ok"; }
    else if (st.kind === "miss") { badge = modelBadge("⚠ 未就绪", "miss"); cls = " bad"; }
    else if (st.kind === "warn") { badge = modelBadge("⬇ 未安装", "warn"); cls = " warn"; }
    else { badge = modelBadge("未启用", "idle"); }
    let control = "";
    if (f.settingKey) control = _selectHtml(f.settingKey, f.options, f.value);
    // 会议标配的卡（说话人分离）**没有"启用"复选框** —— 它一定会做，这里只选由谁做；
    // 下面那句说明就是原来那个复选框的位置（用户要知道"这不再是开关"）。
    if (f.alwaysOn) {
      control += `<div class="mcard-meta"><b>会议一定会做说话人分离</b>（本地跑或走后端都一样，
        不再有开关）。这里选的是<b>由谁做</b>：「自动」按默认链挑（ECHO 后端优先），
        也可指定 ECHO 后端或本机。「本机」需要已装 pyannote；真跑不了时会议详情会明说
        「说话人分离未执行：&lt;原因&gt;」，不会安静地少掉说话人。</div>`;
    }
    // 未就绪：不能只说"会失败"，要接上后端给的**下一步**（"再点一次下载"）与安装命令 ——
    // 用户上次就卡在这一屏，以为坏了。installCommand 走 esc()，并复用已有的 data-mcopy。
    const warn = (st.kind === "miss")
      ? `<div class="mcard-warn">⚠ 所选模型未就绪，现在用它转写会失败`
        + (m && m.nextStep ? ` —— ${esc(m.nextStep)}` : "")
        + (m && m.installCommand
            ? `<div style="margin-top:6px"><button class="btn" data-mcopy="${esc(m.installCommand)}">复制安装命令</button></div>`
            : "")
        + `</div>` : "";
    // 后端的失败说明是多行的（原因 + 命令 + 下一步），必须 pre-wrap 才读得出来
    const failMsg = failed
      ? `<div class="mcard-warn" style="white-space:pre-wrap">下载失败：${esc(job.message || "未知原因")}`
        + (job.installCommand ? `<div style="margin-top:6px"><button class="btn" data-mcopy="${esc(job.installCommand)}">复制安装命令</button></div>` : "")
        + `</div>`
      : "";
    // 本地占用 + 落地路径（路径是代码约定，原样展示不翻译）
    const meta = m ? `<div class="mcard-meta">${esc(m.size)}` +
      `${m.local_mb ? `（本地 ${fmtMb(m.local_mb)}）` : ""} · 落地 <code>${esc(m.target)}</code></div>` : "";
    const how = (m && m.how) ? `<div class="mcard-meta">${esc(m.how)}</div>` : "";
    const bar = running ? `<div class="m-bar"><i style="width:${Math.max(3, job.percent || 0)}%"></i></div>` : "";
    // pyannote 这类"复制命令自行执行"的，保留可展开的完整命令（旧「设置 → 模型」有，别丢）
    const rawCmd = (m && m.cmd_label === "复制下载命令" && m.cmd)
      ? `<details class="mcard-meta"><summary>查看下载命令</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere;user-select:text">${esc(m.cmd)}</pre></details>` : "";
    body = control + warn + failMsg + meta + how + bar + rawCmd +
      `<div class="mcard-act">${modelActions(m)}</div>`;
  }

  // 可折叠（点标题）：id 用功能 id（wake/diar/vp/dev），状态与其它卡片同一份存储
  return `<div class="mcard${cls} collapsible" data-collapse-id="cap-func-${esc(f.id)}">
    <div class="mcard-head"><span class="set-arrow">▶</span><div class="mcard-ic">${f.icon}</div>
      <div class="mcard-title">${esc(f.name)}</div>${badge}</div>
    <div class="mcard-body">${body}</div>
  </div>`;
}



/** 等某个模型下载任务结束（或超时），用于「一键下载缺失」串行排队。 */
function _waitModelJob(id, timeoutMs) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    const t = setInterval(async () => {
      let done = false;
      try {
        const r = await api("/api/models");
        _modelsCache = r.items || _modelsCache;
        const job = (r.jobs && r.jobs.items || {})[id];
        done = !(r.jobs && r.jobs.active) || (job && job.status !== "running");
      } catch (e) { /* 继续等 */ }
      if (done || Date.now() - t0 > (timeoutMs || 1800000)) { clearInterval(t); resolve(); }
    }, 1500);
  });
}

async function downloadMissingModels() {
  const missing = _modelsCache.filter((m) => !m.ready && m.downloadable !== false && m.source !== "copy");
  if (!missing.length) { toast("没有需要下载的模型"); return; }
  toast(`开始下载 ${missing.length} 个模型…（可在本页看进度）`);
  for (const m of missing) {
    try { await post("/api/models/download", { id: m.id, force: false }); } catch (e) { /* 继续下一个 */ }
    await _waitModelJob(m.id);
  }
  toast("缺失模型下载完成");
  loadCapabilities();
}


/** 一行设置的**控件**（v3 紧凑样式；`opts.bare` = 只要控件，不要标签行 —— 后端面板的
 *  「会议引擎」那行把它塞进自己的一行里）。
 *
 *  控件语义与 v2 **完全一致**（`data-key` + 同一个 `value_type` 分支），因为写回走的是
 *  同一个 `PUT /api/settings`：键名、取值、掩码规则都没变，变的只是长相。 */
function sCtlHtml(s, id) {
  if (s.secret) {
    // 密钥（P5 凭据管理）：服务端在出口把它遮成空串，所以这里**必须**是密码框 + 空值，
    // 并明确"留空 = 不改"；要清空走旁边的「清除」按钮（送 __clear__ 哨兵）。
    // 否则整批保存会把空串当新值，把用户的密钥静默清掉。
    return `<input type="password" class="ctl" id="${id}" data-key="${esc(s.key)}" data-setkey="${esc(s.key)}"
        data-secret="1" value="" autocomplete="new-password"
        placeholder="${s.hasValue ? "已配置（留空 = 不改）" : "未配置"}">
      <button type="button" class="btn" data-clear-secret="${esc(s.key)}"
        title="清空这个密钥">清除</button>`;
  }
  if (s.value_type === "bool") {
    return `<label class="schk"><input type="checkbox" class="ctl" id="${id}" data-key="${esc(s.key)}"
      data-setkey="${esc(s.key)}" ${s.value ? "checked" : ""}><span>${esc(sLabel(s))}</span></label>`;
  }
  // 候选项两种形态：字符串（枚举）与 {value,label}（值给程序、名字给人看 ——
  // 例如输入设备：value 是设备名字，label 是「麦克风阵列 (Realtek) [WASAPI · 48 kHz]」）。
  // 平台声明的候选项优先（`platform_options`：macOS 的离线朗读是 say 不是 sapi）。
  const opts = optionPairs((s.platform_options && s.platform_options.length)
    ? s.platform_options : (s.options || []));
  if (opts.length) {
    return `<select class="ctl" id="${id}" data-key="${esc(s.key)}" data-setkey="${esc(s.key)}">`
      + opts.map((o) => `<option value="${esc(o.value)}" `
          + `${String(o.value) === String(s.value) ? "selected" : ""}>`
          + `${esc(sOptLabel(s.key, o.label, o.value))}</option>`).join("")
      + `</select>`;
  }
  if (s.value_type === "int" || s.value_type === "float") {
    return `<input type="number" class="ctl num" id="${id}" data-key="${esc(s.key)}"
      data-setkey="${esc(s.key)}" step="${s.value_type === "float" ? "any" : "1"}"
      value="${esc(s.value)}">`;
  }
  if (s.value_type === "list") {
    return `<input class="ctl" id="${id}" data-key="${esc(s.key)}" data-setkey="${esc(s.key)}"
      value="${esc((s.value || []).join(","))}" placeholder="逗号分隔">`;
  }
  return `<input class="ctl" id="${id}" data-key="${esc(s.key)}" data-setkey="${esc(s.key)}"
    value="${esc(s.value)}">`;
}

function renderSettingRow(s, opts = {}) {
  if (!s) return "";
  const id = "set-" + s.key;
  const ctl = sCtlHtml(s, id);
  if (opts.bare) return ctl;
  const label = sLabel(s);
  const loud = SET_LOUD_DESC.has(s.key);
  const help = s.description ? sHelp(s.description) : "";   // 长说明一律收进 `?`
  const unit = SET_UNITS[s.key] ? `<span class="sunit">${esc(SET_UNITS[s.key])}</span>` : "";
  const boolOnly = s.value_type === "bool" && !s.secret;
  const note = loud
    ? `<div class="sdesc warn">${richText(SET_LOUD_NOTE[s.key] || s.description || "")}</div>`
    : "";
  // 纯复选项：控件自己带标签（`.schk`），整行只剩控件列 —— 否则一行里会出现两遍同样的文字，
  // 左边还会空出 84px 的标签槽（边条里那是很贵的一段）
  if (boolOnly) return `<div class="srow"><div class="sctl">${ctl}${help}</div></div>` + note;
  const lbl = `<div class="lbl"><span class="lt" title="${esc(s.label || s.key)}">${esc(label)}</span>${help}</div>`;
  return `<div class="srow">${lbl}<div class="sctl">${ctl}${unit}</div></div>` + note;
}

/** 密钥「清除」：显式送哨兵值，服务端才真的清空（空串 = 不改）。 */
document.addEventListener("click", async (e) => {
  const key = e.target && e.target.dataset ? e.target.dataset.clearSecret : "";
  if (!key) return;
  if (!window.confirm("确定清空这个密钥？清空后依赖它的在线服务会不可用。")) return;
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: { [key]: "__clear__" } }) });
    toast("已清空");
    await loadSettings();
  } catch (err) { toast("清空失败：" + err.message); }
});

/* 保存（四个设置页签各有一条工具条，按钮是 `[data-set-save]`）。
   收集范围是**四个 pane 里的全部 data-key** —— 一次 loadSettings() 会把四页都渲染出来，
   所以从哪一页点保存，落库的都是同一份完整表单（与整合前"设置页一个保存按钮"行为一致）。
   **不含**「智能体 → 模型路由」那张静态卡（它有自己的「保存」，管成员 + 7 项路由参数，
   见 saveRouter）—— 一件事只有一个按钮。 */
document.addEventListener("click", async (e) => {
  if (!e.target.closest("[data-set-save]")) return;
  const values = collectSettingValues("[data-settab-pane]");
  // apiAuthEnabled 是"双刃"开关：打开后**所有**接口（含本地面板）都要 Bearer 令牌，
  // 而面板不带令牌 → 一开就连不上、也没法再关回来。所以强制二次确认（2026-09-19 设置审计）。
  const authRow = _settingsCache.find((s) => s.key === "apiAuthEnabled");
  if (authRow && !authRow.value && values.apiAuthEnabled === true) {
    const go = window.confirm("开启「API 鉴权」后，所有接口——包括本地面板——都需要 Bearer 令牌。\n"
      + "本地面板不带令牌：开启后会立即连不上，也不能再从这里关掉（只能带令牌或直接改配置库）。\n\n"
      + "只有在外网访问/手机 App 场景下才需要开启。确定开启？");
    if (!go) delete values.apiAuthEnabled;
  }
  // 智能体展开区里改过的字段（不在表单行里，单独并进来）
  Object.keys(_agentDirty).forEach((k) => { values[k] = _agentDirty[k]; });
  if (!Object.keys(values).length) { toast("没有需要保存的改动"); return; }
  try {
    const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ values }) });
    toastSaved(r, "已保存 " + Object.keys(r.updated).length + " 项");
    Object.keys(_agentDirty).forEach((k) => delete _agentDirty[k]);
    // 整页重绘：状态徽标（引擎就绪 / 后端可用性）与"改完后的取值"都要跟着变
    await loadSettings();
  } catch (e) { toast("保存失败：" + e.message); }
});

/* ================= 历史 ================= */

/* 命令 → DSH 会话：DSH 的 Web UI 没有"按会话直达"的 URL，跳不过去；改成在 ECHO 里就地看——
   标题来自 /api/dsh/targets，内容按需读 /api/commands/{id}/session（后端用签名 Cookie 走
   session/page）。
   注：这是硬限制，非 ECHO 疏漏——已对标准版 harness（0.1.5-rc.2，与桌面版共用同一套前端）
   源码复核，bundle 不解析任何 query/hash 参数（无 location.search/hash/URLSearchParams/
   sessionStorage/pathname），别再去试会话直达 URL。 */
let _sessionInfo = {};

async function loadSessionInfo() {
  try {
    const r = await api("/api/dsh/targets");
    const map = {};
    for (const s of (r.sessions || [])) map[s.sessionId] = { title: s.title || "", cwd: s.cwd || "" };
    _sessionInfo = map;
  } catch (e) { /* 拿不到就只显示 ID，不影响历史本身 */ }
  return _sessionInfo;
}

function sessionLabel(sid) {
  const info = _sessionInfo[sid];
  const title = (info && info.title) || "";
  return title ? `会话：${title}` : `会话：${String(sid || "").slice(0, 20)}…`;
}

/** 展开/收起某条命令对应的 DSH 会话内容（按需拉取，拉过就缓存）。 */
async function toggleCommandSession(cmdId) {
  const body = $(`[data-sessbody="${cmdId}"]`);
  if (!body) return;
  const link = $(`[data-sess="${cmdId}"]`);
  const opening = body.classList.contains("hidden");
  body.classList.toggle("hidden", !opening);
  if (link) link.textContent = opening ? "收起会话 ▾" : "看会话 ▸";
  if (!opening || body.dataset.loaded) return;
  body.innerHTML = `<div class="empty">读取会话中…</div>`;
  try {
    const r = await api(`/api/commands/${cmdId}/session?limit=12`);
    body.dataset.loaded = "1";
    body.innerHTML = renderSessionMessages(r);
  } catch (e) {
    body.innerHTML = `<div class="empty">读取失败：${esc(e.message)}</div>`;
  }
}

function renderSessionMessages(r) {
  if (!r.session_id) return `<div class="empty">${esc(r.message || "这条命令没有关联会话")}</div>`;
  const head = `<div class="sess-head">${esc(r.title || "(无标题会话)")}` +
    `${r.cwd ? " · " + esc(r.cwd) : ""}${r.running ? " · 正在执行" : ""}` +
    `${r.exists === false ? " · 会话已不存在（可能已归档/删除）" : ""}` +
    ` · ${r.anchored ? "已定位到这条指令" : "未定位到这条指令，显示会话最近消息"}</div>`;
  if (!r.messages.length) {
    return head + `<div class="empty">${esc(r.error || "这个会话里还没有可显示的消息")}</div>`;
  }
  const items = r.messages.map((m) => {
    const who = m.role === "user" ? "你" : "助手";
    return `<div class="sess-msg ${m.role === "user" ? "su" : "sa"}">` +
      `<span class="sess-who">${who}</span><div class="sess-text">${esc(m.text)}</div></div>`;
  }).join("");
  return head + items + `<div class="muted sess-foot">只显示最近的文本消息（工具调用/步骤已省略）</div>`;
}

async function loadHistory() {
  try {
    const [r] = await Promise.all([api("/api/commands?limit=200"), loadSessionInfo()]);
    renderCmdList($("#historyList"), r.items, true, { sessions: true });
    const id = _focusCmdId;     // 从仪表盘点过来的那条：等渲染完再定位+展开
    _focusCmdId = null;
    if (id) focusCmd(id);
  } catch (e) { toast("加载历史失败：" + e.message); }
}
$("#btnClearCmds").addEventListener("click", async () => {
  if (!(await confirmDialog("确认清空全部命令历史？", { okText: "清空", danger: true }))) return;
  try { await api("/api/commands", { method: "DELETE" }); loadHistory(); }
  catch (e) { toast("清空失败：" + e.message); }
});

/* ================= 会议 ================= */
let _meetings = [];
let _txStatus = {};   // meeting_id -> 转写进度（轮询 /api/transcribe/status）

function meetingBadgeCls(status) {
  if (status === "recording") return "active";
  if (status === "transcribing") return "transcribing";
  if (status === "error" || status === "interrupted") return "error";
  return "idle";
}

/* 会议状态文案：error 在会议语境里是「录音失败（没录到音频）」，比通用的「错误」更能说明问题。
   `imported`（2026-09-25 新增）= 「待转写」：**音频已经在库里、还没有文字**。
   为什么单独加这一档而不是复用别的：
     * `transcribed` 会谎称已完成（库里一行都没有）；
     * `interrupted` 的意思是"录音中断"（进程崩了/设备掉了），用它表达"导进来还没转"
       会让两种完全不同的状况在列表里长得一模一样（同事的夹具脚本当初只能这么办，
       并在交付文档里写明了那是妥协）；
     * `error` 会被读成"录音失败"，而导入的音频明明在。 */
const MEETING_STATUS_TEXT = { recording: "录音中", transcribing: "转写中", transcribed: "已转写",
  imported: "待转写", error: "录音失败", interrupted: "已中断" };

function renderMeetingItems(el, items) {
  if (!items.length) {
    el.innerHTML = `<div class="empty">暂无会议记录</div>`;
    return;
  }
  el.innerHTML = items.map((m) => {
    const started = m.started_at ? m.started_at.replace("T", " ").slice(0, 16) : "";
    const tx = _txStatus[m.id];
    let txHtml = "";
    if (m.status === "transcribing") {
      const pct = tx ? (tx.percent || 0) : 3;
      const info = tx ? `${tx.detail || ""}` : "准备中…";
      txHtml = `<div class="tx-bar"><i style="width:${pct}%"></i></div>
        <div class="tx-info">${esc(info)}</div>`;
    }
    const shortTitle = (m.title || "").trim();
    const dur = Math.round(m.duration_seconds || 0);
    // 已自动命名（转写+纪要生成后写入 title）就只显示正式名称：
    // 开始时的时间戳文件名（m.name）只是目录标识，不再是"会议名称"，
    // 显示出来反而重复——开始时间已经在下面的 m-meta 里了（2026-09-12 起）。
    const nameHtml = `<div class="m-name">${esc(shortTitle || m.name)}</div>`;
    const hasSummary = m.has_summary ? `<span class="m-summary-tag" title="已生成会议纪要">📄</span>` : "";
    // 录音/转写失败（PR #11 起会明确标 error）：显示**后端记下的真实原因**。
    //
    // 2026-09-25 改：原来这里是一句**硬编码**的"没录到音频：麦克风没打开（被占用/权限）
    // 或全程无声…"。真机上因此踩过一次：录到了 1 段音频、0 行文字（真因是会议链路的
    // 本机引擎驱动不了 sherpa），界面却把排查方向指向麦克风，白折腾半天。
    // 现在原因由后端落库（`meetings.error`，`GET /api/meetings` 与详情都带），面板只负责显示；
    // 后端没记下原因时也要说人话，**不许再默认成"麦克风没打开"**。
    const errText = String((m && m.error) || "").trim();
    const errHint = m.status === "error"
      ? `<div class="m-meta" style="color:var(--red)">${esc(errText || "这场会议失败了，但原因没有记下来 —— 详见 启动 → 日志")}</div>`
      : "";
    // 「已压缩」标记（2026-09-26，历史音频无损压成 FLAC）。
    // 数字**全部来自后端**（`meta.json` 里的真实前后字节），面板一个数都不自己算：
    // 估算值只在压缩前的预览里出现，两个数混在一起用户就分不清"预览"与"实况"了。
    const cp = m.compression;
    const compTag = (cp && cp.beforeBytes)
      ? `<span class="m-compress" title="音频已无损压缩为 FLAC${cp.deletedRaw ? "（原始 WAV 已删除）" : "（按「保留原始音频」设置未删原件）"}">已压缩：原 ${esc(cp.beforeText)} → 现 ${esc(cp.afterText)}（省 ${cp.savedPercent}%）</span>`
      : "";
    // `已压缩` 也进 m-meta 那一行（列表窄的时候不至于撑宽卡片）
    const compMeta = compTag ? `<div class="m-meta">${compTag}</div>` : "";
    return `<div class="meeting-item" data-id="${m.id}">
      <span class="badge ${meetingBadgeCls(m.status)}">${MEETING_STATUS_TEXT[m.status] || STATUS_TEXT[m.status] || m.status}</span>
      <div class="grow">
        ${nameHtml}
        <div class="m-meta">${esc(started)} · ${fmtHM(dur)} · ${m.segments || 0} 段 ${hasSummary}</div>
        ${compMeta}
        ${txHtml}
        ${errHint}
      </div>
    </div>`;
  }).join("");
  $$(".meeting-item", el).forEach((it) =>
    it.addEventListener("click", () => openMeetingDetail(parseInt(it.dataset.id, 10))));
}

async function pollTranscribe() {
  try {
    _txStatus = await api("/api/transcribe/status");
  } catch (e) { return; }
  const v = $(".tab.active");
  if (!v) return;
  if (v.dataset.view === "meetings") {
    const r = await api("/api/meetings?limit=100");
    renderMeetingItems($("#meetingList"), r.items);
    refreshMeetingHeader();          // 停留在会议列表页时按钮也要跟着录音状态变
  } else if (v.dataset.view === "dashboard") {
    const r = await api("/api/meetings?limit=5");
    renderMeetingItems($("#recentMeetings"), r.items);
  }
}

async function loadMeetings() {
  try {
    const r = await api("/api/meetings?limit=100");
    _meetings = r.items;
    renderMeetingItems($("#meetingList"), r.items);
  } catch (e) { toast("加载会议失败：" + e.message); }
}

/* ================= 会议详情（独立窗口） ================= */
function openMeetingDetail(id) {
  window.open(`/web/meeting.html?id=${id}`, "_blank");
}

/* 会议列表页的录音按钮：必须跟随录音状态（用户 2026-09-12 反馈——开始录音后点"全部 ›"
   进会议列表，按钮还显示"开始录音"，点了只会重复调 start）。 */
function renderMeetingListBtn(active) {
  const btn = $("#btnMeetingFromList");
  if (!btn) return;
  btn.dataset.active = active ? "1" : "0";
  btn.textContent = active ? "停止录音" : "开始录音";
  btn.className = "btn" + (active ? " danger" : "");
  btn.title = active ? "停止录音并开始转写" : "开始会议录音";
}

/** 只读轻量状态（/api/meeting/status），用于列表页按钮与标题跟随录音状态。 */
async function refreshMeetingHeader() {
  try {
    const st = await api("/api/meeting/status");
    renderMeetingListBtn(!!st.active);
  } catch (e) { /* 面板/服务可能正在重连，忽略 */ }
}

let _meetingListBusy = false;
$("#btnMeetingFromList").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.blur();
  if (_meetingListBusy) return;
  const active = btn.dataset.active === "1";
  if (active && !(await confirmDialog("确定要停止录音并开始转写吗？", { okText: "停止并转写", danger: true }))) return;
  _meetingListBusy = true;
  try {
    const r = await post(active ? "/api/meeting/stop" : "/api/meeting/start", {});
    toast(r.message);
    await refreshMeetingHeader();
    loadMeetings();
  } catch (e2) { toast("失败：" + e2.message); }
  finally { setTimeout(() => { _meetingListBusy = false; }, 800); }
});

/* 清理 2 分钟以内的短会议（含音频文件） */
$("#btnCleanShort").addEventListener("click", async (e) => {
  e.currentTarget.blur();
  if (!(await confirmDialog("删除 2 分钟以内的会议录音（含音频与转写文件）？此操作不可恢复。",
                            { okText: "删除", danger: true }))) return;
  try {
    const r = await post("/api/meetings/clean-short", { max_minutes: 2 });
    toast(r.count ? `已清理 ${r.count} 个短会议` : "没有 2 分钟以内的会议");
    loadMeetings();
  } catch (e2) { toast("清理失败：" + e2.message); }
});

/* ================= 历史音频无损压缩（FLAC）=================
   需求（2026-09-26）：历史会议的音频段无损压成 FLAC（省约一半磁盘、转写文本逐字不变）。
   面板这一块只做三件事，**一个数字都不自己算**：

     ① 点「压缩音频」→ 先算给你看（`GET /api/meetings/compress/preview`，只读）；
     ② 用户确认 → `POST /api/meetings/compress`（后台跑）；
     ③ 压缩中显示进度（复用 `.tx-bar`，与上传/导入同一套视觉）→ 完成后刷新列表，
        卡片上出现「已压缩：原 X → 现 Y（省 Z%）」（数字来自后端 `meta.json`）。

   为什么预览与执行是两个端点：**先算给你看**是需求里点名的一步，
   合并成一个的话用户点下去就已经在压了，没有"看清数字再决定"的机会。 */
let _compressBusy = false;
let _compressPoll = null;
let _compressPreview = null;

function fmtSavedBytes(n) { return fmtSize(Math.max(0, Number(n) || 0)); }

function renderCompressPreview(p) {
  _compressPreview = p;
  const host = $("#compressPreview");
  const note = $("#compressNote");
  const picked = $("#compressPicked");
  if (host) {
    if (!p || !p.count) {
      host.textContent = p && p.alreadyMeetings
        ? `没有可压缩的会议（已有 ${p.alreadyMeetings} 场压缩过）`
        : "没有可压缩的会议";
    } else {
      const keep = p.keepRawAudio
        ? `能少占 ${p.estimateText}（保留原件，实际腾出 0）`
        : `预计省 ${p.reclaimText}`;
      host.textContent = `可压缩 ${p.count} 场 · 原 ${p.beforeText} → 约 ${p.estimateText}`;
      host.textContent += ` · ${keep}`;
    }
  }
  if (note) note.textContent = (p && p.note) || "";
  if (picked) {
    const rows = ((p && p.meetings) || []).filter((m) => m.compressible || m.compressed);
    picked.innerHTML = rows.slice(0, 30).map((m) => {
      if (m.compressed && m.compressedBytes) {
        return `<div class="ip-item">
          <span class="ip-name" title="${esc(m.title || m.name)}">${esc(m.title || m.name)}</span>
          <span class="ip-size">已压缩：原 ${esc(m.compressedBytes.beforeText || fmtSize(m.compressedBytes.before))} → 现 ${esc(m.compressedBytes.afterText || fmtSize(m.compressedBytes.after))}（省 ${m.compressedBytes.percent}%）</span>
        </div>`;
      }
      return `<div class="ip-item">
        <span class="ip-name" title="${esc(m.title || m.name)}">${esc(m.title || m.name)}</span>
        <span class="ip-size">${m.compressible} 段 · ${fmtSize(m.beforeBytes)} → 约 ${fmtSize(m.estimateBytes)}</span>
      </div>`;
    }).join("");
    if (rows.length > 30) {
      picked.innerHTML += `<div class="ip-item"><span class="ip-size">…另有 ${rows.length - 30} 场</span></div>`;
    }
  }
  const btn = $("#btnCompressStart");
  if (btn) btn.disabled = _compressBusy || !(p && p.count);
}

async function loadCompressPreview() {
  try {
    renderCompressPreview(await api("/api/meetings/compress/preview"));
  } catch (e) {
    const host = $("#compressPreview");
    if (host) host.textContent = "算不出来：" + e.message;
  }
}

/** 压缩进度条（复用 `.tx-bar`）+ 完成后的一次性收尾。 */
function setCompressBar(percent, text) {
  const bar = $("#compressBar");
  const state = $("#compressState");
  if (bar) {
    bar.classList.remove("hidden");
    bar.querySelector("i").style.width = Math.max(0, Math.min(100, percent || 0)) + "%";
  }
  if (state) state.textContent = text || "";
}

function hideCompressBar() {
  const bar = $("#compressBar");
  if (bar) { bar.classList.add("hidden"); bar.querySelector("i").style.width = "0%"; }
}

function stopCompressPoll() {
  if (_compressPoll) { clearInterval(_compressPoll); _compressPoll = null; }
}

/** 轮询压缩进度；跑完自动收尾（刷新预览 + 会议列表，让「已压缩」标记出现）。 */
function startCompressPoll() {
  stopCompressPoll();
  const tick = async () => {
    let st = null;
    try { st = await api("/api/meetings/compress/status"); } catch (e) { return; }
    const p = (st && st.progress) || {};
    if (st && st.running) {
      setCompressBar(p.percent || 0, p.detail || p.phase || "压缩中…");
      return;
    }
    // 跑完：显示汇总，刷列表（「已压缩」标记就来自列表接口）
    stopCompressPoll();
    _compressBusy = false;
    const last = (st && st.last) || null;
    setCompressBar(100, (last && last.message) || "压缩完成");
    const btn = $("#btnCompressStart");
    if (btn) btn.disabled = false;
    toast((last && last.message) || "压缩完成", 6000);
    await loadMeetings();
    await loadCompressPreview();
    setTimeout(hideCompressBar, 4000);
  };
  _compressPoll = setInterval(tick, 1500);
  tick();
}

async function runCompress() {
  if (_compressBusy) return;
  const p = _compressPreview;
  const keep = p && p.keepRawAudio;
  const lines = p && p.count
    ? [`可压缩 ${p.count} 场；原 ${p.beforeText} → 约 ${p.estimateText}。`,
       keep ? "当前设置「保留原始音频」为开：**只压缩、不删原件**（不会真正腾出空间）。"
            : "压缩后会删除原始 WAV（读回校验通过才删）。",
       "压缩过程中请勿关闭 ECHO。"]
    : ["没有可压缩的会议。"];
  if (!p || !p.count) { toast("没有可压缩的会议"); return; }
  if (!(await confirmDialog(lines.join("\n"), { okText: "开始压缩" }))) return;
  _compressBusy = true;
  const btn = $("#btnCompressStart");
  if (btn) btn.disabled = true;
  try {
    const r = await post("/api/meetings/compress", {});
    toast(r.message);
    if (!r.ok) { _compressBusy = false; if (btn) btn.disabled = false; return; }
    setCompressBar(0, "已开始…");
    startCompressPoll();
  } catch (e) {
    _compressBusy = false;
    if (btn) btn.disabled = false;
    toast("压缩失败：" + e.message);
  }
}

if ($("#btnCompressToggle")) {
  /** 展开/收起压缩面板；展开时**总是重算一遍**（数字必须是最新的）。
   *
   * `urlFlag` 为真时允许 `?compress=1` 直接展开 —— 与 `?view=` 同一套写法，
   * 用来做无头截图与"把入口链接发给别人"。 */
  async function openCompressBox() {
    const box = $("#compressBox");
    if (!box) return;
    box.classList.remove("hidden");
    const host = $("#compressPreview");
    if (host) host.textContent = "正在计算…";
    await loadCompressPreview();
  }
  $("#btnCompressToggle").addEventListener("click", async (e) => {
    e.currentTarget.blur();
    const box = $("#compressBox");
    if (!box) return;
    box.classList.toggle("hidden");
    if (box.classList.contains("hidden")) { stopCompressPoll(); return; }
    await openCompressBox();
  });
  $("#btnCompressStart").addEventListener("click", (e) => { e.currentTarget.blur(); runCompress(); });
  $("#btnCompressCancel").addEventListener("click", (e) => {
    e.currentTarget.blur();
    if (_compressBusy) { toast("正在压缩，请等它结束（压缩是后台任务，随时可以看进度）"); return; }
    hideCompressBar();
    $("#compressBox").classList.add("hidden");
  });
  // 两件**打开就要接上**的事（刷新页面 / 从别处跳过来）：
  //   ① 已经有一个压缩任务在跑 → 进度条自己接上；
  //   ② URL 带 `?compress=1` → 直接展开（无头截图与分享链接都用它）。
  const _autoOpen = /(?:^|[?&])compress=1(?:&|$)/.test(location.search);
  api("/api/meetings/compress/status").then((st) => {
    const running = !!(st && st.running);
    if (running) _compressBusy = true;
    if (running || _autoOpen) {
      $("#compressBox").classList.remove("hidden");
      if (running) startCompressPoll();
      else loadCompressPreview();
    }
  }).catch(() => {
    if (_autoOpen) openCompressBox();
  });
}

/* ================= 导入录音（成为一场会议）=================
   后端：`POST /api/meetings/import`（multipart，字段 files[] / title / start / notes）。
   这一块只做三件事：
     ① 选文件 —— **可多选，列表里显示顺序**（顺序就是分段顺序，所以顺序要能改）；
     ② 上传 —— 用 XHR，因为只有 XHR 才有上传进度（fetch 没有 upload 进度事件）；
     ③ 导入成功后**什么都不用新造** —— 会议列表的 `pollTranscribe` 会自己把这个 id
        的转写进度画出来（后端导入时就把状态切成 `transcribing` 了）。 */
let _importPicked = [];        // 已选文件（**顺序即分段顺序**）
let _importBusy = false;

function fmtSize(n) {
  const b = Number(n) || 0;
  if (b >= 1024 * 1024) return (b / 1024 / 1024).toFixed(1) + " MB";
  if (b >= 1024) return Math.round(b / 1024) + " KB";
  return b + " B";
}

function renderImportPicked() {
  const host = $("#importPicked");
  if (!host) return;
  host.innerHTML = _importPicked.map((f, i) => `
    <div class="ip-item" data-i="${i}">
      <span class="ip-idx">${i + 1}.</span>
      <span class="ip-name" title="${esc(f.name)}">${esc(f.name)}</span>
      <span class="ip-size">${fmtSize(f.size)}</span>
      <button class="btn" data-act="up" title="上移一段"${i === 0 ? " disabled" : ""}>↑</button>
      <button class="btn" data-act="down" title="下移一段"${i === _importPicked.length - 1 ? " disabled" : ""}>↓</button>
      <button class="btn danger" data-act="del" title="从列表里去掉">移除</button>
    </div>`).join("");
  $$("#importPicked .ip-item").forEach((row) => {
    row.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn || btn.disabled) return;
      const i = parseInt(row.dataset.i, 10);
      const act = btn.dataset.act;
      if (act === "del") _importPicked.splice(i, 1);
      if (act === "up" && i > 0) {
        [_importPicked[i - 1], _importPicked[i]] = [_importPicked[i], _importPicked[i - 1]];
      }
      if (act === "down" && i < _importPicked.length - 1) {
        [_importPicked[i + 1], _importPicked[i]] = [_importPicked[i], _importPicked[i + 1]];
      }
      renderImportPicked();
    });
  });
  const start = $("#btnImportStart");
  if (start) start.disabled = _importBusy || !_importPicked.length;
  const state = $("#importState");
  if (state && !_importBusy) {
    state.textContent = _importPicked.length
      ? `已选 ${_importPicked.length} 个文件，将按上面的顺序变成第 1..${_importPicked.length} 段`
      : "";
  }
}

/** 上传进度条：复用会议卡片那条 `.tx-bar`（不新造进度 UI）。 */
function setImportBar(percent, text) {
  const bar = $("#importBar");
  const state = $("#importState");
  if (bar) {
    bar.classList.remove("hidden");
    bar.querySelector("i").style.width = Math.max(0, Math.min(100, percent)) + "%";
  }
  if (state) state.textContent = text || "";
}

function hideImportBar() {
  const bar = $("#importBar");
  if (bar) { bar.classList.add("hidden"); bar.querySelector("i").style.width = "0%"; }
}

/** 把失败原因**原样**显示出来：后端给的是给人看的一句话（接口 400 的 `detail`）。 */
function importErrorText(xhr) {
  let detail = "";
  try { detail = (JSON.parse(xhr.responseText) || {}).detail || ""; } catch (e) { detail = ""; }
  if (!detail) detail = (xhr.responseText || "").trim().slice(0, 300);
  if (!detail) detail = `HTTP ${xhr.status}`;
  return detail;
}

function runImport() {
  if (_importBusy) return Promise.resolve();
  if (!_importPicked.length) { toast("请先选择录音文件"); return Promise.resolve(); }
  _importBusy = true;
  const start = $("#btnImportStart");
  if (start) start.disabled = true;
  setImportBar(0, "准备上传…");

  const fd = new FormData();
  // **顺序就是分段顺序**：FormData 的 append 顺序 = 后端收到的顺序
  _importPicked.forEach((f) => fd.append("files", f, f.name));
  const title = ($("#importTitle") || {}).value || "";
  const startAt = ($("#importStart") || {}).value || "";
  const notes = ($("#importNotes") || {}).value || "";
  if (title.trim()) fd.append("title", title.trim());
  if (startAt.trim()) fd.append("start", startAt.trim());
  if (notes.trim()) fd.append("notes", notes.trim());

  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/meetings/import");
    xhr.upload.onprogress = (ev) => {
      if (!ev.lengthComputable) { setImportBar(5, "正在上传…"); return; }
      const pct = Math.round(ev.loaded / ev.total * 100);
      setImportBar(pct, `正在上传 ${pct}%（${fmtSize(ev.loaded)} / ${fmtSize(ev.total)}）`);
    };
    xhr.upload.onload = () => setImportBar(100, "上传完成，正在解码/转成 16 kHz 单声道…");
    xhr.onerror = () => {
      _importBusy = false;
      hideImportBar();
      if (start) start.disabled = false;
      toast("导入失败：网络中断（ECHO 可能正在重启）", 6000);
      resolve();
    };
    xhr.onload = () => {
      _importBusy = false;
      if (start) start.disabled = false;
      let body = {};
      try { body = JSON.parse(xhr.responseText) || {}; } catch (e) { body = {}; }
      if (xhr.status !== 200 || body.ok === false) {
        // **显示后端给的真原因**（本项目硬规矩：界面不许自己编原因）
        const why = importErrorText(xhr);
        setImportBar(0, "导入失败：" + why);
        toast("导入失败：" + why, 9000);
        resolve();
        return;
      }
      hideImportBar();
      _importPicked = [];
      const files = $("#importFiles");
      if (files) files.value = "";
      renderImportPicked();
      const st = $("#importState");
      if (st) st.textContent = body.message || "已导入，正在转写";
      toast(body.message || "已导入，正在转写", 5000);
      // 导入后**自动进入既有转写进度显示**：`pollTranscribe` 每 2 秒拉一次
      // `/api/transcribe/status` + `/api/meetings`，这一场已经是 `transcribing`，
      // 进度条会自己出现（不新造一套进度 UI）。
      loadMeetings();
      resolve();
    };
    xhr.send(fd);
  });
}

if ($("#btnImportToggle")) {
  $("#btnImportToggle").addEventListener("click", (e) => {
    e.currentTarget.blur();
    const box = $("#importBox");
    if (!box) return;
    box.classList.toggle("hidden");
    if (!box.classList.contains("hidden")) {
      const hint = $("#importHint");
      if (hint) hint.textContent = "可多选，顺序即分段顺序";
      renderImportPicked();
    }
  });
  $("#importFiles").addEventListener("change", (e) => {
    // 追加而不是替换：用户可能分几次选（第一次挑手机录的、第二次挑会议室的）
    const add = [...(e.target.files || [])];
    const seen = new Set(_importPicked.map((f) => f.name + ":" + f.size + ":" + f.lastModified));
    add.forEach((f) => {
      const key = f.name + ":" + f.size + ":" + f.lastModified;
      if (!seen.has(key)) { _importPicked.push(f); seen.add(key); }
    });
    e.target.value = "";       // 清掉 input 的值：再选同一批文件也要能触发 change
    renderImportPicked();
  });
  $("#btnImportStart").addEventListener("click", (e) => { e.currentTarget.blur(); runImport(); });
  $("#btnImportCancel").addEventListener("click", (e) => {
    e.currentTarget.blur();
    if (_importBusy) { toast("正在导入，请等它结束（上传中取消会留下半场会）"); return; }
    _importPicked = [];
    const files = $("#importFiles");
    if (files) files.value = "";
    renderImportPicked();
    hideImportBar();
    $("#importBox").classList.add("hidden");
  });
}

/* ================= 启动状态（常规页；只讲"这台机器自己的运行态"）=================
   2026-09-25 整合（用户："常规里面的启动状态，语音转写里面的可装模型那个区域内容比较重叠，
   整合到一起"）：**组件/模型"装没装 / 缺什么 / 怎么装"只在「能力与智能体」页渲染一处**，
   这一块从此不再展开那份就绪清单（就绪徽标 / 缺失原因 / 下载 / 复制安装命令都搬走），
   只留：
     * 一句启动摘要（`#bootSummary`，/api/boot/status 的 summary）+ 一行"组件就绪 N/M"的
       **指路**（按钮切到「能力与智能体」页）；
     * 只有这里有、且是运维信息的两样东西：**启动失败的真原因**（`error`/`detail`）与
       **组件进程的启停**（`can_start`/`can_stop` → /api/boot/component/{id}/{start|stop}）；
     * （下文同页）启动日志 `#bootLogs`。
   顺带删掉的是这一块里**重复的设置编辑入口**：`sttModel` / `meetingSttModel` 两个下拉与
   `ttsEngine` 的在线开关 —— 它们各自的家在「语音与设备」（命令采集 / 任务反馈）与
   「能力后端」（语音转写 / 语音合成卡）。 */

function bootBadgeCls(status) {
  if (status === "online" || status === "active") return "online";
  if (status === "failed") return "error";
  if (status === "starting" || status === "running") return "running";
  if (status === "disabled") return "disabled";
  // 「未使用（你选了另一个智能体）」用 idle 徽章：这不是故障，别染成红的
  if (status === "skipped") return "idle";
  if (status === "idle") return "idle";
  return "idle";
}

async function loadBoot() {
  try {
    const [bs, st] = await Promise.all([api("/api/boot/status"), api("/api/status")]);
    renderBoot(bs);
    renderLiveStatus(st);            // 当前状态（启动日志标题右侧）
  } catch (e) {
    const sum = $("#bootSummary");
    if (sum) sum.textContent = "加载失败：" + e.message;
    renderLiveStatus(null);
  }
}

/** 组件/模型的"就绪 + 装没装"汇总：**与「能力与智能体」页顶部那条概览用同一个判据**
 *  （`applicable` 且 `ready === true`），免得两处报出不一样的数字。 */
function compsTally(items) {
  const usable = (items || []).filter((c) => c.applicable);
  return { ready: usable.filter((c) => c.ready === true).length, total: usable.length };
}

/** 「常规 → 启动状态」里那一行摘要 + 跳转（**不展开清单**：清单只在「能力与智能体」页）。
 *  数据来自 `/api/components`（`_compsCache`，loadSettings 取的），与能力页同一份判据。 */
function renderBootReadyNote() {
  const note = $("#bootReadyNote");
  if (!note) return;
  const t = _compsCache ? compsTally(_compsCache.items) : null;
  let line = "读取中…";
  if (t) {
    if (!t.total) {
      line = `后端没给出可判定的组件`;
    } else if (t.ready >= t.total) {
      line = `组件与模型<span class="sbadge ok">全部就绪 ${t.ready}/${t.total}</span>`;
    } else {
      line = `组件与模型<span class="sbadge warn">还有 ${t.total - t.ready} 项没就绪</span>`
           + `<span class="muted">（共 ${t.total} 项）</span>`;
    }
  }
  note.innerHTML = `<div class="srow"><div class="lbl"><span class="lt">组件与模型</span>
      ${sHelp("装没装 / 缺什么 / 怎么装（下载、复制安装命令）都在「能力与智能体」页一处看全 —— "
            + "这一页只报个数，不重复展开那份清单。")}</div>
    <div class="sctl"><span class="smono">${line}</span>
      <span class="sacts"><button class="btn" data-goto="capability"
        title="缺失原因、下一步与下载/复制安装命令都在那一页">去能力与智能体 →</button></span>
    </div></div>
    <div class="snote info"><span>ⓘ</span><span>这一页只管<b>这台机器自己的运行态</b>：
      启动摘要、组件进程启停、启动日志。组件与模型的就绪清单在「能力与智能体」页。</span></div>`;
}

/** 组件进程那几行的**短名**（标签列只有 84px，后端给的 `label` 动辄 8~12 个字会被省略号切：
 *  「命令转写引擎（常驻）」「模型路由（ECHO AUTO）」「标准版 harness」…）。
 *  完整名字仍在 `title` 里 —— 只改显示，不改任何值。 */
const BOOT_SHORT_LABELS = {
  server: "面板服务", dsh: "DSH 引擎", failover: "模型路由", harness: "harness",
  "stt-cmd": "命令转写", "stt-meeting": "会议转写", tts: "语音合成",
  wake: "语音唤醒", hotkey: "热键", meeting: "会议录音", diarize: "说话人分离",
};

function renderBoot(bs) {
  const s = bs.summary || {};
  const sum = $("#bootSummary");
  // 「启动 N/M」是**启动阶段**的服务计数（与下面"组件与模型"那个安装口径不是一回事，
  // 所以标题这句写清是"启动"，免得两个数字看着打架）
  if (sum) sum.textContent = `启动 ${s.ready}/${s.total} · 失败 ${s.failed} · 进行中 ${s.running}`;

  // ① 一行摘要 + 跳转：就绪清单在「能力与智能体」页，这里只报个数并指路
  renderBootReadyNote();

  // ② 只有这里有：启动失败的真原因（后端给的 error/detail）+ 组件进程的启停
  const ops = $("#bootOps");
  if (!ops) return;
  const rows = [];
  const list = (bs.components || []).filter((c) => c.can_start || c.can_stop || c.status === "failed");
  if (!list.length) {
    ops.innerHTML = `<div class="sset-hint">没有可启停的组件（后端没给 can_start/can_stop）。</div>`;
    return;
  }
  ops.innerHTML = `<div class="ssec">组件进程（启停）</div>` + list.map((c) => {
    const cls = bootBadgeCls(c.status);
    const btns = [];
    if (c.can_start) {
      btns.push(`<button class="btn" data-boot="${esc(c.id)}:start">`
        + `${c.status === "failed" ? "重试" : "启动"}</button>`);
    }
    if (c.can_stop) btns.push(`<button class="btn danger" data-boot="${esc(c.id)}:stop">停止</button>`);
    // 失败才把原因摊开（这是"启动失败原因"，只有这一页有）；跑得正常的那些把 detail 收到
    // title 里 —— 既不在页面上重复一遍状态文案，信息也没丢
    const why = c.status === "failed"
      ? `<div class="snote err"><span>⚠</span><span>${richText(c.error || c.detail || "")}</span></div>`
      : "";
    const short = BOOT_SHORT_LABELS[c.id] || c.label || c.id;
    return `<div class="srow" title="${esc(c.detail || "")}">
      <div class="lbl"><span class="lt" title="${esc(c.label || c.id)}">${c.icon || ""} ${esc(short)}</span></div>
      <div class="sctl"><span class="badge ${cls}">${STATUS_TEXT[c.status] || c.status || ""}</span>
        <span class="sacts">${btns.join("")}</span></div></div>${why}`;
  }).join("");
  $$("#bootOps [data-boot]").forEach((btn) => btn.addEventListener("click", async (e) => {
    const [cid, act] = e.currentTarget.dataset.boot.split(":");
    try {
      const r = await post(`/api/boot/component/${cid}/${act}`, {});
      toast(r.message);
      loadBoot();
    } catch (err) { toast("操作失败：" + err.message); }
  }));
}

async function loadBootLogs() {
  try {
    const r = await api("/api/logs?source=boot&limit=80");
    const el = $("#bootLogs");
    // DB 已按 id DESC（最新在前），直接渲染即为倒序
    const items = r.items || [];
    el.innerHTML = items.length
      ? items.map((l) => `<div class="boot-log-line"><span class="muted">${esc(l.ts || "")}</span> <span class="lv-${l.level}">${esc(l.message || "")}</span></div>`).join("")
      : `<div class="empty">暂无启动日志</div>`;
    el.scrollTop = 0;   // 最新在顶部，停在顶部查看
  } catch (e) { /* ignore */ }
}

// PWA：注册 Service Worker（可安装为独立窗口应用）
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
initRouterUI();
/* 折叠条（rail.html）点「模型」时经同源 localStorage 传来的落地页签意图；
   主面板展开会重新加载本页，所以在这里消费一次即清掉。 */
/* ================= 首装向导（D23/D24；设计见 docs/向导-分步设计.md）=================
   两条规矩（改这段代码前请先读设计文档 §0/§1）：

   1. **两相分离**：前 9 步只做「选」——只读体检 + 写自己的计划文件（/api/wizard/plan）。
      点「开始准备」才进执行相（/api/wizard/execute），之后只有进度、重试、跳过。
   2. **读者是没用过 ECHO、没有技术背景的人**：每步都说清「这是干什么的 / 不装会怎样 /
      你要决定什么」，技术词首次出现就地用一句白话解释（术语对照表见设计文档 §0.3）。
      体积一律带人话换算；按钮主位永远是「按建议装上」。
   默认值尽量替用户选好（设计 §0.4）：转写方式默认本机最轻那档；AI 服务**刻意没有默认**
   （那是用户自备的，ECHO 不提供也不代管）。 */
let _wizEnv = null, _wizComps = [], _wizBuilt = null, _wizStep = 0, _wizTimer = null;
let _wizChoices = wizBlankChoices();

function wizBlankChoices() {
  return {
    locations: { models: "", meetings: "", notes: "" },
    engines: [], wake: false, diarize: false, accel: false,
    asrOnline: false, llmReady: false, agent: "", fallback: [],
    /* S6：纪要默认走**智能体**；`llm.direct` 才是"直连兜底"的显式开关（不推荐）。
       填了地址**不等于**启用 —— 见 wizRenderLlm 的说明。 */
    llm: { direct: false, baseUrl: "", apiKey: "", model: "" },
  };
}

/* 体积的人话换算（设计 §3：精确数字降为次行小字）。1 首歌 ≈ 1 MB，与设计文档口径一致。 */
function wizSize(mb) {
  const n = Number(mb) || 0;
  if (!n) return "很小，不占地方";
  if (n >= 2000) return `约 ${(n / 1024).toFixed(1)} GB`;
  if (n >= 100) return `约 ${n} MB（差不多 ${Math.round(n)} 首歌）`;
  return `约 ${n} MB`;
}

/* 确认页要列出「将写入哪些设置」，但**密钥类不能明文显示**：面板可能开着共享屏幕，
   截图/肩窥都算泄漏。判据看键名后缀 —— 与 provider 声明的键名（…ApiKey / …Token）一致。
   2026-09-20：S6 支持就地填地址与密钥之后，密钥才第一次走到这个列表上。 */
function wizMaskSetting(key, value) {
  const text = String(value == null ? "" : value);
  if (!text) return text;
  return /(ApiKey|Token|Secret|Password)$/i.test(String(key || "")) ? "••••••（已隐藏）" : text;
}

/* 三处位置（设计 §2 的 S1）。文案里的"为什么"必须留着 —— 小白靠它理解自己在决定什么。 */
const WIZ_LOCATIONS = [
  { key: "models", label: "模型文件",
    hint: "转写方式、唤醒词这些要额外下载的东西放这里。它最占地方，建议放在空间多的盘。" },
  { key: "meetings", label: "会议文件",
    hint: "录音和文字稿。里面是原始录音，注意隐私；会随使用慢慢变大。" },
  { key: "notes", label: "笔记库",
    hint: "纪要归档到哪里 —— 通常是你已经在用的笔记库。不填也能用，只是纪要留在 ECHO 里。" },
];

const WIZ_STEPS = [
  { id: "env", name: "检查你的电脑", render: wizRenderEnv },
  { id: "where", name: "东西放在哪", render: wizRenderWhere },
  { id: "stt", name: "把录音变成文字", render: wizRenderStt },
  { id: "wake", name: "喊一声就开始", render: wizRenderWake },
  { id: "diarize", name: "分清谁在说话", render: wizRenderDiarize },
  { id: "accel", name: "用显卡加速", render: wizRenderAccel },
  { id: "llm", name: "让 ECHO 会写纪要", render: wizRenderLlm },
  { id: "agent", name: "让 ECHO 能动手", render: wizRenderAgent },
  { id: "offline", name: "断网也能用", render: wizRenderOffline },
  { id: "confirm", name: "确认一下", render: wizRenderConfirm },
  { id: "run", name: "正在准备", render: wizRenderRun },
  { id: "done", name: "你的 ECHO 现在能做什么", render: wizRenderDone },
];

function wizComp(id) { return _wizComps.find((c) => c.id === id) || null; }
function wizByKind(kind) { return _wizComps.filter((c) => c.kind === kind); }

async function loadWizard() {
  const host = $("#wizHost");
  if (!host) return;
  host.innerHTML = `<div class="empty">正在检查这台电脑…</div>`;
  try {
    const [env, comps, plan] = await Promise.all([
      api("/api/wizard/env"),
      api("/api/components?includeBlocked=true"),
      api("/api/wizard/plan"),
    ]);
    _wizEnv = env;
    _wizComps = comps.items || [];
    _wizChoices = wizBlankChoices();
    const saved = (plan.plan && plan.plan.choices) || {};
    Object.assign(_wizChoices, saved);
    _wizChoices.locations = Object.assign({ models: "", meetings: "", notes: "" },
                                           saved.locations || {});
    if (!Array.isArray(_wizChoices.engines)) _wizChoices.engines = [];
    if (!Array.isArray(_wizChoices.fallback)) _wizChoices.fallback = [];
    /* S6 的直连兜底字段：老计划文件里没有 llm 这一块，补成完整骨架再渲染 */
    _wizChoices.llm = Object.assign({ direct: false, baseUrl: "", apiKey: "", model: "" },
                                    saved.llm || {});
    if (plan.plan && plan.plan.state === "running") _wizStep = WIZ_STEPS.findIndex((s) => s.id === "run");
    wizRender();
  } catch (e) {
    host.innerHTML = `<div class="empty">检查失败：${esc(e.message)}</div>`;
  }
}

/* 计划条：随时看得见取舍的代价（设计 §3）。这是"逐步确认"能成立的前提。 */
function wizTally() {
  const c = _wizChoices;
  const ids = [];
  (c.engines || []).forEach((id) => ids.push(id));
  if (c.wake) ids.push("wake-kws");
  if (c.diarize) ids.push("diarize-pyannote");
  if (c.accel) ids.push("accel-cuda");
  (c.fallback || []).forEach((id) => ids.push(id));
  let mb = 0;
  ids.forEach((id) => { const it = wizComp(id); if (it) mb += Number(it.size_mb || 0); });
  if (c.asrOnline) mb = Math.max(0, mb);          // 在线转写不占本机
  const disk = (_wizEnv && _wizEnv.locations || []).find((l) => l.key === "models") || {};
  const free = disk.freeGB == null ? "—" : `${disk.freeGB} GB`;
  const parts = [`已选 ${ids.length} 项`, `合计 ${wizSize(mb)}`, `模型文件盘剩余 ${free}`];
  if (disk.freeGB != null && mb / 1024 > disk.freeGB) parts.push("⚠ 空间可能不够");
  return parts.join(" · ");
}

function wizStepNav() {
  return `<div class="wiz-nav">${WIZ_STEPS.map((s, i) => {
    const cls = i === _wizStep ? "cur" : (i < _wizStep ? "done" : "");
    return `<button class="wiz-dot ${cls}" data-wizgoto="${i}"
              title="${esc(s.name)}"><span>${i + 1}</span>${esc(s.name)}</button>`;
  }).join("")}</div>`;
}

function wizRender() {
  const host = $("#wizHost");
  if (!host) return;
  const step = WIZ_STEPS[_wizStep] || WIZ_STEPS[0];
  host.innerHTML = wizStepNav() + `<div class="wiz-body" id="wizBody"></div>` +
    `<div class="wiz-foot"><span class="wiz-plan">${esc(wizTally())}</span>
       <span class="spacer"></span>
       <button class="btn" id="wizBack" ${_wizStep === 0 ? "disabled" : ""}>上一步</button>
       <button class="btn" id="wizNext">下一步</button></div>`;
  step.render($("#wizBody"));
  $$("[data-wizgoto]", host).forEach((b) => b.addEventListener("click", () => {
    _wizStep = Number(b.dataset.wizgoto);
    wizRender();
  }));
  const back = $("#wizBack");
  if (back) back.addEventListener("click", () => { _wizStep = Math.max(0, _wizStep - 1); wizRender(); });
  const next = $("#wizNext");
  if (next) next.addEventListener("click", () => { _wizStep = Math.min(WIZ_STEPS.length - 1, _wizStep + 1); wizRender(); });
}

/* 每一步的骨架（设计 §3 的三段式）：这是干什么的 / 不装会怎样 / 你要决定什么。 */
function wizCard(title, what, lose, body) {
  return `<div class="card wiz-card">
    <div class="card-title">${esc(title)}
      <span class="spacer"></span>
      <button class="link wiz-why" data-wizwhy="1">这是什么？</button>
    </div>
    <div class="card-body">
      <div class="wiz-what">${what}</div>
      ${lose ? `<div class="wiz-lose">不装会怎样：${lose}</div>` : ""}
      <div class="wiz-why-body hidden">${what}<div class="muted">${
        "这一步只决定「要不要装」。真正开始下载在最后一步 —— 你可以随时返回改动，什么都不会提前落地。"}</div></div>
      <div class="wiz-opts">${body}</div>
    </div>
  </div>`;
}

/* 一个选项行：勾选 + 人话体积 + 要求 + 不满足的原因（禁用而非隐藏，D24）。 */
function wizOption(id, checked, opts) {
  const o = opts || {};
  const item = wizComp(id) || {};
  const blocked = !!item.blockedReason;
  const size = o.sizeMb != null ? o.sizeMb : item.size_mb;
  return `<label class="wiz-opt ${checked ? "on" : ""} ${blocked ? "blocked" : ""}">
    <input type="${o.multi ? "checkbox" : "radio"}" name="${esc(o.name || "wizpick")}"
           value="${esc(id)}" ${checked ? "checked" : ""} ${blocked ? "disabled" : ""}
           data-wizpick="${esc(id)}">
    <span class="wiz-opt-body">
      <span class="wiz-opt-name">${esc(o.label || item.name || id)}${o.recommend ? ' <span class="badge done">建议</span>' : ""}</span>
      <span class="muted">${esc(wizSize(size))}${o.note ? " · " + esc(o.note) : ""}</span>
      ${blocked ? `<span class="wiz-blocked">用不了：${esc(item.blockedReason)}</span>` : ""}
    </span>
  </label>`;
}

function wizBindPicks(host, onChange) {
  $$("[data-wizpick]", host).forEach((el) => el.addEventListener("change", () => {
    onChange(el);
    wizSaveChoices();
    const bar = $(".wiz-plan");
    if (bar) bar.textContent = wizTally();
    $$(".wiz-opt", host).forEach((row) => {
      const inp = row.querySelector("input");
      row.classList.toggle("on", !!(inp && inp.checked));
    });
  }));
}

async function wizSaveChoices(state) {
  try {
    await api("/api/wizard/plan", {
      method: "PUT",
      body: JSON.stringify({ plan: { state: state || "draft", choices: _wizChoices } }),
    });
  } catch (e) { /* 存不上不打断用户，最后一步会再存一次 */ }
}

/* ---------------- S0 检查你的电脑 ---------------- */
function wizRenderEnv(host) {
  const env = _wizEnv || {};
  const gpu = env.gpu || {};
  const rows = [];
  rows.push(`<div class="wiz-env-row"><b>系统</b><span>${esc(env.platformName || env.platform || "—")} ${esc(env.osVersion || "")}</span></div>`);
  rows.push(`<div class="wiz-env-row"><b>显卡</b><span>${
    gpu.vramMb ? esc(`${gpu.name}（显卡内存约 ${(gpu.vramMb / 1024).toFixed(1)} GB）→ 可以让转写更快`)
               : "没检测到独立显卡 —— 不影响使用，只是转写慢一些"}</span></div>`);
  const net = env.network || {};
  rows.push(`<div class="wiz-env-row"><b>网络</b><span>${esc(net.verdict || "—")}</span></div>`);
  const audio = env.audio || {};
  rows.push(`<div class="wiz-env-row"><b>麦克风</b><span>${
    audio.inputs ? `检测到 ${audio.inputs} 个输入设备 ✓` : "没检测到麦克风 —— 录音功能会受影响"}</span></div>`);
  const blocked = env.blocked || [];
  const blockedHtml = blocked.length
    ? `<div class="wiz-lose">先要解决：${blocked.map((b) => esc(`${b.label}（${b.note || "不可写"}）`)).join("、")}</div>`
    : "";
  host.innerHTML = wizCard("检查你的电脑",
    "先看清这台电脑有什么、缺什么，后面每一步的建议都按它来。这一步不会下载任何东西。",
    "", blockedHtml + `<div class="wiz-env">${rows.join("")}</div>
      <div class="muted">这些结论只用来给建议，不会替你决定。</div>`);
  wizWhy(host);
}

function wizWhy(host) {
  $$("[data-wizwhy]", host).forEach((b) => b.addEventListener("click", () => {
    const card = b.closest(".wiz-card");
    const body = card && card.querySelector(".wiz-why-body");
    const main = card && card.querySelector(".wiz-what");
    if (body) body.classList.toggle("hidden");
    if (main) main.classList.toggle("hidden");
    b.textContent = body && body.classList.contains("hidden") ? "这是什么？" : "收起说明";
  }));
}

/* ---------------- S1 东西放在哪 ---------------- */
function wizRenderWhere(host) {
  const disks = {};
  (_wizEnv && _wizEnv.locations || []).forEach((l) => { disks[l.key] = l; });
  const rows = WIZ_LOCATIONS.map((loc) => {
    const d = disks[loc.key] || {};
    const val = _wizChoices.locations[loc.key] || "";
    const state = !val ? "还没设置"
      : (d.writable ? `✓ 可以写 · 这个盘还剩 ${d.freeGB == null ? "—" : d.freeGB + " GB"}`
                    : `✗ ${d.note || "不可写"}`);
    return `<div class="wiz-loc">
      <div class="wiz-opt-name">${esc(loc.label)}</div>
      <div class="muted">${esc(loc.hint)}</div>
      <input class="input wiz-loc-input" data-wizloc="${esc(loc.key)}"
             placeholder="${esc(d.path || "（用默认位置）")}" value="${esc(val)}">
      <div class="muted">${esc(state)}</div>
    </div>`;
  }).join("");
  host.innerHTML = wizCard("东西放在哪",
    "ECHO 会产生三类文件：<b>模型文件</b>（要下载的东西）、<b>会议文件</b>（录音和文字稿）、<b>笔记库</b>（纪要归档的地方）。" +
    "它们可以放在不同的盘 —— 留空就用默认位置。",
    "模型文件盘空间不够会在下载到一半时失败；不指笔记库，纪要就只能留在 ECHO 里。",
    rows + `<div class="muted">路径里的中文字符有时会让转写引擎出问题，建议用纯英文目录。</div>`);
  wizWhy(host);
  $$("[data-wizloc]", host).forEach((inp) => inp.addEventListener("change", () => {
    _wizChoices.locations[inp.dataset.wizloc] = inp.value.trim();
    wizSaveChoices();
  }));
}

/* ---------------- S2 把录音变成文字 ---------------- */
function wizRenderStt(host) {
  const rec = (_wizEnv && _wizEnv.recommend) || {};
  const engine = rec.engine || "stt-sherpa";
  const list = wizByKind("stt");
  const body = list.map((c) => wizOption(c.id, (_wizChoices.engines || []).includes(c.id),
    { multi: true, name: "wizstt", recommend: c.id === "stt-sherpa" || c.id === engine })).join("")
    + wizOption("__online__", _wizChoices.asrOnline,
        { label: "把录音发给在线服务转成文字（不占本机空间）", sizeMb: 0, name: "wizonline",
          note: "需要联网，录音内容会发给那家服务" });
  host.innerHTML = wizCard("把录音变成文字",
    "开完会、说完话，把录音交给 ECHO，它自动变成带标点的文字稿。ECHO 的「会议纪要」和「语音指令」都建立在它之上。",
    "录音不会变成文字 —— 会议纪要和「发指令」都用不了（其它功能不受影响）。",
    body + `<div class="muted">可以多选：日常口述用快的，正式会议用准的。不确定就按建议，只装第一个。</div>`);
  wizWhy(host);
  wizBindPicks(host, (el) => {
    const id = el.dataset.wizpick;
    if (id === "__online__") {
      _wizChoices.asrOnline = el.checked;
      if (el.checked) { _wizChoices.engines = []; wizRender(); }
      return;
    }
    const set = new Set(_wizChoices.engines || []);
    if (el.checked) set.add(id); else set.delete(id);
    _wizChoices.engines = [...set];
    if (el.checked) _wizChoices.asrOnline = false;
  });
}

/* ---------------- S3 唤醒词 ---------------- */
function wizRenderWake(host) {
  const c = wizComp("wake-kws") || {};
  host.innerHTML = wizCard("喊一声就开始",
    "装了它，你可以直接喊一句话开始录音，不用先按快捷键。",
    "只能用快捷键或面板按钮开始录音（对很多人来说这没什么不方便）。",
    wizOption("wake-kws", !!_wizChoices.wake, { label: "喊一声就开始", label2: "" }) +
    `<div class="wiz-lose">要留意：它需要一直听着麦克风，有隐私成本 —— 所以默认不装。</div>`);
  wizWhy(host);
  wizBindPicks(host, (el) => { _wizChoices.wake = true; });
  $$("[data-wizpick='wake-kws']", host).forEach((el) => el.addEventListener("change", () => {
    _wizChoices.wake = el.checked;
  }));
}

/* ---------------- S4 说话人分离 ---------------- */
function wizRenderDiarize(host) {
  host.innerHTML = wizCard("分清谁在说话",
    "开会时自动区分每个人 —— 文字稿里会写成「张三：…」「李四：…」。",
    "会议记录里分不出谁在说，只有一整段文字。",
    wizOption("diarize-pyannote", !!_wizChoices.diarize, { label: "自动区分谁在说话" }) +
    `<div class="wiz-lose">它需要你先在一个外部站点同意使用条款并拿一份授权，所以 ECHO 不能替你下载。</div>`);
  wizWhy(host);
  $$("[data-wizpick='diarize-pyannote']", host).forEach((el) => el.addEventListener("change", () => {
    _wizChoices.diarize = el.checked; wizSaveChoices();
  }));
}

/* ---------------- S5 显卡加速（条件步） ---------------- */
function wizRenderAccel(host) {
  const rec = (_wizEnv && _wizEnv.recommend) || {};
  if (_wizChoices.asrOnline || !rec.showAccel) {
    host.innerHTML = wizCard("用显卡加速", "这一步用不上。", "",
      `<div class="muted">${esc(rec.accelReason || (_wizChoices.asrOnline
        ? "你选了在线转写 —— 转写不在本机跑，不需要显卡加速"
        : "没检测到独立显卡"))}。</div>`);
    wizWhy(host);
    return;
  }
  host.innerHTML = wizCard("用显卡加速",
    "如果这台电脑有独立显卡，可以让转写快好几倍。",
    "能用，只是慢一些（大约 5–10 倍）。",
    wizOption("accel-cuda", !!_wizChoices.accel, { label: "让转写跑在显卡上" }) +
    `<div class="muted">${esc(rec.accelReason || "")}。它要额外下载几个 GB，而且必须按显卡驱动版本装 —— 面板不代装，会给一条可粘贴的命令。</div>`);
  wizWhy(host);
  $$("[data-wizpick='accel-cuda']", host).forEach((el) => el.addEventListener("change", () => {
    _wizChoices.accel = el.checked; wizSaveChoices();
  }));
}

/* ---------------- S6 让 ECHO 会写纪要（默认走智能体） ----------------
   2026-09-20 用户定调：会议纪要、归档、语音指令**都走智能体**（只有它有 skill 机制做
   灵活扩展），**不推荐**直连大模型。所以这一步的主线是"把智能体准备起来"，入口指向第 7 步；
   直连 AI 服务收进折叠区当兜底。

   为什么不能"填了地址就自动算启用"：`providerLlm` 是个**全局**开关（别的功能也读它），
   而且纪要在 `meeting.direct_llm_decision()` 里是 **agent-first** —— 有智能体就走智能体，
   直连只在"没有智能体"时生效。向导不该因为用户随手填了地址，就替他选定"没有智能体时
   用哪个直连 provider"。 */
function wizRenderLlm(host) {
  const agents = (_wizEnv && _wizEnv.agents) || {};
  const harness = agents.harness || {}, desk = agents.dsh || {};
  const ready = !!(harness.online || desk.online);
  const llm = _wizChoices.llm || {};
  const direct = !!llm.direct;
  host.innerHTML = wizCard("让 ECHO 会写纪要",
    "开完会，ECHO 把转写稿整理成带结论和待办的会议纪要。",
    "不能自动生成会议纪要（转写、录音、会议列表都照常可用）。",
    `<div class="wiz-lose">这件事由<b>智能体</b>来做 —— 而且不只是写纪要：把纪要归档进你的
       笔记库、按你的话去整理文件，也都走它。智能体用「技能」扩展，以后想加新玩法不用改 ECHO。</div>
     <div class="wiz-env-row"><b>智能体</b><span>${ready
        ? "已经就绪 ✓ 现在就写得了纪要"
        : "还没准备 —— 准备好就能自动写纪要，也能归档、听指令"}</span></div>
     <div><button class="btn" id="wizGoAgent">${ready ? "去第 7 步看看" : "去第 7 步准备智能体"}</button></div>
     <label class="wiz-opt ${direct ? "on" : ""}">
       <input type="checkbox" data-wizdirect="1" ${direct ? "checked" : ""}>
       <span class="wiz-opt-body"><span class="wiz-opt-name">没有智能体？直连一个 AI 服务（不推荐）</span>
       <span class="muted">这条路只能写纪要：<b>归档和语音指令仍然需要智能体</b>。只在你没有智能体时生效。</span></span>
     </label>
     <div id="wizDirectBox" class="${direct ? "" : "hidden"}">
       <div class="wiz-fields">
         <label class="wiz-field"><span>服务地址</span>
           <input class="input" id="wizLlmBase" placeholder="http://内网地址/v1"
                  value="${esc(llm.baseUrl || "")}"></label>
         <label class="wiz-field"><span>密钥</span>
           <input class="input" id="wizLlmKey" type="password" placeholder="服务方给你的那一串"
                  value="${esc(llm.apiKey || "")}"></label>
         <label class="wiz-field"><span>模型名（可留空）</span>
           <input class="input" id="wizLlmModel" placeholder="例如 deepseek-chat"
                  value="${esc(llm.model || "")}"></label>
       </div>
       <div class="muted">以后想改、或想配多个上游按顺序用，到「模型路由」里改。</div>
     </div>`);
  wizWhy(host);
  const go = $("#wizGoAgent", host);
  if (go) go.addEventListener("click", () => {
    const i = WIZ_STEPS.findIndex((s) => s.id === "agent");
    if (i >= 0) { _wizStep = i; wizRender(); }
  });
  const directBox = $("[data-wizdirect='1']", host);
  if (directBox) directBox.addEventListener("change", () => {
    _wizChoices.llm = Object.assign({ direct: false, baseUrl: "", apiKey: "", model: "" },
                                    _wizChoices.llm || {});
    _wizChoices.llm.direct = directBox.checked;
    const label = directBox.closest(".wiz-opt");
    if (label) label.classList.toggle("on", directBox.checked);
    const box = $("#wizDirectBox", host);
    if (box) box.classList.toggle("hidden", !directBox.checked);
    wizSaveChoices();
  });
  /* 三格用 change 而不是 input：逐键 PUT 计划文件没必要，失焦/下一步时存一次够了。 */
  const bind = (sel, field) => {
    const el = $(sel, host);
    if (!el) return;
    el.addEventListener("change", () => {
      _wizChoices.llm = Object.assign({ direct: false, baseUrl: "", apiKey: "", model: "" },
                                      _wizChoices.llm || {});
      _wizChoices.llm[field] = el.value;
      wizSaveChoices();
    });
  };
  bind("#wizLlmBase", "baseUrl");
  bind("#wizLlmKey", "apiKey");
  bind("#wizLlmModel", "model");
}

/* ---------------- S7 智能体后端（默认标准 DSH） ---------------- */
function wizRenderAgent(host) {
  const agents = (_wizEnv && _wizEnv.agents) || {};
  const node = (_wizEnv && _wizEnv.node) || {};
  const std = agents.harness || {}, desk = agents.dsh || {};
  host.innerHTML = wizCard("让 ECHO 能动手",
    "上面几步让 ECHO 能「听懂」和「写字」，这一步让它能<b>动手</b>：把纪要存进你的笔记库、按你的话去整理文件。",
    "不能帮你操作电脑、整理笔记 —— 转写和纪要不受影响。",
    wizOption("__std__", _wizChoices.agent === "agent-harness",
        { label: "DSH 标准版（推荐）", sizeMb: 0, name: "wizagent",
          note: node.ok ? "点一下我来准备，需要联网，大约几分钟；以后归我管"
                        : "需要先装 Node.js（我会给你入口）" })
    + wizOption("dsh", _wizChoices.agent === "dsh",
        { label: "我已经装了 DSH 桌面客户端", sizeMb: 0, name: "wizagent",
          note: desk.online ? "检测到它正在运行 ✓" : "我会自动找到它并检查" })
    + wizOption("__none__", !_wizChoices.agent,
        { label: "先不用", sizeMb: 0, name: "wizagent", note: "以后想加：面板 → 能力 → 随时补" })
    + (std.online ? `<div class="muted">标准版已经在运行 ✓</div>` : "")
    + (desk.detail ? `<div class="muted">桌面客户端：${esc(desk.detail)}</div>` : "")
    + `<div><button class="btn" id="wizProbeAgents">检测一下</button></div>`);
  wizWhy(host);
  wizBindPicks(host, (el) => {
    const v = el.dataset.wizpick;
    _wizChoices.agent = v === "__none__" ? "" : (v === "__std__" ? "agent-harness" : v);
  });
  const probe = $("#wizProbeAgents", host);
  if (probe) probe.addEventListener("click", async () => {
    probe.disabled = true;
    try { await api("/api/agents?probe=1"); await loadWizard(); }
    catch (e) { toast("检测失败：" + e.message); }
    finally { probe.disabled = false; }
  });
}

/* ---------------- S8 断网也能用 ---------------- */
function wizRenderOffline(host) {
  const tiny = wizComp("stt-whisper-tiny");
  host.innerHTML = wizCard("断网也能用",
    "放一个很小的转写包在本机：网断了、或者在线服务连不上时，它还能把录音变成文字。",
    "断网时没有本地转写（如果你选了在线转写，这一条尤其值得装）。",
    (tiny ? wizOption("stt-whisper-tiny", (_wizChoices.fallback || []).includes("stt-whisper-tiny"),
        { label: tiny.name, multi: true, name: "wizfallback" })
          : `<div class="muted">清单里没有这个模型文件。</div>`));
  wizWhy(host);
  wizBindPicks(host, (el) => {
    const set = new Set(_wizChoices.fallback || []);
    if (el.checked) set.add(el.dataset.wizpick); else set.delete(el.dataset.wizpick);
    _wizChoices.fallback = [...set];
  });
}

/* ---------------- S9 确认一下（唯一闸门） ---------------- */
async function wizRenderConfirm(host) {
  host.innerHTML = `<div class="card"><div class="card-body">正在算一下要装什么…</div></div>`;
  try {
    const r = await api("/api/wizard/preview", {
      method: "POST", body: JSON.stringify({ choices: _wizChoices }),
    });
    _wizBuilt = r.plan || {};
  } catch (e) {
    host.innerHTML = `<div class="card"><div class="card-body">算不出来：${esc(e.message)}</div></div>`;
    return;
  }
  const b = _wizBuilt;
  const dls = (b.downloads || []).map((d) => `<li>${esc(d.label || d.component)} —— ${esc(wizSize(d.approxMb))}${
    d.ready === true ? "（已经装好，会跳过）" : ""}</li>`).join("");
  const man = (b.manual || []).map((m) => `<li>${esc(m.label || m.component)} —— ${esc(m.reason || "")}</li>`).join("");
  const cfg = (b.config || []).map((c) => `<li>${esc(c.key)} → ${esc(wizMaskSetting(c.key, c.value))}</li>`).join("");
  const eg = (b.providers || []).filter((p) => p.egress)
    .map((p) => `<li>${esc(p.name)}：${esc(p.egressNote || "")}</li>`).join("");
  host.innerHTML = wizCard("确认一下",
    "下面是这次要准备的东西。<b>点「开始准备」之前，什么都不会下载</b>；想改就返回上一步。",
    "",
    `<div class="wiz-sum">
       <div><b>${esc((b.summary || {}).downloads || "")}</b></div>
       ${dls ? `<ul>${dls}</ul>` : `<div class="muted">没有要下载的东西</div>`}
       ${man ? `<div class="wiz-lose">这几项要你自己准备（面板不代装）：</div><ul>${man}</ul>` : ""}
       ${cfg ? `<div>将写入这些设置：</div><ul>${cfg}</ul>` : ""}
       ${eg ? `<div class="wiz-lose">会把数据发到外部服务：</div><ul>${eg}</ul>` : ""}
       <div class="muted">开始之后你可以关掉这个页面 —— 下载在 ECHO 里继续，回来还能看进度。</div>
     </div>
     <div><button class="btn" id="wizStart">开始准备</button></div>`);
  wizWhy(host);
  const start = $("#wizStart", host);
  if (start) start.addEventListener("click", async () => {
    start.disabled = true;
    try {
      await api("/api/wizard/execute", { method: "POST", body: JSON.stringify({ choices: _wizChoices }) });
      _wizStep = WIZ_STEPS.findIndex((s) => s.id === "run");
      wizRender();
    } catch (e) { toast("没能开始：" + e.message); start.disabled = false; }
  });
}

/* ---------------- S10 正在准备 ---------------- */
async function wizRenderRun(host) {
  const draw = (st) => {
    // 失败的项要把**原因**显示出来：以前只显示"没成"两个字，而"失败"还被状态词表 bug
    // 显示成了"排队中"（见 app/modelinfo.py 的 job_state 说明）。
    const rows = (st.items || []).map((it) => {
      const detail = it.state === "error" && it.message
        ? ` —— <span class="wiz-lose">${esc(it.message)}</span>` : "";
      return `<div class="wiz-run-row">
      <span>${esc(it.label || it.component)}</span>
      <span class="muted">${esc(it.text || "")}${it.percent != null && it.state === "downloading"
        ? ` ${it.percent}%` : ""}${detail}</span></div>`;
    }).join("");
    const failed = (st.failed || []).map((f) => `<li>${esc(f.component || "")}：${esc(f.error || f.message || "")}</li>`).join("");
    host.innerHTML = `<div class="card"><div class="card-title">正在准备
        <span class="spacer"></span><span class="muted">${esc(st.summary || "")}</span></div>
      <div class="card-body">
        ${rows || `<div class="muted">没有要下载的东西</div>`}
        ${failed ? `<div class="wiz-lose">这几项没成（可以重试，或先跳过）：</div><ul>${failed}</ul>` : ""}
        <div class="muted">可以关掉这个页面：下载在 ECHO 里继续跑，回来还能看进度。</div>
        <div><button class="btn" id="wizFinish">看看现在能做什么</button></div>
      </div></div>`;
    const fin = $("#wizFinish", host);
    if (fin) fin.addEventListener("click", () => {
      if (_wizTimer) { clearInterval(_wizTimer); _wizTimer = null; }
      _wizStep = WIZ_STEPS.findIndex((s) => s.id === "done");
      wizRender();
    });
  };
  const tick = async () => {
    try { draw(await api("/api/wizard/state")); }
    catch (e) { host.innerHTML = `<div class="card"><div class="card-body">读进度失败：${esc(e.message)}</div></div>`; }
  };
  await tick();
  if (_wizTimer) clearInterval(_wizTimer);
  _wizTimer = setInterval(tick, 2000);
}

/* ---------------- S11 你的 ECHO 现在能做什么 ---------------- */
async function wizRenderDone(host) {
  let st = {};
  try { st = await api("/api/wizard/state"); } catch (e) { /* 取不到就只列"怎么开始用" */ }
  const ok = (st.items || []).filter((i) => i.state === "done" || i.state === "skipped");
  const manual = st.manual || [];
  const missing = st.missing || [];
  const cant = (st.failed || []).length + manual.length;
  host.innerHTML = `<div class="card"><div class="card-title">你的 ECHO 现在能做什么</div>
    <div class="card-body">
      <div class="wiz-done-ok">✓ 录音 → 自动变成文字（${ok.length ? "已就绪 " + ok.length + " 项" : "按你选的配置"}）</div>
      ${missing.length ? `<div class="wiz-lose">还不能做什么（以后随时能补）：</div>
        <ul>${missing.map((m) => `<li>${esc(m.feature)} —— ${esc(m.reason || "")}
          ${m.fix ? `<span class="muted">（${esc(m.fix)}）</span>` : ""}</li>`).join("")}</ul>` : ""}
      ${manual.length ? `<div class="wiz-lose">这些还要你自己准备一下：</div>
        <ul>${manual.map((m) => `<li>${esc(m.label || m.component)} —— ${esc(m.reason || "")}
          ${m.command ? `<code>${esc(m.command)}</code>` : ""}</li>`).join("")}</ul>` : ""}
      ${cant ? "" : `<div class="muted">没有失败项。</div>`}
      <div class="wiz-next">
        <div><b>怎么开始用</b></div>
        <div>1. 按 Ctrl + Shift + E 呼出面板</div>
        <div>2. 对着麦克风说一句，或点面板上的录音按钮</div>
        <div>3. 开完会在「会议」里看文字稿和纪要</div>
      </div>
      <div class="muted">以后想加能力：面板 → 能力 → 随时补（不用重装 ECHO）。</div>
      <div><button class="btn" id="wizRestart">回到第一步重新看</button></div>
    </div></div>`;
  const again = $("#wizRestart", host);
  if (again) again.addEventListener("click", () => { _wizStep = 0; wizRender(); });
  /* 走到末页 = 向导走完了：写 data/installed-components.json（设计 §4/§5 的"执行后真值"）。
     这是**首个安装判据**的凭据（`install_state.declared()` 也认它）—— 写完面板就不再提示"还没装完"。
     失败不影响用户看这一页（下次进末页还会再试一次）。 */
  post("/api/wizard/finalize", {}).then(() => renderInstallNotice()).catch(() => {});
}


/* 进哪个页签：显式指定（`?view=…` / `echo.gotoView`）最优先；否则**进仪表盘**。
   深链先过 `normView()`（老值 settings/boot/failover/capabilities 折算到新页签，wizard → general）。
   这里以前是"首装直接进向导"，判据是服务端 installed-components.json 在不在 —— 但那个文件
   只有向导末页才写，而安装现在多半由**助手按 echo-install 技能**完成，于是"技能装完打开面板
   还是进向导"（2026-09-21 实测反馈）。现在改成：**永不自动进向导**，没装完由顶部横幅说清下一步
   （横幅可一键进向导或能力后端页）。想直接看向导：`?view=wizard` —— 落到「常规」后
   会**自动展开向导卡并滚过去**（不能只是"到了常规但看不到向导"）。 */
let _bootView = "";
let _bootWantWizard = false;      // 老深链/老 gotoView 明确要向导：落到常规后要展开那张卡
try {
  const q = new URLSearchParams(location.search).get("view");
  const raw = q || localStorage.getItem("echo.gotoView") || "";
  const want = normView(raw);
  _bootWantWizard = String(raw).trim().toLowerCase() === "wizard";
  if (localStorage.getItem("echo.gotoView")) localStorage.removeItem("echo.gotoView");
  if (want && _VIEWS.includes(want)) _bootView = want;
} catch (e) { /* 忽略 */ }

async function bootView() {
  if (_bootView) {
    switchView(_bootView);
    renderInstallNotice();
    if (_bootWantWizard) gotoWizard();     // 老书签：展开向导卡 + 滚到它
    return;
  }
  switchView("dashboard");
  renderInstallNotice();
}
bootView();
applyCollapsedCards();      // 应用上次的卡片折叠状态（设置页与各页签的可折叠卡片）

/* ---------------- 自动刷新：间隔取自设置 panelAutoRefresh ----------------
   2026-09-19 审计发现这个设置项在面板上摆着却没人读（判定 DEAD），这里把它接上：
   仪表盘/启动页/模型路由页的轮询间隔 = 该项（秒），0 = 不自动刷新。
   1 秒一跳只是"对表"用的最小步进，真正刷不刷由 _panelRefreshDue() 判定。
   注意：会议列表的转写进度条不受此项影响（那是进度指示，停掉就看不到进度了）。 */
const _PANEL_TICK_MS = 1000;
let _panelRefreshAt = 0;
function _panelRefreshSeconds() {
  const s = settingByKey("panelAutoRefresh");
  const n = Number(s ? s.value : 3);          // 设置还没拉到：按默认 3 秒
  return Number.isFinite(n) && n > 0 ? n : 0;  // 0/负数/非法 = 关闭自动刷新
}
function _panelRefreshDue() {
  const sec = _panelRefreshSeconds();
  if (!sec) return false;
  const now = Date.now();
  if (now - _panelRefreshAt < sec * 1000) return false;
  _panelRefreshAt = now;
  return true;
}
/* 启动时先取一次设置元数据：仪表盘轮询在进设置页之前就开始了，
   只等 loadSettings() 会一直按默认间隔跑（用户改了也不生效）。 */
async function loadPanelPrefs() {
  if (_settingsCache.length) return;
  try {
    const r = await api("/api/settings");
    mergeSettingsRows(r.settings || []);      // 并进（不要覆盖）——见 ensureSettings 那段说明
  } catch (e) { /* 取不到就按默认 3 秒，不影响其它功能 */ }
}
loadPanelPrefs();

setInterval(() => {
  if (!_panelRefreshDue()) return;
  const v = $(".tab.active");
  const view = v ? normView(v.dataset.view) : "";
  // `boot` 的内容现在在「常规」页、`failover` 的内容在「智能体」页（2026-09-25 页签整合）
  if (view === "dashboard") refreshDashboard();
  else if (view === "general") { loadBoot(); loadBootLogs(); }
  else if (view === "agent" && !_rtDirty && !_rtBusy()) loadRouter();
}, _PANEL_TICK_MS);
// 转写进度轮询（会议列表进度条）
setInterval(pollTranscribe, 2000);
