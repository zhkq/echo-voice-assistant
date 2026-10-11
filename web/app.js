function onboardCard() {
  // 2026-10-11（用户第 1 条反馈）：向导必须是**整页、且挡住整个 ECHO** 的覆盖层 ——
  // 原来它只是「设置」里的一张卡：既会被设置页重绘冲掉，用户也能绕过它。
  // 现在：fixed 全屏 + 锁滚动 + 只有最后一步的「完成」能出去（每步仍可跳过）。
  let ov = $("#onboardOverlay");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "onboardOverlay";
    ov.setAttribute("role", "dialog");
    ov.setAttribute("aria-modal", "true");
    ov.style.cssText = "position:fixed;inset:0;z-index:9999;background:rgba(20,22,26,.72);"
      + "display:flex;align-items:flex-start;justify-content:center;overflow:auto;padding:24px 12px";
    ov.innerHTML = '<div class="card" id="onboardCard" style="max-width:820px;width:100%">'
      + '<div class="card-title">首次启用向导<span class="sbadge">3 步</span></div>'
      + '<div class="card-body">'
      + '<div class="snote info"><span>ⓘ</span><span>第一次用 ECHO 按这三步走一遍：'
      + '<b>配模型</b> → <b>指笔记库</b> → <b>初始化技能</b>。'
      + '走完之前不能正式使用（每一步都可以跳过）。</span></div>'
      + '<div id="onboardPanel"></div></div></div>';
    document.body.appendChild(ov);
  }
  return $("#onboardCard");
}

function onboardShow() {
  const ov = $("#onboardOverlay");
  if (!ov) return;
  ov.style.display = "flex";
  document.body.style.overflow = "hidden";     // 锁滚动 = 挡住整个面板
  _onboardMinimized = false;
  const chip = $("#onboardResume");
  if (chip) chip.remove();
}

function onboardHide() {
  const ov = $("#onboardOverlay");
  if (ov) ov.style.display = "none";
  document.body.style.overflow = "";
  const chip = $("#onboardResume");
  if (chip) chip.remove();
}

/* 临时最小化去配智能体：藏起覆盖层 + 跳「设置 → 智能体」+ 右下角留一颗「继续」胶囊。 */
function onboardMinimize() {
  onboardHide();
  _onboardMinimized = true;
  switchView("settings");
  let chip = document.createElement("button");
  chip.id = "onboardResume";
  chip.className = "btn";
  chip.textContent = "⟵ 继续首次启用向导";
  chip.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:10000";
  chip.addEventListener("click", () => { onboardEnsureCard(); renderOnboard(); onboardShow(); });
  document.body.appendChild(chip);
}

function onboardEnsureCard() {
  // 2026-10-11（用户实测："向导页一晃而过就进入设置页面了"）：向导卡**建出来了**，
  // 但设置页随后重绘 / 重新应用折叠状态，把它冲掉了 —— 用户只看到一闪。
  // 这个函数幂等：卡在就只重画内容，不在就重建。**任何会重绘设置页的路径之后都要调它。**
  try { onboardCard(); renderOnboard(); } catch (e) { /* 建不出来就算了，别把面板弄挂 */ }
}

function gotoOnboard(step) {
  const card = onboardCard();
  if (typeof step === "number") _onboardStep = Math.max(0, Math.min(ONBOARD_STEPS.length - 1, step));
  renderOnboard();
  onboardShow();
  if (card && card.scrollIntoView) card.scrollIntoView({ behavior: "smooth", block: "start" });
}

function onboardStepBar() {
  return '<div class="srow"><div class="lbl"><span class="lt">第 ' + (_onboardStep + 1)
    + ' / ' + ONBOARD_STEPS.length + ' 步 · ' + esc(ONBOARD_STEPS[_onboardStep].name) + '</span></div>'
    + '<div class="sctl muted">' + ONBOARD_STEPS.map((s, i) => (i === _onboardStep ? '● ' : '○ ') + esc(s.name)).join("　") + '</div></div>';
}

