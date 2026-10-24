package com.huy.auth;

/** Synthetic credentials only. Checks input and fail-closed resolve validation. */
public final class CloudTokenRulesTest {
    private static int assertions;
    private static final String TOKEN = "AbCd0123456789aB";
    private static final String ID = "01234567-89ab-cdef-0123-456789abcdef";
    private static final long NOW = 1792800000000L;

    public static void main(String[] args) {
        equal(TOKEN, token(" \t" + TOKEN + "\r\n"));
        equal(repeat('a', 256), token(repeat('a', 256)));
        for (String value : new String[] {null, "", " \r\n", "abc", repeat('a', 15), repeat('a', 257),
                "AbCd0123456789aG", "AbCd0123 456789ab", "AbCd0123\n456789ab", "access_token=" + TOKEN,
                "https://test.example.com/?access_token=" + TOKEN, "\u0000" + TOKEN}) {
            reject(() -> token(value));
        }
        resolved(TOKEN, TOKEN, ID, NOW + 5000, NOW + 5000, false, NOW);
        assertions++;
        reject(() -> resolved(TOKEN, "abcd0123456789ab", ID, NOW + 5000, NOW + 5000, false, NOW));
        reject(() -> resolved(TOKEN, TOKEN, "../record", NOW + 5000, NOW + 5000, false, NOW));
        reject(() -> resolved(TOKEN, TOKEN, ID, NOW + 5000, NOW + 6000, false, NOW));
        reject(() -> resolved(TOKEN, TOKEN, ID, NOW + 5000, NOW + 5000, true, NOW));
        reject(() -> resolved(TOKEN, TOKEN, ID, NOW, NOW, false, NOW));
        reject(() -> resolved(TOKEN, TOKEN, ID, NOW + 999, NOW + 999, false, NOW));
        resolved(TOKEN, TOKEN, ID, NOW + 1000, NOW + 1000, false, NOW);
        assertions++;
        System.out.println("CloudTokenRulesTest: " + assertions + " assertions passed");
    }

    private static String token(String value) {
        return CloudRules.token(value);
    }

    private static void resolved(String input, String returned, String id, long recordExpiry, long authExpiry,
            boolean expired, long now) {
        CloudRules.resolvedToken(input, returned, id, recordExpiry, authExpiry, expired, now);
    }

    private static String repeat(char value, int count) { return new String(new char[count]).replace('\0', value); }
    private static void equal(Object expected, Object actual) {
        assertions++;
        if (!expected.equals(actual)) throw new AssertionError("Token normalization changed credential bytes");
    }
    private static void reject(Runnable operation) {
        assertions++;
        try { operation.run(); } catch (IllegalArgumentException expected) {
            if (expected.getMessage() != null && expected.getMessage().contains(TOKEN)) {
                throw new AssertionError("Credential leaked in validation error");
            }
            return;
        }
        throw new AssertionError("Expected token validation rejection");
    }
}
