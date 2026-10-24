package com.huy.authcapture;

import android.content.Context;
import android.content.Intent;
import android.os.Bundle;
import com.huy.auth.AutomationContext;
import com.huy.auth.AutomationWire;
import com.huy.auth.CaptureAutomationState;
import java.lang.ref.WeakReference;
import org.json.JSONObject;

final class CaptureAutomation {
    static final CaptureAutomationState STATE=new CaptureAutomationState();
    private static WeakReference<CaptureActivity> activity=new WeakReference<>(null);
    static void attach(CaptureActivity value) { activity=new WeakReference<>(value); }
    static void detach(CaptureActivity value) { if(activity.get()==value)activity.clear(); }
    static Bundle begin(Context app,String arg) {
        AutomationContext next=AutomationWire.capture(arg);
        return AutomationWire.onMain(() -> {
            if(STATE.begin(next,System.currentTimeMillis())) {
                CaptureActivity old=activity.get(); if(old!=null)old.cancelAutomation();
                Intent launch=new Intent(app,CaptureActivity.class).putExtra("automation_capture_id",next.captureId)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK|Intent.FLAG_ACTIVITY_CLEAR_TOP);
                app.startActivity(launch);
            }
            return status(next.captureId);
        });
    }
    static Bundle status(String id) {
        AutomationContext.requireId(id);
        CaptureAutomationState.Snapshot s=STATE.snapshot(id,System.currentTimeMillis());
        try {
            JSONObject j=new JSONObject().put("captureId",s.captureId).put("state",s.state).put("qrRevision",s.qrRevision);
            if(s.errorCode!=null)j.put("errorCode",s.errorCode);
            return AutomationWire.result(j);
        } catch(Exception ignored) { throw new IllegalStateException("自动化状态不可用"); }
    }
    static Bundle cancel(String id) {
        AutomationContext.requireId(id);
        return AutomationWire.onMain(() -> {
            if(STATE.cancel(id)) { CaptureActivity old=activity.get();if(old!=null)old.cancelAutomation(); }
            try { return AutomationWire.result(new JSONObject().put("captureId",id).put("state","cancelled").put("qrRevision",0)); }
            catch(Exception ignored) { throw new IllegalStateException("自动化状态不可用"); }
        });
    }
}
