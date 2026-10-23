# 抓号器 / 上号器

面向雷电 Android 9 和当前火影忍者 QQ 登录入口的工具。扫码授权通过测试服务保存到数据库，上号器支持粘贴 token 或从账号列表手动选择。源码独立编写，使用 Android Activity 结果回调；不使用 Frida、root、游戏内存写入或第三方上号器 APK。

## 使用

1. 安装 `HUYCapture.apk` 和 `HUYLogin.apk`，分别进入网站账号设置，登录同一个测试网站账号。网站密码仅用于登录，不保存。
2. 打开 **抓号器**，填写账号备注，点击 **扫码并上传**，在应用自己的 QQ 网页中扫码确认。不需要打开游戏或给应用读取日志的权限。显示“云端已确认上传”才表示数据库确认成功。
3. 打开 **上号器**，把数据库中当前网站账号所属记录的 `access_token` 粘贴到输入框，点击 **使用 token**。允许首尾空白，需为 16 至 256 位十六进制字符，大小写保持原样。也可刷新账号列表，手动选中要登录的账号。多个账号不会自动选择最新一条。
4. 首次安装后彻底退出并重新打开游戏，让 QQ SDK 重新发现上号器。在游戏登录页选择 **QQ 登录**，检查选中账号，再点 **确认登录**。

上传失败时，抓号器保留私有暂存，恢复网络后可重试。清除本机暂存不会删除数据库记录。上号器每次从服务获取选中记录，不再自动读取旧的本地授权。QQ 提前撤销授权时仍需重新扫码；工具不会刷新或延长授权。

token 必须已由抓号器上传到当前网站账号。服务会读取原记录中的 openid、完整回调及真实有效期；不存在或属于其他用户、已过期、匹配多条记录时不会选中。匹配多条时可回到账号列表手动选择。成功后输入框立即清空，只保存选中记录编号；离开页面也会清空输入，切换网站账号或离开页面前发起的迟到响应不会改变选择。

默认服务地址为 `https://naruto-login-client-git-test-sust-acmer.vercel.app`。升级后仅将之前固定的 `https://naruto-login-client-fx63s9ajo-sust-acmer.vercel.app` 自动替换为该稳定测试地址，同时清除旧网站会话和选择，提示重新登录；用户自行设置的地址保持原值。两款应用都需要更新并分别登录。

应用名称为“抓号器”和“上号器”，界面右上角保留 HUY 小标识。上号器使用 `com.tencent.tim` 包名提供当前游戏会寻找的兼容组件。它不能与正版 TIM 同时安装；不要覆盖已有正版 TIM。QQ 已安装时，游戏可能优先启动 QQ。仅为当前已分析的游戏版本验证兼容性，游戏/QQ SDK 更新后可能需要调整。

## 数据与权限

- 两个应用通过 HTTPS 访问测试服务，拒绝重定向和明文 HTTP。抓号器独立打开 QQ 的 `xui.ptlogin2.qq.com/cgi-bin/xlogin` 网页入口，参数依据用户提供参考 APK 的实际流程，使用 `pf=openmobile_web`。旧版直接构造 `m_authorize`/`openmobile_android` 请求，在扫码后收到 `100044`。
- QQ 网页在本应用 WebView 内运行，允许 QQ 登录所需的第三方 Cookie；网络请求限制为腾讯资源域的 HTTPS，拒绝外部应用跳转。每轮刷新及退出清理 Cookie、WebStorage。只接收本轮 WebView 的 `auth://tauth.qq.com/` 回调，向 QQ `/oauth2.0/me` 核对应用编号和 openid，服务端上传时再次核对。
- 上号器只接受 `com.tencent.KiHan` 发起、应用编号为 `1104307008` 的 QQ 登录结果请求。
- 测试表 `naruto_login.qq_authorizations` 位于 `naruto_login_test` 数据库；同一用户同一 QQ 账号重复扫码更新原记录。数据库以明文列保存 `access_token`、`openid`、`callback_url`，便于直接复制 token；网站用户只能读写自己的记录。账号列表不返回 token。
- 旧密文使用原 `QQ_AUTHORIZATION_ENCRYPTION_KEY` 在事务中迁移，保留原编号、所属用户和有效期；密钥错误或记录损坏时整体回滚。新记录不再使用加密密钥，旧记录迁移完成前需保留原密钥。
- APK 不含数据库密码或服务端密钥。抓号器暂存与网站会话保存在应用私有目录，禁止备份。
- 不把 token 写入日志、URL、错误提示或剪贴板。手动输入框按密码显示，关闭自动纠错、输入状态保存和自动填充；扫码页和上号页禁止系统截图。
- 不能只保存 access_token：还需要 openid、真实有效期、捕获时间及原始完整回调中的扩展字段。
- 这两款应用不需要 root。模拟器是否已经开启 root 是独立设置。

抓号器 1.2.1、上号器 3.7 的 `versionCode` 均为 6。本次新增手动 token 选择及稳定测试地址迁移；构建和纯 Java 测试不代表已完成模拟器扫码、粘贴 token 或游戏登录实测。历史版本 1.2.0 在 3 号实例的独立网页、QQ 核验和账号列表验证记录见输出目录 `independent-qr-verification.md`。网页参数依赖 QQ 服务行为，服务端调整后仍需复验；不能只凭二维码出现判断成功。当前版本不包含游戏日志采集服务、READ_LOGS 权限、调试授权脚本或登录组件停用控制。

## 源码和构建

`apps/capture` 为抓号器，`apps/login` 为上号器，`common` 为授权解析、到期校验、私有存储和云 API 客户端。服务端实现位于同级 `NarutoLoginClient` 仓库。新增接口仅 `APP_ENV=test` 可用，并在建表前核对实际数据库名。

本机便携工具链来自微软 JDK 和 Google Android SDK，不需要安装 Android Studio。默认目录 `C:\Users\PC\AppData\Local\Temp\huy-android-toolchain-20261023`，可用参数或环境变量 `HUY_ANDROID_TOOLCHAIN` 指定其他位置。

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
```

输出在 `artifacts/huy-android/`。`build-report.json` 记录 APK 摘要和签名验证结果。签名私钥和随机密码在 `.secrets/huy-android-signing/`，只允许当前 Windows 用户和 SYSTEM 访问。请保留这套签名材料，否则后续构建无法直接更新已安装应用，也不能读取旧签名抓号器的授权。

本机重新生成的签名不能直接覆盖其他签名的现有安装。安装前应核对原证书与本次 APK 证书；缺少原签名时保留构建产物，不自动卸载现有应用，以免丢失私有暂存和网站会话。

测试只使用合成凭据，不使用个人账号数据。编译和自动测试通过不代表扫码与游戏登录已经验证，云端版实际验证记录见输出目录中的 `cloud-verification.md`。

协议依据：[Android Activity 结果](https://developer.android.com/guide/components/activities/result)、[QQ Implicit Grant](https://wiki.connect.qq.com/使用implicit_grant方式获取access_token)。当前游戏的具体 `key_response` 与 AgentActivity 约定来自本机提供 APK 的静态检查。
