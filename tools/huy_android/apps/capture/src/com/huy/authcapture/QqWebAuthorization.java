package com.huy.authcapture;

import java.net.URLEncoder;

/** Public QQ web-login parameters observed in the user-provided reference APK. */
final class QqWebAuthorization {
    static String startUrl(String state) {
        if (state == null || !state.matches("[a-f0-9-]{36}")) throw new IllegalArgumentException("Invalid flow state");
        try {
            return "https://xui.ptlogin2.qq.com/cgi-bin/xlogin?pt_enable_pwd=1&appid=716027609"
                    + "&pt_3rd_aid=1104307008&daid=381&pt_skey_valid=0&style=35&force_qr=1&autorefresh=1"
                    + "&s_url=http%3A%2F%2Fconnect.qq.com&refer_cgi=m_authorize&ucheck=1&fall_to_wv=1&status_os=0"
                    + "&redirect_uri=auth%3A%2F%2Ftauth.qq.com%2F&client_id=1104307008&pf=openmobile_web"
                    + "&response_type=token&scope=all&sdkp=a&sdkv=3.5.14&sign=0&status_machine=0&switch=1"
                    + "&time=0&loginfrom=add&h5sig=0&loginty=6&state=" + URLEncoder.encode(state,"UTF-8");
        } catch (java.io.UnsupportedEncodingException impossible) { throw new IllegalStateException(); }
    }
}
