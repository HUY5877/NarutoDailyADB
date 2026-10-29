# Python 扫码登录 worker

本分支用于 JEV 实验。worker 与测试网站都设置 `APP_ENV=test`，读取 Cloudflare 的 `hyrz-test` namespace；正式环境设置 `APP_ENV=prod`，读取现有 `hyrz` namespace。两个 namespace 内部使用相同的普通变量名。Redis 队列、OSS 前缀和本地状态目录直接采用选定配置，不按分支名追加后缀。配置字段和测试资源示例见[客户端对接协议](../../docs/test-jev-client-protocol.md)。

本模块位于现有 `NarutoDailyADB` Python 项目中，作为常驻 Redis worker 接收扫码任务，并在扫码登录后复用原完整日常流程。`REDIS_URL` 为必填项；未配置或为空时会明确报错并以退出码 1 结束，**不开放 HTTP 端口**。

网站或 Vercel 上的 `NarutoLoginClient` TypeScript 后端生成 UUID 和独立访问凭证，将登录任务写入 Redis。Python 从同一个 Redis 队列消费任务，为每项任务启动独立 Python 子进程。`mock` 驱动生成模拟二维码；`ldplayer` 驱动从固定实例池分配一个雷电实例、打开游戏并导航到二维码登录，把真实二维码 PNG 写入 OSS，再将阶段状态回写 Redis。客户端后端查询 Redis 状态并读取 OSS，浏览器通过客户端后端显示图片；网站无需通过 HTTP 连接本机 Python。留空实例池并配置模板时，仍兼容原有的复制实例模式。

`mock` 驱动的二维码仅用于联调，不能登录真实游戏，也不运行日常。`ldplayer` 子进程生成二维码后继续等待扫码；父进程收到二维码即上传并回写 `qr_ready`。子进程只通过“开始游戏”按钮模板确认扫码授权，随后等待并复查该按钮后点击，再清理弹窗和回到主界面；扫码流程不识别或点击旧的第二次授权同意按钮。确认登录成功后在同一子进程、同一 ADB serial 上继续执行原 `run all` 的后续日常动作。

队列的顶层 `loginType` 支持 `android_qq`、`ios_qq`、`android_wechat`、`ios_wechat`，分别对应安卓 QQ、iOS QQ、安卓微信、iOS 微信。缺省按历史约定使用安卓 QQ；非法值在操作模拟器前拒绝。父进程把渠道原样传给子进程，生成和刷新二维码始终点击本任务绑定的渠道，刷新不改变占用期限。QQ 页刷新使用返回键；微信页先点击“取消”结束 SDK 请求，再重新选择相同渠道，直接使用返回键会让后续入口失效。

每次任务打开游戏后，先检查恢复的登录页面：旧 HUY 上号器确认页和普通 QQ 授权页按已识别页面取消返回，旧微信授权页点击自身“取消”并等待 SDK 回调，游戏“开始游戏”页使用已有返回按钮回到渠道选择。只有实际点击本任务指定的安卓/iOS QQ/微信按钮后，才接收新二维码或注入本轮授权。已显示的旧二维码不能证明属于本次渠道。未知页面不猜坐标；取消后无法返回时有界失败。此过程不清空游戏数据，也不需要 root。

网页刷新延续已选渠道；微信仍使用原竖屏识别、页面取消和请求重试。QQ 辅助应用缺失且未启用自动补装，或补装失败时，会继续尝试原扫码分支；如果只有上号器且游戏仍被它接管，则明确提示辅助应用安装不完整，不能从上号器确认页获取原 QQ 二维码，也不会自动禁用或卸载应用。

模拟器无需安装 QQ：未安装时，点击所选 QQ 渠道后识别游戏内的“QQ 授权登录 / 使用QQ手机版扫码授权登录”页面；已安装时，继续通过 QQ 的切换账号、其他登录方式和扫码登录进入授权页。微信渠道识别游戏自带的“使用微信扫一扫登录游戏”竖屏页，点击入口后等待 30 秒；若请求失败返回已识别的渠道选择页，最多尝试三次同一渠道，不点击加载中或无法确认的页面。支持横屏 1920×1080 和竖屏 1080×1920 截图。页面均先确认授权场景，再按实际二维码边界裁剪并添加白边。模板只包含公共按钮和文字，不包含用户二维码。

安装 QQ 的流程点击“扫码登录”后只等待二维码加载，最多等待 30 秒，不重复点击该按钮或“其他登录方式”。进入 QQ 消息首页或发现二维码失效会停止任务；上传内容仅为有效授权二维码的裁剪图。未安装 QQ 的路径在扫码后继续复用相同的登录收尾与日常流程。

仅生成或展示二维码不能证明授权成功。服务通过“开始游戏”按钮确认扫码已完成，然后执行 `tasks.login` 中的开始游戏、等待加载、清理弹窗和返回主界面动作，确认主界面后才开始日常。直接通过命令行运行 `login` 或 `all` 时，也需要先完成扫码授权并停留在“开始游戏”页面。

## 配置和启动

需要 Python 3.11+ 和雷电模拟器 9，服务端无需 Node.js。在新电脑 clone 本仓库后，进入项目根目录创建虚拟环境、安装依赖，并只复制根目录的配置示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[login-service]"
Copy-Item .env.example .env
```

已有 `.env` 时直接编辑，不要重复复制覆盖。根目录 `.env` 保存环境选择、Cloudflare 连接凭据和雷电机器参数：

```dotenv
APP_ENV=test
CLOUDFLARE_API_TOKEN=your-read-only-token
CLOUDFLARE_ACCOUNT_ID=your-account-id
CLOUDFLARE_KV_NAMESPACE_ID_PROD=your-hyrz-namespace-id
CLOUDFLARE_KV_NAMESPACE_ID_TEST=your-hyrz-test-namespace-id
```

Token 需要目标账户的 Workers KV Storage Read 权限。`APP_ENV` 仅接受 `prod` 或 `test`，省略时为 `prod`。Token 和 account 共用；prod 选择 `CLOUDFLARE_KV_NAMESPACE_ID_PROD`，兼容旧 `CLOUDFLARE_KV_NAMESPACE_ID`；test 必须提供 `CLOUDFLARE_KV_NAMESPACE_ID_TEST`，不会借用正式 ID。启动通过官方 `cloudflare` SDK 只读取选定 namespace，分页并分批获取普通变量名的纯文本值，完整读取后原子注入环境变量。读取失败或必填资源缺失时停止启动，不回退另一个 namespace 或本地旧业务值。以下是 `hyrz-test` 内部的 KV 示例，每行对应一项普通 key；完整字段见[配置表](../../docs/test-jev-client-protocol.md#cloudflare-配置)：

```dotenv
REDIS_URL=redis://127.0.0.1:6379/0
REDIS_QUEUE_KEY=naruto:login-test-jev:tasks
MULTIMODAL_ROUTING_COUNTER_KEY=naruto:multimodal-test-jev:route:sequence
LDPLAYER_STATE_DIR=.data/login-service/ldplayer/test-jev
OSS_KEY_PREFIX=login-test-jev
OSS_REGION=oss-cn-shanghai
OSS_BUCKET=huy1-hyrz-test
OSS_ACCESS_KEY_ID=your-access-key-id
OSS_ACCESS_KEY_SECRET=your-access-key-secret
```

上方地址仅表示本机 Redis。连接实际 Redis 时使用自己的地址和凭据，例如 `redis://:your-password@redis.example.com:6379/0`；需要 TLS 时使用 `rediss://`。Redis 数据库由 URL 的路径决定，未指定时默认 DB 0。Python 和客户端必须使用相同数据库及队列名。

