package com.huy.auth;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** HTTPS-only API access. All calls are blocking and must run off the main thread. */
public final class CloudClient {
    private static final UploadGuard ALWAYS_ALLOWED = new UploadGuard() {
        @Override public void checkAllowed() {}
        @Override public void beforeSend() {}
    };
    private final CloudSettings.Snapshot session;
    public CloudClient(CloudSettings.Snapshot session) { this.session = session; }

    /** Validates a visible account label before starting the QR authorization flow. */
    public static String validateLabel(String value) { return CloudRules.label(value); }

    public static String validateToken(String value) { return CloudRules.token(value); }

    public static SignIn signIn(String endpoint, String username, String password) throws CloudException {
        try {
            String base = CloudRules.endpoint(endpoint);
            checkEnvironment(base);
            if (!CloudRules.safeText(username, 24) || username.trim().isEmpty() || password == null
                    || password.length() < 6 || password.length() > 128) throw new CloudException("请填写网站账号和密码");
            JSONObject body = new JSONObject().put("username", username.trim()).put("password", password);
            Response response = request(base, "/api/auth/login", "POST", body, null);
            JSONObject user = response.json.getJSONObject("user");
            String userId = CloudRecord.string(user, "id"), canonicalUsername = CloudRecord.string(user, "username");
            if (!CloudRules.recordId(userId) || !CloudRules.safeText(canonicalUsername, 24)
                    || canonicalUsername.isEmpty() || response.cookie == null) throw new CloudException("登录响应无效，请更新客户端后重试");
            return new SignIn(base, response.cookie, userId, canonicalUsername);
        } catch (CloudException error) { throw error; }
        catch (IllegalArgumentException error) { throw new CloudException("请填写正确的 HTTPS 测试服务地址"); }
        catch (Exception error) { throw new CloudException("登录响应无效，请稍后重试"); }
    }

    public List<CloudRecord> list() throws CloudException {
        prepare();
        try {
            JSONArray data = request(session.endpoint, "/api/qq-authorizations", "GET", null, session.cookie).json.getJSONArray("items");
            if (data.length() > 200) throw new JSONException("Too many records");
            List<CloudRecord> result = new ArrayList<>();
            java.util.HashSet<String> ids = new java.util.HashSet<>();
            for (int i = 0; i < data.length(); i++) {
                CloudRecord record = new CloudRecord(data.getJSONObject(i));
                if (!ids.add(record.id)) throw new JSONException("Duplicate record");
                result.add(record);
            }
            return result;
        } catch (CloudException error) { throw error; }
        catch (Exception error) { throw new CloudException("账号列表格式无效，请更新客户端后重试"); }
    }

    public CloudRecord upload(String label, Authorization authorization) throws CloudException {
        return upload(label, authorization, ALWAYS_ALLOWED);
    }

    public CloudRecord upload(String label, Authorization authorization, UploadGuard guard) throws CloudException {
        if (guard == null) throw uploadCancelled();
        prepare();
        guard.checkAllowed();
        try {
            String checkedLabel = CloudRules.label(label);
            authorization.responseJson(System.currentTimeMillis());
            JSONObject body = new JSONObject().put("label", checkedLabel).put("authorization", new JSONObject(authorization.toJson()));
            return new CloudRecord(request(session.endpoint, "/api/qq-authorizations", "POST", body, session.cookie, guard).json.getJSONObject("item"));
        } catch (CloudException error) { throw error; }
        catch (Exception error) { throw new CloudException("上传内容无效或授权已过期，请检查备注并重新扫码"); }
    }

    public Download download(String id) throws CloudException {
        prepare();
        if (!CloudRules.recordId(id)) throw new CloudException("请先在上号器手动选择账号");
        try {
            JSONObject data = request(session.endpoint, "/api/qq-authorizations/" + id, "GET", null, session.cookie).json;
            CloudRecord record = new CloudRecord(data.getJSONObject("item"));
            if (!id.equals(record.id) || record.isExpired()) throw new CloudException("授权已过期，请回到抓号器重新扫码");
            Authorization authorization = Authorization.fromJson(data.getJSONObject("authorization").toString(), System.currentTimeMillis());
            if (authorization.expiresAtMs() != record.expiresAtMs) throw new JSONException("Expiry mismatch");
            return new Download(record, authorization);
        } catch (CloudException error) { throw error; }
        catch (Exception error) { throw new CloudException("云端授权无效，请回到抓号器重新扫码"); }
    }