function renderOnboard() {
  const box = $("#onboardPanel");
  if (!box) return;
  const back = _onboardStep > 0
    ? '<button class="btn ghost" id="onboardBack">上一步</button>' : "";
  box.innerHTML = onboardStepBar() + ONBOARD_STEPS[_onboardStep].draw()
    + '<div class="srow"><div class="lbl"></div><div class="sctl">' + back
    + '<button class="btn ghost" id="onboardSkip">跳过这一步</button></div></div>';
  if ($("#onboardBack")) $("#onboardBack").addEventListener("click", () => gotoOnboard(_onboardStep - 1));
  if ($("#onboardSkip")) $("#onboardSkip").addEventListener("click", () => {
    if (_onboardStep < ONBOARD_STEPS.length - 1) gotoOnboard(_onboardStep + 1); else onboardFinish();
  });
  if ($("#onboardGoAgent")) $("#onboardGoAgent").addEventListener("click", onboardMinimize);
  if ($("#onboardTestAgent")) $("#onboardTestAgent").addEventListener("click", onboardTestAgent);
  if ($("#onboardNextModel")) $("#onboardNextModel").addEventListener("click", () => gotoOnboard(1));
  if ($("#onboardPick")) $("#onboardPick").addEventListener("click", onboardPickFolder);
  if ($("#onboardVault")) $("#onboardVault").addEventListener("input", (e) => { _onboard.vault = e.target.value.trim(); });
  if ($("#onboardNextVault")) $("#onboardNextVault").addEventListener("click", onboardSaveVault);
  if ($("#onboardInstallSkills")) $("#onboardInstallSkills").addEventListener("click", onboardInstallSkills);
  if ($("#onboardFinish")) $("#onboardFinish").addEventListener("click", onboardFinish);
}

/* ---- S1：配模型（判据 = 真的让智能体答一句话）---- */
function onboardRenderModel() {
  const agent = _agentsCache && _agentsCache.find((a) => a.active);
  const now = agent ? (agent.displayName || agent.name) : "（还没选）";
  const chip = _onboard.modelOk
    ? '<div class="snote info"><span>✅</span><span>模型答话了：' + esc(_onboard.reply) + '</span></div>'
    : (_onboard.note ? '<div class="snote warn"><span>⚠</span><span>' + esc(_onboard.note) + '</span></div>' : "");
  return '<div class="srow"><div class="lbl"><span class="lt">当前智能体</span></div>'
    + '<div class="sctl">' + esc(now) + '　<button class="btn ghost" id="onboardGoAgent">去设置智能体</button></div></div>'
    + '<div class="snote info"><span>ⓘ</span><span>模型 key / 命令是在 <b>DSH 标准版</b>里配的'
    + '（或者用 DSH 桌面版，它自己带）。点下面这个按钮会<b>真的让模型答一句话</b>'
    + '（一次很短的调用，会花一点点 token）。</span></div>'
    + '<div class="srow"><div class="lbl"></div><div class="sctl">'
    + '<button class="btn" id="onboardTestAgent">测试模型是否可用</button>'
    + '<button class="btn ghost" id="onboardNextModel">下一步</button></div></div>' + chip;
}

async function onboardTestAgent() {
  const btn = $("#onboardTestAgent");
  if (btn) { btn.disabled = true; btn.textContent = "正在问模型…"; }
  try {
    const r = await post("/api/wizard/test-agent", {});
    _onboard.modelOk = !!(r && r.ok);
    _onboard.reply = (r && r.reply) || "";
    _onboard.note = (r && r.message) || "";
    _onboard.agent = (r && r.agent) || "";
  } catch (e) {
    _onboard.modelOk = false;
    _onboard.note = "测试没成功：" + (e && e.message ? e.message : e);
  }
  renderOnboard();
}

/* ---- S2：指笔记库（原生选择器 + 服务端校验）---- */
function onboardRenderVault() {
  const ok = _onboard.vaultIsVault;
  const chip = _onboard.vault
    ? (ok ? '<div class="snote info"><span>✅</span><span>这里就是一个 Obsidian 库（有 <code>.obsidian</code>）</span></div>'
          : '<div class="snote warn"><span>⚠</span><span>这个目录里没有 <code>.obsidian</code> —— 不是 Obsidian 库也能用，'
            + '我们会直接按目录放笔记；但如果你打算用 Obsidian，建议选到库根目录。</span></div>')
    : "";
  return '<div class="srow"><div class="lbl"><span class="lt">笔记库在哪</span>'
    + sHelp("ECHO 的每日回顾与纪要归档都往这里写。选 Obsidian 库的**根目录**（里面有 .obsidian 的那个）。") + '</div>'
    + '<div class="sctl"><input class="ctl" id="onboardVault" value="' + esc(_onboard.vault)
    + '" placeholder="例如 D:\\OneDrive\\文档\\我的库"></div></div>'
    + '<div class="srow"><div class="lbl"></div><div class="sctl">'
    + '<button class="btn ghost" id="onboardPick">浏览…</button>'
    + '<a class="btn ghost" href="' + ONBOARD_OBSIDIAN_URL + '" target="_blank" rel="noreferrer">还没装 Obsidian？去下载</a>'
    + '<button class="btn" id="onboardNextVault">下一步</button></div></div>'
    + '<div class="snote info"><span>ⓘ</span><span>「浏览…」只在**本机**弹窗（面板就在这台机器上）。'
    + '如果它打不开（没有图形会话 / 不是 Windows），直接把路径粘到上面那个框里也一样。</span></div>' + chip;
}

