# NarutoDailyADB

雷电模拟器 9 的 ADB 坐标自动化实验项目。

第一阶段目标是以固定安卓分辨率和显式坐标配置，分别调试登录、组织祈福、两次免费招财、签到、领取体力、免费招募和一次丰饶之间战斗。项目不包含内存读取、注入、改包、验证码处理、付费操作或反检测功能。

> 在线游戏自动化可能违反游戏服务规则并带来账号风险。请仅在理解风险的前提下用于个人研究和界面自动化测试。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item config.example.json config.local.json
```

先打开雷电模拟器，再检查 ADB 和安卓分辨率：

```powershell
.\.venv\Scripts\naruto-daily.exe check --config config.local.json
```

## 校准和运行

示例配置已按当前雷电 `1920×1080` 安卓画面填写登录流程和组织祈福入口；其余任务数组仍为空。登录坐标来自实际页面：勾选协议、安卓 QQ、授权同意、开始游戏、关闭公告。

关闭公告并领取当天登录奖励后，脚本会调用通用“回到主界面”流程。该流程最多检查十轮，只在安卓画面右上角识别透明背景的橙红色叉；识别成功后先等待 3 秒并再次确认，点击后再等待 3 秒，然后继续处理下一层界面。连续两轮没有发现叉时提前结束，因此可以逐层关闭新忍者、新活动以及二级、三级界面，而不是盲点固定坐标。关闭叉阈值为 `0.82`；真实主界面右上角干扰图标的离线分数为 `0.778`，低于点击阈值。

请只从第一张登录页面开始运行；如果已经处于后续页面，不要执行完整登录流程。

登录阶段除协议勾选框外，其余四个关键按钮都采用“等待模板出现后点击”。每个按钮最多等待 20 秒，每秒检查一次；超时则停止整个流程并输出中文错误，不会继续点击后续坐标。这可以避免加载较慢时误点底部隐私协议链接。

第一次授权完成后，脚本会同时等待“第二次授权页面”或“开始游戏按钮”。如果直接出现开始游戏，说明本次只需一次授权；如果出现第二次授权页面，则点击第二次同意后继续等待开始游戏。该分支最多等待 40 秒。

登录链路中的图片按钮首次识别成功后会先等待 3 秒，再次确认按钮仍存在后才点击，点击完成后再等待 3 秒让页面响应。按钮在前置等待期间消失时不会点击旧坐标，而是继续识别。等待时长可通过 `pre_click_wait_seconds` 和 `post_click_wait_seconds` 调整。

组织祈福任务会先等待并点击主界面右侧的“奖励”，然后逐屏向右滑动任务栏并识别“组织祈福”文字。找到后只检查该卡片自身下半部分：出现“已完成”就记录日志并结束本次任务；出现“立刻前往”才点击实际识别到的按钮中心。两者都没有时会报错停止。任务卡片位置不固定，最多横向滑动六次；脚本不会使用全局固定按钮坐标，也不会点击相邻的“冒险副本挑战”“忍者招募”等卡片。

进入组织祈福页面后，脚本先等待“6000 铜币焚香祈福”出现，以确认页面加载完成。左上角出现“昨日奖励”时会打开其奖励界面，等待识别到“昨日祈福奖励”标题后，每次重新截图并只点击一个橙色“领取”；“未达成”不会被点击。奖励处理完后点击右上安全空白处关闭界面，再重新识别并点击“6000 铜币焚香祈福”。昨日奖励不存在时直接跳过领取。奖励固定为三档，因此最多领取三次；三档全部领取属于正常完成。

祈福处理结束后，脚本用通用回主界面流程逐层点击右上角叉，再次进入“奖励”并横向查找“进行1次金币招财”。卡片显示“已完成”时只跳过招财；否则只点击该卡片下方对应的“立刻前往”。进入招财页后，脚本仅在底部中间安全区域识别 `assets/free_fortune_once.png`，按“识别、等待 3 秒、再次确认、点击、等待 3 秒”的顺序完成两次免费招财。右侧付费按钮位于搜索区域之外，也不使用它的模板。

坐标必须是安卓内部坐标，不能使用包含雷电标题栏和右侧工具栏的桌面坐标。

先打印动作而不发送输入：

```powershell
.\.venv\Scripts\naruto-daily.exe run sign_in --config config.local.json --dry-run
```

登录主流程先 dry-run，再从第一张登录页面实际执行。该命令会在清理登录弹窗后继续执行组织祈福和两次免费招财：

```powershell
.\.venv\Scripts\naruto-daily.exe run login --config config.local.json --dry-run
.\.venv\Scripts\naruto-daily.exe run login --config config.local.json
```

逐项测试：

```powershell
.\.venv\Scripts\naruto-daily.exe run organization_pray --config config.local.json
.\.venv\Scripts\naruto-daily.exe run sign_in --config config.local.json
.\.venv\Scripts\naruto-daily.exe run stamina --config config.local.json
.\.venv\Scripts\naruto-daily.exe run recruit --config config.local.json
.\.venv\Scripts\naruto-daily.exe run abundance --config config.local.json
```

全部坐标分别验证后，才运行 `all`。该命令开始前有五秒倒计时，可按 Ctrl+C 取消：

```powershell
.\.venv\Scripts\naruto-daily.exe run all --config config.local.json
```

纯坐标脚本无法判断弹窗和页面是否正确。任何页面变化、中断或加载异常都应立即停止，不能把付费、金币购买或确认按钮加入配置。

脚本运行时会在终端打印中文日志，包括动作序号、点击坐标、等待时间、模板相似度、是否发现领取按钮以及错误原因。

