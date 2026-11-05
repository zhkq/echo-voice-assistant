/* ECHO 签约用站点：套餐与订单渲染。
 *
 * 套餐唯一来源是 assets/plans.json（与能力后端 server/billing.py 的 DEFAULT_PLANS 对齐）。
 * Phase 3 接入真实收银台时，把 PLANS_URL 改成同源 "/v1/billing/plans" 即可；
 * 返回结构里的 camelCase 字段与 plans.json 保持一致。
 *
 * 支付页只展示“等待支付”状态，不做支付结果判定，也不会自动跳转。
 */
(function () {
  "use strict";

  var PLANS_URL = "/assets/plans.json";
  var page = document.body.getAttribute("data-page") || "";
  var plansCache = null;

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function money(cents, forceCents) {
    var n = Number(cents) || 0;
    var text = (n / 100).toFixed(2);
    if (!forceCents) text = text.replace(/\.00$/, "");
    return "¥" + text;
  }

  function amountText(cents) {
    var n = Number(cents) || 0;
    return n % 100 === 0 ? String(n / 100) : (n / 100).toFixed(2);
  }

  function wan(chars) {
    var n = Number(chars) || 0;
    if (n >= 10000 && n % 10000 === 0) return (n / 10000) + " 万";
    return String(n);
  }

  function fail(message) {
    var box = document.getElementById("planCards") || document.getElementById("payLayout");
    if (box) {
      box.innerHTML = '<p class="load-error">' + esc(message) + "</p>";
    }
    document.documentElement.setAttribute("data-ready", "error");
  }

  function loadPlans() {
    if (plansCache) return Promise.resolve(plansCache);
    return fetch(PLANS_URL, { cache: "no-store" })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (!data || !Array.isArray(data.plans)) throw new Error("plans 字段缺失");
        plansCache = data;
        return data;
      });
  }

  function planOf(data, code) {
    for (var i = 0; i < data.plans.length; i += 1) {
      if (data.plans[i].code === code) return data.plans[i];
    }
    return null;
  }

  function listItems(plan) {
    var items = [
      "每日 ASR <strong>" + esc(plan.dailyAudioMinutes) + "</strong> 分钟",
      "TTS <strong>" + esc(wan(plan.ttsChars)) + "</strong>字符 / 日",
      "<strong>" + esc(plan.qps) + "</strong> QPS（多设备共用）",
      "<strong>" + esc(plan.maxDevices) + "</strong> 台设备",
      "并发 <strong>" + esc(plan.maxConcurrent) + "</strong>",
      "Asia/Shanghai 自然日恢复"
    ];
    return items.map(function (text) {
      return '<li><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" ' +
        'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
        '<path d="M20 6 9 17l-5-5"/></svg><span>' + text + "</span></li>";
    }).join("");
  }

  function renderPricing(data) {
    var mount = document.getElementById("planCards");
    if (!mount) return;
    var monthly = planOf(data, "monthly");
    var trial = planOf(data, "trial");
    if (!monthly) {
      fail("套餐数据不完整：缺少 monthly。");
      return;
    }
    var html = "";
    html += '<article class="plan plan-main">' +
      '<div class="plan-head"><h2>' + esc(monthly.name) + "</h2>" +
      (monthly.badge ? '<span class="badge-inline">' + esc(monthly.badge) + "</span>" : "") +
      "</div>" +
      '<div class="plan-price"><span class="currency">¥</span>' +
      '<span class="amount">' + esc(amountText(monthly.priceCents)) + "</span>" +
      '<span class="per">/ ' + esc(monthly.days) + " 天</span></div>" +
      '<p class="plan-sub">适合日常会议与语音指令的重度使用。</p>' +
      '<ul class="plan-list">' + listItems(monthly) + "</ul>" +
      '<div class="plan-action">' +
      '<a class="btn btn-primary btn-lg btn-block" href="/pay?plan=' + encodeURIComponent(monthly.code) + '">立即购买</a>' +
      "</div>" +
      '<p class="plan-hint">支付由支付宝 Vibe Pay 提供，金额以收银台为准。</p>' +
      "</article>";

    if (trial) {
      html += '<article class="plan">' +
        '<div class="plan-head"><h2>' + esc(trial.name) + "</h2>" +
        (trial.badge ? '<span class="badge-inline">' + esc(trial.badge) + "</span>" : "") +
        "</div>" +
        '<div class="plan-price"><span class="currency">¥</span>' +
        '<span class="amount">' + esc(amountText(trial.priceCents)) + "</span>" +
        '<span class="per">/ ' + esc(trial.days) + " 天</span></div>" +
        '<p class="plan-sub">先用完整额度体验，再决定是否续费。</p>' +
        '<ul class="plan-list">' + listItems(trial) + "</ul>" +
        '<div class="plan-note">试用为邀请制开通，请联系管理员开启，不在本页直接下单。</div>' +
        "</article>";
    }

    mount.innerHTML = html;
    mount.setAttribute("aria-busy", "false");
    document.documentElement.setAttribute("data-ready", "1");
  }

  function renderOrder(data) {
    var layout = document.getElementById("payLayout");
    if (!layout) return;
    var code = "";
    try {
      code = new URLSearchParams(window.location.search).get("plan") || "monthly";
    } catch (err) {
      code = "monthly";
    }
    var plan = planOf(data, code);
    if (!plan) {
      plan = planOf(data, "monthly");
      var warning = document.getElementById("planWarning");
      if (warning) {
        warning.hidden = false;
        warning.textContent = "没有找到套餐「" + code + "」，当前显示月卡。";
      }
    }
    if (!plan) {
      fail("套餐数据不完整：缺少 monthly。");
      return;
    }

    function set(id, text) {
      var node = document.getElementById(id);
      if (node) node.textContent = text;
    }
    set("orderPlan", plan.name);
    set("orderDays", plan.days + " 天（支付成功后起算）");
    set("orderPrice", money(plan.priceCents, true));
    set("payAmount", money(plan.priceCents, true));
    set("orderId", "ECHO-20261105-0001");
    var qr = document.getElementById("payQr");
    if (qr) qr.setAttribute("alt", "支付宝付款二维码：" + plan.name + " " + money(plan.priceCents, true));
    layout.setAttribute("aria-busy", "false");
    document.documentElement.setAttribute("data-ready", "1");
  }

  function showToast(message) {
    var toast = document.getElementById("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(showToast._timer);
    showToast._timer = window.setTimeout(function () {
      toast.classList.remove("show");
    }, 3200);
  }

  function initConsoleButtons() {
    var buttons = document.querySelectorAll("[data-console]");
    for (var i = 0; i < buttons.length; i += 1) {
      buttons[i].addEventListener("click", function () {
        showToast("内测阶段：请在 ECHO 桌面客户端中进入控制台。");
      });
    }
  }

  function initRefresh() {
    var button = document.getElementById("refreshOrder");
    if (!button) return;
    button.addEventListener("click", function () {
      showToast("支付完成后请刷新页面；订单状态以服务端记录为准。");
    });
  }

  function boot() {
    initConsoleButtons();
    initRefresh();
    if (page !== "pricing" && page !== "pay") {
      document.documentElement.setAttribute("data-ready", "1");
      return;
    }
    loadPlans().then(function (data) {
      if (page === "pricing") renderPricing(data);
      if (page === "pay") renderOrder(data);
    }).catch(function (err) {
      fail("套餐数据加载失败（" + (err && err.message ? err.message : "未知错误") + "），请刷新页面。");
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
