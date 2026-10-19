# 沙盒服务转发与 Preview

## 分层

- `services/sandbox/service_forwarding.py`：沙盒内的 loopback HTTP / SSE / WebSocket 转发，不依赖聊天、render 或前端。
- `SandboxSession.service_forwarding_*`：复用现有沙盒通信总线和 sandboxd RPC，返回运行代次、转发数据和关闭请求。不会创建沙盒、启动应用或重新执行失败请求。
- `routes/sandbox_forwarding.py`：独立域名上的转发网关。入口绑定项目、沙盒运行代次、端口和有效期，不暴露宿主机端口。
- `routes/service_preview.py`：主应用适配层，校验当前对话权限，解析 localhost 地址，再获取对应沙盒的转发 URL。
- 消息链接、`render_preview(type="url")`：共用 ServicePreviewRenderer，只负责打开地址。

同一沙盒的身份与端口映射为 `https://p<port>-s<sandbox-generation-hash>.<preview-domain>/`。端口使用子域名而非路径前缀，使 `/api`、模块导入、CSS、图片、SSE 和 WebSocket 保持原来的根路径。不同项目或不同运行代次不共用源。应用层返回 `forwarding_url`、`port`、`path` 和用于安装访问凭证的 `url`；前端不自行猜测沙盒地址。

入口通过一次性导航安装短期 HttpOnly、Secure、host-only Cookie，然后跳转到原始路径。访问许可最长一小时；重新连接需要重新通过主应用权限校验。沙盒停止、项目删除或运行代次变化都会使旧入口无法继续使用。预览不会自动启动或重新运行服务。

## 部署

1. 为独立子域名（例如 `*.preview.flowork.top`）配置指向入口网关的 DNS 和覆盖该泛域名的 HTTPS 证书。
2. 设置 `FLOWORK_SERVICE_PREVIEW_DOMAIN=preview.flowork.top`，API 各实例使用相同的签名密钥。不可把此项设为主应用域名本身或其父域名。
3. 将预览域名全部路径（不仅 `/api`）转发到 API，并保留 Host、WebSocket Upgrade，关闭响应缓冲。
4. 沙盒需使用独立网络与 egress proxy（`SANDBOX_EGRESS_MODE=proxy`）。共享宿主机网络的沙盒不启用此能力，以免把数据库等宿主服务误当作用户应用开放。
5. 网关访问日志不要记录 bootstrap 的查询参数或 Cookie。不要给所有子域名配置主应用的登录 Cookie。

示例 Nginx 配置（证书与 upstream 路径需按部署填写；`$flowork_connection_upgrade` 沿用主入口的 Upgrade map）：

```nginx
server {
    listen 443 ssl;
    server_name *.preview.flowork.top;
    ssl_certificate /path/to/wildcard/fullchain.pem;
    ssl_certificate_key /path/to/wildcard/privkey.pem;
    access_log off; # 或使用不包含查询参数、Cookie 的独立日志格式
    client_max_body_size 8m;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $flowork_connection_upgrade;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }
}
```

## 使用与边界

Agent 先启动沙盒服务并保持进程存活，再返回普通 HTTP(S) localhost 链接，或调用 `render_preview(type="url", source="http://localhost:3000/")`。点击链接只打开 Preview，不会访问用户电脑或重启服务。

支持原始路径、查询参数、页面资源、接口请求及流式通信。请求体上限 8 MiB，WebSocket 单条消息上限 4 MiB。转发有背压，浏览器断开后取消对应传输，不终止用户服务。

应用自己的 Cookie 限定在该服务域名，不能覆盖转发凭证。当前不支持 Service Worker；不绕过应用本身的 CSP 限制。上游自签名 HTTPS 证书仍需受信任，简单开发服务建议使用沙盒 HTTP，由入口提供 HTTPS。页面在关闭预览后仍由沙盒进程生命周期管理，释放沙盒会停止服务。

## 本次验收记录（2026-10-19）

- 前端 19 项：裸 localhost 链接、Markdown 链接、render 共用的 URL renderer、原对话定位、未配置域名提示、手动重新连接、公开分享不激活私有服务链接。
- 转发层 19 项：真实 TCP / WebSocket 服务、HTTP/SSE 背压、文本和二进制消息、地址校验、凭证/端口/运行代次绑定、跨源写请求拒绝、应用 Cookie 隔离、断开时活动计数释放。现有 sandboxd RPC 测试也通过。
- 实机：独立 Bubblewrap 网络沙盒启动测试网页，经本机 HTTPS 网关在 Chromium iframe 中验证 HTML、CSS、图片、ES module、GET/POST、SSE、WebSocket 和定时刷新，全部通过。终止测试沙盒后，再次请求得到明确的 502 服务不可用页面。
- 实机夹具通过测试 Session 接入真实沙盒总线，替代了生产数据库授权查询；授权边界由上述接口测试单独验证。这不代表生产域名、登录授权链路已完成端到端验收。
- TypeScript 检查、生产前端构建、部署路径检查、Python 代码检查和依赖锁检查通过。
- 线上启用仍待泛域名 DNS/TLS 与独立沙盒网络配置。此配置未完成前，主应用返回明确的未配置提示，不回退到用户电脑或宿主机服务。

复现实机测试：使用 `scripts/acceptance/service_preview_host.py` 启动独立测试沙盒和 HTTPS 入口，再运行 `node scripts/acceptance/service-preview-browser.cjs`。测试域名为 `*.preview.qa.test`，浏览器脚本仅在本机将其映射到 loopback，并使用测试证书；不需要公开 DNS。证书路径为 `/tmp/service-preview-cert.pem` 与 `/tmp/service-preview-key.pem`。
