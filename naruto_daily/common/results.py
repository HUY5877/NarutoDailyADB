"""界面清理与回主界面共用的结果类型。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PopupCleanupSummary:
    rounds: int
    clicks: int
