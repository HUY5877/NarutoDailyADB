# 大模型渠道管理实施计划

> **For agentic workers:** 使用 test-driven-development 实施，独立部分并行开发，最后统一审查。

**Goal:** 增加 OpenAI、多 key Redis 轮询、管理员渠道配置及连通性测试。

**Architecture:** worker 沿用 Cloudflare KV 启动快照和现有 Redis 计数器。client 后端只在当前 APP_ENV 选定的 namespace 管理 OpenAI、DeepSeek；浏览器只收脱敏 key 信息。鱼玩只使用本机 `.env` 的 key/model。每个渠道固定 API 地址。

**Tech Stack:** Python、Redis、Cloudflare SDK；client 使用现有 Express、React、TypeScript。

**Spec:** 用户本轮需求及追加的连通性测试。用户明确要求开始实现；现有技术栈不变。

## 共同约定

- worker 从 origin/main 建立 codex/llm-channel-management，并推送同名远端；client 后续按用户要求直接在 main 开发。提交说明中文。
- OPENAI_API_KEY / DEEPSEEK_API_KEY / YUWAN_API_KEY 支持 JSON 字符串数组，兼容旧纯文本单 key。
- 每渠道同一 model，对应 *_MODEL；OpenAI、DeepSeek 的 *_ENABLED 缺省保持旧行为（有 key 就启用），false 跳过；新增 OpenAI 默认关闭。鱼玩仅本机 `.env` 有 key 才启用，无 key 则关闭，不使用云端配置或独立启停变量。
- 保留现有渠道定义顺序，OpenAI 追加；每渠道按 key 列表顺序展开。沿用 INCR % 槽位数，计数器不重置。
- 所有 key 的日志脱敏逐项处理；子进程继承列表及启停配置。
- KV 保持当前启动读取语义，保存后提示重启 worker 生效，不宣传即时生效。
- 管理员可以测试未保存的 model/key 或已保存的指定 key；固定上游 URL，超时、禁重定向，有限输出。测试不保存、不消耗轮询计数，不返回完整上游错误或 key。
- 生产站只操作 prod namespace；不接受浏览器指定 namespace。测试缺配置时不能回退生产。
- 云端先补齐缺失 OpenAI 默认空配置；用户后续要求已将正式、测试 DeepSeek 原密钥迁为单元素列表，回读及连通性验证通过，worker 重启前须升级支持列表的新代码。

## 任务与验证

- [x] worker：channels.py、gateway.py、environment.py、log_events.py；先补多 key、启停、错误配置、OpenAI 请求与逐项脱敏测试，再实现。测试网关、环境、日志、任务继承。
- [x] client 服务端：新增模型配置服务、Cloudflare KV 读写、受管理员保护的 GET/PUT/POST test 接口，同时接入本地和 Vercel 入口。测试权限、环境隔离、脱敏保留、校验、写失败、测试成功与失败。
- [x] client 前端：管理员菜单新增渠道弹窗，启停/model/key 列表编辑、保留/替换/删除 key、单 key 测试、保存及错误提示。沿用现有样式；类型检查、构建、交互验证。
- [x] 云端与文档：补缺省 OpenAI 配置，更新配置文档，说明 worker 重启及 Cloudflare Token 写权限。
- [x] 集成：两仓库运行相关测试与完整可运行检查，独立审查，修正问题，中文提交并推送。

## 审查重点

1. 未登录/普通用户不能查看配置、修改配置或消耗测试额度。
2. JSON key 列表不能作为一个密钥发送，逐项不泄漏至日志。
3. 浏览器中的保留标识不能成为写入 KV 的真实 key。
4. 正式、测试 namespace 不串用，失败不能显示保存成功。
5. 连通性测试有界，鉴权错误、限流、异常响应均给安全、明确结果。

## 进度

主目录已有文档删除保留，worker 在 Codex 托管 worktree 的独立 .venv 验证；client 原目录干净，按用户要求切到新分支开发。两仓库均已建立同名远端分支。

独立审查的三个 P2 均已修复并有先失败后通过的回归测试：所有保存携带原始 KV 快照版本；坏配置按单渠道隔离并可修复；本地鱼玩关闭先于 key 解析。保留现有 Redis INCR 取模与启动快照语义。Cloudflare KV 不提供比较并交换事务，版本检查用于拒绝已经读取到的过期快照，不宣称跨实例写入具有数据库事务保证。

prod/test 已补齐并回读 OpenAI 默认关闭配置。测试环境的管理员服务保存、回读成功；现有 DeepSeek key 的真实连通性测试成功，耗时约 1470 ms。没有填写真实 OpenAI key/model，也没有部署两仓库的新代码。

原有中断任务浏览器测试同时匹配顶部提示与行内状态，已将断言定位到稳定的提交结果提示。原有排行浏览器测试在弹窗入场动画期间测量行高，现等待动画结束后测量；均未修改对应产品行为。

## 收尾验证

- client：385 项服务端自动测试全部通过；渠道管理 18 项、中断任务 9 项、原有管理员界面 20 项浏览器测试通过；生产构建通过。构建仍有现有的前端包大小提示。
- worker：全量执行 4446 项，首轮 4444 项通过，2 项打包测试因本任务虚拟环境缺少 setuptools 失败。安装 pyproject 声明的构建依赖后，两项重跑通过；没有为此修改源代码。准确结果为全套已执行、失败项复验通过，并非单次全量全绿。
- worker 本轮相关 247 项回归测试通过，包含本地关闭鱼玩后忽略坏 key 的边界测试。
- 两仓库完成独立审查与差异格式检查。原主目录的未提交文档删除未改动。生产、测试站均未部署新代码；OpenAI 仍需管理员填写真实 model 和 key 并开启，保存后等待 KV 同步并重启 worker。

## 后续范围修正

用户明确鱼玩仅按各机器的本地网络可用性配置。client 的页面、接口、类型和测试只包含 OpenAI、DeepSeek，拒绝鱼玩的保存和测试请求。worker 丢弃云端鱼玩 key/model/开关，仅使用本地 `.env` 的 key/model；无本地 key 或空列表时关闭，不因云端有 key 而启用，也不因旧开关值关闭已有本地 key。子进程只继承本机解析后的鱼玩配置。

修正验证：worker 环境、Cloudflare 启动、网关、子进程继承、实际日志和测试环境隔离相关 298 项测试通过；client 模型管理、Cloudflare 和 API 相关 70 项自动测试及 25 项浏览器测试通过，类型检查和生产构建通过。先以回归测试复现旧行为，再修复；独立审查未发现 P1/P2。本轮未重跑两仓库完整测试套件，也未部署 worker。
