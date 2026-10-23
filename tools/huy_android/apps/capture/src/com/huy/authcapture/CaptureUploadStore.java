package com.huy.authcapture;

import android.content.Context;
import android.content.SharedPreferences;
import com.huy.auth.Authorization;
import com.huy.auth.CloudSettings;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/** Non-secret upload bookkeeping. The authorization stays in CredentialStore. */
final class CaptureUploadStore {
    private final SharedPreferences preferences;

    CaptureUploadStore(Context context) {
        preferences = context.getSharedPreferences("capture_upload", Context.MODE_PRIVATE);
    }

    String lastLabel() { return preferences.getString("last_label", "我的账号"); }

    void savePending(Authorization authorization, CloudSettings.Snapshot session, String label) throws Exception {
        if (!preferences.edit()
                .putString("document_sha256", fingerprint(authorization))
                .putString("endpoint", session.endpoint)
                .putString("owner_id", session.userId)
                .putString("label", label)
                .putString("last_label", label)
                .remove("record_id")
                .commit()) throw new IllegalStateException("Upload state could not be saved");
    }

    State load(Authorization authorization) throws Exception {
        if (authorization == null || !fingerprint(authorization).equals(preferences.getString("document_sha256", ""))) return null;
        String endpoint = preferences.getString("endpoint", "");
        String ownerId = preferences.getString("owner_id", "");
        String label = preferences.getString("label", "");
        if (endpoint.isEmpty() || ownerId.isEmpty() || label.isEmpty()) return null;
        return new State(endpoint, ownerId, label, preferences.getString("record_id", ""));
    }

    void markUploaded(Authorization authorization, CloudSettings.Snapshot session, String recordId) throws Exception {
        State state = load(authorization);
        if (state == null || !state.belongsTo(session) || recordId == null || recordId.isEmpty()) return;
        if (!preferences.edit().putString("record_id", recordId).commit())
            throw new IllegalStateException("Upload state could not be saved");
    }

    void clear() throws Exception {
        String label = lastLabel();
        if (!preferences.edit().clear().putString("last_label", label).commit())
            throw new IllegalStateException("Upload state could not be cleared");
    }

    private static String fingerprint(Authorization authorization) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(authorization.toJson().getBytes(StandardCharsets.UTF_8));
        StringBuilder output = new StringBuilder(64);
        for (byte value : digest) output.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
        return output.toString();
    }

    static final class State {
        final String endpoint, ownerId, label, recordId;
        State(String endpoint, String ownerId, String label, String recordId) {
            this.endpoint = endpoint; this.ownerId = ownerId; this.label = label; this.recordId = recordId;
        }
        boolean belongsTo(CloudSettings.Snapshot session) {
            return session.isSignedIn() && endpoint.equals(session.endpoint) && ownerId.equals(session.userId);
        }
        boolean uploaded() { return !recordId.isEmpty(); }
    }
}
