# NarutoDailyADB：AI 安装与配置指南

本文件供用户 clone 项目后，让 AI 执行首次安装、配置和环境验收。可以直接对 AI 说：**“请读取根目录 agent.md，按当前电脑的实际情况安装依赖、配置雷电模拟器并完成预检。”** 文件名按本项目约定使用 `agent.md`；若 AI 工具只自动加载 `AGENTS.md`，需要显式要求它读取本文件。

按统一扫码服务入口更新于 2026-10-14。执行前复核 `README.md`、`.env.example`、`config.example.json`、`pyproject.toml` 和相关代码；发生差异时以当前实现为准。

本指南面向公开安装场景，只记录下文列出的配置项。参考仓库文件时，不补充公司内部服务的名称、地址或专用变量。

## 1. 执行范围与配置入口

安装目标是 Windows、Python 3.11+、**雷电模拟器 9**。先检查已有安装和配置，能复用就复用。本文中的安装路径、实例索引和设备号均为示例，不能直接当成用户电脑的实际值。

- 只配置用户指定的模拟器实例。保留已有 `.env`、`config.example.json`、虚拟环境、游戏数据和其他实例；不要用示例文件覆盖已有配置。直接使用根目录共享任务配置，不另建本地任务配置副本。
- 只安装、配置和验收环境。实际运行游戏任务或启动扫码 worker，应由用户明确要求；worker 启动后会消费真实 Redis 任务。
- 密钥写入本地 `.env` 或用户指定的 Cloudflare KV，不写入 JSON、本文、日志或 Git。缺少用户凭据时完成其余步骤，并报告具体缺项，不编造凭据或宣称配置成功。
- 需要 BIOS/UEFI 操作、管理员交互或登录授权时，说明具体步骤交由用户完成。不要为安装模拟器擅自关闭 Hyper-V、WSL、系统安全功能或自动重启电脑。

| 配置位置 | 当前用途 |
| --- | --- |
| 根目录 `config.example.json` | 服务共用的屏幕尺寸、完整任务配置和资源引用；执行时由 worker 覆盖 ADB 路径和 serial |
| 根目录 `.env` | Cloudflare 连接凭据、雷电和 worker 的机器参数 |
| Cloudflare KV | DeepSeek 渠道、Redis、OSS 等运行配置；启动时读取 |
| 雷电每个实例的设置 | 安卓分辨率、DPI、ADB 调试；实例池中的每个实例都必须一致 |

本项目无需为了 Python worker 安装 Node.js，也无需安装 Android Studio。使用雷电安装目录自带的 `adb.exe`，通过完整路径调用即可，无需修改系统 `PATH`。

## 2. 检查和安装基础环境

在 clone 后的项目根目录打开 PowerShell，先检查：

```powershell
Get-Location
git status --short
python --version
Get-Command python, py, git -ErrorAction SilentlyContinue
Test-Path .\.venv\Scripts\python.exe
Test-Path .\.venv-login-service\Scripts\python.exe
Test-Path .\.env
Test-Path .\config.example.json
```

确认实际 Python 版本至少为 3.11。未安装时从 Python 官方安装 Windows 版本，再用明确的 Python 可执行文件创建虚拟环境；不要仅凭存在 `python` 命令就认定版本正确。

