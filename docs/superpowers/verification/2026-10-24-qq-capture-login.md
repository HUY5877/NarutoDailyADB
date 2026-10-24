# QQ 自动抓号登录验证记录

功能分支：两个仓库的 `codex/qq-capture-login`，分别基于 NarutoDailyADB 的远端 main `b81bc27` 和 NarutoLoginClient 的远端 main `667382e`。

功能提交已推送：执行端和 Android 为 `4f727bd`，网站为 `29eb851`。没有合并到 main。

## 本地验证

- Python：最终固定源码运行 `pytest tests -q -k 'login_service or cloudflare_kv or environment'`，1206 项通过、2416 项非相关用例未选中。仅出现既有 Starlette/AnyIO 弃用提示。
- 网站：323 项测试通过，类型检查、前端与服务端构建通过。新增 PGlite 测试使用内存 PostgreSQL，覆盖上传事务、本人隔离、轮次、期限、幂等和冲突。
- 跨组件：从 Python 实现提取实际 SQL，在后端真实表结构及上传服务上执行 begin → upload → refresh → 旧轮拒绝 → 新轮 upload → consume → cancel → 幂等重试。没有连接真实业务数据库。
- 中断补充验证：PGlite 执行最终 Python 撤销 SQL，验证取消记录阻止迟到 begin、旧 attempt 与其他 owner 无法覆盖当前轮、消费后清除 URL 且保留幂等摘要。父进程强杀、未读消息和数据库故障清理的 6 项独立复核测试通过。
- Android：16 个纯 Java 测试入口全部通过；两款 APK 通过 API 28 编译、签名和对齐验证。二维码出口限制为 700000 字节，并验证上下界。

## 雷电验证

设备为 `emulator-5554`，Android 9。两款应用使用原签名覆盖安装，保留数据：抓号器 1.3.0 / code 7，上号器 4.0 / code 9。

- 两套 provider 的 `capabilities` 均返回协议 1 和正确应用类型。
- 合成任务由脚本唤起抓号器，两次 begin/refresh 都取得 287×288 的 PNG，OpenCV 能识别二维码；新轮启用后旧轮图片不可读。最终安装包重新验证后通过。
- 安卓 QQ、iOS QQ 两个真实游戏入口都能拉起可信的上号器 AgentActivity。合成 callback_url 注入后达到 `ready`，随后取消清空；未调用 confirm，没有尝试登录合成账号。
- 测试时发现模拟器时钟比电脑慢约 7 小时 50 分。使用电脑时间构造的未来 `auth_time` 被正确拒绝，改为设备时间后通过。没有修改设备时钟；实际扫码前应同步时间，避免真实授权时间校验或 HTTPS 校验受影响。

## 范围

本次没有用真实账号扫码，没有向真实云数据库上传授权，没有完成游戏真实授权确认，也没有执行日常或周胜。上线需部署网站与执行端同名分支，在相同环境配置 `QQ_CAPTURE_API_BASE` 和 `DATABASE_URL`，再验证一次真实扫码到进入游戏的全过程。

最终安装包 SHA-256：

- HUYCapture.apk：`ad4a5ae38a444c4f8e951ded106de5103f372b93c2a305e08b3fa44c21431b9b`
- HUYLogin.apk：`873bf309b94bc98e385c95f18694c515e9dedf16811874f2e33b83a5aaf43704`
