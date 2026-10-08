# Python 扫码登录 worker

本模块位于现有 `NarutoDailyADB` Python 项目中，作为常驻 Redis worker 接收扫码任务，并在扫码登录后复用原完整日常流程。`REDIS_URL` 为必填项；未配置或为空时会明确报错并以退出码 1 结束，**不开放 HTTP 端口**。

网站或 Vercel 上的 `NarutoLoginClient` TypeScript 后端生成 UUID 和独立访问凭证，将登录任务写入 Redis。Python 从同一个 Redis 队列消费任务，为每项任务启动独立 Python 子进程。`mock` 驱动生成模拟二维码；`ldplayer` 驱动从固定实例池分配一个雷电实例、打开游戏并导航到二维码登录，把真实二维码 PNG 写入 OSS，再将阶段状态回写 Redis。客户端后端查询 Redis 状态并读取 OSS，浏览器通过客户端后端显示图片；网站无需通过 HTTP 连接本机 Python。留空实例池并配置模板时，仍兼容原有的复制实例模式。

`mock` 驱动的二维码仅用于联调，不能登录真实游戏，也不运行日常。`ldplayer` 子进程生成二维码后继续等待扫码；父进程收到二维码即上传并回写 `qr_ready`。子进程复用原登录模块完成可选第二次授权、开始游戏、清理弹窗和回到主界面，确认登录成功后在同一子进程、同一 ADB serial 上继续执行原 `run all` 的后续日常动作。

模拟器无需安装 QQ：未安装时，点击“与安卓 QQ 好友玩”后识别游戏内的“QQ 授权登录 / 使用QQ手机版扫码授权登录”页面；已安装时，继续通过 QQ 的切换账号、其他登录方式和扫码登录进入授权页。两种页面均先确认授权场景，再按实际二维码边界裁剪并添加白边，不依赖同一个固定裁剪区域。游戏内网页的标题、扫码说明和失效提示模板只包含公共界面文字，不包含用户二维码。

安装 QQ 的流程点击“扫码登录”后只等待二维码加载，最多等待 30 秒，不重复点击该按钮或“其他登录方式”。进入 QQ 消息首页或发现二维码失效会停止任务；上传内容仅为有效授权二维码的裁剪图。未安装 QQ 的路径在扫码后继续复用相同的登录收尾与日常流程。

仅生成或展示二维码不能证明授权成功。服务不会执行原保存账号的选择/授权前缀，也不要求 `LOGIN_ACCOUNT_NAME`；从原配置中的 `finish_login_authorization` 衔接，确认主界面后才开始日常。原日常任务的内容和执行顺序保持一致。

## 配置和启动

需要 Python 3.11+ 和雷电模拟器 9，服务端无需 Node.js。在新电脑 clone 本仓库后，进入项目根目录创建虚拟环境、安装依赖，并只复制根目录的配置示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[login-service]"
Copy-Item .env.example .env
```

已有 `.env` 时直接编辑，不要重复复制覆盖。模型、账号、Redis、OSS 和雷电配置统一放在项目根目录 `.env`，无需在服务模块内另建配置文件。运行 Redis worker 至少设置：

```dotenv
REDIS_URL=redis://127.0.0.1:6379/0
```

上方地址仅表示本机 Redis。连接实际 Redis 时使用自己的地址和凭据，例如 `redis://:your-password@redis.example.com:6379/0`；需要 TLS 时使用 `rediss://`。Redis 数据库由 URL 的路径决定，未指定时默认 DB 0。Python 和客户端必须使用相同数据库及队列名。

在项目根目录使用独立环境启动，也可双击 `启动扫码服务.cmd`：

```powershell
.\.venv\Scripts\python.exe -m naruto_daily.login_service
```

已有 `.venv-login-service` 时也可继续用该环境的 Python，双击脚本会优先选择它。激活其他已安装依赖的环境后可使用 `python -m naruto_daily.login_service`，安装后的 `naruto-login-service` 命令也可用。worker 会持续等待并消费任务；暂时断线会自动重连，停止时按 `Ctrl+C`。

