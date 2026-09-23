# 火影忍者扫码登录任务服务

这个 TypeScript 模块运行在现有 `NarutoDailyADB` 项目中，供独立客户端的 **TS 后端**调用。当前默认运行本地模拟链路，电脑不需要安装雷电模拟器，也不需要阿里云凭据。

客户端生成 UUID 和会话令牌 → 投递任务 → 内存 Redis 列表队列 → 独立 Node 子进程模拟启动平板与打开游戏 → 生成 PNG 二维码 → 按 UUID 写入本地模拟 OSS → 客户端后端轮询对象状态并转发图片。

二维码可以被扫码器识别，内容明确标记为模拟数据，**不能用于真实游戏登录**。生成并显示二维码是本阶段的终点，没有实现扫码确认、游戏登录成功或登录凭据获取。

## 启动

使用 Node.js 22 或更新版本，在本目录执行。

```powershell
npm install
Copy-Item .env.example .env
npm run dev
```

默认地址为 `http://127.0.0.1:4320`。客户端后端需要配置相同的 `SERVICE_TOKEN`；默认的 `local-development-token` 仅允许绑定回环地址。两个程序是独立进程，分别启动。

编译运行和验证命令如下。

```powershell
npm run typecheck
npm test
npm run build
npm start
```

`src/app.ts` 导出 `createApp`，`src/runtime.ts` 导出 `LoginRuntime`，测试会启动真实 HTTP 服务和独立模拟器子进程。

## 接口约定

所有 `/internal/*` 接口都要求 `X-Service-Token` 请求头。接口不开放浏览器 CORS；服务令牌及 OSS 凭据只配置在后端，不能放进前端环境变量。

| 接口 | 行为 |
| --- | --- |
| `GET /health` | 无需令牌，返回服务状态、模拟标志、存储驱动和队列计数 |
| `POST /internal/tasks` | 接收 `{id, accessToken, createdAt, expiresAt}`，返回 `202 {id, status: "queued"}` |
| `GET /internal/tasks/:id` | 额外要求 `X-Session-Token: accessToken`，返回任务状态 |
| `HEAD /internal/objects/:id/qr.png` | 对象可读取返回 200，否则 404；用于客户端后端跟踪 OSS 状态 |
| `GET /internal/objects/:id/qr.png` | 转发 PNG，带有 `Cache-Control: no-store` |

`id` 必须为 UUID，`accessToken` 必须为 64 位十六进制随机值。时间戳使用 UTC ISO 格式，例如 `2026-09-23T03:00:00.000Z`。客户端默认传入 120 秒有效期；服务最多允许 10 分钟，拒绝过期任务和异常创建时间。

同一 ID、令牌和时间戳的重试不会重复创建任务或子进程；同 ID 的内容冲突返回 409。错误会话令牌、缺少会话令牌和未知任务均返回 404。队列或任务表满时返回 429。过期二维码立即不可读取，并异步删除文件；任务状态默认额外保留 60 秒，之后返回 404。

任务查询返回字段如下，不包含会话令牌。

```json
{
  "id": "d39bd44a-a7d9-4c2f-9773-7d7c5c410e86",
  "status": "qr_ready",
  "createdAt": "2026-09-23T03:00:00.000Z",
  "expiresAt": "2026-09-23T03:02:00.000Z",
  "simulation": true
}
```

状态依次为 `queued`、`starting_emulator`、`opening_game`、`preparing_qr`、`qr_ready`，也可能进入 `failed` 或 `expired`。失败时附带可展示的 `error`，不会返回 OSS SDK 的原始异常或凭据。

## 队列与模拟器

`src/queue.ts` 提供明确标注的内存 Redis 替身，使用 `LPUSH + BRPOP` 语义维持 FIFO。默认并发 2、等待队列上限 100、任务表上限 500，可以通过 `.env` 配置。队列和任务表不持久化，服务重启后客户端需重新发起登录。

每个被消费的任务都会通过 `child_process.fork` 启动一个独立 Node 子进程。子进程模拟打开火影忍者，再进入准备二维码阶段；“与安卓好友玩 → 切换账号 → 二维码登录”的实际点击尚未实现。退出、过期、失败及服务关闭时都会回收子进程。

未来雷电适配的入口是 `src/emulator.ts` 和 `src/runtime.ts` 中的驱动分支：实现独立实例创建、ADB 连接、游戏启动、二维码截图及销毁，再复用现有队列与存储接口。`EMULATOR_DRIVER=ldplayer` 当前会显式返回失败，不会伪装成真实模拟器已启动。

## 本地与真实 OSS

默认 `STORAGE_DRIVER=local`，对象位于 `.data/objects/login/{id}/qr.png`，相邻 `metadata.json` 保存过期时间，写入使用临时文件与原子重命名。仅服务端受保护的对象接口提供访问，不会将目录公开为静态资源。启动时及运行中每 10 秒清理过期孤立文件，运行中任务另有更及时的过期清理。

默认对象键为 `login/{id}/qr.png`，`OSS_KEY_PREFIX` 可配置前缀。本地 `.env`、`.data`、依赖和编译产物已忽略。

真实 OSS 适配已保留，配置示例如下。

```dotenv
STORAGE_DRIVER=oss
OSS_REGION=oss-cn-hangzhou
OSS_BUCKET=your-private-bucket
OSS_ACCESS_KEY_ID=configure-locally
OSS_ACCESS_KEY_SECRET=configure-locally
# OSS_STS_TOKEN=optional-temporary-token
OSS_KEY_PREFIX=login
```

适配器使用 HTTPS，设置私有对象 ACL、`Content-Type: image/png`、`Cache-Control: no-store` 及 `x-oss-meta-expires-at`。客户端仍通过自己的后端查询对象，浏览器不需要 Bucket 公网读权限、OSS 密钥或 Bucket CORS 配置。凭据需要相应前缀的写入、读取和删除权限。

真实 OSS 尚未使用 Bucket 凭据进行联调。运行时会删除已知过期对象；OSS 的过期元数据本身不触发云端自动删除，生产 Bucket 应额外为此前缀配置生命周期规则，清理服务崩溃或重启遗留的对象。模拟队列不能替代多机器部署时的真实 Redis；真实模拟器及登录成功回调也属于后续适配工作。

## 验证范围

测试覆盖 FIFO 和队列容量、并发限制、幂等投递、服务与会话鉴权、UUID/有效期校验、真实子进程生成 PNG、二维码内容可解码且标记为模拟、HEAD/GET 对象读取、过期文件删除、孤立对象清理、运行中子进程过期及服务关闭回收、未实现雷电驱动的明确失败。
