# JEV 测试环境与扫码客户端协议

worker 与 `NarutoLoginClient` 使用同一套环境选择规则。各自的 `.env` 选择 `prod` 或 `test`，填写共用的 Cloudflare Token/account、对应 namespace ID 与本机参数；业务隔离值统一放在 Cloudflare，代码不按分支名生成 `test-jev` 后缀。

## Cloudflare 配置

```dotenv
APP_ENV=test
CLOUDFLARE_API_TOKEN=your-read-only-token
CLOUDFLARE_ACCOUNT_ID=your-account-id
CLOUDFLARE_KV_NAMESPACE_ID_PROD=your-hyrz-namespace-id
CLOUDFLARE_KV_NAMESPACE_ID_TEST=your-hyrz-test-namespace-id
```

`APP_ENV` 只接受 `prod` 或 `test`，省略时为 `prod`，测试工作区必须显式填写 `test`。正式环境使用现有 `hyrz` namespace，测试环境使用独立的 `hyrz-test` namespace，Token 和 account 共用。prod 选择 `CLOUDFLARE_KV_NAMESPACE_ID_PROD`，兼容旧 `CLOUDFLARE_KV_NAMESPACE_ID`；test 必须提供 `CLOUDFLARE_KV_NAMESPACE_ID_TEST`，不得回退旧 ID 或正式 ID。运行 Token 需要目标账户的 Workers KV Storage Read 权限，允许列出 key 和读取文本值；修改 KV 的管理 Token 另行保管，不放进浏览器或仓库。

每个 namespace 内都使用 `REDIS_QUEUE_KEY`、`OSS_KEY_PREFIX` 等普通变量名，不加环境前缀。启动只读取选定 namespace，所有配置读取成功后才启用；必填配置缺失或读取失败时停止启动，不读取另一个 namespace 或本地旧业务配置。修改 KV 后须重启网站后端和常驻 worker，已有子进程继续使用父进程传入的配置快照。

以下 22 个字段分别保存在 `hyrz` 和 `hyrz-test` 中，名称相同、值独立。密钥值不在文档中列出。

