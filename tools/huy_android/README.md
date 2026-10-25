# 抓号器 / 上号器

面向雷电 Android 9 和火影忍者 QQ 登录入口。抓号器为 1.3.0（versionCode 7），上号器为 4.0（versionCode 9）。QQ 安卓和 QQ iOS 网站任务使用同一套安卓工具；微信流程不使用它们。

## 使用

1. 在网站发起 QQ 扫码任务。已安装两款配套应用时，脚本把网站用户编号和本轮任务票据交给抓号器，打开 QQ 网页二维码。抓号器无需网站账号登录。
2. 网页显示当前二维码。用户扫码并确认后，抓号器沿用 QQ 必要授权校验，将原始完整授权发送到本轮网站接口，服务器保存到对应用户，备注固定为“自己账号”。
3. 脚本从当前任务对应的数据库交付记录读取 URL，等待游戏 QQ 通道打开上号器，再注入完整 URL、读取并确认。上号器只做本地解析，不调用腾讯 `/me`、云端解析接口或凭据下载接口。
4. 普通手动 URL 登录仍可用：打开游戏，选择 QQ 登录，在上号器的游戏确认页粘贴完整 `callback_url`，读取、核对 OpenID 尾号后确认。
5. 上号器的“账号历史”需要登录网站账号，只展示本人的备注、尾号和有效期。历史记录不能选择登录，也不下载授权。网站服务接受 `test` 或 `prod` 环境。

抓号器主页提示“在网页发起任务后自动扫码”。自动任务中的本机刷新按钮仅提示“请在网页刷新二维码”；网页刷新创建新票据，再由脚本启动新轮。抓号器不使用裸用户编号授权远程写入，不包含数据库密码、网站密码或服务端密钥。

URL 必须是完整原始 `auth://tauth.qq.com/` 成功回调，包含 `access_token`、`openid`、`expires_in` 和原有扩展字段。不会补出 openid、pay_token 或真实到期时间。没有原始授权时间时仅表示本地格式校验通过，最终由游戏验证。

## 本地自动化协议

两个 ContentProvider 只接受 ADB shell UID 2000、root UID 0 或应用自身 UID；普通应用调用被拒绝。均不授予 URI 权限。`call` 返回 Bundle 的 `result` 字符串，其内容为无 padding 的 base64url UTF-8 JSON。参数错误只返回固定错误码，不回显输入。

| 应用 | authority | 方法 |
| --- | --- | --- |
| 抓号器 | `com.huy.authcapture.automation` | `capabilities`、`begin`、`refresh`、`status`、`cancel` |
| 上号器 | `com.tencent.tim.huyautomation` | `capabilities`、`inject`、`status`、`confirm`、`clear` / `cancel` |

`capabilities` 不带参数，返回 `{"protocol":1,"kind":"capture"}` 或 `login`。可用 `adb shell content call --uri content://<authority> --method capabilities` 检查支持性。

抓号器 `begin` / `refresh` 的 `--arg` 是下列 8 字段 JSON 的 base64url 编码：

```json
{
  "userId": "网站用户 UUIDv4",
  "taskId": "网站任务 UUIDv4",
  "attemptId": "32 位小写十六进制",
  "captureId": "本轮 UUIDv4",
  "captureToken": "本轮 64 位小写十六进制票据",
  "apiBase": "https://网站根地址",
  "expiresAtEpochMs": 0,
  "loginType": "android_qq 或 ios_qq"
}
```

示例中的过期时间和说明字符串是占位内容，不可直接使用。UUID 必须是小写规范格式，过期时间由服务器给定且必须在未来。`apiBase` 仅允许 HTTPS 网站根地址，不允许用户名、密码、查询参数、片段或自定义端口。

`status --arg <captureId>` 返回 `{captureId,state,qrRevision,errorCode?}`。实际状态为 `preparing`、`qr_ready`、`uploading`、`uploaded`、`error`。`cancel --arg <captureId>` 返回幂等的 `cancelled` 确认，只清除匹配当前轮次；过时轮次的取消不会影响新轮。

二维码通过 `adb exec-out content read --uri content://com.huy.authcapture.automation/qr/<captureId>` 读取 PNG，最大 700000 字节。只导出当前 QQ 页面中真实二维码图片的裁剪区域，不导出网页全文或全屏。图片保留到当前轮取消、刷新或过期，允许扫码刚发生时仍完成本轮图片读取；新轮会立即清除旧轮图片。

抓号器保留 `FLAG_SECURE`。二维码来自受信任 QQ 页面上的二维码图片 DOM 边界，再由本应用 WebView 绘制裁剪；系统截图不会替代此出口。旧 WebView 回调、QQ 核验完成、上传发送前与响应后、异步二维码结果都会检查当前完整上下文。上传仅为 `POST /api/qq-captures`，正文包含 `userId/taskId/attemptId/captureId/captureToken/authorization`，无 Cookie；服务器返回匹配 `captureId` 才确认成功。

