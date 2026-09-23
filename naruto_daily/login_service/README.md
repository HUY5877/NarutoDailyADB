# Python 扫码登录任务服务

本模块位于现有 `NarutoDailyADB` Python 项目中。服务端使用 Python，独立客户端仓库 `NarutoLoginClient` 的前端和后端继续使用 TSX / TypeScript。

客户端 TS 后端生成 UUID 和独立访问凭证，投递到本服务的模拟 Redis FIFO 队列。本服务为每项任务启动独立 Python 子进程，模拟雷电启动、打开火影忍者以及“与安卓好友玩 → 切换账号 → 二维码登录”，生成 PNG，随后写入本地模拟 OSS 或真实阿里云 OSS。客户端根据 ID 跟踪对象存储，读取图片后显示。

当前二维码可被扫码器识别，但内容是模拟会话数据，**不能登录真实游戏**。雷电与实际页面点击、扫码授权结果和游戏会话接管都未实现。

## 启动

需要 Python 3.11+。服务端无需 Node.js。在项目根目录执行：

```powershell
python -m pip install -e ".[login-service]"
python -m naruto_daily.login_service
```

安装后也可使用 `naruto-login-service` 命令。服务默认监听 `127.0.0.1:4320`。

本机已建立独立验证环境，可直接执行：

```powershell
cd E:\code\NarutoDailyADB
.\.venv-login-service\Scripts\python.exe -m naruto_daily.login_service
```

客户端另外启动：

```powershell
cd E:\code\NarutoLoginClient
npm ci
npm run dev
```

打开 `http://127.0.0.1:4300`。客户端页面、API 和开发热更新共用这个端口，只需设置客户端 `PORT` 即可一起调整。如果客户端已完成 `npm run build`，也可用 `npm start` 启动，地址相同。Python 任务服务仍独立监听 `127.0.0.1:4320`。

默认配置直接可用。自定义时将本目录 `.env.example` 复制为 `.env`，也可通过 `--env-file <路径>` 指定。进程环境变量优先于文件；服务不会自动读取原日常脚本的根目录 `.env`，避免混用配置。

以下均为可选设置，只在需要调整时加入本服务的 `.env`：

| 变量 | 默认值与用途 |
| --- | --- |
| `HOST` / `PORT` | `127.0.0.1` / `4320`，任务服务监听地址；变更后同步修改客户端 `LOGIN_SERVICE_URL` |
| `LOCAL_STORAGE_DIR` | `.data/login-service/objects`，相对项目根目录解析的本地对象目录 |
| `WORKER_CONCURRENCY` / `MAX_QUEUE_SIZE` / `MAX_TASKS` | `2` / `100` / `500`，工作进程并发数、队列容量和任务总数上限 |
| `TASK_RETENTION_MS` | `60000`，过期任务状态保留时间 |
| `MOCK_STEP_DELAY_MS` / `EMULATOR_DRIVER` | `600` / `mock`，模拟步骤延迟与驱动 |

## 接口兼容

全部 `/internal/*` 接口要求 `X-Service-Token`，由两边的 `SERVICE_TOKEN` 配置。它是服务间共享凭证，与每次申请的独立访问凭证不同。默认值 `local-development-token` 只允许本机回环地址，无需写入 `.env`；改为非回环监听时，必须为客户端后端与任务服务配置相同的强随机值。

| 接口 | 行为 |
| --- | --- |
| `GET /health` | 服务健康状态、模拟标记和队列统计 |
| `POST /internal/tasks` | `{id,accessToken,createdAt,expiresAt}`，成功返回 `202 {id,status:"queued"}` |
| `GET /internal/tasks/:id` | 额外携带 `X-Session-Token`，返回任务状态，错误凭证为 404 |
| `HEAD /internal/objects/:id/qr.png` | 对象存在且有效为 200，未生成/过期为 404 |
| `GET /internal/objects/:id/qr.png` | 返回 PNG，禁用缓存 |

状态依次为 `queued`、`starting_emulator`、`opening_game`、`preparing_qr`、`qr_ready`；失败为 `failed`，过期为 `expired`。`qr_ready` 表示二维码存在，不代表账号已登录。

同一 UUID、访问凭证及时间戳重试不重复启动工作进程。冲突请求返回 409；队列或任务表满返回 429。有效期由客户端指定，默认 120 秒，最大 10 分钟；过期状态默认保留 60 秒。响应不包含访问凭证或 OSS SDK 原始报错。

## 模块分工

| 文件 | 职责 |
| --- | --- |
| `app.py` / `__main__.py` | FastAPI HTTP 接口、Uvicorn 生命周期及入口 |
| `config.py` / `models.py` | 配置、有效期与任务校验 |
| `queue.py` / `runtime.py` | LPUSH / BRPOP 语义模拟、并发消费和清理 |
| `mock_emulator.py` | 每个任务单独启动的 Python 工作进程，生成模拟二维码 |
| `storage.py` | 本地与阿里云 OSS 存储适配 |

队列和任务表在内存中，默认并发 2、队列上限 100、任务上限 500；单个服务仅使用一个 ASGI worker。重启会丢失任务，客户端需要重新申请。模拟进程生成二维码后退出，过期/异常/服务关闭时也会回收。

后续真实雷电适配应在 `runtime.py` 的驱动分支替换工作进程，完成独立实例创建、ADB 连接、游戏启动、二维码截图和实例销毁。`EMULATOR_DRIVER=ldplayer` 当前明确失败，不会假装已启动雷电。

## 对象存储

默认 `STORAGE_DRIVER=local`，本地目录为项目根目录下的 `.data/login-service/objects`，对象键仍为 `login/{id}/qr.png`，旁边的 `metadata.json` 保存过期时间。写入使用临时文件与原子替换；文件夹不会作为公共静态资源开放。过期对象及上次运行遗留的过期对象会定期清理。

真实 OSS 使用 Python `oss2` SDK，在两边配置一致的 Bucket 和地域：

```dotenv
STORAGE_DRIVER=oss
OSS_REGION=oss-cn-shanghai
OSS_BUCKET=your-private-bucket
OSS_ACCESS_KEY_ID=your-access-key-id
OSS_ACCESS_KEY_SECRET=your-access-key-secret
# OSS_STS_TOKEN=your-temporary-token
```

`OSS_KEY_PREFIX` 默认是 `login`，表示 Bucket 内对象键的目录前缀，例如 `login/{id}/qr.png`，无需重复配置；自定义时两边必须一致。

服务端拥有指定前缀的写入/读取/删除权限，客户端 TS 后端仅需读取权限；凭据可以不同。OSS 对象使用 HTTPS、私有 ACL、PNG 类型、`no-store` 和过期时间元数据。浏览器通过自己的后端读图片，不获得 OSS 密钥，也不需要 OSS CORS。

真实 OSS 已完成二维码上传与客户端读取联调。过期元数据不等于云端生命周期规则，应配置此前缀的 Bucket 生命周期，兜底清理服务崩溃时的遗留对象。切换 OSS 不会自动启用真实雷电。

## 验证

```powershell
python -m pip install -e ".[login-service,login-service-test]"
python -m pytest tests/test_login_service_http.py tests/test_login_service_runtime.py tests/test_login_service_storage.py tests/test_packaging.py
```

测试覆盖队列顺序、并发上限、幂等投递、接口与会话隔离、真实 Python 子进程、PNG 输出、过期与关闭回收、存储清理及 OSS 适配契约。客户端接口测试和浏览器联调另在 TS 客户端侧验证。
