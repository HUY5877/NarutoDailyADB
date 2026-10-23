package com.huy.auth;

import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/** Pure Java validation. Exceptions deliberately contain no callback values or causes. */
final class AuthorizationRules {
    static final String APP_ID = "1104307008";
    static final int MAX_BYTES = 64 * 1024;
    private final Map<String, String> fields;
    private final long capturedAt;
    private final long expiresAt;

    private AuthorizationRules(Map<String, String> fields, long capturedAt, long expiresAt) {
        this.fields = Collections.unmodifiableMap(fields);
        this.capturedAt = capturedAt;
        this.expiresAt = expiresAt;
    }

    static AuthorizationRules fromCallback(String callback, long capturedAt, long now) {
        if (callback == null || callback.length() > MAX_BYTES || capturedAt < 0 || now < capturedAt) {
            throw invalid();
        }
        for (int i = 0; i < callback.length(); i++) {
            char c = callback.charAt(i);
            if (c < 0x21 || c > 0x7e) throw invalid();
        }
        final URI uri;
        try { uri = new URI(callback); }
        catch (Exception ignored) { throw invalid(); }
        if (!"auth".equals(uri.getScheme()) || !"tauth.qq.com".equals(uri.getRawAuthority())
                || !"/".equals(uri.getRawPath())) throw invalid();
        String query = uri.getRawQuery();
        String fragment = uri.getRawFragment();
        if (query != null && !query.isEmpty() && fragment != null && !fragment.isEmpty()) throw invalid();
        String payload = fragment != null && !fragment.isEmpty() ? fragment : query;
        if (payload == null || payload.isEmpty()) throw invalid();

        Map<String, String> fields = new LinkedHashMap<>();
        String[] parts = payload.split("&", -1);
        if (parts.length > 128) throw invalid();
        for (String part : parts) {
            int equals = part.indexOf('=');
            if (equals <= 0) throw invalid();
            String key = decode(part.substring(0, equals));
            String value = decode(part.substring(equals + 1));
            if (!key.matches("[A-Za-z_][A-Za-z0-9_]{0,63}") || fields.containsKey(key)) throw invalid();
            for (int i = 0; i < value.length(); i++) {
                if (Character.isISOControl(value.charAt(i))) throw invalid();
            }
            fields.put(key, value);
        }
        if (!"0".equals(fields.get("ret"))) throw invalid();
        validateHex(fields.get("access_token"));
        validateHex(fields.get("openid"));
        if (fields.containsKey("appid") && !APP_ID.equals(fields.get("appid"))) throw invalid();
        if (fields.containsKey("app_id") && !APP_ID.equals(fields.get("app_id"))) throw invalid();
        long seconds = positiveDecimal(fields.get("expires_in"));
        long lifetimeStart = capturedAt;
        if (fields.containsKey("auth_time")) {
            String authTime = fields.get("auth_time");
            if (!authTime.matches("[0-9]{10}|[0-9]{13}")) throw invalid();
            long timestamp = positiveDecimal(authTime);
            if (authTime.length() == 10) timestamp *= 1000L;
            // Both formats represent modern epoch dates; other widths are ambiguous.
            if (timestamp < 1000000000000L || timestamp > capturedAt) throw invalid();
            lifetimeStart = timestamp;
        }
        final long expiresAt;
        try { expiresAt = Math.addExact(lifetimeStart, Math.multiplyExact(seconds, 1000L)); }
        catch (ArithmeticException ignored) { throw invalid(); }
        AuthorizationRules result = new AuthorizationRules(fields, capturedAt, expiresAt);
        result.remainingSeconds(now);
        return result;
    }

    static long parseCapturedAt(String value) {
        if (value == null || value.length() > 64) throw invalid();
        try {
            long result = OffsetDateTime.parse(value).toInstant().toEpochMilli();
            if (result < 0) throw invalid();
            return result;
        } catch (DateTimeParseException | ArithmeticException ignored) {
            throw invalid();
        }
    }

    static long positiveDecimal(String value) {
        if (value == null || value.length() > 19 || !value.matches("[0-9]+")) throw invalid();
        try {
            long result = Long.parseLong(value);
            if (result <= 0) throw invalid();
            return result;
        } catch (NumberFormatException ignored) { throw invalid(); }
    }

    long expiresAtMs() { return expiresAt; }

    long remainingSeconds(long now) {
        if (now < capturedAt) throw invalid();
        if (now >= expiresAt || (expiresAt - now) / 1000L < 1) {
            throw new IllegalArgumentException("Authorization expired");
        }
        return (expiresAt - now) / 1000L;
    }

    Map<String, String> fields() { return fields; }

    static IllegalArgumentException invalid() {
        return new IllegalArgumentException("Invalid authorization data");
    }

    private static void validateHex(String value) {
        if (value == null || !value.matches("[0-9A-Fa-f]{16,256}")) throw invalid();
    }

    private static String decode(String value) {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream(value.length());
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (c == '%') {
                if (i + 2 >= value.length()) throw invalid();
                int hi = Character.digit(value.charAt(++i), 16);
                int lo = Character.digit(value.charAt(++i), 16);
                if (hi < 0 || lo < 0) throw invalid();
                bytes.write((hi << 4) | lo);
            } else {
                bytes.write(c == '+' ? ' ' : c);
            }
        }
        try {
            return StandardCharsets.UTF_8.newDecoder().onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT).decode(ByteBuffer.wrap(bytes.toByteArray())).toString();
        } catch (CharacterCodingException ignored) { throw invalid(); }
    }
}
