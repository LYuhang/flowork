# Docker 镜像部署

本指南对应 Project → Chat 架构、bubblewrap 沙盒和 DBOS 后台任务。
部署机器只需要 Docker Engine 和 Compose；Python、Node、Codex、Office、
draw.io 和数据库初始化工具均包含在镜像中。

## 1. 构建一次

在源码根目录生成配置。该命令生成随机数据库密码和加密密钥，并统一配置
公开 URL、CORS、Cookie 和浏览器插件的域名：

```bash
./scripts/deploy/local_server.sh init --public-url https://your-domain.example
docker compose build
```

小内存机器建议逐个构建，避免多个大型构建阶段同时占用内存：

```bash
for service in api sandboxd postgres web; do
  docker compose build "$service"
done
```

| 镜像 | Dockerfile | 负责的服务 |
| --- | --- | --- |
| `flowork-api:local` | `api/Dockerfile` | API、DBOS worker、迁移、OpenFGA 初始化、沙盒预热客户端 |
| `flowork-sandbox:local` | `api/Dockerfile`，构建参数 `VIBECANVAS_RUNTIME_ENV_BUILDER=1` | sandboxd 和受控的 Workflow 依赖安装 |
| `flowork-web:local` | `web/Dockerfile` | Nginx 静态前端、API 代理、该域名的浏览器插件下载包 |
| `flowork-postgres:local` | `postgres/Dockerfile` | PostgreSQL 15、pgvector、角色及授权清理初始化脚本 |

Compose 还会拉取固定版本的 OpenFGA、其独立 PostgreSQL 和 Valkey 镜像。
无须在宿主机安装 Python、Node、pnpm、runsc 或 bubblewrap。

当前主要版本：Python 3.11.16、容器 Node 24.21.0、pnpm 10.34.4、
Codex CLI 0.157.1、DBOS 3.0.0、draw.io 31.1.8。精确镜像摘要及依赖锁以
Dockerfile 和仓库 lock 文件为准。Codex 使用官方二进制，不再编译 Rust。

## 2. 镜像准备好后直接启动

```bash
docker compose up -d --no-build --wait
./scripts/deploy/local_server.sh verify
```

启动顺序由 Compose 管理：数据库健康 → 迁移和授权初始化 → 沙盒预热 →
API 和 DBOS worker → Web。首次启动自动建立 Project/Chat 所需数据库结构。
后台队列与定时调度由同一个 DBOS worker 负责，持久化在 PostgreSQL 中；
Valkey 用于临时通知和协调。没有 Celery worker/beat 进程。

默认只将服务端口发布到回环地址。Web 为 `127.0.0.1:9001`，
在宿主机 HTTPS 反向代理后使用；数据库、API 等内部端口不得对公网开放。

### 在另一台机器部署，无需源码

将四个构建结果推送到自己的镜像仓库，在部署用 `.env` 中设置：

```dotenv
FLOWORK_API_IMAGE=registry.example/flowork-api:release-tag
FLOWORK_SANDBOX_IMAGE=registry.example/flowork-sandbox:release-tag
FLOWORK_WEB_IMAGE=registry.example/flowork-web:release-tag
FLOWORK_POSTGRES_IMAGE=registry.example/flowork-postgres:release-tag
```

部署目录只需 `docker-compose.yml` 和已生成的 `.env`。保持 `.env` 权限为
`0600`，使用安全文件传输；数据库初始化脚本已在镜像内，没有源码绑定挂载。

```bash
docker compose pull
docker compose up -d --no-build --wait
```

也可用 `docker save` / `docker load` 离线传输四个自建镜像。其他第三方镜像
仍需拉取，或一起离线传输。`local_server.sh` 是源码环境的辅助工具，
纯镜像部署不依赖它。严格生产发布的镜像签名、TLS 数据服务、KMS/S3 等
要求另外见 [DEPLOY.md](../DEPLOY.md)。

## 3. 域名、HTTPS 和回调

配置域名 A 记录到服务器 IPv4。HTTPS 反向代理监听 80/443，将 HTTP 跳转到
HTTPS，并将应用请求转发到 `127.0.0.1:9001`；支持 SSE 长连接和 WebSocket。
证书续期必须自动化。应用端口继续只绑定回环地址。

### 页面加载性能

- 在公开 HTTPS 入口启用 HTTP/2，避免页面脚本和 API 争抢 HTTP/1.1 连接。
  Ubuntu 24.04 的 nginx 1.24 使用 `listen 443 ssl http2;`；较新版 nginx
  可使用 `http2 on;`，应用前运行 `nginx -t`。
- Web 镜像对 `/assets/` 下带内容哈希的构建文件设置一年缓存，并压缩 JS/CSS。
  外层代理应保留这些响应头；HTML、API 和 SSE 不使用这套长期缓存。
- 手动部署使用 Vite Preview 时，它默认返回 `Cache-Control: no-cache`。
  应由入口 nginx **仅对带内容哈希的静态文件**覆盖为
  `Cache-Control: public, max-age=31536000, immutable`，并启用 JS/CSS 压缩。
  不要把长期缓存应用到整个站点。修改 location 的 `add_header` 时须保留
  server 层已有的安全响应头。
- 更新版本时先上传新静态文件，再替换 `index.html`；短期保留旧哈希文件，
  使仍打开旧页面的用户可以继续加载其按需模块。

