package com.huy.auth;

import java.io.IOException;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;

final class BoundedUtf8 {
    static String read(InputStream stream) throws IOException {
        return read(stream, AuthorizationRules.MAX_BYTES);
    }

    static String read(InputStream stream, int maximumBytes) throws IOException {
        if (maximumBytes < 1 || maximumBytes > 1024 * 1024) throw new IllegalArgumentException("Invalid size limit");
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        while (true) {
            int count = stream.read(buffer, 0, Math.min(buffer.length, maximumBytes + 1 - bytes.size()));
            if (count < 0) break;
            if (count == 0) {
                int next = stream.read();
                if (next < 0) break;
                bytes.write(next);
            } else {
                bytes.write(buffer, 0, count);
            }
            if (bytes.size() > maximumBytes) throw new IOException("Authorization file is too large");
        }
        return StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes.toByteArray())).toString();
    }
}
