package com.huy.auth;

import java.net.URI;

/** Immutable one-round server ticket. Do not log or persist this object. */
public final class AutomationContext {
    public final String userId, taskId, attemptId, captureId, captureToken, apiBase, loginType;
    public final long expiresAtEpochMs;
    public AutomationContext(String userId, String taskId, String attemptId, String captureId,
            String captureToken, String apiBase, long expiresAtEpochMs, String loginType, long now) {
        requireId(userId); requireId(taskId); requireId(captureId);
        if (attemptId == null || !attemptId.matches("[a-f0-9]{32}") || captureToken == null
                || !captureToken.matches("[a-f0-9]{64}") || expiresAtEpochMs <= now
                || !("android_qq".equals(loginType) || "ios_qq".equals(loginType))) throw invalid();
        try {
            URI uri = new URI(apiBase);
            if (!"https".equals(uri.getScheme()) || uri.getHost() == null || uri.getRawUserInfo() != null
                    || uri.getRawQuery() != null || uri.getRawFragment() != null
                    || uri.getPort() != -1 && uri.getPort() != 443
                    || !(uri.getRawPath().isEmpty() || "/".equals(uri.getRawPath()))) throw invalid();
        } catch (Exception ignored) { throw invalid(); }
        this.userId=userId; this.taskId=taskId; this.attemptId=attemptId; this.captureId=captureId;
        this.captureToken=captureToken; this.apiBase=apiBase.endsWith("/") ? apiBase.substring(0,apiBase.length()-1) : apiBase;
        this.expiresAtEpochMs=expiresAtEpochMs; this.loginType=loginType;
    }
    public static void requireId(String value) {
        if (value == null || !value.matches("[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}")) throw invalid();
    }
    public boolean same(AutomationContext other) {
        return other != null && userId.equals(other.userId) && taskId.equals(other.taskId)
                && attemptId.equals(other.attemptId) && captureId.equals(other.captureId)
                && captureToken.equals(other.captureToken) && apiBase.equals(other.apiBase)
                && expiresAtEpochMs==other.expiresAtEpochMs && loginType.equals(other.loginType);
    }
    private static IllegalArgumentException invalid() { return new IllegalArgumentException("自动化任务参数无效"); }
}
