# 多模态网关路由

选择判断与内容生成使用两条显式路径，不启动额外 HTTP 服务。`ChoicePrompt` 交给 `decisions.py` 调用 JEV；普通字符串提示词与 OpenAI 兼容导航继续使用生成模型网关。生成渠道定义在 `channels.py`，路由和 Redis 计数在 `gateway.py`，两条路径的请求时限和重试都使用 `transport.py`。

场景提示词统一在本目录维护：[`prompt_jev.py`](prompt_jev.py) 集中定义 JEV 的通用指令、`ChoicePrompt`、主界面与秘境入口候选、战斗结束、丰饶、装备和一乐外卖提示；[`prompts.py`](prompts.py) 集中定义生成模型使用的文字、数字、坐标读取及毒风导航提示。业务模块只引用这些定义，负责截图、校验和动作执行，不再内嵌场景提示词。

## JEV 选择判断

根目录 `.env` 设置 `APP_ENV=test` 并填写 `CLOUDFLARE_KV_NAMESPACE_ID_TEST`；在测试 namespace `hyrz-test` 内配置以下普通 key，正式值保存在 `hyrz`：

```dotenv
JEV_API_KEY=your-model-key
JEV_MODEL=typesafe/jev-1.14
```

启动仅加载所选 namespace 的普通变量名，修改后重启 worker。测试环境缺少 namespace ID 或配置时不读取正式 namespace。兼容入口仍允许本地 JEV 配置覆盖所选环境（进程变量优先于 `.env`）；当前 JEV 配置保存在 KV。显式的本地空 JEV 值也会覆盖云端值，因此不要在 `.env` 留旧的空占位项。任务子进程只继承父进程解析结果，不再次拉取 KV 或读取本地文件。缺少 JEV 密钥时，选择场景明确报错，不回退到生成模型。

`decisions.py` 固定请求 `https://openrouter.ai/api/alpha/decisions`，发送图片、判定条件和一个 `choice` 问题，并携带图片功能请求头。JEV 只返回候选标签，程序验证标签后深拷贝本地结果，不能让 JEV 生成 JSON、坐标或解释。JEV 与生成模型均通过 `[多模态路由]` 日志在终端显示请求 URL 和实际模型名称；这些后台诊断不发送给客户端任务日志。模型请求和异常日志不包含密钥、图片、提示词或上游响应原文。

每次 JEV 请求发送前，将最长边超过 1568 像素的图片等比缩小至 1568，并使用 PNG 编码；例如 1920×1080 缩为 1568×882。较小图片保留原始内容，重试后的新截图同样处理。本地截图、候选坐标和文字模板复核仍使用原图。

| 选择场景 | 本地结果来源 |
| --- | --- |
| 主界面、装备子菜单、奖励入口、秘境入口 | 当前截图的候选编号映射坐标；页面不符、遮挡或候选不清时保持未确认 |
| 战斗是否结束、丰饶结算前景 | 已结束、进行中和未知等有限状态；未知时保持停止点击并重新观察 |
| 秘境结算兜底 | 每 10 轮未回房时识别一次奖励弹层、失败结算、无遮挡的房间或未知；识别奖励时允许标题被战斗倒计时遮挡 |
| 装备材料动作、弹层类型、所选装备能否进阶 | 固定操作枚举及 6 个装备槽位 |
| 一乐外卖 | 仍可领取、已无可领取、奖励弹层、无法确认四种状态；每轮先点击兼容两种布局的六个固定位置，再判断是否继续 |

奖励候选仍须通过同一帧文字模板复核。需要自由读取昵称、券数、重置次数、装备数值、副本名称，以及生成按钮坐标或执行导航工具的场景继续走原生成模型，JEV 不加入其轮询渠道。JEV 请求不访问 Redis 轮询计数；两条路径仍共用原任务截止时间、单次超时和重新截图重试规则。

