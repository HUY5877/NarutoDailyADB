# QQ 抓号器登录设计

用户授权：从两个仓库远端 main 建立 `codex/qq-capture-login`，开发后推送同名远端分支。只调整 QQ 登录阶段，保留微信、日常与周胜行为。

## 行为

- 安卓 QQ、iOS QQ：启动模拟器后检测 `com.huy.authcapture` 与 `com.tencent.tim`。两者齐全时使用抓号器；缺少任意一个时走原扫码流程。
- 抓号器无需登录网站。脚本把可信网站 userId 和本次任务凭证交给抓号器，抓号器展示二维码，脚本把二维码发给网站用户。扫码授权上传当前环境云端，账号默认名称为“自己账号”。
- 上号器无需网站登录即可接收完整 callback_url。脚本读取本次任务对应的数据库结果，在游戏拉起的可信授权页面填入 URL 并确认；仍需等待游戏出现原有登录完成证据。
- 上号器登录网站账号后可以查看自己的扫码历史。自动登录不依赖历史查询，不增加 QQ token 补全接口。
- 网站刷新二维码沿用现有 requestId/ACK 协议，换轮不延长原任务期限。自动抓号时本地刷新按钮提示从网页刷新，网页刷新调用与普通刷新相同的内部刷新流程。
- 2026-10-25 用户要求取消任务结束时的抓号清理：完成、失败、超时和停止都不清空交付记录 URL，不调用应用取消，也不由父进程补做数据库撤销。保留上传、消费、刷新换轮和原有到期校验。抓号相关实现诊断不得进入网站用户日志、进度提示或失败详情，历史日志同样过滤。

## 跨组件协议

数据库 `naruto_login.qq_capture_requests` 以 `(task_id, attempt_id)` 为主键，每个 attempt 只保留当前 capture_id；包含 owner_id、capture_token_hash、login_type、expires_at、status、callback_url、captured_at、authorization_expires_at、authorization_hash、created_at、updated_at。所有写入、轮询、消费匹配 owner/task/attempt/capture。上传与换轮通过同行事务锁串行化。旧 attempt 不得覆盖新 attempt。

抓号上传：`POST /api/qq-captures`，无需网站 cookie，但必须提交 `{userId, taskId, attemptId, captureId, captureToken, authorization:{schema_version:1,app_id:"1104307008",captured_at_epoch_ms,callback_url}}`。网站校验本次能力凭证、QQ 类型、截止时间及 URL，事务内保存本人历史并完成任务。重复同内容上传幂等，不同内容冲突。凭证是 32 字节随机值的小写十六进制字符串，数据库只保存 SHA-256；attemptId 为 32 位小写十六进制，captureId 为 UUID。

`GET /api/qq-authorizations` 需要网站登录，只返回本人历史元数据，兼容 prod/test 环境，不返回 URL 或 token。无新增 token resolve 或外部 QQ 校验调用。

抓号器 provider：`com.huy.authcapture.automation`；上号器 provider：`com.tencent.tim.huyautomation`。两者仅允许 shell/root/应用自身 UID。`capabilities` 返回 `{protocol:1,kind:"capture"|"login"}`。Bundle `result` 为无 padding 的 base64url JSON，命令 JSON 参数同样编码。

- capture `begin` / `refresh` 参数：`{userId,taskId,attemptId,captureId,captureToken,apiBase,expiresAtEpochMs,loginType}`。
- capture `status(captureId)` 返回 `{captureId,state,qrRevision,errorCode?}`，state 为 preparing/qr_ready/uploading/uploaded/error。
- capture PNG：`content://com.huy.authcapture.automation/qr/<captureId>`，仅返回当前轮二维码，最大 700000 字节；保留 FLAG_SECURE。
- capture `cancel(captureId)` 只清理匹配轮次。
- login `inject` 参数：`{taskId,captureId,callbackUrl}`，只允许当前可信游戏调用的 AgentActivity。
- login `status(captureId)` 返回 waiting/ready/submitted/error；`confirm(captureId)` 复用原确认流程；`cancel(captureId)` 清理匹配上下文。用户编辑、切换模式或 Activity 生命周期改变时使旧自动化上下文失效。

## 约束

可信 userId 来自已有 Redis worker ownership 校验，不相信队列传入任意 userId。新增 `QQ_CAPTURE_API_BASE` 为 HTTPS 网站根地址；只有 QQ 工作流私有传入 DATABASE_URL，不把身份、密钥、URL 写入公开任务视图或日志。双应用齐全但版本/配置不符时明确报错，不静默改变登录方式。实际授权有效期来自捕获时间和 URL；不得从注入时间延长有效期。测试不访问真实用户数据库，不执行真实用户日常或周胜。