在项目根目录使用独立环境启动，也可双击 `启动扫码服务.cmd`：

```powershell
.\.venv\Scripts\python.exe -m naruto_daily.login_service
```

已有 `.venv-login-service` 时也可继续用该环境的 Python，双击脚本会优先选择它。激活其他已安装依赖的环境后可使用 `python -m naruto_daily.login_service`，安装后的 `naruto-login-service` 命令也可用。worker 会持续等待并消费任务；暂时断线会自动重连，停止时按 `Ctrl+C`。

服务入口和实例池管理命令 `python -m naruto_daily.login_service.pool_admin` 默认使用根目录 `.env` 中的 Cloudflare 连接凭据，不依赖启动工作目录；`--env-file <路径>` 可指定另一份本地配置。连接凭据及机器参数允许进程环境覆盖，KV 中的业务配置覆盖同名进程值。日常任务通过 `DAILY_CONFIG_PATH` 加载 JSON。

Redis、OSS、数据库和隔离路径统一从选定 KV namespace 读取，不回退到本地文件。JEV、DeepSeek 的 key/model 放在该 namespace；鱼玩 `YUWAN_API_KEY` / `YUWAN_MODEL` 按原约定成对保存在本地 `.env`，由父进程启动时解析并传给任务子进程。启动时按 key 是否非空冻结有效渠道，不请求上游校验密钥；启用的渠道必须填写独立模型名。每次生成模型 HTTP 尝试通过同一 Redis 的配置计数器执行 `INCR`，详见[多模态路由说明](../multimodal/README.md)。修改配置后，请重启正在运行的任务和常驻 worker。凭据不放进浏览器或提交到仓库。

QQ 通知使用选定 namespace 的普通 `QQ_BOT_APP_ID`、`QQ_BOT_APP_SECRET`、`QQ_BOT_GROUP_OPENID` 和 `DATABASE_URL` 字段，不再使用本地 `TEST_JEV_*` 变量。三项机器人配置全部留空时不主动发群消息；启用时须同时填写，并提供当前环境数据库配置。通知通过只读查询选定数据库的 `users` 获取用户名，并从 `qq_bindings` 查询当前机器人、当前群内的绑定。机器人配置不传给任务子进程；数据库配置仅供 QQ 登录子进程的抓号交接使用。

安卓 QQ 和 iOS QQ 的日常、周胜任务会在已启动的本次实例中检查 `com.huy.authcapture`（抓号器）和 `com.tencent.tim`（上号器）。启用自动补装时，缺哪款就下载并安装哪款，两款都缺时先抓号器、后上号器。两款应用都在时，先由抓号器生成二维码，网站用户扫码后上传本次授权，再由同一实例打开游戏中对应 QQ 通道，把完整授权 URL 交给游戏拉起的上号器确认。两款应用的自动流程均无需登录网站账号，云端账号名称固定为“自己账号”。只有游戏实际出现原有授权完成标记后才进入登录收尾、日常或决斗场周胜。未启用补装、下载、安装或包查询失败时，确认任务和实例仍有效后继续尝试原 QQ 扫码流程。实例所有权、实际设备串号和截止时间校验失败仍然终止。微信和仅取二维码模式保持原流程。

自动补装由选定 Cloudflare KV namespace 的三项配置共同启用：`QQ_APK_RELEASE_PREFIX` 为 `releases/huy-android/<固定版本>`，目录内文件名必须为 `HUYCapture.apk`、`HUYLogin.apk`；`QQ_CAPTURE_APK_SHA256` 和 `QQ_LOGIN_APK_SHA256` 为对应文件的 64 位 SHA256。对象存放在该环境的私有 OSS Bucket，worker 的 OSS 身份需要对这两个对象有读取权限。三项都不配置时保留原缺包处理；配置不完整时启动报错。已有应用直接保留，不覆盖、不升级，也不卸载，旧版仍需自行升级到支持自动流程的版本。

父进程使用 OSS SDK 为两个固定对象的 GET 请求生成带 Date 的请求头签名，通过内部 `QQ_APK_DOWNLOADS` 环境变量把 URL、签名头和摘要传给 QQ 任务子进程；不传长期签名密钥，也不把下载授权写入公开任务状态或日志。默认 OSS 公网域名限制 APK 查询参数签名链接下载，因此使用官方支持的 Authorization 请求头方式，保持对象私有。缺失应用先下载到电脑的 `${LDPLAYER_STATE_DIR}/apk-cache/<sha256>.apk`，校验摘要后用 ADB 安装到本次实例；后续任务复用校验通过的缓存。下载仅允许 HTTPS、不跟随重定向，每包最多 8 MiB、30 秒，安装每包最多 60 秒，均受原任务剩余时间约束。下载和安装等待不占用全局实例管理锁；安装前后与失败回退前均复核实例归属和实际 serial。

启用抓号路径前需同时升级两款应用到支持 `protocol:1` provider 的版本，部署网站抓号接口和表结构，并在选定 Cloudflare KV namespace 配置 `QQ_CAPTURE_API_BASE=https://你的登录网站域名` 及同环境 `DATABASE_URL`。地址只能是 HTTPS 网站根地址，端口须为默认或 443，不能含路径、查询参数或凭据。数据库账户需可读写 `naruto_login.qq_capture_requests`；worker 不查询用户的最新历史授权，而是按可信 Redis 会话的用户、任务、执行轮次和二维码轮次精确交接。双应用齐全但缺少身份、数据库、网站地址或新版 provider 时明确报配置错误。业务配置不从本地 `.env` 回退。进入抓号路径的子进程即使被系统强制终止，父进程仍按可信用户、任务和执行轮次尽力撤销；数据库临时不可用时仅记录不含凭据的警告，并保留原有效期限制。

网站的刷新按钮沿用现有 requestId/上传成功后 ACK 机制，每次刷新先使旧抓号轮次失效再生成新码，不延长任务原截止时间。自动抓号模式的本机刷新按钮会提示到网页刷新。二维码通过仅允许本机 ADB shell/root/应用自身访问的 provider 导出本轮 PNG，保留应用的 `FLAG_SECURE`。授权 URL、任务能力凭证和数据库连接串不会放入公开任务状态或日志；成功消费、失败和取消均只清理本轮数据，旧执行轮次不能清理新任务。

