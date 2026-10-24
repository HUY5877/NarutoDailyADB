package com.huy.auth;

import java.util.Arrays;
import java.util.LinkedHashSet;

/** Synchronized gate shared by the provider, UI and upload worker. */
public final class CaptureAutomationState {
    private AutomationContext context;
    private String state="error", errorCode="NO_ACTIVE_CAPTURE";
    private byte[] qr;
    private int qrRevision;
    private final LinkedHashSet<String> used = new LinkedHashSet<>();
    public synchronized boolean begin(AutomationContext next,long now) {
        if (next.expiresAtEpochMs <= now) throw new IllegalArgumentException("自动化任务已过期");
        if (context != null && context.captureId.equals(next.captureId)) {
            if (!context.same(next)) throw new IllegalArgumentException("自动化任务不匹配");
            return false;
        }
        if (used.contains(next.captureId)) throw new IllegalArgumentException("自动化任务已失效");
        used.add(next.captureId);
        while (used.size()>64) used.remove(used.iterator().next());
        context=next; state="preparing"; errorCode=null; qr=null; qrRevision=0;
        return true;
    }
    public synchronized AutomationContext context(String id) { return context != null && context.captureId.equals(id) ? context : null; }
    public synchronized boolean isCurrent(AutomationContext expected,long now) {
        expire(now);
        return context==expected && expected!=null && now<expected.expiresAtEpochMs
                && !"refresh_requested".equals(state) && !"error".equals(state) && !"uploaded".equals(state);
    }
    public synchronized boolean update(AutomationContext expected,String next,String error,long now) {
        if (!isCurrent(expected,now)) return false;
        state=next; errorCode=error;
        if ("error".equals(next)||"refresh_requested".equals(next)) qr=null;
        return true;
    }
    public synchronized boolean publishQr(AutomationContext expected,byte[] image,long now) {
        if (!isCurrent(expected,now) || !("preparing".equals(state)||"qr_ready".equals(state))
                || image==null || image.length==0 || image.length>700000 || Arrays.equals(qr,image)) return false;
        qr=image.clone(); qrRevision++; state="qr_ready"; errorCode=null; return true;
    }
    public synchronized byte[] qr(String id,long now) {
        expire(now);
        return context(id)!=null && ("qr_ready".equals(state)||"uploading".equals(state)||"uploaded".equals(state))
                && qr!=null ? qr.clone() : null;
    }
    public synchronized boolean cancel(String id) {
        if (context(id)==null) return false;
        context=null; qr=null; state="error"; errorCode="CANCELLED"; qrRevision=0; return true;
    }
    public synchronized boolean requestRefresh(AutomationContext expected,long now) {
        if(context!=expected||expected==null||now>=expected.expiresAtEpochMs||"uploaded".equals(state))return false;
        state="refresh_requested";errorCode=null;qr=null;return true;
    }
    public synchronized Snapshot snapshot(String id,long now) {
        expire(now);
        return context(id)==null ? new Snapshot(id,"error",0,"NO_ACTIVE_CAPTURE")
                : new Snapshot(id,state,qrRevision,errorCode);
    }
    private void expire(long now) {
        if (context!=null && now>=context.expiresAtEpochMs) {
            qr=null;
            if(!"uploaded".equals(state)) { state="error"; errorCode="CAPTURE_EXPIRED"; }
        }
    }
    public static final class Snapshot {
        public final String captureId,state,errorCode;
        public final int qrRevision;
        Snapshot(String id,String state,int revision,String error) { captureId=id;this.state=state;qrRevision=revision;errorCode=error; }
    }
}
