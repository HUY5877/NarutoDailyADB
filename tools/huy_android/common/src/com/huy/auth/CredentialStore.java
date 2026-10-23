package com.huy.auth;

import android.content.Context;
import android.util.AtomicFile;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileNotFoundException;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

/** App-private, backup-excluded credentials. No external storage or credential logging. */
public final class CredentialStore {
    private static final String FILE_NAME = "huy_authorization.json";

    private CredentialStore() { }

    public static synchronized Authorization load(Context context) {
        AtomicFile file = atomicFile(context);
        try (FileInputStream stream = file.openRead()) {
            return Authorization.fromJson(BoundedUtf8.read(stream), System.currentTimeMillis());
        } catch (FileNotFoundException ignored) {
            File base = file.getBaseFile();
            if (!base.exists() && !new File(base.getPath() + ".bak").exists()) return null;
            throw storageError();
        } catch (IOException | SecurityException ignored) {
            throw storageError();
        }
    }

    public static synchronized void save(Context context, Authorization authorization) {
        if (authorization == null) throw AuthorizationRules.invalid();
        authorization.responseJson(System.currentTimeMillis());
        byte[] data = authorization.toJson().getBytes(StandardCharsets.UTF_8);
        if (data.length > AuthorizationRules.MAX_BYTES) throw AuthorizationRules.invalid();
        AtomicFile file = atomicFile(context);
        FileOutputStream stream = null;
        try {
            stream = file.startWrite();
            stream.write(data);
            file.finishWrite(stream);
        } catch (IOException | SecurityException ignored) {
            if (stream != null) file.failWrite(stream);
            throw storageError();
        }
    }

    public static synchronized void clear(Context context) {
        AtomicFile file = atomicFile(context);
        try {
            file.delete();
            File base = file.getBaseFile();
            if (base.exists() || new File(base.getPath() + ".bak").exists() || new File(base.getPath() + ".new").exists()) {
                throw storageError();
            }
        } catch (SecurityException ignored) { throw storageError(); }
    }

    private static AtomicFile atomicFile(Context context) {
        try {
            File directory = context.getNoBackupFilesDir();
            if (directory == null || !directory.isDirectory()) throw storageError();
            return new AtomicFile(new File(directory, FILE_NAME));
        } catch (SecurityException ignored) { throw storageError(); }
    }

    private static IllegalStateException storageError() {
        return new IllegalStateException("Cannot access private authorization storage");
    }
}