worker 在任务开始时从自己的有效 Redis 会话读取 `userId`，通过只读、参数绑定的数据库查询取得一次 `username`，保存在 `runs.username` 并供通知复用。二维码上传 OSS 并成功发布 `qr_ready` 后，异步发送群消息；未绑定时使用“用户名，你的二维码准备好啦！快打开登录页面扫码吧～”，已绑定时使用定向 @ 并省略用户名。通知不展示任务编号；初次生成及每个成功刷新各通知一次，过期二维码不再提醒。任务全部完成、超时或报错退出后再发送一次最终结果，失败原因使用安全提示；登录会话过期不影响结束通知。Ctrl+C、服务停止或任务取消不发送结束通知，模拟二维码生成也不会误报日常完成。数据库查询或 QQ 接口失败只记后台日志，不改变任务结果；查不到用户名时跳过提醒。连接、SQL 和 QQ 请求均有超时，通知不自动重试。机器人需要已加入目标群，且群主已开启主动消息权限；`group_openid` 是该机器人收到群事件时的群标识，并非 QQ 群号。无需保持 WebSocket 连接，也不需要客户端接入 QQ。

QQ群结束通知按任务类型区分：日常成功仍提示“日常任务全部搞定啦！辛苦啦～”；决斗场周胜正常结束提示“决斗场周胜任务结束啦！辛苦啦～”，不将运行时长结束表述为周胜目标全部达成。若日常执行超时前已确认至少一次秘境探险成功结算，通知会说明日常全流程已跑通、本次时间已用完而扫荡券尚未用完。尚未完成秘境的超时及其他失败仍使用原有提示；运行记录保留真实的 `failed` 状态、`daily_timeout` 错误码和首次秘境完成时间。

### 绑定 QQ 群提醒

用户在配套网站登录后打开「QQ 群提醒」，生成 5 分钟有效的一次性绑定码，在指定群通过 QQ 成员列表选择机器人 @，再发送 `绑定 CODE`，随后回到网页确认本人发送。网站通过 QQ 官方 Webhook 验证事件并保存 `naruto_login.qq_bindings`；worker 不接收群回调，也不需要额外开放本机端口。

网站与 worker 必须选择同一 `APP_ENV`，连接该环境的数据库，并使用相同的 `QQ_BOT_APP_ID`、`QQ_BOT_APP_SECRET`、`QQ_BOT_GROUP_OPENID`。测试回调为 `https://test.huy1.site/api/qq/events`，正式回调为 `https://huy1.site/api/qq/events`，均订阅 `GROUP_AT_MESSAGE_CREATE`。`group_openid` 是该机器人收到目标群事件时的标识，不是 QQ 群号；正式群与测试群不同时，用户需在正式站重新绑定，不复制测试绑定记录。

正式发布按以下顺序进行：

1. 核对正式配置；在正式数据库提前创建并核对 `naruto_login.qq_bindings`、`naruto_login.qq_binding_challenges`，使用网站 `qq-binding-store.ts` 的表定义和约束。网站需两表的增删改查权限，worker 需 `qq_bindings` 及 `users` 的查询权限；不要只验证数据库能连接。
2. 合并并部署网站，等待正式部署成功，再把腾讯后台回调切到正式地址并确认校验成功。地址校验只验证签名协议，不验证绑定表和群内主动提醒。
3. 等旧 worker 的在途任务结束，正常停止服务，更新代码并重启；不要在任务运行期间直接替换代码。此次 QQ 绑定功能没有新增 Python 依赖或 APK 更新要求。
4. 在正式站完成“生成绑定码 → QQ 中选择机器人 @ 并发送绑定命令 → 网页确认 → 新建扫码任务，核对出码、刷新及任务结束的真实定向 @ 提醒 → 解绑 → 再触发提醒”的验收，确认解绑后恢复原有用户名提醒。确认绑定本身不会发送提醒，需由任务触发。

正式与测试共用同一机器人时，腾讯后台配置的回调决定群事件的去向。切到正式地址后，测试站收不到后续群事件，无法完成新的绑定；测试站已有绑定的 worker 主动通知仍可通过 QQ API 发送。不要为恢复测试绑定而直接覆盖正式回调；两套环境需同时绑定时，应另用机器人或实现统一入口按群分流。

绑定后，worker 将当前网站用户与 `bot_app_id`、`group_openid` 一起查询，使用真实 `member_openid` 构造 Markdown @。定向 @ 消息只显示 @ 和提醒正文，不再重复网站用户名，也不保留用户名后的逗号；二维码首次生成、刷新和任务结束通知均遵循此规则。每次提醒前重新查询，用户解绑后不再使用原成员身份；独立登录会话过期后，结束通知使用任务中已核实的网站用户 ID 查询。未绑定或绑定查询失败时继续发送用户名纯文本提醒，故障仅记不含凭据的后台日志。网站解绑只移除定向 @，不取消任务和原有群内文字通知。

worker 不负责创建绑定表；未提前建表时，网站在首次绑定业务请求中初始化，回调地址校验和部署本身不会建表。群主仍需允许机器人主动发言；绑定成功不代表腾讯一定接受后续通知。测试群联调后，正式群仍需完成真实 @ 验收。

查看固定实例池占用也使用同一环境和配置：

```powershell
.\.venv\Scripts\python.exe -m naruto_daily.login_service.pool_admin list
```

| 变量 | 默认值与用途 |
| --- | --- |
| `REDIS_URL` | 必填；未设置、为空或仅包含空白时，启动命令报错退出 |
| `REDIS_QUEUE_KEY` | 选定 KV namespace 必填；必须与客户端相同，代码不改写其值 |
| `WORKER_CONCURRENCY` | `2`，同时运行的任务子进程数 |
| `OSS_KEY_PREFIX` | 选定 KV namespace 必填；必须与客户端相同，代码不追加后缀 |
| `MOCK_STEP_DELAY_MS` / `EMULATOR_DRIVER` | `600` / `mock`，模拟步骤延迟与驱动 |
| `LDPLAYER_CONSOLE_PATH` | `ldplayer` 必填，雷电控制台程序的完整路径 |
| `LDPLAYER_POOL_INDICES` | 空；推荐设为 `0,2`，使用逗号分隔的现有雷电实例索引，不复制实例 |
| `LDPLAYER_TEMPLATE` | 仅复制模式必填；配置实例池时无需模板 |
| `LDPLAYER_STATE_DIR` | 选定 KV namespace 必填；任务、日志和实例状态目录，相对项目根目录解析，绝对路径原样使用 |
| `LDPLAYER_PACKAGE` | `com.tencent.KiHan`，游戏 Android 包名 |
| `QQ_CAPTURE_API_BASE` | 选定 KV namespace 的 HTTPS 登录网站根地址；双辅助应用 QQ 抓号路径必填 |
| `QQ_APK_RELEASE_PREFIX` | 可选；私有 OSS 的 `releases/huy-android/<固定版本>`，须与两项摘要共同配置以启用缺包补装 |
| `QQ_CAPTURE_APK_SHA256` / `QQ_LOGIN_APK_SHA256` | 可选；发布目录中两款 APK 的 SHA256，防止安装不完整或内容不符的文件 |
| `DATABASE_URL` | 选定 KV namespace 的 PostgreSQL 地址；QQ 通知读取用户名，QQ 抓号路径读写本次任务轮次 |
| `LDPLAYER_COMMAND_TIMEOUT_SECONDS` | `20`，单条雷电控制台命令的超时秒数 |
| `DAILY_CONFIG_PATH` | 项目根目录的 `config.example.json`；复用原日常任务配置，相对路径以项目根目录解析 |
| `DAILY_TIMEOUT_SECONDS` | 默认 `3000` 秒；扫码授权成功后起算，登录收尾和全部日常共用。可设置更短时限；旧配置中超过3000秒的值按3000秒执行，最多50分钟 |
| `RUN_LOG_RETENTION_SECONDS` | `604800`，日志默认保留 7 天，范围 60–604800；执行摘要至少保留 7 天，运行中的 TTL 额外加上日常超时预算 |
| `RUN_LOG_MAX_ENTRIES` | `2000`，每个任务在 Redis 保留的最新日志条数，范围 100–10000 |

