package com.huy.authlogin;

import android.os.Bundle;
import com.huy.auth.AutomationWire;
import com.huy.auth.LocalAutomationProvider;

public final class LoginAutomationProvider extends LocalAutomationProvider {
    @Override public Bundle call(String method,String arg,Bundle extras) {
        checkCaller();
        try {
            if("capabilities".equals(method))return AutomationWire.capabilities("login");
            if("inject".equals(method))return LoginAutomation.inject(arg);
            if("status".equals(method))return LoginAutomation.status(arg);
            if("confirm".equals(method))return LoginAutomation.confirm(arg);
            if("clear".equals(method)||"cancel".equals(method))return LoginAutomation.clear(arg);
        } catch(RuntimeException ignored) { return AutomationWire.status(null,"error","INVALID_REQUEST"); }
        return AutomationWire.status(null,"error","UNSUPPORTED_METHOD");
    }
}
