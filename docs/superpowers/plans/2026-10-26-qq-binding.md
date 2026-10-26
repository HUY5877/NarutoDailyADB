# QQ 群提醒绑定实施计划

> **For agentic workers:** Use the approved QQ binding design; implement with test-driven development and independent final review.

**Goal:** 网站用户完成群内验证并确认后，现有任务通知能 @ 该用户。
**Architecture:** NarutoLoginClient 承担 Webhook、PostgreSQL 绑定和 React 入口；NarutoDailyADB 查询相同数据库并使用官方 Markdown 发送。
**Tech Stack:** Express/TypeScript/React/Node crypto/PostgreSQL；Python/psycopg/pytest。
**Spec:** ../specs/2026-10-26-qq-binding-design.md

## Global Constraints

- 中文 Git 提交；用户最新要求两项目均在 test 分支开发，不修改或推送 main。
- 只使用当前 APP_ENV namespace，不读取或输出密钥，不实际发群消息。
- 码有效期 5 分钟；绑定 scope 为 user_id + bot_app_id + group_openid。
- 回调对原始请求体验签，绑定接口需登录与现有 CSRF 防护。
- 现有未绑定用户名通知保持兼容，通知故障不影响任务。

## Review Focus

- 重放/重复事件不得更改已接收候选或复活失效码。
- 并发重新生成/确认/解绑不得跨用户串绑。
- worker 结束时重新查询绑定，尊重解绑与独立会话到期。
- 纯文本昵称不可变成 Markdown 链接或额外 @ 标签。
- serverless 入口与本地入口均注册服务，敏感 body 不写日志。

## Task 1: 网站绑定服务与回调

Files: NarutoLoginClient/src/server/{qq-binding*,app.ts,config.ts,cloudflare.ts,index.ts}, api/index.ts, tests/qq-binding*.test.ts。
Produces: spec 中五个 HTTP 端点及 PostgreSQL qq_bindings 表。

- [x] 写签名向量、事件鉴权、用户权限及真实 SQL 行为的失败测试。
- [x] 运行观察失败，然后实现最小服务、事务存储、回调及两种启动入口。
- [x] 运行新测试、npm test、npm run typecheck。

## Task 2: 网站绑定界面

Files: NarutoLoginClient/src/frontend/components/qq-binding-dialog.tsx、hooks/use-qq-binding.ts、App.tsx、对应行为测试。
Consumes: Task 1 HTTP contract; no member openid exposed to browser.

- [x] 为异步账号切换、到期、确认和错误编写行为测试并观察失败。
- [x] 实现入口、绑定命令复制、轮询/确认/解绑及请求取消。
- [x] 类型检查、构建、浏览器检查桌面/手机显示。

## Task 3: worker 绑定查询及 @ 通知

Files: naruto_daily/login_service/{qq_bindings.py,qq_notify.py,runtime.py,models.py}, tests/test_login_service_*。
Consumes: Task 1 qq_bindings schema.

- [x] 添加限定 bot/group 的只读绑定查询、真实 Markdown @、过期会话后查询与解绑的失败测试。
- [x] 实现可信 userId 缓存、发通知前实时绑定查询及安全 Markdown 构造。
- [x] 运行相关及完整 pytest；检查原有通知与失败隔离。

## Task 4: 集成验证与交付

- [x] 使用内存 PostgreSQL/模拟签名事件完成生成码→回调→确认→查询绑定→解绑的集成测试。
- [x] 更新两仓库部署说明，明确回调路径、订阅事件、Cloudflare 三项配置与群主动权限。
- [x] 独立审查改动，修复重要问题，重跑受影响验证。
- [x] 检查 diff，中文提交；按用户最新要求保留 test 分支，不修改或推送 main。报告回调地址与尚需控制台联调的部分。
