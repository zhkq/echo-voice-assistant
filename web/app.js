/* ECHO 控制面板前端逻辑（原生 JS，无构建链） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel, root = document) => [...(root || document).querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

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
function switchView(name) {
  $$(".view").forEach((v) => v.classList.add("hidden"));
  $(`#view-${name}`).classList.remove("hidden");
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
  if (name === "dashboard") { refreshDashboard(); loadTargets(); }
  if (name === "settings") loadSettings();
  if (name === "history") loadHistory();
  if (name === "meetings") { loadMeetings(); refreshMeetingHeader(); }
  if (name === "boot") { loadBoot(); loadBootLogs(); }
  if (name === "failover") loadRouter();
  if (name === "capabilities") loadCapabilities();
}
$$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));

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
document.addEventListener("click", (e) => {
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
  switchView("failover");
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
    (reg.registered ? `<b>已注册</b>` : `<b style="color:var(--yellow)">未注册</b>`) +
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

/** 正在这一页上操作（改昵称、选下拉、点开关）时，别让 2 秒轮询把 DOM 换掉、抢走焦点 */
function _rtBusy() {
  const a = document.activeElement;
  const sec = $("#view-failover");
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
    loadRouterSettings();      // 路由参数（原「设置 → 模型路由」那一组）
  } catch (e) { toast("加载模型路由失败：" + e.message); }
  loadRouterLlm();      // 语言模型那一块（用哪个实现 + 在线服务）也在这个页签里
}

/** 读一次设置（`_settingsCache`）。两个页签都要用（能力页签 / 模型路由页签），
 *  所以做成"没有才拉"的共享入口 —— 各拉一遍还会出现"一处改了另一处是旧值"。 */
async function ensureSettings(force) {
  if (!force && _settingsCache.length) return _settingsCache;
  const r = await api("/api/settings");
  _settingsCache = r.settings || [];
  _agentsCache = r.agents || _agentsCache;
  return _settingsCache;
}

/** 路由参数卡（原「设置 → 模型路由」那一组 7 项）。
 *
 *  2026-09-19 用户实测："设置中的模型路由标签整合到模型路由中" —— 这些项只有和本页的
 *  成员/优先级放一起才看得懂（"注册到 DSH"、"模型组显示名"本来就是这一页在用的东西）。
 *  渲染直接复用设置页的 renderSettingRow()（同样的标签/控件/说明样式），
 *  保存走本卡的按钮（只收集本卡内的 data-key）。
 */
let _rtTabOk = true;

async function loadRouterSettings() {
  const host = $("#rtSetHost");
  if (!host) return;
  const wasOk = _rtTabOk;
  try {
    const rows = (await ensureSettings()).filter((s) => ROUTER_KEYS.has(s.key));
    rows.sort((a, b) => ((a.order ?? 1e6) - (b.order ?? 1e6))
      || String(a.key).localeCompare(String(b.key)));
    host.innerHTML = rows.map(renderSettingRow).join("")
      || `<div class="muted" style="font-size:12px">没有可显示的路由参数。</div>`;
    _rtTabOk = true;
    const btn = $("#rtSetSave");
    if (btn && !btn.dataset.bound) {
      btn.dataset.bound = "1";
      btn.addEventListener("click", saveRouterSettings);
    }
    if (!wasOk) loadSettings();      // 页签恢复：设置页里回退显示的那一组可以收起来了
  } catch (e) {
    host.innerHTML = `<div class="muted" style="font-size:12px">读取失败：${esc(e.message)}</div>`;
    _rtTabOk = false;
    if (wasOk) { try { loadSettings(); } catch (_) { /* 忽略 */ } }
  }
}

/** 保存路由参数：只收本卡里的字段（设置页那套按钮与本卡互不影响）。 */
async function saveRouterSettings() {
  const values = {};
  $$("#rtSetHost [data-key]").forEach((el) => {
    const key = el.dataset.key;
    const meta = _settingsCache.find((s) => s.key === key);
    if (!meta) return;
    if (meta.value_type === "bool") values[key] = el.checked;
    else if (meta.value_type === "list") values[key] = el.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean);
    else if (meta.value_type === "int") values[key] = parseInt(el.value, 10) || 0;
    else if (meta.value_type === "float") values[key] = parseFloat(el.value) || 0;
    else values[key] = el.value;
  });
  if (!Object.keys(values).length) { toast("没有需要保存的项"); return; }
  const state = $("#rtSetState");
  try {
    // 后端在 PUT 里会把路由项同步写到 dsh-failover/config.json 并热重载（见 api.put_settings）
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ values }) });
    toast("已保存 " + Object.keys(values).length + " 项路由参数");
    if (state) state.textContent = "已保存并热重载";
    await loadRouterSettings();
  } catch (err) {
    toast("保存失败：" + err.message);
    if (state) state.textContent = "保存失败";
  }
}

