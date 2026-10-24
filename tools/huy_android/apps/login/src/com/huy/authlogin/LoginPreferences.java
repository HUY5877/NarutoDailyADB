package com.huy.authlogin;

import android.content.Context;
import android.content.SharedPreferences;

/** Only nonsecret mode preferences are shared between the two screens. */
public final class LoginPreferences {
    private final SharedPreferences preferences;
    public LoginPreferences(Context context) {
        preferences = context.getSharedPreferences("huy_login_modes", Context.MODE_PRIVATE);
    }
    public void restore(LoginRequestState<?> state) {
        state.setMode(preferences.getBoolean("cloud", false)
                ? LoginRequestState.Mode.CLOUD : LoginRequestState.Mode.DIRECT);
    }
    public void save(LoginRequestState<?> state) {
        preferences.edit().putBoolean("cloud", state.mode() == LoginRequestState.Mode.CLOUD).remove("url").apply();
    }
}
