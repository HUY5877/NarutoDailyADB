# NarutoDailyADB

基于 Windows 和雷电模拟器 9 的《火影忍者》手游 ADB 自动化项目，支持日常任务、秘境探险和 QQ 扫码登录。

当前分支为 `test-jev` 实验实现。主界面入口、战斗结束、秘境入口、丰饶结算、装备页面/材料动作和一乐外卖领取改用 JEV 多模态选择；程序先列出可选状态或当前截图的候选编号，再把选项还原为本地结果和坐标。昵称、数字 OCR、自由坐标和导航动作仍由现有生成模型处理。JEV 返回未知状态或非法选项时不授权点击，也不自动回退到生成模型。

测试 worker 和扫码客户端均在各自 `.env` 设置 `APP_ENV=test`，读取 Cloudflare 的 `hyrz-test` namespace；正式环境使用 `APP_ENV=prod`，读取现有 `hyrz` namespace。两者内部使用相同的普通变量名，Redis 队列、日志、OSS 前缀、模型计数器和本地状态目录均由各自配置决定，代码不追加 `test-jev` 后缀。缺少测试配置时停止启动，不读取正式配置。客户端消息、当前配置示例和部署条件见[测试协议](docs/test-jev-client-protocol.md)。测试必须使用独立模拟器实例池。

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
| 根目录 `.env` | `APP_ENV=test`、共用的 Cloudflare Token/account、`CLOUDFLARE_KV_NAMESPACE_ID_TEST`、本地鱼玩 `YUWAN_API_KEY` / `YUWAN_MODEL`，以及雷电控制台路径和测试专用实例池等机器参数 |
| Cloudflare KV | `hyrz` 与 `hyrz-test` 两个 namespace 各自保存普通变量名的纯文本配置，例如 `REDIS_QUEUE_KEY`、`OSS_KEY_PREFIX`、`JEV_API_KEY` 和 `JEV_MODEL`。字段见[配置表](docs/test-jev-client-protocol.md#cloudflare-配置) |

Cloudflare Token 需要目标账户的 **Workers KV Storage → Read** 权限，列出 key 和批量读取均须允许。`APP_ENV` 只能是 `prod` 或 `test`，省略时为 `prod`；JEV 测试工作区须显式设置 `test`。prod 选择 `CLOUDFLARE_KV_NAMESPACE_ID_PROD`，兼容旧 `CLOUDFLARE_KV_NAMESPACE_ID`；test 必须提供 `CLOUDFLARE_KV_NAMESPACE_ID_TEST`。启动只读取选定 namespace，全部读取成功才发布配置，修改后需重启。Redis、OSS、数据库和隔离路径不从本地旧配置回退；鱼玩 key/model 按原约定成对保存在本地 `.env`，启动解析后由任务子进程继承。

扫码服务还需配置 OSS、雷电控制台路径和实例池，见[扫码服务说明](naruto_daily/login_service/README.md)。服务按分配到的实例覆盖任务配置中的 ADB 路径和 `serial`。

## 启动

完成配置后，双击唯一启动入口 [启动扫码服务.cmd](启动扫码服务.cmd)。它运行常驻 Redis worker，生成并上传二维码，扫码后执行日常；按 `Ctrl+C` 停止。服务默认是 `mock`，真实运行需设置 `EMULATOR_DRIVER=ldplayer`。

奖励列表使用本地 OpenCV 识别卡片内部起点，每次左拖 450 像素、持续 600 毫秒。前后截图确认实际移动后才计入成功次数，默认最多成功滑动 20 次；未移动会换起点重试，不占用这 20 次。日志分别记录尝试次数、实际位移和成功次数。无法确认位移或多个内部起点都不响应时暂停滑动，日常服务继续在原任务总时限内观察。

完整日常和登录后的日常在生存挑战之后、秘境探险之前执行小队突袭。从奖励卡片进入，卡片显示“已完成”时跳过小队处理，不读取剩余次数。准备页使用本地模板识别；首次进入时若四倍奖励已勾选，就点击一次关闭并复查，首次匹配前不检查收益上限。出现提示时只点“继续出战”，每隔 5 秒观察是否回到准备页；回到后若收益未满则再次匹配，达到上限则返回主界面继续秘境。旧配置可以不包含 `squad_assault`；使用新版配置时，也可运行 `python -m naruto_daily run squad_assault --config config.example.json` 单独执行。独立刷秘境线路 `secret_realm_farm` 不包含小队突袭。

奖励入口由 JEV 选择当前截图的候选，点击前用同一帧中的“奖励”文字模板（普通和红点两种样式）复核所选区域；选中商城等其他入口时取消点击并重新识别。点击后最多再观察 10 秒确认奖励任务卡片布局，未确认则停止本次任务，避免在错误页面持续查找卡片。

被奖励入口复核拒绝的最新一次识别会保存在 `.data/diagnostics/main-screen/rejected-rewards.zip`，包含原图、实际发送的候选拼图、提示词和原始模型结果。证据包每次覆盖，不包含 API 密钥，且不进入 Git；日志会记录候选编号、复核分数和保存位置。

启动前可查看实例池配置和占用，不执行游戏任务或模型请求：

```powershell
.\.venv\Scripts\python.exe -m naruto_daily.login_service.pool_admin list
```

该命令仍需读取 Cloudflare KV，并要求服务配置完整。每个实例的分辨率和 DPI 要单独核对；首次资源下载、回归活动和额外引导需先手动完成。

详细安装与验收见 [agent.md](agent.md)，扫码实例池和服务参数见[扫码服务说明](naruto_daily/login_service/README.md)。请仅用于个人研究和界面自动化测试，并了解游戏规则与账号风险。
