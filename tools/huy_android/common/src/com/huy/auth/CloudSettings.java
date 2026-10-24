package com.huy.auth;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.UUID;

/** A website session, separate from every QQ WebView cookie and credential. */
public final class CloudSettings {
    public static final String DEFAULT_ENDPOINT = "https://naruto-login-client-git-test-sust-acmer.vercel.app";
    private final SharedPreferences preferences;

    public CloudSettings(Context context) {
        preferences = context.getApplicationContext().getSharedPreferences("huy_cloud", Context.MODE_PRIVATE);
        synchronized (CloudSettings.class) {
            if (CloudRules.legacyEndpoint(preferences.getString("endpoint", ""))) {
                preferences.edit().putString("endpoint", DEFAULT_ENDPOINT)
                        .remove("cookie").remove("user_id").remove("username").remove("selected_id")
                        .putString("revision", UUID.randomUUID().toString())
                        .putBoolean("endpoint_sign_in_required", true).commit();
            }
        }
    }

    public boolean needsEndpointSignIn() { return preferences.getBoolean("endpoint_sign_in_required", false); }

    public void registerListener(SharedPreferences.OnSharedPreferenceChangeListener listener) {
        preferences.registerOnSharedPreferenceChangeListener(listener);
    }

    public void unregisterListener(SharedPreferences.OnSharedPreferenceChangeListener listener) {
        preferences.unregisterOnSharedPreferenceChangeListener(listener);
    }

    public Snapshot snapshot() {
        synchronized (CloudSettings.class) {
            return new Snapshot(preferences.getString("endpoint", DEFAULT_ENDPOINT),
                    preferences.getString("cookie", ""), preferences.getString("user_id", ""),
                    preferences.getString("username", ""), preferences.getString("selected_id", ""),
                    preferences.getString("revision", ""));
        }
    }

    public boolean isCurrent(Snapshot snapshot) {
        return snapshot != null && snapshot.revision.equals(snapshot().revision);
    }

    public void saveSession(CloudClient.SignIn session) {
        synchronized (CloudSettings.class) {
            preferences.edit().putString("endpoint", session.endpoint).putString("cookie", session.cookie)
                    .putString("user_id", session.userId).putString("username", session.username)
                    .remove("selected_id").remove("endpoint_sign_in_required")
                    .putString("revision", UUID.randomUUID().toString()).commit();
        }
    }

    public void clearSession() {
        synchronized (CloudSettings.class) {
            preferences.edit().remove("cookie").remove("user_id").remove("username").remove("selected_id")
                    .putString("revision", UUID.randomUUID().toString()).commit();
        }
    }

    public void select(String id) {
        if (!id.isEmpty() && !CloudRules.recordId(id)) throw new IllegalArgumentException("账号记录无效");
        synchronized (CloudSettings.class) {
            preferences.edit().putString("selected_id", id).putString("revision", UUID.randomUUID().toString()).commit();
        }
    }

    public static final class Snapshot {
        public final String endpoint, cookie, userId, username, selectedId, revision;
        private Snapshot(String endpoint, String cookie, String userId, String username, String selectedId, String revision) {
            this.endpoint = endpoint; this.cookie = cookie; this.userId = userId;
            this.username = username; this.selectedId = selectedId; this.revision = revision;
        }
        public boolean isSignedIn() { return !endpoint.isEmpty() && !cookie.isEmpty() && !userId.isEmpty(); }
    }
}
