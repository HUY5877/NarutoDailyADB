# QQ 群提醒绑定

用户已在调研后选择 QQ 官方机器人 Webhook 并要求开始开发。实现网站账号到群成员的绑定及现有二维码、结束通知中的真正 @。

用户最新补充要求：先在两项目的 `test` 分支开发，不修改或推送 `main`。网站使用 `C:/Users/1/Desktop/NarutoLoginClient`；worker 使用已有 `test` 工作区 `C:/Users/1/.codex/worktrees/test-main-review/NarutoDailyADB`。

## 方案

复用官方机器人；QQ 互联登录本身不能提供群成员绑定，NapCat 另有部署、账号和许可成本，故不引入。网站项目为相邻的 NarutoLoginClient；Windows 执行端为本项目。两端同环境共用 PostgreSQL，网站接收 HTTPS 回调，worker 保持向官方 API 发送通知。

## 接口与存储

- 网站配置使用现有普通变量 QQ_BOT_APP_ID、QQ_BOT_APP_SECRET、QQ_BOT_GROUP_OPENID，三者同配或全空，经当前 Cloudflare namespace 读取。
- `POST /api/qq/events`：公开签名鉴权回调，先于普通 JSON/会话中间件注册；op=13 按官方 Ed25519 挑战响应；op=0 先验证 AppID、原始请求体签名及时间，再处理 GROUP_AT_MESSAGE_CREATE。限制请求大小，校验指定群与成员标识，忽略其他消息。正常确认使用 op=12，持久化失败不确认成功。
- 登录接口 `GET /api/qq-binding` 返回 `{enabled, binding, challenge}`；binding 为 `{boundAt}` 或 null；challenge 为 `{id, expiresAt, received, senderName}` 或 null，senderName 可为空，不向浏览器返回成员 openid。
- `POST /api/qq-binding/challenge` 返回 `{id, code, expiresAt}`，生成 8 位随机一次性码和独立 UUID，5 分钟有效；创建新码废弃旧码，网站提示 `@机器人 绑定 CODE`。
- `POST /api/qq-binding/confirm` 请求体 `{challengeId}` 必须与当前用户所见挑战 UUID 相同，仅确认其未过期的候选身份，防止旧页面确认另一页面后来创建的候选；拒绝未收到群事件、过期、成员已被另一网站账号绑定等情况；成功保存绑定并删除挑战。
- `DELETE /api/qq-binding`：解绑并取消当前挑战。
- PostgreSQL `naruto_login.qq_bindings(user_id uuid, bot_app_id text, group_openid text, member_openid text, bound_at timestamptz)`；主键 `(user_id, bot_app_id, group_openid)`，唯一键 `(bot_app_id, group_openid, member_openid)`，用户删除级联。worker 以三元组读取，不跨机器人或群复用。
- 挑战表独立，码只存哈希，challenge_id 保存本轮 UUID；指定用户/机器人/群最多一份当前挑战。收到带码事件时原子设置首个候选；重复事件不改变候选。确认及解绑使用事务避免竞争。保留当前绑定直到新绑定确认。

## 用户界面

登录后提供 QQ 群提醒入口，显示未配置、未绑定、等待群验证、等待网页确认、已绑定状态。支持生成/重新生成码、复制命令、到期提示、轮询状态、确认和解绑；切换用户或关闭窗口取消旧请求，避免旧账号结果串入。沿用当前 React 组件样式与手机布局。

## 执行端

只读查询绑定，通知使用当前配置的 bot AppID/group openid，不从外部输入选择通知群。已绑定发送 `msg_type=2`、`markdown.content` 中 `<qqbot-at-user id="..." />`，只转义正文而保留机器人标签；未绑定沿用现有用户名纯文本。通知失败不影响业务任务。任务缓存网站用户 ID，在结束时重新查询绑定以尊重解绑，不将绑定结果永久锁定于任务启动时；独立会话到期后仍可凭缓存的可信 user ID 查询。

## 验证与范围

先写能失败的测试，再实现；覆盖官方挑战签名向量、无效签名/时间/AppID/群、挑战到期/重放/竞争、登录鉴权/CSRF、成员唯一性、解绑、环境隔离、worker @ 与安全转义及兼容通知。运行两仓库相关和完整测试、网站类型检查和构建，最后独立代码审查。本地联调使用固定密钥/内存数据库，不向真实群发消息；上线回调需公网 HTTPS。
