package com.huy.auth;

public final class CloudRulesTest {
    private static int assertions;
    public static void main(String[] args) {
        equal("https://test.example.com", CloudRules.endpoint("  https://Test.Example.com/  "));
        equal("https://test.example.com", CloudRules.endpoint("https://test.example.com:443"));
        for (String url : new String[] {"http://test.example.com", "https://user:pass@test.example.com", "https://test.example.com/api", "https://test.example.com?x=1", "https://test.example.com/#x", "https://test.example.com:8443", "https://test.example.com@evil.example", "https://test.example.com\\@evil.example", "https://", "", "https://test.example.com./"}) reject(() -> CloudRules.endpoint(url));
        equal("naruto_session=abc.def.ghi", CloudRules.sessionCookie("naruto_session=abc.def.ghi; Path=/; Secure; HttpOnly"));
        for (String cookie : new String[] {"x=plain", "x=abc.def.ghi\r\nX-Test: leak", "x=abc.def.ghi, other=x", "bad name=abc.def.ghi", "x=abc.def.ghi;\n", "x=abc.def.ghi;\r"}) equal(null, CloudRules.sessionCookie(cookie));
        equal("我的账号", CloudRules.label(" 我的账号 "));
        reject(() -> CloudRules.label(" "));
        reject(() -> CloudRules.label("授权\n账号"));
        reject(() -> CloudRules.label("授权\u202e账号"));
        reject(() -> CloudRules.label("授权\u061c账号"));
        reject(() -> CloudRules.label("授权\u200e账号"));
        reject(() -> CloudRules.label("授权\u200f账号"));
        reject(() -> CloudRules.label("授权\ud83d账号"));
        reject(() -> CloudRules.label("授权\ude00账号"));
        equal("授权\ud83d\ude00", CloudRules.label("授权\ud83d\ude00"));
        reject(() -> CloudRules.label(new String(new char[41]).replace('\0', 'a')));
        equal(true, CloudRules.recordId("01234567-89ab-cdef-0123-456789abcdef"));
        equal(false, CloudRules.recordId("../../authorization"));
        equal(false, CloudRules.recordId("01234567-89ab-cdef-0123-456789abcdef?x=y"));
        equal(1792713600123L, CloudRules.timestamp("2026-10-23T00:00:00.123Z"));
        reject(() -> CloudRules.timestamp("2026-10-23T00:00:00"));
        reject(() -> CloudRules.timestamp("invalid"));
        reject(() -> CloudRules.timestamp("1960-01-01T00:00:00Z"));
        System.out.println("CloudRulesTest: " + assertions + " assertions passed");
    }
    private static void equal(Object expected, Object actual) {
        assertions++;
        if (expected == null ? actual != null : !expected.equals(actual)) throw new AssertionError("Unexpected validation result");
    }
    private static void reject(Runnable operation) {
        assertions++;
        try { operation.run(); } catch (IllegalArgumentException expected) { return; }
        throw new AssertionError("Expected invalid input rejection");
    }
}