async function saveRouter() {
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
$$("[data-goto-link='failover']").forEach((a) =>
  a.addEventListener("click", (e) => { e.preventDefault(); gotoFailover(); }));

async function refreshDashboard() {
  refreshFailoverCard();            // 模型路由小卡片（独立容错，不阻塞主刷新）
  // 智能体 Web 界面小图标（独立 harness）：跟着仪表盘刷新一起更新，失败不阻塞
  refreshAgentsForDashboard().catch(() => {});
  try {
    const st = await api("/api/status");
    document.body.classList.remove("echo-offline");   // 顶栏去掉常驻状态后，靠这个红标表示"连不上"
    renderLiveStatus(st);                             // 启动页"启动日志"标题右侧的在线时长 + 状态
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
  el.innerHTML = `<span>已在线 <b>${up}</b></span><span class="badge ${v.cls}">${v.text}</span>`;
  el.title = `ECHO 服务连续在线 ${up}，当前${v.text} · ${v.tip}`;
}

const STATUS_TEXT = { online: "在线", offline: "离线", active: "工作中", idle: "空闲",
  error: "错误", disabled: "未启用", paused: "暂停", unknown: "未知",
  transcribing: "转写中", sent: "已发送", running: "执行中", done: "完成", failed: "失败",
  pending: "排队中", recording: "录音中", transcribed: "已完成", interrupted: "已中断",
  starting: "启动中" };

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

/* ================= 设置 ================= */
let _settingsCache = [];

/* 一级分组（后端 grp）展示顺序 = 业务相关性：
   智能体(决定谁干活) → 语音命令(主用法，内含 4 个二级小节) → 唤醒词 → 会议 → 纪要归档
   → 面板与服务 → 存储路径 → 模型路由。
   `model` 只在「模型」页签加载失败时出现（那 9 项回退到设置页）。 */
const SET_GROUP_ORDER = ["agent", "model", "voice", "wake", "meeting",
  "worklog", "panel", "paths", "router", "dsh"];
const SET_GROUP_NAMES = { agent: "智能体", model: "模型与引擎", voice: "语音命令",
  wake: "唤醒词", meeting: "会议", worklog: "纪要归档",
  panel: "面板与服务", paths: "存储路径", router: "模型路由", dsh: "DSH 服务" };

/* 二级小节（后端 sub）：**包含在**所属分组里，不是与它并列的分组。
   用户 2026-09-19 反馈："语音命令和 beep / command / speech 应该是包含不是并列" ——
   所以「语音命令」下按"怎么录 → 发到哪 → 播报什么 → 提示音"分四节，
   小节名与顺序都在这里，后端只声明归属（config.py 的 sub）。 */
const SET_SUB_ORDER = ["record", "command", "speech", "beep"];
const SET_SUB_NAMES = { record: "录音与转写", command: "命令与会话",
  speech: "朗读与反馈", beep: "提示音与通知" };

/* "用哪个实现"相关的配置项：从「设置」页移出，统一由顶部「能力」页签承载
   （前端过滤，后端 grp 不动）——它们都是"每个能力用哪个实现/装没装"这一类问题。
   ttsEngine 也在其中：它是朗读的唯一开关（本地/在线/关闭），能力页签的"语音合成"卡承载它。 */
const MODEL_KEYS = new Set([
  "sttModel", "meetingSttModel", "device", "ttsEngine",
  "wakeEngine", "meetingDiarize",
  "voiceprintEnabled", "voiceprintAutoEnroll", "voiceprintThreshold", "voiceprintMargin",
]);
/* 路由参数：同样从「设置」页移出，由顶部「模型路由」页签承载（前端过滤，后端 grp 不动）。
   为什么：这些项只有和那一页的成员/优先级放一起才看得懂（"注册到 DSH"、"模型组显示名"
   本来就是那个页签在用的东西）；2026-09-19 用户实测："设置中的模型路由标签整合到模型路由中"。
   键盘值：与 config.DEFAULTS 里 grp="router" 的项一一对应（tests 里钉住，防漂移）。 */
const ROUTER_KEYS = new Set([
  "routerAutoRegister", "routerDisplayName", "routerProbeInterval",
  "routerFirstByteTimeout", "routerConnectTimeout",
  "routerBreakerThreshold", "routerBreakerCooldown",
]);
/* 默认展开；用户折叠过的分组/小节记在 localStorage，刷新/重开面板后保持。
   两级各一份状态：`echo.settings.collapsedGroups`（一级）、`...collapsedSubs`（二级）。 */
const SET_COLLAPSE_KEY = "echo.settings.collapsedGroups";
const SET_SUB_COLLAPSE_KEY = "echo.settings.collapsedSubs";

function _collapsedGroups() {
  try { return new Set(JSON.parse(localStorage.getItem(SET_COLLAPSE_KEY) || "[]")); }
  catch (e) { return new Set(); }
}
function _saveCollapsedGroups(set) {
  try { localStorage.setItem(SET_COLLAPSE_KEY, JSON.stringify([...set])); } catch (e) { /* 忽略 */ }
}
/* 二级小节的折叠状态按 "分组/小节" 记：同名小节将来若出现在别的分组也不会串。 */
function _collapsedSubs() {
  try { return new Set(JSON.parse(localStorage.getItem(SET_SUB_COLLAPSE_KEY) || "[]")); }
  catch (e) { return new Set(); }
}
function _saveCollapsedSubs(set) {
  try { localStorage.setItem(SET_SUB_COLLAPSE_KEY, JSON.stringify([...set])); } catch (e) { /* 忽略 */ }
}

/* 智能体元信息（来自 /api/agents）：
   {name, displayName, vendor, description, configKey, enabled, active, available, reason, probe} */
let _agentsCache = [];
const _agentDirty = {};        // 展开区里改过、但还没点保存的值

/** 智能体状态徽标（探测结论）。 */
function agentStatusChip(a) {
  if (!a) return "";
  let cls = "off", text = "未启用";
  if (a.enabled && a.available) { cls = "on"; text = "可用"; }
  else if (a.enabled && !a.available) { cls = "bad"; text = "不可用"; }
  return `<span class="agent-chip ${cls}">${esc(text)}</span>`;
}

/** 智能体表格：每行一个开关（互斥单选），选中的那行下方展开它的设置内容。 */
function renderAgentTable() {
  const host = $("#agentTable");
  if (!host) return;
  if (!_agentsCache.length) { host.innerHTML = `<div class="empty">未取到智能体列表</div>`; return; }
  const rows = _agentsCache.map((a) => `<div class="agent-row${a.active ? " active" : ""}"
      data-agent-row="${esc(a.name)}">
      <div class="agent-cell-name">
        <div class="an">${esc(a.displayName)}</div>
        <div class="av">${esc(a.vendor || "")}</div>
      </div>
      <div class="agent-cell-state">${agentStatusChip(a)}</div>
      <div class="agent-cell-switch">
        <label class="rt-sw" title="${a.active ? "当前正在使用" : "设为 ECHO 使用的智能体"}">
          <input type="checkbox" data-agent-toggle="${esc(a.name)}" ${a.active ? "checked" : ""}><i></i>
        </label>
      </div>
    </div>`).join("");
  host.innerHTML = `<div class="agent-table">${rows}</div>
    <div class="agent-detail" id="agentDetail">${agentDetailHtml()}</div>`;
}

/** 当前选中智能体的展开设置内容。
 *
 *  它自己的配置项来自 `/api/agents` 的 `settings` 字段（后端按适配器的 `settings_keys` 组装）——
 *  这些键在设置页里是 hidden 的：把"DSH 服务地址"摊在「面板与服务」里，用户根本不知道它跟谁有关
 *  （2026-09-19 实测反馈："下面的 dsh 没必要吧，或者把端口挪上去"）。现在它们长在对应智能体的
 *  展开区里，与"检测"、状态说明同处一地。
 */
function agentDetailHtml() {
  const cur = _agentsCache.find((a) => a.active);
  if (!cur) {
    return `<div class="agent-detail-head">没有选中的智能体 —— 打开上表中任意一个开关即可启用</div>`;
  }
  const probeBtn = `<button type="button" class="btn" data-agent-probe="${esc(cur.name)}"
      title="重新探测该智能体是否可用">检测</button>`;
  const fields = (cur.settings || []).map((s) => {
    const id = "agent-set-" + s.key;
    const cur_val = _agentDirty[s.key] !== undefined ? _agentDirty[s.key] : s.value;
    let ctl;
    if (s.secret) {
      // 密钥：只显示"配没配"，值永不下发（后端也不回显）→ 密码框 + 留空 = 不改
      // （2026-09-19 用户实测问"我在哪里配置 key"：这类键原来被整条跳过，面板上没处填）
      ctl = `<input type="password" class="ctl" id="${id}" data-agent-field="${esc(s.key)}"
               data-secret="1" value="" autocomplete="new-password"
               placeholder="${s.hasValue ? "已配置（留空 = 不改）" : "未配置"}">`;
    } else if (s.value_type === "bool") {
      ctl = `<input type="checkbox" class="ctl" id="${id}" data-agent-field="${esc(s.key)}"
               ${cur_val ? "checked" : ""}>`;
    } else if (s.options && s.options.length) {
      ctl = `<select class="ctl" id="${id}" data-agent-field="${esc(s.key)}">` +
        s.options.map((o) => `<option value="${esc(o)}" ${String(o) === String(cur_val) ? "selected" : ""}>` +
          `${esc(o)}</option>`).join("") + `</select>`;
    } else {
      ctl = `<input class="ctl" id="${id}" data-agent-field="${esc(s.key)}" value="${esc(cur_val || "")}">`;
    }
    return `<div class="agent-field">
      <label for="${id}">${esc(s.label)}</label>
      ${ctl}
      <div class="desc">${esc(s.description || "")}</div>
    </div>`;
  });
  const note = cur.reason
    ? `<div class="agent-detail-msg ${cur.available ? "ok" : "warn"}">${
        cur.available ? "✅ " : "⚠ "}${esc(cur.reason)}</div>`
    : "";
  const warn = cur.available ? "" :
    `<div class="agent-detail-msg warn">探测未通过时无法用它执行命令；按上面的提示处理后点「检测」重试。</div>`;
  return `<div class="agent-detail-head">
      <strong>${esc(cur.displayName)}</strong>
      <span class="muted">${esc(cur.vendor || "")}</span>
      <span class="spacer"></span>${probeBtn}
    </div>
    <div class="agent-detail-desc">${esc(cur.description || "")}</div>
    ${fields.join("")}${note}${warn}`;
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

/* ---------------- 设置 → 智能体：开关即单选，切换立即保存 ---------------- */
$("#settingsForm").addEventListener("change", async (e) => {
  const toggle = e.target.closest("[data-agent-toggle]");
  if (toggle) {
    const name = toggle.dataset.agentToggle;
    const cur = _agentsCache.find((a) => a.active);
    if (cur && cur.name === name) return;                 // 点的就是当前项
    /* 选中即启用：产品自带的"启用开关"（如 agentCodebuddyEnabled）一并打开。
       否则会出现"开关已选中、状态却是未启用"的自相矛盾 —— 两个开关表达同一件事，
       面板只留一个入口（2026-09-19 设置去重）。 */
    const target = _agentsCache.find((a) => a.name === name);
    const values = { agentBackend: name };
    if (target && target.configKey) values[target.configKey] = true;
    try {
      await api("/api/settings", { method: "PUT",
        body: JSON.stringify({ values }) });
      await loadAgents(false);
      renderAgentTable();                                 // 重绘：其余开关自动回弹
      const now = _agentsCache.find((a) => a.active);
      toast(`已切换到 ${now ? now.displayName : name}`);
    } catch (err) { toast("切换失败：" + err.message); renderAgentTable(); }
    return;
  }
  // 展开区字段：先记下，点全局「保存」时随表单一起落库
  const field = e.target.closest("[data-agent-field]");
  if (field) _agentDirty[field.dataset.agentField] = field.value;
});

/* 展开区里的「检测」按钮 */
$("#settingsForm").addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-agent-probe]");
  if (!btn) return;
  btn.disabled = true;
  const old = btn.textContent;
  btn.textContent = "检测中…";
  const r = await loadAgents(true);
  btn.disabled = false;
  btn.textContent = old;
  if (!r) { toast("检测失败：服务未响应"); return; }
  renderAgentTable();
  const a = _agentsCache.find((x) => x.name === btn.dataset.agentProbe);
  if (a) toast(a.available ? `${a.displayName} 可用` : `${a.displayName} 不可用：${a.reason || ""}`);
});

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
  if (c.service) return modelBadge(c.ready === false ? "⏹ 未运行" : "未知", "warn");
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
    return modelActions(m);
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
    return `<div class="cap-comp${isCur ? " cur" : ""}">
      <div class="cap-comp-main">
        ${isCur ? `<span class="cap-cur">▶ 当前</span>` : ""}
        <span class="cap-comp-name">${esc(c.name || c.id)}</span>
        <span class="cap-comp-badge">${capCompBadge(c)}</span>
      </div>
      <div class="cap-comp-meta">${esc(meta)}</div>
      ${how}
      <div class="cap-comp-acts">${capCompActions(c)}</div>
    </div>`;
  }).join("");
  return `<div class="cap-comps">${rows}</div>`;
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

