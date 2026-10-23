package com.huy.authlogin;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.text.InputType;
import android.view.View;
import android.view.ViewGroup;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import com.huy.auth.CloudAccountActivity;
import com.huy.auth.CloudClient;
import com.huy.auth.CloudException;
import com.huy.auth.CloudRecord;
import com.huy.auth.CloudSettings;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Selects a cloud record by list or token; AgentActivity fetches it again for game login. */
public final class MainActivity extends Activity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private HuyUi.Screen screen;
    private CloudSettings settings;
    private CloudSettings.Snapshot listSession;
    private RadioGroup choices;
    private EditText tokenInput;
    private Button refreshButton, tokenButton, gameButton;
    private List<CloudRecord> records = new ArrayList<>();
    private int generation;
    private boolean busy;
    private boolean foreground;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        settings = new CloudSettings(this);
        screen = new HuyUi.Screen(this, "上号器", "粘贴 token，或选择一个已保存的 QQ 账号。");
        tokenInput = new EditText(this);
        tokenInput.setHint("粘贴 access_token");
        tokenInput.setContentDescription("QQ 授权 token");
        tokenInput.setSingleLine(true);
        tokenInput.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        tokenInput.setImeOptions(EditorInfo.IME_ACTION_DONE | EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING);
        tokenInput.setSaveEnabled(false);
        tokenInput.setFreezesText(false);
        tokenInput.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        screen.column.addView(tokenInput, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        tokenButton = screen.button("使用 token", true, view -> resolveToken());
        choices = new RadioGroup(this);
        choices.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        layout.bottomMargin = dp(16);
        screen.column.addView(choices, layout);
        refreshButton = screen.button("刷新账号列表", true, view -> refresh());
        gameButton = screen.button("打开游戏", false, view -> openGame());
        screen.button("云端账号设置", false, view -> startActivity(new Intent(this, CloudAccountActivity.class)));
        screen.footnote("token 需已上传到当前网站账号。选择后打开游戏，点击 QQ 登录，再确认使用这个账号。");
    }

    @Override protected void onResume() { super.onResume(); foreground = true; refresh(); }

    private void refresh() {
        if (busy) return;
        final int attempt = ++generation;
        CloudSettings.Snapshot session = settings.snapshot();
        listSession = null;
        records = new ArrayList<>();
        choices.removeAllViews();
        HuyUi.enabled(gameButton, false);
        if (!session.isSignedIn()) {
            tokenInput.setText("");
            screen.show(settings.needsEndpointSignIn() ? "测试服务地址已更新，请重新登录" : "未登录网站账号",
                    "点击“云端账号设置”，登录测试网站账号后读取已保存的 QQ 账号。");
            setBusy(false);
            return;
        }
        setBusy(true);
        screen.busy("正在读取已保存的账号列表。");
        worker.execute(() -> {
            try {
                List<CloudRecord> result = new CloudClient(session).list();
                runOnUiThread(() -> {
                    if (!active(attempt)) return;
                    setBusy(false);
                    if (!settings.isCurrent(session)) { refresh(); return; }
                    records = result;
                    CloudRecord chosen = find(session.selectedId);
                    if (!session.selectedId.isEmpty() && (chosen == null || chosen.isExpired())) settings.select("");
                    listSession = settings.snapshot();
                    render();
                });
            } catch (CloudException error) {
                runOnUiThread(() -> {
                    if (!active(attempt)) return;
                    setBusy(false);
                    if (!settings.isCurrent(session)) { refresh(); return; }
                    if (error.isAuthenticationRequired()) settings.clearSession();
                    setBusy(false);
                    screen.show("读取失败", error.getMessage());
                });
            }
        });
    }

    private void resolveToken() {
        if (busy || !foreground) return;
        final String token;
        try { token = CloudClient.validateToken(tokenInput.getText().toString()); }
        catch (IllegalArgumentException error) { screen.show("token 无效", error.getMessage()); return; }
        final CloudSettings.Snapshot session = settings.snapshot();
        if (!session.isSignedIn()) { refresh(); return; }
        final int attempt = ++generation;
        setBusy(true);
        screen.busy("正在当前网站账号下查找授权。");
        worker.execute(() -> {
            try {
                CloudClient.Download result = new CloudClient(session).resolveToken(token);
                runOnUiThread(() -> {
                    if (!active(attempt)) return;
                    setBusy(false);
                    if (!settings.isCurrent(session)) { tokenInput.setText(""); refresh(); return; }
                    if (result.record.isExpired()) {
                        screen.show("授权已过期", "请回到抓号器重新扫码。");
                        return;
                    }
                    // Persist only the record ID. The full callback is downloaded again at game login.
                    settings.select(result.record.id);
                    tokenInput.setText("");
                    List<CloudRecord> updated = new ArrayList<>();
                    updated.add(result.record);
                    if (listSession != null && listSession.revision.equals(session.revision)) {
                        for (CloudRecord record : records) if (!record.id.equals(result.record.id)) updated.add(record);
                    }
                    records = updated;
                    listSession = settings.snapshot();
                    render();
                });
            } catch (CloudException error) {
                runOnUiThread(() -> {
                    if (!active(attempt)) return;
                    setBusy(false);
                    if (!settings.isCurrent(session)) { tokenInput.setText(""); refresh(); return; }
                    if (error.isAuthenticationRequired()) {
                        settings.clearSession();
                        listSession = null;
                        records = new ArrayList<>();
                        choices.removeAllViews();
                        tokenInput.setText("");
                    }
                    setBusy(false);
                    screen.show("无法使用 token", error.getMessage());
                });
            }
        });
    }

    private void setBusy(boolean value) {
        busy = value;
        boolean signedIn = settings.snapshot().isSignedIn();
        HuyUi.enabled(refreshButton, !value);
        HuyUi.enabled(tokenButton, !value && signedIn);
        tokenInput.setEnabled(!value && signedIn);
        CloudRecord chosen = listSession == null ? null : find(listSession.selectedId);
        HuyUi.enabled(gameButton, !value && chosen != null && !chosen.isExpired());
        for (int i = 0; i < choices.getChildCount(); i++) {
            choices.getChildAt(i).setEnabled(!value && !records.get(i).isExpired());
        }
    }

    private void render() {
        choices.removeAllViews();
        if (listSession == null) return;
        CloudRecord chosen = find(listSession.selectedId);
        HuyUi.enabled(gameButton, chosen != null && !chosen.isExpired());
        if (records.isEmpty()) {
            screen.show("还没有保存的账号", "请先在抓号器登录同一个网站账号，再扫码上传授权。");
            return;
        }
        if (chosen == null) screen.show("请选择账号", "共 " + records.size() + " 个已保存账号。点击下面的账号，再打开游戏。");
        else screen.show("已选择：" + chosen.label, "尾号 " + chosen.accountHint + " · 有效期至 " + expiry(chosen));
        for (CloudRecord record : records) {
            RadioButton choice = new RadioButton(this);
            choice.setId(android.view.View.generateViewId());
            choice.setText(record.label + "\n尾号 " + record.accountHint + (record.isExpired() ? " · 已过期" : " · 至 " + expiry(record)));
            choice.setTextSize(15);
            choice.setTextColor(0xff222222);
            choice.setPadding(dp(8), dp(12), dp(8), dp(12));
            choice.setButtonTintList(android.content.res.ColorStateList.valueOf(0xff3568a8));
            choice.setEnabled(!record.isExpired());
            choice.setAlpha(record.isExpired() ? 0.5f : 1f);
            choices.addView(choice, new RadioGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
            choice.setChecked(record.id.equals(listSession.selectedId));
            choice.setOnClickListener(view -> choose(record));
        }
    }

    private void choose(CloudRecord record) {
        if (busy || !foreground) return;
        if (listSession == null || !settings.isCurrent(listSession)) { refresh(); return; }
        if (record.isExpired()) { screen.show("授权已过期", "请回到抓号器重新扫码。"); return; }
        settings.select(record.id);
        listSession = settings.snapshot();
        render();
    }

    private CloudRecord find(String id) {
        for (CloudRecord record : records) if (record.id.equals(id)) return record;
        return null;
    }

    private void openGame() {
        if (busy || !foreground) return;
        if (listSession == null || !settings.isCurrent(listSession)) { refresh(); return; }
        CloudRecord record = find(listSession.selectedId);
        if (record == null || record.isExpired()) { refresh(); return; }
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage(RequestPolicy.GAME_PACKAGE);
            if (launch == null) { screen.show("未找到火影忍者", "请先安装游戏。"); return; }
            startActivity(launch);
        } catch (RuntimeException error) { screen.show("无法打开游戏", "请从桌面打开火影忍者，再选择 QQ 登录。"); }
    }

    private boolean active(int attempt) { return foreground && attempt == generation && !isFinishing() && !isDestroyed(); }
    private String expiry(CloudRecord record) { return new SimpleDateFormat("MM月dd日 HH:mm", Locale.SIMPLIFIED_CHINESE).format(new Date(record.expiresAtMs)); }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    @Override protected void onStop() {
        foreground = false;
        generation++;
        busy = false;
        tokenInput.setText("");
        super.onStop();
    }
    @Override protected void onDestroy() { worker.shutdownNow(); super.onDestroy(); }
}
