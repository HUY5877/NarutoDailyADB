# 通用视觉导航 agent

由具体业务模块通过 Python 代码调用，使用 `pydantic-ai-slim[openai]==2.41.0`。
安装项目时由 pip 自动安装，不需要额外运行时。

`runner.py` 用 Pydantic AI 的 `Agent.run()` 驱动完整会话和工具循环：
截图 → 模型在同一次工具调用中给出画面事实与动作 → 本地判断事实 → 单次 WASD 移动 → 等待 0.5 秒 → 重新截图。
`move` 是 SDK 注册的函数工具，返回移动后的图片；`finish` 是 SDK 的输出工具。
两个工具都直接接收结构化事实字段，`move` 另外接收 `direction`，完成状态由本地规则推导。
原生工具参数使用平铺字段，不嵌套 `evidence` 对象或 JSON 字符串。
SDK hooks 检查每轮只有一个完整工具调用、每次请求只携带最新截图。
SDK 严格验证参数并限制请求次数；移动工具在执行 ADB 前检查页面、完成条件和移动上限。

模型、接口地址、超时和密钥复用已有 `MultimodalClient` 配置，在同一 Python 进程中使用。
服务需支持 OpenAI 兼容的 Chat Completions、图片和原生 `tools`。
原有昵称、挑战券等 JSON 图片识别继续使用现有客户端。

## 秘境内部调用

现有秘境任务已经接入，无需额外启动命令或传命令行参数：

1. 出战后的加载与提示处理完成，`secret_realm/pre_battle.py` 截图识别顶部副本名称。
2. 阴阳秘境调用 `exit_secret_battle()`，执行暂停、退出战斗、确认。
3. 毒风秘境调用 `secret_realm/navigation.py` 的 `navigate_to_poison_seal()`，传入目标、摇杆配置与毒风事实判定规则。
4. 每轮模型在 `move` 或 `finish` 参数中给出 `page`、`boss_visible`、`battle_started`、`seal_prompt_visible`、`visible_text` 和 `observation`。
5. 本地规则确认当前是战斗页面、待解封提示明确消失，并有 BOSS 或实际交战的正向证据，才进入通用战斗模块。即使模型请求 `move`，事实已经满足完成条件时也直接成功，不再多移动一步。
6. 满 50 步仍在战斗页面且未确认完成，则复用退出函数。主动退出后回房间重新读券，不计入通关次数，也不要求券数下降。

中央仍显示“解开『封印』，召唤 BOSS”或提示是否消失未知时，状态仍为待完成。
`seal_prompt_visible`、`boss_visible`、`battle_started` 严格使用 `yes`、`no`、`unknown` 三选一字符串，分别表示事实成立、不成立和无法确认，不接受布尔值或字符串 `True` / `False`。
人物脚下的蓝色选中圈、靠近疑似封印和提示消失本身都不足以放行。
失败、胜利、结算、其他或无法确认的页面立即停止，即使模型同时请求移动，也不会再导航、攻击或尝试暂停退出。
画面事实和动作来自同一次 SDK 模型响应，由代码检查事实之间及事实与动作之间的一致性。
完成检查不会另拍截图或发起独立视觉请求。

其他秘境继续原有战斗流程。副本名称无法识别、导航模型报错、非法动作或退出失败时，
抛出异常并停止，不进入战斗或重复出战。

## 在其他模块复用

```python
from naruto_daily.multimodal.agent import run_agent

# 业务模块提供已连接的 adb、已配置的 model，以及目标和当前布局的摇杆位置。
result = run_agent(adb, model, goal_prompt, joystick=joystick, max_steps=50)
if result.reached:
    continue_task()
elif result.status == 'max_steps':
    exit_task()
```

默认通用工具直接使用 `{"status": "pending", "observation": "可见依据"}` 等事实字段，
其中 `status` 必须选择 `pending`、`reached` 或 `blocked` 中的一个值。
毒风模块提供专用事实字段和本地判定规则，不接收模型自报的 `reached` 布尔值。

W/A/S/D 对应上/左/下/右，实际通过 ADB 从摇杆中心向目标方向滑动一次。
项目使用固定 1920×1080 布局，秘境模块中的摇杆中心 `(320, 840)` 来自已有战斗参考截图，
半径 80、单次滑动 300 毫秒。布局变化时由代码中的场景配置调整，不交由模型生成点击坐标。
当前坐标已核对参考截图，但毒风实机导航尚未验证。

最多 50 次移动，正常最多 51 次模型请求，包括最后移动后的画面判断。
如果事实仍为待完成，模型却提前调用 `finish`，SDK 使用 `ModelRetry` 在同一会话、同一截图上要求修正，
整个导航最多允许 3 次此类重试，因此总请求上限为 54 次，已执行步数不重置。
持续矛盾超过额度则停止，不进入战斗。第 50 步之后未完成时，`finish` 或再次请求 `move` 均返回 `max_steps`，不会执行第 51 步。
最后一帧事实确认完成仍视为成功；非战斗状态优先停止，不会作为正常步数耗尽去点击退出。

每轮只接受一个工具调用，纯文本“完成”不算成功；错误参数、一次请求多个动作或不完整响应均停止。
`AgentResult.history` 是已执行动作及其移动前观察，`observation` 是最终事实判定依据。
网络和设备失败不会重试动作。成功、异常和取消都会关闭 HTTP 连接。

测试：`python -m pytest` 通过本地 OpenAI 兼容服务运行真实 SDK 工具循环，并覆盖秘境业务分支。
测试将 SDK 回调与原有 JSON 识别分开，确保导航不会偷偷增加独立视觉请求。
真实毒风移动效果尚未实机验证。

官方接口参考：[精简安装](https://pydantic.dev/docs/ai/overview/install/)、
[Agent](https://pydantic.dev/docs/ai/core-concepts/agent/)、
[工具](https://pydantic.dev/docs/ai/tools-toolsets/tools-advanced/)。