秘境结算按外层轮次计数，同轮的模板补查不重复计数；第 10、20、30…轮仍未回房才启用兜底，新截图本地已确认房间、大厅或忍者选择页则直接按原流程处理。JEV 确认奖励或失败时使用原有空白坐标关闭，确认房间则结束结算；未知仅继续观察，不立即再次调用。每次 worker 接口重试都重新截图并先跑本地检查，模型响应后也重新检查本地页面，避免请求期间已经回房还点奖励。重试与等待继续共用原任务截止时间。

## 配置

在选定 Cloudflare namespace 中配置渠道的 key 列表、模型名、启停状态、Redis 和路由计数器。以下为示例，每行对应一项 KV，示例 key 需替换为真实值：

```dotenv
DEEPSEEK_API_KEY=["your-deepseek-key-1","your-deepseek-key-2"]
DEEPSEEK_MODEL=your-multimodal-model
DEEPSEEK_ENABLED=true
OPENAI_API_KEY=[]
OPENAI_MODEL=
OPENAI_ENABLED=false
REDIS_URL=redis://127.0.0.1:6379/0
MULTIMODAL_ROUTING_COUNTER_KEY=naruto:multimodal-test-jev:route:sequence
```

| 渠道 | 固定基础地址 | 密钥变量 | 模型变量 |
| --- | --- | --- | --- |
| 鱼玩 | `https://llm-proxy.yuwan-game.com/v1` | `YUWAN_API_KEY` | `YUWAN_MODEL` |
| DeepSeek 官方 | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` |
| OpenAI 官方 | `https://api.openai.com/v1` | `OPENAI_API_KEY` | `OPENAI_MODEL` |

`*_API_KEY` 支持 JSON 字符串数组，也兼容原有纯文本单 key。每个渠道最多 20 个 key，每个 key 最长 512 个字符；列表元素不能为空或含控制字符。模型名最长 200 个字符。`*_ENABLED` 使用 `true` / `false`；未设置时保留原行为，有 key 就启用。设置 `false` 后跳过整个渠道；空列表或空 key 也不参与轮询。每个 key 与该渠道的同一模型配成一项，按列表顺序轮询。JEV 保持独立配置，不加入生成模型轮询。

OpenAI 沿用项目的 Chat Completions 协议，需要配置支持图片输入及所需工具调用的模型；连通性测试只证明基本请求可达，不保证所有任务能力。请求参数依据 [OpenAI 官方接口文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)，OpenAI 路由将 `max_tokens` 转换为 `max_completion_tokens`，去掉 DeepSeek 的 `thinking` 和通用请求中的 `reasoning_effort`，避免向不支持这些参数的模型发送它们。

鱼玩按原约定在 worker 本地 `.env` 配置 `YUWAN_API_KEY` 和 `YUWAN_MODEL`，启用时两项须成对填写；修改后重启 worker。任务子进程继承父进程解析后的鱼玩配置，不重新读取本地文件。

