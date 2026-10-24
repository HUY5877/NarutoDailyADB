package com.huy.auth;

import java.io.InputStream;
import java.io.IOException;

final class CloudResponse {
    private CloudResponse() {}

    static String read(InputStream input, boolean accountList) throws IOException {
        int maximum = accountList ? 256 * 1024 : 64 * 1024;
        return readLimit(input, maximum);
    }

    static String readAuthorizationEnvelope(InputStream input) throws IOException {
        // Account metadata adds bytes around the still independently limited authorization.
        return readLimit(input, 80 * 1024);
    }

    private static String readLimit(InputStream input, int maximum) throws IOException {
        String json = BoundedUtf8.read(input, maximum);
        StrictJson.validateObject(json, maximum);
        return json;
    }
}
