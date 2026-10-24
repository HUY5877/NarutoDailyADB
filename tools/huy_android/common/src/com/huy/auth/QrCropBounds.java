package com.huy.auth;

/** Validate CSS bounds and convert only the square QR plus its quiet-zone margin. */
public final class QrCropBounds {
    private QrCropBounds() {}
    public static int[] compute(double x,double y,double width,double height,double viewportWidth,double viewportHeight,int viewWidth,int viewHeight) {
        for(double n:new double[]{x,y,width,height,viewportWidth,viewportHeight}) if(Double.isNaN(n)||Double.isInfinite(n)) throw invalid();
        if(x<0||y<0||width<80||height<80||width>1500||height>1500||viewportWidth<=0||viewportHeight<=0
                ||width/height<0.8||width/height>1.25||x+width>viewportWidth||y+height>viewportHeight
                ||viewWidth<=0||viewHeight<=0) throw invalid();
        double scale=viewWidth/viewportWidth;
        int left=Math.max(0,(int)Math.floor((x-8)*scale));
        int top=Math.max(0,(int)Math.floor((y-8)*scale));
        int right=Math.min(viewWidth,(int)Math.ceil((x+width+8)*scale));
        int bottom=Math.min(viewHeight,(int)Math.ceil((y+height+8)*scale));
        if(right-left<80||bottom-top<80||right-left>2048||bottom-top>2048||(y+height)*scale>viewHeight) throw invalid();
        return new int[]{left,top,right-left,bottom-top};
    }
    private static IllegalArgumentException invalid() { return new IllegalArgumentException("二维码区域无效"); }
}
