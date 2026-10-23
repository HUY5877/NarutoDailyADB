package com.huy.authlogin;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.ViewGroup;
import android.widget.Button;
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

/** Lists metadata only. A complete QQ authorization is fetched inside AgentActivity. */
public final class MainActivity extends Activity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private HuyUi.Screen screen;
    private CloudSettings settings;
    private CloudSettings.Snapshot listSession;
    private RadioGroup choices;
    private Button refreshButton, gameButton;
    private List<CloudRecord> records = new ArrayList<>();
    private int generation;
    private boolean busy;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        settings = new CloudSettings(this);
        screen = new HuyUi.Screen(this, "上号器", "选择一个已保存的 QQ 账号。");
        choices = new RadioGroup(this);
        choices.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        layout.bottomMargin = dp(16);
        screen.column.addView(choices, layout);
        refreshButton = screen.button("刷新账号列表", true, view -> refresh());
        gameButton = screen.button("打开游戏", false, view -> openGame());
        screen.button("云端账号设置", false, view -> startActivity(new Intent(this, CloudAccountActivity.class)));
        screen.footnote("选择后打开游戏，点击 QQ 登录，再确认使用这个账号。");
    }

    @Override protected void onResume() { super.onResume(); refresh(); }

    private void refresh() {
        if (busy) return;
        final int attempt = ++generation;
        CloudSettings.Snapshot session = settings.snapshot();
        listSession = null;
        records = new ArrayList<>();
        choices.removeAllViews();
        HuyUi.enabled(gameButton, false);
        if (!session.isSignedIn()) {
            screen.show("未登录网站账号", "点击“云端账号设置”，登录测试网站账号后读取已保存的 QQ 账号。");
            HuyUi.enabled(refreshButton, true);
            return;
        }
        busy = true;
        HuyUi.enabled(refreshButton, false);
        screen.busy("正在读取已保存的账号列表。");
        worker.execute(() -> {
            try {
                List<CloudRecord> result = new CloudClient(session).list();
                runOnUiThread(() -> {
                    if (!active(attempt)) return;
                    busy = false;
                    HuyUi.enabled(refreshButton, true);
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
                    busy = false;
                    HuyUi.enabled(refreshButton, true);
                    if (!settings.isCurrent(session)) { refresh(); return; }
                    if (error.isAuthenticationRequired()) settings.clearSession();
                    screen.show("读取失败", error.getMessage());
                });
            }
        });
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
        if (listSession == null || !settings.isCurrent(listSession)) { refresh(); return; }
        CloudRecord record = find(listSession.selectedId);
        if (record == null || record.isExpired()) { refresh(); return; }
        try {
            Intent launch = getPackageManager().getLaunchIntentForPackage(RequestPolicy.GAME_PACKAGE);
            if (launch == null) { screen.show("未找到火影忍者", "请先安装游戏。"); return; }
            startActivity(launch);
        } catch (RuntimeException error) { screen.show("无法打开游戏", "请从桌面打开火影忍者，再选择 QQ 登录。"); }
    }

    private boolean active(int attempt) { return attempt == generation && !isFinishing() && !isDestroyed(); }
    private String expiry(CloudRecord record) { return new SimpleDateFormat("MM月dd日 HH:mm", Locale.SIMPLIFIED_CHINESE).format(new Date(record.expiresAtMs)); }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }
    @Override protected void onStop() { generation++; busy = false; super.onStop(); }
    @Override protected void onDestroy() { worker.shutdownNow(); super.onDestroy(); }
}
