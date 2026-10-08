# 多模态网关路由

普通图片识别和 OpenAI 兼容导航共用进程内路由层，不启动额外 HTTP 服务。渠道定义在 `channels.py`，路由和 Redis 计数在 `gateway.py`，请求超时及重试在 `transport.py`。

## 配置

在项目根目录 `.env` 中独立配置每个渠道的 key 和模型名：

```dotenv
YUWAN_API_KEY=
YUWAN_MODEL=deepseek-v4.1-flash
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-flash
REDIS_URL=redis://127.0.0.1:6379/0
```

| 渠道 | 固定基础地址 | 密钥变量 | 模型变量 |
| --- | --- | --- | --- |
| 鱼玩 | `https://llm-proxy.yuwan-game.com/v1` | `YUWAN_API_KEY` | `YUWAN_MODEL` |
| DeepSeek 官方 | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` |

两者均使用 `/chat/completions`。DeepSeek 地址依据[官方 API 文档](https://api-docs.deepseek.com/)。模型名是独立配置项，示例名称可按对应渠道提供的多模态模型修改。

DeepSeek 官方不支持在思考模式下强制或指定工具，详见[请求参数文档](https://api-docs.deepseek.com/api/create-chat-completion)。路由层仅对该渠道此类导航请求关闭思考模式，保留工具约束；普通图片识别沿用原参数，重试切到鱼玩时也不会继承官方参数适配。

启动时只按去除首尾空白后的 key 是否非空确定有效渠道，不请求上游验证 key、模型或额度。有效渠道必须填写模型名；没有有效渠道或缺少 `REDIS_URL` 时明确报配置错误。只配置一个渠道也正常参与 Redis 计数。

各渠道 key、model 只读取项目根目录 `.env`，进程环境和 `--env-file` 不覆盖它们；有效渠道列表和模型配置在每个进程首次初始化时固定，修改后重启服务及运行中的任务。Redis 沿用服务配置的环境优先级；常驻 worker 通过子进程环境传递其已解析的 `REDIS_URL`，确保父子进程使用同一数据库。普通脚本访问模型也需要 Redis。

配置只使用上表中的渠道变量，key 留空即禁用对应渠道。任务 JSON 的 `multimodal` 只需保留 `timeout_seconds` 和 `max_calls`，请删除旧的 URL、模型占位符；它们不再决定实际路由，也不再通过旧配置自动补齐。

## 请求流程

1. 启动时按固定顺序 `[yuwan, deepseek]` 筛选有 key 的渠道，冻结配置。
2. 每次实际 HTTP 尝试前执行一次原子 `INCR naruto:multimodal:route:sequence`。
3. 用返回值对有效渠道数取模，作为有效列表的下标，同时替换 URL、Authorization 和请求体中的 `model`。
4. 请求成功返回；非 200 或网络异常按现有规则等待并重试，下一次尝试再次 `INCR`。图片重试仍重新截图，导航历史和已执行的工具动作保留。

计数器不存在时 Redis 从 0 增加到 1，所以两个渠道都有效时第一请求选 DeepSeek，第二请求选鱼玩。已有计数器继续累计，不在服务启动时清零，也不设置过期时间。多个进程共用同一 Redis URL、数据库和 key；它们应部署一致的渠道配置。并发情况下其他请求也会消耗序号，因此单个请求的相邻重试不保证切换到另一渠道。

Redis 操作失败会记录 `error_kind=redis_routing`，在原有请求预算内重试；不会回退到进程内计数或绕过路由发送 HTTP。有效列表不会因一次请求失败而动态剔除渠道。日常任务只接受 HTTP 200，其他状态和网络错误每隔 10 秒重试，单次请求上限 30 秒，所有请求共用任务截止时间；独立调用最多额外重试 3 次。

每次路由选择（包括重试）的日志显示渠道名、完整请求 URL、模型名、序号和有效渠道数量，不输出密钥和 Redis 凭据。
