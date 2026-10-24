package com.tencent.open.agent;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.Editable;
import android.text.TextWatcher;
import android.widget.Button;
import android.widget.EditText;

import com.huy.auth.Authorization;
import com.huy.auth.AuthorizationInput;
import com.huy.auth.CloudClient;
import com.huy.auth.CloudException;
import com.huy.auth.CloudSettings;
import com.huy.authlogin.HuyUi;
import com.huy.authlogin.LoginRequestState;
import com.huy.authlogin.MainActivity;
import com.huy.authlogin.RequestPolicy;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Compatibility endpoint invoked by the game's QQ SDK. */
public final class AgentActivity extends Activity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final LoginRequestState<Authorization> requests = new LoginRequestState<>();
    private HuyUi.Screen screen;
    private EditText tokenInput;
    private Button readButton, confirmButton;
    private CloudSettings settings;
    private boolean foreground;
    private boolean firstResume = true;
    private boolean reading;
    private boolean clearingInput;
    private final SharedPreferences.OnSharedPreferenceChangeListener sessionListener = (preferences, key) -> {
        if ("revision".equals(key) && foreground) sessionChanged();
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        setResult(RESULT_CANCELED);
        firstResume = state == null;
        settings = new CloudSettings(this);
        screen = new HuyUi.Screen(this, "确认登录", "粘贴 token 或完整 QQ 回调地址，读取并确认要登录的账号。");
        tokenInput = HuyUi.authorizationInput(screen);
        readButton = screen.button("读取授权", true, view -> readInput());
        confirmButton = screen.button("确认登录", true, view -> confirmLogin());
        HuyUi.enabled(confirmButton, false);
        screen.button("取消", false, view -> cancelLogin());
        screen.button("打开上号器", false, view -> openHome());
        screen.footnote("需先在上号器登录保存该授权的网站账号。回调地址不会延长授权有效期。取消不会删除已保存的授权。");
        tokenInput.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence text, int start, int count, int after) {}
            @Override public void onTextChanged(CharSequence text, int start, int before, int count) {
                if (clearingInput) return;
                invalidateAuthorization();
                if (foreground) showInputPrompt();
            }
            @Override public void afterTextChanged(Editable text) {}
        });
    }

    @Override protected void onResume() {
        super.onResume();
        foreground = true;
        requests.resume();
        settings.registerListener(sessionListener);
        CloudSettings.Snapshot session = settings.snapshot();
        boolean loadSelected = firstResume && session.isSignedIn() && !session.selectedId.isEmpty();
        firstResume = false;
        showInputPrompt();
        updateControls();
        if (isTrustedRequest() && loadSelected) readAuthorization(session, null);
    }

    private void readInput() {
        if (!foreground || reading || !isTrustedRequest()) return;
        invalidateAuthorization();
        CloudSettings.Snapshot session = settings.snapshot();
        if (!session.isSignedIn()) { showInputPrompt(); return; }
        final AuthorizationInput input;
        try { input = AuthorizationInput.parse(tokenInput.getText().toString(), System.currentTimeMillis()); }
        catch (IllegalArgumentException error) { failed(error.getMessage()); return; }
        readAuthorization(session, input);
    }

    private void readAuthorization(CloudSettings.Snapshot session, AuthorizationInput input) {
        final long attempt = requests.begin(session.revision);
        reading = true;
        updateControls();
        screen.busy(input == null ? "正在读取所选账号的授权，也可直接粘贴其他授权。" : "正在当前网站账号下查找授权。");
        worker.execute(() -> {
            try {
                CloudClient client = new CloudClient(session);
                CloudClient.Download result = input == null ? client.download(session.selectedId) : client.resolveInput(input);
                runOnUiThread(() -> {
                    if (!canApply(attempt, session)) return;
                    try { result.authorization.responseJson(System.currentTimeMillis()); }
                    catch (IllegalArgumentException error) { failed("授权已过期，请回到抓号器重新扫码。"); return; }
                    if (result.record.isExpired()) { failed("授权已过期，请回到抓号器重新扫码。"); return; }
                    if (!requests.accept(attempt, settings.snapshot().revision, result.authorization)) return;
                    reading = false;
                    clearInput();
                    screen.show("登录：" + result.record.label,
                            "尾号 " + result.record.accountHint + "\n请核对账号，点击确认后返回游戏。");
                    updateControls();
                });
            } catch (CloudException error) {
                runOnUiThread(() -> {
                    if (!canApply(attempt, session)) return;
                    if (error.isAuthenticationRequired()) { settings.clearSession(); clearInput(); }
                    failed(error.getMessage());
                });
            }
        });
    }

    private boolean canApply(long attempt, CloudSettings.Snapshot session) {
        if (!foreground || isFinishing() || isDestroyed() || !requests.isCurrent(attempt, session.revision)) return false;
        if (!settings.isCurrent(session)) { sessionChanged(); return false; }
        if (!isTrustedRequest()) { failed("只接受火影忍者发起的 QQ 登录请求。"); return false; }
        return true;
    }

    private boolean isTrustedRequest() {
        try {
            Intent request = getIntent();
            if (request == null) return false;
            Bundle parameters = request.getBundleExtra("key_params");
            if (parameters == null) return false;
            return RequestPolicy.accepts(getCallingPackage(), request.getStringExtra("key_action"),
                    parameters.getString("client_id"));
        } catch (RuntimeException error) { return false; }
    }

    private void confirmLogin() {
        if (!foreground || reading || !isTrustedRequest()) return;
        CloudSettings.Snapshot session = settings.snapshot();
        Authorization authorization = requests.value(session.revision);
        if (!session.isSignedIn() || authorization == null) { sessionChanged(); return; }
        try {
            // Check the cloud authorization's original lifetime at the explicit user confirmation.
            String response = authorization.responseJson(System.currentTimeMillis());
            if (!settings.isCurrent(session) || !isTrustedRequest()) { sessionChanged(); return; }
            Intent result = new Intent();
            result.putExtra("key_response", response);
            setResult(RESULT_OK, result);
            requests.stop();
            clearInput();
            finish();
        } catch (IllegalArgumentException error) { failed("授权已过期，请回到抓号器重新扫码。"); }
    }

    private void showInputPrompt() {
        if (!isTrustedRequest()) {
            screen.show("已拒绝此次请求", "只接受火影忍者发起的 QQ 登录请求。请从游戏中选择 QQ 登录。");
        } else if (!settings.snapshot().isSignedIn()) {
            screen.show("未登录网站账号", "请点击“打开上号器”，在云端账号设置登录保存该授权的网站账号，再从游戏重新打开 QQ 登录。");
        } else {
            screen.show("请读取授权", "粘贴 token 或完整 QQ 回调地址后点击“读取授权”，核对账号尾号，再点击确认登录。");
        }
    }

    private void updateControls() {
        CloudSettings.Snapshot session = settings.snapshot();
        boolean allowed = foreground && session.isSignedIn() && isTrustedRequest();
        tokenInput.setEnabled(allowed);
        HuyUi.enabled(readButton, allowed && !reading);
        HuyUi.enabled(confirmButton, allowed && !reading && requests.value(session.revision) != null);
    }

    private void invalidateAuthorization() {
        requests.invalidate();
        reading = false;
        updateControls();
    }

    private void sessionChanged() {
        invalidateAuthorization();
        clearInput();
        showInputPrompt();
        if (settings.snapshot().isSignedIn()) {
            screen.show("网站账号或所选账号已变化", "请重新粘贴授权并读取，或打开上号器重新选择账号。");
        }
    }

    private void failed(String detail) {
        invalidateAuthorization();
        screen.show("暂时无法登录", detail);
    }

    private void clearInput() {
        clearingInput = true;
        try { tokenInput.setText(""); }
        finally { clearingInput = false; }
    }

    private void cancelLogin() {
        requests.stop();
        clearInput();
        setResult(RESULT_CANCELED);
        finish();
    }

    @Override public void onBackPressed() { cancelLogin(); }

    private void openHome() {
        requests.stop();
        clearInput();
        setResult(RESULT_CANCELED);
        startActivity(new Intent(this, MainActivity.class));
        finish();
    }

    @Override protected void onStop() {
        foreground = false;
        requests.stop();
        reading = false;
        clearInput();
        updateControls();
        settings.unregisterListener(sessionListener);
        super.onStop();
    }

    @Override protected void onDestroy() {
        requests.stop();
        clearInput();
        worker.shutdownNow();
        super.onDestroy();
    }
}