新环境使用项目根目录 `.venv`，无需激活环境，也无需调整 PowerShell 执行策略：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test,login-service]"
if (-not (Test-Path .\.env)) {
    Copy-Item .\.env.example .\.env
}
```

已有 `.venv` 时跳过创建，检查其 Python 版本后安装依赖。`启动扫码服务.cmd` 优先使用 `.venv-login-service`，其次 `.venv`；两者同时存在时，应检查实际被选中的环境是否安装了当前依赖。下文预检示例使用 `.venv`，若服务选择已有 `.venv-login-service`，应替换成该环境的 Python 路径。

## 3. 安装雷电模拟器 9

先通过已有快捷方式的“打开文件所在位置”或已安装程序信息确定雷电 9 的安装目录。没有安装时，从[雷电官方历史版本页面](https://www.ldmnq.com/other/version-history-and-release-notes.html)选择 **9.0 版本**下载安装。官网首页当前主推版本可能不同，不能因此把项目运行环境替换成雷电 14。

使用官方安装程序，记录实际安装目录；不要猜测安装器支持哪些静默参数。安装完成后确认同一目录内存在 `adb.exe`，以及 `ldconsole.exe` 或 `dnconsole.exe`。控制台命令参考[雷电官方命令行说明](https://help.ldmnq.com/docs/LDPlayer-ming-ling-hang-kai-fa-zhe-shi-yong-shuo-ming)。

检查任务管理器“性能 → CPU → 虚拟化”。未启用时按[官方 VT 指南](https://help.ldmnq.com/docs/jg8S1J)由用户进入 BIOS/UEFI 开启 Intel VT-x 或 AMD-V/SVM；具体菜单依主板而定。

以下 PowerShell 变量仅对当前终端有效。将路径改为实际安装目录，再执行：

```powershell
$LDHome = 'C:\LDPlayer\LDPlayer9'
$Adb = Join-Path $LDHome 'adb.exe'
$Console = Join-Path $LDHome 'ldconsole.exe'
if (-not (Test-Path -LiteralPath $Console)) {
    $Console = Join-Path $LDHome 'dnconsole.exe'
}
if (-not (Test-Path -LiteralPath $Adb)) { throw '未找到雷电自带 adb.exe' }
if (-not (Test-Path -LiteralPath $Console)) { throw '未找到雷电控制台程序' }
& $Console list2
```

`list2` 每行首列是实例索引，次列是实例名称。记录目标实例的实际索引；它与 ADB serial 是不同字段。后续所有命令都应指向这个已核对的实例。

## 4. 雷电实例必须这样配置

**每个用于本项目的实例都必须采用 1920×1080 横屏、DPI 240。** 坐标和模板基于这一布局，不能通过只修改 JSON 的 `screen` 字段来适配其他分辨率。

| 设置 | 要求 | 原因或验收方式 |
| --- | --- | --- |
| 安卓分辨率 | 宽 **1920**、高 **1080** | 横屏，不能填成 1080×1920；检查 `wm size` 和原始 ADB 截图 |
| DPI | **240** | 影响按钮、文字和授权页布局；单独检查 `wm density` |
| ADB 调试 | **开启本地连接** | Python 在同一电脑操作模拟器，无需开放远程连接 |
| 屏幕方向 | 保持横屏 | 安卓实际画面及游戏截图必须为 1920×1080 |
| ROOT 权限 | 本项目不要求开启 | 当前操作使用普通 ADB 截图、控件查询和输入命令 |
| CPU / 内存 | 单实例可从 4 核、4096 MB 起步，按主机资源调整 | 属于性能建议，仓库没有强制要求；多开需考虑总资源 |
| 帧率 | 可先保留默认值 | 仓库没有固定 FPS 要求；不要将性能建议误写成坐标匹配条件 |

### 在设置界面配置

1. 打开目标实例的齿轮“设置”。在性能/分辨率设置中选择自定义，填写宽 `1920`、高 `1080`、DPI `240`。不同雷电 9 小版本的菜单名称可能略有差异，以实际字段为准。
2. 在“其他设置”中将“ADB 调试”设为“开启本地连接”，保存设置并重启**目标实例**。该入口也见[雷电官方设置说明](https://www.ldplayer.net/support/how-to-solve-locked-screen.html)；其中解决锁屏所用的 ROOT 操作不属于本项目安装要求。
3. 启动后逐项验收。调整桌面窗口大小、最大化或 Windows 显示缩放，不能替代安卓分辨率设置。项目坐标不包含标题栏和右侧工具栏。

### 通过控制台配置分辨率和 DPI

目标实例应先停止，运行中的任务应先结束。先核对索引再执行；不要用 `quitall` 关闭其他实例。

```powershell
$InstanceIndex = 0  # 示例：替换为 list2 中核对过的目标实例索引
& $Console quit --index $InstanceIndex
# 等待 list2 确认该实例已停止，再修改
& $Console list2
& $Console modify --index $InstanceIndex --resolution '1920,1080,240'
& $Console launch --index $InstanceIndex
```

`--resolution` 参数依次是宽、高、DPI。这个命令不替代设置界面的 ADB 调试开关，也不应附加修改 IMEI、MAC 或其他设备身份的参数。

### 启动后验收 ADB、尺寸和 DPI

等模拟器完成安卓启动，按[雷电 9 官方 ADB 教程](https://help.ldmnq.com/docs/LD9adbserver)查询设备：

```powershell
& $Console adb --index $InstanceIndex --command 'get-serialno'
& $Adb devices
$DeviceSerial = 'emulator-5554'  # 示例：填写上面核对过且状态为 device 的实际 serial
& $Adb -s $DeviceSerial shell getprop sys.boot_completed
& $Adb -s $DeviceSerial shell wm size
& $Adb -s $DeviceSerial shell wm density
```

验收要求：

- 指定设备在 `adb devices` 中的状态为 `device`，`sys.boot_completed` 输出 `1`。
- `wm size` 的 `Physical size` 为 `1920x1080`。若还有 `Override size`，其值也必须一致；原始截图也必须是 1920×1080。
- `wm density` 的实际生效密度为 `240`。若存在 `Override density`，需一起核对，不能只看 `Physical density`。

当前 `naruto_daily/common/adb_client.py` 的尺寸读取优先使用 `Physical size`；扫码实例管理检查 `wm size` 输出中的最后一个尺寸。**两条路径均不自动检查 DPI**，因此尺寸检查成功仍不能证明 DPI 正确。不要只通过 `adb shell wm size` 临时覆盖尺寸来绕过配置，应在雷电设置中配置并重启；已有尺寸或密度覆盖时，应在核对目标设备后处理覆盖，再重新验收。

若没有设备，先核对本地 ADB 开关、保存/重启是否完成和安装路径。必要时按官方端口规则连接目标实例，连接后重新读取实际 serial：

```powershell
$AdbPort = 5555 + 2 * $InstanceIndex
& $Adb connect "127.0.0.1:$AdbPort"
& $Adb devices
```

不要把端口公式当成 serial，也不要假定所有机器都是 `emulator-5554`。多设备或同一实例出现多种连接标识时，要明确选择目标。重启 ADB server 会影响本机其他 ADB 会话，只在确认可以中断时执行。

## 5. 安装游戏并准备登录页面

在目标实例安装官方《火影忍者》手游，并完成首次资源下载、更新、协议及必要的手动引导。游戏不包含在 Git 仓库中。

- 扫码 worker 支持模拟器安装 QQ 或不安装 QQ；无 QQ 时使用游戏内 QQ 网页扫码授权。手机扫码确认由用户完成，服务无需保存账号昵称。
- 检查实际游戏包名，尤其不要凭游戏名称假定包名为 `com.tencent.naruto`。扫码实现当前默认值是 **`com.tencent.KiHan`**，以实际安装结果为准。

```powershell
& $Adb -s $DeviceSerial shell pm list packages KiHan
# 未找到或使用其他发行版本时，再查看第三方应用并核对具体游戏
& $Adb -s $DeviceSerial shell pm list packages -3
```

扫码 worker 可用 `LDPLAYER_PACKAGE` 覆盖实际包名。不同发行版本的登录 UI 也需要另行验证。

worker 会重开游戏并导航到扫码登录页面。长期未登录账号的回归活动、额外引导等情况目前未完整兼容，应先由用户处理，不能用完整任务当作首次安装验收。

## 6. 核对共享日常配置

服务统一使用根目录 `config.example.json`，保留完整 `tasks` 和 `assets/` 引用，不复制本地任务配置。机器参数通过 `.env` 配置；worker 使用雷电控制台同目录下的 `adb.exe` 和分配实例的真实 serial，覆盖日常配置中的对应字段，无需把示例 ADB 路径替换成新机器路径。

```powershell
$ConfigPath = Join-Path (Get-Location).Path 'config.example.json'
$TaskConfig = Get-Content -Raw -Encoding UTF8 -LiteralPath $ConfigPath | ConvertFrom-Json
$TaskConfig.screen
```

`screen` 的宽、高必须为 1920、1080，实际模拟器尺寸和 DPI 仍需单独验收。调整任务时直接修改共享文件，采用 UTF-8 无 BOM，保留完整 `tasks`。资源路径相对 JSON 所在目录解析，保持根目录 JSON 与 `assets/` 的相对关系。

## 7. 配置 `.env` 和 Cloudflare KV

### 本地 `.env`

编辑根目录现有 `.env`，按 `.env.example` 填写：

```dotenv
CLOUDFLARE_API_TOKEN=用户提供的只读Token
CLOUDFLARE_ACCOUNT_ID=用户的32位十六进制账户ID
CLOUDFLARE_KV_NAMESPACE_ID=用户的32位十六进制命名空间ID
```

Cloudflare Token 需要目标账户的 **Workers KV Storage → Read** 权限。这里的中文均为待填写说明，不是可运行凭据；扫码服务不要求 `LOGIN_ACCOUNT_NAME`。

### Cloudflare KV

以下每个变量名对应命名空间中的一个 key，值保存为**纯文本**，不要把整段 dotenv 或 JSON 放进一个 key：

| KV key | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` | 本指南使用 DeepSeek，key 和 model 必填 |
| `REDIS_URL` | 扫码服务的任务队列与模型路由需要；地址须为 `redis://` 或 `rediss://` |
| `REDIS_QUEUE_KEY` | 扫码队列名，默认 `naruto:login:tasks` |
| `OSS_REGION`、`OSS_BUCKET`、`OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET` | 扫码 worker 必填 |
| `OSS_STS_TOKEN` | 使用临时 OSS 凭据时填写 |
| `OSS_KEY_PREFIX` | OSS 对象前缀，默认 `login` |

