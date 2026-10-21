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

### 战斗区域点击

丰饶之间、秘境探险和决斗场在兼容的雷电设备上优先使用高速 ADB 点击通道。丰饶之间通过 `run_battle_without_first_skill` 点击二技能、大招、普攻，目标每个按钮每秒 5 次，每 30 秒暂停检查；秘境通过 `run_battle(..., prefer_fast_taps=True)` 点击一技能、二技能、大招、普攻，每 18 秒暂停检查。两者使用各自配置中的坐标，保留原有战前检查和结束判断。共享配置保持原来的四按钮结构，不需要新增配置。

决斗场提供独立函数 `naruto_daily.battle.run_duel_battle(client, battle, ...)`。调用方完成入场并传入适用于决斗场的结算识别配置后，该函数将按钮列表替换为以下六个区域的点击位置；字母仅用于对应截图中的区域，不会发送键盘信号。I 对应二技能，不包含一技能 J。

| 截图区域 | 操作 | 1920×1080 点击坐标 |
| --- | --- | --- |
| E | 秘卷 | `(1730,445)` |
| R | 通灵 | `(1680,260)` |
| I | 二技能 | `(1530,750)` |
| K | 普攻 | `(1715,885)` |
| 空格 | 替身 | `(1300,965)` |
| L | 大招 | `(1730,650)` |

高速通道使用持续 ADB 连接，各按钮使用独立触点同步按下、短暂保持后抬起。丰饶目标每个按钮每秒 5 次，秘境和决斗场目标每秒 20 次；配置了更长点击间隔时遵守该间隔。每轮确认抬起后才发送下一轮，不积压待发批次。截图和模型检查期间只停止发送点击，保持连接；继续战斗时复用原连接。通道只在战前检查通过后的首次实际点击时打开，按下前重新检查任务时限和结算检查边界，战斗结束或异常退出时释放触点并关闭连接。决斗场使用上述六个按钮（含替身），通灵坐标向左偏移以避开大厅入口；分辨率不为 1920×1080 时停止。

高速通道预检设备名、触点数量、1920×1080 坐标范围、写权限和 Android 事件格式；预检不兼容时沿用原有的多线程 ADB 坐标点击。发送后发生通道错误则停止，不补发同一批次。三个场景共用通道管理和战斗循环，支持 `model`、`end_detector`、`before_attack` 和 `check_interval_seconds`，不会修改传入的战斗配置。调用方仍需确保在战斗画面启用循环，坐标点击本身不识别场景。

扫码客户端通过 `userOptions.mainTask: "arena_weekly"` 选择决斗场周胜，省略或选择 `daily` 时沿用完整日常。周胜复用扫码登录收尾和弹窗清理，随后执行 `config.example.json` 中独立的 `tasks.arena_weekly`：通过 OpenCV 识别主界面设置按钮，进入高级设置，在两列选项之间的空白区域向上滑动 10 次滚到底部（`scroll_swipes` 可配置为 1 到 20 次）；定位“决斗场只匹配玩家”，仅在已勾选时点击取消，并重新截图确认未勾选。随后调用现有回主界面模块，确认回到主界面后先在主场景安全区域向右滑动 5 次，归位到最左侧；再逐屏向左滑动查找决斗场，最多 15 次。每屏先由 JEV 判断是否出现清晰的决斗场图标，确认后再由大模型返回按钮位置并通过 ADB 点击。进入决斗场后，使用 OpenCV 模板点击左下角忍术对战图标，并确认“好友切磋”标记出现后开始战斗。识别或入场失败时停止本次任务。

第一次调用决斗场六区战斗模块时开始独立计时 40 分钟；准备页面耗时不占用这 40 分钟。进入后持续运行该模块，到时正常结束本次任务，父进程立即终止执行子进程并关闭、释放实例，常驻 worker 继续领取其他任务。该定时结束不经过失败保留现场的 60 秒等待。更新时需同时保留新版 `config.example.json`、`assets/arena_ninjutsu_battle.png`、`assets/arena_friendly_match.png`，以及新增的 `assets/main_settings.png`、`assets/settings_advanced_tab.png`、`assets/arena_players_only_label.png`、`assets/settings_checkbox_checked.png`、`assets/settings_checkbox_unchecked.png`，并重启常驻 worker。

## 启动

完成配置后，双击唯一启动入口 [启动扫码服务.cmd](启动扫码服务.cmd)。它运行常驻 Redis worker，生成并上传二维码，扫码后执行日常；按 `Ctrl+C` 停止。服务默认是 `mock`，真实运行需设置 `EMULATOR_DRIVER=ldplayer`。

奖励列表使用本地 OpenCV 识别卡片内部起点，每次左拖 450 像素、持续 600 毫秒。前后截图确认实际移动后才计入成功次数，默认最多成功滑动 20 次；未移动会换起点重试，不占用这 20 次。日志分别记录尝试次数、实际位移和成功次数。无法确认位移或多个内部起点都不响应时暂停滑动，日常服务继续在原任务总时限内观察。

完整日常和登录后的日常在生存挑战之后、秘境探险之前执行小队突袭。从奖励卡片进入，卡片显示“已完成”时跳过小队处理，不读取剩余次数。准备页使用本地模板识别；首次进入时若四倍奖励已勾选，就点击一次关闭并复查，首次匹配前不检查收益上限。出现提示时只点“继续出战”，每隔 5 秒观察是否回到准备页；等待战斗结束期间，每轮若未匹配到中间“自动战斗中”文案，就点击左侧固定坐标 `(45,330)` 一次（自动按钮左上侧，避开准备页的声望奖励卡片），再继续原轮询，不额外识别自动按钮或截图确认点击结果。文字模板只保留文字与描边，背景及省略号不参与匹配。回到后若收益未满则再次匹配，达到上限则返回主界面继续秘境。旧配置可以不包含 `squad_assault`；使用新版配置时，也可运行 `python -m naruto_daily run squad_assault --config config.example.json` 单独执行。独立刷秘境线路 `secret_realm_farm` 不包含小队突袭。

奖励入口由 JEV 选择当前截图的候选，点击前用同一帧中的“奖励”文字模板（普通和红点两种样式）复核所选区域；选中商城等其他入口时取消点击并重新识别。点击后最多再观察 10 秒确认奖励任务卡片布局，未确认则停止本次任务，避免在错误页面持续查找卡片。

被奖励入口复核拒绝的最新一次识别会保存在 `.data/diagnostics/main-screen/rejected-rewards.zip`，包含原图、实际发送的候选拼图、提示词和原始模型结果。证据包每次覆盖，不包含 API 密钥，且不进入 Git；日志会记录候选编号、复核分数和保存位置。

启动前可查看实例池配置和占用，不执行游戏任务或模型请求：

```powershell
.\.venv\Scripts\python.exe -m naruto_daily.login_service.pool_admin list
```

该命令仍需读取 Cloudflare KV，并要求服务配置完整。每个实例的分辨率和 DPI 要单独核对；首次资源下载、回归活动和额外引导需先手动完成。

详细安装与验收见 [agent.md](agent.md)，扫码实例池和服务参数见[扫码服务说明](naruto_daily/login_service/README.md)。请仅用于个人研究和界面自动化测试，并了解游戏规则与账号风险。