服务入口和实例池管理命令 `python -m naruto_daily.login_service.pool_admin` 默认都读取项目根目录 `.env`，不依赖启动工作目录。需要另一套服务参数时，可显式传入 `--env-file <路径>`；Redis、OSS 等非模型变量继续以进程环境优先。日常任务通过 `DAILY_CONFIG_PATH` 加载 JSON，账号等普通任务变量也读取根目录 `.env`，不随 JSON 路径改变。

模型配置统一只读取项目根目录 `.env` 中的 `MULTIMODAL_YUWAN_API_KEY`、`MULTIMODAL_YUWAN_MODEL`、`MULTIMODAL_DEEPSEEK_API_KEY`、`MULTIMODAL_DEEPSEEK_MODEL`，与普通脚本共用同一份。进程环境及 `--env-file` 指定文件均不能覆盖这些变量。启动时按 key 是否非空冻结有效渠道，不请求上游校验密钥；启用的渠道必须填写独立模型名。每次模型 HTTP 尝试通过同一 Redis 的 `INCR` 计数轮询渠道，详见[多模态路由说明](../multimodal/README.md)。修改配置后，请重启正在运行的任务和常驻 worker。凭据不放进浏览器或提交到仓库。

查看固定实例池占用也使用同一环境和配置：

```powershell
.\.venv\Scripts\python.exe -m naruto_daily.login_service.pool_admin list
```

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
| `DAILY_CONFIG_PATH` | 项目根目录的 `config.example.json`；复用原日常任务配置，相对路径以项目根目录解析 |
| `DAILY_TIMEOUT_SECONDS` | 默认 `3000` 秒；扫码授权成功后起算，登录收尾和全部日常共用。可设置更短时限；旧配置中超过3000秒的值按3000秒执行，最多50分钟 |
| `RUN_LOG_RETENTION_SECONDS` | `86400`，最终执行记录和日志的保留秒数，范围 60–604800；运行中的 TTL 额外加上日常超时预算 |
| `RUN_LOG_MAX_ENTRIES` | `2000`，每个任务在 Redis 保留的最新日志条数，范围 100–10000 |

日常 JSON 及引用的图片必须一同保留，图片路径相对 JSON 所在目录解析。若通过 wheel 安装服务，请将原配置和完整 `assets/` 目录放在本机，并将 `DAILY_CONFIG_PATH` 指向该 JSON；无需修改 JSON 中原有的 `adb_path` 或 `serial`，执行时强制使用本次任务分配到的实例。

固定实例池只支持 Redis worker 模式；配置 `LDPLAYER_POOL_INDICES` 时必须同时配置 `REDIS_URL`。旧 HTTP 本地演示只保留 `mock` 驱动，启动时会拒绝固定池配置，避免池满后错误地把任务取出并标记失败。

客户端在其自己的环境中启动或部署。Python worker 所在机器需要能够连接 Redis 和 OSS，并在网站处理登录申请期间保持运行。网站/Vercel 和 Python 使用同一 Redis、数据库、队列、OSS Bucket、地域及对象前缀；客户端不需要配置指向本机 Python 的 `LOGIN_SERVICE_URL`。

真实雷电冷启动、游戏启动和等待扫码可能超过客户端默认的 120 秒。TypeScript 客户端建议设置 `SESSION_TTL_MS=600000`（10 分钟）；该值同时受本服务任务最大有效期 10 分钟限制。会话到期时仍未完成扫码授权，会停止子进程、记录登录超时，保留模拟器60秒后再释放实例。观察到扫码后的二次授权页、开始游戏页或游戏主界面后，子进程立即发送 `authorized`，不等待点击按钮及点击前后的延时。子进程同时发送本次执行的绝对截止时间 `executionDeadline`，父子进程共用同一截止时间。后续授权按钮操作、加载、清弹窗、额外主界面核验和全部日常任务共享最多50分钟，进入主界面或切换任务时不重新计时，也不再受二维码期限限制。各阶段不再单独设置时间预算；普通图片识别和当前鱼玩 OpenAI 兼容导航的模型单次请求最多30秒，只有HTTP 200算成功，其他状态（包括400、401、403）、网络异常和超时均等待10秒、重新截图继续重试，直到成功、取消或总时限到达。到达总截止时间后父进程终止执行子进程，保留模拟器现场60秒且继续占用实例，随后关闭并释放。二维码和客户端会话仍按原 TTL 过期，不续期。

