# Python 扫码登录 worker

本模块位于现有 `NarutoDailyADB` Python 项目中，与日常自动化独立运行。启动命令仅运行常驻 Redis worker，`REDIS_URL` 为必填项；未配置或为空时会明确报错并以退出码 1 结束，**不开放 HTTP 端口**。

网站或 Vercel 上的 `NarutoLoginClient` TypeScript 后端生成 UUID 和独立访问凭证，将登录任务写入 Redis。Python 从同一个 Redis 队列消费任务，为每项任务启动独立 Python 子进程。`mock` 驱动生成模拟二维码；`ldplayer` 驱动从固定实例池分配一个雷电实例、打开游戏并导航到二维码登录，把真实二维码 PNG 写入 OSS，再将阶段状态回写 Redis。客户端后端查询 Redis 状态并读取 OSS，浏览器通过客户端后端显示图片；网站无需通过 HTTP 连接本机 Python。留空实例池并配置模板时，仍兼容原有的复制实例模式。

`mock` 驱动的二维码仅用于联调，不能登录真实游戏。`ldplayer` 驱动只负责生成真实登录二维码；扫码后的账号绑定、会话接管和日常任务启动属于后续流程，本 worker 不会运行日常任务。

点击“扫码登录”后只等待二维码加载，最多等待 30 秒，不重复点击该按钮或“其他登录方式”。进入 QQ 消息首页会停止任务；上传内容仅为授权二维码裁剪图。

后续系统必须先确认用户已完成本次新二维码的扫码授权，再把该实例绑定到账号并运行日常任务。仅生成或展示二维码不能证明授权成功。

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

在项目根目录使用独立环境启动，也可双击 `启动扫码服务.cmd`：

```powershell
.\.venv-login-service\Scripts\python.exe -m naruto_daily.login_service
```

其他环境使用 `python -m naruto_daily.login_service`，安装后的 `naruto-login-service` 命令也可用。worker 会持续等待并消费任务；暂时断线会自动重连，停止时按 `Ctrl+C`。

也可用 `--env-file <路径>` 指定配置文件。进程环境变量优先于文件；服务不会自动读取原日常脚本的根目录 `.env`，避免混用配置。Redis 凭据和 OSS 密钥只放在服务端配置中，不放进浏览器或提交到仓库。

| 变量 | 默认值与用途 |
| --- | --- |
| `REDIS_URL` | 必填；未设置、为空或仅包含空白时，启动命令报错退出 |
| `REDIS_QUEUE_KEY` | `naruto:login:tasks`，必须与客户端相同 |
| `WORKER_CONCURRENCY` | `2`，同时运行的任务子进程数 |
| `STORAGE_DRIVER` | `local`；网站/Vercel 链路使用 `oss` |
| `LOCAL_STORAGE_DIR` | `.data/login-service/objects`，相对项目根目录解析的本地对象目录 |
| `OSS_KEY_PREFIX` | `login`，必须与客户端相同 |
| `MOCK_STEP_DELAY_MS` / `EMULATOR_DRIVER` | `600` / `mock`，模拟步骤延迟与驱动 |
| `LDPLAYER_CONSOLE_PATH` | `ldplayer` 必填，雷电控制台程序的完整路径 |
| `LDPLAYER_POOL_INDICES` | 空；推荐设为 `0,2`，使用逗号分隔的现有雷电实例索引，不复制实例 |
| `LDPLAYER_TEMPLATE` | 仅复制模式必填；配置实例池时无需模板 |
| `LDPLAYER_STATE_DIR` | `.data/login-service/ldplayer`，任务与实例状态目录；相对项目根目录解析 |
| `LDPLAYER_PACKAGE` | `com.tencent.KiHan`，游戏 Android 包名 |
| `LDPLAYER_COMMAND_TIMEOUT_SECONDS` | `20`，单条雷电控制台命令的超时秒数 |

固定实例池只支持 Redis worker 模式；配置 `LDPLAYER_POOL_INDICES` 时必须同时配置 `REDIS_URL`。旧 HTTP 本地演示只保留 `mock` 驱动，启动时会拒绝固定池配置，避免池满后错误地把任务取出并标记失败。

