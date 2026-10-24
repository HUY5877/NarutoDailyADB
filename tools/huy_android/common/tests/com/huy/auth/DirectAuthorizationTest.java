package com.huy.auth;

/** Pure local contract checks using synthetic credentials only. */
public final class DirectAuthorizationTest {
    private static final long NOW = 1700000000000L;
    private static final String TOKEN = "0123456789abcdef0123456789abcdef";
    private static final String OPENID = "ABCDEF0123456789ABCDEF0123456789";
    private static int assertions;

    public static void main(String[] args) {
        preservesOriginalFieldsAndEscapesJson();
        deductsWaitingWithoutInventingCaptureTime();
        retainsOriginalAuthorizationDeadline();
        rejectsIncompleteOrAmbiguousUrls();
        System.out.println("DirectAuthorizationTest: " + assertions + " assertions passed");
    }

    private static void preservesOriginalFieldsAndEscapesJson() {
        DirectAuthorization value = parse("\ufeff \n" + callback("3600")
                + "&pay_token=feed&pf=openmobile_android&pfkey=a%2Bb"
                + "&nickname=%E6%B5%8B%E8%AF%95&note=%22%5C%2F\u00a0", NOW);
        String response = response(value, NOW);
        StrictJson.validateObject(response);
        contains(response, "\"access_token\":\"" + TOKEN + "\"");
        contains(response, "\"openid\":\"" + OPENID + "\"");
        contains(response, "\"pay_token\":\"feed\"");
        contains(response, "\"pf\":\"openmobile_android\"");
        contains(response, "\"pfkey\":\"a+b\"");
        contains(response, "\"nickname\":\"测试\"");
        contains(response, "\"note\":\"\\\"\\\\/\"");
        contains(response, "\"ret\":0");
        equal("6789", value.accountHint());
        contains(response(parse(callback("3600").replace("/#", "/?"), NOW), NOW), "\"expires_in\":\"3600\"");
    }

    private static void deductsWaitingWithoutInventingCaptureTime() {
        DirectAuthorization value = parse(callback("3600"), NOW);
        equal(false, value.hasKnownLifetime());
        String first = response(value, NOW);
        contains(first, "\"expires_in\":\"3600\"");
        absent(first, "captured_at");
        absent(first, "auth_time");
        absent(first, "pay_token");
        contains(response(value, NOW + 1), "\"expires_in\":\"3599\"");
        contains(response(value, NOW + 2000), "\"expires_in\":\"3598\"");
        contains(response(value, NOW + 3599000), "\"expires_in\":\"1\"");
        reject(() -> response(value, NOW + 3599001));
        reject(() -> response(value, NOW + 3600000));
        reject(() -> response(value, NOW - 1));
    }

    private static void retainsOriginalAuthorizationDeadline() {
        DirectAuthorization seconds = parse(callback("3600") + "&auth_time=1699999999", NOW);
        equal(true, seconds.hasKnownLifetime());
        contains(response(seconds, NOW), "\"expires_in\":\"3599\"");
        contains(response(seconds, NOW + 1000), "\"expires_in\":\"3598\"");
        contains(response(seconds, NOW), "\"auth_time\":\"1699999999\"");
        DirectAuthorization millis = parse(callback("3600") + "&auth_time=1699999998500", NOW);
        contains(response(millis, NOW), "\"expires_in\":\"3598\"");
        reject(() -> parse(callback("2") + "&auth_time=1699999998", NOW));
        reject(() -> response(seconds, NOW + 3599000));
    }

    private static void rejectsIncompleteOrAmbiguousUrls() {
        String good = callback("3600");
        for (String bad : new String[] {
                null, "", TOKEN, "access_token=" + TOKEN, "{\"access_token\":\"" + TOKEN + "\"}",
                good.replace("auth://", "https://"), good.replace("tauth.qq.com", "tauth.qq.com.evil.example"),
                good.replace("tauth.qq.com", "user@tauth.qq.com"), good.replace("tauth.qq.com", "tauth.qq.com:80"),
                good.replace("tauth.qq.com/", "tauth.qq.com/other"), good.replace("/#", "/?ret=0#"),
                good + "&ret=0", good + "&%72et=0", good + "&access_token=" + TOKEN,
                good + "&x=%GG", good + "&x=%C0%AF", good + "&x=%ED%A0%80", good + "&x=%0Asecret",
                good + "&appid=999", good + "&app_id=999", good + "&appid=1104307008&app_id=999",
                good.replace("ret=0", "ret=1"), good.replace("ret=0&", ""),
                good.replace("access_token=" + TOKEN, "access_token="),
                good.replace("&openid=" + OPENID, ""), good.replace(OPENID, "not-a-valid-openid"),
                good.replace("expires_in=3600&", ""), good.replace("expires_in=3600", "expires_in=0"),
                good.replace("expires_in=3600", "expires_in=-1"), good.replace("expires_in=3600", "expires_in=1.5"),
                good.replace("expires_in=3600", "expires_in=9223372036854775807"),
                good + "&auth_time=1700000000001", good + "&auth_time=1700000001", good + "&auth_time=garbage",
                good + "&auth_time=17000000000", good + "\n&pf=secret", good + "&note=raw space",
                good + "&padding=" + repeat('a', 65536)}) {
            reject(() -> parse(bad, NOW));
        }
        reject(() -> parse(good, -1));
        reject(() -> parse(good, Long.MAX_VALUE - 1000));
        contains(response(parse(good + "&appid=1104307008&app_id=1104307008", NOW), NOW), "\"appid\":\"1104307008\"");
    }

    private static DirectAuthorization parse(String text, long now) {
        return DirectAuthorization.fromUrl(text, now);
    }

    private static String response(GameAuthorization value, long now) {
        return value.responseJson(now);
    }

    private static String callback(String expires) {
        return "auth://tauth.qq.com/#ret=0&access_token=" + TOKEN
                + "&expires_in=" + expires + "&openid=" + OPENID;
    }

    private static String repeat(char value, int count) {
        StringBuilder text = new StringBuilder(count);
        for (int i = 0; i < count; i++) text.append(value);
        return text.toString();
    }

    private static void reject(Runnable action) {
        try { action.run(); }
        catch (IllegalArgumentException expected) {
            String message = expected.getMessage();
            if (message == null || message.contains(TOKEN) || message.contains(OPENID)
                    || message.contains("secret") || expected.getCause() != null) {
                throw new AssertionError("Credential data leaked through a validation error");
            }
            assertions++;
            return;
        }
        throw new AssertionError("Expected the invalid or expired callback to be rejected");
    }

    private static void equal(Object expected, Object actual) {
        if (!expected.equals(actual)) throw new AssertionError("Unexpected public result");
        assertions++;
    }

    private static void contains(String value, String expected) {
        if (!value.contains(expected)) throw new AssertionError("A supplied field was lost or changed");
        assertions++;
    }

    private static void absent(String value, String forbidden) {
        if (value.contains(forbidden)) throw new AssertionError("An absent authorization field was invented");
        assertions++;
    }
}
