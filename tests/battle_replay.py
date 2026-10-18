"""固定旧四键坐标回放，避免共享示例的键盘模式触及真实窗口。"""

from naruto_daily.config import load_settings


def load_legacy_adb_settings(path):
    settings = load_settings(path)
    for actions in settings.tasks.values():
        for action in actions:
            if "battle" not in action:
                continue
            action["battle"]["input_mode"] = "adb"
            action["battle"]["buttons"] = [
                dict(name="一技能", x=1500, y=960),
                dict(name="二技能", x=1535, y=750),
                dict(name="大招", x=1725, y=650),
                dict(name="普攻", x=1715, y=885),
            ]
    return settings