客户端在其自己的环境中启动或部署。Python worker 所在机器需要能够连接 Redis 和 OSS，并在网站处理登录申请期间保持运行。网站/Vercel 和 Python 使用同一 Redis、数据库、队列、OSS Bucket、地域及对象前缀；客户端不需要配置指向本机 Python 的 `LOGIN_SERVICE_URL`。

真实雷电冷启动、游戏启动和页面导航可能超过客户端默认的 120 秒。TypeScript 客户端建议设置 `SESSION_TTL_MS=600000`（10 分钟）；该值同时受本服务任务最大有效期 10 分钟限制。会话一旦过期，worker 会停止尚未完成的出码流程，客户端也无法继续查询该任务。

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

状态依次为 `queued`、`starting_emulator`、`opening_game`、`preparing_qr`、`qr_ready`；失败为 `failed`，过期为 `expired`。`qr_ready` 表示二维码已生成，不代表账号已登录。客户端会话中的 `simulation: true` 是现有跨服务协议的兼容字段，真实驱动也必须保持该值。worker 会跳过过期、格式无效或重复的任务。

按 `Ctrl+C` 优雅停止时，worker 回收子进程并将未完成任务重新入队；已完成的二维码保留至过期。队列使用直接 `BRPOP`，没有消息确认与崩溃恢复机制：进程被强制终止或机器崩溃后，已取出的任务可能无法恢复，此时需要在网站重新申请。Redis 断线期间尚未完成的操作也受任务有效期限制。

## 保留的 HTTP 接口代码

`app.py` 中保留原有 HTTP 演示接口，供独立接口测试使用。命令行入口不再启动此模式，缺少 `REDIS_URL` 时直接报错退出。以下接口的队列和任务状态保存在内存中，重启后丢失，不参与真实 Redis 队列消费。

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
| `__main__.py` | 校验必填的 `REDIS_URL` 并启动常驻 worker |
| `app.py` | 旧 HTTP 演示的 FastAPI 接口与生命周期 |
| `config.py` / `models.py` | 配置、有效期与任务校验 |
| `queue.py` / `runtime.py` | Redis 队列与内存演示队列、并发消费、状态回写和清理 |
| `mock_emulator.py` | 每个任务单独启动的 Python 工作进程，生成模拟二维码 |
| `ldplayer.py` / `ldplayer_worker.py` | 固定实例分配、持久绑定、启动和隔离工作进程 |
| `qr_navigation.py` / `templates/` | 页面识别、ADB 点击和真实二维码裁剪，不调用多模态 API |
| `pool_admin.py` | 查看实例占用，按任务 ID 显式释放实例 |
| `storage.py` | 本地与阿里云 OSS 存储适配 |

子进程生成二维码后退出。真实雷电实例会继续运行以便用户扫码；成功到达 `qr_ready` 后，即使二维码对象过期也只删除图片，不会自动关闭或释放实例。未出码即失败或过期、worker 关闭中断任务时，会按任务 ID 执行 `quit` 并释放池槽，不会删除实例。后续绑定流程或管理员 CLI 必须显式释放成功任务占用的实例。

释放池槽只会关闭雷电进程并解除任务对实例索引的占用，不会删除实例，也不会清除 QQ 或游戏的账号数据。固定池不提供账号数据擦除或账号隔离。为避免继承上一个任务仍在运行的登录页面，每次新任务启动实例后会先强制停止游戏和 QQ 进程，再重新打开游戏；这同样不会清除应用数据。

配置 `LDPLAYER_POOL_INDICES=0,2` 时，最多只有两个 `active` 或 `qr_ready` 任务占用实例。第三个任务会保持在 Redis 的 `queued` 状态，直到管理员 CLI 释放一个槽位；排队期间仍遵守客户端会话 TTL，超时后不会再处理。`WORKER_CONCURRENCY=2` 只控制本进程同时生成二维码的 worker 数，不能扩大池容量。复制模式不受固定池容量限制。

真实驱动通过 `python -u -m naruto_daily.login_service.ldplayer_worker` 运行隔离子进程。父进程只传任务 ID、有效期和雷电公开配置，不向子进程传 Redis URL 或 OSS 密钥；子进程错误只会转换为固定的安全提示。

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
