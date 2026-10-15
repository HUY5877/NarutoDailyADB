# 多模态网关路由

选择判断与内容生成使用两条显式路径，不启动额外 HTTP 服务。`ChoicePrompt` 交给 `decisions.py` 调用 JEV；普通字符串提示词与 OpenAI 兼容导航继续使用生成模型网关。生成渠道定义在 `channels.py`，路由和 Redis 计数在 `gateway.py`，两条路径的请求时限和重试都使用 `transport.py`。

## JEV 选择判断

在根目录被 Git 忽略的 `.env` 中配置：

```dotenv
JEV_API_KEY=
JEV_MODEL=typesafe/jev-1.14
```

本地 JEV 配置优先于共享 KV，显式进程变量优先于本地文件；任务子进程继承父进程解析的值。修改配置后重启 worker。缺少 JEV 密钥时，选择场景明确报错，不回退到生成模型。

`decisions.py` 固定请求 `https://openrouter.ai/api/alpha/decisions`，发送图片、判定条件和一个 `choice` 问题，并携带图片功能请求头。JEV 只返回候选标签，程序验证标签后深拷贝本地结果，不能让 JEV 生成 JSON、坐标或解释。模型请求和异常日志不包含密钥、图片、提示词或上游响应原文。

| 选择场景 | 本地结果来源 |
| --- | --- |
| 主界面、装备子菜单、奖励入口、秘境入口 | 当前截图的候选编号映射坐标；页面不符、遮挡或候选不清时保持未确认 |
| 战斗是否结束、丰饶结算前景 | 已结束、进行中和未知等有限状态；未知时保持停止点击并重新观察 |
| 装备材料动作、弹层类型、所选装备能否进阶 | 固定操作枚举及 6 个装备槽位 |
| 一乐外卖 | 3 个位置的 8 种可领取组合，或奖励/未知前景 |

奖励候选仍须通过同一帧文字模板复核。需要自由读取昵称、券数、重置次数、装备数值、副本名称，以及生成按钮坐标或执行导航工具的场景继续走原生成模型，JEV 不加入其轮询渠道。JEV 请求不访问 Redis 轮询计数；两条路径仍共用原任务截止时间、单次超时和重新截图重试规则。

## 配置

在 Cloudflare KV 中配置 DeepSeek 的 key 和模型名，以下每行对应一项 KV：

```dotenv
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-flash
REDIS_URL=redis://127.0.0.1:6379/0
```

| 渠道 | 固定基础地址 | 密钥变量 | 模型变量 |
| --- | --- | --- | --- |
| 鱼玩 | `https://llm-proxy.yuwan-game.com/v1` | `YUWAN_API_KEY` | `YUWAN_MODEL` |
| DeepSeek 官方 | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` |

DeepSeek 使用 `/chat/completions`，地址依据[官方 API 文档](https://api-docs.deepseek.com/)。模型名是独立配置项，示例名称可按该渠道提供的多模态模型修改。

DeepSeek 官方不支持在思考模式下强制或指定工具，详见[请求参数文档](https://api-docs.deepseek.com/api/create-chat-completion)。路由层仅对该渠道此类导航请求关闭思考模式，保留工具约束；普通图片识别沿用原参数，重试切到其他兼容渠道时不会继承 DeepSeek 官方参数适配。

启动时只按去除首尾空白后的 key 是否非空确定有效渠道，不请求上游验证 key、模型或额度。有效渠道必须填写模型名；没有有效渠道或缺少 `REDIS_URL` 时明确报配置错误。只配置一个渠道也正常参与 Redis 计数。

DeepSeek 和 Redis 只读取启动时从 Cloudflare KV 注入的环境变量，不回退到本地 `.env`。根目录 `.env` 还需配置 `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID` 和 `CLOUDFLARE_KV_NAMESPACE_ID`，详见[项目配置说明](../../README.md)。

服务启动只拉取一次 KV，读取全部成功后初始化网关。有效渠道列表在本次启动中固定，修改配置后重启服务；任务子进程继承父进程的模型配置和已解析 `REDIS_URL`，不重复拉取云端。普通脚本访问模型也使用相同的启动加载流程。

DeepSeek 配置使用上表中的变量，key 留空即禁用该渠道。任务 JSON 的 `multimodal` 只需保留 `timeout_seconds` 和 `max_calls`，请删除旧的 URL、模型占位符；它们不再决定实际路由，也不再通过旧配置自动补齐。

## 请求流程

1. 启动时按预定义顺序筛选有 key 的渠道，冻结有效列表和配置。
2. 每次生成模型的实际 HTTP 尝试前执行一次原子 `INCR naruto:multimodal-test-jev:route:sequence`，与正式环境隔离。
3. 用返回值对有效渠道数取模，作为有效列表的下标，同时替换 URL、Authorization 和请求体中的 `model`。
4. 请求成功返回；非 200 或网络异常按现有规则等待并重试，下一次尝试再次 `INCR`。图片重试仍重新截图，导航历史和已执行的工具动作保留。

计数器不存在时 Redis 从 0 增加到 1，按有效列表长度取模选择下标；仅一个有效渠道时，下标始终为 0。已有计数器继续累计，不在服务启动时清零，也不设置过期时间。多个进程共用同一 Redis URL、数据库和 key；它们应部署一致的渠道配置。并发情况下其他请求也会消耗序号，因此单个请求的相邻重试不保证切换到另一渠道。

Redis 操作失败会记录 `error_kind=redis_routing`，在原有请求预算内重试；不会回退到进程内计数或绕过路由发送 HTTP。有效列表不会因一次请求失败而动态剔除渠道。日常任务只接受 HTTP 200，其他状态和网络错误每隔 10 秒重试，单次请求上限 30 秒，所有请求共用任务截止时间；独立调用最多额外重试 3 次。

每次路由选择（包括重试）的日志显示渠道名、完整请求 URL、模型名、序号和有效渠道数量，不输出密钥和 Redis 凭据。这类日志仅输出到 Python 后台诊断日志，不转发为客户端日志事件，也不写入客户端读取的 Redis 日志流。