async function onboardPickFolder() {
  const btn = $("#onboardPick");
  if (btn) { btn.disabled = true; btn.textContent = "等你选…"; }
  try {
    const r = await post("/api/dialog/pick-folder", { title: "选你的 Obsidian 笔记库（库根目录）", initial: _onboard.vault });
    if (r && r.ok && r.path) {
      _onboard.vault = r.path;
      await onboardCheckVault(r.path);
    } else if (r && r.cancelled) {
      _onboard.note = "";                      // **取消不是失败**：什么都不说，继续让用户手敲
    } else {
      _onboard.note = (r && r.message) || "选择器打不开，直接粘贴路径即可";
    }
  } catch (e) {
    _onboard.note = "选择器打不开（" + (e && e.message ? e.message : e) + "）—— 直接粘贴路径即可";
  }
  renderOnboard();
}

async function onboardCheckVault(path) {
  try {
    const r = await post("/api/dialog/check-vault", { path: path });
    _onboard.vaultIsVault = !!(r && r.isVault);
    if (r && !r.ok) _onboard.note = r.message || "这个路径看起来不对";
  } catch (e) { /* 校验失败不挡路 */ }
}

async function onboardSaveVault() {
  const el = $("#onboardVault");
  const path = ((el && el.value) || _onboard.vault || "").trim();
  if (!path) { _onboard.note = "先填上笔记库路径（或点「浏览…」）"; renderOnboard(); return; }
  _onboard.vault = path;
  await onboardCheckVault(path);
  try {
    await post("/api/settings", { values: { worklogVaultRoot: path, dailyReviewVaultRoot: path } });
    _onboard.note = "";
    gotoOnboard(2);
    return;
  } catch (e) {
    _onboard.note = "保存失败：" + (e && e.message ? e.message : e);
  }
  renderOnboard();
}

/* ---- S3：初始化技能 ---- */
function onboardRenderSkills() {
  const s = _onboard.skills;
  let chip = "";
  if (s) {
    const parts = [];
    if ((s.installed || []).length) parts.push("已装 " + s.installed.length + " 项");
    if ((s.skipped || []).length) parts.push("保留你已有的 " + s.skipped.length + " 个（"
      + s.skipped.map((x) => String(x).split("@")[0]).join("、") + "）");
    if ((s.others || []).length) parts.push("你库里还有 " + s.others.length
      + " 个技能不在本向导清单内：" + s.others.join("、"));
    if ((s.absent || []).length) parts.push("包里缺 " + s.absent.join("、"));
    chip = '<div class="snote ' + (s.ok ? "info" : "warn") + '"><span>' + (s.ok ? "✅" : "⚠")
      + '</span><span>' + esc(parts.join("；") || "没有需要装的") + '</span></div>';
  }
  return '<div class="snote info"><span>ⓘ</span><span>三个技能直接决定 ECHO 能不能干活：'
    + '<b>开启录音</b>、<b>每日回顾</b>、<b>会议归档</b>。'
    + '装完它们就在你的 DSH 家目录里；<b>你自己改过的同名技能不会被覆盖</b>。'
    + '每人的登记规则不同 —— 技能<b>第一次用到时会问你一句</b>，然后记进 '
    + '<code>&lt;笔记库&gt;/.echo/profile.md</code>。</span></div>'
    + '<div class="srow"><div class="lbl"></div><div class="sctl">'
    + '<button class="btn" id="onboardInstallSkills">初始化这三个技能</button>'
    + '<button class="btn ghost" id="onboardFinish">完成，开始用</button></div></div>' + chip;
}

async function onboardInstallSkills() {
  const btn = $("#onboardInstallSkills");
  if (btn) { btn.disabled = true; btn.textContent = "正在装…"; }
  try {
    _onboard.skills = await post("/api/skills/setup", {});
  } catch (e) {
    _onboard.skills = { ok: false, absent: ["（装失败了：" + (e && e.message ? e.message : e) + "）"] };
  }
  renderOnboard();
}

async function onboardFinish() {
  try { await post("/api/wizard/first-run", {}); } catch (e) { /* 登记失败不挡人 */ }
  _onboardNeeded = false;                       // 走完了：不再挡
  onboardHide();
  switchView("dashboard");
  renderInstallNotice();
}

