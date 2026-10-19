# 沙盒服务转发与 Preview

## 分层

- `services/sandbox/service_forwarding.py`：沙盒内的 loopback HTTP / SSE / WebSocket 转发，不依赖聊天、render 或前端。
- `SandboxSession.service_forwarding_*`：复用现有沙盒通信总线和 sandboxd RPC，返回运行代次、转发数据和关闭请求。不会创建沙盒、启动应用或重新执行失败请求。
- `routes/sandbox_forwarding.py`：支持同域路径和可选独立域名的转发网关。入口绑定项目、沙盒运行代次、端口和有效期，不暴露宿主机端口。
- `routes/service_preview.py`：主应用适配层，校验当前对话权限，解析 localhost 地址，再获取对应沙盒的转发 URL。
- 消息链接、`render_preview(type="url")`：共用 ServicePreviewRenderer，只负责打开地址。

## 同域名路径入口（默认）

未配置 `FLOWORK_SERVICE_PREVIEW_DOMAIN` 时，使用 `https://flowork.top/preview/<签名沙盒标识>/<端口>/...`。
签名标识绑定用户、项目、沙盒运行代次和端口，有效期一小时；不能通过修改端口访问其它服务。主应用生成地址，沙盒转发层不感知 Preview。

入口只需反向代理 `/preview/` 到 API，并转发 WebSocket Upgrade；无需新增 DNS、证书或公网端口。保留 `/preview`（无尾斜线）供原有预览页面使用。访问日志不得记录包含访问凭证的路径。

响应强制 `CSP sandbox`，不授予 `allow-same-origin`，并限制资源、接口和 WebSocket 请求只到当前服务前缀。代理剥离主应用 Cookie、Authorization 和上游 Set-Cookie，防止生成的页面继承主应用身份。此模式不支持应用 Cookie、localStorage、Service Worker；需要这些能力的完整应用应使用下面的独立域名模式。

适配 HTML 的 base、资源属性和 CSS url，以及页面的 fetch、XHR、EventSource、WebSocket 地址。对于硬编码根路径的模块导入、路由器及开发服务器热更新等场景，应将应用 base path 配置为转发前缀，不能假定任意网页都无需适配。普通单文件 HTML 时钟无需修改。

## 独立域名入口（可选）

同一沙盒的身份与端口映射为 `https://p<port>-s<sandbox-generation-hash>.<preview-domain>/`。端口使用子域名而非路径前缀，使 `/api`、模块导入、CSS、图片、SSE 和 WebSocket 保持原来的根路径。不同项目或不同运行代次不共用源。应用层返回 `forwarding_url`、`port`、`path` 和用于安装访问凭证的 `url`；前端不自行猜测沙盒地址。

入口通过一次性导航安装短期 HttpOnly、Secure、host-only Cookie，然后跳转到原始路径。访问许可最长一小时；重新连接需要重新通过主应用权限校验。沙盒停止、项目删除或运行代次变化都会使旧入口无法继续使用。预览不会自动启动或重新运行服务。


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
- 上述记录为前一版独立域名实现的验证。当前路径模式的真实应用内 Agent 对话验收见下方记录；底层测试不替代用户流程验收。

复现实机测试：使用 `scripts/acceptance/service_preview_host.py` 启动独立测试沙盒和 HTTPS 入口，再运行 `node scripts/acceptance/service-preview-browser.cjs`。测试域名为 `*.preview.qa.test`，浏览器脚本仅在本机将其映射到 loopback，并使用测试证书；不需要公开 DNS。证书路径为 `/tmp/service-preview-cert.pem` 与 `/tmp/service-preview-key.pem`。

## 服务存活与项目 TTL

TTL 的权威在 `sandboxd`：`SandboxService._reap` 定期调用 `SandboxManager.sweep_idle`。
沙盒按 Project 共享，不按 Chat 单独回收。任一 Chat 正在运行，或其启动的服务仍监听 TCP 端口，都暂停整个 Project 的空闲 TTL；最后一个服务及最后一次 Agent 执行结束后，从该时刻重新计时。既有 resident 部署租约不改变。

维护周期复用 runtime bus 查询沙盒内 `/proc` 的监听端口及进程归属，不增加数据库记录或独立监控进程。服务通过启动环境中的 Chat ID（及进程祖先/工作目录）归属对话；平台运行时自身的内部监听不算用户服务。观察失败时保留暂停状态，不把“未知”误判为空闲。关闭 Preview 或浏览器不会停止服务。

