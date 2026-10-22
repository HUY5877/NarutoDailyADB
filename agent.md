# AI 安装指南

给 AI 的任务：在 Windows 上安装本项目及运行依赖，配好一个雷电实例，完成本机预检。保留已有配置、虚拟环境和其他实例；不要在安装验收时启动真实任务。

仓库已实现雷电实例管理、ADB 点击和截图、扫码登录、日常任务及日志。直接安装并使用现有代码，**无需另外开发“雷电操作系统”、模拟器控制器或 ADB 驱动**。

## 1. 需要下载什么

| 内容 | 要求 |
| --- | --- |
| 项目源码 | 下载完整仓库，保留 `assets/`、`config.example.json` 和 `启动扫码服务.cmd` |
| Python | **3.11 或以上**；先检查实际版本，再创建虚拟环境 |
| 雷电模拟器 | **雷电 9（9.x）**，从[官方历史版本页](https://www.ldmnq.com/other/version-history-and-release-notes.html)选择“9.0版本历史”，不要默认下载首页推广的其他大版本 |
| 游戏 | 在雷电内安装官方《火影忍者》手游，完成资源下载、更新和首次引导 |

维护机安装的 `dnplayer.exe` 文件版本为 **9.5.37.0**，可作参考；仓库没有锁定小版本，不能省略后面的显示和 ADB 预检。无需单独安装 Node.js、Android Studio 或另一套 ADB，使用雷电自带的 `adb.exe`。

尚未下载项目时执行；已有源码则直接进入现有项目目录：

```powershell
git clone https://github.com/HUY5877/NarutoDailyADB.git
cd NarutoDailyADB
```

## 2. 安装 Python 依赖

在项目根目录打开 PowerShell。启动入口优先使用 `.venv-login-service`，其次 `.venv`，安装依赖时必须选中同一个环境：

```powershell
python --version
$Python = @('.\.venv-login-service\Scripts\python.exe', '.\.venv\Scripts\python.exe') |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Python) {
    python -m venv .venv-login-service
    $Python = '.\.venv-login-service\Scripts\python.exe'
}
& $Python --version
& $Python -m pip install -e ".[login-service]"
& $Python -m pip check
```

两次版本检查均需满足 Python 3.11+。未安装 Python 时从[官网](https://www.python.org/downloads/windows/)安装，再执行。无需激活虚拟环境或修改 PowerShell 执行策略，也不要逐个手装二维码、OSS、模型或日志 SDK；项目依赖会一并安装。

## 3. 配好雷电和游戏

只选一个专用实例开始。**以下配置必须对每个将来加入实例池的实例生效：**

| 设置 | 必须采用的值 |
| --- | --- |
| 模拟器版本 | **雷电 9** |
| 安卓分辨率 | **宽 1920 × 高 1080，横屏** |
| DPI | **240** |
| ADB 调试 | **开启本地连接** |
| CPU 虚拟化 | 开启 Intel VT-x / AMD-V（SVM）；未开启时由用户在 BIOS/UEFI 中设置 |

在雷电实例的设置界面修改分辨率、DPI 和 ADB 开关，保存后重启该实例。改变桌面窗口大小、Windows 缩放或只改任务 JSON，都不能替代这些设置。CPU 核数、内存和 FPS 按机器资源调整，项目没有固定值要求；ROOT 无需开启。

找到实际雷电安装目录，确认同目录有 `adb.exe` 和 `ldconsole.exe`（或 `dnconsole.exe`）。将下面路径改成本机值：

```powershell
$LDHome = 'C:\LDPlayer\LDPlayer9'  # 示例，必须替换
$Console = Join-Path $LDHome 'ldconsole.exe'
if (-not (Test-Path $Console)) { $Console = Join-Path $LDHome 'dnconsole.exe' }
$Adb = Join-Path $LDHome 'adb.exe'
& $Console list2
```

`list2` 每行首列是实例索引，次列是名称。记录专用实例的实际索引，不能把它当作 ADB 设备号。安装游戏并完成更新、协议和首次引导后，退出已有账号，停在游戏登录页；模拟器内无需另装 QQ，手机扫码确认由用户完成。

## 4. 本机预检

保持目标实例启动。以下命令只查询设备，不执行游戏任务：

```powershell
$InstanceIndex = 0  # 示例，替换为 list2 核对的索引
& $Console adb --index $InstanceIndex --command 'get-serialno'
& $Adb devices
$DeviceSerial = 'emulator-5554'  # 示例，替换为上面查到的实际设备号
& $Adb -s $DeviceSerial shell getprop sys.boot_completed
& $Adb -s $DeviceSerial shell wm size
& $Adb -s $DeviceSerial shell wm density
& $Adb -s $DeviceSerial shell pm list packages KiHan
```

验收结果必须满足：

- 设备状态为 `device`，安卓启动完成值为 `1`。
- 实际分辨率为 **1920x1080**，实际 DPI 为 **240**；若输出含 `Override`，同时核对覆盖值。代码不会自动检查 DPI。
- 找到游戏包。默认包名是 **`com.tencent.KiHan`**；找不到时用 `pm list packages -3` 核对实际安装结果，不要猜成 `com.tencent.naruto`。
- Python 环境的 `pip check` 通过。保留完整共享任务配置和资源，不另建任务 JSON；服务会自动使用雷电自带 ADB 和分配实例的设备号。

设备未连接时，先检查 ADB 本地连接开关、保存后是否重启及安装路径。只操作目标实例，不为排障擅自关闭 Hyper-V、WSL 或其他模拟器。

## 5. 安装完成后

向用户报告 Python 路径、雷电版本和安装路径、实例索引、设备号、分辨率及 DPI 实测值。未通过的项目明确标为未验证；本机预检不代表云服务已经联通。

配置由管理员统一提供，只需核对 `LDPLAYER_CONSOLE_PATH`、`LDPLAYER_POOL_INDICES` 与本机一致。真实运行使用 `EMULATOR_DRIVER=ldplayer`，并发从 `WORKER_CONCURRENCY=1` 开始。不要求用户自行搭建 Redis、OSS、Cloudflare 或模型服务，也不在安装过程中生成或索要一整套密钥。

拿到配置后，仅在用户要求实际运行时双击根目录 `启动扫码服务.cmd`；它会领取真实任务，扫码后自动执行。需要查服务细节时再读[扫码服务说明](naruto_daily/login_service/README.md)。

**最后提示用户：请联系管理员获取本项目的 `.env` 配置文件，放到项目根目录。已有 `.env` 时保留原文件，密钥不输出、不提交 Git。**
