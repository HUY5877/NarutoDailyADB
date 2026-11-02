# 统一任务状态与刷新恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 日常和周胜从排队、扫码到执行结束只使用 runs 状态；刷新恢复异常和活动任务，已成功的旧任务显示待开始，杜绝重复申请。

**Architecture:** 网站提交时创建完整 runs JSON，worker 原子更新同一条记录，进度报告只合并受控字段。用户 active 索引仅用于定位 run，并与 run 同寿命；扫码业务期限与 Redis 留存期限分开。保留现有 HTTP 路径和 PostgreSQL 历史表，二者不再作为扫码和执行的两份独立事实。

**Tech Stack:** Python asyncio/Redis Lua，TypeScript Express/Redis/PostgreSQL，React。

**Spec:** 用户本轮三条要求及本文件约定。用户明确要求直接开始，两端同时修改。

## Global Constraints

- 工作端复用隔离工作区 codex/remove-obsolete-docs，基于 origin/main b74542c；网站按 AGENTS.md 在 main 修改，基于 34f1c47。
- 所有提交说明中文；不修改密钥、不操作真实任务、不重启运行中的 worker。
- canonical status：queued、starting_emulator、opening_game、preparing_qr、waiting_scan、finishing_login、running_daily、completed、failed、interrupted、expired。
- runs 包含 id/taskId、userId/tokenHash、createdAt/expiresAt、userOptions/loginType、simulation，以及 workerId/attemptId、扫码期限及刷新控制字段、执行进度时间戳。
- runs 和 active 索引保留至少 604800 秒。扫码期限仍为领取后 600 秒，授权后不得因扫码期限过期释放占用。
- public Task 可投影 waiting_scan/finishing_login/running_daily 为 qr_ready 并携带授权标记；canonical 状态必须通过 runStatus 暴露，禁止再次据旧扫码日期把执行中判成 expired。
- 不再创建 sessions key；允许兼容已有旧格式 runs，但不能覆盖其活动状态或泄露凭证。
- /api/task-runs/latest 直接返回 {task,run}，run 是完整公开投影；扫码阶段仍可通过既有登录申请接口获取二维码控制字段。
- 首次恢复失败保持禁用并可重试；只有首次恢复就为 completed 才隐藏旧结果，本页实时 completed 仍显示。

## Review Focus

- 原子检查占用，两个浏览器与过期数据库预约都不能绕过活动 run。
- 进度合并必须保留身份、扫码和刷新字段，旧 worker/attempt 和晚到更新不能覆盖新任务或中断。
- 排队和扫码逻辑期限必须独立于留存；过期前授权不被十分钟限制截断。
- 完成、失败、中断和过期释放申请能力；失联/缺失状态不能默认为成功或无任务。
- 老数据、首次加载失败、旧响应晚到和实时完成必须按用户规则显示。

### Task 1: Python worker 单记录状态

- [x] 先新增失败测试：同一 runs 保留网站字段，扫码授权后超过600秒继续运行、拒绝迟到写和中断覆盖。
- [x] 修改 queue.py/run_logs.py/runtime.py 的读写为单 runs，保留原子身份/轮次校验、刷新、重排和失败收尾。
- [x] 完整 Python 测试 4538 项通过，1 条既有 AnyIO 弃用警告；兼容与升级限制已记录。

### Task 2: 网站后端统一状态与申请保护

- [x] 先新增失败测试：创建即 queued run、扫码后不按日期过期、原子拦截 running、完整 latest 投影。
- [x] 修改 queue.ts/login-events.ts/task-runs.ts，保留历史和接口兼容，统一从 runs 获取事实。
- [x] 新申请必须先找活动 run；数据库过期不会覆盖活动 run。缺少 active 索引时检查持久化历史关联 run。
- [x] 完整网站测试 408 项通过；使用单文件并发避免 Windows 端口测试在负载高时超时，生产构建通过。

### Task 3: 前端刷新恢复

- [x] 先写失败测试，覆盖未恢复前禁用、扫码恢复不创建、失败/中断保留、成功刷新隐藏、本页完成保留。
- [x] 修改 frontend 的 tracker/hooks/App，首次加载直接读取完整状态，明确恢复成功和失败；按原始 terminal 状态决定是否隐藏。
- [x] 运行单测与浏览器集成，刷新恢复浏览器测试 21 项通过，日常和周胜慢请求窗口均禁止重新申请。

### Task 4: 跨端核验与交付

- [x] 独立审查单 key schema、原子生命周期和并发；隔离 Redis 7.4.2 的真实两端 Lua/HTTP 测试 14 组通过，不连接生产。
- [x] 检查 diff 和所有失败项，网站本地中文提交 95cf738，工作端沿用本分支交付合并请求。
- [x] 两端均未上线，升级顺序已写入两端 README；不触碰正在运行的任务。
- [ ] 网站 origin/main 推送与生产切换：等待确认所有旧 worker 结束并同步更新。

## 已处理的恢复与发布边界

- 活动用户索引丢失时查保存期内的历史，并在入队 Lua 再次检查；直接恢复未投递申请也不能绕过检查。
- 未投递意图沿用原编号恢复；明确失败但没有 run 的旧预约只读显示失败。已投递却无法确认状态时保持禁用，不把数据库自动 expired 当执行结束。
- 已超过七天且 run 已清理的旧任务恢复为待开始，避免永久禁用。
- 网站本地提交为 95cf738；推送 main 会自动触发生产发布。两端新旧协议不能混跑，须先确认全部旧任务及队列结束，再更新 worker，最后发布网站。
- 未重启真实 worker、未提交真实申请、未改变生产 Redis 数据。

Ruling: 网站先保留 main 本地提交，暂不推送。已读取 Vercel 项目配置，main 启用 Git 自动生产发布；新旧 worker 协议不能混跑。先完成代码与验证，确认同步升级后再推送，避免在旧 worker 仍接单时发布新格式。
