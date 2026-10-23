package com.huy.authlogin;

import java.io.IOException;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;

final class AuthorizationDocument {
    static final int MAX_BYTES = 64 * 1024;

    static String read(InputStream stream) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        while (true) {
            int count = stream.read(buffer, 0, Math.min(buffer.length, MAX_BYTES + 1 - bytes.size()));
            if (count < 0) break;
            if (count == 0) {
                int next = stream.read();
                if (next < 0) break;
                bytes.write(next);
            } else {
                bytes.write(buffer, 0, count);
            }
            if (bytes.size() > MAX_BYTES) throw new TooLargeException();
        }
        return StandardCharsets.UTF_8.newDecoder()
                .onMalformedInput(CodingErrorAction.REPORT)
                .onUnmappableCharacter(CodingErrorAction.REPORT)
                .decode(ByteBuffer.wrap(bytes.toByteArray())).toString();
    }

    static final class TooLargeException extends IOException {
        private static final long serialVersionUID = 1L;
    }
}
