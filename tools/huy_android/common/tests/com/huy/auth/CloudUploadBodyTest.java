package com.huy.auth;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;

public final class CloudUploadBodyTest {
    private static final byte[] SYNTHETIC = new byte[] {1, 2, 3};
    private static int assertions;

    public static void main(String[] args) throws Exception {
        Guard before = new Guard();
        before.cancel();
        int[] opened = {0};
        expectDenied(() -> CloudUploadBody.write(() -> { opened[0]++; return new ByteArrayOutputStream(); }, SYNTHETIC, before));
        equal(0, opened[0]);
        equal(false, before.started);

        Guard whileConnecting = new Guard();
        Probe cancelledBody = new Probe();
        expectDenied(() -> CloudUploadBody.write(() -> { whileConnecting.cancel(); return cancelledBody; }, SYNTHETIC, whileConnecting));
        equal(0, cancelledBody.size());
        equal(true, cancelledBody.closed);
        equal(false, whileConnecting.started);

        Guard switchedAccount = new Guard();
        Probe switchedBody = new Probe();
        expectDenied(() -> CloudUploadBody.write(() -> { switchedAccount.sameSession = false; return switchedBody; }, SYNTHETIC, switchedAccount));
        equal(0, switchedBody.size());
        equal(false, switchedAccount.started);

        Guard deadlineElapsed = new Guard();
        Probe expiredBody = new Probe();
        expectDenied(() -> CloudUploadBody.write(() -> { deadlineElapsed.withinDeadline = false; return expiredBody; }, SYNTHETIC, deadlineElapsed));
        equal(0, expiredBody.size());
        equal(false, deadlineElapsed.started);

        Guard afterClaim = new Guard();
        Probe permittedBody = new Probe() {
            @Override public void write(byte[] bytes, int offset, int length) {
                equal(true, afterClaim.started);
                afterClaim.cancel();
                equal(true, afterClaim.uncertain);
                super.write(bytes, offset, length);
            }
        };
        CloudUploadBody.write(() -> permittedBody, SYNTHETIC, afterClaim);
        equal(SYNTHETIC.length, permittedBody.size());
        equal(true, permittedBody.closed);

        Guard closeFailure = new Guard();
        expectDenied(() -> CloudUploadBody.write(() -> {
            closeFailure.cancel();
            return new OutputStream() {
                @Override public void write(int value) { throw new AssertionError("Cancelled body must not be written"); }
                @Override public void close() throws IOException { throw new IOException("Synthetic close failure"); }
            };
        }, SYNTHETIC, closeFailure));

        Probe legacy = new Probe();
        CloudUploadBody.write(() -> legacy, SYNTHETIC, null);
        equal(SYNTHETIC.length, legacy.size());
        equal(true, legacy.closed);
        System.out.println("CloudUploadBodyTest: " + assertions + " checks passed");
    }

    private static class Probe extends ByteArrayOutputStream {
        boolean closed;
        @Override public void close() { closed = true; }
    }

    private static final class Guard implements CloudClient.UploadGuard {
        private boolean active = true, sameSession = true, withinDeadline = true, started, uncertain;
        synchronized void cancel() { active = false; uncertain = started; }
        @Override public synchronized void checkAllowed() throws CloudException {
            if (!active || !sameSession || !withinDeadline) throw new CloudException("Synthetic denial");
        }
        @Override public synchronized void beforeSend() throws CloudException {
            checkAllowed();
            started = true;
        }
    }

    private interface Check { void run() throws Exception; }
    private static void expectDenied(Check check) throws Exception {
        assertions++;
        try { check.run(); } catch (CloudException expected) { return; }
        throw new AssertionError("Expected cancelled upload to stop");
    }
    private static void equal(Object expected, Object actual) {
        assertions++;
        if (!expected.equals(actual)) throw new AssertionError("Upload cancellation boundary mismatch");
    }
}
