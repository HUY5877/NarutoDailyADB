# 测试环境授权收集与手动选号

**目标：** 抓号器上传自己扫码得到的完整 QQ 授权；上号器列出当前网站用户保存的账号，由用户手动选择，再完成原有安卓回调。

**设计：** 两个 APK 通过 HTTPS 访问现有 NarutoLoginClient API。数据库密码和独立加密密钥仅在服务端。仅测试环境 `naruto_login_test` 启用新增接口与表，每次数据库初始化核对实际数据库名。

**技术：** 原生 Java / Android 28、Express / TypeScript、PostgreSQL、AES-256-GCM。延续简洁浅色 UI 和小 HUY 标识。

- [x] 后端：新增 `naruto_login.qq_authorizations`，完整授权加密保存，同一用户的同一 QQ 账号重复扫码更新。独立密钥从测试 Cloudflare KV 读取。
- [x] 接口：复用网站账号会话；上传、列表、下载、删除均校验所属用户。列表仅包含备注、账号末尾、有效期。过期授权不能下载；服务端验证官方 QQ 身份与原始有效期。
- [x] 共用客户端：独立保存 HTTPS 地址及网站会话；不保存密码；拒绝重定向；网站会话与扫码 WebView Cookie 隔离。
- [x] 抓号器：扫码前填写备注；成功回调先私有暂存，再上传；失败可重试，明确区分本机保存和云端已上传。
- [x] 上号器：加载账号列表，手动选择，保存记录 ID；游戏请求到达后重新下载选中授权；不自动换号，不回退旧本地授权。
- [x] 验证：接口身份隔离、有效期、篡改密文、正式环境关闭；真实测试库合成数据回归；全部 Java 校验、APK 签名与安装；仅 3 号模拟器检查界面。
- [x] 部署：只部署测试预览并验证 `/api/health` 为 test，避免向 main 推送而触发正式发布。

API 合同：`POST /api/qq-authorizations` 接收 `{label, authorization}`；`GET` 返回 `{items}`；`GET /:id` 返回 `{item,authorization}`；`DELETE /:id` 返回 204。metadata 为 `{id,label,accountHint,capturedAt,expiresAt,updatedAt,expired}`，日期 ISO。authorization 为原 canonical JSON。真实 token 不写入日志或报告。

完成边界：数据库和接口自动验证可使用合成账号；真实用户扫码与游戏成功进入需要模拟器端真实授权验证，未完成时必须单独说明。