## Redis 消息和状态

TypeScript 后端通过 `LPUSH` 投递，Python 先用 `LINDEX -1` 查看最早的任务并校验时间；通过后使用 Lua 原子核对队首内容、登记 worker 归属并 `RPOP`，多消费者只能有一个领取成功。队列为空或任务时间暂不符合条件时，每秒重试一次。默认队列为 `naruto:login:tasks`，消息格式如下；`createdAt` 和 `expiresAt` 均为带时区的 ISO 8601 字符串：

```json
{
  "version": 1,
  "id": "客户端生成的 UUID",
  "createdAt": "2026-09-23T08:00:00.000Z",
  "expiresAt": "2026-09-23T08:02:00.000Z",
  "objectKey": "login/客户端生成的 UUID/qr.png",
  "userOptions": {
    "equipmentStamina": true
  }
}
```

示例时间戳仅说明结构，实际任务必须使用当前创建时间和有效的过期时间。队列消息不携带明文访问凭证。

`userOptions` 统一承载用户附加参数，并作为完整 JSON 对象从队列经父进程传给执行子进程。`equipmentStamina` 控制装备栏体力刷取：`true` 执行原有装备材料扫荡、合成、装备和进阶流程；`false` 跳过整个 `equipment_upgrade` 任务组，包括装备菜单导航，其他日常及其顺序保持不变。旧消息省略 `userOptions` 或传 `{}` 时默认开启，显式 `false` 不会被默认值覆盖。独立刷秘境线路原本不包含装备任务，不受此选项影响。`null`、数组、未知选项和非布尔值均按无效任务拒绝，不启动模拟器。客户端和 worker 更新后需重启常驻 worker 才会消费此参数；此前已运行的任务继续使用其启动时的选项。

对应会话位于 `${REDIS_QUEUE_KEY}:sessions:${id}`，例如 `naruto:login:tasks:sessions:<UUID>`。客户端先创建会话，Python 回写处理阶段时保留客户端的 `tokenHash` 和原有 TTL，不延长会话有效期。

状态依次为 `queued`、`starting_emulator`、`opening_game`、`preparing_qr`、`qr_ready`；失败为 `failed`，过期为 `expired`。`qr_ready` 表示二维码已生成，不代表账号已登录。客户端会话中的 `simulation: true` 是现有跨服务协议的兼容字段，真实驱动也必须保持该值。

时间戳可解析但未通过校验的任务保留原队列位置和 `queued` 状态，不启动模拟器、不写失败记录、不延长有效期；例如本机时间偏慢、创建时间超过本机当前时间 5 秒时，这台消费者等待重试，时间正常的其他消费者仍能领取同一任务。实际领取时还会校验 Redis 时间，避免按错误的本机时钟接收未来任务。若持续等待，应检查并同步执行服务器系统时间；本地警告每 30 秒最多输出一次。只有 Redis 时间确认已过期，或原会话已不存在时，才清理这个失效队列项，避免堵住后续任务。

无法解析的坏消息按原规则清理；版本、对象路径等数据校验失败且能与现有排队会话核对时，仍写入 `failed`、安全错误码、中文原因和 ERROR 日志。无法确认归属的坏消息仅记录本地警告；不会根据不可信消息覆盖已被其他 worker 领取的任务，也不会重建已过期会话。失败记录沿用执行日志的独立保留时间，二维码会话到期后仍可查看。

日常状态不写入原客户端的 `status` 白名单，独立保存在 `${LDPLAYER_STATE_DIR}/runs/<任务UUID>.json`，并将执行快照和日志同步到 Redis。二维码生成后依次为 `waiting_scan`、`finishing_login`、`running_daily`、`completed`，失败为 `failed`，服务停止为 `interrupted`。`finishing_login` 表示扫码授权完成、正在进入游戏；`authorizedAt` 保存授权完成时间，`startedAt` 保存确认主界面后开始日常的时间，后续终态保留这两个时间。客户端收到对应任务的授权完成状态后收起二维码并停止倒计时。进入游戏失败与等待扫码失败使用不同错误码。

