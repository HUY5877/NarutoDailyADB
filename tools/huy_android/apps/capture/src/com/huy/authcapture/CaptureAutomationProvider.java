package com.huy.authcapture;

import android.net.Uri;
import android.os.Bundle;
import android.os.ParcelFileDescriptor;
import com.huy.auth.AutomationContext;
import com.huy.auth.AutomationWire;
import com.huy.auth.LocalAutomationProvider;
import java.io.FileNotFoundException;
import java.io.OutputStream;
import java.util.List;

/** The QR is the only exported file; no callback or authorization is readable here. */
public final class CaptureAutomationProvider extends LocalAutomationProvider {
    @Override public Bundle call(String method,String arg,Bundle extras) {
        checkCaller();
        try {
            if("capabilities".equals(method))return AutomationWire.capabilities("capture");
            if("begin".equals(method)||"refresh".equals(method))return CaptureAutomation.begin(getContext(),arg);
            if("status".equals(method))return CaptureAutomation.status(arg);
            if("cancel".equals(method))return CaptureAutomation.cancel(arg);
        } catch(RuntimeException ignored) { return AutomationWire.status(null,"error","INVALID_REQUEST"); }
        return AutomationWire.status(null,"error","UNSUPPORTED_METHOD");
    }
    @Override public ParcelFileDescriptor openFile(Uri uri,String mode) throws FileNotFoundException {
        checkCaller();
        try {
            List<String> path=uri.getPathSegments();
            if(!"r".equals(mode)||!"com.huy.authcapture.automation".equals(uri.getAuthority())
                    ||path.size()!=2||!"qr".equals(path.get(0))||uri.getQuery()!=null||uri.getFragment()!=null)throw new IllegalArgumentException();
            AutomationContext.requireId(path.get(1));
            final byte[] png=CaptureAutomation.STATE.qr(path.get(1),System.currentTimeMillis());
            if(png==null)throw new IllegalArgumentException();
            ParcelFileDescriptor[] pipe=ParcelFileDescriptor.createPipe();
            new Thread(() -> {
                try(OutputStream out=new ParcelFileDescriptor.AutoCloseOutputStream(pipe[1])) { out.write(png); }
                catch(Exception ignored) { /* Consumer may cancel; never log QR bytes. */ }
            },"huy-qr-export").start();
            return pipe[0];
        } catch(Exception ignored) { throw new FileNotFoundException("当前二维码不可用"); }
    }
    @Override public String getType(Uri uri) { checkCaller();return "image/png"; }
}