    /** Resolve only the signed-in website user's record; no credentials are stored here. */
    public Download resolveToken(String value) throws CloudException {
        final String token;
        try { token = CloudRules.token(value); }
        catch (IllegalArgumentException error) { throw new CloudException(error.getMessage()); }
        prepare();
        try {
            JSONObject body = new JSONObject().put("access_token", token);
            JSONObject data = request(session.endpoint, "/api/qq-authorizations/resolve", "POST", body, session.cookie).json;
            CloudRecord record = new CloudRecord(data.getJSONObject("item"));
            long now = System.currentTimeMillis();
            Authorization authorization = Authorization.fromJson(data.getJSONObject("authorization").toString(), now);
            CloudRules.resolvedToken(token, authorization.accessToken(), record.id, record.expiresAtMs,
                    authorization.expiresAtMs(), record.expired, now);
            return new Download(record, authorization);
        } catch (CloudException error) { throw error; }
        catch (Exception error) { throw new CloudException("云端授权与输入不匹配或已过期，请重新扫码"); }
    }

    /** Callback input is only a lookup and identity check; the cloud retains the original lifetime. */
    public Download resolveInput(AuthorizationInput input) throws CloudException {
        if (input == null) throw new CloudException("请先粘贴 token 或完整的 QQ 授权回调地址");
        Download result = resolveToken(input.accessToken());
        try {
            input.validateResolved(result.authorization.accessToken(), result.authorization.openid(),
                    result.authorization.expiresAtMs(), System.currentTimeMillis());
            return result;
        } catch (IllegalArgumentException error) { throw new CloudException(error.getMessage()); }
    }

    public void delete(String id) throws CloudException {
        prepare();
        if (!CloudRules.recordId(id)) throw new CloudException("账号记录无效");
        request(session.endpoint, "/api/qq-authorizations/" + id, "DELETE", new JSONObject(), session.cookie);
    }

    private void prepare() throws CloudException {
        if (session == null || !session.isSignedIn()) throw new CloudException("请先登录测试网站账号", true);
        try {
            if (!CloudRules.endpoint(session.endpoint).equals(session.endpoint)
                    || !session.cookie.equals(CloudRules.sessionCookie(session.cookie))) throw new IllegalArgumentException();
        } catch (Exception error) { throw new CloudException("云端设置无效，请重新登录", true); }
        checkEnvironment(session.endpoint);
    }

    private static void checkEnvironment(String endpoint) throws CloudException {
        Response health = request(endpoint, "/api/health", "GET", null, null);
        Object environment=health.json.opt("environment");
        if (!"test".equals(environment)&&!"prod".equals(environment)) throw new CloudException("网站环境不可用，请检查服务地址");
    }

    private static Response request(String endpoint, String path, String method, JSONObject body, String cookie) throws CloudException {
        return request(endpoint, path, method, body, cookie, null);
    }

