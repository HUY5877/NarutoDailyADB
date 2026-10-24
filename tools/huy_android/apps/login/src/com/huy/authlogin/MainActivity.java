package com.huy.authlogin;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.RadioButton;
import android.widget.RadioGroup;
import android.widget.TextView;
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

/** Direct mode only guides the game launch; it never stores or hands off credentials. */
public final class MainActivity extends Activity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final LoginRequestState<Boolean> requests = new LoginRequestState<>();
    private HuyUi.Screen screen;
    private CloudSettings settings;
    private LoginPreferences preferences;
    private CloudSettings.Snapshot listSession;
    private RadioGroup choices;
    private LinearLayout directSection, cloudSection;
    private Button refreshButton, gameButton;
    private List<CloudRecord> records = new ArrayList<>();
    private CloudRecord selected;
    private boolean foreground;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        settings = new CloudSettings(this);
        preferences = new LoginPreferences(this);
        preferences.restore(requests);
        screen = new HuyUi.Screen(this, "上号器", "选择回调 URL 登录或云端账号登录，再打开游戏。");
        RadioGroup modes = HuyUi.selector(screen, screen.column, new String[]{"回调 URL 登录", "云端账号登录"},
                requests.mode() == LoginRequestState.Mode.DIRECT ? 0 : 1, this::switchMode);
        screen.column.removeView(modes);
        screen.column.addView(modes, 0);
        directSection = screen.section();
        TextView instructions = new TextView(this);
        instructions.setText("打开游戏后选择 QQ 登录，在确认页粘贴原始完整 callback_url。回调 URL 登录仅本地解析，无需网站登录。");
        instructions.setTextSize(14);
        instructions.setPadding(0, 8, 0, 20);
        directSection.addView(instructions);
        cloudSection = screen.section();
        choices = new RadioGroup(this);
        choices.setSaveEnabled(false);
        cloudSection.addView(choices);
        refreshButton = screen.button(cloudSection, "刷新账号列表", true, view -> refresh());
        screen.button(cloudSection, "云端账号设置", false, view -> startActivity(new Intent(this, CloudAccountActivity.class)));
        gameButton = screen.button("打开游戏", false, view -> openGame());
        screen.footnote("登录模式切换会清空当前选择。云端模式在游戏确认页仍需手动选择、读取并确认账号。");
        renderMode();
    }

    @Override protected void onResume() {
        super.onResume();
        foreground = true;
        requests.resume();
        clearCloud();
        if (requests.mode() == LoginRequestState.Mode.CLOUD) refresh();
        else { showDirect(); updateControls(); }
    }

    private void switchMode(int index) {
        requests.setMode(index == 0 ? LoginRequestState.Mode.DIRECT : LoginRequestState.Mode.CLOUD);
        preferences.save(requests);
        clearCloud();
        renderMode();
        if (foreground && requests.mode() == LoginRequestState.Mode.CLOUD) refresh();
        else { showDirect(); updateControls(); }
    }

    private void renderMode() {
        boolean direct = requests.mode() == LoginRequestState.Mode.DIRECT;
        directSection.setVisibility(direct ? View.VISIBLE : View.GONE);
        cloudSection.setVisibility(direct ? View.GONE : View.VISIBLE);
    }

    private void showDirect() {
        screen.show("到游戏确认页粘贴完整 callback_url", "点击打开游戏，选择 QQ 登录。读取后核对账号，再点击确认登录；此模式无需网站账号。");
    }

    private void refresh() {
        if (!foreground || requests.mode() != LoginRequestState.Mode.CLOUD || requests.isPending()) return;
        requests.invalidate();
        clearCloud();
        final CloudSettings.Snapshot session = settings.snapshot();
        if (!session.isSignedIn()) {
            screen.show(settings.needsEndpointSignIn() ? "测试服务地址已更新，请重新登录" : "未登录网站账号",
                    "点击云端账号设置，登录网站账号后读取已保存的 QQ 账号。");
            updateControls();
            return;
        }
        final long attempt = requests.begin(session.revision);
        screen.busy("正在读取已保存的账号列表。");
        updateControls();
        worker.execute(() -> {
            try {
                List<CloudRecord> result = new CloudClient(session).list();
                runOnUiThread(() -> {
                    if (!active(attempt, session)) return;
                    requests.accept(attempt, session.revision, Boolean.TRUE);
                    records = result;
                    listSession = session;
                    render();
                    updateControls();
                });
            } catch (CloudException error) {
                runOnUiThread(() -> {
                    if (!active(attempt, session)) return;
                    requests.invalidate();
                    if (error.isAuthenticationRequired()) settings.clearSession();
                    clearCloud();
                    updateControls();
                    screen.show("读取失败", error.getMessage());
                });
            }
        });
    }

    private boolean active(long attempt, CloudSettings.Snapshot session) {
        if (!foreground || isFinishing() || isDestroyed() || !requests.isCurrent(attempt, session.revision)) return false;
        if (!settings.isCurrent(session)) {
            requests.invalidate();
            clearCloud();
            refresh();
            return false;
        }
        return true;
    }

    private void render() {
        choices.removeAllViews();
        if (listSession == null) return;
        if (records.isEmpty()) {
            screen.show("还没有保存的账号", "请先在抓号器登录同一个网站账号，再扫码上传授权。");
            return;
        }
        screen.show(selected == null ? "请选择账号" : "已选择：" + selected.label,
                selected == null ? "共 " + records.size() + " 个已保存账号。手动选择一个账号后打开游戏。"
                        : "尾号 " + selected.accountHint + " · 有效期至 " + expiry(selected));
        for (CloudRecord record : records) {
            RadioButton choice = new RadioButton(this);
            choice.setId(View.generateViewId());
            choice.setSaveEnabled(false);
            choice.setText(record.label + "\n尾号 " + record.accountHint + (record.isExpired() ? " · 已过期" : " · 至 " + expiry(record)));
            choice.setTextSize(15);
            choice.setTextColor(0xff222222);
            choice.setPadding(8, 12, 8, 12);
            choice.setEnabled(!record.isExpired());
            choice.setAlpha(record.isExpired() ? 0.5f : 1f);
            choices.addView(choice, new RadioGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
            choice.setChecked(selected != null && record.id.equals(selected.id));
            choice.setOnClickListener(view -> choose(record));
        }
    }

    private void choose(CloudRecord record) {
        if (!foreground || requests.mode() != LoginRequestState.Mode.CLOUD || requests.isPending()) return;
        if (listSession == null || !settings.isCurrent(listSession)) { refresh(); return; }
        if (record.isExpired()) { screen.show("授权已过期", "请回到抓号器重新扫码。"); return; }
        requests.invalidate();
        selected = record;
        settings.select(record.id); // Only a nonsecret record ID may persist.
        listSession = settings.snapshot();
        render();
        updateControls();
    }

    private void updateControls() {
        boolean direct = requests.mode() == LoginRequestState.Mode.DIRECT;
        HuyUi.enabled(refreshButton, foreground && !direct && !requests.isPending());
        HuyUi.enabled(gameButton, foreground && (direct || (!requests.isPending() && selected != null && !selected.isExpired())));
    }

    private void openGame() {
        if (!foreground || requests.isPending()) return;
        if (requests.mode() == LoginRequestState.Mode.CLOUD) {
            if (listSession == null || !settings.isCurrent(listSession) || selected == null || selected.isExpired()) { refresh(); return; }
        }
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage(RequestPolicy.GAME_PACKAGE);
            if (launch == null) { screen.show("未找到火影忍者", "请先安装游戏。"); return; }
            // The still-live home screen can outlast mode changes made in the game screen.
            // Persist what this screen actually shows before the SDK opens AgentActivity.
            preferences.save(requests);
            startActivity(launch);
        } catch (RuntimeException error) { screen.show("无法打开游戏", "请从桌面打开火影忍者，再选择 QQ 登录。"); }
    }

    private void clearCloud() { selected = null; listSession = null; records = new ArrayList<>(); if (choices != null) choices.removeAllViews(); }
    private String expiry(CloudRecord record) { return new SimpleDateFormat("MM月dd日 HH:mm", Locale.SIMPLIFIED_CHINESE).format(new Date(record.expiresAtMs)); }
    @Override protected void onStop() { foreground = false; requests.stop(); clearCloud(); updateControls(); super.onStop(); }
    @Override protected void onDestroy() { worker.shutdownNow(); super.onDestroy(); }
}