### 用户执行记录

客户端后端在任务入队时先记录「登录申请已提交」，工作进程随后记录启动模拟器、打开游戏、准备二维码、等待扫码、登录成功并开始日常以及最终结果。子进程通过 `log` JSON 事件转发日志；用户面板保留带动作进度的 INFO 节点及 WARNING、ERROR，省略反复截图、模板匹配和点击坐标等无进度 INFO 细节。父进程校验事件后交给独立的后台发布协程，写入两个 key：

本机终端同时输出各子进程完整的日常 INFO 日志、普通打印和失败堆栈，每行标注任务 ID，以便区分并发任务。模型识别会记录截图开始、请求开始、调用时限、返回耗时、失败原因及重试次数。诊断使用单独的 stderr 管道持续读取，不占用 stdout 的任务事件协议；完整堆栈只显示在终端，网页仍保留关键执行记录。终端会遮盖凭据，正常诊断行不按网页的 1000 字符上限截断。更新后需重启常驻 worker 才能启用日志读取。

| Key | 内容 |
| --- | --- |
| `${REDIS_QUEUE_KEY}:runs:<UUID>` | String JSON；执行阶段、更新时间、开始/结束时间、当前动作及安全错误码 |
| `${REDIS_QUEUE_KEY}:logs:<UUID>` | Stream；每条日志的 `json` 字段包含 `time`、`level`、`message`，可选 `actionIndex`、`actionTotal` |

网站通过账号 Cookie 和 PostgreSQL 中的任务归属读取执行记录，每 1.5 秒增量查询，刷新时恢复最近的任务；二维码过期后仍能查看日常。日志读取不会消费或删除条目，不同页面可分别使用自己的 Stream ID 游标。成功状态在子进程退出和模拟器清理之后发布；执行失败会立即上报，不等待模拟器现场保留结束。网站读完终态的剩余日志后停止轮询。

日志先过滤已知模型密钥、认证头及凭证字段，单条最多 1000 字，不包含异常堆栈。Redis 短暂故障会后台重试，不阻塞游戏动作；每个任务的内存缓冲最多保留最新 256 条，发生丢弃会在恢复后的日志中提示。进程被强杀时尚未上传的内存日志可能丢失，本功能不提供任务接管或执行重放。

Redis 用 Lua 同步追加日志、更新快照和设置 TTL；worker 标识与递增序号用于拒绝旧 worker 写入及避免响应丢失后的重复日志。二维码会话过期不会删除已有运行记录。默认最多保留最近 2000 条日志，最终写入后保留 24 小时；未收到终态的运行记录也会按独立 TTL 过期。客户端无需新增连接配置，继续使用相同 `REDIS_URL` 和 `REDIS_QUEUE_KEY`。

按 `Ctrl+C` 优雅停止时，worker 回收子进程并关闭对应实例。出码前未过期的任务仍尝试重新入队；已发布二维码或已开始日常的任务记录为中断，不自动重排，以免重复执行游戏操作。队列在校验后原子移除并登记归属，仍没有消息确认与崩溃恢复机制：若领取成功但响应丢失、进程被强制终止或机器崩溃，需先检查 Redis 归属、本地运行记录和实例占用，再决定是否释放或重新申请。

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
| `__main__.py` | 校验 `REDIS_URL` 和有效模型渠道并启动常驻 worker |
| `app.py` | 旧 HTTP 演示的 FastAPI 接口与生命周期 |
| `config.py` / `models.py` | 配置、有效期与任务校验 |
| `queue.py` / `runtime.py` | Redis 队列与内存演示队列、并发消费、状态回写和清理 |
| `mock_emulator.py` | 每个任务单独启动的 Python 工作进程，生成模拟二维码 |
| `ldplayer.py` / `ldplayer_worker.py` | 固定实例分配、持久绑定、启动和隔离工作进程 |
| `daily.py` | 在任务绑定的 ADB 设备上复用原登录收尾和 `ActionRunner` 日常流程 |
| `log_events.py` / `run_logs.py` | 子进程日志脱敏、结构化输出，以及父进程有界缓冲和 Redis 执行记录发布 |
| `qr_navigation.py` / 根目录 `assets/` | 页面识别、ADB 点击和真实二维码裁剪；8 张扫码专用模板平铺为 `assets/login_*.png`，QQ 安卓登录、切换账号、开始游戏继续复用原有三张图片；不调用多模态 API |
| `pool_admin.py` | 查看实例占用，按任务 ID 显式释放实例 |
| `storage.py` | 本地与阿里云 OSS 存储适配 |

