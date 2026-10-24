package com.huy.auth;

/** Synthetic credentials only. Run this main on a normal JVM. */
public final class AuthorizationRulesTest {
    private static final long NOW = 1700000000000L;
    private static final String TOKEN = "0123456789abcdef0123456789abcdef";
    private static final String OPENID = "ABCDEF0123456789ABCDEF0123456789";
    private static int assertions;

    public static void main(String[] args) {
        acceptsReorderedFieldsAndPreservesExtensions();
        rejectsUntrustedOriginsAndAmbiguousParameters();
        rejectsUnusableCredentialsWithoutEchoingInput();
        validatesExpirationWithoutOverflowOrTimeReset();
        honorsServerAuthorizationTimeWithoutAmbiguity();
        acceptsUnambiguousIsoTimes();
        System.out.println("AuthorizationRulesTest: " + assertions + " assertions passed");
    }

    private static void acceptsReorderedFieldsAndPreservesExtensions() {
        AuthorizationRules value = AuthorizationRules.fromCallback(callback("3600")
                + "&pf=openmobile_android&pfkey=a%2Bb&pay_token=feed&nickname=%E6%B5%8B%E8%AF%95", NOW, NOW);
        equal(NOW + 3600000L, value.expiresAtMs());
        equal("openmobile_android", value.fields().get("pf"));
        equal("a+b", value.fields().get("pfkey"));
        equal("feed", value.fields().get("pay_token"));
        equal("测试", value.fields().get("nickname"));
        equal(3599L, value.remainingSeconds(NOW + 1000));
        equal(TOKEN, value.fields().get("access_token"));
        AuthorizationRules query = AuthorizationRules.fromCallback(callback("3600").replace("/#", "/?"), NOW, NOW);
        equal(3600L, query.remainingSeconds(NOW));
        reject(() -> value.fields().put("access_token", "bad"), UnsupportedOperationException.class);
    }

    private static void rejectsUntrustedOriginsAndAmbiguousParameters() {
        String good = callback("3600");
        String[] invalid = {
            good.replace("auth://", "https://"), good.replace("tauth.qq.com", "tauth.qq.com.evil.example"),
            good.replace("tauth.qq.com", "user@tauth.qq.com"), good.replace("tauth.qq.com", "tauth.qq.com:80"),
            good.replace("tauth.qq.com/", "tauth.qq.com/other"), good.replace("tauth.qq.com/", "tauth.qq.com./"),
            good.replace("/#", "/?ret=0#"), good + "&ret=0", good + "&%72et=0", good + "&broken",
            good + "&nickname=%GG", good + "&nickname=%C0%AF", good + "&nickname=%0Aprivate",
            good + "&appid=999", good + "&app_id=999", good + "&=x"
        };
        for (String input : invalid) rejectSafe(() -> AuthorizationRules.fromCallback(input, NOW, NOW));
        AuthorizationRules.fromCallback(good + "&appid=1104307008&app_id=1104307008", NOW, NOW);
        assertions++;
    }

    private static void rejectsUnusableCredentialsWithoutEchoingInput() {
        String good = callback("3600");
        String[] invalid = {
            good.replace("ret=0", "ret=1"), good.replace("ret=0&", ""), good.replace(TOKEN, ""),
            good.replace(TOKEN, "private-token-not-hex"), good.replace(TOKEN, "abcd"),
            good.replace(OPENID, ""), good.replace(OPENID, "g123456789abcdef0123456789abcdef"),
            good.replace("&openid=" + OPENID, ""), good.replace("expires_in=3600&", ""),
            good.replace("expires_in=3600", "expires_in=0"), good.replace("expires_in=3600", "expires_in=-1"),
            good.replace("expires_in=3600", "expires_in=1.5"), good.replace("expires_in=3600", "expires_in=NaN"),
            good.replace("expires_in=3600", "expires_in=%2B1"),
            good.replace("expires_in=3600", "expires_in=9223372036854775808"),
            good.replace("expires_in=3600", "expires_in=9223372036854775807")
        };
        for (String input : invalid) rejectSafe(() -> AuthorizationRules.fromCallback(input, NOW, NOW));
        rejectSafe(() -> AuthorizationRules.fromCallback(null, NOW, NOW));
    }

