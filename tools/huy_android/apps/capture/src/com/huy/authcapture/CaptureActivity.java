package com.huy.authcapture;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.net.http.SslError;
import android.os.Bundle;
import android.os.Handler;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.CookieManager;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebStorage;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import com.huy.auth.Authorization;
import com.huy.auth.CloudClient;
import com.huy.auth.CloudException;
import com.huy.auth.CloudRecord;
import com.huy.auth.CloudSettings;
import com.huy.auth.CredentialStore;
import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.UUID;
import javax.net.ssl.HttpsURLConnection;
import org.json.JSONObject;

/** Independent QQ web authorization. No game launch, log access or exported callback. */
public final class CaptureActivity extends Activity {
    private final Object flowLock = new Object();
    private final Handler handler = new Handler();
    private WebView web;
    private LinearLayout page;
    private TextView status;
    private Button refreshButton;
    private volatile int generation;
    private volatile boolean active, completing;
    private String state, label;
    private CloudSettings settings;
    private CloudSettings.Snapshot session;
    private CaptureUploadStore uploadStore;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        settings = new CloudSettings(this); session = settings.snapshot();
        uploadStore = new CaptureUploadStore(this);
        if (!session.isSignedIn() || !session.revision.equals(getIntent().getStringExtra("capture_revision"))) {
            android.widget.Toast.makeText(this,"请返回首页，登录网站账号后重新扫码。",1).show(); finish(); return;
        }
        try { label = CloudClient.validateLabel(getIntent().getStringExtra("capture_label")); }
        catch (IllegalArgumentException ignored) { finish(); return; }
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
        getWindow().setStatusBarColor(Color.WHITE);
        getWindow().setNavigationBarColor(CaptureUi.BG);
        getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        page = new LinearLayout(this); page.setOrientation(LinearLayout.VERTICAL); page.setBackgroundColor(CaptureUi.BG);
        LinearLayout toolbar = new LinearLayout(this); toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(CaptureUi.dp(this,16),0,CaptureUi.dp(this,16),0); toolbar.setBackgroundColor(Color.WHITE);
        toolbar.addView(CaptureUi.text(this,"扫码",20,CaptureUi.INK),new LinearLayout.LayoutParams(0,-2,1));
        TextView mark = CaptureUi.text(this,"HUY",12,CaptureUi.MUTED); mark.setTypeface(Typeface.MONOSPACE); toolbar.addView(mark);
        page.addView(toolbar,new LinearLayout.LayoutParams(-1,CaptureUi.dp(this,56)));
        LinearLayout actions = new LinearLayout(this);
        refreshButton = new Button(this); refreshButton.setText("刷新二维码"); refreshButton.setOnClickListener(v -> startFlow());
        Button back = new Button(this); back.setText("返回"); back.setOnClickListener(v -> finish());
        actions.addView(refreshButton,new LinearLayout.LayoutParams(0,-2,1));
        actions.addView(back,new LinearLayout.LayoutParams(0,-2,1)); page.addView(actions);
        status = CaptureUi.text(this,"正在打开 QQ 授权页…",14,CaptureUi.INK);
        status.setPadding(CaptureUi.dp(this,16),CaptureUi.dp(this,10),CaptureUi.dp(this,16),CaptureUi.dp(this,10));
        page.addView(status,new LinearLayout.LayoutParams(-1,-2)); setContentView(page);
        startFlow();
    }

    private void startFlow() {
        if (completing) return;
        if (!settings.isCurrent(session)) { status.setText("网站账号已变化，请返回首页重新开始。"); return; }
        final int flow;
        synchronized (flowLock) { active = false; flow = ++generation; }
        handler.removeCallbacksAndMessages(null); destroyWebView();
        WebStorage.getInstance().deleteAllData();
        CookieManager.getInstance().removeAllCookies(done -> {
            if (flow != generation || isFinishing() || isDestroyed()) return;
            createWebView(flow); state = UUID.randomUUID().toString(); active = true;
            status.setText("正在打开 QQ 授权页…");
            web.loadUrl(QqWebAuthorization.startUrl(state));
        });
    }

    private void createWebView(final int flow) {
        web = new WebView(this); page.addView(web,new LinearLayout.LayoutParams(-1,0,1));
        WebView.setWebContentsDebuggingEnabled(false);
        WebSettings s = web.getSettings(); s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false); s.setAllowContentAccess(false); s.setAllowFileAccessFromFileURLs(false);
        s.setAllowUniversalAccessFromFileURLs(false); s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE); s.setJavaScriptCanOpenWindowsAutomatically(false); s.setSupportMultipleWindows(false);
        CookieManager.getInstance().setAcceptCookie(true);
        // The QQ web entry uses cross-site login cookies. The flow still blocks non-Tencent network requests.
        CookieManager.getInstance().setAcceptThirdPartyCookies(web,true);
        web.setWebChromeClient(new WebChromeClient() {
            @Override public boolean onConsoleMessage(ConsoleMessage message) { return true; }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView v,String url) { return flow != generation || navigate(url,flow); }
            @Override public boolean shouldOverrideUrlLoading(WebView v,WebResourceRequest r) {
                if (flow != generation) return true;
                return r.isForMainFrame() ? navigate(r.getUrl().toString(),flow) : !trustedHttps(r.getUrl());
            }
            @Override public void onPageFinished(WebView v,String url) {
                if (flow == generation && active && !completing && trustedHttps(Uri.parse(url))) {
                    status.setText("请用 QQ 扫码并确认"); checkProviderError(flow);
                }
            }
            @Override public void onReceivedSslError(WebView v,SslErrorHandler h,SslError error) {
                h.cancel(); if (flow == generation) status.setText("连接证书校验失败，请检查模拟器日期和网络。");
            }
            @Override public void onReceivedError(WebView v,WebResourceRequest r,WebResourceError e) {
                if (flow == generation && r.isForMainFrame() && !completing) status.setText("授权页面加载失败，请检查网络后刷新。");
            }
            @Override public WebResourceResponse shouldInterceptRequest(WebView v,WebResourceRequest r) {
                if (trustedHttps(r.getUrl()) || "about".equals(r.getUrl().getScheme())) return null;
                return new WebResourceResponse("text/plain","UTF-8",new ByteArrayInputStream(new byte[0]));
            }
        });
    }

    private void checkProviderError(final int flow) {
        if (flow != generation || !active || completing || web == null) return;
        // Only a boolean leaves the page. Never read page contents, cookies or credentials into diagnostics.
        web.evaluateJavascript("(function(){var t=document.body?document.body.innerText:'';return t.indexOf('100044')>=0;})()", value -> {
            if (flow != generation || !active || completing) return;
            if ("true".equals(value)) status.setText("QQ 拒绝了应用身份校验（100044）。本次没有取得授权。");
            else handler.postDelayed(() -> checkProviderError(flow),1500);
        });
    }

    private static boolean trustedHttps(Uri uri) {
        if (!"https".equals(uri.getScheme()) || uri.getHost()==null || uri.getUserInfo()!=null
                || uri.getPort()!=-1 && uri.getPort()!=443) return false;
        String host=uri.getHost().toLowerCase(Locale.ROOT);
        for (String domain:new String[]{"qq.com","gtimg.com","gtimg.cn","qpic.cn","qlogo.cn","idqqimg.com","tencent.com"})
            if (host.equals(domain) || host.endsWith("."+domain)) return true;
        return false;
    }

    private boolean navigate(String url,int flow) {
        Uri uri;
        try { uri=Uri.parse(url); } catch (Exception ignored) { return true; }
        if ("auth".equals(uri.getScheme())) {
            if ("cancel".equals(uri.getHost()) || "close".equals(uri.getHost())) { finish(); return true; }
            if (flow==generation && active && !completing && "tauth.qq.com".equals(uri.getHost())) receive(url,flow);
            return true;
        }
        if (!trustedHttps(uri)) { status.setText("请在本页完成 QQ 扫码。外部应用跳转已停止。"); return true; }
        return false;
    }

    private void receive(String callback,final int flow) {
        try {
            final Authorization authorization=Authorization.fromCallback(callback,System.currentTimeMillis());
            final JSONObject fields=new JSONObject(authorization.responseJson(System.currentTimeMillis()));
            if (fields.has("state") && !state.equals(fields.getString("state"))) throw new IllegalArgumentException();
            completing=true; refreshButton.setEnabled(false); web.stopLoading();
            handler.removeCallbacksAndMessages(null); status.setText("正在核对授权…");
            new Thread(() -> complete(authorization,fields,flow),"huy-capture-upload").start();
        } catch (Exception ignored) { status.setText("收到的授权不完整或已失效，请刷新后重试。原授权已保留。"); }
    }

    private boolean current(int flow) { return flow==generation && active && settings.isCurrent(session); }

    private void complete(Authorization authorization,JSONObject fields,final int flow) {
        boolean saved=false;
        try {
            if (!verifyWithQQ(fields)) throw new IllegalArgumentException();
            synchronized (flowLock) {
                if (!current(flow)) return;
                CredentialStore.save(this,authorization); uploadStore.savePending(authorization,session,label); saved=true;
            }
            runOnUiThread(() -> { if (current(flow)) { destroyWebView(); status.setText("本机授权已保存，正在上传到测试环境…"); } });
            CloudRecord record=new CloudClient(session).upload(label,authorization,new CloudClient.UploadGuard() {
                @Override public void checkAllowed() throws CloudException {
                    synchronized (flowLock) { if (!current(flow)) throw CloudClient.uploadCancelled(); }
                }
                @Override public void beforeSend() throws CloudException { checkAllowed(); }
            });
            synchronized (flowLock) {
                if (!current(flow)) return;
                uploadStore.markUploaded(authorization,session,record.id);
            }
            runOnUiThread(() -> { if (current(flow)) { active=false; finish(); } });
        } catch (Exception problem) {
            final boolean wasSaved=saved;
            final String detail=problem instanceof CloudException ? problem.getMessage() : "请检查网络后重试。";
            runOnUiThread(() -> {
                if (!current(flow) || isFinishing() || isDestroyed()) return;
                completing=false; refreshButton.setEnabled(true);
                status.setText(wasSaved ? "本机授权已保存，云端尚未确认成功。\n"+detail+"\n返回首页可重试上传，无需再次扫码。"
                        : "QQ 授权核验未通过，请刷新后重新扫码。原授权已保留。");
            });
        }
    }

    private static boolean verifyWithQQ(JSONObject fields) {
        HttpsURLConnection connection=null;
        try {
            String address=Uri.parse("https://graph.qq.com/oauth2.0/me").buildUpon().appendQueryParameter("access_token",fields.getString("access_token")).build().toString();
            connection=(HttpsURLConnection)new URL(address).openConnection();
            connection.setConnectTimeout(15000); connection.setReadTimeout(15000); connection.setUseCaches(false); connection.setInstanceFollowRedirects(false);
            if (connection.getResponseCode()!=200) return false;
            ByteArrayOutputStream bytes=new ByteArrayOutputStream();
            try (InputStream input=connection.getInputStream()) {
                byte[] buffer=new byte[2048]; int n;
                while ((n=input.read(buffer))!=-1) { if (bytes.size()+n>16384) return false; bytes.write(buffer,0,n); }
            }
            String response=new String(bytes.toByteArray(),StandardCharsets.UTF_8).trim();
            int start=response.indexOf('{'),end=response.lastIndexOf('}'); if (start<0 || end<=start) return false;
            JSONObject info=new JSONObject(response.substring(start,end+1));
            return "1104307008".equals(info.optString("client_id")) && fields.getString("openid").equals(info.optString("openid")) && !info.has("error");
        } catch (Exception ignored) { return false; }
        finally { if (connection!=null) connection.disconnect(); }
    }

    private void destroyWebView() {
        if (web!=null) {
            web.stopLoading(); web.setWebViewClient(new WebViewClient()); web.clearCache(true); web.clearHistory();
            ((ViewGroup)web.getParent()).removeView(web); web.destroy(); web=null;
        }
    }
    @Override public void finish() {
        synchronized (flowLock) { active=false; ++generation; }
        super.finish();
    }
    @Override protected void onDestroy() {
        synchronized (flowLock) { active=false; ++generation; }
        handler.removeCallbacksAndMessages(null); destroyWebView();
        CookieManager.getInstance().removeAllCookies(null); WebStorage.getInstance().deleteAllData();
        super.onDestroy();
    }
}
