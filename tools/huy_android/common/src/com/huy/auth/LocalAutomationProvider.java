package com.huy.auth;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Binder;
import android.os.Process;

public abstract class LocalAutomationProvider extends ContentProvider {
    @Override public boolean onCreate() { return true; }
    protected final void checkCaller() {
        if(!AutomationAccess.allowedUid(Binder.getCallingUid(),Process.myUid())) throw new SecurityException("自动化调用方不受信任");
    }
    @Override public Cursor query(Uri u,String[] p,String s,String[] a,String o) { checkCaller();throw unsupported(); }
    @Override public Uri insert(Uri u,ContentValues v) { checkCaller();throw unsupported(); }
    @Override public int update(Uri u,ContentValues v,String s,String[] a) { checkCaller();throw unsupported(); }
    @Override public int delete(Uri u,String s,String[] a) { checkCaller();throw unsupported(); }
    @Override public String getType(Uri u) { checkCaller();throw unsupported(); }
    private static SecurityException unsupported() { return new SecurityException("不支持此自动化操作"); }
}