| 字段 | 用途与填写规则 |
| --- | --- |
| `REDIS_URL` | Redis 连接地址；同一环境的客户端和 worker 必须使用相同主机、数据库编号及认证信息 |
| `REDIS_QUEUE_KEY` | 队列及会话、账号锁、刷新、摘要、日志 key 的根前缀；必填 |
| `MULTIMODAL_ROUTING_COUNTER_KEY` | worker 生成模型路由的 Redis 计数器；必填，两个环境分别配置 |
| `LDPLAYER_STATE_DIR` | worker 的绑定、锁、运行状态和本地日志目录；必填，相对 worker 项目根目录解析 |
| `OSS_REGION` / `OSS_BUCKET` | 各环境的 OSS 地域及 Bucket；该环境内客户端与 worker 必须一致，当前正式与测试使用不同 Bucket |
| `OSS_ACCESS_KEY_ID` / `OSS_ACCESS_KEY_SECRET` | 后端 OSS 凭据；只授权相应对象前缀的所需操作 |
| `OSS_KEY_PREFIX` | 二维码对象目录前缀；客户端与 worker 必须一致，必填 |
| `STORAGE_DRIVER` | 网站对象读取方式；当前两个 namespace 均为 `oss` |
| `DATABASE_URL` | 网站 PostgreSQL 及 worker 通知查询所用的数据库连接；同主机、账号等连接信息，数据库名分别配置 |
| `JWT_SECRET` | 网站登录签名密钥；两个 namespace 使用独立随机值，至少 32 字节 |
| `SESSION_COOKIE_NAME` | 网站账号 Cookie 名；两个 namespace 分开配置，必填 |
| `PORT` | 网站本机端口；两个 namespace 分开配置，必填 |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` | worker DeepSeek 生成渠道，key 留空禁用；启用时模型名必填 |
| `JEV_API_KEY` / `JEV_MODEL` | worker JEV 选择判断渠道；JEV 场景需要有效 key/model |
| `QQ_BOT_APP_ID` / `QQ_BOT_APP_SECRET` / `QQ_BOT_GROUP_OPENID` | worker 主动通知；三项同时填写或全部留空，测试环境当前全部留空 |
| `QQ_BOT_ORIGINAL_GROUP_OPENID` | 保留的机器人原群标识；不代替运行时 `QQ_BOT_GROUP_OPENID` |

可选扩展也使用普通变量名，例如 `OSS_STS_TOKEN`、`ALLOWED_ORIGINS`。测试网站若改用 `local` 存储，须在 `hyrz-test` 显式配置独立的 `LOCAL_OBJECTS_DIR`，不可复用正式目录。鱼玩按原约定在 worker 本地 `.env` 配置 `YUWAN_API_KEY` 和 `YUWAN_MODEL`，启用时两项须成对填写；修改后重启，任务子进程继承父进程解析结果。QQ 通知使用测试 namespace 的普通 `QQ_BOT_*` 和 `DATABASE_URL`，不再单列本地测试专用变量。

当前资源值如下。它们是可修改的 Cloudflare 配置，不是源码默认值或自动重写规则。

| 用途 | `hyrz`（prod） | `hyrz-test`（test） |
| --- | --- | --- |
| Redis 队列 | `naruto:login:tasks` | `naruto:login-test-jev:tasks` |
| 模型计数器 | `naruto:multimodal:route:sequence` | `naruto:multimodal-test-jev:route:sequence` |
| OSS Bucket | 现有正式 Bucket | `huy1-hyrz-test` |
| OSS 前缀 | `login` | `login-test-jev` |
| worker 状态目录 | `.data/login-service/ldplayer` | `.data/login-service/ldplayer/test-jev` |
| PostgreSQL 数据库名 | `naruto_login` | `naruto_login_test` |
| 账号 Cookie | `naruto_session` | `naruto_session_test_jev` |
| 网站本机端口 | `4300` | `4301` |

PostgreSQL 复用同一主机、端口和账号，仅 URL 中的数据库名不同。测试库为独立空库，未复制正式用户或业务数据；测试站首次启动的建表流程只应作用于测试库。Redis URL 中的数据库编号与 PostgreSQL 数据库名是两件事；同一测试环境两端的 Redis URL 必须一致。当前 Redis 连接和 DB 共用、key 分开；测试 OSS 使用独立的 `huy1-hyrz-test` Bucket。Bucket 和对象前缀均可在各自 namespace 配置，该环境内客户端与 worker 必须一致。

## 任务、状态和日志

所有 Redis 任务资源从选定队列派生，不需要客户端自行拼入固定后缀。

| 用途 | 运行时 key / 路径 |
| --- | --- |
| FIFO 队列 | `${REDIS_QUEUE_KEY}` |
| 登录会话 | `${REDIS_QUEUE_KEY}:sessions:<taskId>` |
| 用户活动任务锁 | `${REDIS_QUEUE_KEY}:users:<userId>:active` |
| 二维码刷新命令 | `${REDIS_QUEUE_KEY}:refresh:<taskId>` |
| 执行摘要 | `${REDIS_QUEUE_KEY}:runs:<taskId>` |
| 执行日志 Stream | `${REDIS_QUEUE_KEY}:logs:<taskId>` |
| OSS 二维码 | `${OSS_KEY_PREFIX}/<taskId>/qr.png` |

消息字段、状态机和 TTL 沿用[worker 协议](../naruto_daily/login_service/README.md#redis-消息和状态)。客户端先创建带有 `tokenHash`、`simulation: true`、`queued` 状态、`userId` 和原时间戳的会话及用户活动锁，再 `LPUSH` 到所选队列。worker 原子领取，客户端不消费队列。

```json
{
  "version": 1,
  "loginTimeoutSeconds": 600,
  "id": "客户端生成的小写 UUID v4",
  "createdAt": "当前带时区的 ISO 8601 时间",
  "expiresAt": "创建后有效期内的带时区 ISO 8601 时间",
  "objectKey": "login-test-jev/同一个 UUID/qr.png",
  "loginType": "android_qq",
  "userOptions": {"equipmentStamina": true, "secretRealmMode": "all"}
}
```

示例使用当前测试 OSS 前缀，实际 `objectKey` 必须精确等于 `${OSS_KEY_PREFIX}/${taskId}/qr.png`。时间和 UUID 是占位符，实际值须符合既有校验。刷新命令格式为 `{taskId, requestId, requestedAt}`，TTL 600 秒；子进程只读取当前任务的刷新 key，上传成功后由父进程确认和删除。会话、摘要、日志、刷新幂等和保留规则沿用原协议。

浏览器通过自己的网站后端访问状态和二维码，不接收 Redis、数据库、OSS 或 Cloudflare 凭据。测试站的 JWT 和 Cookie 独立配置，测试账号须在测试站重新注册。

客户端 `test` 分支可在同一 `userOptions` 对象增加 `"mainTask":"arena_weekly"` 选择决斗场周胜，`daily` 或省略该字段继续原日常。周胜沿用相同扫码流程，清弹窗后从奖励入口查找“决斗场击败1名忍者”卡片并前往，第一次调用六区战斗模块起独立执行 40 分钟。到时执行子进程终止，任务正常记录 `completed` 并释放实例；登录和奖励入口准备不占用战斗的 40 分钟。

## 本机启动与实机验收

本机 `.env` 的 `EMULATOR_DRIVER=mock` 保持模拟运行。切换真实驱动前，配置测试专用的 `LDPLAYER_POOL_INDICES`，逐个确认 1920×1080、DPI 240 和 ADB；状态目录隔离不能防止两个 worker 指定同一模拟器索引。

worker 启动先完整读取选定 Cloudflare namespace，再在解析后的 `LDPLAYER_STATE_DIR` 重置实例登记。任务子进程继承 `APP_ENV`、队列、路由计数器、Redis 和模型配置，不重新读取云端或本地 `.env`。OSS、数据库、QQ 和 Cloudflare 凭据不传给执行子进程。

配置写入并不代表网站已部署或实机验收。本次需另行部署测试站，确认它使用 `APP_ENV=test`，再在测试专用模拟器池联调任务投递、二维码上传与刷新、扫码授权、日常执行、日志查询和清理；正式站、正式 worker 与正式模拟器池均保持各自配置。
