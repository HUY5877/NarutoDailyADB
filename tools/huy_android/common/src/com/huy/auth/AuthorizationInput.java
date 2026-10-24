package com.huy.auth;

import java.util.Map;

/** In-memory lookup hints only. This class never creates or extends an authorization. */
public final class AuthorizationInput {
    private final String token;
    private final String openid;

    private AuthorizationInput(String token, String openid) {
        this.token = token;
        this.openid = openid;
    }

    public static AuthorizationInput parse(String value, long now) {
        try {
            if (value == null || value.length() > AuthorizationRules.MAX_BYTES || now < 0) throw invalid();
            String text = CloudRules.trimInputWhitespace(value);
            if (text.matches("[0-9a-fA-F]{16,256}")) return new AuthorizationInput(text, null);
            Map<String, String> fields = AuthorizationRules.lookupFields(text, now);
            return new AuthorizationInput(fields.get("access_token"), fields.get("openid"));
        } catch (IllegalArgumentException ignored) { throw invalid(); }
    }

    public String accessToken() { return token; }

    /** All expiry values here must come from the downloaded cloud authorization. */
    public void validateResolved(String returnedToken, String returnedOpenid, long cloudExpiry, long now) {
        if (!token.equals(returnedToken) || (openid != null && !openid.equals(returnedOpenid))
                || now < 0 || cloudExpiry <= now || cloudExpiry - now < 1000) {
            throw new IllegalArgumentException("云端授权与输入不匹配或已过期，请重新扫码");
        }
    }

    private static IllegalArgumentException invalid() {
        return new IllegalArgumentException("请粘贴 16 至 256 位十六进制 token，或完整的 QQ 授权回调地址");
    }
}
