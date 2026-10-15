# ECHO 后端加 TLS（DNS-01 自动续期）—— 在那台跑 `echo-backend` 容器的机器上执行

## 为什么是 DNS-01（不是 HTTP-01）

实测过这台出口：**443 被别人家的站占着**（证书 `CN=aeros.zetone.cn`）、**80 超时** →
`HTTP-01` / `TLS-ALPN-01` 都走不通。**DNS-01** 只需要一个能改 `zhkq.online` TXT 记录的
凭据，**完全不需要开放 80/443**，签出来的仍是公网信任的正规证书。

**证书绑域名、不绑端口** —— 所以服务监听 `8443` 也完全合法，客户端用
`https://echoserver.zhkq.online:8443`。

## 目标形态

```
手机 / 电脑 ──HTTPS:8443──▶ Caddy(容器) ──HTTP:127.0.0.1:8900──▶ echo-backend 容器
                                 ▲
                                 └── 证书由 acme.sh 用阿里云 DNS-01 自动签、自动续（续完 reload Caddy）
```

容器本身**不用动**（继续在 8900 上说 HTTP）。**8901（管理面）永远不暴露。**

## 步骤

### 1. 把 8900 收回内网（**这一步不做，等于白加 TLS**）

```bash
docker inspect echo-backend --format '{{json .HostConfig.PortBindings}}'   # 先看现在怎么映射的
```

在 compose 文件里把数据面端口改成只绑回环（管理面本来就该是回环）：

```yaml
    ports:
      - "127.0.0.1:8900:8900"     # 原来是 "8900:8900"
      - "127.0.0.1:8901:8901"     # 若原来是 0.0.0.0，一起收回
```

```bash
docker compose up -d        # 或 docker compose up -d --force-recreate echo-backend
```

**判据**（在**另一台机器**上跑，本机回环测不出来）：
`http://echoserver.zhkq.online:8900/v1/health` 必须**连不上**；本机上 `curl 127.0.0.1:8900/v1/health` 仍是 200。

### 2. 签证书（acme.sh + 阿里云 DNS）

凭据文件放**仓库外**、权限收紧（本仓库的约定：密钥绝不进 git）：

```bash
install -d -m 700 /root/.echo-tls
cat > /root/.echo-tls/aliyun.env <<'EOF'
Ali_Key=<RAM 子账号的 AccessKeyId>
Ali_Secret=<RAM 子账号的 AccessKeySecret>
EOF
chmod 600 /root/.echo-tls/aliyun.env
```

```bash
curl -s https://get.acme.sh | sh -s email=you@example.com     # 装到 ~/.acme.sh
. /root/.echo-tls/aliyun.env
export Ali_Key Ali_Secret
~/.acme.sh/acme.sh --set-default-ca --server letsencrypt
~/.acme.sh/acme.sh --issue --dns dns_ali -d echoserver.zhkq.online \
    --keylength ec-256 --dnssleep 30
```

> ⚠️ 阿里云免费版 **TXT 的 TTL 必须 ≥600**：acme.sh 的 `dns_ali` 默认就用 600，
> 别手动改成 60（会报 `QuotaExceeded.TTL`，看着像权限问题）。

### 3. 让证书自动装到 Caddy 并热加载（续期也是这条）

```bash
install -d -m 755 /opt/echo-tls/certs
~/.acme.sh/acme.sh --install-cert -d echoserver.zhkq.online --ecc \
    --key-file       /opt/echo-tls/certs/echoserver.key \
    --fullchain-file /opt/echo-tls/certs/echoserver.crt \
    --reloadcmd      "docker exec echo-caddy caddy reload --config /etc/caddy/Caddyfile"
```

`acme.sh` 装完会自己挂一个 cron/定时器 —— **到期前 30 天自动续、续完自动 reload**，
不需要人盯有效期。

### 4. 起 Caddy（docker，用仓库里那份 `Caddyfile`）

```bash
docker run -d --name echo-caddy --restart unless-stopped \
    -p 8443:8443 \
    -v /opt/echo-tls/certs:/certs:ro \
    -v /opt/echo-tls/Caddyfile:/etc/caddy/Caddyfile:ro \
    caddy:2
```

`Caddyfile` 用静态证书（签发交给 acme.sh，Caddy 只做终结与反代）—— 这样**不用**给 Caddy
编 DNS 插件（官方镜像不含，要自己 xcaddy 构建，麻烦且容易在升级时踩坑）。

### 5. 客户端改地址（**代码不用改**）

配对地址从 `http://echoserver.zhkq.online:8900` 换成 `https://echoserver.zhkq.online:8443`：
- 电脑侧：面板 → 能力后端/配对，或重新配对一次（地址一换，旧凭据作废是正常的）
- 手机侧：配对页的「后端地址」填新地址
两边的 HTTP 客户端都从 `base_url` 派生，没有硬编码端口；Android 用**公网 CA** 证书可直接信任
（**别用自签**，那要额外信任配置）。

### 6. 自检

```bash
bash scripts/tls/verify-tls.sh echoserver.zhkq.online 8443
```

它会查：证书链与到期日、`https://…/v1/health` 是否 200、**旧的明文 8900 是否已经不可达**、
8901 是否没暴露。四项全绿才算做完。

## 常见坑（都记在这儿，省得再踩一遍）

| 现象 | 真因 |
|---|---|
| `QuotaExceeded.TTL` | 免费版 TXT TTL 必须 600–86400 |
| 加了证书但"还是明文能连" | 8900 没收回内网（第 1 步） |
| `https://…:8900` 握手失败 | 8900 是数据面 HTTP 口，TLS 在 8443 上，别混 |
| Android 提示证书不可信 | 用了自签证书（换成正规 CA 签的） |
| 续期后浏览器还报旧证书 | `reloadcmd` 没配（Caddy 没热加载） |
