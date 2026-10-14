# NarutoDailyADB

基于 Windows 和雷电模拟器 9 的《火影忍者》手游 ADB 自动化项目，支持日常任务、秘境探险和 QQ 扫码登录。

需要 Python 3.11+。模拟器必须设置为 **1920×1080 横屏、DPI 240**，并开启 ADB 本地连接。

新电脑可以让 AI 读取根目录 [agent.md](agent.md)，按指南完成安装、配置和预检。

## 安装

clone 后在项目根目录执行。已有虚拟环境或配置文件会保留。

```powershell
if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    python -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install -e ".[test,login-service]"
if (-not (Test-Path .\.env)) {
    Copy-Item .\.env.example .\.env
}
```

## 配置

| 位置 | 需要填写的内容 |
| --- | --- |
| `config.example.json` | 服务共用的完整日常任务配置；保留现有任务和资源引用，屏幕尺寸保持 1920×1080，无需复制本地任务配置 |
| 根目录 `.env` | `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_KV_NAMESPACE_ID`，以及雷电控制台路径和实例池等机器参数 |
| Cloudflare KV | 分别保存纯文本 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`、`REDIS_URL` |

Cloudflare Token 需要目标账户的 **Workers KV Storage → Read** 权限。配置在启动时读取，修改后需重启；DeepSeek、Redis 和 OSS 不从本地 `.env` 回退。

扫码服务还需配置 OSS、雷电控制台路径和实例池，见[扫码服务说明](naruto_daily/login_service/README.md)。服务按分配到的实例覆盖任务配置中的 ADB 路径和 `serial`。

## 启动

完成配置后，双击唯一启动入口 [启动扫码服务.cmd](启动扫码服务.cmd)。它运行常驻 Redis worker，生成并上传二维码，扫码后执行日常；按 `Ctrl+C` 停止。服务默认是 `mock`，真实运行需设置 `EMULATOR_DRIVER=ldplayer`。

奖励列表使用本地 OpenCV 识别卡片内部起点，每次左拖 450 像素、持续 600 毫秒。前后截图确认实际移动后才计入成功次数，默认最多成功滑动 20 次；未移动会换起点重试，不占用这 20 次。日志分别记录尝试次数、实际位移和成功次数。无法确认位移或多个内部起点都不响应时暂停滑动，日常服务继续在原任务总时限内观察。

奖励入口仍由模型选择候选，点击前用同一帧中的“奖励”文字模板（普通和红点两种样式）复核所选区域；模型选中商城等其他入口时取消点击并重新识别。点击后最多再观察 10 秒确认奖励任务卡片布局，未确认则停止本次任务，避免在错误页面持续查找卡片。

被奖励入口复核拒绝的最新一次识别会保存在 `.data/diagnostics/main-screen/rejected-rewards.zip`，包含原图、实际发送的候选拼图、提示词和原始模型结果。证据包每次覆盖，不包含 API 密钥，且不进入 Git；日志会记录候选编号、复核分数和保存位置。

启动前可查看实例池配置和占用，不执行游戏任务或模型请求：

```powershell
.\.venv\Scripts\python.exe -m naruto_daily.login_service.pool_admin list
```

该命令仍需读取 Cloudflare KV，并要求服务配置完整。每个实例的分辨率和 DPI 要单独核对；首次资源下载、回归活动和额外引导需先手动完成。

详细安装与验收见 [agent.md](agent.md)，扫码实例池和服务参数见[扫码服务说明](naruto_daily/login_service/README.md)。请仅用于个人研究和界面自动化测试，并了解游戏规则与账号风险。
