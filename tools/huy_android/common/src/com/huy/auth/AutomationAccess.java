package com.huy.auth;

/** ADB is the local trust boundary; ordinary installed apps are never authorized. */
public final class AutomationAccess {
    private AutomationAccess() {}
    public static boolean allowedUid(int caller, int own) { return caller == 0 || caller == 2000 || caller == own; }
}
