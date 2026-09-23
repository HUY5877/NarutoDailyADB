# NarutoDailyADB

用于雷电模拟器 9 的《火影忍者》日常自动化实验项目，通过 ADB、截图模板和视觉模型完成操作，适配 **1920×1080 安卓分辨率**。

## 安装与配置

需要 Windows、Python 3.11+、已安装游戏的雷电模拟器，以及支持图片识别的模型 API。请先确认游戏服务规则允许你的使用方式。

在项目目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"

# 仅首次创建配置；已有文件不要覆盖。
Copy-Item config.example.json config.local.json
Copy-Item .env.example .env
```

编辑 `config.local.json`：

| 配置项 | 设置方式 |
| --- | --- |
| `adb_path` | 改为自己雷电安装目录中的 `adb.exe` 路径 |
| `serial` | 单设备可保留 `null`；多设备时填写目标 ADB 设备编号 |
| `screen` | 保持 `width: 1920`、`height: 1080`，并将模拟器分辨率设为相同值 |
| `tasks` | 各任务的坐标、识别模板、等待时间和模型调用限制 |

编辑 `.env`，接口示例与 [.env.example](.env.example) 一致：

```dotenv
MULTIMODAL_API_KEY=你的API密钥
MULTIMODAL_BASE_URL=https://api.qnaigc.com/v1
MULTIMODAL_MODEL=deepseek/deepseek-v4.1-flash
LOGIN_ACCOUNT_NAME="要登录的完整昵称"
```

`BASE_URL` 不包含 `/chat/completions`。更换服务时需同时匹配接口地址、模型名称和密钥。程序读取配置文件同目录下的 `.env`，系统环境变量优先；修改后重新启动脚本生效。相关界面截图会发送给配置的模型服务，`.env` 和 `config.local.json` 不应提交到 Git。

## 启动

打开雷电，让游戏停在**第一张登录页面**，每次只运行一个脚本：

| 启动文件 | 用途 |
| --- | --- |
| [启动日常.cmd](启动日常.cmd) | 登录后执行完整日常队列 |
| [启动秘境.cmd](启动秘境.cmd) | 登录后从“忍者挑战”进入秘境，按挑战券循环 |

首次运行先检查设备并预览动作：

```powershell
.\.venv\Scripts\naruto-daily.exe check --config config.local.json
.\.venv\Scripts\naruto-daily.exe run all --config config.local.json --dry-run
```

`--dry-run` 会检查连接和分辨率，但不点击、不调用模型。确认后双击对应启动文件。运行中按 **Ctrl+C** 停止；正常结束或出错后，窗口保留输出，按回车关闭。日志默认只显示在窗口，不自动保存文件。

## 当前功能

日常顺序：登录 → 一乐外卖 → 装备养成 → 组织祈福及免费招财 → 免费招募 → 丰饶之间 → 生存挑战 → 奖励入口秘境。

- **装备养成**：每轮选择最低进阶装备，先检查“进阶”按钮；有就点击并重新检查，没有才处理材料、扫荡、合成和装备。体力 ≤5 时结束装备任务，继续后续日常。
- **秘境**：每次回房间重新读取挑战券，券为 0 时结束；无法识别时停止。阴阳秘境主动退出，毒风秘境先由视觉导航寻找封印，再进入战斗。
- **战斗**：固定坐标点击四个技能/普攻按钮，定期检查结算；当前没有通用的边走边打功能。
- **签到、领取体力**：示例配置仍为空，完整队列会跳过这两项。

布局变化时需要重新校准坐标和模板。坐标以安卓画面为准，不包含雷电窗口标题栏和工具栏。

## 单项运行

已登录并处于主界面时，可以单独执行已配置任务，例如：

```powershell
.\.venv\Scripts\naruto-daily.exe run equipment_upgrade --config config.local.json
.\.venv\Scripts\naruto-daily.exe run abundance --config config.local.json
```

其他任务名和参数可通过 `.\.venv\Scripts\naruto-daily.exe run --help` 查看。注意：`run login` 还会接着执行主要日常任务，并非只登录；完整日常统一使用 `run all`。`secret_realm_farm` 包含登录，仍须从第一张登录页面启动。

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| 找不到设备或有多个设备 | 确认雷电已启动、`adb_path` 正确；多设备时设置 `serial` |
| 分辨率不一致、按钮点偏 | 将安卓分辨率设为 1920×1080；界面布局变化后重新校准 |
| API 网络异常或超时 | 检查接口连接及对应任务的 `multimodal.timeout_seconds`；仅凭超时不能判断欠费 |
| API 返回 HTTP 错误 | 根据状态码和服务商控制台检查密钥、模型权限、限流或额度 |
| 识别失败后退出 | 查看报错前的场景日志；确认当前页面，恢复到任务要求的起点后再运行，不支持自动断点续跑 |

## 开发

| 位置 | 内容 |
| --- | --- |
| `naruto_daily/tasks.py`、`runner.py` | 任务顺序与调度 |
| `naruto_daily/common/` | ADB、图像匹配、共用流程 |
| `naruto_daily/multimodal/` | 图片模型客户端、提示词、视觉导航 |
| `naruto_daily/` 下各功能目录 | 登录、装备、招募、战斗、秘境等业务逻辑 |
| `assets/`、`tests/` | 图片模板与自动化测试 |

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```
