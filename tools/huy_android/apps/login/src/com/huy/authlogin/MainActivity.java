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
    private boolean foreground;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        settings = new CloudSettings(this);
        preferences = new LoginPreferences(this);
        preferences.restore(requests);
        screen = new HuyUi.Screen(this, "上号器", "回调 URL 可直接登录；网站账号仅用于查看本人历史记录。");
        RadioGroup modes = HuyUi.selector(screen, screen.column, new String[]{"回调 URL 登录", "账号历史"},
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
        refreshButton = screen.button(cloudSection, "刷新历史记录", true, view -> refresh());
        screen.button(cloudSection, "网站账号设置", false, view -> startActivity(new Intent(this, CloudAccountActivity.class)));
        gameButton = screen.button("打开游戏", false, view -> openGame());
        screen.footnote("历史记录仅显示本人账号的备注、尾号和有效期。自动上号使用当前任务的回调 URL，无需网站登录。");
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
                    "点击网站账号设置，登录网站账号后查看本人的 QQ 历史记录。");
            updateControls();
            return;
        }
        final long attempt = requests.begin(session.revision);
        screen.busy("正在读取本人历史记录。");
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
            screen.show("还没有历史记录", "请在网站发起 QQ 扫码任务，完成扫码后刷新。");
            return;
        }
        screen.show("本人账号历史", "共 " + records.size() + " 个账号。这里只显示历史信息，不读取或选择登录凭据。");
        for (CloudRecord record : records) {
            TextView choice = new TextView(this);
            choice.setSaveEnabled(false);
            choice.setText(record.label + "\n尾号 " + record.accountHint + (record.isExpired() ? " · 已过期" : " · 至 " + expiry(record)));
            choice.setTextSize(15);
            choice.setTextColor(0xff222222);
            choice.setPadding(8, 12, 8, 12);
            choice.setAlpha(record.isExpired() ? 0.5f : 1f);
            choices.addView(choice, new RadioGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        }
    }

    private void updateControls() {
        boolean direct = requests.mode() == LoginRequestState.Mode.DIRECT;
        HuyUi.enabled(refreshButton, foreground && !direct && !requests.isPending());
        HuyUi.enabled(gameButton, foreground && !requests.isPending());
    }

    private void openGame() {
        if (!foreground || requests.isPending()) return;
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage(RequestPolicy.GAME_PACKAGE);
            if (launch == null) { screen.show("未找到火影忍者", "请先安装游戏。"); return; }
            // The still-live home screen can outlast mode changes made in the game screen.
            // Persist what this screen actually shows before the SDK opens AgentActivity.
            preferences.save(new LoginRequestState<Boolean>());
            startActivity(launch);
        } catch (RuntimeException error) { screen.show("无法打开游戏", "请从桌面打开火影忍者，再选择 QQ 登录。"); }
    }

    private void clearCloud() { listSession = null; records = new ArrayList<>(); if (choices != null) choices.removeAllViews(); }
    private String expiry(CloudRecord record) { return new SimpleDateFormat("MM月dd日 HH:mm", Locale.SIMPLIFIED_CHINESE).format(new Date(record.expiresAtMs)); }
    @Override protected void onStop() { foreground = false; requests.stop(); clearCloud(); updateControls(); super.onStop(); }
    @Override protected void onDestroy() { worker.shutdownNow(); super.onDestroy(); }
}
