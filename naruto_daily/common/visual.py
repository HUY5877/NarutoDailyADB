from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


class VisualError(RuntimeError):
    """当前截图无法解析或尚未确认；观察可以重新截图。"""


class NonRetryableVisualError(VisualError):
    """资源或配置无效，重新观察无法恢复。"""


class TemplateConfigurationError(NonRetryableVisualError):
    """模板资源或检测区域配置无效，重新截图无法恢复。"""


def _decode_png(data: bytes) -> np.ndarray:
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise VisualError("无法解析 ADB 截图")
    return image


def _load_template(path: Path) -> np.ndarray:
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError as exc:
        raise TemplateConfigurationError(f"无法读取模板图片：{path}") from exc
    template = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if template is None:
        raise TemplateConfigurationError(f"无法解析模板图片：{path}")
    return template


def find_template_in_png(
    png_data: bytes,
    template_path: Path,
    roi: tuple[int, int, int, int],
    threshold: float,
) -> tuple[tuple[int, int] | None, float]:
    screen = _decode_png(png_data)
    loaded_template = _load_template(Path(template_path))
    mask = None
    if loaded_template.ndim == 3 and loaded_template.shape[2] == 4:
        template = loaded_template[:, :, :3]
        alpha = loaded_template[:, :, 3]
        if not np.any(alpha):
            raise TemplateConfigurationError("透明模板没有任何可识别内容")
        mask = cv2.merge((alpha, alpha, alpha))
    else:
        template = loaded_template
    left, top, right, bottom = roi
    region = screen[top:bottom, left:right]
    template_height, template_width = template.shape[:2]
    if region.shape[0] < template_height or region.shape[1] < template_width:
        raise TemplateConfigurationError("模板尺寸大于检测区域")

    if mask is not None:
        result = cv2.matchTemplate(region, template, cv2.TM_CCORR_NORMED, mask=mask)
        result = np.nan_to_num(result, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, maximum, _, maximum_location = cv2.minMaxLoc(result)
        score = float(maximum)
        location = maximum_location
    elif float(template.std()) < 1e-6:
        result = cv2.matchTemplate(region, template, cv2.TM_SQDIFF_NORMED)
        minimum, _, minimum_location, _ = cv2.minMaxLoc(result)
        score = 1.0 - float(minimum)
        location = minimum_location
    else:
        result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _, maximum, _, maximum_location = cv2.minMaxLoc(result)
        score = float(maximum)
        location = maximum_location

    if score < threshold:
        return None, score
    center = (left + location[0] + template_width // 2, top + location[1] + template_height // 2)
    return center, score
