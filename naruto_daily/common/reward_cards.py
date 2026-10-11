"""Locate safe reward-card drag points and verify actual horizontal movement.

The supported reward page is the existing 1920 by 1080 layout. Only its
card bodies are inspected; the sidebar, account HUD and animated boxes are
excluded. Uncertain pages or motion produce no actionable result.
"""

from __future__ import annotations

import cv2
import numpy as np


def _frame(png: bytes) -> np.ndarray | None:
    if not isinstance(png, bytes) or not png:
        return None
    try:
        frame = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    except (cv2.error, ValueError):
        return None
    return frame if frame is not None and frame.shape == (1080, 1920, 3) else None


def _gold_ranges(frame: np.ndarray, top: int, bottom: int) -> list[tuple[int, int]]:
    hsv = cv2.cvtColor(frame[top:bottom], cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, np.array((15, 45, 150)), np.array((40, 255, 255)))
    columns = (np.mean(gold > 0, axis=0) >= 0.75).astype(np.uint8)[None, :]
    # Close tiny texture holes without joining the roughly 30px card gaps.
    columns = cv2.morphologyEx(columns, cv2.MORPH_CLOSE, np.ones((1, 7), np.uint8))[0]
    edges = np.diff(np.pad(columns.astype(np.int8), (1, 1)))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return [(int(left), int(right)) for left, right in zip(starts, ends)
            if left > 300 and right < 1920 and 280 <= right - left <= 380]


def _card_ranges(frame: np.ndarray) -> list[tuple[int, int]]:
    # Both strips cross plain card background, avoiding each card's icon.
    upper = _gold_ranges(frame, 215, 240)
    lower = _gold_ranges(frame, 430, 445)
    cards = [(max(left, other_left), min(right, other_right))
             for left, right in upper for other_left, other_right in lower
             if abs(left - other_left) <= 8 and abs(right - other_right) <= 8]
    gray = cv2.cvtColor(frame[230:520], cv2.COLOR_BGR2GRAY)
    cards = [(left, right) for left, right in cards
             if float(gray[:, left + 55:right - 55].std()) >= 12]
    return cards if len(cards) >= 2 else []


def reward_card_swipe_starts(png: bytes) -> list[tuple[int, int]]:
    """Return current full-card centers, ordered by proximity to x1450.

    Each point uses y550 and leaves a 450px leftward endpoint inside the
    list (x >= 300). Empty output means this page could not be confirmed.
    """
    frame = _frame(png)
    if frame is None:
        return []
    centers = [((left + right - 1) // 2, 550) for left, right in _card_ranges(frame)]
    return sorted((point for point in centers if point[0] - 450 >= 300),
                  key=lambda point: (abs(point[0] - 1450), -point[0]))


def _patch_displacements(before: np.ndarray, after: np.ndarray,
                         cards: list[tuple[int, int]]) -> list[float]:
    # Every patch contains its own icon and task title, not the repeated
    # border or "go now" button. Different cards supply independent votes.
    search = after[215:535, 300:1920]
    displacements = []
    for left, right in cards:
        patch_left = left + 55
        patch = before[230:520, patch_left:right - 55]
        if float(patch.std()) < 12:
            continue
        scores = cv2.matchTemplate(search, patch, cv2.TM_CCOEFF_NORMED)
        _, best, _, location = cv2.minMaxLoc(scores)
        if best < 0.9 or abs(215 + location[1] - 230) > 3:
            continue
        # A second distinct horizontal peak indicates a repeated/ambiguous
        # icon. Adjacent pixels around the same peak are one candidate.
        other_scores = scores.copy()
        other_scores[:, max(0, location[0] - 25):location[0] + 26] = -1
        if best - float(other_scores.max()) < 0.08:
            continue
        displacement = float(300 + location[0] - patch_left)
        if abs(displacement) <= 850:
            displacements.append(displacement)
    return displacements


def _aligned_content_agrees(before: np.ndarray, after: np.ndarray,
                            displacement: float) -> bool:
    shift = round(displacement)
    left, right = max(300, 300 - shift), min(1920, 1920 - shift)
    if right - left < 600:
        return False
    original = before[215:580, left:right].astype(np.float32)
    moved = after[215:580, left + shift:right + shift].astype(np.float32)
    # Verify the shared contents after alignment, so a repeating card frame
    # cannot turn a match one card-width away into a claimed movement.
    original_centered = original - original.mean()
    moved_centered = moved - moved.mean()
    denominator = float(np.linalg.norm(original_centered) * np.linalg.norm(moved_centered))
    if denominator <= 0:
        return False
    correlation = float(np.sum(original_centered * moved_centered) / denominator)
    return correlation >= 0.9 and float(np.mean(np.abs(original - moved))) <= 18


def reward_card_displacement(before: bytes, after: bytes) -> float | None:
    """Return verified card movement in pixels: left negative, stationary 0.

    At least two distinct card patches must agree, with a final aligned
    content check. None means movement or the reward page is unconfirmed.
    """
    first, second = _frame(before), _frame(after)
    if first is None or second is None:
        return None
    cards = _card_ranges(first)
    if not cards or not _card_ranges(second):
        return None
    first_gray = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
    second_gray = cv2.cvtColor(second, cv2.COLOR_BGR2GRAY)
    votes = _patch_displacements(first_gray, second_gray, cards)
    if len(votes) < 2:
        return None
    groups = [[vote for vote in votes if abs(vote - candidate) <= 8] for candidate in votes]
    consensus = max(groups, key=len)
    if len(consensus) < 2 or any(len(group) >= 2 and abs(np.median(group) - np.median(consensus)) > 8
                                 for group in groups):
        return None
    displacement = float(np.median(consensus))
    return displacement if _aligned_content_agrees(first_gray, second_gray, displacement) else None
