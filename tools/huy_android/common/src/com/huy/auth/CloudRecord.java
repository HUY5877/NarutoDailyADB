package com.huy.auth;

import org.json.JSONObject;
import org.json.JSONException;

public final class CloudRecord {
    public final String id, label, accountHint, capturedAt, expiresAt, updatedAt;
    public final boolean expired;
    public final long expiresAtMs;

    CloudRecord(JSONObject data) throws JSONException {
        id = string(data, "id"); label = string(data, "label"); accountHint = string(data, "accountHint");
        capturedAt = string(data, "capturedAt"); expiresAt = string(data, "expiresAt"); updatedAt = string(data, "updatedAt");
        if (!CloudRules.recordId(id) || !CloudRules.label(label).equals(label)
                || !CloudRules.safeText(accountHint, 80) || !(data.get("expired") instanceof Boolean)) throw new JSONException("Invalid record");
        expiresAtMs = CloudRules.timestamp(expiresAt);
        if (CloudRules.timestamp(capturedAt) >= expiresAtMs) throw new JSONException("Invalid expiry");
        CloudRules.timestamp(updatedAt);
        expired = data.getBoolean("expired");
    }

    public boolean isExpired() { return expired || expiresAtMs - System.currentTimeMillis() < 1000; }

    static String string(JSONObject data, String key) throws JSONException {
        Object value = data.get(key);
        if (!(value instanceof String)) throw new JSONException("Invalid string");
        return (String) value;
    }
}