日常 JSON 及引用的图片必须一同保留，图片路径相对 JSON 所在目录解析。若通过 wheel 安装服务，请将当前版本的配置和完整 `assets/` 目录放在本机，并将 `DAILY_CONFIG_PATH` 指向该 JSON；无需修改 JSON 中原有的 `adb_path` 或 `serial`，执行时强制使用本次任务分配到的实例。

升级时，如果 `DAILY_CONFIG_PATH` 指向自行保存的旧版 JSON，请将其中 `tasks.login` 更新为当前 `config.example.json` 的登录收尾配置。保存账号选择、第二次授权和未使用的顶层动作类型已移除，不再读取账号昵称；其余自定义任务设置保留，已删除类型会明确报错。

固定实例池只支持 Redis worker 模式；配置 `LDPLAYER_POOL_INDICES` 时必须同时配置 `REDIS_URL`。旧 HTTP 本地演示只保留 `mock` 驱动，启动时会拒绝固定池配置，避免池满后错误地把任务取出并标记失败。

固定池的 `bindings.json` 使用版本 2，`bindings` 按实例编号保存，例如配置 `LDPLAYER_POOL_INDICES=0,2` 时只有 `"0"` 和 `"2"` 两个元素。每个元素保存本机实际实例名、ADB serial、当前 `taskId` 和 `state`；空闲为 `idle`，分配后为 `reserved`，二维码发布后为 `qr_ready`。关闭完成后恢复 `idle` 并清空 `taskId`，不累计已结束任务。任务历史和重新领取资格由 Redis 管理，旧任务的迟到清理不会操作已分配给新任务的实例。

配置固定池后，通过 `启动扫码服务.cmd` 启动时，服务入口先读取本机 `.env` 和进程环境，选择并完整加载 Cloudflare namespace，再使用解析后的状态目录、实例索引和雷电控制台路径重置实例登记。它查询 `ldconsole list2` 确认实际实例，再覆盖该目录中的 `bindings.json`：只保留本机配置的编号和实际名称，每个槽位统一为 `idle`，`taskId` 和 `serial` 均为 `null`。Cloudflare 加载失败时不会先重置本地登记。实例编号和名称不硬编码，serial 在任务启动实例时查询；新电脑无需复制其他电脑的绑定文件。

启动重置只在服务启动入口执行，任务子 worker、领取任务和 `pool_admin list` 不执行；查看或领取不会把现有占用清空。升级前应正常停止旧服务，再使用启动入口运行更新后的服务。

单独使用实例池管理器读取旧版状态时，仍在文件锁内自动迁移并保留正在占用的任务，合并已释放历史；移除仍被占用的实例、占用记录冲突或实例被改名时会报错并保留原文件。这是管理器读取状态的兼容行为，与服务启动入口重建空闲槽位不同。

仓库根目录的 `bindings.json` 全空闲模板可保留作手动初始化参考，正常启动无需人工替换。若要手动覆盖 `${LDPLAYER_STATE_DIR}/bindings.json`，须先停止 worker、确认实例没有正在执行的任务，并核对本机实际状态目录；模板的预置编号不限制本机 `LDPLAYER_POOL_INDICES` 配置。

客户端在其自己的环境中启动或部署。Python worker 所在机器需要能够连接 Redis 和 OSS，并在网站处理登录申请期间保持运行。每个环境内的网站/Vercel 和 Python 使用同一 Redis、数据库、队列、OSS Bucket、地域及对象前缀；测试 Bucket 为 `huy1-hyrz-test`，正式 Bucket 保持原配置。客户端不需要配置指向本机 Python 的 `LOGIN_SERVICE_URL`。

新客户端发送 `loginTimeoutSeconds: 600`。任务排队时仍受队列 `expiresAt` 约束；领取、分配实例时固定设置 `loginStartedAt` 和 `loginExpiresAt`，会话与账号锁同步保留 10 分钟。准备游戏和等待扫码共用这段占用时间，刷新二维码不续期。10 分钟内未授权即终止子进程并立即关闭实例，不再额外保留 60 秒。旧客户端省略此字段时仍沿用原队列截止时间。只有“开始游戏”按钮模板匹配成功，子进程才立即发送 `authorized`，不等待点击按钮及点击前后的延时；旧同意按钮和主界面识别不再作为扫码授权依据。子进程同时发送本次执行的绝对截止时间 `executionDeadline`，父子进程共用同一截止时间。后续开始游戏按钮操作、加载、清弹窗、额外主界面核验和全部日常任务共享最多50分钟，进入主界面或切换任务时不重新计时，也不再受二维码期限限制。除丰饶之间战斗的 180 秒保护外，各阶段不再单独设置时间预算；普通图片识别和 OpenAI 兼容导航的模型单次请求最多30秒，只有HTTP 200算成功，其他状态（包括400、401、403）、网络异常和超时均等待10秒、重新截图继续重试，直到成功、取消或总时限到达。到达总截止时间后父进程终止执行子进程，保留模拟器现场60秒且继续占用实例，随后关闭并释放。二维码和客户端会话仍按原 TTL 过期，不续期。

日常执行中的页面、按钮和数值未确认时，会在同一总截止时间内等待并重新截图，不因 JSON 中的旧识别次数、阶段轮数或局部等待上限提前结束。除下述一乐外卖循环外，领奖或合成已经点击后，只观察结果更新；未确认时不重复领取同一份奖励或重复合成，也不把缺失数值、未结束动画当作成功。明确没有可领取奖励、挑战次数或可用体力时正常结束相应任务；总时限到达、取消、ADB 故障或本地配置错误仍会停止。

一乐外卖兼容有、无“体力追回”一栏的两种布局。每轮先从左到右点击六个固定位置 `(450,520)`、`(550,520)`、`(880,520)`、`(1100,520)`、`(1310,520)`、`(1650,520)`，再重新截图让 JEV 在“仍可领取、已无可领取、奖励弹层、无法确认”中选择，不再识别具体位置组合。仍可领取就继续下一轮六次点击，直到确认已无可领取盒子。识别到奖励弹层时先关闭并重新检查，页面未确认时只重新识别。循环不延长任务总时限。

免费招募点击后若迟迟未出现结果页，会直接在新截图中用模型复查当前类别的免费按钮，补点不再要求招募菜单或红点模板匹配。两次模型识别确认同一免费抽数后，使用第二次的新坐标补点一次；未确认免费按钮时继续观察，不点击付费按钮。检测到任一招募结果页后即禁止补点招募，继续原有的忍者展示和普通结果确认流程。首次招募仍按原流程检查菜单与红点；补点不延长任务总时限。

