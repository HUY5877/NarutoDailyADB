package com.huy.auth;

public final class StrictJsonTest {
    private static int assertions;

    public static void main(String[] args) {
        acceptsValidJsonWithoutChangingValues();
        rejectsDuplicateKeysAndNonstandardJson();
        System.out.println("StrictJsonTest: " + assertions + " assertions passed");
    }

    private static void acceptsValidJsonWithoutChangingValues() {
        String[] valid = {"{}", " {\"app_id\":1104307008,\"text\":\"\\u4e2d文\"} \n",
            "{\"extras\":[1,-2,1.5,1e2,true,false,null,{\"a\":1}],\"a\":2}"};
        for (String input : valid) { StrictJson.validateObject(input); assertions++; }
    }

    private static void rejectsDuplicateKeysAndNonstandardJson() {
        String[] invalid = {null, "", "[]", "{\"a\":1,\"a\":2}", "{\"a\":1,\"\\u0061\":2}",
            "{\"x\":{\"a\":1,\"a\":2}}", "{\"a\":1}{}", "{a:1}", "{'a':1}", "{\"a\":01}",
            "{\"a\":NaN}", "{\"a\":true,}", "{\"a\":1/*comment*/}", "{\"a\":\"line\nfeed\"}",
            "{\"a\":\"\\q\"}", "{\"a\":+1}", "{\"a\":1.}", "{\"a\":[1,]}", "{\"a\":\"\\uD800\"}",
            "{\"a\":\"\\uDC00\"}", "{\"a\":\"\uD800\"}", "{\"a\":\"\\u１２３４\"}"};
        for (String input : invalid) {
            try { StrictJson.validateObject(input); }
            catch (IllegalArgumentException expected) {
                if (expected.getCause() != null || expected.getMessage().contains("\"a\"")) {
                    throw new AssertionError("Unsafe JSON exception");
                }
                assertions++;
                continue;
            }
            throw new AssertionError("Expected malformed or ambiguous JSON rejection");
        }
    }
}
