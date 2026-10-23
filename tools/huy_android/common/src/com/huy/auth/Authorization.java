package com.huy.auth;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.Map;

/** Immutable credentials. Never log this object's serialized form or callback. */
public final class Authorization {
    private final String callbackUrl;
    private final long capturedAt;
    private final AuthorizationRules rules;

    private Authorization(String callbackUrl, long capturedAt, AuthorizationRules rules) {
        this.callbackUrl = callbackUrl;
        this.capturedAt = capturedAt;
        this.rules = rules;
    }

    public static Authorization fromCallback(String callbackUrl, long now) {
        return new Authorization(callbackUrl, now, AuthorizationRules.fromCallback(callbackUrl, now, now));
    }

    public static Authorization fromJson(String json, long now) {
        StrictJson.validateObject(json);
        try {
            JSONObject root = new JSONObject(json);
            if (!AuthorizationRules.APP_ID.equals(decimalText(root.get("app_id")))) throw AuthorizationRules.invalid();
            if (root.has("schema_version") && !"1".equals(decimalText(root.get("schema_version")))) {
                throw AuthorizationRules.invalid();
            }
            long capturedAt;
            if (root.has("captured_at_epoch_ms")) {
                capturedAt = nonnegativeLong(root.get("captured_at_epoch_ms"));
                if (root.has("captured_at") && capturedAt != AuthorizationRules.parseCapturedAt(string(root.get("captured_at")))) {
                    throw AuthorizationRules.invalid();
                }
            } else {
                capturedAt = AuthorizationRules.parseCapturedAt(string(root.get("captured_at")));
            }
            String callback = string(root.get("callback_url"));
            AuthorizationRules rules = AuthorizationRules.fromCallback(callback, capturedAt, now);
            return new Authorization(callback, capturedAt, rules);
        } catch (JSONException ignored) {
            throw AuthorizationRules.invalid();
        }
    }

    public String toJson() {
        try {
            JSONObject root = new JSONObject();
            root.put("schema_version", 1);
            root.put("app_id", AuthorizationRules.APP_ID);
            root.put("captured_at_epoch_ms", capturedAt);
            root.put("callback_url", callbackUrl);
            return root.toString();
        } catch (JSONException ignored) {
            throw AuthorizationRules.invalid();
        }
    }

    /** The original lifetime is never reused when returning a previously saved login. */
    public String responseJson(long now) {
        long remaining = rules.remainingSeconds(now);
        try {
            JSONObject response = new JSONObject();
            for (Map.Entry<String, String> field : rules.fields().entrySet()) {
                response.put(field.getKey(), field.getValue());
            }
            response.put("ret", 0);
            response.put("expires_in", Long.toString(remaining));
            return response.toString();
        } catch (JSONException ignored) {
            throw AuthorizationRules.invalid();
        }
    }

    public long expiresAtMs() { return rules.expiresAtMs(); }

    String accessToken() { return rules.fields().get("access_token"); }

    private static String string(Object value) {
        if (!(value instanceof String)) throw AuthorizationRules.invalid();
        return (String) value;
    }

    private static String decimalText(Object value) {
        if (value instanceof Integer || value instanceof Long) return value.toString();
        if (value instanceof String && ((String) value).matches("[0-9]{1,19}")) return (String) value;
        throw AuthorizationRules.invalid();
    }

    private static long nonnegativeLong(Object value) {
        try {
            long result = Long.parseLong(decimalText(value));
            if (result < 0) throw AuthorizationRules.invalid();
            return result;
        } catch (NumberFormatException ignored) { throw AuthorizationRules.invalid(); }
    }
}
