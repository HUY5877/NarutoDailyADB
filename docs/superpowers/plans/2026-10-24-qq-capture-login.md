# QQ 抓号器登录 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** QQ 双应用实例免网站登录扫码抓号、绑定本人云端账户，再通过 callback_url 自动上号。

**Architecture:** Python worker 编排模拟器及任务轮次；PostgreSQL 提供带所有权和期限的任务结果交接；Android provider 连接抓号与可信游戏授权页面。网站上传接口校验任务能力凭证并事务保存本人历史。

**Tech Stack:** Python/pytest/psycopg、TypeScript/Express/PostgreSQL/PGlite、Java/Android/ADB。

**Spec:** `docs/superpowers/specs/2026-10-24-qq-capture-login.md`

## Global Constraints

- 两仓库均从远端 main 创建并推送 `codex/qq-capture-login`；Git 提交说明使用中文。
- 微信、缺少任意辅助 App 的 QQ、后续日常与周胜沿用旧路径。
- 默认 label 为“自己账号”；自动流程不要求两款 App 登录网站。
- 不增加 token 补全或外部 QQ 校验接口；自动上号只传完整 URL。
- 不在日志、公开任务状态、异常原因暴露 URL、任务凭证、数据库连接串。

## Review Focus

- 刷新、上传、超时和消费同时发生，旧轮不得用于新轮；过期授权不得因注入刷新有效期。
- 旧 worker attempt 恢复运行，不得覆盖当前 attempt；上传不能跨用户绑定。
- 两应用版本不匹配、二维码尚未绘制、provider 失败应给明确可诊断错误。
- iOS QQ 选择与安卓 QQ 不同的既有游戏通道；微信不触发新增数据库或辅助应用逻辑。
- 数据上传成功不等于游戏登录成功；确认提交后继续原有游戏界面验证。

## Task 1: 云端上传与本人历史（backend agent）

**Files:** 后端仓库 `src/server/qq-captures.ts`、`src/server/app.ts`、`src/server/index.ts`、`api/index.ts`、相关 tests/config/schema。

**Interfaces:** 实现设计中的 POST /api/qq-captures、GET /api/qq-authorizations 与 qq_capture_requests 表；QQ 入队前初始化表，微信不触发新增初始化。

- [x] 写失败测试：免 cookie 凭证上传、跨 owner/attempt/capture 拒绝、过期拒绝、同内容幂等、冲突拒绝、本人历史隔离。
- [x] 实现严格本地 URL 解析、事务保存历史与任务结果、复合主键及兼容既有 plaintext 历史表。
- [x] 用 PGlite 内存测试真实 SQL 约束、锁定更新条件、刷新旧轮与消费幂等，不访问真实数据库。
- [x] 运行 server tests、typecheck、前后端构建，交付接口及 DDL 供 worker 对接。

## Task 2: Android 捕获与 URL 注入（android agent）

**Files:** `tools/huy_android`，从既有 test 工作树选择性引入源码与测试，不复制 secrets 或构建产物。

**Interfaces:** 实现设计中两套 provider、task upload body、本人历史元数据接口；capture 7/1.3.0，login 9/4.0。

- [x] 写失败测试：provider 参数编码/校验、身份及轮次隔离、无网站会话 URL 登录、错误/过期数据拒绝。
- [x] 实现免登录任务抓号、当前 QR 导出、网站刷新复用；异步页面/授权/上传回调均绑定 generation。
- [x] 实现 shell/root/self provider 权限、可信前台 AgentActivity 注入与确认，界面或生命周期改变清除旧上下文。
- [x] 保留网站登录后查看本人历史；固定 label“自己账号”。
- [x] 运行 Java 测试和 Android 构建，验证安装包版本、签名和 provider manifest。

## Task 3: Worker 编排（worker agent）

**Files:** `naruto_daily/login_service`、`tests/test_login*`、相关配置/README。

**Interfaces:** 消费任务表与 provider 协议；从 Redis ownership 读取可信 userId，私有传递 captureAttemptId/captureUserId/QQ 配置。

- [x] 写失败测试：安卓/iOS QQ 双应用切换、缺一回退、微信不探测、版本不符明确失败。
- [x] 实现参数化 SQL begin/poll/consume/cancel、复合主键隔离、原 deadline 与安全异常处理。
- [x] 实现二维码导出和原网站 refresh requestId/ACK；刷新优先消费且每轮更换凭证。
- [x] 实现正确游戏 QQ 通道、URL 注入确认、实际登录成功验证；后续 finish/daily/arena 调用保持原样。
- [x] 测试超时、取消、迟到上传、交接期间刷新、旧 attempt、敏感日志清理；运行相关 Python 测试。

## Task 4: 集成验证与交付（root）

- [x] 对照两仓库最终 DDL、HTTP body、provider 名称/参数、状态和版本，补跨组件契约测试。
- [x] 审阅登录完成证据、refresh ACK 时序、真实授权期限与隐私边界，修复有证据的问题。
- [x] 运行相关回归、全量后端测试/typecheck/build、Java 测试/APK 构建；模拟器仅做受控测试，不运行真实日常。
- [x] 更新部署配置与升级说明，明确两 App 同时升级、网站/脚本配置要求和未完成的真实扫码验证。
- [x] 使用中文提交到两仓库功能分支，并推送同名远端分支，验证远端提交一致。
