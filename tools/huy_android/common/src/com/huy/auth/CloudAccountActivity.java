package com.huy.auth;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Native website account settings. QQ WebView cookies are never used here. */
public final class CloudAccountActivity extends Activity {
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private CloudSettings settings;
    private EditText endpoint, username, password;
    private Button login, logout;
    private TextView status;
    private boolean busy;

    @Override public void onCreate(Bundle savedState) {
        super.onCreate(savedState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        getWindow().setStatusBarColor(Color.WHITE);
        getWindow().setNavigationBarColor(0xfff4f4f4);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        settings = new CloudSettings(this);
        CloudSettings.Snapshot saved = settings.snapshot();
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(0xfff4f4f4);
        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setBackgroundColor(Color.WHITE);
        toolbar.setPadding(dp(24), 0, dp(24), 0);
        toolbar.addView(text("网站账号", 20, 0xff222222), new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        TextView brand = text("HUY", 12, 0xff777777);
        brand.setTypeface(Typeface.MONOSPACE);
        toolbar.addView(brand);
        root.addView(toolbar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(56)));
        ScrollView scroll = new ScrollView(this);
        root.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        FrameLayout host = new FrameLayout(this);
        scroll.addView(host);
        LinearLayout column = new LinearLayout(this) {
            @Override protected void onMeasure(int widthMeasureSpec, int heightMeasureSpec) {
                super.onMeasure(MeasureSpec.makeMeasureSpec(Math.min(MeasureSpec.getSize(widthMeasureSpec), dp(480)), MeasureSpec.EXACTLY), heightMeasureSpec);
            }
        };
        column.setOrientation(LinearLayout.VERTICAL);
        column.setPadding(dp(24), dp(24), dp(24), dp(24));
        host.addView(column, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP | Gravity.CENTER_HORIZONTAL));
        status = text(saved.isSignedIn() ? "已登录：" + saved.username
                : settings.needsEndpointSignIn() ? "网站服务地址已更新，请重新登录网站账号。"
                : "登录网站账号后可查看本人历史记录；自动扫码和回调 URL 登录无需登录。", 14, 0xff777777);
        column.addView(status, spacing(0, 20));
        endpoint = input(column, "网站服务地址", saved.endpoint, InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        username = input(column, "网站账号", saved.username, InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);
        password = input(column, "网站密码", "", InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        password.setSaveEnabled(false);
        password.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        login = button(column, "登录", true, view -> signIn());
        logout = button(column, "退出网站账号", false, view -> signOut());
        logout.setEnabled(saved.isSignedIn());
        button(column, "返回", false, view -> finish());
        TextView note = text("使用网站账号登录。密码不会保存在手机里。", 12, 0xff777777);
        column.addView(note, spacing(10, 0));
        setContentView(root);
    }

    private void signIn() {
        if (busy) return;
        String address = endpoint.getText().toString();
        String name = username.getText().toString();
        String secret = password.getText().toString();
        password.setText("");
        CloudSettings.Snapshot before = settings.snapshot();
        setBusy(true);
        status.setText("正在连接网站并登录…");
        worker.execute(() -> {
            try {
                CloudClient.SignIn session = CloudClient.signIn(address, name, secret);
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed()) return;
                    setBusy(false);
                    if (!settings.isCurrent(before)) {
                        status.setText("账号设置已变化，请重新登录。");
                        return;
                    }
                    settings.saveSession(session);
                    finish();
                });
            } catch (CloudException error) {
                runOnUiThread(() -> {
                    if (isFinishing() || isDestroyed()) return;
                    setBusy(false);
                    status.setText(error.getMessage());
                });
            }
        });
    }

    private void signOut() {
        if (busy) return;
        settings.clearSession();
        password.setText("");
        username.setText("");
        status.setText("已退出。云端保存的 QQ 授权会保留。");
        logout.setEnabled(false);
    }

    private void setBusy(boolean value) {
        busy = value;
        endpoint.setEnabled(!value); username.setEnabled(!value); password.setEnabled(!value);
        login.setEnabled(!value); logout.setEnabled(!value && settings.snapshot().isSignedIn());
    }

    private EditText input(LinearLayout column, String label, String value, int type) {
        column.addView(text(label, 13, 0xff777777), spacing(0, 4));
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setTextSize(16);
        input.setInputType(type);
        input.setText(value);
        input.setSaveEnabled(false);
        input.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        input.setContentDescription(label);
        column.addView(input, spacing(0, 14));
        return input;
    }

    private Button button(LinearLayout column, String title, boolean primary, View.OnClickListener action) {
        Button button = new Button(this);
        button.setText(title); button.setTextSize(16); button.setAllCaps(false);
        button.setTextColor(primary ? Color.WHITE : 0xff222222);
        button.setMinHeight(0); button.setMinimumHeight(0);
        button.setElevation(0); button.setStateListAnimator(null);
        android.graphics.drawable.GradientDrawable background = new android.graphics.drawable.GradientDrawable();
        background.setColor(primary ? 0xff3568a8 : Color.WHITE);
        background.setCornerRadius(dp(3));
        if (!primary) background.setStroke(dp(1), 0xffdddddd);
        button.setBackground(background);
        button.setOnClickListener(action);
        LinearLayout.LayoutParams layout = spacing(0, 10); layout.height = dp(44);
        column.addView(button, layout);
        return button;
    }

    private TextView text(String value, int size, int color) {
        TextView result = new TextView(this);
        result.setText(value); result.setTextSize(size); result.setTextColor(color);
        result.setLineSpacing(dp(4), 1);
        return result;
    }

    private LinearLayout.LayoutParams spacing(int top, int bottom) {
        LinearLayout.LayoutParams layout = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        layout.topMargin = dp(top); layout.bottomMargin = dp(bottom);
        return layout;
    }
    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    @Override protected void onDestroy() {
        password.setText("");
        worker.shutdownNow();
        super.onDestroy();
    }
}
