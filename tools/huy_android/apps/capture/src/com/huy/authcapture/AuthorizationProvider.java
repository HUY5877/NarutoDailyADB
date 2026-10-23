package com.huy.authcapture;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.Binder;
import com.huy.auth.Authorization;
import com.huy.auth.CredentialStore;

public final class AuthorizationProvider extends ContentProvider {
    @Override public boolean onCreate() { return true; }
    private void check(Uri uri) {
        if (!"com.huy.authcapture.credentials".equals(uri.getAuthority()) || !"/authorization".equals(uri.getPath()))
            throw new IllegalArgumentException("不支持的资源");
        getContext().enforceCallingPermission("com.huy.authcapture.permission.READ_AUTHORIZATION","仅供 HUY 上号器使用");
        String[] packages = getContext().getPackageManager().getPackagesForUid(Binder.getCallingUid());
        boolean allowed = false;
        if (packages != null) for (String name : packages) {
            if ("com.tencent.tim".equals(name) && getContext().getPackageManager().checkSignatures(getContext().getPackageName(),name) == PackageManager.SIGNATURE_MATCH) allowed = true;
        }
        if (!allowed) throw new SecurityException("调用方不受信任");
    }
    @Override public Cursor query(Uri uri,String[] projection,String selection,String[] args,String order) {
        check(uri);
        MatrixCursor result = new MatrixCursor(new String[]{"authorization"});
        try {
            Authorization auth = CredentialStore.load(getContext());
            if (auth != null) { auth.responseJson(System.currentTimeMillis()); result.addRow(new Object[]{auth.toJson()}); }
        } catch (Exception ignored) { /* No credential material is logged or returned on failure. */ }
        return result;
    }
    @Override public String getType(Uri uri) { check(uri); return "vnd.android.cursor.item/vnd.huy.authorization"; }
    @Override public Uri insert(Uri u,ContentValues v) { throw new SecurityException("只读接口"); }
    @Override public int update(Uri u,ContentValues v,String s,String[] a) { throw new SecurityException("只读接口"); }
    @Override public int delete(Uri u,String s,String[] a) { throw new SecurityException("只读接口"); }
}