async function bootView() {
  // 2026-10-11（用户实测："装完没看到向导，没法走那三步"）：**把向导卡常驻在「设置」里**。
  // 自动进入可能因为三种原因不发生 —— 显式深链（`?view=…` 按设计优先）、浏览器缓存了旧
  // app.js、或判据判成升级；而**装得完整时顶部横幅是隐藏的**，于是用户连手动入口都没有。
  // 这里在首屏就把卡建出来并渲染（设置页里可见、可点），任何情况下都能走到这三步。
  onboardEnsureCard();
  // 2026-10-11（用户口径）：**首次安装**进「首次启用向导」（配 DSH/模型 key、笔记库、
  // 三个技能）；**升级安装**直接进工作状态，向导入口只留在设置里。
  // 判据**只问服务端**（`/api/wizard/first-run` 的 `shouldOnboard` = `install_state.onboarding()`）——
  // 前端再算一遍必然与它漂移（2026-09-21 那次"技能装完还进向导"就是判据写窄了）。
  // 老书签/深链（`?view=…` / `?wizstep=…`）**优先**：用户明确指定就别抢首屏。
  // `?view=onboard` 最优先（手动进新三步向导）
  if (_bootWantOnboard) {
    switchView("settings");
    renderInstallNotice();
    gotoOnboard(0);
    return;
  }
  if (!_bootView && !_bootWantWizard) {
    try {
      const on = await api("/api/wizard/first-run");
      if (on && on.shouldOnboard) {
        _onboardNeeded = true;         // 完成前一直挡着（用户第 1 条：配完前不能正式使用）
        switchView("settings");
        renderInstallNotice();
        gotoOnboard(0);
        return;
      }
    } catch (e) {
      /* 问不到就不抢首屏 —— 宁可少进一次向导，也不要把面板挡在门外 */
    }
  }
  if (_bootView) {
    if (_bootWantHist) _histTab = _bootWantHist;
    switchView(_bootView);
    renderInstallNotice();
    if (_bootWantWizard) gotoWizard();     // 老书签：展开向导卡 + 滚到它
    return;
  }
  switchView("dashboard");
  renderInstallNotice();
}
bootView();
applyHistoryTabOrder();     // 历史三个子页签按 HIST_TAB_ORDER 排（会议 · 指令 · 回顾）
/* **首屏也要把历史子页签的状态统一**（2026-10-07 用户实测："我测了一条指令，
   从网页端看不到"）。原来只有 `switchView("history")` 会调 `switchHistoryTab()`，
   而首屏走的是仪表盘 —— 于是三样状态各说各话：
     * 容器可见性：静态 HTML 默认 `#htab-commands` 可见（另两个带 `hidden`）；
     * 高亮：`switchView()` 按 `_histTab`（初值 `meetings`）算 → **一个页签都不亮**；
     * 列表：没人调 `loadHistory()` → `#historyList` 空着。
   用户看到的就是"历史页可见 + 没有高亮 + 指令列表空白"，点一下「指令」才恢复。
   这里按**统一口径**初始化一次（高亮 + 可见容器 + 拉数据），并把它记进 `_histTab`。 */
function initHistoryTab() {
  if (_bootWantHist) { switchHistoryTab(_bootWantHist); return; }
  // 默认取**当前可见的那个容器**（静态 HTML 已经写明了答案，不去猜用户意图）
  const visible = HIST_TAB_ORDER.find((t) => {
    const el = $("#htab-" + t);
    return el && !el.classList.contains("hidden");
  });
  switchHistoryTab(visible || HIST_TAB_DEFAULT);
}
initHistoryTab();
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
  else if (view === "settings") { loadBoot(); loadBootLogs(); }
  else if (view === "agent" && !_rtDirty && !_rtBusy()) loadRouter();
}, _PANEL_TICK_MS);
// 转写进度轮询（会议列表进度条）
setInterval(pollTranscribe, 2000);


/* ---------------- 开发版牌子（仅 dev；交付物不受影响） ----------------
   用户 2026-10-04："开发版有标志就行，这样不影响交付物"。
   实例名来自本机 `~/.echo-instances.json`（客户机没有 → 不显示）。
   只有 name == "dev" 才亮牌子，稳定版与客户机**什么都不做**。 */
let _instBadgeTried = false;
async function initInstanceBadge() {
  if (_instBadgeTried) return;
  _instBadgeTried = true;
  let info = null;
  try { info = await api("/api/instance"); } catch (e) { return; }
  if (!info || !info.badge) return;                 // 非开发版：保持隐藏、标题也不动
  const el = $("#instBadge");
  if (!el) return;
  el.textContent = info.badge;
  el.title = `这是开发版（${info.root || ""}）—— 与稳定版面板长得一样，别搞混`;
  el.classList.remove("hidden");
  document.title = `ECHO · ${info.badge}`;
}
document.addEventListener("DOMContentLoaded", () => { initInstanceBadge().catch(() => {}); });
