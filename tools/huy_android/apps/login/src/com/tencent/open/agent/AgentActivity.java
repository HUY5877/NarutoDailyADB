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
import com.huy.authlogin.LoginAutomation;
import com.tencent.tim.R;
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
    private Button readButton, refreshButton, confirmButton;
    private CloudSettings settings;
    private LoginPreferences preferences;
    private CloudSettings.Snapshot listSession;
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
        requests.setMode(LoginRequestState.Mode.DIRECT);
        screen = new HuyUi.Screen(this, "确认登录", "选择一种登录方式，读取后核对账号并确认。");
        screen.statusTitle.setId(R.id.login_status);
        RadioGroup modes = HuyUi.selector(screen, screen.column, new String[]{"回调 URL 登录", "账号历史"},
                requests.mode() == LoginRequestState.Mode.DIRECT ? 0 : 1, this::switchMode);
        screen.column.removeView(modes);
        screen.column.addView(modes, 0);
        // Stable resource IDs also make the active mode inspectable by ADB UI automation.
        modes.setOnCheckedChangeListener(null);
        modes.getChildAt(0).setId(R.id.login_direct_mode);modes.getChildAt(1).setId(R.id.login_cloud_mode);
        modes.check(requests.mode()==LoginRequestState.Mode.DIRECT?R.id.login_direct_mode:R.id.login_cloud_mode);
        modes.setOnCheckedChangeListener((group,id) -> switchMode(id==R.id.login_direct_mode?0:1));
        directSection = screen.section();
        callbackInput = HuyUi.authorizationInput(screen, directSection);
        callbackInput.setId(R.id.login_callback_url);
        readButton = screen.button(directSection, "读取回调授权", true, view -> readDirect());
        readButton.setId(R.id.login_read_callback);
        cloudSection = screen.section();
        choices = new RadioGroup(this);
        choices.setSaveEnabled(false);
        cloudSection.addView(choices);
        refreshButton = screen.button(cloudSection, "刷新历史记录", false, view -> refreshCloud());
        confirmButton = screen.button("确认登录", true, view -> confirmLogin());
        confirmButton.setId(R.id.login_confirm);
        screen.button("取消", false, view -> cancelLogin());
        screen.button("打开上号器", false, view -> openHome());
        screen.footnote("回调 URL 仅本地解析，无需网站登录。账号历史只显示本人的历史信息，不提供凭据下载或账号选择登录。");
        callbackInput.addTextChangedListener(new TextWatcher() {
            @Override public void beforeTextChanged(CharSequence text, int start, int count, int after) {}
            @Override public void onTextChanged(CharSequence text, int start, int before, int count) {
                if (clearingInput) return;
                LoginAutomation.invalidate(AgentActivity.this);
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
        LoginAutomation.attach(this);
        clearCloud();
        settings.registerListener(sessionListener);
        showPrompt();
        updateControls();
        if (requests.mode() == LoginRequestState.Mode.CLOUD && isTrustedRequest()) refreshCloud();
    }

    private void switchMode(int index) {
        LoginAutomation.invalidate(this);
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
        LoginAutomation.invalidate(this);
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
        screen.busy("正在读取本人历史记录。");
        updateControls();
        worker.execute(() -> {
            try {
                List<CloudRecord> result = new CloudClient(session).list();
                runOnUiThread(() -> {
                    if (!canApply(attempt, session)) return;
                    requests.invalidate();
                    listSession = session;
                    renderRecords(result);
                    screen.show(result.isEmpty() ? "还没有历史记录" : "本人账号历史", "这里只显示历史信息。切换至回调 URL 登录后读取本次授权。");
                    updateControls();
                });
            } catch (CloudException error) { cloudFailure(attempt, session, error); }
        });
    }

    private void renderRecords(List<CloudRecord> records) {
        choices.removeAllViews();
        for (CloudRecord record : records) {
            android.widget.TextView option = new android.widget.TextView(this);
            option.setSaveEnabled(false);
            option.setText(record.label + " · 尾号 " + record.accountHint + (record.isExpired() ? " · 已过期" : ""));
            choices.addView(option, new RadioGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        }
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
        if (!allowed() || requests.isPending() || requests.mode()!=LoginRequestState.Mode.DIRECT) return;
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
            LoginAutomation.submitted(this);
            requests.stop();
            clearInput();
            finish();
        } catch (IllegalArgumentException error) { failed(error.getMessage()); }
    }

    private void showPrompt() {
        if (!isTrustedRequest()) screen.show("已拒绝此次请求", "只接受火影忍者发起的 QQ 登录请求。请从游戏中选择 QQ 登录。");
        else if (requests.mode() == LoginRequestState.Mode.DIRECT) screen.show("请粘贴完整 callback_url", "本地读取后核对 OpenID 尾号，再确认登录；无需登录网站账号。");
        else if (!settings.snapshot().isSignedIn()) screen.show("未登录网站账号", "如需查看本人历史，可在上号器的网站账号设置中登录。回调 URL 登录不受影响。");
        else screen.show("本人账号历史", "刷新可查看本人历史记录。切换至回调 URL 登录后读取本次授权。");
    }

    private void updateControls() {
        boolean allowed = allowed();
        boolean direct = requests.mode() == LoginRequestState.Mode.DIRECT;
        boolean cloud = !direct && settings.snapshot().isSignedIn();
        boolean pending = requests.isPending();
        callbackInput.setEnabled(allowed && direct);
        HuyUi.enabled(readButton, allowed && direct && !pending);
        HuyUi.enabled(refreshButton, allowed && cloud && !pending);
        HuyUi.enabled(confirmButton, allowed && direct && !pending && requests.value(currentRevision()) != null);
    }

    private void sessionChanged() {
        if (requests.mode() != LoginRequestState.Mode.CLOUD) return;
        requests.websiteSessionChanged();
        clearCloud();
        clearInput();
        showPrompt();
        updateControls();
    }

    private void failed(String detail) { LoginAutomation.invalidate(this); requests.invalidate(); updateControls(); screen.show("暂时无法登录", detail); }
    private void clearCloud() { listSession = null; if (choices != null) choices.removeAllViews(); }
    private void clearInput() { clearingInput = true; try { callbackInput.setText(""); } finally { clearingInput = false; } }
    private void cancelLogin() { requests.stop(); clearInput(); setResult(RESULT_CANCELED); finish(); }
    @Override public void onBackPressed() { cancelLogin(); }
    private void openHome() { requests.stop(); clearInput(); setResult(RESULT_CANCELED); startActivity(new Intent(this, MainActivity.class)); finish(); }

    public boolean injectAutomation(String callback) {
        if(!allowed())return false;
        ((RadioButton)findViewById(R.id.login_direct_mode)).setChecked(true);
        switchMode(0);
        callbackInput.setText(callback);
        readDirect();
        return allowed()&&requests.mode()==LoginRequestState.Mode.DIRECT&&requests.value(null)!=null;
    }
    public boolean confirmAutomation() {
        if(!allowed()||requests.mode()!=LoginRequestState.Mode.DIRECT||requests.isPending()||requests.value(null)==null)return false;
        confirmLogin();
        return isFinishing();
    }
    public void clearAutomation() { requests.invalidate();clearInput();showPrompt();updateControls(); }

    @Override protected void onPause() {
        foreground=false;
        LoginAutomation.detach(this);
        requests.stop();clearInput();updateControls();
        super.onPause();
    }

    @Override protected void onStop() {
        foreground = false;
        LoginAutomation.detach(this);
        requests.stop();
        clearInput();
        clearCloud();
        updateControls();
        settings.unregisterListener(sessionListener);
        super.onStop();
    }
    @Override protected void onDestroy() { LoginAutomation.detach(this);requests.stop(); clearInput(); worker.shutdownNow(); super.onDestroy(); }
}
