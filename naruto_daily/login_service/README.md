# Python 扫码登录 worker

本模块位于现有 `NarutoDailyADB` Python 项目中，与日常自动化独立运行。配置 `REDIS_URL` 后，启动命令会运行常驻 Redis worker，**不开放 HTTP 端口**。

网站或 Vercel 上的 `NarutoLoginClient` TypeScript 后端生成 UUID 和独立访问凭证，将登录任务写入 Redis。Python 从同一个 Redis 队列消费任务，为每项任务启动独立 Python 子进程，模拟雷电启动、打开火影忍者以及“与安卓好友玩 → 切换账号 → 二维码登录”，把二维码 PNG 写入 OSS，并将阶段状态回写 Redis。客户端后端查询 Redis 状态并读取 OSS，浏览器通过客户端后端显示图片；网站无需通过 HTTP 连接本机 Python。

当前二维码可被扫码器识别，但内容是模拟会话数据，**不能登录真实游戏**。雷电与实际页面点击、扫码授权结果和游戏会话接管都未实现。

## 配置和启动

需要 Python 3.11+，服务端无需 Node.js。在项目根目录安装依赖：

```powershell
python -m pip install -e ".[login-service]"
```

配置文件位于 `naruto_daily/login_service/.env`。首次配置可复制本目录的 `.env.example`；已有 `.env` 时直接编辑，保留其中的 OSS 配置。最少添加：

```dotenv
REDIS_URL=redis://127.0.0.1:6379/0
```

上方地址仅表示本机 Redis。连接实际 Redis 时使用自己的地址和凭据，例如 `redis://:your-password@redis.example.com:6379/0`；需要 TLS 时使用 `rediss://`。Redis 数据库由 URL 的路径决定，未指定时默认 DB 0。Python 和客户端必须使用相同数据库及队列名。

本机已有独立环境，可直接执行：

```powershell
cd E:\code\NarutoDailyADB
.\.venv-login-service\Scripts\python.exe -m naruto_daily.login_service
```

其他环境使用 `python -m naruto_daily.login_service`，安装后的 `naruto-login-service` 命令也可用。worker 会持续等待并消费任务；暂时断线会自动重连，停止时按 `Ctrl+C`。

也可用 `--env-file <路径>` 指定配置文件。进程环境变量优先于文件；服务不会自动读取原日常脚本的根目录 `.env`，避免混用配置。Redis 凭据和 OSS 密钥只放在服务端配置中，不放进浏览器或提交到仓库。

| 变量 | 默认值与用途 |
| --- | --- |
| `REDIS_URL` | 未设置时使用下方的旧 HTTP 本地演示；设置后启动 Redis worker |
| `REDIS_QUEUE_KEY` | `naruto:login:tasks`，必须与客户端相同 |
| `WORKER_CONCURRENCY` | `2`，同时运行的任务子进程数 |
| `STORAGE_DRIVER` | `local`；网站/Vercel 链路使用 `oss` |
| `LOCAL_STORAGE_DIR` | `.data/login-service/objects`，相对项目根目录解析的本地对象目录 |
| `OSS_KEY_PREFIX` | `login`，必须与客户端相同 |
| `MOCK_STEP_DELAY_MS` / `EMULATOR_DRIVER` | `600` / `mock`，模拟步骤延迟与驱动 |

客户端在其自己的环境中启动或部署。Python worker 所在机器需要能够连接 Redis 和 OSS，并在网站处理登录申请期间保持运行。网站/Vercel 和 Python 使用同一 Redis、数据库、队列、OSS Bucket、地域及对象前缀；客户端不需要配置指向本机 Python 的 `LOGIN_SERVICE_URL`。

## Redis 消息和状态

TypeScript 后端通过 `LPUSH` 投递，Python 通过 `BRPOP` 按 FIFO 顺序取出任务。默认队列为 `naruto:login:tasks`，消息格式如下；`createdAt` 和 `expiresAt` 均为带时区的 ISO 8601 字符串：

```json
{
  "version": 1,
  "id": "客户端生成的 UUID",
  "createdAt": "2026-09-23T08:00:00.000Z",
  "expiresAt": "2026-09-23T08:02:00.000Z",
  "objectKey": "login/客户端生成的 UUID/qr.png"
}
```

示例时间戳仅说明结构，实际任务必须使用当前创建时间和有效的过期时间。队列消息不携带明文访问凭证。

对应会话位于 `${REDIS_QUEUE_KEY}:sessions:${id}`，例如 `naruto:login:tasks:sessions:<UUID>`。客户端先创建会话，Python 回写处理阶段时保留客户端的 `tokenHash` 和原有 TTL，不延长会话有效期。

状态依次为 `queued`、`starting_emulator`、`opening_game`、`preparing_qr`、`qr_ready`；失败为 `failed`，过期为 `expired`。`qr_ready` 表示二维码已生成，不代表账号已登录。worker 会跳过过期、格式无效或重复的任务。

按 `Ctrl+C` 优雅停止时，worker 回收子进程并将未完成任务重新入队；已完成的二维码保留至过期。队列使用直接 `BRPOP`，没有消息确认与崩溃恢复机制：进程被强制终止或机器崩溃后，已取出的任务可能无法恢复，此时需要在网站重新申请。Redis 断线期间尚未完成的操作也受任务有效期限制。

## 旧 HTTP 本地演示兼容

