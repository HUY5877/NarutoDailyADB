package com.huy.auth;

/** A replaced/expired task must never publish another user's QR or result. */
public final class AutomationStateTest {
    private static final long NOW = 1000000;
    private static final String USER = "00000000-0000-4000-8000-000000000001";
    private static final String TASK = "00000000-0000-4000-8000-000000000002";
    private static final String FIRST = "00000000-0000-4000-8000-000000000003";
    private static final String SECOND = "00000000-0000-4000-8000-000000000004";
    public static void main(String[] args) {
        assert AutomationAccess.allowedUid(2000, 10001);
        assert AutomationAccess.allowedUid(0, 10001);
        assert AutomationAccess.allowedUid(10001, 10001);
        assert !AutomationAccess.allowedUid(10002, 10001);
        AutomationContext first = context(FIRST, NOW + 60000);
        AutomationContext second = context(SECOND, NOW + 60000);
        CaptureAutomationState state = new CaptureAutomationState();
        state.begin(first, NOW);
        assert state.publishQr(first, new byte[]{1,2}, NOW);
        assert state.snapshot(FIRST, NOW).qrRevision == 1;
        assert !state.publishQr(first, new byte[]{1,2}, NOW);
        assert state.snapshot(FIRST, NOW).qrRevision == 1;
        assert state.update(first,"uploading",null,NOW);
        assert state.qr(FIRST,NOW)!=null : "QR read must survive a scan between status and content read";
        assert state.update(first,"uploaded",null,NOW);
        assert state.qr(FIRST,NOW)!=null;
        assert state.qr(FIRST,NOW+60000)==null;
        state.begin(second, NOW);
        assert !state.publishQr(first, new byte[]{3}, NOW);
        assert !state.update(first, "uploaded", null, NOW);
        assert state.qr(FIRST, NOW) == null;
        assert state.snapshot(SECOND, NOW).qrRevision == 0;
        assert !state.cancel(FIRST);
        assert state.isCurrent(second, NOW);
        assert state.publishQr(second, new byte[]{4}, NOW);
        assert state.update(second, "refresh_requested", null, NOW);
        assert state.qr(SECOND, NOW) == null;
        assert !state.isCurrent(second, NOW);
        reject(() -> state.begin(context(FIRST, NOW+1000), NOW));
        CaptureAutomationState expiryState = new CaptureAutomationState();
        expiryState.begin(context(FIRST, NOW+1000), NOW);
        assert "error".equals(expiryState.snapshot(FIRST, NOW+1000).state);
        assert expiryState.qr(FIRST, NOW+1000) == null;
        CaptureAutomationState sizeState = new CaptureAutomationState();
        sizeState.begin(first,NOW);
        assert sizeState.publishQr(first,new byte[700000],NOW);
        assert !sizeState.publishQr(first,new byte[700001],NOW) : "QR must respect the worker's 700000-byte limit";
        assert sizeState.qr(FIRST,NOW).length==700000;
        reject(() -> context(FIRST, NOW));
        reject(() -> new AutomationContext(USER,TASK,"a",FIRST,repeat('b',64),"https://example.com",NOW+60000,"android_qq",NOW));
        reject(() -> new AutomationContext(USER,TASK,repeat('a',32),FIRST,repeat('b',64),"https://example.com/path",NOW+60000,"android_qq",NOW));
        reject(() -> new AutomationContext(USER,TASK,repeat('a',32),FIRST,repeat('b',64),"http://example.com",NOW+60000,"android_qq",NOW));
        reject(() -> new AutomationContext(USER,TASK,repeat('a',32),FIRST,repeat('b',64),"https://example.com",NOW+60000,"android_wx",NOW));
        System.out.println("AutomationStateTest: task isolation, expiry and caller checks passed");
    }
    private static AutomationContext context(String capture,long expiry) {
        return new AutomationContext(USER,TASK,repeat('a',32),capture,repeat('b',64),"https://example.com",expiry,"android_qq",NOW);
    }
    private static String repeat(char c,int n) { StringBuilder b=new StringBuilder(); while(n-->0)b.append(c);return b.toString(); }
    private static void reject(Runnable call) { try { call.run(); throw new AssertionError("Expected rejection"); } catch (IllegalArgumentException expected) {} }
}
