package com.huy.authlogin;

/** Runs on a plain JVM; fixtures never contain an account credential. */
public final class RequestPolicyTest {
    public static void main(String[] args) {
        expect(true, "com.tencent.KiHan", "action_login", "1104307008");
        expect(false, null, "action_login", "1104307008");
        expect(false, "com.example.other", "action_login", "1104307008");
        expect(false, "com.tencent.kihan", "action_login", "1104307008");
        expect(false, "com.tencent.KiHan", null, "1104307008");
        expect(false, "com.tencent.KiHan", "action_share", "1104307008");
        expect(false, "com.tencent.KiHan", "action_login", null);
        expect(false, "com.tencent.KiHan", "action_login", "");
        expect(false, "com.tencent.KiHan", "action_login", "1104307009");
        expect(false, "com.tencent.KiHan", "action_login", " 1104307008");
        System.out.println("RequestPolicyTest: 10 checks passed");
    }

    private static void expect(boolean expected, String caller, String action, String appId) {
        if (RequestPolicy.accepts(caller, action, appId) != expected) {
            throw new AssertionError("Request policy did not enforce caller, action, and application ID");
        }
    }
}
