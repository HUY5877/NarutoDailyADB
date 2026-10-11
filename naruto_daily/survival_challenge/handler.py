"""生存挑战：进入奖励卡片后执行扫荡、等待和按模型读取重置次数。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from naruto_daily.common.recognition import ImageRecognition
from naruto_daily.common.execution import check_execution, current_execution, retry_observation, stage_timeout
from naruto_daily.common.visual import VisualError, find_template_in_png
from naruto_daily.multimodal.client import MultimodalClient
from naruto_daily.multimodal.prompts import SURVIVAL_RESET_COUNT


LOGGER = logging.getLogger("naruto_daily")
# 1920×1080：点击传送弹层下方、底部操作栏上方的空白处；弹层内部点击不会关闭。
TRANSFER_DISMISS = (960, 850)
BUTTON_TIMEOUT_SECONDS = 20.0
START_SWEEP_MAX_RETRIES = 2
START_SWEEP_RETRY_SECONDS = 2.0
SWEEP_FOREGROUND_STAGES = ('ready', 'confirm_first', 'confirm_second')


def _wait_and_click(client, images: ImageRecognition, target: dict, *, deadline: float,
                    poll_seconds: float, post_click_wait: float,
                    elapsed: Callable[[], float], skip_if: dict | None = None,
                    page_guard: Callable[[bytes], bool] | None = None,
                    resume_if: Callable[[bytes], str | None] | None = None) -> bool | str:
    def probe(png):
        if resume_if is not None:
            stage = resume_if(png)
            if stage is not None:
                return None, 0.0, False, stage
        if page_guard is not None and not page_guard(png):
            return None, 0.0, False, None
        # 先确认前景的扫荡提示，不能点击弹窗后方变暗的“准备就绪”。
        if skip_if is not None:
            center, score = images.match(png, skip_if)
            if center is not None:
                return None, score, True, None
        center, score = images.match(png, target)
        return center, score, False, None

    deadline = min(deadline, elapsed() + stage_timeout(BUTTON_TIMEOUT_SECONDS))
    round_number = 0
    score = 0.0
    while elapsed() < deadline:
        round_number += 1
        remaining = deadline - elapsed()
        LOGGER.info("[生存挑战：%s 第%d轮] 等待按钮，剩余 %.2f 秒",
                    target['name'], round_number, remaining)
        name = target['name'] if skip_if is None else f"{target['name']} / {skip_if['name']}"
        center, score, skip, resume_stage = images.check(
            probe, lambda result: result[0] is not None or result[2] or result[3] is not None, name,
            deadline=images.elapsed + remaining,
        )
        # ADB 截图和模板计算也计入本步骤预算，迟到的匹配结果不能触发点击。
        if elapsed() >= deadline:
            break
        if resume_stage is not None:
            LOGGER.info('等待%s期间已进入%s，未点击%s，继续处理当前扫荡页面',
                        target['name'], resume_stage, target['name'])
            return resume_stage
        if skip:
            LOGGER.info("已出现%s，名单已确认，跳过准备就绪和名单确认", skip_if['name'])
            return False
        if center is not None:
            LOGGER.info("确认%s，相似度 %.3f，点击坐标：(%d, %d)",
                        target["name"], score, *center)
            client.tap(*center)
            if post_click_wait:
                images.sleep(post_click_wait)
            return True
        LOGGER.info("未找到%s，相似度 %.3f，阈值 %.3f",
                    target['name'], score, target['threshold'])
        remaining = deadline - elapsed()
        if remaining > 0:
            images.sleep(min(poll_seconds, remaining))
    raise VisualError(f"等待生存挑战按钮{target['name']}超时，最后相似度 {score:.3f}，未继续点击")


def _dismiss_transfer_if_present(client, images: ImageRecognition, target: dict, *,
                                 deadline: float = float('inf'),
                                 elapsed: Callable[[], float] | None = None) -> None:
    if elapsed is None:
        elapsed = lambda: images.elapsed
    deadline = min(deadline, elapsed() + stage_timeout(BUTTON_TIMEOUT_SECONDS))
    clicks = 0
    while elapsed() < deadline:
        center, score = retry_observation(
            lambda: images.match(client.screenshot(), target), name='生存挑战传送提示',
        )
        check_execution()
        if elapsed() >= deadline:
            break
        if center is None:
            if clicks:
                LOGGER.info("重新截图确认“传送”提示已关闭，继续生存挑战")
            else:
                LOGGER.info("未发现包含“传送”的提示界面，继续生存挑战")
            return
        clicks += 1
        LOGGER.info("%s“传送”提示界面，相似度 %.3f，第%d次点击弹层外空白处 (%d, %d) 退出，随后重新截图确认",
                    '发现' if clicks == 1 else '仍发现', score, clicks, *TRANSFER_DISMISS)
        client.tap(*TRANSFER_DISMISS)
        images.sleep(min(1, max(0, deadline - elapsed())))
    raise VisualError("关闭生存挑战传送提示超时，未继续开始扫荡")


def _sweep_page(images: ImageRecognition, action: dict, png: bytes, *, check_map=False):
    # Foreground dialogs take precedence over buttons left visible behind them.
    for key, stage in [('sweep_prompt', 'confirm_second'), ('confirm_second', 'confirm_second'),
                       ('confirm_first', 'confirm_first'), ('ready', 'ready')]:
        if key in action:
            center, score = images.match(png, action[key])
            if center is not None:
                return stage, center, score
    if check_map:
        for key in ('transfer_marker', 'reset_confirm'):
            if key in action and images.match(png, action[key])[0] is not None:
                return None, None, 0.0
    center, score = images.match(png, action['start_sweep'])
    if center is not None:
        if check_map and images.match(png, action['reset'])[0] is not None:
            return 'map', center, score
        return 'start_sweep', center, score
    return None, None, 0.0


def _start_sweep_and_wait(client, images: ImageRecognition, action: dict, *, deadline: float,
                          poll_seconds: float, post_click_wait: float,
                          elapsed: Callable[[], float]) -> str:
    scoped = current_execution() is not None
    start_guard = (lambda png: _sweep_page(images, action, png, check_map=True)[0]
                   in ('start_sweep', 'map')) if scoped else None
    _wait_and_click(client, images, action['start_sweep'], deadline=deadline,
                    poll_seconds=poll_seconds, post_click_wait=post_click_wait, elapsed=elapsed,
                    page_guard=start_guard)
    deadline = min(deadline, elapsed() + stage_timeout(BUTTON_TIMEOUT_SECONDS))
    retry_at = elapsed() + START_SWEEP_RETRY_SECONDS
    retries = 0

    def probe(png):
        stage, center, score = _sweep_page(images, action, png, check_map=scoped)
        if stage == 'map':
            stage = ('reset' if retries >= START_SWEEP_MAX_RETRIES and elapsed() >= retry_at
                     else 'start_sweep')
        return stage, center, score

    while elapsed() < deadline:
        remaining = deadline - elapsed()
        LOGGER.info('[生存挑战：开始扫荡后] 确认页面切换，剩余 %.2f 秒', remaining)
        stage, center, score = images.check(
            probe, lambda result: result[0] is not None, '扫荡后的名单/确认页面或开始扫荡',
            deadline=images.elapsed + remaining,
        )
        if elapsed() >= deadline:
            break
        if stage == 'reset':
            LOGGER.info('开始扫荡补点%d次后仍确认在生存地图，转入重置次数检查，未继续补点', retries)
            return stage
        if stage is not None and stage != 'start_sweep':
            LOGGER.info('已确认%s页面，停止补点开始扫荡，继续处理当前页面', action[stage]['name'])
            return stage
        if stage == 'start_sweep' and elapsed() >= retry_at:
            if retries >= START_SWEEP_MAX_RETRIES:
                if current_execution() is None:
                    LOGGER.info('开始扫荡补点%d次后仍停留原页面，本轮无法继续扫荡，转入重置次数检查', retries)
                    return 'reset'
                LOGGER.info('开始扫荡补点%d次后仍停留原页面，继续截图等待页面切换，未继续补点', retries)
            else:
                retries += 1
                LOGGER.info('仍在开始扫荡页面，相似度 %.3f，第%d/%d次补点，坐标：(%d, %d)',
                            score, retries, START_SWEEP_MAX_RETRIES, *center)
                client.tap(*center)
                retry_at = elapsed() + START_SWEEP_RETRY_SECONDS
                if post_click_wait:
                    images.sleep(min(post_click_wait, max(0, deadline - elapsed())))
        elif stage is None:
            LOGGER.info('尚未确认扫荡后的页面，继续等待，不补点')
        remaining = deadline - elapsed()
        if remaining > 0:
            images.sleep(min(poll_seconds, remaining))
    raise VisualError('开始扫荡后等待名单或扫荡确认页面超时，未继续点击')


def _read_reset_count(model: MultimodalClient, client, *, timeout_seconds: float,
                      page_probe: Callable[[bytes], str | None] | None = None) -> int | str:
    def observe():
        png = client.screenshot()
        if page_probe is not None:
            stage = page_probe(png)
            if stage in SWEEP_FOREGROUND_STAGES:
                return stage
            if stage != 'map':
                raise VisualError('尚未确认生存地图，继续截图，未读取或操作重置')
        result = model.complete_json(png, SURVIVAL_RESET_COUNT,
                                     timeout_seconds=stage_timeout(timeout_seconds))
        if page_probe is not None:
            stage = page_probe(client.screenshot())
            if stage in SWEEP_FOREGROUND_STAGES:
                LOGGER.info('读取重置次数期间已进入%s，忽略旧读数，继续处理当前扫荡页面', stage)
                return stage
            if stage != 'map':
                raise VisualError('读取重置次数后地图状态未确认，继续截图，未重置或结束')
        count = result.get("reset_count") if isinstance(result, dict) else None
        if type(count) is not int or count < 0:
            raise VisualError("大模型未能确认生存挑战重置次数")
        return count

    count = retry_observation(observe, name='生存挑战重置次数')
    if type(count) is int:
        LOGGER.info("大模型识别生存挑战重置次数：%d", count)
    return count


def handle_survival_challenge(
    client, action: dict, *, model: MultimodalClient | None = None,
    template_matcher: Callable = find_template_in_png,
    asset_root: Path = Path("."), sleep: Callable[[float], None] = time.sleep,
) -> None:
    images = ImageRecognition(client, template_matcher, asset_root, sleep)
    started = time.monotonic()

    def elapsed() -> float:
        # 实际运行计入 ADB/识别/模型耗时；测试中的替代 sleep 仍推进等待预算。
        return max(images.elapsed, time.monotonic() - started)

    if model is None:
        model = MultimodalClient(action["multimodal"], asset_root=asset_root)
    deadline = elapsed() + stage_timeout(float(action["timeout_seconds"]))
    post_click_wait = float(action.get("post_click_wait_seconds", 1))
    poll_seconds = float(action["poll_seconds"])
    page_probe = lambda png: _sweep_page(images, action, png, check_map=True)[0]

    def forward_stage(png):
        stage = page_probe(png)
        return stage if stage in SWEEP_FOREGROUND_STAGES else None

    round_number = 0
    while current_execution() is not None or round_number < int(action["max_rounds"]):
        check_execution()
        round_number += 1
        LOGGER.info("[生存挑战第%d轮] 关闭可选传送提示→开始扫荡→处理名单/扫荡确认→等待%d秒→检查重置次数",
                    round_number, int(action["wait_seconds"]))
        _dismiss_transfer_if_present(client, images, action["transfer_marker"],
                                     deadline=deadline, elapsed=elapsed)
        stage = _start_sweep_and_wait(
            client, images, action, deadline=deadline, poll_seconds=poll_seconds,
            post_click_wait=post_click_wait, elapsed=elapsed,
        )
        while True:
            count = None
            if stage == 'reset' and current_execution() is not None:
                result = _read_reset_count(model, client, timeout_seconds=deadline - elapsed(),
                                           page_probe=page_probe)
                if type(result) is int:
                    count = result
                else:
                    stage = result
            if stage == 'reset':
                LOGGER.info('本轮未进入扫荡，跳过名单/扫荡确认和等待，直接检查能否重置')
            else:
                if stage == 'ready':
                    roster_open = _wait_and_click(
                        client, images, action['ready'], deadline=deadline, poll_seconds=poll_seconds,
                        post_click_wait=post_click_wait, elapsed=elapsed, skip_if=action.get('sweep_prompt'),
                    )
                else:
                    roster_open = stage == 'confirm_first'
                if roster_open:
                    _wait_and_click(client, images, action['confirm_first'], deadline=deadline,
                                    poll_seconds=poll_seconds, post_click_wait=post_click_wait, elapsed=elapsed)
                _wait_and_click(client, images, action['confirm_second'], deadline=deadline,
                                poll_seconds=poll_seconds, post_click_wait=post_click_wait, elapsed=elapsed)
                if current_execution() is None and elapsed() + float(action["wait_seconds"]) >= deadline:
                    raise VisualError("生存挑战等待扫荡完成超时")
                images.sleep(float(action["wait_seconds"]))
            remaining = deadline - elapsed()
            if remaining <= 0:
                raise VisualError("生存挑战等待扫荡完成超时")
            if count is None:
                count = _read_reset_count(model, client, timeout_seconds=remaining)
            if elapsed() >= deadline:
                raise VisualError("生存挑战重置次数识别超时，未继续点击")
            if count == 0:
                LOGGER.info("生存挑战重置次数为0，结束扫荡循环")
                return
            LOGGER.info("生存挑战剩余重置次数为%d，点击重置并确认后回到下一轮扫荡", count)
            reset_result = _wait_and_click(
                client, images, action["reset"], deadline=deadline,
                poll_seconds=poll_seconds, post_click_wait=post_click_wait, elapsed=elapsed,
                page_guard=(lambda png: page_probe(png) == 'map')
                if current_execution() is not None else None,
                resume_if=forward_stage if current_execution() is not None else None,
            )
            if isinstance(reset_result, str):
                # The last START responded before RESET: finish that sweep using fresh state.
                stage = reset_result
                continue
            break
        _wait_and_click(client, images, action["reset_confirm"], deadline=deadline,
                        poll_seconds=poll_seconds, post_click_wait=post_click_wait,
                        elapsed=elapsed)
    raise VisualError(f"生存挑战达到最多 {action['max_rounds']} 轮，未能结束")