生存挑战若因昨天未完全通关而无法开始扫荡，初次点击和最多两次补点后，会留足末次点击的 2 秒响应时间。仍确认停在生存地图时重新截图读取重置次数：大于 0 才点击重置、确认并再次开始扫荡，为 0 则结束。页面或次数未确认时继续观察；读取次数或准备重置期间若已进入名单、准备就绪或扫荡确认页，就接着处理当前扫荡，不再重置。重置及其确认各点击一次，确认页未消失时不会点击后方的开始扫荡按钮。

## Redis 消息和状态

TypeScript 后端通过 `LPUSH` 投递，Python 先用 `LINDEX -1` 查看最早的任务并校验时间；通过后使用 Lua 原子核对队首内容、登记 worker 归属并 `RPOP`，多消费者只能有一个领取成功。队列为空或任务时间暂不符合条件时，每秒重试一次。队列由选定 namespace 的 `REDIS_QUEUE_KEY` 指定，下面消息示例使用当前测试 OSS 前缀；`createdAt` 和 `expiresAt` 均为带时区的 ISO 8601 字符串：

```json
{
  "version": 1,
  "loginTimeoutSeconds": 600,
  "id": "客户端生成的 UUID",
  "createdAt": "2026-09-23T08:00:00.000Z",
  "expiresAt": "2026-09-23T08:02:00.000Z",
  "objectKey": "login-test-jev/客户端生成的 UUID/qr.png",
  "userOptions": {
    "equipmentStamina": true,
    "secretRealmMode": "all"
  }
}
```

示例时间戳仅说明结构，实际任务必须使用当前创建时间和有效的过期时间。队列消息不携带明文访问凭证。

`userOptions` 统一承载用户附加参数，并作为完整 JSON 对象从队列经父进程传给执行子进程，不拆成独立字段或 Redis key。可选的 `mainTask` 接受 `daily`、`arena_weekly`，缺省沿用旧日常协议且不补写该字段。`arena_weekly` 复用登录与清弹窗，执行 `tasks.arena_weekly`：通过 OpenCV 识别主界面设置按钮，进入高级设置，在两列选项之间的空白区域向上滑动 10 次滚到底部（`scroll_swipes` 可配置为 1 到 20 次）；定位“决斗场只匹配玩家”，仅在已勾选时点击取消，并重新截图确认未勾选。随后调用现有回主界面模块，确认回到主界面后先向右滑动主场景 5 次归位，再从左向右搜索，最多向左滑动 15 次；每轮先用 JEV 判断决斗场入口是否清楚可见，阳性后才调用生成模型定位，点击前重新截图复查，未知前景不授权点击或滑动。进入后通过 OpenCV 点击“忍术对战”图标并确认“好友切磋”可见，再持续调用原决斗场六区战斗模块；装备和秘境选项不参与周胜线路。首次调用战斗模块时通过子进程事件 `arena_started` 提交绝对 `executionDeadline`，开始独立的 40 分钟预算。父进程核对任务类型、登录阶段、单次启动和时限上限后替换执行截止时间；到时结束执行子进程，正常记录 `completed`、关闭并释放实例，不走失败保留现场。

日常线路中的 `equipmentStamina` 控制装备栏体力刷取：`true` 执行原有装备材料扫荡、合成、装备和进阶流程；`false` 跳过整个 `equipment_upgrade` 任务组，包括装备菜单导航。缺省时默认开启。独立刷秘境线路原本不包含装备任务，不受此选项影响。

`secretRealmMode` 只接受 `once`、`all`、`skip`，默认 `all`。`once` 在首次明确成功结算并完成返回房间收尾后结束整个日常；进入房间时或出战前复查发现券为 0，也正常结束。失败结算和主动退出不会触发首次成功，仍按原逻辑恢复并继续挑战。`all` 保持用完剩余挑战券的既有循环；`skip` 跳过整个秘境任务组，包括入口、房间和返回导航，其余日常照常执行，独立刷秘境线路则仅保留登录。`skip` 不生成秘境首次成功标记；`once`、`all` 在可靠成功结算后仍上报 `secretRealmCompletedAt`，零券不伪造此标记。

旧 `secretRealmOnce: true` 兼容为 `once`，`false` 或省略兼容为 `all`；规范化后的对象只保留 `secretRealmMode`。新旧字段同时出现时须表达同一种模式，否则拒绝任务。`null`、数组、未知选项、非法模式和非布尔的旧字段或装备字段均按无效任务拒绝，不启动模拟器。三种模式均不改变最多 50 分钟的总执行时限。更新后由用户重启常驻 worker 才会消费新参数；此前已运行的任务继续使用启动时的选项。

对应会话位于 `${REDIS_QUEUE_KEY}:sessions:${id}`，例如 `naruto:login-test-jev:tasks:sessions:<UUID>`。客户端先创建会话，Python 回写处理阶段时保留客户端的 `tokenHash` 和原有 TTL，不延长会话有效期。

状态依次为 `queued`、`starting_emulator`、`opening_game`、`preparing_qr`、`qr_ready`；失败为 `failed`，过期为 `expired`。`qr_ready` 表示二维码已生成，不代表账号已登录。客户端会话中的 `simulation: true` 是现有跨服务协议的兼容字段，真实驱动也必须保持该值。

`preparing_qr` 包括等待游戏加载、进入所选登录渠道以及获取二维码。若启动广告挡住登录入口，会先用本地模板识别灰底白色叉号，下一轮重新截图确认后点击新位置，再继续原登录流程。模板只包含按钮内部，不包含广告游戏名称、数字或图案；支持按钮位置变化和 0.7～1.5 倍缩放。每次准备流程最多关闭 3 次，仍出现则明确报错。已有二维码或二维码标题、竖屏授权页面、已执行登录操作及二维码刷新期间均不执行广告关闭；该检测不调用多模态 API，也不延长原登录截止时间。

时间戳可解析但未通过校验的任务保留原队列位置和 `queued` 状态，不启动模拟器、不写失败记录、不延长有效期；例如本机时间偏慢、创建时间超过本机当前时间 5 秒时，这台消费者等待重试，时间正常的其他消费者仍能领取同一任务。实际领取时还会校验 Redis 时间，避免按错误的本机时钟接收未来任务。若持续等待，应检查并同步执行服务器系统时间；本地警告每 30 秒最多输出一次。只有 Redis 时间确认已过期，或原会话已不存在时，才清理这个失效队列项，避免堵住后续任务。

无法解析的坏消息按原规则清理；版本、对象路径等数据校验失败且能与现有排队会话核对时，仍写入 `failed`、安全错误码、中文原因和 ERROR 日志。无法确认归属的坏消息仅记录本地警告；不会根据不可信消息覆盖已被其他 worker 领取的任务，也不会重建已过期会话。失败记录沿用执行日志的独立保留时间，二维码会话到期后仍可查看。

日常状态不写入原客户端的 `status` 白名单，独立保存在 `${LDPLAYER_STATE_DIR}/runs/<任务UUID>.json`，并将执行快照同步到 Redis、日志异步上传到 SLS。二维码生成后依次为 `waiting_scan`、`finishing_login`、`running_daily`、`completed`，失败为 `failed`，服务停止为 `interrupted`。`finishing_login` 表示扫码授权完成、正在进入游戏；`authorizedAt` 保存授权完成时间，`startedAt` 保存确认主界面后开始日常的时间，后续终态保留这两个时间。客户端收到对应任务的授权完成状态后收起二维码并停止倒计时。进入游戏失败与等待扫码失败使用不同错误码。

