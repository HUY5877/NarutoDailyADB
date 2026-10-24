# QQ 抓号正式环境准备与审查记录

日期：2026-10-24。worker 基线为远端 main `b81bc2736bea7cd738f83ac594df257de91fcf4e`，提交分支为 `codex/qq-capture-login`。网站后端为 NarutoLoginClient `29eb8519e1e96c328b7e300bd6f04b600f0e5a13`。

worker [合并请求 #23](https://github.com/HUY5877/NarutoDailyADB/pull/23) 已创建并保持草稿；实现和测试提交为 `aeb8925`，后续交付记录更新仅修改文档。

## 已完成的正式配置

- 明确选择 `prod` namespace，核对正式数据库、Redis 队列和 OSS 与测试环境分离。
- 正式数据库 `naruto_login` 原先没有两张新表。先在事务中演练并回滚，再执行后端导出的同一份 `qqCaptureSchema`，创建 `naruto_login.qq_authorizations`、`naruto_login.qq_capture_requests` 和历史查询索引。验证列、主键、唯一约束、外键和当前数据库连接的读写权限，两表初始均为 0 行。未变更既有业务记录。
- 建表 SQL SHA-256：`0b9dc0263c6421636c773d7bf092024f82f1a0f8579033aaedf01ce2406e83f4`。迁移采用事务、advisory lock 和有界锁等待，未用 `CREATE IF NOT EXISTS` 掩盖旧密文表差异。
- 正式 Cloudflare KV 仅新增 `QQ_CAPTURE_API_BASE=https://huy1.site`。回读并比较其余配置值，全部保持不变。既有同环境 `DATABASE_URL` 继续使用；测试 namespace 未修改。
- 配套网站后端经两轮独立审查后，从 `667382e` 快进发布到 main `29eb851`。Vercel Production 部署 `A3paFee2kUUw1bo1HqzZGwsStG2P` 已 Ready，绑定 `huy1.site`。未将测试环境预览部署提升为正式。
- 正式 HTTP 冒烟：`GET /api/health` 为 200 且 `environment=prod`；空体 `POST /api/qq-captures` 为 400 / `INVALID_QQ_CAPTURE`；未登录 `GET /api/qq-authorizations` 为 401 / `AUTH_REQUIRED`。未创建用户、任务或真实授权。

## 安装组合与回退

| 场景 | 结果 |
| --- | --- |
| QQ 安卓/iOS 日常、周胜，两款应用都没有 | 原游戏扫码流程，不初始化新增抓号接口或数据库 |
| 仅抓号器或仅上号器 | 同上 |
| 包查询失败、空输出或混合错误输出，任务仍有效 | 安全警告后原流程，不输出 ADB 原始内容 |
| 两款应用均在且支持协议 1 | 本轮抓号、任务绑定上传、数据库消费、游戏通道上号 |
| 双应用但配置、身份或 provider 不匹配 | 明确失败，不能把历史授权或其他任务授权当作本轮结果 |
| 实例归属、设备串号、运行状态、期限失效 | 终止，不通过回退绕过校验 |
| 微信安卓/iOS、仅取二维码模式 | 原流程，不执行辅助应用探测 |

首次独立审查发现可选包查询失败会提前中断旧流程；以失败测试复现后修复。新增 61 项回归，其中 48 组是通过真实 `LDPlayerManager` 和模拟控制台边界运行的 worker 组合矩阵，另外覆盖 7 种探测错误和 6 种硬失败边界。

## 验证与审查

- 后端：完整 323 项测试通过，TypeScript 类型检查、前端构建和服务端构建通过。两位独立审查员复核了事务、本人隔离、刷新/消费锁序、异常回滚、初始化失败重试以及微信和旧队列兼容。
- worker：包检测与 worker 相关回归 245 项通过；另一位审查员独立运行抓号、刷新、ADB 和 worker 测试 234 项通过。最终固定实现完整运行 `python -m pytest -q --junitxml=.data/qq-capture-verification/final-worker-pytest.xml`，3692 项通过、2 项失败，耗时 297.17 秒。失败仅为下述 main 已有的两项导航异常类型用例；两个计时用例本轮通过。
- Android：16 个纯 Java 测试入口通过。当前未修改 APK，也未重新安装或卸载模拟器应用。
- 全套首次运行 3629 通过、4 失败。两项导航测试期望 `AgentError` 而现有实现抛出 `MultimodalRequestError`；两项网络超时用例的 0.6 秒下界偶发得到 0.594 秒。四项均已在独立归档的原 main、同一 Python 环境下实际复现，相关测试和实现与本分支无差异。
- `git diff --check` 通过；审查与测试产物不含生产凭据，临时完整测试日志保留在本机忽略的 `.data/qq-capture-verification/`。

## 合并和上线边界

数据库、Cloudflare 和网站依赖已就绪；worker 只提出到 main 的合并请求，不在本次合并或启动正式 worker。更新执行机器前确认项目声明的依赖均已安装（包括 `psycopg[binary]`），使用正式 `APP_ENV=prod`，待在途任务结束后再切换并重启 worker。启用双应用路径的实例需两款兼容协议 1 的 APK；缺少任意一款继续旧流程。

自动测试不能替代真实用户扫码：仍需在受控实例验证 QQ 安卓/iOS 的扫码、网页刷新、授权回传、进入游戏以及原日常/周胜衔接。已有模拟器验证曾发现设备时钟偏差，实测前需核对设备时间。本文没有宣称真实账号端到端验收通过。

如需回退执行端，停止领取新任务，等待或取消在途任务后恢复旧 worker 版本。保留新增表及已有历史数据；不要通过删表或删除 Cloudflare 地址模拟开关，因为装有双应用的新 worker 会因此报配置错误。后端保持旧队列兼容，必要时在停止新抓号路径后再回退网站部署。
