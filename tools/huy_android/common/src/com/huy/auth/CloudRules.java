package com.huy.auth;

import java.net.URI;
import java.time.Instant;

/** Pure validation shared by settings, transport and tests. */
final class CloudRules {
    private CloudRules() {}

    static boolean legacyEndpoint(String value) {
        return "https://naruto-login-client-fx63s9ajo-sust-acmer.vercel.app".equals(value);
    }

    static String endpoint(String value) {
        try {
            String text = value == null ? "" : value.trim();
            if (text.length() > 512) throw new IllegalArgumentException();
            URI uri = new URI(text);
            if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null
                    || uri.getRawUserInfo() != null || uri.getRawQuery() != null
                    || uri.getRawFragment() != null || (uri.getPort() != -1 && uri.getPort() != 443)
                    || !(uri.getRawPath().isEmpty() || "/".equals(uri.getRawPath()))
                    || !uri.getHost().matches("[A-Za-z0-9.-]+")
                    || uri.getHost().startsWith(".") || uri.getHost().endsWith(".")) {
                throw new IllegalArgumentException();
            }
            return "https://" + uri.getHost().toLowerCase(java.util.Locale.ROOT);
        } catch (Exception error) {
            throw new IllegalArgumentException("请填写 HTTPS 测试服务地址，例如 https://test.example.com");
        }
    }

    static boolean recordId(String value) {
        return value != null && value.matches("[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}");
    }

    static String token(String value) {
        if (value == null) throw new IllegalArgumentException("请粘贴 16 至 256 位十六进制 token");
        int start = 0, end = value.length();
        while (start < end && whitespace(value.charAt(start))) start++;
        while (end > start && whitespace(value.charAt(end - 1))) end--;
        String result = value.substring(start, end);
        if (!result.matches("[0-9a-fA-F]{16,256}")) {
            throw new IllegalArgumentException("请粘贴 16 至 256 位十六进制 token");
        }
        return result;
    }

    private static boolean whitespace(char value) {
        return Character.isWhitespace(value) || Character.isSpaceChar(value) || value == '\ufeff';
    }

    static void resolvedToken(String input, String returned, String id, long recordExpiry, long authExpiry,
            boolean expired, long now) {
        if (!token(input).equals(returned) || !recordId(id) || recordExpiry != authExpiry
                || expired || recordExpiry <= now || recordExpiry - now < 1000) {
            throw new IllegalArgumentException("云端授权与输入不匹配或已过期，请重新扫码");
        }
    }

    static String sessionCookie(String setCookie) {
        if (setCookie == null || setCookie.length() > 4096 || setCookie.indexOf('\r') >= 0 || setCookie.indexOf('\n') >= 0) return null;
        String pair = setCookie.split(";", 2)[0].trim();
        if (pair.length() > 2200 || !pair.matches("[A-Za-z0-9_-]{1,80}=[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+")) return null;
        return pair;
    }

    static String label(String value) {
        String result = value == null ? "" : value.trim();
        if (result.isEmpty() || result.length() > 40 || !safeText(result, 40)) throw new IllegalArgumentException("账号备注需要 1 至 40 个字");
        return result;
    }

    static boolean safeText(String value, int maximum) {
        if (value == null || value.length() > maximum) return false;
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (Character.isISOControl(c) || c == '\u061c' || c == '\u200e' || c == '\u200f'
                    || (c >= '\u202a' && c <= '\u202e') || (c >= '\u2066' && c <= '\u2069')) return false;
            if (Character.isHighSurrogate(c)) {
                if (++i >= value.length() || !Character.isLowSurrogate(value.charAt(i))) return false;
            } else if (Character.isLowSurrogate(c)) return false;
        }
        return true;
    }

    static long timestamp(String value) {
        try {
            if (value == null || value.length() > 40 || !value.endsWith("Z")) throw new IllegalArgumentException();
            long millis = Instant.parse(value).toEpochMilli();
            if (millis <= 0) throw new IllegalArgumentException();
            return millis;
        } catch (Exception error) {
            throw new IllegalArgumentException("云端返回的数据格式无效");
        }
    }
}