| 配置/路径 | 应使用的值 |
| --- | --- |
| `VIBECANVAS_PUBLIC_URL` | `https://your-domain.example` |
| `VIBECANVAS_API_CORS_ORIGINS` | `https://your-domain.example` |
| `WEB_ALLOWED_HOSTS` | `your-domain.example` |
| `VIBECANVAS_EXTENSION_WEB_BASE` | `https://your-domain.example` |
| `VIBECANVAS_EXTENSION_ALLOWED_ORIGINS` | `https://your-domain.example` |
| `WEB_SESSION_COOKIE_SECURE` | `true` |
| OpenRouter 回调 | 公开 URL + `/settings/openrouter/callback/<一次性 state>` |
| MCP OAuth 回调 | 公开 URL + `/api/v1/mcp-servers/oauth/callback` |
| SSO 回调（启用时） | 公开 URL + `/api/v1/auth/sso/callback` |
| 插件下载 | 公开 URL + `/downloads/flowork-extension.zip` |

**插件的域名白名单在 Web 镜像构建时写入。** 改域名后，要重新构建 Web
镜像并重新下载、加载插件；仅重启容器不能修改旧镜像里的插件包。
Web/API 默认使用同源路径，`VITE_API_BASE` 保持空。内部服务地址例如
`http://api:8000`、PostgreSQL、OpenFGA URL 继续使用 Compose 服务名。

## 4. 资源与持久化

原生、默认 Compose、release overlay 和纯镜像部署统一默认
`SANDBOX_RUNTIME=bubblewrap`、`SANDBOX_TYPE=rootless-warm`、
`SANDBOX_MAX_RESIDENT=2`、`DBOS_MAX_EXECUTOR_THREADS=2`、
`BACKGROUND_QUEUE_CONCURRENCY=1`（每个队列）。bubblewrap 共享宿主内核，
没有 gVisor 的进程快照恢复能力。Compose 中只有 sandboxd 是特权服务，
用于嵌套沙盒命名空间；API、worker、Web 不以特权模式运行。
release overlay 使用相同默认值，并保留用户的显式配置，不会自动切换到
gVisor。镜像中保留的 runsc 仅供显式选择 gVisor 时使用，默认不启动它。

小规模运行可从 2 vCPU / 2 GiB RAM 加 swap 起步，4 GiB 更宽裕。
这不表示能同时处理多个模型、浏览器和 Office 作业。镜像构建所需资源与
空闲运行不同；使用串行构建，并为镜像层、缓存和数据增长留出额外空间。

默认 Compose 将数据库、加密对象及运行时数据放在持久卷中。使用 POSIX 时可叠加
`docker-compose.posix.yml`，通过 `WORKSPACE_STORAGE_HOST_ROOT` 挂载已准备好的加密宿主目录；
API、Worker 与 sandboxd 必须使用同一目录。新建容器不清空该目录，切换后端不自动迁移旧文件。
具体步骤见 [POSIX 安装与迁移](installation.md#persistent-posix-workspaces)。
`.env` 中的加密密钥必须与这些数据一起保留。`docker compose down` 只停止并
移除容器；`docker compose down -v` 会删除应用数据，只用于明确需要重置的环境。

```bash
docker compose ps
docker compose logs --tail=100 api background_worker sandboxd
curl -f http://127.0.0.1:9001/healthz
docker compose down
```

升级使用明确版本的镜像标签或摘要，拉取后再 `up -d --no-build --wait`。
不要同时启动占用同一端口的原生服务和 Docker 服务。

## 5. 已完成的部署验证

2026-09-28 在 Ubuntu 24.04 amd64、2 vCPU / 2 GiB RAM、1 GiB swap 上完成：

- 四个自建镜像实际构建成功；整套 Compose 启动成功。
- 数据库迁移、API/Web 健康检查、bubblewrap 沙盒预热通过。
- 注册登录、一个 Project 创建两个 Chat、共享文件目录、OpenRouter 回调 URL 通过。
- Workflow 在容器沙盒中执行并完成。
- 从仅含 `docker-compose.yml` 和 `.env` 的目录，以 `--no-build` 启动成功。
- 测试结束后已关闭测试容器；线上继续使用原生服务。

Project 共享工作目录和沙盒；各 Chat 的 Codex 会话、MCP 回调及取消控制独立。
并发修改同一文件仍需要协作。应用镜像重建会包含当前源码的这项改动。

生成的 `.env` 包含数据库密码和数据加密密钥，不能提交到 Git。
修改域名时可再次执行 `init --public-url ...`，脚本保留原有密钥并更新 URL 配置。


## 常驻部署的资源配额

常驻部署需要 cgroup v2 委派。CPU、内存配额、切流期间容量检查、原生 systemd 启动方式与 Docker 配置见 [Resident deployment resources](resident-deployments.md)。


## SubAgent 的 Skill / MCP 环境

API 镜像已通过锁定的 `requirements-runtime.txt` 安装 MCP SDK、LangChain
和 JSON Schema。此功能不需要新增常驻容器；更新源码后应重建 API 镜像，并按正常
升级流程执行数据库迁移和 OpenFGA 引导。迁移 145 保存任务、部署身份的资源委托。

- Skill 由平台发布并物化到只读 `/skills` 目录，不需要把用户 Skill 打进镜像。
  发布新内容后，下一次执行使用最新版，已有执行继续使用自己的快照。
- stdio MCP 的命令及依赖必须存在于容器内的沙盒运行环境。
  开发者电脑上的绝对路径不能直接作为容器内命令使用。
- HTTP/SSE MCP 使用资源页面中保存的连接配置与凭据；出口策略必须允许目标地址。
  凭据不要放入 Workflow JSON、Prompt 或 Docker 构建参数。
- SubAgent 只加载节点所选 MCP。任务与部署以自己的执行身份访问资源，
  不依赖创建者的浏览器会话或 Chat 连接。

连接生命周期和撤权清理见[架构说明](architecture.md#subagent-workflow-resources)。
此节说明环境要求，不代表已重新完成本次功能的整套 Docker 实测。
