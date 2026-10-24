package com.huy.authcapture;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.os.Bundle;
import android.widget.LinearLayout;
import android.widget.TextView;
import com.huy.auth.CloudAccountActivity;
import com.huy.auth.CloudSettings;
import com.huy.auth.CredentialStore;

/** Automatic capture is started by a task ticket from the local script. */
public final class MainActivity extends Activity {
    private TextView account,status;
    private CloudSettings settings;
    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        settings=new CloudSettings(this);
        LinearLayout body=CaptureUi.page(this,"抓号器","火影忍者 · QQ");
        account=CaptureUi.text(this,"",13,CaptureUi.MUTED);
        account.setId(R.id.capture_user_id);body.addView(account);
        status=CaptureUi.status(this,body);status.setId(R.id.capture_status);
        status.setText("在网页发起任务后自动扫码\n脚本会填写本次用户和任务信息，无需登录网站账号。扫码结果保存为“自己账号”。");
        CaptureUi.button(this,body,"网站账号设置",false,v -> startActivity(new Intent(this,CloudAccountActivity.class)));
        CaptureUi.button(this,body,"打开上号器 / 查看本人历史",true,v -> {
            Intent launch=getPackageManager().getLaunchIntentForPackage("com.tencent.tim");
            if(launch==null||getPackageManager().checkSignatures(getPackageName(),"com.tencent.tim")
                    !=android.content.pm.PackageManager.SIGNATURE_MATCH) {
                status.setText("未找到配套的上号器，请先安装。");return;
            }
            startActivity(launch);
        });
        CaptureUi.button(this,body,"清除旧版本本机授权",false,v -> new AlertDialog.Builder(this)
                .setTitle("清除旧版本本机授权？").setMessage("只清除旧版本在本机保存的授权，不删除网站历史记录。")
                .setNegativeButton("取消",null).setPositiveButton("清除",(dialog,which) -> {
                    try { CredentialStore.clear(this);new CaptureUploadStore(this).clear();status.setText("旧版本本机授权已清除。"); }
                    catch(Exception ignored) { status.setText("清除失败，请重试。"); }
                }).show());
        body.addView(CaptureUi.text(this,"自动任务的授权和票据只保留在本轮内存中；二维码刷新请在网页操作。本人历史在上号器登录网站账号后查看。",12,CaptureUi.MUTED));
    }
    @Override protected void onResume() {
        super.onResume();
        CloudSettings.Snapshot session=settings.snapshot();
        account.setText(session.isSignedIn()?"网站账号："+session.username:"网站账号：未登录（自动扫码无需登录）");
    }
}
