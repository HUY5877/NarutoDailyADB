package com.huy.auth;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/** Local callback relay. Never persists credentials or looks them up through a service. */
public final class DirectAuthorization implements GameAuthorization {
    private final Map<String, String> fields;
    private final AuthorizationRules originalLifetime;
    private final long receivedAt;
    private final long suppliedLifetimeMs;

    private DirectAuthorization(Map<String, String> fields, AuthorizationRules originalLifetime, long now) {
        this.fields = Collections.unmodifiableMap(new LinkedHashMap<>(fields));
        this.originalLifetime = originalLifetime;
        this.receivedAt = now;
        this.suppliedLifetimeMs = AuthorizationRules.positiveDecimal(fields.get("expires_in")) * 1000L;
    }

    public static DirectAuthorization fromUrl(String value, long now) {
        try {
            if (value == null || value.length() > AuthorizationRules.MAX_BYTES) throw AuthorizationRules.invalid();
            String callback = CloudRules.trimInputWhitespace(value);
            Map<String, String> fields = AuthorizationRules.lookupFields(callback, now);
            // Only an original auth_time establishes a deadline. Paste time is never a capture time.
            AuthorizationRules original = fields.containsKey("auth_time")
                    ? AuthorizationRules.fromCallback(callback, now, now) : null;
            return new DirectAuthorization(fields, original, now);
        } catch (IllegalArgumentException error) {
            if ("Authorization expired".equals(error.getMessage())) throw expired();
            throw new IllegalArgumentException("需要完整的 auth://tauth.qq.com/ 回调地址，包含成功状态、access_token、openid 和 expires_in。");
        }
    }

    /** Whether the supplied callback includes an original auth_time; this is not an online verification. */
    public boolean hasKnownLifetime() { return originalLifetime != null; }

    @Override public String accountHint() {
        String openid = fields.get("openid");
        return openid.substring(openid.length() - 4);
    }

    @Override public String responseJson(long now) {
        long remaining = remainingSeconds(now);
        StringBuilder json = new StringBuilder("{");
        boolean first = true;
        for (Map.Entry<String, String> field : fields.entrySet()) {
            if (!first) json.append(',');
            first = false;
            appendString(json, field.getKey());
            json.append(':');
            if ("ret".equals(field.getKey())) json.append('0');
            else appendString(json, "expires_in".equals(field.getKey())
                    ? Long.toString(remaining) : field.getValue());
        }
        return json.append('}').toString();
    }

    private long remainingSeconds(long now) {
        if (now < receivedAt) throw new IllegalArgumentException("授权状态已失效，请重新读取完整回调地址。");
        if (originalLifetime != null) {
            try { return originalLifetime.remainingSeconds(now); }
            catch (IllegalArgumentException error) { throw expired(); }
        }
        // The callback's age is unknown. Deduct only this confirmation's wait and make no claim
        // about its actual expiry. The game still has to verify the supplied authorization.
        long elapsed = now - receivedAt;
        if (elapsed >= suppliedLifetimeMs || (suppliedLifetimeMs - elapsed) / 1000L < 1) throw expired();
        return (suppliedLifetimeMs - elapsed) / 1000L;
    }

    private static void appendString(StringBuilder target, String value) {
        target.append('"');
        for (int i = 0; i < value.length(); i++) {
            char item = value.charAt(i);
            // The shared callback parser rejects control characters and malformed UTF-8.
            if (item == '"' || item == '\\') target.append('\\');
            target.append(item);
        }
        target.append('"');
    }

    private static IllegalArgumentException expired() {
        return new IllegalArgumentException("授权已过期或等待时间过长，请重新获取完整回调地址。");
    }
}
