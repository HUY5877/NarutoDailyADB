package com.huy.auth;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;

public final class CloudResponseTest {
    private static int assertions;
    public static void main(String[] args) throws Exception {
        String label = new String(new char[40]).replace('\0', '测');
        String record = "{\"id\":\"01234567-89ab-cdef-0123-456789abcdef\",\"label\":\"" + label
                + "\",\"accountHint\":\"QQ · ****1234\",\"capturedAt\":\"2026-10-23T00:00:00.000Z\",\"expiresAt\":\"2026-11-23T00:00:00.000Z\",\"updatedAt\":\"2026-10-23T00:00:00.000Z\",\"expired\":false}";
        StringBuilder list = new StringBuilder("{\"items\":[");
        for (int i = 0; i < 200; i++) { if (i != 0) list.append(','); list.append(record); }
        list.append("]}");
        byte[] bytes = list.toString().getBytes(StandardCharsets.UTF_8);
        if (bytes.length <= 64 * 1024) throw new AssertionError("Fixture must exceed credential limit");
        same(list.toString(), CloudResponse.read(new ByteArrayInputStream(bytes), true));
        reject(() -> CloudResponse.read(new ByteArrayInputStream(bytes), false));
        reject(() -> CloudResponse.read(new ByteArrayInputStream(new byte[256 * 1024 + 1]), true));
        reject(() -> CloudResponse.read(new ByteArrayInputStream("{\"items\":[],\"items\":[]}".getBytes(StandardCharsets.UTF_8)), true));
        reject(() -> CloudResponse.read(new ByteArrayInputStream(new byte[] {(byte) 0xc0, (byte) 0xaf}), true));
        String exact = "{\"pad\":\"" + new String(new char[256 * 1024 - 10]).replace('\0', 'x') + "\"}";
        same(exact, CloudResponse.read(new ByteArrayInputStream(exact.getBytes(StandardCharsets.UTF_8)), true));
        // A maximal accepted POST becomes larger when GET wraps its authorization in metadata.
        String callbackPrefix = "auth://tauth.qq.com/#ret=0&access_token=0123456789abcdef0123456789abcdef&openid=abcdef0123456789abcdef0123456789&expires_in=3600&padding=";
        String authPrefix = "{\"schema_version\":1,\"app_id\":\"1104307008\",\"captured_at_epoch_ms\":1792713600000,\"callback_url\":\"";
        String uploadPrefix = "{\"label\":\"a\",\"authorization\":";
        int paddingSize = 64 * 1024 - (uploadPrefix + authPrefix + callbackPrefix + "\"}}").getBytes(StandardCharsets.UTF_8).length;
        String callback = callbackPrefix + new String(new char[paddingSize]).replace('\0', 'x');
        String authorization = authPrefix + callback + "\"}";
        String upload = uploadPrefix + authorization + "}";
        if (upload.getBytes(StandardCharsets.UTF_8).length != 64 * 1024) throw new AssertionError("Fixture must fill accepted POST size");
        StrictJson.validateObject(upload);
        StrictJson.validateObject(authorization);
        AuthorizationRules.fromCallback(callback, 1792713600000L, 1792713600000L);
        String download = "{\"item\":" + record + ",\"authorization\":" + authorization + "}";
        if (download.getBytes(StandardCharsets.UTF_8).length <= 64 * 1024) throw new AssertionError("Download envelope must exceed old limit");
        same(download, CloudResponse.readAuthorizationEnvelope(new ByteArrayInputStream(download.getBytes(StandardCharsets.UTF_8))));
        reject(() -> CloudResponse.read(new ByteArrayInputStream(download.getBytes(StandardCharsets.UTF_8)), false));
        reject(() -> CloudResponse.readAuthorizationEnvelope(new ByteArrayInputStream(new byte[80 * 1024 + 1])));
        String oversizedAuthorization = "{\"padding\":\"" + new String(new char[64 * 1024]).replace('\0', 'x') + "\"}";
        String allowedEnvelope = "{\"item\":" + record + ",\"authorization\":" + oversizedAuthorization + "}";
        same(allowedEnvelope, CloudResponse.readAuthorizationEnvelope(new ByteArrayInputStream(allowedEnvelope.getBytes(StandardCharsets.UTF_8))));
        reject(() -> StrictJson.validateObject(oversizedAuthorization));
        System.out.println("CloudResponseTest: " + assertions + " assertions passed");
    }
    private static void same(String expected, String actual) { assertions++; if (!expected.equals(actual)) throw new AssertionError("Response changed"); }
    private static void reject(Check check) throws Exception {
        assertions++;
        try { check.run(); } catch (java.io.IOException | IllegalArgumentException expected) { return; }
        throw new AssertionError("Expected response rejection");
    }
    private interface Check { void run() throws Exception; }
}