上号器 `inject` 的 `--arg` 为 `{taskId,captureId,callbackUrl}` 的 base64url 编码。仅当由 `com.tencent.KiHan` 发起且应用编号为 `1104307008` 的游戏确认页正在前台时接收。注入后进入本地 URL 模式、读取授权，状态为 `ready`；不会自动确认。`confirm --arg <captureId>` 再次校验请求与轮次后返回游戏，状态为 `submitted`。`status` 不返回 URL。输入编辑、模式切换、暂停或停止页面均使旧注入失效，凭据不跨页面保存。

便于可视调试的资源 ID：抓号器 `capture_status`、`capture_refresh`、`capture_qr`；上号器 `login_callback_url`、`login_read_callback`、`login_confirm`、`login_direct_mode`、`login_cloud_mode`、`login_status`。

## 数据、兼容与验证

- worker 在任务完成、失败、超时或停止后不再清空交付记录的 URL，也不调用两款应用的 `cancel` / `clear`。已上传的账号授权保持保存；新任务、二维码刷新和原有到期校验继续按各自轮次处理。
- 抓号、上号、应用补装和授权交接的实现诊断只保留在后台，不进入网站用户日志、进度提示或失败详情。网站读取历史日志时也执行过滤，正常扫码状态与日常进度继续展示。
- 自动任务票据、回调和二维码仅在本轮内存中；自动模式不写入旧版本授权缓存。应用禁止备份，凭据不写日志、错误提示或剪贴板。
- QQ WebView 仍限制腾讯资源域 HTTPS，关闭调试，每轮清理 Cookie/WebStorage，捕获后保留既有 QQ `/oauth2.0/me` 身份核验。上号器本地 URL 路径不联网。
- 使用 `com.tencent.tim` 包名兼容当前游戏 QQ SDK，不能与正版 TIM 同时安装。新版本保留旧签名才能覆盖原安装；构建不会操作设备或自动卸载应用。
- 纯 Java 测试覆盖严格回调解析、生命周期失效、任务替换/过期/旧轮取消、一次性确认、二维码边界和状态到文件读取的竞态。编译目标与最低版本均为 API 28。
- 编译通过不代表真实扫码、服务器上传或游戏登录成功。设备二维码导出、刷新和安装验证应单独记录；不要用真实令牌做自动测试。

## 源码和构建

`apps/capture` 为抓号器，`apps/login` 为上号器，`common` 为授权解析、任务状态、私有存储及 API 客户端。网站服务在同级 NarutoLoginClient 仓库，脚本与数据库交付逻辑位于本仓库 `naruto_daily/login_service`。

本机便携工具链来自微软 JDK 和 Google Android SDK，不需要安装 Android Studio。默认使用系统临时目录下的 `huy-android-toolchain-20261023`，可用参数或环境变量 `HUY_ANDROID_TOOLCHAIN` 指定其他位置。

另一台电脑需要先准备 Python 3.10 或以上、JDK 17、Android SDK Build Tools 35.0.0 和 Android API 28 平台。工具链不在 Git 中，构建脚本不会自动下载安装。可以用 Android Studio 的 SDK Manager 安装指定版本，或按 [Google 命令行工具说明](https://developer.android.com/tools/sdkmanager) 准备 `sdkmanager`，安装 `"build-tools;35.0.0"` 和 `"platforms;android-28"`。

使用标准 SDK 目录时，在仓库根目录执行下面的命令，并把三个示例路径换成实际位置。`--jdk-home` 应包含 `bin/javac.exe`，`--build-tools` 应包含 `aapt2.exe`、`zipalign.exe` 和 `lib/`，`--android-jar` 指向平台中的文件：

```powershell
python tools/huy_android/build_apks.py --test `
  --jdk-home "C:\Tools\jdk-17" `
  --build-tools "C:\Android\Sdk\build-tools\35.0.0" `
  --android-jar "C:\Android\Sdk\platforms\android-28\android.jar"
```

三个路径均显式指定后，不依赖本机临时工具链目录。只运行测试时，将 `--test` 换成 `--test-only`。如果沿用本机便携目录，则可以使用以下简写：

```powershell
python tools/huy_android/build_apks.py --test
python tools/huy_android/build_apks.py --test-only
python tools/huy_android/build_apks.py --test --app login
```

输出在 `artifacts/huy-android/`。`build-report.json` 记录 APK 摘要和签名验证结果。签名私钥和随机密码在 `.secrets/huy-android-signing/`，只允许当前 Windows 用户和 SYSTEM 访问。请保留这套签名材料，否则后续构建无法直接更新已安装应用，也不能读取旧签名抓号器的授权。

本机重新生成的签名不能直接覆盖其他签名的现有安装。安装前应核对原证书与本次 APK 证书；缺少原签名时保留构建产物，不自动卸载现有应用，以免丢失私有暂存和网站会话。

测试只使用合成凭据，不使用个人账号数据。编译和自动测试通过不代表扫码与游戏登录已经验证，云端版实际验证记录见输出目录中的 `cloud-verification.md`。

协议依据：[Android Activity 结果](https://developer.android.com/guide/components/activities/result)、[QQ Implicit Grant](https://wiki.connect.qq.com/使用implicit_grant方式获取access_token)。当前游戏的具体 `key_response` 与 AgentActivity 约定来自本机提供 APK 的静态检查。