DeepSeek 使用 `/chat/completions`，地址依据[官方 API 文档](https://api-docs.deepseek.com/)。模型名是独立配置项，示例名称可按该渠道提供的多模态模型修改。

DeepSeek 官方不支持在思考模式下强制或指定工具，详见[请求参数文档](https://api-docs.deepseek.com/api/create-chat-completion)。路由层仅对该渠道此类导航请求关闭思考模式，保留工具约束；普通图片识别沿用原参数，重试切到其他兼容渠道时不会继承 DeepSeek 官方参数适配。

启动时验证配置格式，并按启停状态和 key 列表确定有效项，不请求上游验证 key、模型或额度。有效渠道必须填写模型名；没有有效渠道或缺少 `REDIS_URL` 时明确报配置错误。只配置一个 key 也正常参与 Redis 计数。

DeepSeek、Redis 和路由计数器使用所选 namespace 中的同名 KV key，不回退到本地 `.env`。根目录 `.env` 还需配置 `APP_ENV`、`CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID` 和对应的 `CLOUDFLARE_KV_NAMESPACE_ID_PROD` / `CLOUDFLARE_KV_NAMESPACE_ID_TEST`，详见[项目配置说明](../../README.md)与[完整字段表](../../docs/test-jev-client-protocol.md#cloudflare-配置)。

服务启动只拉取一次所选 KV namespace，读取全部成功后初始化网关。有效渠道列表在本次启动中固定，修改配置后重启服务；任务子进程继承父进程的 `APP_ENV`、模型配置、已解析 `REDIS_URL` 和 `MULTIMODAL_ROUTING_COUNTER_KEY`，不重复拉取云端。普通脚本访问模型也使用相同的启动加载流程。

DeepSeek 和 OpenAI 配置使用上表中的变量，key 留空即不参与轮询。任务 JSON 的 `multimodal` 只需保留 `timeout_seconds` 和 `max_calls`，请删除旧的 URL、模型占位符；它们不再决定实际路由，也不再通过旧配置自动补齐。

`NarutoLoginClient` 的管理员菜单提供「大模型渠道」管理：查看当前环境、编辑模型及 key 列表、启停渠道、逐个 key 测试连通性。页面只显示已存 key 的脱敏摘要，可保留、替换、删除或新增；测试当前填写的值不会保存配置，也不会消耗 Redis 路由计数。保存写入当前网站环境的 namespace，等待 KV 同步后重启 worker 生效；已经运行的子进程继续使用旧快照。鱼玩存在本地 `.env` 覆盖时，应同时检查本地配置。

升级顺序：先部署支持列表的 worker 和 client，再通过管理页保存列表并重启 worker。旧版 worker 不认识 JSON key 列表，不能先把正在使用的单 key 改成 JSON。初始 OpenAI 配置为 `OPENAI_API_KEY=[]`、空 `OPENAI_MODEL`、`OPENAI_ENABLED=false`，填写真实 key 与模型、测试通过后再启用。

## 请求流程

1. 启动时按鱼玩、DeepSeek、OpenAI 的预定义顺序筛选启用渠道，将每个渠道的 key 列表展开成独立轮询项，冻结有效列表和配置。
2. 每次生成模型的实际 HTTP 尝试前，对所选 namespace 的 `MULTIMODAL_ROUTING_COUNTER_KEY` 执行原子 `INCR`。当前测试值为 `naruto:multimodal-test-jev:route:sequence`，代码不固定 key 或追加后缀；缺少该配置时停止初始化。
3. 用返回值对有效 `model + key` 项数取模，作为有效列表的下标，同时替换 URL、Authorization 和请求体中的 `model`。
4. 请求成功返回；非 200 或网络异常按现有规则等待并重试，下一次尝试再次 `INCR`。图片重试仍重新截图，导航历史和已执行的工具动作保留。

计数器不存在时 Redis 从 0 增加到 1，按有效列表长度取模选择下标；仅一个有效渠道时，下标始终为 0。已有计数器继续累计，不在服务启动时清零，也不设置过期时间。多个进程共用同一 Redis URL、数据库和 key；它们应部署一致的渠道配置。并发情况下其他请求也会消耗序号，因此单个请求的相邻重试不保证切换到另一渠道。

Redis 操作失败会记录 `error_kind=redis_routing`，在原有请求预算内重试；不会回退到进程内计数或绕过路由发送 HTTP。有效列表不会因一次请求失败而动态剔除渠道。日常任务只接受 HTTP 200，其他状态和网络错误每隔 10 秒重试，单次请求上限 30 秒，所有请求共用任务截止时间；独立调用最多额外重试 3 次。

每次路由选择（包括重试）的日志显示渠道名、完整请求 URL、模型名、序号和有效渠道数量，不输出密钥和 Redis 凭据。这类日志仅输出到 Python 后台诊断日志，不转发为客户端日志事件，也不写入客户端读取的 Redis 日志流。