管理员中断正在执行的任务时，客户端应原子地修改 `${REDIS_QUEUE_KEY}:runs:<UUID>` 中的 `status` 为 `interrupted`，保留原 JSON 的 `taskId`、`workerId`、`attemptId`、`sequence` 等字段及 TTL；不要把整个值替换成字符串，也不要把登录会话 `sessions` 的状态改为 `interrupted`。worker 在每次已有的摘要回写中，用同一个 Lua 脚本检查中断并决定是否更新，不新增 Redis 轮询。同一次执行已中断时，进度、完成和失败均不得覆盖它，且无需管理员额外增加序号。

发现中断后，回调只终止该任务的执行子进程，唤醒该任务的失败现场保留等待，随后关闭并释放对应模拟器、结束登录会话，不重新入队，也不停止其他任务。最后一次结果尚未确认回写时不提前发送成功通知；Redis 较慢而导致迟到的中断回调，也会补做已释放实例的会话清理。正常服务停止后的重排仍可沿用原任务 UUID；每次成功领取生成新的 `attemptId`，旧执行的迟到回写和回调均不能影响新执行。

战斗中的每轮既有结算检查会在原 INFO 日志事件上附加内部 `executionCheckpoint: true` 标记。父进程收到后只刷新当前执行的轻量快照，保留状态、动作进度、执行编号和日志游标，不新增 SLS 日志。周胜当前每段连点约 30 秒，因此即使一直停留在同一战斗动作、没有新的公开进度，也能在下一轮战斗检查触发回写并发现中断。旧版父进程会继续忽略普通 INFO 及这个可选字段，不会因新子进程发送未知事件而失败。每次连点和普通识别 INFO 日志不触发这次刷新；没有增加独立的 Redis 查询循环、定时器或截图。

中断检查与更新在 Redis 内原子执行，管理员命令不能插入两者之间。管理员先写中断，worker 就拒绝覆盖；worker 先写完，管理员随后写入的中断也会阻止后续回写。这仍是回写时发现中断的机制，不是即时推送；其他没有检查点或有效摘要回写的阶段不会立即发现中断。已经发出的游戏操作、日志和通知不会回滚。升级后需要重启常驻 worker，新启动的执行子进程才会使用检查点协议。

### 日常模块超时截图

扫码任务在父进程独立监控每次模块调用。单次运行达到 **600 秒**时，使用任务绑定的 ADB 设备即时截取整屏 PNG，并复用当前环境的 OSS 配置上传到：

```text
{OSS_KEY_PREFIX}/exceptions/{任务id}_{cnt}.png
```

`cnt` 从 1 开始，在任务内递增；同一次模块调用只截图一次，再次进入同名模块重新计时。已有对象禁止覆盖，任务重试遇到同名文件会跳到下一个可用编号。异常图片保持私有，不随二维码过期或任务结束删除；可在 OSS 控制台查看，需要清理时按异常目录配置生命周期。

检查覆盖 `ActionRunner` 调用的非秘境模块，包括招财、招募、丰饶、生存挑战、小队突袭、装备、一乐、组织祈福、页面导航及登录收尾。`return_to_main` 每次调用独立计时，包括其他模块内部调用的返回主界面。`enter_secret_realm` 和 `handle_secret_realm` 本身免检查。决斗场周胜按一次模块调用检查，600 秒时截图，仍沿用原 40 分钟执行期限。

600 秒是诊断阈值，不会强行跳过模块或结束任务。执行子进程没有新日志、ADB 或模型请求阻塞时，父进程仍会检查。截图有独立的 15 秒时限，最多接受 16 MiB；上传最多尝试 3 次，共用 45 秒等待预算。尚在进行的上传不会因单次等待较慢而启动重复请求；总等待超时后停止重试。截图或上传失败仅记录后台诊断，不改变任务结果。该功能不需要新增配置，升级后重启常驻 worker 生效；本地独立 CLI 没有扫码任务 ID 和父进程，因此不执行这项 OSS 诊断。

### 用户执行记录

worker 领取任务或确认可归属的拒绝任务后，先按申请时间生成「登录申请已提交」事件，再记录启动模拟器、打开游戏、准备二维码、等待扫码、登录成功并开始日常以及最终结果。排队期间由任务状态显示等待领取，尚未领取的任务没有 SLS 日志。子进程通过 `log` JSON 事件转发日志；用户面板保留带动作进度的 INFO 节点及 WARNING、ERROR，省略反复截图、模板匹配和点击坐标等无进度 INFO 细节。父进程校验、脱敏后同步放入有界内存队列，后台通过官方 Python SDK 批量发送 SLS；游戏协程不等待 SDK 网络请求。

本机终端同时输出各子进程的日常 INFO 日志（ADB 操作明细除外）、普通打印和失败堆栈，每行标注任务 ID，以便区分并发任务。模型识别会记录截图开始、请求开始、调用时限、返回耗时、失败原因及重试次数。诊断使用单独的 stderr 管道持续读取，不占用 stdout 的任务事件协议；完整堆栈只保留在本地。网页异常日志以「具体错误：」开头，包含异常类型、脱敏后的异常原因、底层原因及执行阶段，整条最多 1000 字符，不包含堆栈源码行。管理员可在失败详情中结合动作进度和近期日志定位原因。终端会遮盖凭据，正常诊断行不按网页的 1000 字符上限截断。更新后需重启常驻 worker；过去未上报的异常详情无法补回。

常驻 worker 加载配置后，同时把本地日志追加到 `${LDPLAYER_STATE_DIR}/logs/worker.log`，包括带任务 ID 的子进程诊断和完整错误堆栈；重启后继续追加。单文件达到 10 MiB 时轮转，最多保留 `worker.log.1`～`worker.log.5`，不提交到 Git。日志沿用凭据脱敏规则。`${LDPLAYER_STATE_DIR}/runs/<任务UUID>.json` 仍只保存任务状态摘要。

二维码导航和日常执行中的每次 ADB 点击、滑动、返回均记录操作开始及完成／失败，包括命令发出时的时间（含时区）、实例 serial、执行线程、点击坐标或滑动起终点及持续时间、命令耗时和失败类型。战斗中的并发连点也逐次记录；“完成”表示 ADB 命令成功返回，游戏页面是否生效仍由原识别流程判断。这些操作明细只写入本地 `worker.log` 日志文件，不输出到终端，也不上传到客户端 SLS 日志；其他日常、模型和异常日志仍按原规则输出，不改变点击顺序、等待、重试或执行总时限。

多模态 API 的 HTTP 失败、网络异常、单次请求超时和接口重试诊断仅保留在 Python 后台，不进入用户 SLS 日志。上层识别失败和最终异常摘要也沿用明确的接口异常分类，避免重复暴露。代码错误、截图或请求构造失败、配置错误、任务总执行时限到期仍对用户显示；即使由接口异常引起，也只省略接口原因，保留外层错误。任务最终失败状态及通用失败提示照常发布。该规则只控制新增日志的展示，不改变重试和截止时间，也不清理已有用户日志。

