package com.huy.authcapture;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Bundle;
import android.text.InputFilter;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import com.huy.auth.Authorization;
import com.huy.auth.CloudAccountActivity;
import com.huy.auth.CloudClient;
import com.huy.auth.CloudException;
import com.huy.auth.CloudRecord;
import com.huy.auth.CloudSettings;
import com.huy.auth.CredentialStore;
import java.text.DateFormat;
import java.util.Date;

public final class MainActivity extends Activity {
    private TextView account, status;
    private EditText label;
    private Button scan, retry, settingsButton, openLogin, clear;
    private CloudSettings settings;
    private CaptureUploadStore uploadStore;
    private boolean busy;
    private boolean openingCapture;
    private int operation;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        settings = new CloudSettings(this);
        uploadStore = new CaptureUploadStore(this);
        LinearLayout body = CaptureUi.page(this, "抓号器", "火影忍者 · QQ");
        account = CaptureUi.text(this, "", 13, CaptureUi.MUTED);
        LinearLayout.LayoutParams accountParams = new LinearLayout.LayoutParams(-1, -2);
        accountParams.setMargins(0, CaptureUi.dp(this, 8), 0, 0);
        body.addView(account, accountParams);
        status = CaptureUi.status(this, body);
        body.addView(CaptureUi.text(this, "账号备注", 13, CaptureUi.MUTED));
        label = new EditText(this);
        label.setSingleLine(true);
        label.setTextSize(15);
        label.setTextColor(CaptureUi.INK);
        label.setHint("用于上号器里的账号列表");
        label.setFilters(new InputFilter[]{new InputFilter.LengthFilter(40)});
        label.setText(uploadStore.lastLabel());
        LinearLayout.LayoutParams labelParams = new LinearLayout.LayoutParams(-1, CaptureUi.dp(this, 48));
        labelParams.setMargins(0, 0, 0, CaptureUi.dp(this, 16));
        body.addView(label, labelParams);
        scan = CaptureUi.button(this, body, "扫码并上传", true, v -> beginCapture());
        retry = CaptureUi.button(this, body, "重试上传", false, v -> retryUpload());
        settingsButton = CaptureUi.button(this, body, "网站账号设置", false,
                v -> startActivity(new Intent(this, CloudAccountActivity.class)));
        openLogin = CaptureUi.button(this, body, "打开上号器", false, v -> {
            Intent launch = getPackageManager().getLaunchIntentForPackage("com.tencent.tim");
            if (launch == null || getPackageManager().checkSignatures(getPackageName(), "com.tencent.tim")
                    != android.content.pm.PackageManager.SIGNATURE_MATCH) {
                status.setText("未找到配套的上号器，请先安装。"); return;
            }
            startActivity(launch);
        });
        clear = CaptureUi.button(this, body, "清除本机授权", false, v -> confirmClear());
        body.addView(CaptureUi.text(this, "清除本机授权不会删除云端已上传的账号。", 12, CaptureUi.MUTED));
    }

    @Override protected void onResume() {
        super.onResume();
        openingCapture = false;
        if (!busy) refresh();
    }

    private void beginCapture() {
        if (busy || openingCapture) return;
        CloudSettings.Snapshot session = settings.snapshot();
        if (!session.isSignedIn()) {
            status.setText("请先在网站账号设置中登录测试环境。");
            startActivity(new Intent(this, CloudAccountActivity.class)); return;
        }
        final String value;
        try {
            value = CloudClient.validateLabel(label.getText().toString());
        } catch (IllegalArgumentException ignored) {
            label.setError("请输入 1 到 40 个字的账号备注，不能含不可见控制字符"); return;
        }
        Intent intent = new Intent(this, CaptureActivity.class);
        intent.putExtra("capture_label", value);
        intent.putExtra("capture_revision", session.revision);
        openingCapture = true;
        startActivity(intent);
    }

    private void retryUpload() {
        if (busy) return;
        final CloudSettings.Snapshot session = settings.snapshot();
        final Authorization authorization;
        final CaptureUploadStore.State pending;
        try {
            authorization = CredentialStore.load(this);
            if (authorization == null) { refresh(); return; }
            authorization.responseJson(System.currentTimeMillis());
            pending = uploadStore.load(authorization);
            if (pending == null || !pending.belongsTo(session)) {
                status.setText("请登录这份本机授权所属的网站账号后重试。"); return;
            }
            if (pending.uploaded()) { refresh(); return; }
        } catch (Exception ignored) {
            status.setText("本机授权不可用或已过期，请重新扫码。"); return;
        }
        setBusy(true);
        final int request = ++operation;
        status.setText("本机授权已保存\n正在上传到测试环境…");
        new Thread(() -> {
            CloudRecord record = null;
            String failure = null;
            try {
                if (!settings.isCurrent(session)) throw new IllegalStateException();
                record = new CloudClient(session).upload(pending.label, authorization);
            } catch (CloudException problem) {
                failure = problem.getMessage();
            } catch (Exception ignored) {
                failure = "上传失败，请稍后重试。";
            }
            final CloudRecord uploaded = record;
            final String message = failure;
            runOnUiThread(() -> {
                if (request != operation || isFinishing() || isDestroyed()) return;
                setBusy(false);
                if (!settings.isCurrent(session)) { refresh(); return; }
                if (uploaded == null) {
                    refresh();
                    status.setText("本机授权已保存，云端未确认上传成功。\n" + message);
                    return;
                }
                try {
                    uploadStore.markUploaded(authorization, session, uploaded.id);
                    refresh();
                } catch (Exception ignored) {
                    status.setText("云端已确认上传成功。\n本机状态保存失败，可在上号器刷新账号列表。");
                }
            });
        }, "huy-upload-retry").start();
    }

    private void confirmClear() {
        if (busy) return;
        new AlertDialog.Builder(this).setTitle("清除本机授权？")
            .setMessage("会删除这台模拟器中的授权和待上传记录。云端已上传的账号仍然保留。")
            .setNegativeButton("取消", null).setPositiveButton("清除", (dialog, which) -> {
                if (busy) return;
                try {
                    CredentialStore.clear(this);
                    uploadStore.clear();
                    android.webkit.CookieManager.getInstance().removeAllCookies(null);
                    android.webkit.WebStorage.getInstance().deleteAllData();
                    refresh();
                } catch (Exception ignored) { status.setText("清除失败，请重试。"); }
            }).show();
    }

    private void refresh() {
        CloudSettings.Snapshot session = settings.snapshot();
        account.setText(session.isSignedIn() ? "网站账号：" + session.username
                : settings.needsEndpointSignIn() ? "测试服务地址已更新，请重新登录网站账号" : "网站账号：未登录");
        retry.setVisibility(View.GONE);
        try {
            Authorization authorization = CredentialStore.load(this);
            if (authorization == null) {
                status.setText("本机：未保存授权\n登录网站账号后，扫码并上传。"); return;
            }
            authorization.responseJson(System.currentTimeMillis());
            CaptureUploadStore.State state = uploadStore.load(authorization);
            String expiry = DateFormat.getDateTimeInstance(DateFormat.MEDIUM, DateFormat.SHORT)
                    .format(new Date(authorization.expiresAtMs()));
            if (state == null) {
                status.setText("本机有旧授权，尚未关联网站账号。\n请重新扫码并上传。\n有效期：" + expiry);
            } else if (!state.belongsTo(session)) {
                status.setText("本机授权属于其他网站账号。\n请切回原账号，或重新扫码。\n有效期：" + expiry);
            } else if (state.uploaded()) {
                status.setText("云端已确认上传：" + state.label + "\n有效期：" + expiry + "\n上号器刷新列表后可选用。");
            } else {
                retry.setVisibility(View.VISIBLE);
                status.setText("本机已保存：" + state.label + "\n云端未确认上传成功，可重试上传。\n有效期：" + expiry);
            }
        } catch (Exception ignored) {
            status.setText("本机授权不可用或已过期\n请重新扫码并上传。");
        }
    }

    private void setBusy(boolean value) {
        busy = value;
        for (Button button : new Button[]{scan, retry, settingsButton, openLogin, clear}) button.setEnabled(!value);
        label.setEnabled(!value);
    }

    @Override protected void onDestroy() {
        ++operation;
        super.onDestroy();
    }
}
