package com.huy.auth;

import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.concurrent.Callable;
import java.util.concurrent.FutureTask;
import java.util.concurrent.TimeUnit;
import org.json.JSONObject;

/** Bounded, opaque ADB transport. Never include arguments in errors. */
public final class AutomationWire {
    private AutomationWire() {}
    public static JSONObject decode(String value,int maxBytes) {
        try {
            if(value==null||value.length()>((maxBytes+2)/3)*4||!value.matches("[A-Za-z0-9_-]+")) throw new IllegalArgumentException();
            byte[] bytes=Base64.getUrlDecoder().decode(value);
            if(bytes.length>maxBytes)throw new IllegalArgumentException();
            String json=StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes)).toString();
            StrictJson.validateObject(json);
            return new JSONObject(json);
        } catch(Exception ignored) { throw new IllegalArgumentException("自动化参数无效"); }
    }
    public static String string(JSONObject json,String name) throws Exception {
        Object value=json.get(name); if(!(value instanceof String))throw new IllegalArgumentException();return (String)value;
    }
    public static AutomationContext capture(String arg) {
        try {
            JSONObject j=decode(arg,8192);
            if(j.length()!=8)throw new IllegalArgumentException();
            Object expiry=j.get("expiresAtEpochMs");
            if(!(expiry instanceof Long)&&!(expiry instanceof Integer))throw new IllegalArgumentException();
            return new AutomationContext(string(j,"userId"),string(j,"taskId"),string(j,"attemptId"),
                    string(j,"captureId"),string(j,"captureToken"),string(j,"apiBase"),((Number)expiry).longValue(),
                    string(j,"loginType"),System.currentTimeMillis());
        } catch(Exception ignored) { throw new IllegalArgumentException("自动化参数无效"); }
    }
    public static Bundle result(JSONObject json) {
        Bundle result=new Bundle();
        result.putString("result",Base64.getUrlEncoder().withoutPadding().encodeToString(json.toString().getBytes(StandardCharsets.UTF_8)));
        return result;
    }
    public static Bundle status(String id,String state,String code) {
        try { JSONObject j=new JSONObject().put("captureId",id==null?"":id).put("state",state);
            if(code!=null)j.put("errorCode",code); return result(j);
        } catch(Exception ignored) { throw new IllegalStateException("自动化状态不可用"); }
    }
    public static Bundle capabilities(String kind) {
        try { return result(new JSONObject().put("protocol",1).put("kind",kind)); }
        catch(Exception ignored) { throw new IllegalStateException("自动化状态不可用"); }
    }
    public static <T> T onMain(Callable<T> action) {
        FutureTask<T> task=new FutureTask<>(action);
        if(Looper.myLooper()==Looper.getMainLooper()) task.run();
        else new Handler(Looper.getMainLooper()).post(task);
        try { return task.get(5,TimeUnit.SECONDS); }
        catch(Exception ignored) { task.cancel(false); throw new IllegalStateException("自动化界面暂不可用"); }
    }
}
