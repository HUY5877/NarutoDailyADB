package com.huy.auth;

import com.huy.auth.Authorization;
import com.huy.auth.AutomationContext;
import com.huy.auth.BoundedUtf8;
import com.huy.auth.CloudClient;
import com.huy.auth.CloudException;
import com.huy.auth.CloudUploadBody;
import com.huy.auth.StrictJson;
import java.io.InputStream;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import javax.net.ssl.HttpsURLConnection;
import org.json.JSONObject;

/** One capture ticket authorizes one upload; website credentials are never used. */
public final class AutomationCaptureClient {
    public static void upload(AutomationContext context,Authorization authorization,CloudClient.UploadGuard guard) throws CloudException {
        HttpsURLConnection connection=null;
        try {
            guard.checkAllowed();
            authorization.responseJson(System.currentTimeMillis());
            JSONObject body=new JSONObject().put("userId",context.userId).put("taskId",context.taskId)
                    .put("attemptId",context.attemptId).put("captureId",context.captureId).put("captureToken",context.captureToken)
                    .put("authorization",new JSONObject(authorization.toJson()));
            byte[] bytes=body.toString().getBytes(StandardCharsets.UTF_8);
            if(bytes.length>65536)throw new IllegalArgumentException();
            connection=(HttpsURLConnection)new URL(context.apiBase+"/api/qq-captures").openConnection();
            connection.setInstanceFollowRedirects(false);connection.setUseCaches(false);
            connection.setConnectTimeout(12000);connection.setReadTimeout(15000);connection.setRequestMethod("POST");
            connection.setRequestProperty("Content-Type","application/json; charset=utf-8");
            connection.setRequestProperty("Accept","application/json");connection.setDoOutput(true);connection.setFixedLengthStreamingMode(bytes.length);
            final HttpsURLConnection target=connection;
            CloudUploadBody.write(() -> { target.connect();return target.getOutputStream(); },bytes,guard);
            int status=connection.getResponseCode();
            if(status<200||status>=300)throw new IllegalArgumentException();
            String type=connection.getContentType();
            if(type==null||!type.toLowerCase(java.util.Locale.ROOT).startsWith("application/json"))throw new IllegalArgumentException();
            String text;
            try(InputStream stream=connection.getInputStream()) { text=BoundedUtf8.read(stream,16384); }
            StrictJson.validateObject(text);
            JSONObject response=new JSONObject(text);
            if(!context.captureId.equals(response.optString("captureId")))throw new IllegalArgumentException();
            guard.checkAllowed();
        } catch(CloudException error) { throw error; }
        catch(Exception ignored) { throw new CloudException("扫码结果同步失败，请重新获取二维码"); }
        finally { if(connection!=null)connection.disconnect(); }
    }
}