/** 转写卡的本地面：两个引擎下拉（命令/会议）+ 可装的本地模型（当前用的标出来）。 */
function capAsrLocal() {
  const stt = settingByKey("sttModel");
  const mstt = settingByKey("meetingSttModel");
  const curIds = [engineModelId(stt && stt.value), engineModelId(mstt && mstt.value)];
  return `<div class="cap-sub">本地引擎（选「本地转写引擎」时生效）</div>
    <div class="cap-engine-row">
      <label>命令转写${_selectHtml("sttModel", stt && stt.options, stt && stt.value)}</label>
      <label>会议转写${_selectHtml("meetingSttModel", mstt && mstt.options, mstt && mstt.value)}</label>
    </div>
    <div class="cap-sub">可装的本地模型</div>
    ${capCompTable(capCompsOf("stt"), curIds)}`;
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
    body += `<div class="mcard-act"><button type="button" class="btn mini" data-goto="failover">去模型路由</button></div>`;
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
  const usable = items.filter((c) => c.applicable);
  const readyN = usable.filter((c) => c.ready === true).length;
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
  if (sum) sum.textContent = `组件就绪 ${readyN}/${usable.length}`;
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
  const readyN = (((_capCache.comps || {}).items) || [])
    .filter((c) => c.applicable && c.ready === true).length;
  const totalN = (((_capCache.comps || {}).items) || []).filter((c) => c.applicable).length;
  host.innerHTML = `<div class="card collapsible" data-collapse-id="cap-env">
    <div class="card-title"><span class="set-arrow">▶</span><span class="ic">🧱</span>运行环境
      <span class="muted">${esc(plat)}</span>
      <span class="muted" style="margin-left:auto">就绪 ${readyN}/${totalN}</span>
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

/** 能力页签的事件绑定（一个页面一次，重绘不用重绑）。 */
function bindCapCards() {
  const host = $("#view-capabilities");
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
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: { [key]: val } }) });
      toast("已更新");
      const r = await api("/api/settings");
      _settingsCache = r.settings || _settingsCache;
      loadCapabilities();
    } catch (err) { toast("更新失败：" + err.message); }
  });
  host.addEventListener("click", async (e) => {
    const save = e.target.closest("[data-cap-save]");
    if (save) {
      const values = {};
      $$("#view-capabilities [data-key]").forEach((el) => {
        const key = el.dataset.key;
        const meta = (((_capCache.cfg || {}).settings) || []).find((x) => x.key === key);
        if (!meta) return;
        if (meta.secret) { if (el.value && el.value.trim()) values[key] = el.value; return; }
        values[key] = el.value;
      });
      if (!Object.keys(values).length) { toast("没有需要保存的改动"); return; }
      try {
        await api("/api/settings", { method: "PUT", body: JSON.stringify({ values }) });
        toast("已保存 " + Object.keys(values).length + " 项");
        loadCapabilities();
      } catch (err) { toast("保存失败：" + err.message); }
      return;
    }
    const dl = e.target.closest("[data-msdl]");
    if (dl) {
      dl.disabled = true;
      try {
        const r = await post("/api/models/download", { id: dl.dataset.msdl, force: dl.dataset.force === "1" });
        toast(r.message || "已开始下载");
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
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ values }) });
      toast("已保存 " + Object.keys(values).length + " 项");
      loadRouterLlm();
    } catch (err) { toast("保存失败：" + err.message); }
  });
}

/** 某个"用哪个实现"选择变化后，刷新**当前正在看的**那一页（两个页签各有一块）。 */
function refreshAfterProviderChange() {
  const tab = $(".tab.active");
  const view = tab && tab.dataset ? tab.dataset.view : "";
  if (view === "failover") { loadRouter(); return; }   // loadRouter 内部会顺带刷语言模型块
  if (view === "capabilities") { loadCapabilities(); return; }
  loadSettings();
}

/* 选 provider：立即写配置（与"命令目标"下拉同一种交互：选中即持久化）。
   TTS 不走这里：它的开关是 ttsEngine（见 bindCapCards 的 data-tts-engine）。 */
document.addEventListener("change", async (e) => {
  const kind = e.target && e.target.dataset ? e.target.dataset.providerKind : "";
  if (!kind) return;
  const key = "provider" + kind.charAt(0).toUpperCase() + kind.slice(1);
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: { [key]: e.target.value } }) });
    toast("已切换到：" + e.target.value);
    const r = await api("/api/settings");
    _settingsCache = r.settings || _settingsCache;
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
  // 语言模型那块在「模型路由」页签里，语音转写在「能力」页签里 —— 按当前页签找字段
  const scope = ($(".tab.active") || {}).dataset?.view === "failover" ? "#rtLlmHost" : "#view-capabilities";
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
    _modelJobsCache = (modelsRes.jobs && modelsRes.jobs.items) || {};
    _sttCache = sttRes;
    _vpCache = vpRes;
    _capTabOk = true;
    renderCapOverview();
    host.innerHTML = CAP_KINDS.map(capKindCard).join("");
    const funcs = $("#capFuncCards");
    if (funcs) funcs.innerHTML = modelFunctions()
      .filter((f) => ["wake", "diar", "vp", "dev"].indexOf(f.id) >= 0).map(renderModelCard).join("");
    renderCapEnv();
    bindCapCards();
    applyCollapsedCards($("#view-capabilities"));   // 应用上次的卡片折叠状态（动态卡片要重绘后应用）
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



/* ---- 环境体检 + 迁移已有会议（2.0 / P1、D20、D21） ----
   只读展示四类根（ECHO/DATA/MEETINGS/MODELS）的存在、可写性与磁盘余量，并提供
   "迁移已有会议"。迁移的用法刻意设计成两步：**先改上面的「会议目录」并保存，再点迁移**——
   卡片在渲染时记下"当时的会议目录"作为 source，因为配置一旦保存，"当前目录"就已经是新值了，
   不显式传旧值的话服务端只能回答"无需迁移"（正确但不是用户想要的）。 */
async function renderEnvCheck(host) {
  host.innerHTML = `<div class="set-group" data-grp="envcheck">
    <div class="set-group-title" role="button" tabindex="0" aria-expanded="true">
      <span class="set-arrow">▶</span><span>环境体检</span><span class="set-count" id="envCheckCount">…</span>
    </div>
    <div class="set-group-body"><div id="envCheckBody" class="muted">读取中…</div></div>
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

async function loadSettings() {
  try {
    const r = await api("/api/settings");
    _settingsCache = r.settings;
    _agentsCache = r.agents || [];
    const groups = {};
    // 「能力」页签承载的项（用哪个实现）与「模型路由」页签承载的项（路由参数），都从这里过滤掉；
    // **对应页签加载失败时回退显示**（_capTabOk / _rtTabOk = false），免得唯一入口挂掉时改不回来。
    // 组内顺序用后端给的 `order`（= DEFAULTS 声明顺序）：/api/settings 是按 (grp, key)
    // 字母序来的，直接渲染会把"三个提示音开关"这类编排打散（见 config.SETTING_ORDER）。
    const rows = r.settings.filter((s) =>
      !(_capTabOk && MODEL_KEYS.has(s.key)) && !(_rtTabOk && ROUTER_KEYS.has(s.key)));
    rows.sort((a, b) => ((a.order ?? 1e6) - (b.order ?? 1e6))
      || String(a.key).localeCompare(String(b.key)));
    rows.forEach((s) => { (groups[s.grp] = groups[s.grp] || []).push(s); });
    // 「智能体」分组的配置项都是 hidden（不进 settings），这里补一个空分组占位
    if (!groups.agent) groups.agent = [];
    // 已知分组按业务相关性排序，未知分组排到末尾（保持出现顺序）
    const known = SET_GROUP_ORDER.filter((g) => groups[g]);
    const extra = Object.keys(groups).filter((g) => !SET_GROUP_ORDER.includes(g));
    const collapsed = _collapsedGroups();
    const form = $("#settingsForm");
    // 被移走的项要给一句指路；对应页签挂掉时反过来提示它们仍在本页
    const modelHintRow = _capTabOk
      ? `<div class="muted" style="margin:0 0 10px">转写引擎 / 朗读实现 / 计算设备 / 唤醒 / 声纹 / 说话人分离，
          以及在线服务的地址与密钥，都已移到顶部「<b>能力</b>」页签（用哪个实现 + 装没装，一处看全）。</div>`
      : `<div class="mcard-warn" style="margin:0 0 10px">「能力」页签加载失败，模型与在线服务相关设置暂时保留在本页；页签恢复后会自动收起。</div>`;
    const routerHintRow = _rtTabOk
      ? `<div class="muted" style="margin:0 0 10px">路由参数（注册 / 显示名 / 探测与熔断）已移到顶部
          「<b>模型路由</b>」页签，和成员、优先级放在一起。</div>`
      : `<div class="mcard-warn" style="margin:0 0 10px">「模型路由」页签加载失败，路由参数暂时保留在本页；页签恢复后会自动收起。</div>`;
    form.innerHTML = modelHintRow + routerHintRow + [...known, ...extra].map((g) => {
      const items = groups[g];
      const isCollapsed = collapsed.has(g);
      const no = g === "agent" ? (_agentsCache.filter((a) => a.active).length || 0) : items.length;
      const body = g === "agent"
        ? `<div id="agentTable" class="agent-table-wrap"></div>`
        : renderGroupBody(g, items);
      return `<div class="set-group${isCollapsed ? " collapsed" : ""}" data-grp="${esc(g)}">
        <div class="set-group-title" role="button" tabindex="0" aria-expanded="${!isCollapsed}">
          <span class="set-arrow">▶</span>
          <span>${esc(SET_GROUP_NAMES[g] || g)}</span>
          <span class="set-count">${no}</span>
        </div>
        <div class="set-group-body">${body}</div>
      </div>`;
    }).join("");
    renderAgentTable();
    // 环境体检卡片（2.0 / P1）：四类根 + 一键迁移已有会议
    try {
      const envHost = document.createElement("div");
      envHost.id = "envCheckHost";
      form.appendChild(envHost);
      renderEnvCheck(envHost);
    } catch (e) { /* 体检卡片失败不能拖垮设置页 */ }
    _syncSettingsCollapseAll();     // 重绘后让顶部双箭头跟着当前折叠状态
  } catch (e) { toast("加载设置失败：" + e.message); }
}

/* 一级分组折叠/展开（事件委托，重绘后无需重新绑定） */
function toggleSetGroup(titleEl) {
  const box = titleEl.closest(".set-group");
  if (!box) return;
  const g = box.dataset.grp;
  const nowCollapsed = box.classList.toggle("collapsed");
  titleEl.setAttribute("aria-expanded", String(!nowCollapsed));
  const collapsed = _collapsedGroups();
  if (nowCollapsed) collapsed.add(g); else collapsed.delete(g);
  _saveCollapsedGroups(collapsed);
}

/** 分组内容：有条目带 `sub` 时按二级小节渲染（可各自折叠），其余直接平铺。
 *
 *  为什么是"包含"而不是并列：语音命令这一个功能里有"怎么录 / 发到哪 / 播报什么 / 提示音"
 *  四件事，各成一节才看得清，但它们都属于「语音命令」——用户 2026-09-19 明确要求
 *  "语音命令和 beep / command / speech 应该是包含不是并列"。小节名与顺序见 SET_SUB_NAMES。
 */
function renderGroupBody(grp, items) {
  const subbed = items.filter((s) => s.sub);
  if (!subbed.length) return items.map((s) => renderSettingRow(s)).join("");
  const bySub = {};
  subbed.forEach((s) => { (bySub[s.sub] = bySub[s.sub] || []).push(s); });
  const collapsedSubs = _collapsedSubs();
  // 已知小节按声明的流程顺序；未知小节（后端口径变了）排到末尾，不隐藏任何设置项
  const order = SET_SUB_ORDER.filter((k) => bySub[k]);
  const extra = Object.keys(bySub).filter((k) => !SET_SUB_ORDER.includes(k));
  const plain = items.filter((s) => !s.sub);          // 没归小节的老项：仍平铺在末尾
  return [...order, ...extra].map((k) => {
    const stateKey = grp + "/" + k;
    const isCollapsed = collapsedSubs.has(stateKey);
    return `<div class="set-subgroup${isCollapsed ? " collapsed" : ""}" data-sub="${esc(stateKey)}">
      <div class="set-sub-title" role="button" tabindex="0" aria-expanded="${!isCollapsed}">
        <span class="set-arrow">▶</span>
        <span>${esc(SET_SUB_NAMES[k] || k)}</span>
        <span class="set-count">${bySub[k].length}</span>
      </div>
      <div class="set-sub-body">${bySub[k].map((s) => renderSettingRow(s)).join("")}</div>
    </div>`;
  }).join("") + plain.map((s) => renderSettingRow(s)).join("");
}

/** 二级小节折叠/展开。 */
function toggleSetSub(titleEl) {
  const box = titleEl.closest(".set-subgroup");
  if (!box) return;
  const key = box.dataset.sub;
  const nowCollapsed = box.classList.toggle("collapsed");
  titleEl.setAttribute("aria-expanded", String(!nowCollapsed));
  const collapsed = _collapsedSubs();
  if (nowCollapsed) collapsed.add(key); else collapsed.delete(key);
  _saveCollapsedSubs(collapsed);
}

$("#settingsForm").addEventListener("click", (e) => {
  const sub = e.target.closest(".set-sub-title");
  if (sub) { toggleSetSub(sub); return; }
  const title = e.target.closest(".set-group-title");
  if (title) toggleSetGroup(title);
});
$("#settingsForm").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const sub = e.target.closest(".set-sub-title");
  if (sub) { e.preventDefault(); toggleSetSub(sub); return; }
  const title = e.target.closest(".set-group-title");
  if (title) { e.preventDefault(); toggleSetGroup(title); }
});
/* 全部折叠 / 全部展开（顶部双箭头图标按钮：方向表示点下去会发生什么，
   悬停说明也跟着变；2026-09-13 用户要求把原来的"折叠/展开"文字按钮换成双箭头） */
