# HUY Android 授权工具实施计划

> 历史初版计划，保留当时的步骤和验证记录。当前实现已加入云端保存、账号列表和独立扫码，见 [工具说明](../../../tools/huy_android/README.md)、[云端授权方案](2026-10-23-huy-cloud-authorization.md) 和 [独立扫码修复](2026-10-23-huy-independent-qr-fix.md)。本计划中的“无需 Git 提交”是当时范围；用户已于 2026-10-24 明确要求提交并推送源码。

> **For agentic workers:** Use superpowers:subagent-driven-development or native execution; independent files may be developed in parallel under the current session's delegation instructions.

**Goal:** 提供 HUY 抓号器和上号器 APK，通过安卓回调完成用户本人账号登录。

**Architecture:** 两个原生 Android 应用，共用授权校验和私有存储代码，以签名权限通信。

**Tech Stack:** Java 8 语法，Android API 28，JDK 17，Android Build Tools 35，Python 构建脚本。

**Spec:** `docs/superpowers/specs/2026-10-23-huy-android-auth-design.md`

## Global Constraints

- 仅 emulator-5560；不修改原服务、`.env`、其他模拟器。
- 不使用 Frida、root、游戏内存写入或第三方 APK。
- 不输出凭据。签名材料位于忽略目录 `.secrets/huy-android-signing/`。
- 源码 `tools/huy_android/`；输出 `artifacts/huy-android/`。
- 同一签名签两个 APK。上号器通过游戏启动的 Activity 返回结果。
- 当前用户已授权制作与本机验证；不重复请求相同批准。实现选择见设计文档。无需 Git 提交。

## Review Focus

- 过期或重新导入的授权不得延长寿命。
- 非游戏调用者及非同签名应用不得取得授权。
- 取消扫码、失败回调不得覆盖已保存授权。
- 网络错误及异常信息不得泄露回调 URL。
- 安装前检查已有 TIM，不能覆盖其他应用。

## Tasks

1. [x] 官方便携工具链、Python 构建脚本；编译、签名并验证临时无业务 APK。
2. [x] `common/src/com/huy/auth/`：授权解析、JSON 适配、原子私有存储；`common/tests/` 使用合成数据验证异常与到期。
3. [x] `apps/login/`：HUY 状态页、SAF 导入、签名 Provider 读取、AgentActivity 调用者校验和结果返回。
4. [x] `apps/capture/`：HUY 状态页、官方 QQ WebView、完整回调接收、签名 Provider、清除与取消。
5. [ ] 集成构建、独立代码检查、仅实例 3 安装、真实上号与本人扫码验证。
6. [ ] 使用说明、APK 交付、记录验证结果与未验证项。

## 公共接口合同

`com.huy.auth.Authorization`：`fromJson(String,long)`、`fromCallback(String,long)`、`toJson()`、`responseJson(long)`、`expiresAtMs()`，解析失败抛出不含输入值的 `IllegalArgumentException`。

JSON 格式 `schema_version=1`、`app_id=1104307008`、`captured_at_epoch_ms`、`callback_url`。兼容以前保存的 `app_id`、`captured_at` ISO 时间、`callback_url` 格式。

`com.huy.auth.CredentialStore`：静态 `load(Context)`（不存在返回 null）、`save(Context,Authorization)`、`clear(Context)`；私有原子文件。

Provider URI `content://com.huy.authcapture.credentials/authorization`，query 返回单行 `authorization` 列，内容为 `Authorization.toJson()`；未保存返回空游标。仅签名权限 `com.huy.authcapture.permission.READ_AUTHORIZATION` 与包名 `com.tencent.tim` 可读取。只读 Provider，所有写入方法拒绝。

## 当前验证记录

2026-10-23：116 项合成断言/检查通过；两 APK 编译、D8、同签名和对齐检查通过。独立审查未发现阻止交付的问题；扫码刷新旧回调和网页存储清理两项已修复并复核。APK 已安装到 emulator-5560；抓号器首页已检查；普通 ADB shell 读取 Provider 被拒绝。等待本人扫码后验证私有存储、跨应用读取和游戏返回结果。
