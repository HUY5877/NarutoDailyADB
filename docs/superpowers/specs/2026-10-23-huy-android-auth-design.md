# HUY 抓号器与上号器

> 历史初版设计。后续已改为两端通过测试网站上传和读取授权，初版“上号器无需联网”等描述不再代表当前实现。当前使用与构建方式见 [工具说明](../../../tools/huy_android/README.md)，云端变更见 [云端授权方案](../plans/2026-10-23-huy-cloud-authorization.md)。

## 目标与授权范围

为用户本人在雷电 3 号实例使用的火影忍者 QQ 账号制作两个独立 Android 应用。用户已明确选择自有安卓组件方案，要求不使用 Frida、不改游戏内存、不运行他人提供的 APK。继续在当前工作区实现，不修改原 Python 登录服务、`.env` 或其他模拟器实例。

## 实现选择

抓号器使用系统 WebView 打开 QQ 官方授权页面，在本应用内接收完整授权回调。上号器由游戏原有 QQ SDK 启动，通过 Android Activity 结果返回授权 JSON。相比已试验过的 Frida 路径，这条路径只使用普通应用权限。纯 Python 无法充当由游戏启动的 Android Activity；本次用户明确要求制作专用应用。

原生 Java，无第三方运行库，minSdk/targetSdk 28。HUY 抓号器包名 `com.huy.authcapture`；HUY 上号器包名 `com.tencent.tim`，提供 `com.tencent.open.agent.AgentActivity` 以兼容已分析的游戏入口。界面与应用名称明确显示 HUY，不冒充正版 TIM。若已安装其他签名的 TIM，安装过程停止，不能覆盖。

## 授权数据

固定游戏包名 `com.tencent.KiHan`，固定 QQ app_id `1104307008`。回调必须来自 `auth://tauth.qq.com/`，校验 ret、access_token、openid、expires_in；保留 pay_token、pf、pfkey 等真实字段。记录捕获时间并计算到期时间，导入不得重置有效期。返回游戏前再次校验并计算剩余有效秒数。

应用私有目录保存凭据，禁用备份与调试，不把凭据写入日志、剪贴板、截图文字或代码。抓号器仅用 HTTPS 请求 QQ 官方页面；上号器无需联网权限。两个应用使用同一 HUY 签名，通过 signature 权限保护的 ContentProvider 读取授权。用户也可通过 Android 文件选择器主动导入以前保存的完整授权 JSON。清除功能删除私有凭据。

AgentActivity 只接受游戏发起的结果请求，验证调用包名与请求 appid。应用主页提供授权状态和打开游戏入口；没有授权时提示先扫码。用户在 AgentActivity 明确点击确认登录后才返回凭据。

## 验收

纯 Java 测试校验乱序字段、重复字段、错误 host/appid、失败 ret、缺字段、过期、有效期溢出及扩展字段保留。构建并验证两个 APK 的签名/权限。只在 emulator-5560 安装自有 APK，测试签名保护、导入、游戏原生结果回调与扫码。需要本人扫码时等待用户操作。实际未跑通的步骤明确标注，不以编译成功代替登录成功。

## 2026-10-23 界面修订

用户明确要求应用名不要添加 HUY 前缀，仅保留小标识，并要求去掉模板化的界面。当前展示名称为“抓号器”和“上号器”；使用浅色安卓工具栏、紧凑状态区、普通按钮，HUY 仅位于工具栏右侧。删除“PRIVATE TOOLS”“专属于 HUY”等宣传文字。此修订仅影响界面和展示名称，包名、签名及授权处理保持原方案。
