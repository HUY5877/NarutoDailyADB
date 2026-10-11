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
if (-not (Test-Path .\config.local.json)) {
    Copy-Item .\config.example.json .\config.local.json
}
if (-not (Test-Path .\.env)) {
    Copy-Item .\.env.example .\.env
}
```

## 配置

| 位置 | 需要填写的内容 |
| --- | --- |
| `config.local.json` | 雷电自带 `adb.exe` 的实际路径；多开时指定设备 `serial`，屏幕尺寸保持 1920×1080 |
| 根目录 `.env` | `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`、`CLOUDFLARE_KV_NAMESPACE_ID`；保存账号登录还需 `LOGIN_ACCOUNT_NAME` |
| Cloudflare KV | 分别保存纯文本 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL`、`REDIS_URL` |

Cloudflare Token 需要目标账户的 **Workers KV Storage → Read** 权限。配置在启动时读取，修改后需重启；DeepSeek、Redis 和 OSS 不从本地 `.env` 回退。

本地扫码秘境也需要 Redis 供模型路由使用，但不消费扫码任务队列、不上传二维码。网站扫码服务还需配置 OSS、雷电控制台路径和实例池，见[扫码服务说明](naruto_daily/login_service/README.md)。

## 启动

完成配置后，双击对应脚本。

| 脚本 | 用途 |
| --- | --- |
| [启动日常.cmd](启动日常.cmd) | 保存账号登录后执行完整日常，未配置的任务跳过 |
| [启动秘境.cmd](启动秘境.cmd) | 保存账号登录后独立刷秘境 |
| [启动扫码秘境.cmd](启动扫码秘境.cmd) | 手机 QQ 扫码后只刷秘境 |
| [启动扫码服务.cmd](启动扫码服务.cmd) | 常驻 Redis worker，生成并上传二维码，扫码后执行日常 |

本地脚本从游戏第一张登录页开始；同一实例每次只运行一个脚本，按 `Ctrl+C` 停止。扫码服务默认是 `mock`，真实运行需设置 `EMULATOR_DRIVER=ldplayer`。

奖励列表使用本地 OpenCV 识别卡片内部起点，每次左拖 450 像素、持续 600 毫秒。前后截图确认实际移动后才计入成功次数，默认最多成功滑动 10 次；未移动会换起点重试，不占用这 10 次。日志分别记录尝试次数、实际位移和成功次数。无法确认位移或多个内部起点都不响应时暂停滑动，日常服务继续在原任务总时限内观察。

先预览任务，不执行点击或模型请求：

```powershell
.\启动日常.cmd --dry-run
# 仅扫码登录时使用这个入口，无需保存账号昵称
.\启动扫码秘境.cmd --dry-run
```

预览仍需读取 Cloudflare KV，并检查设备与分辨率；DPI 要单独核对。首次资源下载、回归活动和额外引导需先手动完成。

详细安装与验收见 [agent.md](agent.md)，扫码实例池和服务参数见[扫码服务说明](naruto_daily/login_service/README.md)。请仅用于个人研究和界面自动化测试，并了解游戏规则与账号风险。