| Key | 内容 |
| --- | --- |
| `${REDIS_QUEUE_KEY}:runs:<UUID>` | String JSON；执行阶段、更新时间、开始/结束时间、当前动作、安全错误码及 `logSource` / `logCursor` |
| `${REDIS_QUEUE_KEY}:logs:<UUID>` | 仅保留迁移前的历史 Stream；新任务和新事件不创建、不追加，也不续期 |

网站通过账号 Cookie 和 PostgreSQL 中的任务归属读取执行记录，每 1.5 秒增量查询，刷新时恢复最近任务；二维码过期后仍能查看日常。SLS 日志按任务和环境过滤，使用 `ms-seq` 游标增量读取；旧 Redis Stream 仅用于历史数据回退。成功状态在子进程退出和模拟器清理之后发布；执行失败不等待模拟器现场保留结束。网站核验终态 `logCursor` 已进入 SLS 索引后才结束轮询，索引未就绪时显示等待日志。

Redis 只保存队列、会话、锁、刷新指令和执行摘要。每个任务只保留一个待写摘要，约每 250 毫秒合并更新一次；Lua 继续检查 worker 归属、递增序号和终态，阻止旧 worker 覆盖新归属。二维码会话过期不会删除已有运行记录，执行摘要至少保留 7 天。拒绝任务先原子校验会话归属并保存失败状态，再发送安全错误事件，随后用比较并更新的方式写入日志水位，不改变原 TTL。

### 阿里云 SLS 异步日志

安装或更新 `.[login-service]` 后，可在所选 Cloudflare KV namespace 启用 SLS。`APP_ENV=test` 和 `prod` 各自配置目标 Project/Logstore 和凭据；同名配置项不跨 namespace 回退，子进程不继承 SLS 密钥。本地 `.env` 不接受这些业务配置，修改 KV 后需重启常驻 worker。

| KV 配置 | 要求 |
| --- | --- |
| `SLS_ENABLED` | 缺省 `false`；启用填 `true` |
| `SLS_ENDPOINT` | 地域 HTTPS 地址，例如 `https://cn-shanghai.log.aliyuncs.com` |
| `SLS_PROJECT` / `SLS_LOGSTORE` | 当前环境独立的目标资源 |
| `SLS_ACCESS_KEY_ID` / `SLS_ACCESS_KEY_SECRET` | 具有当前 Logstore 写入权限的 RAM 凭据；可复用已有 OSS 用户，但需单独授予 SLS 权限并显式填写这两个配置项 |
| `SLS_STS_TOKEN` | 使用临时凭据时填写，同时更新对应 AccessKey；过期需更新 KV 并重启 |

SLS Logstore 保留期设置为 **7 天**。无论开关状态，worker 都不再向 Redis 追加日志正文；`SLS_ENABLED=false` 时摘要记录 `logSource=disabled`，新用户日志不上传，本地诊断日志照常保留。启用时摘要记录 `logSource=sls` 和最近接受事件的 `logCursor`。不会采集完整 `worker.log`、ADB 点击明细或私有模型诊断，也不扫描或搬运历史 Redis 日志。

默认全进程内存日志队列上限为 4096 条、16 MiB（含正在上传的批次），每批最多 200 条，约每秒发送一批并压缩；失败按固定间隔重试同一批次，SDK 网络调用运行在独立后台线程。队列满时优先淘汰尚未上传的较旧日志，保留已接受的任务尾日志；容量仍不足时拒绝新日志，并用 checkpoint 标记 `truncated=true`。没有磁盘缓冲或 Redis 备份，运行期间长时间故障导致的队列溢出、强制终止或断电仍可能丢失内存中的日志，重启无法补回。正常 Ctrl+C 退出会等待已接收的内存日志全部上传成功，不再设置 2 秒丢弃期限。

事件字段为 `kind=event`、`taskId`、`environment`、`id`、`firstId`、`streamMs`、`streamSeq`、`time`、`level`、`message`，以及可选且成对的 `actionIndex` / `actionTotal`。`id` 由 publisher 生成，格式兼容旧 Stream 游标；`firstId` 标识所属发布器的任务段，核验计数时用于排除其他发布器迟到的事件。领取重试任务时，在同一次 Redis 原子领取操作中读取旧 `logCursor`，作为新 ID 的下界。仅上传经过校验和脱敏的字段，单条正文最多 1000 字，不上传 session、用户名、tokenHash 或完整任务对象。重试保持事件 ID 和时间不变，查询端按任务和环境范围内的 `id` 去重。

每批附带 `kind=checkpoint`，包含 `firstId`、`throughId`、`entryCount`、`truncated` 及相同身份/排序字段。`entryCount` 是该 publisher 任务段累计成功上传的有效去重事件数；标记和事件可能不同步进入索引，查询端必须核验 `COUNT(DISTINCT id)`，并核对 Redis 摘要中的 `logCursor`，防止终态先到而尾日志尚未到。继承旧水位但没有旧 publisher 内存状态时标记可能有历史缺口。旧 Redis 日志回退不能恢复本次新事件；SLS 不可用或未入索引时需等待恢复。

索引需要 `streamMs`、`streamSeq`、`entryCount`、`actionIndex`、`actionTotal` 为 long，其余上述查询字段为 text，并允许 SQL 统计。`kind` 必须参与查询，避免把 checkpoint 当成用户日志。云凭据不能提交到 Git。

按 `Ctrl+C` 优雅停止时，worker 先停止接收任务，回收子进程并关闭对应实例，等待消费协程和定时清理协程结束并收齐尾部日志，再关闭状态上报器并排空 SLS 日志队列。SLS 网络故障期间持续重试，直到上传成功才退出；重复按 `Ctrl+C` 或再次发送 `SIGTERM` 只保留停止请求，不取消上传，也不清空队列。请保持窗口开启等待日志上传完成；关闭窗口、结束进程或断电不属于正常退出。出码前未过期的任务仍尝试重新入队；重新入队前将本次执行快照、结束日志和本地状态记为中断，避免网页一直显示准备中。任务 ID、登录凭证及原会话过期时间保留，下一个 worker 领取后更新归属和执行阶段。已发布二维码或已开始日常的任务记录为中断，不自动重排，以免重复执行游戏操作。尚未领取的任务保留在队列中。队列在校验后原子移除并登记归属，仍没有消息确认与崩溃恢复机制：若领取成功但响应丢失、进程被强制终止或机器崩溃，需先检查 Redis 归属、本地运行记录和实例占用，再决定是否释放或重新申请。

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
| `qr_navigation.py` / 根目录 `assets/` | 启动广告关闭、登录页面识别、ADB 点击和真实二维码裁剪；专用模板平铺为 `assets/login_*.png`，QQ 安卓登录、切换账号、开始游戏继续复用原有三张图片；不调用多模态 API |
| `pool_admin.py` | 查看实例占用，按任务 ID 显式释放实例 |
| `storage.py` | 阿里云 OSS 二维码存储、读取及过期清理 |

