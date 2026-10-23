package com.huy.authlogin;

import android.content.Context;
import android.content.pm.PackageManager;
import android.content.pm.ProviderInfo;
import android.database.Cursor;
import android.net.Uri;

import com.huy.auth.Authorization;
import com.huy.auth.CredentialStore;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/** All credentials stay in memory or the app-private CredentialStore. */
public final class AuthRepository {
    private static final Uri CAPTURE_URI =
            Uri.parse("content://com.huy.authcapture.credentials/authorization");

    private AuthRepository() {}

    public static final class Outcome {
        public final Authorization authorization;
        public final String title;
        public final String detail;

        private Outcome(Authorization authorization, String title, String detail) {
            this.authorization = authorization;
            this.title = title;
            this.detail = detail;
        }

        public boolean isReady() {
            return authorization != null;
        }
    }

    public static Outcome loadPreferred(Context context) {
        Outcome capture = readCapture(context);
        if (capture.isReady()) return capture;
        try {
            Authorization saved = CredentialStore.load(context);
            if (saved == null) return capture;
            saved.responseJson(System.currentTimeMillis());
            return ready(saved, "正在使用本机缓存。有效期至 " + expiry(saved));
        } catch (Exception error) {
            return invalid(error);
        }
    }

    public static Outcome readCapture(Context context) {
        try {
            PackageManager packages = context.getPackageManager();
            ProviderInfo provider = packages.resolveContentProvider(CAPTURE_URI.getAuthority(), 0);
            if (provider == null) {
                return unavailable("未找到抓号器", "请先安装抓号器并扫码，或导入以前保存的完整授权文件。");
            }
            if (!"com.huy.authcapture".equals(provider.packageName)
                    || packages.checkSignatures(context.getPackageName(), provider.packageName)
                    != PackageManager.SIGNATURE_MATCH) {
                return unavailable("抓号器签名不匹配", "请安装同一套签名的抓号器与上号器。");
            }
        } catch (RuntimeException error) {
            return unavailable("无法检查抓号器", "请确认抓号器与上号器已正确安装后重试。");
        }
        try (Cursor cursor = context.getContentResolver().query(
                CAPTURE_URI, new String[] { "authorization" }, null, null, null)) {
            if (cursor == null || !cursor.moveToFirst()) {
                return unavailable("暂无可用授权", "请先在抓号器扫码，或导入以前保存的完整授权文件。");
            }
            int column = cursor.getColumnIndex("authorization");
            if (column < 0) {
                return unavailable("读取授权失败", "抓号器返回的数据不完整，请更新抓号器与上号器后重试。");
            }
            String json = cursor.getString(column);
            if (json == null || json.length() > AuthorizationDocument.MAX_BYTES
                    || json.getBytes(StandardCharsets.UTF_8).length > AuthorizationDocument.MAX_BYTES) {
                return unavailable("授权格式无效", "请在抓号器重新扫码。");
            }
            Authorization authorization = Authorization.fromJson(json, System.currentTimeMillis());
            CredentialStore.save(context, authorization);
            return ready(authorization, "已从抓号器同步。有效期至 " + expiry(authorization));
        } catch (SecurityException error) {
            return unavailable("无法读取抓号器", "请安装同一套签名的抓号器与上号器，或主动导入授权文件。");
        } catch (IllegalArgumentException error) {
            return invalid(error);
        } catch (Exception error) {
            return unavailable("读取授权失败", "请确认抓号器已安装并完成扫码，然后重试。");
        }
    }

    public static Outcome importDocument(Context context, Uri uri) {
        if (uri == null || !"content".equalsIgnoreCase(uri.getScheme())) {
            return unavailable("无法读取文件", "请通过系统文件选择器选择授权 JSON 文件。");
        }
        try (InputStream stream = context.getContentResolver().openInputStream(uri)) {
            if (stream == null) {
                return unavailable("无法读取文件", "文件提供方未返回内容，请重新选择文件。");
            }
            String json = AuthorizationDocument.read(stream);
            Authorization authorization = Authorization.fromJson(json, System.currentTimeMillis());
            CredentialStore.save(context, authorization);
            return ready(authorization, "授权文件已导入。有效期至 " + expiry(authorization));
        } catch (AuthorizationDocument.TooLargeException error) {
            return unavailable("文件过大", "授权文件不能超过 64 KiB，请选择完整授权 JSON 文件。");
        } catch (IllegalArgumentException error) {
            return invalid(error);
        } catch (Exception error) {
            return unavailable("导入失败", "文件无法读取或保存。请重新选择 UTF-8 编码的完整授权 JSON 文件。");
        }
    }

    public static Outcome clear(Context context) {
        try {
            CredentialStore.clear(context);
            return unavailable("本机缓存已清除", "抓号器中的授权保留。需要时可再次点击“读取抓号器”。");
        } catch (Exception error) {
            return unavailable("清除失败", "暂时无法删除本机缓存，请重试。");
        }
    }

    public static Outcome expired() {
        return unavailable("授权已过期", "请回到抓号器重新扫码。重新导入同一文件不会延长有效期。");
    }

    public static Outcome unavailable(String title, String detail) {
        return new Outcome(null, title, detail);
    }

    private static Outcome ready(Authorization authorization, String detail) {
        return new Outcome(authorization, "授权可用", detail);
    }

    private static Outcome invalid(Exception error) {
        // The shared parser guarantees fixed messages without input values.
        if ("Authorization expired".equals(error.getMessage())) return expired();
        return unavailable("授权格式无效", "需要包含原始回调与捕获时间的完整授权 JSON，请重新扫码或选择正确文件。");
    }

    private static String expiry(Authorization authorization) {
        return new SimpleDateFormat("MM月dd日 HH:mm", Locale.SIMPLIFIED_CHINESE)
                .format(new Date(authorization.expiresAtMs()));
    }
}
