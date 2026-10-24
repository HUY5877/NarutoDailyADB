package com.huy.authlogin;

import com.huy.auth.AutomationContext;
import com.huy.auth.AutomationWire;
import com.tencent.open.agent.AgentActivity;
import android.os.Bundle;
import java.lang.ref.WeakReference;
import org.json.JSONObject;

/** An active game request is mandatory; this bridge never queues a credential for later. */
public final class LoginAutomation {
    private static WeakReference<AgentActivity> activity=new WeakReference<>(null);
    private static final AutomationLoginState STATE=new AutomationLoginState();
    private LoginAutomation() {}
    public static void attach(AgentActivity current) { activity=new WeakReference<>(current);STATE.invalidate(); }
    public static void detach(AgentActivity current) {
        if(activity.get()!=current)return;
        activity.clear();
        String id=STATE.captureId();
        if(!"submitted".equals(STATE.status(id)))STATE.invalidate();
    }
    public static void invalidate(AgentActivity current) { if(activity.get()==current)STATE.invalidate(); }
    public static boolean submitted(AgentActivity current) {
        return activity.get()==current && STATE.submit(STATE.captureId());
    }
    static Bundle inject(String arg) {
        try {
            JSONObject input=AutomationWire.decode(arg,65536);
            if(input.length()!=3)throw new IllegalArgumentException();
            String task=AutomationWire.string(input,"taskId"), id=AutomationWire.string(input,"captureId");
            String callback=AutomationWire.string(input,"callbackUrl");
            AutomationContext.requireId(task);AutomationContext.requireId(id);
            return AutomationWire.onMain(() -> {
                AgentActivity current=activity.get();
                STATE.invalidate();
                if(current==null||!current.injectAutomation(callback))return AutomationWire.status(id,"error","NO_ACTIVE_GAME_REQUEST");
                STATE.ready(task,id);
                return status(id);
            });
        } catch(Exception ignored) { return AutomationWire.status(null,"error","INVALID_REQUEST"); }
    }
    static Bundle status(String id) {
        AutomationContext.requireId(id);
        return AutomationWire.status(id,STATE.status(id),null);
    }
    static Bundle confirm(String id) {
        AutomationContext.requireId(id);
        return AutomationWire.onMain(() -> {
            AgentActivity current=activity.get();
            if(!STATE.canConfirm(id)||current==null||!current.confirmAutomation())return AutomationWire.status(id,"error","NO_ACTIVE_GAME_REQUEST");
            return status(id);
        });
    }
    static Bundle clear(String id) {
        AutomationContext.requireId(id);
        return AutomationWire.onMain(() -> {
            if(STATE.clear(id)) { AgentActivity current=activity.get();if(current!=null)current.clearAutomation(); }
            return status(id);
        });
    }
}
