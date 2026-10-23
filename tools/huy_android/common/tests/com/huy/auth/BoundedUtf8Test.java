package com.huy.auth;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

public final class BoundedUtf8Test {
    public static void main(String[] args) throws Exception {
        byte[] boundary = new byte[64 * 1024];
        java.util.Arrays.fill(boundary, (byte) 'a');
        if (BoundedUtf8.read(new ByteArrayInputStream(boundary)).length() != boundary.length) {
            throw new AssertionError("Maximum-size file was truncated");
        }
        String text = "合成授权";
        if (!text.equals(BoundedUtf8.read(new ByteArrayInputStream(text.getBytes(StandardCharsets.UTF_8))))) {
            throw new AssertionError("UTF-8 was not preserved");
        }
        reject(new byte[64 * 1024 + 1]);
        reject(new byte[] {(byte) 0xc0, (byte) 0xaf});
        reject(new byte[] {(byte) 0xe6, (byte) 0xb5});
        System.out.println("BoundedUtf8Test: 5 assertions passed");
    }

    private static void reject(byte[] input) throws Exception {
        try { BoundedUtf8.read(new ByteArrayInputStream(input)); }
        catch (IOException expected) { return; }
        throw new AssertionError("Expected oversized or malformed UTF-8 input rejection");
    }
}