子进程从生成二维码一直运行到等待扫码、确认登录和日常执行结束，期间始终占用同一实例。执行失败或运行超时后，父进程停止脚本并立即上报失败，保留雷电模拟器60秒供排查，再按任务 ID 执行 `quit` 并释放池槽，不删除实例。保留期间不继续执行游戏动作，也不把该实例分配给新任务；其他空闲实例仍可处理任务。成功和主动停止服务时立即清理；保留期间按 `Ctrl+C` 也会提前结束等待并清理。若关闭失败，保留占位，并在运行记录中标记 `instance_cleanup_failed`，供管理员检查后通过 CLI 释放。

释放池槽只会关闭雷电进程并解除任务对实例索引的占用，不会删除实例，也不会清除 QQ 或游戏的账号数据。固定池不提供账号数据擦除或账号隔离。为避免继承上一个任务仍在运行的登录页面，每次新任务启动实例后会先强制停止游戏和 QQ 进程，再重新打开游戏；这同样不会清除应用数据。

配置 `LDPLAYER_POOL_INDICES=0,1,2`、`WORKER_CONCURRENCY=3` 时，最多有三个任务同时占用实例，等待扫码和执行日常都计入占用。第四个任务保持在 Redis 的 `queued` 状态，直到已有任务结束并释放槽位；排队期间仍遵守客户端会话 TTL。并发数不能扩大实际实例池容量，冷启动管理操作仍通过锁依次执行。

真实驱动通过 `python -u -m naruto_daily.login_service.ldplayer_worker` 运行隔离子进程。父进程传递任务 ID、登录有效期、雷电配置、日常配置路径和独立超时，并通过子进程环境传入已解析的 `REDIS_URL`，供模型路由使用同一个全局计数器；该地址不进入 JSON 协议或日志。OSS 密钥和服务环境中的模型配置不传递，子进程从项目根目录 `.env` 读取渠道 key 和模型名。子进程 stdout 只用于结构化事件，执行异常只输出固定错误码。

## 对象存储

默认 `STORAGE_DRIVER=local`，可用于本机验证。本地目录为项目根目录下的 `.data/login-service/objects`，对象键为 `login/{id}/qr.png`，旁边的 `metadata.json` 保存过期时间。写入使用临时文件与原子替换；过期对象及上次运行遗留的过期对象会定期清理。Redis worker 不提供本地文件的 HTTP 访问，因此网站/Vercel 链路应使用真实 OSS。

真实 OSS 使用 Python `oss2` SDK，在项目根目录 `.env` 中保留或填写以下配置，并与客户端使用同一 Bucket 和地域：

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
.\.venv\Scripts\python.exe -m pip install -e ".[login-service,login-service-test]"
.\.venv\Scripts\python.exe -m pytest tests/test_login_service_runtime.py tests/test_login_service_http.py tests/test_login_service_storage.py tests/test_login_service_redis.py tests/test_login_service_worker.py tests/test_packaging.py
```

Redis worker 的验证应包括 FIFO 消费、消息校验、会话状态与 TTL 保留、重连及关闭处理；HTTP 模式继续验证接口隔离、并发、PNG 输出、过期清理与 OSS 适配。实际网站联调需要客户端与 Python 使用同一 Redis 和 OSS 配置。
