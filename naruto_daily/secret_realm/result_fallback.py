"""结算卡住时用 JEV 识别前景，点击仍由结算流程执行。"""
from __future__ import annotations

import logging
import time

from naruto_daily.common.execution import current_execution, retry_observation, stage_timeout
from naruto_daily.common.visual import VisualError
from naruto_daily.multimodal.diagnostics import model_request_context
from naruto_daily.multimodal.prompt_jev import SECRET_RESULT_FALLBACK


LOGGER = logging.getLogger('naruto_daily')
SECRET_RESULT_FALLBACK_INTERVAL = 10
_LOCAL_PRIORITY = {'room', 'lobby', 'ninja_selection'}


def observe_result_fallback(client, images, model, *, probe, deadline, clock=time.monotonic,
                            on_reward=None):
    """每次网络重试整组重拍；一次正常的 unknown 结果交回外层轮询。"""
    def remaining():
        budget = stage_timeout(deadline - images.elapsed)
        if budget <= 0:
            raise VisualError('秘境结算 JEV 兜底识别超时，未继续点击')
        return budget

    def observe():
        remaining()
        png = client.screenshot()
        state = probe(png)
        remaining()
        if state is not None and state[0] in _LOCAL_PRIORITY:
            LOGGER.info('[秘境结算 JEV 兜底] 新截图已由本地识别为 %s，跳过模型', state[0])
            return state

        started = clock()
        try:
            with model_request_context('秘境结算兜底/jev'):
                # worker 由场景层重做截图与本地检查；独立执行也不能重发旧截图。
                kwargs = {} if current_execution() is not None else {'refresh_image': client.screenshot}
                result = model.complete_json(png, SECRET_RESULT_FALLBACK,
                                             timeout_seconds=remaining(), **kwargs)
        finally:
            if current_execution() is None:
                images.elapsed += max(0, clock() - started)
        remaining()
        page = result.get('page') if isinstance(result, dict) else None
        if not isinstance(page, str) or page not in {'room', 'reward', 'failure', 'unknown'}:
            raise VisualError('秘境结算 JEV 兜底返回了无效页面，未点击')
        LOGGER.info('[秘境结算 JEV 兜底] page=%s，耗时 %.2f 秒', page, max(0, clock() - started))
        if page == 'reward' and on_reward is not None:
            # 请求帧已确认成功，即使随后自动回房，也要保留这次结算证据。
            on_reward()

        # 请求可能耗时数秒；响应后重新检查，防止已回房或进入忍者页仍点旧奖励。
        latest = probe(client.screenshot())
        remaining()
        if latest is not None:
            LOGGER.info('[秘境结算 JEV 兜底] 响应后本地复查为 %s，按最新页面处理', latest[0])
            return latest
        return page, None

    return retry_observation(observe, name='秘境结算 JEV 兜底')