function _syncSettingsCollapseAll() {
  const btn = $("#btnSettingsCollapseAll");
  if (!btn) return;
  const anyOpen = $$("#settingsForm .set-group").some((b) => !b.classList.contains("collapsed"));
  btn.classList.toggle("unfold", !anyOpen);
  const label = anyOpen ? "折叠全部分组" : "展开全部分组";
  btn.title = label;
  btn.setAttribute("aria-label", label);
}
$("#btnSettingsCollapseAll")?.addEventListener("click", () => {
  const boxes = $$("#settingsForm .set-group");
  const anyOpen = boxes.some((b) => !b.classList.contains("collapsed"));
  const collapsed = _collapsedGroups();
  const subs = _collapsedSubs();
  boxes.forEach((b) => {
    b.classList.toggle("collapsed", anyOpen);
    b.querySelector(".set-group-title")?.setAttribute("aria-expanded", String(!anyOpen));
    if (anyOpen) collapsed.add(b.dataset.grp); else collapsed.delete(b.dataset.grp);
  });
  // 二级小节跟着一起收/放：否则"全部折叠"后再点开某个分组，里面还是一片展开的项
  $$("#settingsForm .set-subgroup").forEach((b) => {
    b.classList.toggle("collapsed", anyOpen);
    b.querySelector(".set-sub-title")?.setAttribute("aria-expanded", String(!anyOpen));
    if (anyOpen) subs.add(b.dataset.sub); else subs.delete(b.dataset.sub);
  });
  _saveCollapsedGroups(collapsed);
  _saveCollapsedSubs(subs);
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

$("#btnRestartEcho").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
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

/** 引擎值 → 模型清单 id（whisper 档位前缀 whisper-；large → large-v3）。 */
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

/** 六个功能卡片的数据模型。 */
function modelFunctions() {
  const stt = settingByKey("sttModel");
  const mstt = settingByKey("meetingSttModel");
  const wake = settingByKey("wakeEngine");
  const dev = settingByKey("device");
  const diar = settingByKey("meetingDiarize");
  return [
    { id: "stt", icon: "🎤", name: "命令转写", settingKey: "sttModel", options: stt && stt.options,
      value: stt && stt.value, catalogId: engineModelId(stt && stt.value) },
    { id: "mstt", icon: "📝", name: "会议转写", settingKey: "meetingSttModel", options: mstt && mstt.options,
      value: mstt && mstt.value, catalogId: engineModelId(mstt && mstt.value) },
    { id: "wake", icon: "🔔", name: "唤醒", settingKey: "wakeEngine", options: wake && wake.options,
      value: wake && wake.value, catalogId: "kws" },
    { id: "diar", icon: "👥", name: "说话人分离", toggleKey: "meetingDiarize",
      value: !!(diar && diar.value), catalogId: "pyannote" },
    { id: "vp", icon: "🧬", name: "声纹", special: "voiceprint" },
    { id: "dev", icon: "💻", name: "计算设备", settingKey: "device", options: dev && dev.options,
      value: dev && dev.value, special: "device" },
  ];
}

/* 状态：ok=就绪 / miss=红色（转写引擎缺依赖，会失败）/ warn=黄色（可选模型未装）/ idle=未启用 */
function _loadState(f) {
  if (f.special === "device") return { kind: "ok", text: "就绪" };
  if (f.special === "voiceprint") {
    const on = !!(settingByKey("voiceprintEnabled") || {}).value;
    return { kind: on ? "ok" : "idle", text: on ? "已开启" : "未开启" };
  }
  const m = modelById(f.catalogId);
  if (!m) return { kind: "idle", text: "—" };
  if (m.ready) return { kind: "ok", text: "就绪" };
  const critical = f.id === "stt" || f.id === "mstt";   // 转写引擎缺失会直接导致失败
  return critical ? { kind: "miss", text: "未就绪" } : { kind: "warn", text: "未安装" };
}


/** 单张功能卡。 */
function renderModelCard(f) {
  let badge = "", cls = "", body = "";

  if (f.special === "voiceprint") {
    const on = !!(settingByKey("voiceprintEnabled") || {}).value;
    const auto = !!(settingByKey("voiceprintAutoEnroll") || {}).value;
    const thr = settingByKey("voiceprintThreshold");
    const mar = settingByKey("voiceprintMargin");
    const count = (_vpCache && Array.isArray(_vpCache.items)) ? _vpCache.items.length : 0;
    badge = modelBadge(on ? "已开启" : "未开启", on ? "ok" : "idle");
    body = `<label class="mcard-sw"><input type="checkbox" data-mbool="voiceprintEnabled" ${on ? "checked" : ""}><span>启用声纹识别</span></label>
      <label class="mcard-sw"><input type="checkbox" data-mbool="voiceprintAutoEnroll" ${auto ? "checked" : ""}><span>改名时自动入库</span></label>
      <div class="mcard-nums">
        <label>匹配阈值<input type="number" step="0.01" class="ctl" data-mnum="voiceprintThreshold" value="${esc(thr ? thr.value : 0.65)}"></label>
        <label>歧义间隔<input type="number" step="0.01" class="ctl" data-mnum="voiceprintMargin" value="${esc(mar ? mar.value : 0.05)}"></label>
      </div>
      <div class="mcard-meta">声纹库：<b>${count}</b> 位联系人（在会议详情里把说话人改名成联系人即入库）</div>`;
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
    if (f.toggleKey) {
      control += `<label class="mcard-sw"><input type="checkbox" data-mbool="${esc(f.toggleKey)}" ${f.value ? "checked" : ""}><span>启用（录音时区分说话人）</span></label>`;
    }
    const warn = (st.kind === "miss")
      ? `<div class="mcard-warn">⚠ 所选模型未就绪，现在用它转写会失败</div>` : "";
    const failMsg = failed
      ? `<div class="mcard-warn">下载失败：${esc(job.message || "未知原因")}</div>` : "";
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


function renderSettingRow(s) {
  const id = "set-" + s.key;
  let ctl = "";
  if (s.secret) {
    // 密钥（P5 凭据管理）：服务端在出口把它遮成空串，所以这里**必须**是密码框 + 空值，
    // 并明确"留空 = 不改"；要清空走旁边的「清除」按钮（送 __clear__ 哨兵）。
    // 否则整批保存会把空串当新值，把用户的密钥静默清掉。
    // 输入框与「清除」必须同一行：`.set-row` 是纵向布局，所以要自己套一层 flex。
    // （2026-09-19 用户实测反馈：原来的按钮掉到了输入框下面、还渲染成一个大白块。）
    ctl = `<div style="display:flex;gap:6px;align-items:center">
             <input type="password" class="ctl" id="${id}" data-key="${s.key}" data-secret="1"
               value="" autocomplete="new-password" style="flex:1"
               placeholder="${s.hasValue ? "已配置（留空 = 不改）" : "未配置"}">
             <button type="button" class="btn" data-clear-secret="${s.key}"
               style="flex:0 0 auto;padding:2px 8px;font-size:12px"
               title="清空这个密钥">清除</button>
           </div>`;
  } else if (s.value_type === "bool") {
    ctl = `<input type="checkbox" class="ctl" id="${id}" data-key="${s.key}" ${s.value ? "checked" : ""}>`;
  } else if (s.options && s.options.length) {
    ctl = `<select class="ctl" id="${id}" data-key="${s.key}">` +
      s.options.map((o) => `<option value="${esc(o)}" ${String(o) === String(s.value) ? "selected" : ""}>${esc(o)}</option>`).join("") +
      `</select>`;
  } else if (s.value_type === "int" || s.value_type === "float") {
    ctl = `<input type="number" step="${s.value_type === "float" ? "any" : "1"}" class="ctl" id="${id}" data-key="${s.key}" value="${esc(s.value)}">`;
  } else if (s.value_type === "list") {
    ctl = `<input class="ctl" id="${id}" data-key="${s.key}" value="${esc((s.value || []).join(","))}" placeholder="逗号分隔">`;
  } else {
    ctl = `<input class="ctl" id="${id}" data-key="${s.key}" value="${esc(s.value)}">`;
  }
  return `<div class="set-row">
    <label for="${id}">${esc(s.label || s.key)}</label>
    ${ctl}
    <div class="desc">${esc(s.description || "")}</div>
  </div>`;
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

$("#btnSettingsSave").addEventListener("click", async () => {
  const values = {};
  $$("#settingsForm [data-key]").forEach((el) => {
    const key = el.dataset.key;
    const meta = _settingsCache.find((s) => s.key === key);
    if (!meta) return;
    if (meta.secret) {
      // 密钥：**只在这轮真的输入了新值时才提交**（空 = 不改）。
      // 服务端另有同样的闸（空串不改、清除走 __clear__），这里是第一道。
      if (el.value && el.value.trim()) values[key] = el.value;
      return;
    }
    if (meta.value_type === "bool") values[key] = el.checked;
    else if (meta.value_type === "list") values[key] = el.value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
    else if (meta.value_type === "int") values[key] = parseInt(el.value, 10) || 0;
    else if (meta.value_type === "float") values[key] = parseFloat(el.value) || 0;
    else values[key] = el.value;
  });
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
  try {
    const r = await api("/api/settings", { method: "PUT", body: JSON.stringify({ values }) });
    toast("已保存 " + Object.keys(r.updated).length + " 项");
    Object.keys(_agentDirty).forEach((k) => delete _agentDirty[k]);
    await loadAgents(false);
    renderAgentTable();
  } catch (e) { toast("保存失败：" + e.message); }
});

/* ================= 历史 ================= */

/* 命令 → DSH 会话：DSH 的 Web UI 没有"按会话直达"的 URL（实测前端不解析任何 URL 参数，
   也没有自定义协议），所以跳不过去；改成在 ECHO 里就地看——标题来自 /api/dsh/targets，
   内容按需读 /api/commands/{id}/session（后端用签名 Cookie 走 session/page）。 */
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

/* 会议状态文案：error 在会议语境里是「录音失败（没录到音频）」，比通用的「错误」更能说明问题 */
const MEETING_STATUS_TEXT = { recording: "录音中", transcribing: "转写中", transcribed: "已转写",
  error: "录音失败", interrupted: "已中断" };

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
    // 录音失败（PR #11 起会明确标 error）：给一句能行动的说明，而不是只有「错误」两个字
    const errHint = m.status === "error"
      ? `<div class="m-meta" style="color:var(--red)">没录到音频：麦克风没打开（被占用/权限）或全程无声；换设备后重试，详见 启动 → 日志</div>`
      : "";
    return `<div class="meeting-item" data-id="${m.id}">
      <span class="badge ${meetingBadgeCls(m.status)}">${MEETING_STATUS_TEXT[m.status] || STATUS_TEXT[m.status] || m.status}</span>
      <div class="grow">
        ${nameHtml}
        <div class="m-meta">${esc(started)} · ${fmtHM(dur)} · ${m.segments || 0} 段 ${hasSummary}</div>
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

/* ================= 启动 ================= */
let _bootSettings = [];

function bootBadgeCls(status) {
  if (status === "online" || status === "active") return "online";
  if (status === "failed") return "error";
  if (status === "starting" || status === "running") return "running";
  if (status === "disabled") return "disabled";
  if (status === "idle") return "idle";
  return "idle";
}

function _settingMeta(key) {
  return _bootSettings.find((s) => s.key === key) || {};
}

function _settingValue(key) {
  return _settingMeta(key).value;
}

async function loadBoot() {
  try {
    const [bs, sr, st] = await Promise.all([
      api("/api/boot/status"), api("/api/settings"), api("/api/status")]);
    _bootSettings = sr.settings;
    renderBoot(bs);
    renderLiveStatus(st);            // 在线时长 + 状态（启动日志标题右侧）
  } catch (e) {
    $("#bootSummary").textContent = "加载失败：" + e.message;
    renderLiveStatus(null);
  }
}

function renderBoot(bs) {
  const s = bs.summary;
  $("#bootSummary").textContent =
    `就绪 ${s.ready}/${s.total} · 失败 ${s.failed} · 进行中 ${s.running}`;
  const sttOpts = _settingMeta("sttModel").options || [];
  const meetOpts = _settingMeta("meetingSttModel").options || [];
  const ttsEngine = _settingValue("ttsEngine");
  $("#bootComponents").innerHTML = bs.components.map((c) => {
    const cls = bootBadgeCls(c.status);
    const bar = c.status === "starting"
      ? `<div class="boot-bar"><i style="width:${Math.max(4, Math.round(c.progress * 100))}%"></i></div>` : "";
    let ctl = "";
    if (c.id === "stt-cmd" || c.id === "stt-meeting") {
      const opts = c.id === "stt-cmd" ? sttOpts : meetOpts;
      const cur = c.id === "stt-cmd" ? _settingValue("sttModel") : _settingValue("meetingSttModel");
      ctl = `<select class="ctl boot-model" data-model="${c.id}">` +
        opts.map((o) => `<option value="${esc(o)}" ${String(o) === String(cur) ? "selected" : ""}>${esc(o)}</option>`).join("") + `</select>`;
    } else if (c.id === "tts") {
      const online = ttsEngine !== "sapi";
      ctl = `<label class="boot-switch"><input type="checkbox" data-ttsonline ${online ? "checked" : ""}>
        <span>${online ? "在线" : "离线"}</span></label>`;
    }
    const btns = [];
    if (c.can_start) btns.push(`<button class="btn mini" data-boot="${c.id}:start">${c.status === "failed" ? "重试" : "启动"}</button>`);
    if (c.can_stop) btns.push(`<button class="btn mini danger" data-boot="${c.id}:stop">停止</button>`);
    const sub = c.substep ? ` · <span class="boot-sub">${esc(c.substep)}</span>` : "";
    const dur = c.duration ? `<span class="boot-dur">${c.duration}s</span>` : "";
    // 没有控件就不渲染 boot-ctl：模板里那个空 span 在窄边条的网格布局下会多占一行（含行间距）
    const ctlHtml = (ctl || btns.length)
      ? `<span class="boot-ctl">${ctl} ${btns.join("")}</span>` : "";
    return `<div class="boot-row" data-cid="${c.id}">
      <span class="boot-ic">${c.icon}</span>
      <span class="boot-name">${esc(c.label)}</span>
      <span class="badge ${cls}">${STATUS_TEXT[c.status] || c.status}</span>
      <span class="boot-detail">${esc(c.detail)}${sub}</span>
      ${dur}
      ${bar}
      ${ctlHtml}
    </div>`;
  }).join("");
  // 事件
  $$("#bootComponents .boot-model").forEach((sel) => sel.addEventListener("change", async (e) => {
    const key = e.currentTarget.dataset.model === "stt-cmd" ? "sttModel" : "meetingSttModel";
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: { [key]: e.currentTarget.value } }) });
      toast("已切换模型，重新加载中…");
      await post(`/api/boot/component/${e.currentTarget.dataset.model}/start`, {});
      loadBoot();
    } catch (err) { toast("切换失败：" + err.message); }
  }));
  $$("#bootComponents [data-ttsonline]").forEach((chk) => chk.addEventListener("change", async (e) => {
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify({ values: { ttsEngine: e.currentTarget.checked ? "edge-tts" : "sapi" } }) });
      toast("已切换 TTS 模式");
      loadBoot();
    } catch (err) { toast("切换失败：" + err.message); }
  }));
  $$("#bootComponents [data-boot]").forEach((btn) => btn.addEventListener("click", async (e) => {
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
const _VIEWS = ["dashboard", "settings", "history", "meetings", "boot", "failover", "capabilities"];
let _bootView = "dashboard";
try {
  const q = new URLSearchParams(location.search).get("view");
  const want = q || localStorage.getItem("echo.gotoView");
  if (localStorage.getItem("echo.gotoView")) localStorage.removeItem("echo.gotoView");
  if (want && _VIEWS.includes(want)) _bootView = want;
} catch (e) { /* 忽略 */ }
switchView(_bootView);
applyCollapsedCards();      // 应用上次的卡片折叠状态（「模型路由 → 路由参数」等）

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
    _settingsCache = r.settings || [];
  } catch (e) { /* 取不到就按默认 3 秒，不影响其它功能 */ }
}
loadPanelPrefs();

setInterval(() => {
  if (!_panelRefreshDue()) return;
  const v = $(".tab.active");
  if (v && v.dataset.view === "dashboard") refreshDashboard();
  else if (v && v.dataset.view === "boot") { loadBoot(); loadBootLogs(); }
  else if (v && v.dataset.view === "failover" && !_rtDirty && !_rtBusy()) loadRouter();
}, _PANEL_TICK_MS);
// 转写进度轮询（会议列表进度条）
setInterval(pollTranscribe, 2000);
