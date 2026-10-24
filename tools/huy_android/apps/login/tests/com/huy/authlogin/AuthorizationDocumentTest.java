package com.huy.authlogin;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;

public final class AuthorizationDocumentTest {
    public static void main(String[] args) throws Exception {
        String text = "合成测试 JSON";
        equal(text, AuthorizationDocument.read(new ByteArrayInputStream(text.getBytes(StandardCharsets.UTF_8))));
        equal("", AuthorizationDocument.read(new ByteArrayInputStream(new byte[0])));
        byte[] limit = new byte[65536];
        Arrays.fill(limit, (byte) 'a');
        equal(65536, AuthorizationDocument.read(new ByteArrayInputStream(limit)).length());
        try {
            AuthorizationDocument.read(new ByteArrayInputStream(new byte[65537]));
            throw new AssertionError("Oversized document was accepted");
        } catch (AuthorizationDocument.TooLargeException expected) { }
        try {
            AuthorizationDocument.read(new ByteArrayInputStream(new byte[] {(byte) 0xC3, 0x28}));
            throw new AssertionError("Invalid UTF-8 was silently replaced");
        } catch (IOException expected) { }
        InputStream stalled = new ByteArrayInputStream(new byte[] {'o', 'k'}) {
            private boolean first = true;
            @Override public synchronized int read(byte[] buffer, int offset, int length) {
                if (first) { first = false; return 0; }
                return super.read(buffer, offset, length);
            }
        };
        equal("ok", AuthorizationDocument.read(stalled));
        System.out.println("AuthorizationDocumentTest: 6 checks passed");
    }

    private static void equal(Object expected, Object actual) {
        if (!expected.equals(actual)) throw new AssertionError("Document read changed the content or exceeded the byte limit");
    }
}
