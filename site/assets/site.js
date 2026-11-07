/* ECHO 官网：账号、套餐下单、支付宝收银台与订单/权益。
 *
 * 设计约束：
 *  - 纯静态站点，无构建链；所有页面只引用站内资源。
 *  - 金额、套餐名、订单号、订单状态、权益全部来自能力后端 `/v1/billing/*`；
 *    页面不写死金额，也不使用静态二维码。
 *  - 登录令牌存 localStorage（退出登录清除）；接口 401 时清掉并回登录页。
 *  - 支付页用真实订单的 payUrl 跳转支付宝收银台，并轮询/主动 sync 订单状态；
 *    不做自动跳转，不把“等待支付”当成功。
 */
(function () {
  "use strict";

  var TOKEN_KEY = "echo.userToken";
  var USER_KEY = "echo.user";
  var PROFILE_KEY = "echo.userProfile";
  var API_BASE = "";
  var PLAN_NAMES = { monthly: "月卡", trial: "7 天试用", internal: "内部账号" };
  var plansCache = null;
  var pollingTimer = null;
  var pollingStartedAt = 0;
  var currentOrder = null;
  var syncInFlight = false;

  var page = document.body.getAttribute("data-page") || "";
  var metaApi = document.querySelector('meta[name="echo-api-base"]');
  if (metaApi && metaApi.getAttribute("content")) {
    API_BASE = String(metaApi.getAttribute("content")).replace(/\/+$/, "");
  }

  // ---------------------------------------------------------------- 基础工具

  function $(id) {
    return document.getElementById(id);
  }

  function qsa(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function setText(id, text) {
    var node = $(id);
    if (node) node.textContent = String(text == null ? "" : text);
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

  function formatDate(seconds) {
    var ts = Number(seconds) || 0;
    if (!ts) return "—";
    try {
      return new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"
      }).format(new Date(ts * 1000));
    } catch (err) {
      return new Date(ts * 1000).toLocaleDateString();
    }
  }

  function formatDateTime(seconds) {
    var ts = Number(seconds) || 0;
    if (!ts) return "—";
    try {
      return new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false
      }).format(new Date(ts * 1000));
    } catch (err) {
      return new Date(ts * 1000).toLocaleString();
    }
  }

  function platformLabel(value) {
    var key = String(value || "").toLowerCase();
    if (key === "win32" || key === "windows") return "Windows";
    if (key === "darwin" || key === "macos") return "macOS";
    if (key === "linux") return "Linux";
    if (key === "android") return "Android";
    if (key === "ios") return "iOS";
    return key ? String(value) : "未知平台";
  }

  function planName(code, serverName) {
    return String(serverName || "") || PLAN_NAMES[String(code || "")] ||
      String(code || "套餐");
  }

  function formatExpiry(seconds) {
    return Number(seconds) > 0 ? formatDate(seconds) : "长期有效";
  }

  function showToast(message) {
    var toast = $("toast");
    if (!toast) return;
    toast.textContent = String(message || "");
    toast.classList.add("show");
    window.clearTimeout(showToast._timer);
    showToast._timer = window.setTimeout(function () {
      toast.classList.remove("show");
    }, 3600);
  }

  function markReady() {
    document.documentElement.setAttribute("data-ready", "1");
  }

  function markError() {
    document.documentElement.setAttribute("data-ready", "error");
  }

  function currentUrl() {
    return window.location.pathname + window.location.search;
  }

  // ---------------------------------------------------------------- 令牌

  function loadToken() {
    try {
      return window.localStorage.getItem(TOKEN_KEY) || "";
    } catch (err) {
      return "";
    }
  }

  function loadUser() {
    try {
      var raw = window.localStorage.getItem(USER_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (err) {
      return null;
    }
  }

  function saveSession(data) {
    try {
      if (data && data.userToken) window.localStorage.setItem(TOKEN_KEY, data.userToken);
      var user = (data && data.user) || (data && data.userId ? {
        userId: data.userId, name: (data.user && data.user.name) || "",
        employeeNo: (data.user && data.user.employeeNo) || ""
      } : null);
      if (user) window.localStorage.setItem(USER_KEY, JSON.stringify(user));
      if (data && data.user) window.localStorage.setItem(PROFILE_KEY, JSON.stringify(data.user));
    } catch (err) { /* 隐私模式写不了就只保留内存态 —— 页面照常工作 */ }
  }

  function clearSession() {
    try {
      window.localStorage.removeItem(TOKEN_KEY);
      window.localStorage.removeItem(USER_KEY);
      window.localStorage.removeItem(PROFILE_KEY);
    } catch (err) { /* ignore */ }
  }

  function safeNext(raw) {
    var value = String(raw || "");
    if (!value || value.charAt(0) !== "/" || value.charAt(1) === "/") return "";
    // 反斜杠会被浏览器当成 `/`（`/\\evil.com` → `//evil.com`）：登录回跳只允许站内路径。
    if (value.indexOf("\\") >= 0 || value.indexOf("\n") >= 0 ||
        value.indexOf("\r") >= 0) return "";
    return value;
  }

  function redirectToLogin(next) {
    var target = safeNext(next || currentUrl()) || "/account";
    window.location.assign("/account?next=" + encodeURIComponent(target));
  }

  function updateAccountCta() {
    var signedIn = !!loadToken();
    qsa("[data-account-cta]").forEach(function (node) {
      node.textContent = signedIn ? "我的账户" : "登录 / 注册";
    });
  }

  // ---------------------------------------------------------------- API

  function api(path, options) {
    var opts = options || {};
    var headers = { Accept: "application/json" };
    var token = loadToken();
    if (token && opts.auth !== false) headers.Authorization = "Bearer " + token;
    var init = { method: opts.method || "GET", headers: headers, cache: "no-store" };
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(API_BASE + path, init).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (err) { data = null; }
        }
        if (!resp.ok) {
          var error = new Error(
            (data && (data.detail || data.message)) || ("请求失败（HTTP " + resp.status + "）"));
          error.status = resp.status;
          error.code = (data && data.code) || "";
          error.detail = (data && data.detail) || "";
          error.retryAfter = (data && data.retryAfter) || 0;
          throw error;
        }
        return data == null ? {} : data;
      });
    });
  }

  function explainError(err) {
    if (!err) return "未知错误";
    if (err.code === "payment_unconfigured") return "在线支付暂时不可用，请稍后再试。";
    if (err.code === "payment_gateway_error") return "支付网关暂时不可用，请稍后刷新重试。";
    if (err.code === "order_not_found") return "订单不存在，或不属于当前登录账号。";
    if (err.code === "order_state_conflict") return "订单状态已变化，请刷新页面。";
    if (err.code === "plan_change_unsupported") {
      return err.detail || err.message || "暂不支持升级/降级；请等当前套餐到期后再换。";
    }
    if (err.code === "invalid_email") return "请输入正确的邮箱地址。";
    if (err.code === "password_weak") {
      return err.detail || "密码至少 8 位，且同时包含字母和数字。";
    }
    if (err.code === "verification_code_invalid") return "验证码不正确，请重新输入。";
    if (err.code === "verification_code_expired") return "验证码已过期，请重新获取。";
    if (err.code === "verification_code_attempts_exceeded") {
      return "验证码错误次数过多，请重新获取。";
    }
    if (err.code === "email_taken") return "这个邮箱已注册，请直接登录或重置密码。";
    if (err.code === "invite_invalid") return "邀请码无效或已用完，请向管理员确认。";
    if (err.code === "email_disabled") return "邮箱注册暂时未开放。";
    if (err.code === "email_unconfigured") return "邮件服务暂时不可用，请稍后再试。";
    if (err.code === "email_send_failed") return "验证码邮件发送失败，请稍后再试。";
    if (err.code === "device_not_found") return "没有这台设备，可能已经解绑。";
    if (err.code === "device_limit_reached") {
      return err.detail || err.message || "设备数已满，请先解绑一台旧设备。";
    }
    if (err.code === "device_bound_to_other_account") {
      return "这台设备已绑定到另一个账号，请先在原账号解绑。";
    }
    if (err.code === "rate_limited") {
      var wait = Number(err.retryAfter || 0);
      return wait > 0 ? "操作过于频繁，请 " + wait + " 秒后再试。" : "操作过于频繁，请稍后再试。";
    }
    if (err.status === 401) return "登录已失效，请重新登录。";
    return err.message || err.detail || "请求失败，请稍后重试。";
  }

  // ---------------------------------------------------------------- 套餐

  function loadPlans() {
    if (plansCache) return Promise.resolve(plansCache);
    return api("/v1/billing/plans", { auth: false })
      .then(function (data) {
        if (!data || !Array.isArray(data.plans)) throw new Error("套餐数据不完整");
        plansCache = data;
        return data;
      })
      .catch(function (err) {
        // 静态预览（没有后端）时回退到站内 plans.json 仅供展示；
        // 下单仍会打真实接口，失败时给明确提示。
        return fetch("/assets/plans.json", { cache: "no-store" })
          .then(function (resp) {
            if (!resp.ok) throw err;
            return resp.json();
          })
          .then(function (data) {
            if (!data || !Array.isArray(data.plans)) throw err;
            data.fallback = true;
            data.payment = { provider: "", configured: false, sandbox: false };
            plansCache = data;
            return data;
          });
      });
  }

  function planOf(data, code) {
    var list = (data && data.plans) || [];
    for (var i = 0; i < list.length; i += 1) {
      if (list[i].code === code) return list[i];
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

  function planAction(plan) {
    if (!plan.purchasable) {
      return '<div class="plan-note">试用为邀请制开通，请联系管理员开启，不在本页直接下单。</div>';
    }
    return '<div class="plan-action">' +
      '<button type="button" class="btn btn-primary btn-lg btn-block" data-buy="' +
      esc(plan.code) + '">立即购买</button></div>' +
      '<p class="plan-hint">登录后下单；支付由支付宝提供，金额以收银台为准。</p>';
  }

  function planCard(plan, main) {
    var cls = main ? "plan plan-main" : "plan";
    var sub = plan.purchasable
      ? "按月订阅，覆盖转写、语音合成与设备额度。"
      : "先用完整额度体验，再决定是否续费。";
    return '<article class="' + cls + '">' +
      '<div class="plan-head"><h2>' + esc(plan.name) + "</h2>" +
      (plan.badge ? '<span class="badge-inline">' + esc(plan.badge) + "</span>" : "") +
      "</div>" +
      '<div class="plan-price"><span class="currency">¥</span>' +
      '<span class="amount">' + esc(amountText(plan.priceCents)) + "</span>" +
      '<span class="per">/ ' + esc(plan.days) + " 天</span></div>" +
      '<p class="plan-sub">' + sub + "</p>" +
      '<ul class="plan-list">' + listItems(plan) + "</ul>" +
      planAction(plan) +
      "</article>";
  }

  function renderPricing(data) {
    var mount = $("planCards");
    if (!mount) return;
    var list = (data && data.plans) || [];
    var paid = [];
    var trial = null;
    for (var i = 0; i < list.length; i += 1) {
      var p = list[i];
      if (p && p.code === "internal") continue;
      if (p && p.purchasable && Number(p.priceCents) > 0) paid.push(p);
      if (p && p.code === "trial") trial = p;
    }
    paid.sort(function (a, b) { return Number(a.priceCents) - Number(b.priceCents); });
    var ordered = paid.slice();
    if (trial) ordered.push(trial);
    if (!paid.length) {
      throw new Error("套餐数据不完整：没有可购买的付费套餐。");
    }
    var html = "";
    for (var j = 0; j < ordered.length; j += 1) {
      html += planCard(ordered[j], j === 0);
    }

    var payment = data.payment || {};
    if (payment.provider && payment.configured === false) {
      html += '<p class="plan-hint pricing-payment-note">在线支付暂时不可用，请稍后再试或联系管理员。</p>';
    }
    mount.innerHTML = html;
    mount.setAttribute("aria-busy", "false");
    document.documentElement.setAttribute("data-ready", "1");
  }

  function buyPlan(code, button) {
    if (!loadToken()) {
      redirectToLogin(currentUrl());
      return;
    }
    var original = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "正在创建订单…";
    }
    api("/v1/billing/orders", { method: "POST", body: { planCode: code } })
      .then(function (order) {
        if (!order || !order.orderId) throw new Error("下单响应缺少订单号");
        window.location.assign("/pay?order=" + encodeURIComponent(order.orderId));
      })
      .catch(function (err) {
        if (err.status === 401) {
          clearSession();
          redirectToLogin(currentUrl());
          return;
        }
        showToast(explainError(err));
        if (button) {
          button.disabled = false;
          button.textContent = original;
        }
      });
  }

  function initPricing() {
    var mount = $("planCards");
    loadPlans().then(renderPricing).catch(function (err) {
      if (mount) {
        mount.innerHTML = '<p class="load-error">套餐加载失败：' + esc(explainError(err)) +
          ' <button type="button" class="btn btn-ghost btn-sm" id="retryPlans">重试</button></p>';
        var retry = $("retryPlans");
        if (retry) retry.addEventListener("click", function () { window.location.reload(); });
      }
      markError();
    });
    if (mount) {
      mount.addEventListener("click", function (event) {
        var button = event.target.closest ? event.target.closest("[data-buy]") : null;
        if (!button) return;
        buyPlan(button.getAttribute("data-buy"), button);
      });
    }
  }

  // ---------------------------------------------------------------- 支付页

  function parseOrderId() {
    var params;
    try {
      params = new URLSearchParams(window.location.search);
    } catch (err) {
      return "";
    }
    return String(params.get("order") || params.get("out_trade_no") || "").trim();
  }

  function stopPolling() {
    if (pollingTimer) {
      window.clearInterval(pollingTimer);
      pollingTimer = null;
    }
  }

  function startPolling(orderId) {
    if (pollingTimer) return;
    pollingStartedAt = Date.now();
    pollingTimer = window.setInterval(function () {
      if (Date.now() - pollingStartedAt > 5 * 60 * 1000) {
        stopPolling();
        setText("payStatusText", "支付结果暂未确认，可稍后点“刷新订单状态”。");
        return;
      }
      if (syncInFlight) return;
      api("/v1/billing/orders/" + encodeURIComponent(orderId))
        .then(handleOrderData)
        .catch(function (err) {
          if (err.status === 401) {
            clearSession();
            redirectToLogin(currentUrl());
          }
        });
    }, 3000);
  }

  function setStatusTag(text, kind) {
    var tag = $("payStatusTag");
    var orderTag = $("orderStatusTag");
    if (tag) {
      tag.textContent = text;
      tag.className = "tag" + (kind ? " tag-" + kind : "");
    }
    if (orderTag) {
      orderTag.textContent = text;
      orderTag.className = "tag tag-muted" + (kind ? " tag-" + kind : "");
    }
  }

  function statusText(status) {
    switch (String(status || "")) {
      case "created": return "待支付";
      case "paying": return "等待支付";
      case "paid": return "已支付";
      case "refunded": return "已退款";
      case "closed": return "已关闭";
      default: return "未知状态";
    }
  }

  function safePayUrl(url) {
    var value = String(url || "");
    return /^(https?:\/\/|mock:\/\/)/i.test(value) ? value : "";
  }

  function renderWaiting(order) {
    var waiting = $("payWaiting");
    var result = $("payResult");
    if (waiting) waiting.hidden = false;
    if (result) result.hidden = true;
    var payUrl = safePayUrl(order.payUrl);
    var open = $("openCashier");
    var copy = $("copyPayUrl");
    var missing = $("cashierMissing");
    if (open) {
      open.hidden = !payUrl;
      open.setAttribute("href", payUrl || "#");
      open.setAttribute("aria-disabled", payUrl ? "false" : "true");
    }
    if (copy) copy.hidden = !payUrl;
    if (missing) missing.hidden = !!payUrl;
    var expired = Number(order.expiresAt || 0) * 1000 > 0 &&
      Date.now() > Number(order.expiresAt) * 1000;
    setText("payStatusText", expired
      ? "订单已超过支付时限，请刷新或重新下单。"
      : (order.payUrl ? "等待支付 · 支付成功后自动更新" : "等待支付 · 暂无收银台链接"));
    setStatusTag(expired ? "已超时" : statusText(order.status), expired ? "warn" : "");
    if (expired) stopPolling();
  }

  function resultRows(rows) {
    return rows.map(function (row) {
      return '<div class="order-row"><span>' + esc(row[0]) + "</span><span>" +
        esc(row[1]) + "</span></div>";
    }).join("");
  }

  function button(label, className, attrs) {
    return '<button type="button" class="' + className + '" ' + (attrs || "") + ">" +
      esc(label) + "</button>";
  }

  function renderResult(order, kind, entitlement) {
    var waiting = $("payWaiting");
    var result = $("payResult");
    if (waiting) waiting.hidden = true;
    if (result) result.hidden = false;
    var card = $("resultCard");
    var icon = $("resultIcon");
    var title = $("resultTitle");
    var message = $("resultMessage");
    var rows = $("resultRows");
    var actions = $("resultActions");
    if (card) card.className = "result-card result-" + kind;
    var iconId = kind === "paid" ? "icon-check"
      : (kind === "closed" || kind === "timeout" || kind === "error"
        ? "icon-alert" : "icon-shield");
    if (icon) icon.innerHTML = '<svg><use href="/assets/icons.svg#' + iconId + '"></use></svg>';
    if (title) title.textContent = kind === "paid" ? "支付成功"
      : (kind === "refunded" ? "订单已退款"
        : (kind === "closed" ? "订单已关闭"
          : (kind === "timeout" ? "订单已超时" : "订单读取失败")));
    if (message) {
      message.textContent = kind === "paid"
        ? "权益已开通，可在“我的账户”查看到期时间与剩余额度。"
        : (kind === "refunded"
          ? "退款已处理，权益按剩余未退款订单重新计算。"
          : (kind === "closed"
            ? "这笔订单没有完成支付，已关闭。可以重新选择套餐下单。"
            : (kind === "timeout"
              ? "这笔订单已超过支付时限，不会再入账。请重新下单。"
              : "暂时无法读取订单，请稍后重试。")));
    }
    var orderRows = [
      ["订单号", order && order.orderId ? order.orderId : "—"],
      ["套餐", planName(order && order.planCode, order && order.planName)],
      ["金额", money(order && order.amountCents, true)]
    ];
    if (kind === "paid") {
      orderRows.push(["支付时间", formatDateTime(order.paidAt)]);
      if (entitlement) {
        orderRows.push(["到期时间", entitlement.paidUntil > 0
          ? formatDate(entitlement.paidUntil) : "长期有效"]);
        orderRows.push(["今日剩余转写", (entitlement.dailyMinutesLeft || 0) + " / " +
          (entitlement.dailyMinutes || 0) + " 分钟"]);
        orderRows.push(["今日剩余 TTS", Number(entitlement.ttsCharsLeft || 0).toLocaleString() +
          " 字符"]);
      }
    }
    if (rows) rows.innerHTML = resultRows(orderRows).replace(
      /class="order-row"/g, 'class="order-row result-row"');
    if (actions) {
      if (kind === "paid") {
        actions.innerHTML = '<a class="btn btn-primary btn-block" href="/account">查看我的权益</a>' +
          '<a class="btn btn-ghost btn-block" href="/pricing">续费</a>';
      } else if (kind === "refunded") {
        actions.innerHTML = '<a class="btn btn-primary btn-block" href="/account">查看我的权益</a>' +
          '<a class="btn btn-ghost btn-block" href="/pricing">再次购买</a>';
      } else if (kind === "closed") {
        actions.innerHTML = '<a class="btn btn-primary btn-block" href="/pricing">重新购买</a>' +
          '<a class="btn btn-ghost btn-block" href="/account">返回我的账户</a>';
      } else if (kind === "timeout") {
        actions.innerHTML = '<a class="btn btn-primary btn-block" href="/pricing">重新购买</a>' +
          '<button type="button" class="btn btn-ghost btn-block" id="retryTimeout">刷新订单状态</button>';
        var retryTimeout = $("retryTimeout");
        if (retryTimeout) {
          retryTimeout.addEventListener("click", function () {
            syncOrder(order && order.orderId);
          });
        }
      } else {
        actions.innerHTML = '<button type="button" class="btn btn-primary btn-block" id="retryOrder">重试</button>' +
          '<a class="btn btn-ghost btn-block" href="/pricing">返回套餐页</a>';
        var retry = $("retryOrder");
        if (retry) retry.addEventListener("click", function () { window.location.reload(); });
      }
    }
    if (kind === "paid" || kind === "refunded" || kind === "closed" ||
        kind === "timeout") stopPolling();
  }

  function handleOrderData(order) {
    if (!order || !order.orderId) throw new Error("订单数据不完整");
    currentOrder = order;
    var layout = $("payLayout");
    if (layout) layout.hidden = false;
    var loading = $("payLoading");
    if (loading) loading.hidden = true;
    var error = $("payError");
    if (error) error.hidden = true;

    setText("orderPlan", planName(order.planCode, order.planName));
    var paid = String(order.status || "") === "paid";
    setText("orderCurrentLabel", paid ? "支付前到期" : "当前到期");
    setText("orderProjectedLabel", paid ? "本次支付后到期" : "支付后预计到期");
    setText("orderCurrentUntil", Number(order.currentPaidUntil || 0) > 0
      ? formatDate(order.currentPaidUntil)
      : (order.renewalBasis === "never_expires" ? "长期有效" : "尚未开通"));
    setText("orderDays", Number(order.renewalDays || order.days || 0) > 0
      ? (order.renewalDays || order.days) + " 天" : "长期有效");
    setText("orderProjectedUntil", order.renewalBasis === "not_applicable"
      ? "—" : formatExpiry(order.projectedPaidUntil));
    setText("renewalRule", order.renewalRule || "续费规则以服务端为准。");
    setText("orderId", order.orderId);
    setText("orderCreated", formatDateTime(order.createdAt));
    setText("orderPrice", money(order.amountCents, true));

    var notice = $("testNotice");
    if (notice) notice.hidden = !order.sandbox;

    var status = String(order.status || "");
    if (status === "paid") {
      setStatusTag(statusText(status), "ok");
      renderResult(order, "paid", null);
      markReady();
      api("/v1/billing/entitlement").then(function (ent) {
        if (ent) renderResult(order, "paid", ent);
      }).catch(function () { /* 权益读取失败不影响"已支付"这个事实 */ });
      return;
    }
    if (status === "refunded") {
      setStatusTag(statusText(status), "muted");
      renderResult(order, "refunded");
      markReady();
      return;
    }
    if (status === "closed") {
      setStatusTag(statusText(status), "warn");
      renderResult(order, "closed");
      markReady();
      return;
    }
    var expiresAt = Number(order.expiresAt || 0) * 1000;
    if ((status === "created" || status === "paying") &&
        expiresAt > 0 && Date.now() > expiresAt) {
      setStatusTag("已超时", "warn");
      renderResult(order, "timeout");
      markReady();
      return;
    }
    renderWaiting(order);
    startPolling(order.orderId);
    markReady();
  }

  function syncOrder(orderId) {
    if (!orderId || syncInFlight) return Promise.resolve();
    syncInFlight = true;
    var button = $("refreshOrder");
    var original = button ? button.textContent : "";
    if (button) {
      button.disabled = true;
      button.textContent = "正在查询…";
    }
    return api("/v1/billing/orders/" + encodeURIComponent(orderId) + "/sync",
      { method: "POST", body: {} })
      .then(handleOrderData)
      .catch(function (err) {
        if (err.status === 401) {
          clearSession();
          redirectToLogin(currentUrl());
          return;
        }
        showToast(explainError(err));
      })
      .then(function () {
        syncInFlight = false;
        if (button) {
          button.disabled = false;
          button.textContent = original;
        }
      });
  }

  function showPayError(err) {
    var loading = $("payLoading");
    if (loading) loading.hidden = true;
    var box = $("payError");
    if (box) {
      box.hidden = false;
      box.innerHTML = esc(explainError(err)) +
        ' <a href="/pricing">返回套餐页重新下单</a>';
    }
    markError();
  }

  function initPay() {
    var orderId = parseOrderId();
    if (!orderId) {
      showPayError({ message: "链接里没有订单号，请从套餐页购买。" });
      return;
    }
    if (!loadToken()) {
      redirectToLogin(currentUrl());
      return;
    }
    api("/v1/billing/orders/" + encodeURIComponent(orderId))
      .then(function (order) {
        handleOrderData(order);
        if (order && (order.status === "created" || order.status === "paying")) {
          syncOrder(orderId);
        }
      })
      .catch(showPayError);

    var refresh = $("refreshOrder");
    if (refresh) refresh.addEventListener("click", function () { syncOrder(orderId); });
    var cancel = $("cancelOrder");
    if (cancel) {
      cancel.addEventListener("click", function () {
        if (!window.confirm("取消这笔订单？取消后需要重新下单。")) return;
        api("/v1/billing/orders/" + encodeURIComponent(orderId) + "/cancel",
          { method: "POST", body: {} })
          .then(function () { return api("/v1/billing/orders/" + encodeURIComponent(orderId)); })
          .then(handleOrderData)
          .catch(function (err) { showToast(explainError(err)); });
      });
    }
    var copy = $("copyPayUrl");
    if (copy) {
      copy.addEventListener("click", function () {
        var payUrl = safePayUrl(currentOrder && currentOrder.payUrl);
        if (!payUrl) return;
        copyText(payUrl);
      });
    }
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible" && currentOrder &&
          (currentOrder.status === "created" || currentOrder.status === "paying")) {
        syncOrder(currentOrder.orderId);
      }
    });
    window.addEventListener("pageshow", function () {
      if (currentOrder &&
          (currentOrder.status === "created" || currentOrder.status === "paying")) {
        syncOrder(currentOrder.orderId);
      }
    });
  }

  function copyText(text) {
    function fallback() {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try { document.execCommand("copy"); } catch (err) { /* ignore */ }
      document.body.removeChild(area);
      showToast("支付链接已复制");
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        showToast("支付链接已复制");
      }).catch(fallback);
    } else {
      fallback();
    }
  }

  // ---------------------------------------------------------------- 账户页

  var authMode = "login";
  var codeTimer = null;
  var codeDeadline = 0;

  function isValidEmail(value) {
    var text = String(value || "").trim();
    return /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$/.test(text);
  }

  function stopCodeCountdown() {
    if (codeTimer) {
      window.clearInterval(codeTimer);
      codeTimer = null;
    }
    codeDeadline = 0;
    var button = $("sendCodeBtn");
    if (button) {
      button.disabled = false;
      button.textContent = "发送验证码";
    }
  }

  function startCodeCountdown(seconds) {
    var total = Math.max(1, Math.floor(Number(seconds) || 60));
    stopCodeCountdown();
    codeDeadline = Date.now() + total * 1000;
    var button = $("sendCodeBtn");
    if (!button) return;
    function tick() {
      var left = Math.ceil((codeDeadline - Date.now()) / 1000);
      if (left <= 0) {
        stopCodeCountdown();
        return;
      }
      button.disabled = true;
      button.textContent = "重新发送 (" + left + "s)";
    }
    tick();
    codeTimer = window.setInterval(tick, 1000);
  }

  function authErrorText(err) {
    if (!err) return "未知错误";
    if (err.code === "unauthorized" || err.status === 401) return "邮箱或密码不对。";
    return explainError(err);
  }

  function setAuthMode(mode) {
    authMode = mode === "register" ? "register" : (mode === "reset" ? "reset" : "login");
    var isLogin = authMode === "login";
    var isRegister = authMode === "register";
    var isReset = authMode === "reset";
    qsa("[data-auth-tab]").forEach(function (tab) {
      var active = tab.getAttribute("data-auth-tab") === authMode;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    var tabs = $("authTabs");
    if (tabs) tabs.hidden = isReset;
    var codeRow = $("authCodeRow");
    if (codeRow) codeRow.hidden = isLogin;
    var password2Row = $("authPassword2Row");
    if (password2Row) password2Row.hidden = isLogin;
    var inviteRow = $("authInviteRow");
    if (inviteRow) inviteRow.hidden = !isRegister;
    var forgot = $("authForgot");
    if (forgot) forgot.hidden = !isLogin;
    var back = $("authBack");
    if (back) back.hidden = !isReset;
    var hint = $("authPasswordHint");
    if (hint) hint.hidden = isLogin;
    var passwordLabel = $("authPasswordLabel");
    if (passwordLabel) passwordLabel.textContent = isLogin ? "密码" : (isReset ? "新密码" : "设置密码");
    var password = $("authPassword");
    if (password) {
      password.autocomplete = isLogin ? "current-password" : "new-password";
      password.placeholder = isLogin ? "登录密码" : "至少 8 位，含字母和数字";
    }
    var submit = $("authSubmit");
    if (submit) {
      submit.disabled = false;
      submit.textContent = isLogin ? "登录" : (isRegister ? "注册并登录" : "重置密码");
    }
    var note = $("authNote");
    if (note) {
      note.textContent = isLogin
        ? "已有账号？用邮箱和密码登录。"
        : (isRegister
          ? "首次使用：邮箱注册，验证码会发到你的邮箱。"
          : "输入邮箱收到的验证码，设置新密码。");
    }
    stopCodeCountdown();
    showAuthError("");
  }

  function showAuthError(message) {
    var box = $("authError");
    if (!box) return;
    box.hidden = !message;
    box.textContent = message || "";
  }

  function showAuth() {
    var auth = $("authView");
    var dash = $("dashboardView");
    var loading = $("accountLoading");
    if (loading) loading.hidden = true;
    if (auth) auth.hidden = false;
    if (dash) dash.hidden = true;
    var layout = $("accountLayout");
    if (layout) layout.hidden = false;
    setText("accountTitle", "登录 / 注册");
    setText("accountLead", "用邮箱注册/登录，查看套餐权益、到期时间和最近订单。");
    setText("accountCrumb", "登录 / 注册");
    var form = $("authForm");
    if (form) form.reset();
    setAuthMode("login");
    markReady();
  }

  function showDashboard() {
    var auth = $("authView");
    var dash = $("dashboardView");
    var loading = $("accountLoading");
    if (loading) loading.hidden = true;
    if (auth) auth.hidden = true;
    if (dash) dash.hidden = false;
    var layout = $("accountLayout");
    if (layout) layout.hidden = false;
    setText("accountTitle", "我的账户");
    setText("accountLead", "查看当前权益、到期时间与最近订单。");
    setText("accountCrumb", "我的账户");
    markReady();
  }

  function entitlementStatusLabel(status) {
    switch (String(status || "none")) {
      case "active": return "有效";
      case "expired": return "已到期";
      case "refunded": return "已退款";
      case "frozen": return "已冻结";
      case "inactive": return "已停用";
      default: return "未开通";
    }
  }

  function statusClass(status) {
    switch (String(status || "")) {
      case "active": case "paid": return "ok";
      case "expired": case "closed": case "frozen": return "warn";
      case "refunded": case "inactive": return "muted";
      default: return "muted";
    }
  }

  function renderMe(me) {
    var user = (me && me.user) || {};
    var ent = (me && me.entitlement) || {};
    var email = user.email || "";
    var name = user.name || (loadUser() && loadUser().name) || "";
    if (!name && email) name = email.split("@")[0];
    if (!name) name = "ECHO 用户";
    var employeeNo = user.employeeNo || "";
    setText("profileName", name);
    setText("profileMeta", email ? "邮箱：" + email
      : (employeeNo ? "工号：" + employeeNo : "已登录"));
    var avatar = $("profileAvatar");
    if (avatar) avatar.textContent = String(name).slice(0, 1) || "E";
    var status = String(ent.status || "none");
    setText("entitlementStatus", entitlementStatusLabel(status));
    var entTag = $("entitlementStatus");
    if (entTag) entTag.className = "tag tag-" + statusClass(status);
    setText("entitlementPlan", status === "none"
      ? "尚未开通套餐"
      : (planName(ent.plan, ent.planName) + (status === "active" ? " · 使用中" : "")));
    setText("entitlementRule", ent.renewalRule || "");
    setText("entitlementPolicy", [
      ent.planChangeRule || "",
      ent.refundRule || "",
      ent.autoRenew ? "自动续费已开启。" : "手动续费，不会自动扣款。"
    ].filter(Boolean).join(" "));
    setText("entitlementExpiry", Number(ent.paidUntil || 0) > 0
      ? formatDate(ent.paidUntil) : (status === "active" ? "长期有效" : "—"));
    setText("entitlementMinutes", (ent.dailyMinutesLeft == null ? 0 : ent.dailyMinutesLeft) +
      " / " + (ent.dailyMinutes == null ? 0 : ent.dailyMinutes) + " 分钟");
    setText("entitlementTts", Number(ent.ttsCharsLeft || 0).toLocaleString() + " 字符");
    setText("entitlementQps", (ent.qps || 0) + " QPS · 并发 " + (ent.maxConcurrent || 0));
    setText("entitlementDevices", (ent.devices || 0) + " / " + (ent.maxDevices || 0) + " 台");

    var actions = $("entitlementActions");
    if (actions) {
      var primary = status === "none" || status === "expired" || status === "refunded" ||
        status === "frozen" || status === "inactive";
      actions.innerHTML = primary
        ? '<a class="btn btn-primary" href="/pricing">购买 / 续费</a>' +
          '<a class="btn btn-ghost" href="/download">下载客户端</a>'
        : '<a class="btn btn-primary" href="/pricing">续费</a>' +
          '<a class="btn btn-ghost" href="/download">下载客户端</a>';
    }
  }

  function orderStatusLabel(status) {
    return statusText(status);
  }

  function renderDevices(devices) {
    var list = $("devicesList");
    var empty = $("devicesEmpty");
    if (!list) return;
    var rows = Array.isArray(devices) ? devices : [];
    setText("deviceCount", rows.length + " 台");
    if (!rows.length) {
      list.innerHTML = "";
      list.setAttribute("aria-busy", "false");
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    list.innerHTML = rows.map(function (device) {
      var status = String(device.status || "active");
      var active = status === "active";
      var meta = [
        platformLabel(device.platform),
        "绑定 " + formatDateTime(device.boundAt),
        Number(device.lastSeen || 0) > 0
          ? "最近活跃 " + formatDateTime(device.lastSeen) : "尚未活跃"
      ].join(" · ");
      return '<article class="device-item">' +
        '<div class="device-item-main">' +
        '<div class="device-item-title">' + esc(device.deviceName || device.clientId) +
        '<span class="tag tag-' + statusClass(active ? "active" : status) + '">' +
        esc(active ? "使用中" : "已解绑") + "</span></div>" +
        '<div class="device-item-meta">' + esc(meta) + " · " + esc(device.clientId) +
        "</div></div>" +
        '<div class="device-item-side">' +
        (active ? '<button class="btn btn-ghost btn-sm" type="button" data-unbind-client="' +
          esc(device.clientId) + '">解绑</button>' : "") +
        "</div></article>";
    }).join("");
    list.setAttribute("aria-busy", "false");
  }

  function renderOrders(orders) {
    var list = $("ordersList");
    var empty = $("ordersEmpty");
    if (!list) return;
    var rows = Array.isArray(orders) ? orders : [];
    if (!rows.length) {
      list.innerHTML = "";
      list.setAttribute("aria-busy", "false");
      if (empty) empty.hidden = false;
      return;
    }
    if (empty) empty.hidden = true;
    list.innerHTML = rows.map(function (order) {
      var status = String(order.status || "");
      var tagClass = statusClass(status);
      var action = "";
      if (status === "paying" || status === "created") {
        action = '<a class="order-link" href="/pay?order=' +
          encodeURIComponent(order.orderId) + '">继续支付</a>';
      } else if (status === "paid" || status === "refunded") {
        action = '<a class="order-link" href="/pay?order=' +
          encodeURIComponent(order.orderId) + '">查看</a>';
      } else if (status === "closed") {
        action = '<a class="order-link" href="/pricing">重新购买</a>';
      }
      return '<article class="order-item">' +
        '<div class="order-item-main">' +
        '<div class="order-item-title">' + esc(planName(order.planCode, order.planName)) +
        '<span class="tag tag-' + tagClass + '">' + esc(orderStatusLabel(status)) + "</span></div>" +
        '<div class="order-item-meta">' + esc(formatDateTime(order.createdAt)) +
        " · " + esc(order.orderId) + "</div></div>" +
        '<div class="order-item-side"><strong>' + esc(money(order.amountCents, true)) +
        "</strong>" + action + "</div>" +
        "</article>";
    }).join("");
    list.setAttribute("aria-busy", "false");
  }

  function loadAccountData() {
    return Promise.all([
      api("/v1/account/me"),
      api("/v1/billing/orders").catch(function (err) {
        if (err.status === 401) throw err;
        return { orders: [] };
      })
    ]).then(function (results) {
      var me = results[0];
      renderMe(me);
      renderDevices(me && me.devices);
      renderOrders(results[1] && results[1].orders);
      showDashboard();
    });
  }

  function sendAuthCode() {
    var email = ($("authEmail") && $("authEmail").value || "").trim();
    if (!isValidEmail(email)) {
      showAuthError("请输入正确的邮箱地址。");
      return;
    }
    if (authMode === "login") return;
    var purpose = authMode === "reset" ? "reset" : "register";
    var button = $("sendCodeBtn");
    if (button) {
      button.disabled = true;
      button.textContent = "发送中…";
    }
    showAuthError("");
    api("/v1/account/email/send-code",
        { method: "POST", body: { email: email, purpose: purpose }, auth: false })
      .then(function (data) {
        startCodeCountdown((data && data.cooldownSeconds) || 60);
        showToast("验证码已发送，请查看邮箱。");
        var code = $("authCode");
        if (code) code.focus();
      })
      .catch(function (err) {
        var wait = Number((err && err.retryAfter) || 0);
        if (wait > 0) {
          startCodeCountdown(wait);
        } else if (button) {
          button.disabled = false;
          button.textContent = "发送验证码";
        }
        showAuthError(authErrorText(err));
      });
  }

  function initAccount() {
    var next = "";
    try {
      next = safeNext(new URLSearchParams(window.location.search).get("next"));
    } catch (err) { next = ""; }

    qsa("[data-auth-tab]").forEach(function (tab) {
      tab.addEventListener("click", function () {
        setAuthMode(tab.getAttribute("data-auth-tab"));
      });
    });
    var forgot = $("forgotPassword");
    if (forgot) {
      forgot.addEventListener("click", function (event) {
        event.preventDefault();
        setAuthMode("reset");
      });
    }
    var back = $("backToLogin");
    if (back) {
      back.addEventListener("click", function (event) {
        event.preventDefault();
        setAuthMode("login");
      });
    }
    var sendCode = $("sendCodeBtn");
    if (sendCode) {
      sendCode.addEventListener("click", sendAuthCode);
    }

    var form = $("authForm");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var mode = authMode;
        var email = ($("authEmail") && $("authEmail").value || "").trim();
        var password = ($("authPassword") && $("authPassword").value || "");
        var password2 = ($("authPassword2") && $("authPassword2").value || "");
        var code = ($("authCode") && $("authCode").value || "").trim();
        var inviteCode = ($("authInviteCode") && $("authInviteCode").value || "").trim();
        if (!isValidEmail(email)) {
          showAuthError("请输入正确的邮箱地址。");
          return;
        }
        if (mode !== "login") {
          if (!/^\d{6}$/.test(code)) {
            showAuthError("请输入 6 位邮箱验证码。");
            return;
          }
          if (mode === "register" && !inviteCode) {
            showAuthError("内测期注册需要邀请码，请填写管理员发放的通用邀请码。");
            return;
          }
        }
        if (!password) {
          showAuthError("请填写密码。");
          return;
        }
        if (mode !== "login") {
          if (password.length < 8 || !/[A-Za-z]/.test(password) || !/\d/.test(password)) {
            showAuthError("密码至少 8 位，且同时包含字母和数字。");
            return;
          }
          if (password !== password2) {
            showAuthError("两次输入的密码不一致。");
            return;
          }
        }
        var path = "/v1/account/login";
        var body = { email: email, password: password };
        if (mode === "register") {
          path = "/v1/account/register";
          body = { email: email, code: code, password: password, inviteCode: inviteCode };
        } else if (mode === "reset") {
          path = "/v1/account/password/reset";
          body = { email: email, code: code, newPassword: password };
        }
        var submit = $("authSubmit");
        var original = submit ? submit.textContent : "";
        if (submit) {
          submit.disabled = true;
          submit.textContent = mode === "login" ? "正在登录…"
            : (mode === "register" ? "正在注册…" : "正在重置…");
        }
        showAuthError("");
        api(path, { method: "POST", body: body, auth: false })
          .then(function (data) {
            if (mode === "reset") {
              var keep = email;
              showToast("密码已重置，请用新密码登录。");
              setAuthMode("login");
              var emailInput = $("authEmail");
              if (emailInput) emailInput.value = keep;
              var pw = $("authPassword");
              if (pw) pw.value = "";
              var pw2 = $("authPassword2");
              if (pw2) pw2.value = "";
              var codeInput = $("authCode");
              if (codeInput) codeInput.value = "";
              return null;
            }
            saveSession(data);
            updateAccountCta();
            if (next) {
              window.location.assign(next);
              return null;
            }
            return loadAccountData();
          })
          .catch(function (err) {
            showAuthError(authErrorText(err));
            if (submit) {
              submit.disabled = false;
              submit.textContent = original;
            }
          });
      });
    }

    var logout = $("logoutBtn");
    if (logout) {
      logout.addEventListener("click", function () {
        api("/v1/account/logout", { method: "POST", body: {} })
          .catch(function () { /* 令牌已经失效也照常清本地 */ })
          .then(function () {
            clearSession();
            updateAccountCta();
            showAuth();
            showToast("已退出登录");
          });
      });
    }

    var refresh = $("refreshAccount");
    if (refresh) {
      refresh.addEventListener("click", function () {
        refresh.disabled = true;
        loadAccountData().catch(function (err) {
          showToast(explainError(err));
        }).then(function () { refresh.disabled = false; });
      });
    }

    var devicesList = $("devicesList");
    if (devicesList) {
      devicesList.addEventListener("click", function (event) {
        var btn = event.target && event.target.closest
          ? event.target.closest("[data-unbind-client]") : null;
        if (!btn) return;
        var clientId = btn.getAttribute("data-unbind-client") || "";
        if (!clientId) return;
        if (!window.confirm("解绑这台设备？解绑后它会立即失效，需要重新登录绑定。")) return;
        btn.disabled = true;
        api("/v1/account/devices/unbind",
            { method: "POST", body: { clientId: clientId } })
          .then(function () {
            showToast("已解绑这台设备");
            return loadAccountData();
          })
          .catch(function (err) {
            btn.disabled = false;
            showToast(explainError(err));
          });
      });
    }

    var refreshDevices = $("refreshDevices");
    if (refreshDevices) {
      refreshDevices.addEventListener("click", function () {
        refreshDevices.disabled = true;
        loadAccountData().catch(function (err) {
          showToast(explainError(err));
        }).then(function () { refreshDevices.disabled = false; });
      });
    }

    if (!loadToken()) {
      showAuth();
      return;
    }
    loadAccountData().catch(function (err) {
      if (err.status === 401) {
        clearSession();
        updateAccountCta();
        showAuth();
        showToast("登录已失效，请重新登录。");
        return;
      }
      var loading = $("accountLoading");
      if (loading) loading.hidden = true;
      var box = $("accountError");
      if (box) {
        box.hidden = false;
        box.textContent = explainError(err);
      }
      markError();
    });
  }

  // ---------------------------------------------------------------- 启动

  function boot() {
    updateAccountCta();
    if (page === "pricing") {
      initPricing();
    } else if (page === "pay") {
      initPay();
    } else if (page === "account") {
      initAccount();
    } else {
      markReady();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
