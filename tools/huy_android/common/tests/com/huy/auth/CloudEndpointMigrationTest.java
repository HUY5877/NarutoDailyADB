package com.huy.auth;

public final class CloudEndpointMigrationTest {
    public static void main(String[] args) {
        String old = "https://naruto-login-client-fx63s9ajo-sust-acmer.vercel.app";
        check(true, CloudRules.legacyEndpoint(old));
        for (String endpoint : new String[] {null, "", "https://test.example.com", old + "/", old + ".evil.example",
                "https://naruto-login-client-git-test-sust-acmer.vercel.app"}) {
            check(false, CloudRules.legacyEndpoint(endpoint));
        }
        System.out.println("CloudEndpointMigrationTest: 7 checks passed");
    }
    private static void check(boolean expected, boolean actual) {
        if (expected != actual) throw new AssertionError("Incorrect endpoint migration decision");
    }
}
