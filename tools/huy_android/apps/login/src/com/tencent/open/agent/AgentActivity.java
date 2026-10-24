package com.tencent.open.agent;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.Editable;
import android.text.TextWatcher;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import com.huy.auth.CloudClient;
import com.huy.auth.CloudException;
import com.huy.auth.CloudRecord;
import com.huy.auth.CloudSettings;
import com.huy.auth.DirectAuthorization;
import com.huy.auth.GameAuthorization;
import com.huy.authlogin.HuyUi;
import com.huy.authlogin.LoginPreferences;
import com.huy.authlogin.LoginRequestState;
import com.huy.authlogin.MainActivity;
import com.huy.authlogin.RequestPolicy;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** QQ SDK endpoint; direct credentials are parsed locally and never sent to HUY. */
public final class AgentActivity extends Activity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final LoginRequestState<GameAuthorization> requests = new LoginRequestState<>();
    private HuyUi.Screen screen;
    private EditText callbackInput;
    private LinearLayout directSection, cloudSection;
    private RadioGroup choices;
    private Button readButton, refreshButton, downloadButton, confirmButton;
    private CloudSettings settings;
    private LoginPreferences preferences;
    private CloudSettings.Snapshot listSession;
    private CloudRecord selected;
    private boolean foreground, clearingInput;
    private final SharedPreferences.OnSharedPreferenceChangeListener sessionListener = (store, key) -> {
        if ("revision".equals(key) && foreground && requests.mode() == LoginRequestState.Mode.CLOUD) sessionChanged();
    };

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        setResult(RESULT_CANCELED);
        settings = new CloudSettings(this);
        preferences = new LoginPreferences(this);
        preferences.restore(requests);
        screen = new HuyUi.Screen(this, "确认登录", "选择一种登录方式，读取后核对账号并确认。");
        RadioGroup modes = HuyUi.selector(screen, screen.column, new String[]{"回调 URL 登录", "云端账号登录"},
                requests.mode() == LoginRequestState.Mode.DIRECT ? 0 : 1, this::switchMode);
        screen.column.removeView(modes);
        screen.column.addView(modes, 0);
        directSection = screen.section();
        callbackInput = HuyUi.authorizationInput(screen, directSection);
        readButton = screen.button(directSection, "读取回调授权", true, view -> readDirect());
        cloudSection = screen.section();
        choices = new RadioGroup(this);
        choices.setSaveEnabled(false);
        cloudSection.addView(choices);
        refreshButton = screen.button(cloudSection, "刷新账号列表", false, view -> refreshCloud());
        downloadButton = screen.button(cloudSection, "读取所选账号授权", true, view -> downloadSelected());
        confirmButton = screen.button("确认登录", true, view -> confirmLogin());
        screen.button("取消", false, view -> cancelLogin());
        screen.button("打开上号器", false, view -> openHome());
        screen.footnote("回调 URL 仅本地解析，无需网站登录。请粘贴原始完整 callback_url；云端账号需登录网站并手动选择。");
        callbackInput.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence text, int start, int count, int after) {}
            @Override public void onTextChanged(CharSequence text, int start, int before, int count) {
                if (clearingInput) return;
                requests.invalidate();
                if (foreground) showPrompt();
                updateControls();
            }
            @Override public void afterTextChanged(Editable text) {}
        });
        renderMode();
    }

    @Override protected void onResume() {
        super.onResume();
        foreground = true;
        requests.resume(); // Also handles onPause -> onResume without onStop.
        clearCloud();
        settings.registerListener(sessionListener);
        showPrompt();
        updateControls();
        if (requests.mode() == LoginRequestState.Mode.CLOUD && isTrustedRequest()) refreshCloud();
    }

    private void switchMode(int index) {
        requests.setMode(index == 0 ? LoginRequestState.Mode.DIRECT : LoginRequestState.Mode.CLOUD);
        preferences.save(requests);
        clearInput();
        clearCloud();
        renderMode();
        showPrompt();
        updateControls();
        if (foreground && requests.mode() == LoginRequestState.Mode.CLOUD) refreshCloud();
    }

    private void renderMode() {
        boolean direct = requests.mode() == LoginRequestState.Mode.DIRECT;
        directSection.setVisibility(direct ? View.VISIBLE : View.GONE);
        cloudSection.setVisibility(direct ? View.GONE : View.VISIBLE);
        callbackInput.setHint("粘贴完整 callback_url");
    }

    private void readDirect() {
        if (!allowed() || requests.mode() != LoginRequestState.Mode.DIRECT || requests.isPending()) return;
        String input = callbackInput.getText().toString();
        long attempt = requests.begin(null);
        try {
            GameAuthorization result = DirectAuthorization.fromUrl(input, System.currentTimeMillis());
            result.responseJson(System.currentTimeMillis());
            if (!canApply(attempt, null) || !requests.accept(attempt, null, result)) return;
            clearInput();
            screen.show("已读取授权，等待游戏验证", "OpenID 尾号 " + result.accountHint() + "\n请核对后点击确认登录。缺少原始时间时无法确认实际到期时间。");
            updateControls();
        } catch (IllegalArgumentException error) { failed(error.getMessage()); }
    }

    private void refreshCloud() {
        if (!allowed() || requests.mode() != LoginRequestState.Mode.CLOUD || requests.isPending()) return;
        requests.invalidate();
        clearCloud();
        final CloudSettings.Snapshot session = settings.snapshot();
        if (!session.isSignedIn()) { showPrompt(); updateControls(); return; }
        final long attempt = requests.begin(session.revision);
        screen.busy("正在读取云端账号列表，请手动选择账号。");
        updateControls();
        worker.execute(() -> {
            try {
                List<CloudRecord> result = new CloudClient(session).list();
                runOnUiThread(() -> {
                    if (!canApply(attempt, session)) return;
                    requests.invalidate();
                    listSession = session;
                    renderRecords(result);
                    screen.show(result.isEmpty() ? "还没有保存的账号" : "请选择云端账号", "手动选择一项，再读取授权。不会自动选择此前的账号。");
                    updateControls();
                });
            } catch (CloudException error) { cloudFailure(attempt, session, error); }
        });
    }

    private void renderRecords(List<CloudRecord> records) {
        choices.removeAllViews();
        for (CloudRecord record : records) {
            RadioButton option = new RadioButton(this);
            option.setId(View.generateViewId());
            option.setSaveEnabled(false);
            option.setText(record.label + " · 尾号 " + record.accountHint + (record.isExpired() ? " · 已过期" : ""));
            option.setEnabled(!record.isExpired());
            choices.addView(option, new RadioGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
            option.setOnClickListener(view -> {
                if (!allowed() || requests.mode() != LoginRequestState.Mode.CLOUD || listSession == null) return;
                if (!settings.isCurrent(listSession)) { sessionChanged(); return; }
                requests.invalidate();
                selected = record;
                screen.show("已选择：" + record.label, "尾号 " + record.accountHint + "\n点击读取所选账号授权，再确认登录。");
                updateControls();
            });
        }
    }

    private void downloadSelected() {
        if (!allowed() || requests.mode() != LoginRequestState.Mode.CLOUD || requests.isPending() || selected == null) return;
        final CloudSettings.Snapshot session = listSession;
        if (!settings.isCurrent(session)) { sessionChanged(); return; }
        if (selected.isExpired()) { failed("授权已过期，请回到抓号器重新扫码。"); return; }
        final String id = selected.id;
        final long attempt = requests.begin(session.revision);
        screen.busy("正在读取所选账号授权。");
        updateControls();
        worker.execute(() -> {
            try {
                CloudClient.Download result = new CloudClient(session).download(id);
                runOnUiThread(() -> {
                    if (!canApply(attempt, session)) return;
                    try { result.authorization.responseJson(System.currentTimeMillis()); }
                    catch (IllegalArgumentException error) { failed("授权已过期，请回到抓号器重新扫码。"); return; }
                    if (result.record.isExpired()) { failed("授权已过期，请回到抓号器重新扫码。"); return; }
                    if (!requests.accept(attempt, settings.snapshot().revision, result.authorization)) return;
                    screen.show("登录：" + result.record.label, "尾号 " + result.record.accountHint + "\n请核对账号，点击确认后返回游戏。");
                    updateControls();
                });
            } catch (CloudException error) { cloudFailure(attempt, session, error); }
        });
    }

    private void cloudFailure(long attempt, CloudSettings.Snapshot session, CloudException error) {
        runOnUiThread(() -> {
            // Gate before side effects: a stale cloud auth failure cannot clear a direct login.
            if (!canApply(attempt, session)) return;
            if (error.isAuthenticationRequired()) settings.clearSession();
            failed(error.getMessage());
        });
    }

    private boolean canApply(long attempt, CloudSettings.Snapshot session) {
        if (!allowed() || !requests.isCurrent(attempt, session == null ? null : session.revision)) return false;
        if (session != null && !settings.isCurrent(session)) { sessionChanged(); return false; }
        return true;
    }

    private boolean allowed() { return foreground && !isFinishing() && !isDestroyed() && isTrustedRequest(); }
    private boolean isTrustedRequest() {
        try {
            Intent request = getIntent();
            if (request == null) return false;
            Bundle parameters = request.getBundleExtra("key_params");
            return parameters != null && RequestPolicy.accepts(getCallingPackage(), request.getStringExtra("key_action"), parameters.getString("client_id"));
        } catch (RuntimeException error) { return false; }
    }

    private String currentRevision() {
        return requests.mode() == LoginRequestState.Mode.CLOUD ? settings.snapshot().revision : null;
    }

    private void confirmLogin() {
        if (!allowed() || requests.isPending()) return;
        GameAuthorization authorization = requests.value(currentRevision());
        if (authorization == null) { showPrompt(); updateControls(); return; }
        CloudSettings.Snapshot session = requests.mode() == LoginRequestState.Mode.CLOUD ? settings.snapshot() : null;
        if (session != null && (!session.isSignedIn() || !settings.isCurrent(listSession))) { sessionChanged(); return; }
        try {
            String response = authorization.responseJson(System.currentTimeMillis());
            if (!isTrustedRequest() || (session != null && !settings.isCurrent(session))) { sessionChanged(); return; }
            Intent result = new Intent();
            result.putExtra("key_response", response);
            setResult(RESULT_OK, result);
            requests.stop();
            clearInput();
            finish();
        } catch (IllegalArgumentException error) { failed(error.getMessage()); }
    }

    private void showPrompt() {
        if (!isTrustedRequest()) screen.show("已拒绝此次请求", "只接受火影忍者发起的 QQ 登录请求。请从游戏中选择 QQ 登录。");
        else if (requests.mode() == LoginRequestState.Mode.DIRECT) screen.show("请粘贴完整 callback_url", "本地读取后核对 OpenID 尾号，再确认登录；无需登录网站账号。");
        else if (!settings.snapshot().isSignedIn()) screen.show("未登录网站账号", "打开上号器，在云端账号设置登录后，重新从游戏打开 QQ 登录。");
        else screen.show("请选择云端账号", "刷新列表并手动选择账号，再读取授权并确认登录。");
    }

    private void updateControls() {
        boolean allowed = allowed();
        boolean direct = requests.mode() == LoginRequestState.Mode.DIRECT;
        boolean cloud = !direct && settings.snapshot().isSignedIn();
        boolean pending = requests.isPending();
        callbackInput.setEnabled(allowed && direct);
        HuyUi.enabled(readButton, allowed && direct && !pending);
        HuyUi.enabled(refreshButton, allowed && cloud && !pending);
        HuyUi.enabled(downloadButton, allowed && cloud && !pending && selected != null && !selected.isExpired());
        HuyUi.enabled(confirmButton, allowed && !pending && requests.value(currentRevision()) != null);
    }

    private void sessionChanged() {
        if (requests.mode() != LoginRequestState.Mode.CLOUD) return;
        requests.websiteSessionChanged();
        clearCloud();
        clearInput();
        showPrompt();
        updateControls();
    }

    private void failed(String detail) { requests.invalidate(); updateControls(); screen.show("暂时无法登录", detail); }
    private void clearCloud() { selected = null; listSession = null; if (choices != null) choices.removeAllViews(); }
    private void clearInput() { clearingInput = true; try { callbackInput.setText(""); } finally { clearingInput = false; } }
    private void cancelLogin() { requests.stop(); clearInput(); setResult(RESULT_CANCELED); finish(); }
    @Override public void onBackPressed() { cancelLogin(); }
    private void openHome() { requests.stop(); clearInput(); setResult(RESULT_CANCELED); startActivity(new Intent(this, MainActivity.class)); finish(); }

    @Override protected void onStop() {
        foreground = false;
        requests.stop();
        clearInput();
        clearCloud();
        updateControls();
        settings.unregisterListener(sessionListener);
        super.onStop();
    }
    @Override protected void onDestroy() { requests.stop(); clearInput(); worker.shutdownNow(); super.onDestroy(); }
}
