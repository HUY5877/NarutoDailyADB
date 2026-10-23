# 明文授权与 token 输入实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 数据库可直接复制 token，上号器可粘贴并选择对应授权。
**Architecture:** 服务端迁移到明文列并提供当前用户的 token 查询；安卓继续使用现有 Cookie 与按编号读取授权流程。
**Tech Stack:** TypeScript、PostgreSQL、Android Java、Python APK 构建脚本。
**Spec:** ../specs/2026-10-24-huy-token-input-design.md

## Global Constraints

只修改两个仓库的 test 分支，中文提交。授权归属与真实有效期不变，不把 token 放进 URL/日志，不删除用户记录。源代码不能包含真实凭据。

## Review Focus

- 旧密钥错误时整个迁移回滚，不留下半迁移状态。
- 过期旧授权仍能迁移到可复制列，API仍返回410。
- 多账号、相同token、不同用户时不选错记录。
- token输入请求进行时切换账号或离开页面，旧响应不得改变新会话。
- 新安装与迁移完成的数据库没有加密密钥仍能上传读取。

### Task 1: 服务端明文存储、迁移与 token 查询

**Files:** NarutoLoginClient/src/server/qq-authorizations.ts、qq-authorization-data.ts、app.ts、config.ts；tests/qq-authorizations.test.ts；docs/qq-authorizations-test.md。
**Interfaces:** POST /api/qq-authorizations/resolve 接受且只接受 {access_token:string}，返回 {item,authorization}；401未登录、400无效输入、404不属于本人/不存在、410过期、409重复匹配。与现有下载返回结构一致。

- [x] 先补服务与HTTP测试：无密钥明文写入、token精确匹配与归属限制、过期及重复匹配。
- [x] 运行专项测试，确认旧实现不满足新增行为。
- [x] 实现明文记录与迁移；新指纹使用 owner/app/openid 的 SHA256，迁移校验旧AAD/HMAC与时间后转为新指纹。
- [x] 用真实 PostgreSQL 兼容的本地测试验证迁移原子性、过期记录和重复初始化；保留原始时间。
- [x] 运行 npm test、npm run typecheck、npm run build，更新文档并提交。

### Task 2: 安卓手动 token 入口

**Files:** tools/huy_android/common/src/com/huy/auth/CloudClient.java、CloudRules.java 及 tests；apps/login/src/com/huy/authlogin/MainActivity.java；README.md；必要版本号。
**Interfaces:** CloudClient.resolveToken(String) 返回 CloudClient.Download，调用 Task 1 接口；验证返回token等于输入，记录ID与有效期有效。成功后只保存选择的record id。

- [x] 先写 token 输入合法性及服务返回校验测试，运行确认新增行为缺失。
- [x] 实现接口及输入UI，保留列表选择，输入禁用自动纠错/保存状态，成功清空输入。
- [x] 防止并发输入、账号切换及页面销毁后的旧响应选中错误账号。
- [x] 运行 build_apks.py --test，构建并检查签名与版本。
- [x] 更新使用文档并提交中文说明。

### Task 3: 联调与交付

- [x] 独立审查两个仓库差异，修复重要问题并重跑相关测试。
- [x] 校验数据库迁移与客户端协议一致，提交并推送对应test分支。
- [x] 在可用测试部署中执行迁移并验证，不输出真实token；交付新APK并核对可更新安装条件。

## 执行记录

- 2026-10-24：源码基线 Android d265bed，服务端 1c308d2。工作目录分别为现有 test-main-review 工作树与桌面 NarutoLoginClient。无未提交用户改动。

- 2026-10-24 完成：后端提交 75a2cc1，安卓功能提交 ea4fea7，均推送 test；独立任务审查及跨仓库最终审查通过。
- 验证：后端完整 298 项及 15 项授权专项通过，Android 10 组 214 项通过，APK 签名/对齐/版本通过。
- 真实测试库原 3 条记录迁移后逐字段一致，移除密文列，无密钥读取 3/3 通过。稳定测试地址 https://naruto-login-client-git-test-sust-acmer.vercel.app 已部署成功。
- 远端 HTTP 18 项检查通过（正常登录、resolve/get、归属隔离、过期、冲突、重复 JSON、CSRF、列表不含凭据）；创建的 2 个临时用户及 4 条合成授权已清理，原 3 条记录保持不变。
- 用户确认卸载重装后，两款 versionCode 6 APK 已安装到 emulator-5554：抓号器 1.2.1、上号器 3.7，启动界面检查通过。个人账号实际扫码/游戏登录待用户重新登录后使用，未宣称已验证。
- 新版 APK 保存于 Downloads/HUY-Android-2026-10-24-v6；签名材料在工作树 .secrets/huy-android-signing，并保留副本于 Desktop/NarutoDailyADB/.secrets/huy-android-signing。
