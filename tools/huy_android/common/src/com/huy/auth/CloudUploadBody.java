package com.huy.auth;

import java.io.IOException;
import java.io.OutputStream;

/** The final permission check happens after connect, immediately before body transmission. */
final class CloudUploadBody {
    interface OutputFactory { OutputStream open() throws IOException; }

    private CloudUploadBody() {}

    static void write(OutputFactory factory, byte[] body, CloudClient.UploadGuard guard) throws IOException, CloudException {
        if (guard != null) guard.checkAllowed();
        try (OutputStream output = factory.open()) {
            if (guard != null) guard.beforeSend();
            output.write(body);
        }
    }
}