子进程从生成二维码一直运行到等待扫码、确认登录和日常执行结束，期间始终占用同一实例。执行失败或运行超时后，父进程停止脚本并立即上报失败，保留雷电模拟器60秒供排查，再按任务 ID 执行 `quit` 并释放池槽，不删除实例。保留期间不继续执行游戏动作，也不把该实例分配给新任务；其他空闲实例仍可处理任务。成功和主动停止服务时立即清理；保留期间按 `Ctrl+C` 也会提前结束等待并清理。若关闭失败，保留占位，并在运行记录中标记 `instance_cleanup_failed`，供管理员检查后通过 CLI 释放。

释放池槽只会关闭雷电进程并解除任务对实例索引的占用，不会删除实例，也不会清除 QQ 或游戏的账号数据。固定池不提供账号数据擦除或账号隔离。为避免继承上一个任务仍在运行的登录页面，每次新任务启动实例后会先强制停止游戏和 QQ 进程，再重新打开游戏；这同样不会清除应用数据。

配置 `LDPLAYER_POOL_INDICES=0,1,2`、`WORKER_CONCURRENCY=3` 时，最多有三个任务同时占用实例，等待扫码和执行日常都计入占用。第四个任务保持在 Redis 的 `queued` 状态，直到已有任务结束并释放槽位；排队期间仍遵守客户端会话 TTL。并发数不能扩大实际实例池容量，冷启动管理操作仍通过锁依次执行。

真实驱动通过 `python -u -m naruto_daily.login_service.ldplayer_worker` 运行隔离子进程。父进程传递任务 ID、登录有效期、雷电配置、日常配置路径和独立超时，并通过子进程环境传入 `APP_ENV`、已解析的 `REDIS_URL`、`MULTIMODAL_ROUTING_COUNTER_KEY` 和模型渠道配置。QQ 日常、周胜子进程还会收到同环境的 `DATABASE_URL` 与 `QQ_CAPTURE_API_BASE`，仅在进入抓号路径后使用。子进程使用同一 namespace 的模型计数器及任务刷新 key；Redis 和数据库地址不进入 JSON 协议或日志。选定队列名和状态目录原样传给子进程，不重新拉取 KV，也不重新读取本地模型配置；OSS、QQ 密钥和 Cloudflare Token 不传递。子进程 stdout 只用于结构化事件，执行异常只输出固定错误码。

## 对象存储

扫码服务使用阿里云 OSS 保存二维码，对象键为 `${OSS_KEY_PREFIX}/{id}/qr.png`，前缀来自选定 namespace。上传、读取、删除和过期清理统一走 OSS；启动时检查地域、Bucket 和访问密钥是否配置，缺少任一项都会停止启动。

真实 OSS 使用 Python `oss2` SDK，在 Cloudflare KV 中逐项配置以下测试值，并与测试客户端使用同一 Bucket 和地域；正式值独立保存在 `hyrz` namespace：

```dotenv
OSS_REGION=oss-cn-shanghai
OSS_BUCKET=huy1-hyrz-test
OSS_ACCESS_KEY_ID=your-access-key-id
OSS_ACCESS_KEY_SECRET=your-access-key-secret
# OSS_STS_TOKEN=your-temporary-token
```

`OSS_KEY_PREFIX` 表示 Bucket 内对象键的目录前缀，必须在所选 namespace 显式配置，例如当前测试值为 `OSS_KEY_PREFIX=login-test-jev`。程序不会根据 `APP_ENV` 修改前缀值；测试客户端和 worker 必须一致，队列中的 `objectKey` 也应使用相同前缀。

服务端拥有指定前缀的写入/读取/删除权限，客户端 TS 后端仅需读取权限；凭据可以不同。OSS 对象使用 HTTPS、私有 ACL、PNG 类型、`no-store` 和过期时间元数据。浏览器通过自己的后端读图片，不获得 OSS 密钥，也不需要 OSS CORS。

过期元数据不等于云端生命周期规则，应配置此前缀的 Bucket 生命周期，兜底清理服务崩溃时的遗留对象。环境配置完成不代表测试站已部署或实机验收；真实雷电需设置 `EMULATOR_DRIVER=ldplayer`，指定测试专用实例池，并实际核对出码、扫码、刷新和执行日志。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[login-service,login-service-test]"
.\.venv\Scripts\python.exe -m pytest tests/test_login_service_runtime.py tests/test_login_service_http.py tests/test_login_service_storage.py tests/test_login_service_redis.py tests/test_login_service_worker.py tests/test_packaging.py
```

Redis worker 的验证应包括 FIFO 消费、消息校验、会话状态与 TTL 保留、重连及关闭处理；HTTP 模式继续验证接口隔离、并发、PNG 输出、过期清理与 OSS 适配。实际网站联调需要同一环境内客户端与 Python 的 Redis 和 OSS 配置一致。

## 二维码刷新与实例占用

`<REDIS_QUEUE_KEY>:refresh:<taskId>` 是客户端后端写入的刷新命令，值为 `{taskId, requestId, requestedAt}`，TTL 600 秒。子进程等待授权时每秒检查自己的 key，不扫描其他任务。一次请求只执行一次；父进程上传期间 key 仍存在，但子进程不会重复刷新。

每次导航前只检查开始游戏按钮，匹配成功则执行登录收尾和日常。仍在 QQ 授权页（有效/失效码）时发送一次 Android Back，再点击游戏登录页右下角 QQ 安卓按钮；已经在登录页则直接点击。识别使用按钮/标题的局部模板，登录背景变化不影响判断。等待扫码期间不调用模型识别游戏主界面，保证轮询及时。

子进程发送 `refreshing` 和带 `requestId` 的 `qr` 事件。父进程核对任务及请求 ID，先上传到原 OSS 对象，然后原子更新 `qrRevision`、清除会话 `refreshRequestId` 并删除该命令。确认支持幂等重试，旧确认不会删除较新的请求。授权成功与刷新并发时，授权优先并清除待刷命令。上述操作均不重置 10 分钟占用时间。

QQ 抓号路径在成功读取本轮扫码结果后、打开游戏前发送 `scan_captured`。父进程写入会话 `scanCaptured` 和执行记录 `scanCapturedAt`，使用现有 `opening_game` 状态，清除待处理刷新请求。网站据此收起二维码和刷新入口，显示扫码成功、正在登录游戏。此时不设置 `loginAuthorized` / `authorizedAt`，也不开始日常计时；仍需游戏实际确认授权。会话更新暂时失败时，执行记录继续重试发布，游戏登录不因此中断。

扫码结果优先于迟到的刷新。刷新只允许替换仍为 `pending` 的抓号轮次；如果上传恰好先完成，则保留并读取原结果，不让刷新覆盖已完成的扫码。无效、过期或其他执行轮次的结果不会当作成功。

升级时客户端与 worker 需同时更新，重启常驻 worker 后新领取的任务才会启用刷新；已经运行的旧子进程不会热更新。扫码前的失败现场保留最多到占用截止时间，扫码成功后的执行期限与失败保留策略沿用原规则。

`scan_captured` 是新的父子进程事件。发布时必须让常驻 worker 和新启动的子进程使用同一版本；不要在仍运行旧 worker 的目录中直接替换代码，否则旧父进程会把新事件当作协议错误。
