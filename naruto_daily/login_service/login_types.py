"""Login channel values shared with the web client's queue protocol."""

DEFAULT_LOGIN_TYPE = "android_qq"
LOGIN_TYPES = (DEFAULT_LOGIN_TYPE, "ios_qq", "android_wechat", "ios_wechat")
LOGIN_LABELS = {
    "android_qq": "安卓 QQ 区", "ios_qq": "iOS QQ 区",
    "android_wechat": "安卓微信区", "ios_wechat": "iOS 微信区",
}


def parse_login_type(value=DEFAULT_LOGIN_TYPE) -> str:
    if not isinstance(value, str) or value not in LOGIN_TYPES:
        raise ValueError("Invalid login type")
    return value
