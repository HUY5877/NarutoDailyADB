package com.tencent.open.agent;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.Button;

import com.huy.auth.Authorization;
import com.huy.auth.CloudClient;
import com.huy.auth.CloudException;
import com.huy.auth.CloudSettings;
import com.huy.authlogin.HuyUi;
import com.huy.authlogin.MainActivity;
import com.huy.authlogin.RequestPolicy;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Compatibility endpoint invoked by the game's QQ SDK. */
public final class AgentActivity extends Activity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private HuyUi.Screen screen;
    private Button confirmButton;
    private Button homeButton;
    private Authorization authorization;
    private CloudSettings settings;
    private CloudSettings.Snapshot session;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        setResult(RESULT_CANCELED);
        settings = new CloudSettings(this);
        screen = new HuyUi.Screen(this, "确认登录", "使用手动选择的 QQ 账号登录火影忍者。");
        confirmButton = screen.button("确认登录", true, view -> confirmLogin());
        HuyUi.enabled(confirmButton, false);
        screen.button("取消", false, view -> finish());
        homeButton = screen.button("打开上号器", false, view -> openHome());
        homeButton.setVisibility(android.view.View.GONE);
        screen.footnote("取消不会删除已保存的授权。");
        if (!isTrustedRequest()) {
            screen.show("已拒绝此次请求", "只接受火影忍者发起的 QQ 登录请求。请从游戏中选择 QQ 登录。");
            return;
        }
        session = settings.snapshot();
        if (!session.isSignedIn() || session.selectedId.isEmpty()) {
            screen.show("尚未选择账号", "请打开上号器，登录网站账号并手动选择一个 QQ 账号。");
            homeButton.setVisibility(android.view.View.VISIBLE);
            return;
        }
        screen.busy("正在读取所选账号的授权。");
        worker.execute(() -> {
            try {
                CloudClient.Download result = new CloudClient(session).download(session.selectedId);
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed()) return;
                    if (!settings.isCurrent(session)) {
                        failed("所选账号或网站登录已变化，请回到上号器重新选择。");
                        return;
                    }
                    authorization = result.authorization;
                    screen.show("登录：" + result.record.label, "尾号 " + result.record.accountHint + "\n点击确认后返回游戏。");
                    HuyUi.enabled(confirmButton, true);
                });
            } catch (CloudException error) {
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed()) return;
                    if (settings.isCurrent(session) && error.isAuthenticationRequired()) settings.clearSession();
                    failed(error.getMessage());
                });
            }
        });
    }

    private boolean isTrustedRequest() {
        try {
            Intent request = getIntent();
            if (request == null) return false;
            Bundle parameters = request.getBundleExtra("key_params");
            if (parameters == null) return false;
            return RequestPolicy.accepts(getCallingPackage(), request.getStringExtra("key_action"),
                    parameters.getString("client_id"));
        } catch (RuntimeException error) {
            return false;
        }
    }

    private void confirmLogin() {
        if (authorization == null || !isTrustedRequest()) return;
        if (!settings.isCurrent(session)) {
            failed("所选账号或网站登录已变化，请回到上号器重新选择。");
            return;
        }
        try {
            // Recheck expiration immediately before returning any credential.
            String response = authorization.responseJson(System.currentTimeMillis());
            Intent result = new Intent();
            result.putExtra("key_response", response);
            setResult(RESULT_OK, result);
            authorization = null;
            finish();
        } catch (IllegalArgumentException error) {
            failed("授权已过期，请回到抓号器重新扫码。");
        }
    }

    private void failed(String detail) {
        authorization = null;
        HuyUi.enabled(confirmButton, false);
        screen.show("暂时无法登录", detail);
        homeButton.setVisibility(android.view.View.VISIBLE);
    }

    private void openHome() {
        setResult(RESULT_CANCELED);
        startActivity(new Intent(this, MainActivity.class));
        finish();
    }

    @Override protected void onDestroy() {
        authorization = null;
        worker.shutdownNow();
        super.onDestroy();
    }
}