未配置 `REDIS_URL` 时，仍保留原有 HTTP 演示模式，默认监听 `127.0.0.1:4320`。此模式的队列和任务状态保存在内存中，重启后丢失，不参与真实 Redis 队列消费。

配合支持旧 HTTP 模式的本地客户端时，在 `E:\code\NarutoLoginClient` 执行 `npm ci`、`npm run dev`，打开 `http://127.0.0.1:4300`。客户端页面与 API 共用该端口，Python 独立监听 `4320`。客户端 `LOGIN_SERVICE_URL` 仅用于这条旧链路。

全部 `/internal/*` 接口要求 `X-Service-Token`，由两边的 `SERVICE_TOKEN` 配置。它是服务间共享凭证，与每次申请的独立访问凭证不同。默认值 `local-development-token` 只允许本机回环地址，无需写入 `.env`；改为非回环监听时，必须为客户端后端与任务服务配置相同的强随机值。

| 接口 | 行为 |
| --- | --- |
| `GET /health` | 服务健康状态、模拟标记和队列统计 |
| `POST /internal/tasks` | `{id,accessToken,createdAt,expiresAt}`，成功返回 `202 {id,status:"queued"}` |
| `GET /internal/tasks/:id` | 额外携带 `X-Session-Token`，返回任务状态，错误凭证为 404 |
| `HEAD /internal/objects/:id/qr.png` | 对象存在且有效为 200，未生成/过期为 404 |
| `GET /internal/objects/:id/qr.png` | 返回 PNG，禁用缓存 |

同一 UUID、访问凭证及时间戳重试不重复启动工作进程。冲突请求返回 409；队列或任务表满返回 429。有效期由客户端指定，默认 120 秒，最大 10 分钟；过期状态默认保留 60 秒。响应不包含访问凭证或 OSS SDK 原始报错。

HTTP 模式可用 `HOST` / `PORT` 修改监听地址，用 `MAX_QUEUE_SIZE` / `MAX_TASKS` 调整内存队列与任务总数上限，默认分别为 `100` / `500`；`TASK_RETENTION_MS` 默认 `60000`。仅使用一个 ASGI worker。

## 模块分工

| 文件 | 职责 |
| --- | --- |
| `__main__.py` | 根据 `REDIS_URL` 选择常驻 worker 或 HTTP 演示入口 |
| `app.py` | 旧 HTTP 演示的 FastAPI 接口与生命周期 |
| `config.py` / `models.py` | 配置、有效期与任务校验 |
| `queue.py` / `runtime.py` | Redis 队列与内存演示队列、并发消费、状态回写和清理 |
| `mock_emulator.py` | 每个任务单独启动的 Python 工作进程，生成模拟二维码 |
| `storage.py` | 本地与阿里云 OSS 存储适配 |

模拟进程生成二维码后退出，过期、异常或服务关闭时也会回收。默认并发为 2，可通过 `WORKER_CONCURRENCY` 调整。

后续真实雷电适配应在 `runtime.py` 的驱动分支替换工作进程，完成独立实例创建、ADB 连接、游戏启动、二维码截图和实例销毁。`EMULATOR_DRIVER=ldplayer` 当前明确失败，不会假装已启动雷电。

## 对象存储

默认 `STORAGE_DRIVER=local`，可用于本机验证。本地目录为项目根目录下的 `.data/login-service/objects`，对象键为 `login/{id}/qr.png`，旁边的 `metadata.json` 保存过期时间。写入使用临时文件与原子替换；过期对象及上次运行遗留的过期对象会定期清理。Redis worker 不提供本地文件的 HTTP 访问，因此网站/Vercel 链路应使用真实 OSS。

真实 OSS 使用 Python `oss2` SDK，在服务专用 `.env` 中保留或填写以下配置，并与客户端使用同一 Bucket 和地域：

```dotenv
STORAGE_DRIVER=oss
OSS_REGION=oss-cn-shanghai
OSS_BUCKET=your-private-bucket
OSS_ACCESS_KEY_ID=your-access-key-id
OSS_ACCESS_KEY_SECRET=your-access-key-secret
# OSS_STS_TOKEN=your-temporary-token
```

`OSS_KEY_PREFIX` 默认是 `login`，表示 Bucket 内对象键的目录前缀，例如 `login/{id}/qr.png`，无需重复配置；自定义时两边必须一致，队列中的 `objectKey` 也应使用相同前缀。

服务端拥有指定前缀的写入/读取/删除权限，客户端 TS 后端仅需读取权限；凭据可以不同。OSS 对象使用 HTTPS、私有 ACL、PNG 类型、`no-store` 和过期时间元数据。浏览器通过自己的后端读图片，不获得 OSS 密钥，也不需要 OSS CORS。

真实 OSS 已完成二维码上传与客户端读取联调。过期元数据不等于云端生命周期规则，应配置此前缀的 Bucket 生命周期，兜底清理服务崩溃时的遗留对象。切换 OSS 不会自动启用真实雷电。

## 验证

```powershell
python -m pip install -e ".[login-service,login-service-test]"
python -m pytest tests/test_login_service_runtime.py tests/test_login_service_http.py tests/test_login_service_storage.py tests/test_login_service_redis.py tests/test_login_service_worker.py tests/test_packaging.py
```

Redis worker 的验证应包括 FIFO 消费、消息校验、会话状态与 TTL 保留、重连及关闭处理；HTTP 模式继续验证接口隔离、并发、PNG 输出、过期清理与 OSS 适配。实际网站联调需要客户端与 Python 使用同一 Redis 和 OSS 配置。