按本指南配置 DeepSeek，key 和 model 必须成对且非空。模型与图片请求兼容性按当前网关验证，不要自行添加 API 地址变量。

**配置加载规则必须遵守：**

1. `naruto_daily/common/environment.py` 在服务启动时读取 Cloudflare KV；读取失败则停止，不提供离线回退。实例池管理命令也会读取 KV。
2. DeepSeek、Redis、OSS 不从本地 `.env` 回退，写入这些本地变量不能替代 KV；旧的同名进程变量也不会被保留作为兜底。
3. 普通机器参数优先使用已有进程环境，再读本地 `.env`；KV 若包含同名机器参数，加载后会覆盖它们。建议不同机器的雷电路径和实例索引保留在各自 `.env`。
4. 当前代码枚举命名空间全部 key。key 名需符合大写环境变量格式，值需可读取为文本；不要将 Cloudflare 连接凭据或内部 `_NARUTO_CLOUD_CONFIG_LOADED` 标记存入该命名空间。
5. 修改配置后重启对应进程。不要在验收输出中打印 `.env` 全文、KV 值、Redis 含密码的地址或 OSS 密钥。

## 8. 配置扫码服务实例池

推荐先配置一个专用实例，再扩展池；先前章节中的显示和 ADB 设置必须对池内每个实例逐一完成。