    private static Response request(String endpoint, String path, String method, JSONObject body, String cookie, UploadGuard guard) throws CloudException {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(endpoint + path).openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setConnectTimeout(12000);
            connection.setReadTimeout(15000);
            connection.setRequestMethod(method);
            connection.setRequestProperty("Accept", "application/json");
            if (cookie != null) connection.setRequestProperty("Cookie", cookie);
            if (body != null) {
                byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
                if (bytes.length > 64 * 1024) throw new CloudException("上传内容超过 64 KiB，请重新扫码");
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                connection.setFixedLengthStreamingMode(bytes.length);
                final HttpURLConnection sendingConnection = connection;
                CloudUploadBody.write(() -> {
                    sendingConnection.connect();
                    return sendingConnection.getOutputStream();
                }, bytes, guard);
            }
            int status = connection.getResponseCode();
            if (status == 401) {
                if ("/api/auth/login".equals(path)) throw new CloudException("网站账号或密码不正确");
                throw new CloudException("网站登录已失效，请重新登录", true);
            }
            if (status == 403) throw new CloudException("当前网站账号无权访问，请检查账号或服务地址");
            if (status == 404) throw new CloudException("/api/qq-authorizations/resolve".equals(path)
                    ? "当前网站账号下未找到此 token，请检查网站账号或重新扫码上传"
                    : "未找到账号或接口，请刷新列表并检查服务地址");
            if (status == 409) throw new CloudException("/api/qq-authorizations/resolve".equals(path)
                    ? "此 token 对应多条记录，请从账号列表手动选择"
                    : "账号数据发生变化，请刷新后重试");
            if (status == 410) throw new CloudException("授权已过期，请回到抓号器重新扫码");
            if (status == 413) throw new CloudException("上传内容过大，请重新扫码");
            if (status == 429) throw new CloudException("请求过于频繁，请稍后重试");
            if (status >= 300 && status < 400) throw new CloudException("服务地址发生跳转，请填写最终的测试服务地址");
            if (status < 200 || status >= 300) throw new CloudException(status == 400 ? "请求内容无效，请检查后重试" : "测试服务暂时不可用，请稍后重试");
            if (status == 204) return new Response(new JSONObject(), null);
            String contentType = connection.getContentType();
            if (contentType == null || !contentType.toLowerCase(java.util.Locale.ROOT).startsWith("application/json")) throw new CloudException("服务响应格式不正确，请检查测试服务地址");
            String json;
            try (InputStream input = connection.getInputStream()) {
                boolean download = "GET".equals(method) && path.startsWith("/api/qq-authorizations/")
                        && CloudRules.recordId(path.substring("/api/qq-authorizations/".length()));
                boolean resolve = "POST".equals(method) && "/api/qq-authorizations/resolve".equals(path);
                json = download || resolve ? CloudResponse.readAuthorizationEnvelope(input)
                        : CloudResponse.read(input, "GET".equals(method) && "/api/qq-authorizations".equals(path));
            }
            String sessionCookie = null;
            for (Map.Entry<String, List<String>> header : connection.getHeaderFields().entrySet()) {
                if (header.getKey() == null || !"Set-Cookie".equalsIgnoreCase(header.getKey())) continue;
                for (String value : header.getValue()) {
                    String candidate = CloudRules.sessionCookie(value);
                    if (candidate != null) {
                        if (sessionCookie != null && !sessionCookie.equals(candidate)) throw new CloudException("登录会话响应不明确，请稍后重试");
                        sessionCookie = candidate;
                    }
                }
            }
            return new Response(new JSONObject(json), sessionCookie);
        } catch (CloudException error) { throw error; }
        catch (Exception error) { throw new CloudException("无法连接测试服务，请检查地址、网络和系统时间"); }
        finally { if (connection != null) connection.disconnect(); }
    }

    public static final class SignIn {
        public final String endpoint, cookie, userId, username;
        private SignIn(String endpoint, String cookie, String userId, String username) {
            this.endpoint = endpoint; this.cookie = cookie; this.userId = userId; this.username = username;
        }
    }
    /** The caller must serialize both methods with its cancellation/session state lock. */
    public interface UploadGuard {
        /** Pure check after health verification and again before establishing the POST connection. */
        void checkAllowed() throws CloudException;
        /** Atomically check permission and mark transmission started, immediately before the first body byte. */
        void beforeSend() throws CloudException;
    }

    public static CloudException uploadCancelled() {
        return new CloudException("上传已取消或网站账号已变化，本次未继续发送授权");
    }
    public static final class Download {
        public final CloudRecord record;
        public final Authorization authorization;
        private Download(CloudRecord record, Authorization authorization) { this.record = record; this.authorization = authorization; }
    }
    private static final class Response {
        final JSONObject json;
        final String cookie;
        Response(JSONObject json, String cookie) { this.json = json; this.cookie = cookie; }
    }
}
