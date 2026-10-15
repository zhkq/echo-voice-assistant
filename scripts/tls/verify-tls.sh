#!/usr/bin/env bash
# ECHO 后端 TLS 自检 —— 四项全绿才算"加 TLS 这件事真做完了"。
#
#   bash verify-tls.sh [域名] [HTTPS端口]
#   默认：echoserver.zhkq.online 8443
#
# 为什么这四项：加证书最容易只做对一半 —— 证书签了、但**明文 8900 还开着**
# （那等于没加），或者 HTTPS 通了、但**管理面 8901 也跟着暴露**（那是更严重的问题）。
# 所以这里不只查"新口通不通"，还查"旧口是不是已经关了"。
set -uo pipefail

DOMAIN="${1:-echoserver.zhkq.online}"
PORT="${2:-8443}"
API="https://${DOMAIN}:${PORT}/v1/health"
fail=0
ok()   { printf '  [OK]   %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; fail=1; }
info() { printf '  [ .. ] %s\n' "$1"; }

echo "== 1) 证书链与到期日 =="
if command -v openssl >/dev/null 2>&1; then
    end=$(echo | openssl s_client -servername "$DOMAIN" -connect "${DOMAIN}:${PORT}" 2>/dev/null \
          | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
    issuer=$(echo | openssl s_client -servername "$DOMAIN" -connect "${DOMAIN}:${PORT}" 2>/dev/null \
          | openssl x509 -noout -issuer 2>/dev/null)
    if [ -n "${end:-}" ]; then
        ok "证书到期：$end"
        info "签发者：${issuer:-未知}"
        # 剩下不到 14 天就该有人管了（acme.sh 正常会在 30 天前就续）
        exp_epoch=$(date -d "$end" +%s 2>/dev/null || echo 0)
        now=$(date +%s)
        if [ "$exp_epoch" -gt 0 ] && [ $((exp_epoch - now)) -lt $((14*86400)) ]; then
            bad "证书 14 天内到期 —— 自动续期可能没生效（看 acme.sh 的定时任务）"
        fi
    else
        bad "取不到证书（TLS 没起来？端口不对？）"
    fi
else
    info "这台机器没有 openssl，跳过错证书检查"
fi

echo "== 2) HTTPS 数据面是否可用 =="
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$API" || echo 000)
if [ "$code" = "200" ]; then ok "$API -> 200"
elif [ "$code" = "000" ]; then bad "$API 连不上（Caddy 没起？防火墙没开 $PORT？）"
else bad "$API -> HTTP $code（期望 200）"; fi

echo "== 3) 旧的明文 8900 是否已经不可达（**这条最容易漏**）=="
if curl -s -o /dev/null --max-time 8 "http://${DOMAIN}:8900/v1/health"; then
    bad "明文 8900 还能从外面访问 —— compose 里没收回内网，等于没加 TLS"
else
    ok "外部明文 8900 已不可达"
fi

echo "== 4) 管理面 8901 是否没有暴露 =="
if curl -s -o /dev/null --max-time 8 "http://${DOMAIN}:8901/" ; then
    bad "8901 从外面能连上 —— 管理面必须只走回环/ssh 隧道，请立刻收回"
else
    ok "管理面 8901 未暴露"
fi

echo
if [ "$fail" = "0" ]; then
    echo "全部通过：对外只有 https://${DOMAIN}:${PORT} 这一个入口。"
else
    echo "有未通过项（见上）—— 别急着把客户端切过来。"
fi
exit "$fail"