在根目录 `.env` 添加或更新以下机器参数，路径和索引必须采用实际结果：

```dotenv
EMULATOR_DRIVER=ldplayer
LDPLAYER_CONSOLE_PATH='C:\LDPlayer\LDPlayer9\ldconsole.exe'
LDPLAYER_POOL_INDICES=0
WORKER_CONCURRENCY=1
LDPLAYER_PACKAGE=com.tencent.KiHan
DAILY_CONFIG_PATH=config.example.json
```

- `.env.example` 中默认没有启用真实驱动，实际默认是 `mock`；mock 二维码不能登录游戏。
- `LDPLAYER_POOL_INDICES` 是 `list2` 的现有实例索引，用逗号分隔、不能重复；固定池支持索引 `0`，不要照搬旧复制模式的限制。示例 `0,2` 只有在这两个实例真实存在且均已配置好时才能使用。
- 配置固定池后不需要 `LDPLAYER_TEMPLATE`，也不会复制实例。并发数建议不超过已准备实例数，实际占用不能超过池容量。
- 每个实例预装游戏并完成资源更新，交给 worker 前退出已有账号到登录页并停止实例；不要让实例池接管正在手动游玩的实例。worker 会启动、操作并在结束后关闭它们，不删除池实例；同一个实例不能同时交给多个任务或人工操作。
- worker 使用控制台同目录下的 `adb.exe`，并以分配实例的 serial 和 ADB 路径覆盖日常 JSON 的对应字段。
- `DAILY_CONFIG_PATH` 使用默认值 `config.example.json`，任务改动直接维护该文件。任务 JSON 和完整 `assets/` 必须同时保留。
- 状态目录默认 `.data/login-service/ldplayer`，不要删除它来强行解除占用。只查看占用可运行下面的 `list`；释放实例是另一个操作，应核实具体任务后按模块文档处理。

```powershell
.\.venv\Scripts\python.exe -m naruto_daily.login_service.pool_admin list
```

