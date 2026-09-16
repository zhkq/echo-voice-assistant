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
  if (name === "settings") { loadSettings(); loadModelList(); }
  if (name === "history") loadHistory();
  if (name === "meetings") { loadMeetings(); refreshMeetingHeader(); }
  if (name === "boot") { loadBoot(); loadBootLogs(); loadGuardLogs(); }
  if (name === "failover") loadRouter();
}
$$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));

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
  } catch (e) { toast("加载模型路由失败：" + e.message); }
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
    </div>`;
  }).join("");

  $$("[data-expand]", el).forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();   // 别被算成"整条点击"（仪表盘上整条是跳转）
    toggleReply(a);
  }));
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

async function loadTargets() {
  try {
    _targets = await api("/api/dsh/targets");
  } catch (e) {
    _targets = { workspaces: [], sessions: [] };
  }
  const ws = $("#wsSelect");
  if (!ws) return;
  const prevWs = ws.dataset.val || "";
  ws.innerHTML = `<option value="">默认（ECHO 固定会话）</option>` +
    _targets.workspaces.map((w) =>
      `<option value="${esc(w)}">${esc(shortName(w))}</option>`).join("");
  ws.dataset.val = prevWs || ws.value || "";
  renderSessSelect();
}

function renderSessSelect() {
  const wsSel = $("#wsSelect");
  const ss = $("#sessSelect");
  if (!wsSel || !ss) return;
  const ws = wsSel.value;
  const list = ws ? (_targets.sessions || []).filter((s) => s.cwd === ws) : [];
  const prev = ss.dataset.val || "";
  ss.innerHTML = `<option value="">自动（该工作区最近对话 / 新建）</option>` +
    list.map((s) =>
      `<option value="${esc(s.sessionId)}">${esc(s.title || shortName(s.sessionId))}${s.running ? " ●" : ""}</option>`).join("");
  ss.dataset.val = prev || ss.value || "";
}
$("#wsSelect").addEventListener("change", renderSessSelect);

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

/* 分组展示顺序 = 业务相关性（与后端 grp 取值解耦，后端不因展示顺序而改动）：
   智能体(决定谁干活) → 通用(基础) → 语音命令(主用法) → 唤醒词 → 会议 → 纪要归档
   → 模型路由 → 面板(界面) → DSH(底层接入) */
const SET_GROUP_ORDER = ["agent", "general", "voice", "wake", "meeting", "worklog", "router", "panel", "dsh"];
const SET_GROUP_NAMES = { agent: "智能体", general: "通用", voice: "语音命令", wake: "唤醒词",
  meeting: "会议", worklog: "纪要归档", router: "模型路由", panel: "面板", dsh: "DSH 服务" };
/* 默认展开；用户折叠过的分组记在 localStorage，刷新/重开面板后保持 */
const SET_COLLAPSE_KEY = "echo.settings.collapsedGroups";

function _collapsedGroups() {
  try { return new Set(JSON.parse(localStorage.getItem(SET_COLLAPSE_KEY) || "[]")); }
  catch (e) { return new Set(); }
}
function _saveCollapsedGroups(set) {
  try { localStorage.setItem(SET_COLLAPSE_KEY, JSON.stringify([...set])); } catch (e) { /* 忽略 */ }
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

/** 当前选中智能体的展开设置内容。 */
function agentDetailHtml() {
  const cur = _agentsCache.find((a) => a.active);
  if (!cur) {
    return `<div class="agent-detail-head">没有选中的智能体 —— 打开上表中任意一个开关即可启用</div>`;
  }
  const probeBtn = `<button type="button" class="btn" data-agent-probe="${esc(cur.name)}"
      title="重新探测该智能体是否可用">检测</button>`;
  const fields = [];
  if (cur.name === "codebuddy") {
    const v = _agentDirty.agentCustomPath !== undefined
      ? _agentDirty.agentCustomPath : (settingsValue("agentCustomPath") || "");
    fields.push(`<div class="agent-field">
      <label for="agent-custom-path">CLI 路径</label>
      <input class="ctl" id="agent-custom-path" data-agent-field="agentCustomPath"
             value="${esc(v)}" placeholder="留空 = 自动探测（PATH → WorkBuddy 内置目录）">
      <div class="desc">仅当自动探测失败时才需要手填可执行文件路径</div>
    </div>`);
  }
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

/** 取某个配置项的当前值（展开区输入框回填用）。 */
function settingsValue(key) {
  const s = _settingsCache.find((x) => x.key === key);
  return s ? s.value : "";
}

/** 拉取智能体可用性；probe=true 时做重探测（会真的执行 CLI 探测）。 */
async function loadAgents(probe = false) {
  try {
    const r = await api("/api/agents" + (probe ? "?probe=1" : ""));
    _agentsCache = r.agents || [];
    return r;
  } catch (e) { return null; }
}

/* ---------------- 设置 → 智能体：开关即单选，切换立即保存 ---------------- */
$("#settingsForm").addEventListener("change", async (e) => {
  const toggle = e.target.closest("[data-agent-toggle]");
  if (toggle) {
    const name = toggle.dataset.agentToggle;
    const cur = _agentsCache.find((a) => a.active);
    if (cur && cur.name === name) return;                 // 点的就是当前项
    try {
      await api("/api/settings", { method: "PUT",
        body: JSON.stringify({ values: { agentBackend: name } }) });
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

async function loadSettings() {
  try {
    const r = await api("/api/settings");
    _settingsCache = r.settings;
    _agentsCache = r.agents || [];
    const groups = {};
    r.settings.forEach((s) => { (groups[s.grp] = groups[s.grp] || []).push(s); });
    // 「智能体」分组的配置项都是 hidden（不进 settings），这里补一个空分组占位
    if (!groups.agent) groups.agent = [];
    // 已知分组按业务相关性排序，未知分组排到末尾（保持出现顺序）
    const known = SET_GROUP_ORDER.filter((g) => groups[g]);
    const extra = Object.keys(groups).filter((g) => !SET_GROUP_ORDER.includes(g));
    const collapsed = _collapsedGroups();
    const form = $("#settingsForm");
    form.innerHTML = [...known, ...extra].map((g) => {
      const items = groups[g];
      const isCollapsed = collapsed.has(g);
      const no = g === "agent" ? (_agentsCache.filter((a) => a.active).length || 0) : items.length;
      const body = g === "agent"
        ? `<div id="agentTable" class="agent-table-wrap"></div>`
        : items.map((s) => renderSettingRow(s)).join("");
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
    _syncSettingsCollapseAll();     // 重绘后让顶部双箭头跟着当前折叠状态
  } catch (e) { toast("加载设置失败：" + e.message); }
}

/* 分组折叠/展开（事件委托，重绘后无需重新绑定） */
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
$("#settingsForm").addEventListener("click", (e) => {
  const title = e.target.closest(".set-group-title");
  if (title) toggleSetGroup(title);
});
$("#settingsForm").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
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
  boxes.forEach((b) => {
    b.classList.toggle("collapsed", anyOpen);
    b.querySelector(".set-group-title")?.setAttribute("aria-expanded", String(!anyOpen));
    if (anyOpen) collapsed.add(b.dataset.grp); else collapsed.delete(b.dataset.grp);
  });
  _saveCollapsedGroups(collapsed);
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

/* ---------------- 设置 → 模型：清单 + 下载 ----------------
   清单来自 /api/models（app/modelinfo.py）：显示名可随便起，但"落地路径"是代码约定、
   改了加载器就找不到模型，所以路径在界面上原样展示、不翻译。
   pyannote 仅提供可复制的下载命令；source=copy 的模型保留拷贝说明。 */
let _modelJobs = {};
let _modelPoll = null;

function fmtMb(mb) {
  if (!mb) return "0 MB";
  return mb >= 1024 ? (mb / 1024).toFixed(1) + " GB" : mb + " MB";
}

function renderModelList(items, jobs) {
  const el = $("#modelList");
  if (!el) return;
  _modelJobs = (jobs && jobs.items) || {};
  const ready = items.filter((m) => m.ready).length;
  $("#modelSummary").textContent = `已就绪 ${ready}/${items.length}`;
  el.innerHTML = items.map((m) => {
    const job = _modelJobs[m.id] || {};
    const running = job.status === "running";
    const failed = job.status === "failed";
    const downloadable = m.source !== "copy" && m.downloadable !== false;
    const badge = running ? `<span class="badge running">下载中 ${job.percent || 0}%</span>`
      : (m.ready ? `<span class="badge online">${m.id === "pyannote" ? "模型已下载" : "已就绪"}</span>` : `<span class="badge idle">未安装</span>`);
    const size = `${m.size}${m.local_mb ? `（本地 ${fmtMb(m.local_mb)}）` : ""}`;
    const btns = [];
    if (downloadable) {
      btns.push(`<button class="btn mini" data-dl="${esc(m.id)}" data-force="${m.ready ? "1" : "0"}" `
        + `${running || (_modelJobs.__active && !failed) ? "disabled" : ""}>`
        + (running ? `下载中 ${job.percent || 0}%` : (failed ? "重试" : (m.ready ? "重新下载" : esc(m.download_label || "下载")))) + `</button>`);
    }
    if (m.cmd) btns.push(`<button class="btn mini" data-copy="${esc(m.cmd)}">${esc(m.cmd_label || "复制命令")}</button>`);
    for (const link of (m.links || [])) {
      if (link.url.startsWith("https://huggingface.co/"))
        btns.push(`<a class="btn mini" href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)}</a>`);
    }
    if (m.source === "copy") btns.push(`<button class="btn mini" data-copy="${esc(m.target)}">复制目标路径</button>`);
    return `<div class="model-row${m.ready ? " ok" : ""}">
      <div class="m-head"><span class="m-name">${esc(m.name)}</span>${badge}</div>
      <div class="m-meta">${esc(m.purpose)} · ${esc(size)}</div>
      <div class="m-path" title="落地路径（代码约定，不要改名）">${esc(m.target)}</div>
      <div class="m-how">${esc(m.how)}</div>
      ${m.cmd_label === "复制下载命令" ? `<details class="m-how"><summary>查看下载命令</summary><pre style="white-space:pre-wrap;overflow-wrap:anywhere;user-select:text">${esc(m.cmd)}</pre></details>` : ""}
      ${running ? `<div class="m-bar"><i style="width:${Math.max(3, job.percent || 0)}%"></i></div>` : ""}
      ${failed ? `<div class="m-how" style="color:var(--red)">${esc(job.message || "下载失败")}</div>` : ""}
      <div class="m-actions">${btns.join("")}</div>
    </div>`;
  }).join("");

  $$("#modelList [data-dl]").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    try {
      const r = await post("/api/models/download", { id: b.dataset.dl, force: b.dataset.force === "1" });
      toast(r.message);
    } catch (e) { toast("下载请求失败：" + e.message); }
    loadModelList();
  }));
  $$("#modelList [data-copy]").forEach((b) => b.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(b.dataset.copy); toast("已复制"); }
    catch (e) { toast("复制失败，请手动选择文本"); }
  }));
}

async function loadModelList() {
  const el = $("#modelList");
  if (!el) return;
  try {
    const r = await api("/api/models");
    renderModelList(r.items || [], r.jobs || {});
    // 有任务在跑就持续刷新进度，跑完自动停
    const active = r.jobs && r.jobs.active;
    if (active && !_modelPoll) {
      _modelPoll = setInterval(loadModelList, 1500);
    } else if (!active && _modelPoll) {
      clearInterval(_modelPoll);
      _modelPoll = null;
      loadSettings();          // 下载完成后模型下拉的状态也可能变
    }
  } catch (e) {
    el.innerHTML = `<div class="empty">加载失败：${esc(e.message)}</div>`;
  }
}

function renderSettingRow(s) {
  const id = "set-" + s.key;
  let ctl = "";
  if (s.value_type === "bool") {
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

$("#btnSettingsSave").addEventListener("click", async () => {
  const values = {};
  $$("#settingsForm [data-key]").forEach((el) => {
    const key = el.dataset.key;
    const meta = _settingsCache.find((s) => s.key === key);
    if (!meta) return;
    if (meta.value_type === "bool") values[key] = el.checked;
    else if (meta.value_type === "list") values[key] = el.value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
    else if (meta.value_type === "int") values[key] = parseInt(el.value, 10) || 0;
    else if (meta.value_type === "float") values[key] = parseFloat(el.value) || 0;
    else values[key] = el.value;
  });
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
async function loadHistory() {
  try {
    const r = await api("/api/commands?limit=200");
    renderCmdList($("#historyList"), r.items, true);
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
    return `<div class="meeting-item" data-id="${m.id}">
      <span class="badge ${meetingBadgeCls(m.status)}">${STATUS_TEXT[m.status] || m.status}</span>
      <div class="grow">
        ${nameHtml}
        <div class="m-meta">${esc(started)} · ${fmtHM(dur)} · ${m.segments || 0} 段 ${hasSummary}</div>
        ${txHtml}
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

async function loadGuardLogs() {
  try {
    const r = await api("/api/logs?source=guard&limit=60");
    const el = $("#guardLogs");
    const items = r.items || [];
    el.innerHTML = items.length
      ? items.map((l) => `<div class="boot-log-line"><span class="muted">${esc(l.ts || "")}</span> <span class="lv-${l.level}">${esc(l.message || "")}</span></div>`).join("")
      : `<div class="empty">暂无守护进程关键事件（echo-host 尚未上报）</div>`;
    el.scrollTop = 0;
  } catch (e) { /* ignore */ }
}

// PWA：注册 Service Worker（可安装为独立窗口应用）
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
initRouterUI();
switchView("dashboard");
setInterval(() => {
  const v = $(".tab.active");
  if (v && v.dataset.view === "dashboard") refreshDashboard();
  else if (v && v.dataset.view === "boot") { loadBoot(); loadBootLogs(); loadGuardLogs(); }
  else if (v && v.dataset.view === "failover" && !_rtDirty && !_rtBusy()) loadRouter();
}, 2000);
// 转写进度轮询（会议列表进度条）
setInterval(pollTranscribe, 2000);
