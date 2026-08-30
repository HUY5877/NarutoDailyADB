# NarutoDailyADB

雷电模拟器 9 的 ADB 坐标自动化实验项目。

第一阶段目标是以固定安卓分辨率和显式坐标配置，分别调试登录、组织祈福、签到、领取体力、免费招募和一次丰饶之间战斗。项目不包含内存读取、注入、改包、验证码处理、付费操作或反检测功能。

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

关闭公告后，脚本会执行最多四轮弹窗清理。每轮依次检查底部中央的 `assets/claim_button.png` 和右上角的 `assets/promo_close.png`。推广关闭模板使用透明背景，只比较橙红色叉本身，相似度不低于 `0.82` 时才点击模板中心；普通主界面干扰图标的实测相似度为 `0.778`。它可以处理当天登录奖励、新忍者上架和新高招忍者上架等连续弹窗；连续一轮没有发现可处理弹窗时提前结束。

请只从第一张登录页面开始运行；如果已经处于后续页面，不要执行完整登录流程。

登录阶段除协议勾选框外，其余四个关键按钮都采用“等待模板出现后点击”。每个按钮最多等待 20 秒，每秒检查一次；识别成功就立即点击，超时则停止整个流程并输出中文错误，不会继续点击后续坐标。这可以避免加载较慢时误点底部隐私协议链接。

第一次授权完成后，脚本会同时等待“第二次授权页面”或“开始游戏按钮”。如果直接出现开始游戏，说明本次只需一次授权；如果出现第二次授权页面，则点击第二次同意后继续等待开始游戏。该分支最多等待 40 秒。

组织祈福任务会先等待并点击主界面右侧的“奖励”，进入奖励任务列表后先识别“组织祈福”文字，再仅在该文字同一列下方的小区域识别“立刻前往”，最后点击实际识别到的按钮中心。找到文字但没有找到对应按钮时会报错停止；当前页面没有“组织祈福”时才向左滑动任务卡片，最多滑动六次。脚本不会使用全局固定按钮坐标，也不会点击“冒险副本挑战”或其他卡片。

进入组织祈福页面后，脚本先等待“6000 铜币焚香祈福”出现，以确认页面加载完成。左上角出现“昨日奖励”时会打开其奖励界面，等待识别到“昨日祈福奖励”标题后，每次重新截图并只点击一个橙色“领取”；“未达成”不会被点击。奖励处理完后点击右上安全空白处关闭界面，再重新识别并点击“6000 铜币焚香祈福”。昨日奖励不存在时直接跳过领取。奖励固定为三档，因此最多领取三次；三档全部领取属于正常完成。

坐标必须是安卓内部坐标，不能使用包含雷电标题栏和右侧工具栏的桌面坐标。

先打印动作而不发送输入：

```powershell
.\.venv\Scripts\naruto-daily.exe run sign_in --config config.local.json --dry-run
```

登录主流程先 dry-run，再从第一张登录页面实际执行。该命令会在清理登录弹窗后继续执行组织祈福：

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

