# QQ 群提醒绑定验证记录

## 分支与范围

按用户最新要求，两仓库均只修改 `test`。网站路径为 `C:/Users/1/Desktop/NarutoLoginClient`；执行端路径为 `C:/Users/1/.codex/worktrees/test-main-review/NarutoDailyADB`。两个已有 `main` 工作区已恢复干净，没有将功能合入正式分支。

实现范围：官方 Webhook 地址验证、原始请求体 Ed25519 验签、一次性绑定码、群候选身份、带挑战版本的网页确认、解绑，以及执行端的 Markdown @。没有读取真实密钥或向真实 QQ 群发消息。

## 通过的检查

- 网站 `npm test`：312 项通过，0 失败。
- 网站 `npm run build`：类型检查、前端和服务端构建通过；前端存在既有单包大于 500 kB 提示。
- 新绑定界面 `node --test tests/frontend-qq-binding.integration.mjs`：8 项通过、0 跳过；包含桌面、390px 手机、过期、登录状态丢失、换账号旧响应隔离、关闭取消、确认成功后状态读取失败、挑战版本变化等。
- 执行端相关 `pytest`：130 项通过，覆盖现有通知及新增 19 项绑定用例。
- 两仓库 `git diff --check` 通过。
- 独立代码审查：需求符合性、代码质量均通过，未发现需修复的重要问题；多连接锁等待主要通过 PostgreSQL 事务语义与代码审查确认，PGlite 测试连接采用串行化。

后端测试使用 PGlite 执行真实 SQL，HTTP 请求在本地测试服务器处理；浏览器测试拦截所有 API，不启动真实依赖。官方地址校验与请求签名均使用文档固定向量交叉验证。另覆盖公开挑战签名接口不能签署 JSON 事件、回调故障不确认成功、绑定码到期和重放、首候选不可被覆盖、成员唯一性回滚、跨群/机器人隔离、用户删除级联和旧页面确认冲突。

## 全量回归的已有失败

执行端完整 `pytest -q`：3097 项通过、3 项失败、1 个依赖弃用警告，耗时 316 秒。

1. `tests/test_daily_transport.py::test_production_sync_request_wall_timeout_closes_real_slow_connection[drip_body]`：真实慢连接测试要求耗时至少 0.6 秒，实测 0.594 秒。在当前代码单独重跑通过；干净 HEAD 副本中的该组测试也通过，属于计时边界波动。
2. `tests/test_navigation_agent.py::test_model_error_after_move_does_not_continue`。
3. `tests/test_navigation_agent.py::test_agent_sdk_does_not_add_retries_after_shared_limit`。

后两项预期的 SDK 异常类型与实际冒出的 `MultimodalRequestError` 不一致，在 `git archive HEAD` 生成的未修改副本中，用同一 Python 环境重跑同样失败（2 失败、2 通过）。相关导航代码与测试不在本次 diff 中。

网站已有浏览器套件：71 项计数、66 通过、5 失败，其中包含两个父测试，实际失败叶场景为以下三项：

- `frontend-admin.integration.mjs`：`daily table has a separate log column and every task state opens real logs only on click`，列头结果为空。
- `frontend-admin.integration.mjs`：`touch tasks all have log buttons and long logs scroll with a fixed close button in every orientation`，等待滚动事件时上下文已关闭。
- `frontend-main-task.integration.mjs`：`reload preserves an unconfirmed weekly intent and reuses its saved request ID`，原断言出现 `3 !== 2`。

前端实现者以 `git show HEAD:src/frontend/App.tsx` 覆盖 Vite 加载的 App，保持原测试不变，分别复现上述三个叶场景失败，排除了新增 QQ 入口导致的回归。本次保留这些已有问题，未扩大修改导航、管理员页面或主任务逻辑。

## 联调条件

测试站部署后，将 QQ 控制台回调设为 `https://测试站域名/api/qq/events`，订阅 `GROUP_AT_MESSAGE_CREATE`。网站和执行端均显式使用 `APP_ENV=test`，在对应 namespace 配置相同的 `QQ_BOT_APP_ID`、`QQ_BOT_APP_SECRET`、`QQ_BOT_GROUP_OPENID`，并连接同一测试数据库。先验证地址，再用测试账号完成群内绑定与网页确认，最后检查真实 @。

数据库表由网站正常业务请求自动初始化，`op=13` 地址验证不依赖数据库。群主须另外允许机器人主动发言。真实 QQ 接口接收、客户端 Markdown 渲染和主动消息权限尚未实测。