项目状态接口返回 `serving_chat_ids` 和 `serving_ports`。历史条目复用实时运行状态，再合并服务归属：运行、等待交互、正在停止或仍有服务时，聊天气泡使用绿色轮廓；其余灰色。前端现有 5 秒项目状态刷新获取服务变化，TTL 不依赖前端是否在线。

Preview 工具栏“停止服务”通过认证接口校验原对话和项目，绑定沙盒运行代次及已观察到的端口进程身份。读取绑定 procfs 时，将宿主机编号转换为沙盒 PID namespace 编号，并用 pidfd 固定进程身份；只向对应监听进程发送 SIGTERM，不终止共享沙盒或整个进程组。最多等待 3 秒确认端口关闭；未关闭、服务已替换或权限失败均明确报错，不自动重试或强制杀进程。停止一个服务不影响其它端口服务的 TTL 保持。

## 本轮验收记录

- 通过真实应用对话，使用 Terra 启动时钟 HTML 服务，并同时返回 localhost 链接和 render Preview；两种入口均成功。
- 原对话 `62723301-1c31-4bd1-89ec-48466289c13e`：恢复原时钟后，页面时间从 17:14:24 更新至 17:14:26；应用刷新后重新打开正常，浏览器未记录错误。
- 同域路径 iframe 无法读取父页面 DOM 或 localStorage；自身 fetch('/') 能通过沙盒服务转发成功。
- 服务生命周期相关后端回归：79 项通过，覆盖真实监听进程停止、过期进程身份、多个 Chat/服务和已有沙盒回收逻辑。
- 真实 UI 生命周期验收通过：同一 Project 两个 Chat 分别启动 8080/8081，关闭 Preview 后两服务继续运行；逐一点击红色停止按钮均返回成功。停止 8080 后仅其 Chat 变灰，8081 保持绿色且 TTL 暂停；停止最后的 8081 后，两 Chat 变灰，TTL 从该时刻重新计时。
- 实测发现并修复嵌套 Bubblewrap 绑定 procfs 时的 PID 编号差异：监听扫描先转换到沙盒 PID，再通过 pidfd 确认进程身份；真实 namespace 内停止及上述生产 UI 操作均通过。

## 跨 API worker 与远端沙盒

API worker 不维护服务进程表，也不对本机 PID 发送信号。查询和停止均通过现有 sandboxd RPC，按租户和 Project 工作区定位已加载的沙盒，再由该沙盒运行时检查端口所属进程并执行停止。停止请求附带 runtime_id 和观察到的进程启动时间，避免旧 Preview 停止重建后的服务。只有确认监听消失才返回成功；失败保留错误，不自动重发停止指令。

多个 API worker 应使用同一个权威 sandboxd 入口。沙盒位于其它机器时使用 `SANDBOX_SERVICE_ENDPOINT=grpcs://host:port`，配置 `SANDBOX_SERVICE_CA_FILE`、`SANDBOX_SERVICE_CERT_FILE`、`SANDBOX_SERVICE_KEY_FILE`，由已有 mTLS RPC 通道通信。所有 API 实例还需使用一致的预览签名密钥。不依赖浏览器请求粘在同一个 API worker 上。

当前配置是明确的 sandboxd 入口，并未实现多个独立 sandboxd 的自动归属路由；不能在互不共享 Project 沙盒状态的 sandboxd 前直接随机负载均衡。未来扩展沙盒节点池，需要在入口按 Project 找到归属节点。这与多个 API worker 访问同一远端沙盒服务是不同范围。

跨 worker 回归：两个独立 RemoteSandboxManager 经真实 gRPC 连接同一测试 sandboxd，A 查询、B 停止、A 再次查询均使用同一租户/Project；同端口的其它 Project 不受影响，错误租户无法找到该沙盒。相关 16 项测试通过（隔离运行，无数据库依赖）；此测试不等于多台物理机器或多 sandboxd 节点池验收。原时钟服务在停止验收后已通过真实 Agent 对话恢复，页面时间持续更新，第二个验收服务保持停止。

### 后续扩展方向：单管理 worker，分布式执行资源

集中管理服务可以先维持单 worker，API 仍可多 worker。管理服务负责 Project 到沙盒实例及执行节点的归属、生命周期与请求路由，执行节点负责本机沙盒及进程操作。这样不需要为管理服务引入多主协调。归属信息需要持久化并在管理服务重启后核对真实实例；管理通道暂时不可用不能被当作服务已经停止，也不能自动重放执行操作。

这是后续节点池方案，并非本次已实现能力。目前 sandboxd 仍负责所在机器的沙盒管理，现有远程 RPC 支持 API 与 sandboxd 分机部署；分布式执行节点的注册、持久归属和重连恢复仍需单独实现与验收。
