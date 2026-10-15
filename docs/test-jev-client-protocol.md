# test-jev 扫码测试客户端协议

本实验分支与正式 worker 可以连接同一个 Redis 数据库和 OSS Bucket，但必须使用下面的测试命名空间。当前修改仅在 Python worker 仓库中；独立的 `NarutoLoginClient` 测试站部署时需要同步本协议。

## 测试站配置

```dotenv
REDIS_QUEUE_KEY=naruto:login-test-jev:tasks
OSS_KEY_PREFIX=login-test-jev
```

`REDIS_URL`、OSS Bucket、地域和密钥只配置在测试站后端。Redis URL 中的数据库编号须与 worker 相同。浏览器仍通过测试站后端访问状态和二维码，不获得云资源凭据。

worker 在加载配置后强制规范化命名空间，不能通过共享 Cloudflare KV 中的正式 `REDIS_QUEUE_KEY=naruto:login:tasks` 或 `OSS_KEY_PREFIX=login` 取消隔离。正式队列不会被该实验分支消费。

| 用途 | 默认测试 key / 路径 |
| --- | --- |
| FIFO 队列 | `naruto:login-test-jev:tasks` |
| 登录会话 | `naruto:login-test-jev:tasks:sessions:<taskId>` |
| 用户活动任务锁 | `naruto:login-test-jev:tasks:users:<userId>:active` |
| 二维码刷新命令 | `naruto:login-test-jev:tasks:refresh:<taskId>` |
| 执行摘要 | `naruto:login-test-jev:tasks:runs:<taskId>` |
| 执行日志 Stream | `naruto:login-test-jev:tasks:logs:<taskId>` |
| 生成模型轮询计数 | `naruto:multimodal-test-jev:route:sequence` |
| OSS 二维码 | `login-test-jev/<taskId>/qr.png` |

客户端的限流、账号锁或其他自有 Redis key 也应从测试队列或 `naruto:login-test-jev` 派生。不要复用正式站的锁、计数或日志 key。

自定义队列规则与 `naruto_daily/common/namespace.py` 一致。`naruto:<subsystem>:...` 在第二段追加 `-test-jev`；其他队列在第一段追加。例如 `naruto:login:custom` 变为 `naruto:login-test-jev:custom`，`custom:tasks` 变为 `custom-test-jev:tasks`。规范化后的 key 最多 128 字符。OSS 在第一段追加后缀，例如 `login/qr` 变为 `login-test-jev/qr`。已带后缀的目标段保持不变，两端重复规范化不产生双后缀。

## 投递和读取

消息字段、状态机和 TTL 沿用[worker 协议](../naruto_daily/login_service/README.md#redis-消息和状态)。测试站先创建带有 `tokenHash`、`simulation: true`、`queued` 状态、`userId` 和原时间戳的会话及用户活动锁，再 `LPUSH` 到测试队列。worker 原子领取，客户端不消费队列。

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

示例中的时间和 UUID 为占位符，实际投递必须符合既有校验。`objectKey` 必须精确等于 `${OSS_KEY_PREFIX}/${taskId}/qr.png`；保留正式 `login/` 路径的消息会被拒绝，不会自动改写到正式对象。

刷新命令写入测试 `refresh` key，格式 `{taskId, requestId, requestedAt}`，TTL 600 秒。子进程只读取当前任务的测试 key，上传成功后由父进程确认和删除。会话、执行摘要和日志的字段、刷新幂等规则、读取权限及保留时间保持既有协议。

测试站应使用独立的网站数据库、任务归属记录、域名和 Cookie 配置。Redis/OSS 前缀隔离不会自动分离网站的 PostgreSQL 记录或浏览器登录身份。

## 本机执行和通知

本地实例绑定、文件锁和运行日志写入 `${LDPLAYER_STATE_DIR}/test-jev`；默认目录为 `.data/login-service/ldplayer/test-jev`，重复配置该末段不会再次追加。绝对路径也遵守此规则，启动时实例登记重置、任务子进程及实例池管理命令均使用同一个隔离目录。

测试服务还必须配置单独的雷电实例池。目录隔离并不能防止两个进程显式选择相同的模拟器索引。测试分支的本地配置默认 `mock`，切换真实驱动前再指定测试专用的实例索引。

QQ 群主动通知默认关闭，正式 `QQ_BOT_*` 和 `DATABASE_URL` 不参与本分支通知配置。需要通知时，在测试 worker 的 `.env` 中显式配置以下四项，并指向测试机器人、测试群及测试站数据库：

```dotenv
TEST_JEV_QQ_BOT_APP_ID=
TEST_JEV_QQ_BOT_APP_SECRET=
TEST_JEV_QQ_BOT_GROUP_OPENID=
TEST_JEV_DATABASE_URL=
```

三项机器人参数必须一起填写，启用通知须同时提供测试数据库；这些参数不传给执行子进程。
