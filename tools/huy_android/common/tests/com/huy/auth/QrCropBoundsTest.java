package com.huy.auth;

/** Reject offscreen and non-square page content instead of exporting the webpage. */
public final class QrCropBoundsTest {
    public static void main(String[] args) {
        int[] crop=QrCropBounds.compute(100,100,200,200,400,800,800,1600);
        assert crop[0]==184 && crop[1]==184 && crop[2]==432 && crop[3]==432;
        reject(() -> QrCropBounds.compute(-10,100,200,200,400,800,800,1600));
        reject(() -> QrCropBounds.compute(300,100,200,200,400,800,800,1600));
        reject(() -> QrCropBounds.compute(10,10,300,100,400,800,800,1600));
        reject(() -> QrCropBounds.compute(10,10,10,10,400,800,800,1600));
        reject(() -> QrCropBounds.compute(Double.NaN,10,200,200,400,800,800,1600));
        System.out.println("QrCropBoundsTest: bounded QR-only crop passed");
    }
    private static void reject(Runnable call) { try { call.run(); throw new AssertionError("Expected rejection"); } catch (IllegalArgumentException expected) {} }
}