    private static void validatesExpirationWithoutOverflowOrTimeReset() {
        AuthorizationRules old = AuthorizationRules.fromCallback(callback("2"), NOW, NOW + 1000);
        equal(NOW + 2000L, old.expiresAtMs());
        equal(1L, old.remainingSeconds(NOW + 1000));
        rejectSafe(() -> old.remainingSeconds(NOW + 1001));
        rejectSafe(() -> old.remainingSeconds(NOW + 2000));
        rejectSafe(() -> old.remainingSeconds(NOW - 1));
        rejectSafe(() -> AuthorizationRules.fromCallback(callback("2"), NOW, NOW + 2000));
        rejectSafe(() -> AuthorizationRules.fromCallback(callback("2"), NOW + 1, NOW));
        rejectSafe(() -> AuthorizationRules.fromCallback(callback("2"), -1, NOW));
        rejectSafe(() -> AuthorizationRules.fromCallback(callback("2"), Long.MAX_VALUE - 500, Long.MAX_VALUE - 500));
        rejectSafe(() -> AuthorizationRules.fromCallback(callback("2"), NOW, -1));
    }

    private static void acceptsUnambiguousIsoTimes() {
        equal(NOW, AuthorizationRules.parseCapturedAt("2023-11-14T22:13:20Z"));
        equal(NOW, AuthorizationRules.parseCapturedAt("2023-11-15T06:13:20+08:00"));
        equal(NOW + 123, AuthorizationRules.parseCapturedAt("2023-11-14T22:13:20.123456Z"));
        rejectSafe(() -> AuthorizationRules.parseCapturedAt("2023-11-14T22:13:20"));
        rejectSafe(() -> AuthorizationRules.parseCapturedAt("not-a-time"));
    }

    private static void honorsServerAuthorizationTimeWithoutAmbiguity() {
        AuthorizationRules milliseconds = AuthorizationRules.fromCallback(callback("3600") + "&auth_time=1699999999000", NOW, NOW);
        equal(1700003599000L, milliseconds.expiresAtMs());
        equal(3599L, milliseconds.remainingSeconds(NOW));
        equal("1699999999000", milliseconds.fields().get("auth_time"));
        AuthorizationRules seconds = AuthorizationRules.fromCallback(callback("3600") + "&auth_time=1699999999", NOW, NOW);
        equal(1700003599000L, seconds.expiresAtMs());
        String[] invalidTimes = {"garbage", "", "0", "17000000000", "1700000000001", "1700000001", "1699999999.5"};
        for (String invalid : invalidTimes) {
            rejectSafe(() -> AuthorizationRules.fromCallback(callback("3600") + "&auth_time=" + invalid, NOW, NOW));
        }
        rejectSafe(() -> AuthorizationRules.fromCallback(callback("2") + "&auth_time=1699999998", NOW, NOW));
    }

    private static String callback(String expires) {
        return "auth://tauth.qq.com/#expires_in=" + expires + "&openid=" + OPENID
                + "&ret=0&access_token=" + TOKEN;
    }

    private static void rejectSafe(Runnable action) {
        try { action.run(); } catch (IllegalArgumentException expected) {
            String message = expected.getMessage();
            if (message == null || message.contains(TOKEN) || message.contains(OPENID)
                    || message.contains("private") || message.contains("auth://") || expected.getCause() != null) {
                throw new AssertionError("Validation exception exposed input or retained an unsafe cause");
            }
            assertions++;
            return;
        }
        throw new AssertionError("Expected safe validation failure");
    }

    private static void reject(Runnable action, Class<? extends Throwable> type) {
        try { action.run(); } catch (Throwable expected) {
            if (!type.isInstance(expected)) throw new AssertionError("Wrong exception type");
            assertions++;
            return;
        }
        throw new AssertionError("Expected rejection");
    }

    private static void equal(Object expected, Object actual) {
        if (!expected.equals(actual)) throw new AssertionError("Unexpected result");
        assertions++;
    }
}