该命令也会加载 KV，并要求 worker 配置完整，包括 OSS。网站客户端与 worker 要使用相同 Redis 数据库、队列、OSS Bucket、地域和对象前缀；worker 不开放 HTTP 服务，无需为了它开放本机 4320 端口。

兼容的旧复制模式仅在用户明确需要时使用：留空 `LDPLAYER_POOL_INDICES`，配置 `LDPLAYER_TEMPLATE`，准备一个已安装游戏、显示配置正确、退出账号且停止的干净模板；模板索引不能是 `0`。具体生命周期见 `naruto_daily/login_service/README.md`。

## 9. 环境验收和交付

先验收 ADB、尺寸、DPI，再验证 Python 依赖：

```powershell
.\.venv\Scripts\python.exe -m pip check
```

按服务实际使用的规则检查共享日常 JSON，跳过保存账号登录前缀：

```powershell
.\.venv\Scripts\python.exe -c "from pathlib import Path; from naruto_daily.config import load_settings; load_settings(Path('config.example.json'), skip_account_login=True); print('任务配置检查通过')"
```

JSON 检查会读取 Cloudflare KV，不执行游戏点击或模型请求，也不连接 Redis 或 OSS，不能证明 Redis、OSS 或模型接口已联通或 DPI 正确；明确记录验收范围。

可在游戏登录页保存原始截图，核对尺寸和横屏布局。使用 `pull` 传输 PNG，避免旧 PowerShell 的二进制重定向损坏图片：

```powershell
New-Item -ItemType Directory -Force .\artifacts | Out-Null
& $Adb -s $DeviceSerial shell screencap -p /sdcard/naruto-setup.png
& $Adb -s $DeviceSerial pull /sdcard/naruto-setup.png .\artifacts\setup-screen.png
.\.venv\Scripts\python.exe -c "from PIL import Image; print(Image.open('artifacts/setup-screen.png').size)"
```

尺寸应为 `(1920, 1080)`。截图可能包含账号或二维码，只保留本地，不提交到 Git。

仅在用户要求实际启动时，使用唯一入口 `启动扫码服务.cmd`。它运行常驻 Redis worker，生成二维码并上传 OSS，扫码后执行日常；按 `Ctrl+C` 停止。

最终向用户报告实际 Python/虚拟环境路径、雷电版本与安装路径、目标实例索引、ADB serial、尺寸和 DPI 实测值、已通过的预检和仍缺少的凭据或手动步骤。没有通过的项目应标为未验证，不能用“文件已写入”替代“环境可运行”。

## 10. 常见失败的排查顺序

| 现象 | 先检查 |
| --- | --- |
| 找不到 `adb.exe` / 控制台 | `.env` 中的实际控制台路径及同目录 `adb.exe`；不要沿用示例路径 |
| 无设备、`offline`、`unauthorized` | 实例启动状态、ADB 本地连接、保存后重启、目标 serial 和授权提示 |
| 多个在线设备 | 核对实例池索引、任务绑定和对应 serial；worker 必须操作分配到的实例 |
| 分辨率错误或点击偏移 | 每个实例的 1920×1080、DPI 240、方向、尺寸/密度覆盖及原始截图 |
| 缺少 Cloudflare 凭据或 KV 读取失败 | 三个本地连接变量、32 位 ID、Token 权限、网络和命名空间全部 key 的格式 |
| 没有有效模型渠道或缺少 Redis | KV 中的 `DEEPSEEK_API_KEY`、`DEEPSEEK_MODEL` 和 `REDIS_URL` |
| 扫码显示模拟二维码 | `EMULATOR_DRIVER` 是否实际设为 `ldplayer`；是否被进程环境或 KV 覆盖 |
| worker 找不到游戏或池实例 | 实际包名、`list2` 索引、每个实例的安装状态和显示配置 |
| worker 启动后没有任务 | 客户端与 worker 是否使用相同 Redis 数据库和队列；不要盲目重建实例或删除状态 |
| 首次登录或回归弹窗导致中断 | 游戏资源更新、额外引导和登录起始页；先由用户处理未兼容页面 |

维护依据：`naruto_daily/config.py`、`common/adb_client.py`、`common/environment.py`、`common/cloudflare_kv.py`、`multimodal/gateway.py`、`login_service/config.py`、`login_service/ldplayer.py`、`login_service/daily.py` 和根目录 `启动扫码服务.cmd`。
