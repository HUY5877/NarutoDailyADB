package com.huy.authlogin;

/** The game SDK's verified startActivityForResult contract. */
public final class RequestPolicy {
    public static final String GAME_PACKAGE = "com.tencent.KiHan";

    private RequestPolicy() {}

    public static boolean accepts(String caller, String action, String appId) {
        return GAME_PACKAGE.equals(caller)
                && "action_login".equals(action)
                && "1104307008".equals(appId);
    }
}
