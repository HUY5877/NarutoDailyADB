package com.huy.auth;

/** Lookup input never becomes a newly captured authorization. Synthetic values only. */
public final class AuthorizationInputTest {
    private static final String TOKEN = "AbCd0123456789aB";
    private static final String OPENID = "0123456789ABCDEF";
    private static final long NOW = 1792800000000L;
    private static int assertions;

    public static void main(String[] args) {
        AuthorizationInput raw = AuthorizationInput.parse(" \t" + TOKEN + "\r\n", NOW);
        equal(TOKEN, raw.accessToken());
        raw.validateResolved(TOKEN, "abcdef0123456789", NOW + 1000, NOW);
        assertions++;
        for (String separator : new String[] {"#", "?"}) {
            AuthorizationInput input = AuthorizationInput.parse(" \n" + callback(separator) + "\r\n", NOW);
            equal(TOKEN, input.accessToken());
            input.validateResolved(TOKEN, OPENID, NOW + 1000, NOW);
            assertions++;
            reject(() -> input.validateResolved(TOKEN, "abcdef0123456789", NOW + 1000, NOW));
            reject(() -> input.validateResolved("abcd0123456789ab", OPENID, NOW + 1000, NOW));
            reject(() -> input.validateResolved(TOKEN, OPENID, NOW, NOW));
            reject(() -> input.validateResolved(TOKEN, OPENID, NOW + 999, NOW));
            // Even a callback claiming 3600 seconds cannot replace cloud expiry.
            reject(() -> input.validateResolved(TOKEN, OPENID, NOW + 1000, NOW + 1000));
        }
        String good = callback("#");
        for (String value : new String[] {null, "", "abc", "access_token=" + TOKEN,
                "{\"access_token\":\"" + TOKEN + "\"}", good.replace("auth://", "https://"),
                good.replace("tauth.qq.com", "tauth.qq.com.evil.example"), good + "&ret=0",
                good + "&%72et=0", good + "&access_token=" + TOKEN, good + "&openid=" + OPENID,
                good.replace("ret=0", "ret=1"), good + "&appid=999", good + "&app_id=999",
                good.replace("/#", "/?ret=0#"), good + "&nickname=%GG", good + "&nickname=%C0%AF",
                good + "&nickname=%0Asecret", good.replace(OPENID, "bad"), good.replace(TOKEN, "bad"),
                good.replace("3600", "0"), good.replace("3600", "9223372036854775807"),
                good.replace("3600", "9223372036854775"),
                good + "&auth_time=garbage", good + "&auth_time=1792800000001"}) {
            reject(() -> AuthorizationInput.parse(value, NOW));
        }
        // Old auth_time is allowed as a lookup hint, but cannot revive an expired cloud record.
        AuthorizationInput old = AuthorizationInput.parse(good + "&auth_time=1700000000", NOW);
        old.validateResolved(TOKEN, OPENID, NOW + 5000, NOW);
        assertions++;
        reject(() -> old.validateResolved(TOKEN, OPENID, NOW - 1, NOW));
        System.out.println("AuthorizationInputTest: " + assertions + " assertions passed");
    }

    private static String callback(String separator) {
        return "auth://tauth.qq.com/" + separator + "ret=0&access_token=" + TOKEN
                + "&openid=" + OPENID + "&expires_in=3600&appid=1104307008";
    }

    private static void equal(Object expected, Object actual) {
        assertions++;
        if (!expected.equals(actual)) throw new AssertionError("Lookup altered credential bytes");
    }

    private static void reject(Runnable operation) {
        assertions++;
        try { operation.run(); } catch (IllegalArgumentException expected) {
            String message = expected.getMessage();
            if (message == null || message.contains(TOKEN) || message.contains(OPENID)
                    || message.contains("auth://") || expected.getCause() != null) {
                throw new AssertionError("Input leaked in error");
            }
            return;
        }
        throw new AssertionError("Expected input or resolved authorization rejection");
    }
}
