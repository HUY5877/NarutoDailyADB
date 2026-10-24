package com.huy.auth;

import java.nio.charset.StandardCharsets;
import java.util.HashSet;
import java.util.Set;

/** Rejects JSON ambiguities before Android's deliberately lenient org.json parser runs. */
final class StrictJson {
    private final String text;
    private int position;

    private StrictJson(String text) { this.text = text; }

    static void validateObject(String text) {
        validateObject(text, AuthorizationRules.MAX_BYTES);
    }

    static void validateObject(String text, int maximumBytes) {
        if (maximumBytes < 1 || maximumBytes > 1024 * 1024 || text == null || text.length() > maximumBytes
                || text.getBytes(StandardCharsets.UTF_8).length > maximumBytes) fail();
        StrictJson parser = new StrictJson(text);
        parser.space();
        parser.object(0);
        parser.space();
        if (parser.position != text.length()) fail();
    }

    private void object(int depth) {
        if (depth > 32) fail();
        expect('{');
        space();
        if (take('}')) return;
        Set<String> keys = new HashSet<>();
        while (true) {
            String key = string();
            if (!keys.add(key)) fail();
            space();
            expect(':');
            value(depth + 1);
            space();
            if (take('}')) return;
            expect(',');
            space();
        }
    }

    private void array(int depth) {
        if (depth > 32) fail();
        expect('[');
        space();
        if (take(']')) return;
        while (true) {
            value(depth + 1);
            space();
            if (take(']')) return;
            expect(',');
        }
    }

    private void value(int depth) {
        space();
        char c = peek();
        if (c == '{') object(depth);
        else if (c == '[') array(depth);
        else if (c == '"') string();
        else if (c == 't') literal("true");
        else if (c == 'f') literal("false");
        else if (c == 'n') literal("null");
        else number();
    }

    private String string() {
        expect('"');
        StringBuilder result = new StringBuilder();
        while (position < text.length()) {
            char c = text.charAt(position++);
            if (c == '"') {
                for (int i = 0; i < result.length(); i++) {
                    char item = result.charAt(i);
                    if (Character.isHighSurrogate(item)) {
                        if (++i >= result.length() || !Character.isLowSurrogate(result.charAt(i))) fail();
                    } else if (Character.isLowSurrogate(item)) fail();
                }
                return result.toString();
            }
            if (c < 0x20) fail();
            if (c == '\\') {
                if (position >= text.length()) fail();
                char escaped = text.charAt(position++);
                if (escaped == '"' || escaped == '\\' || escaped == '/') c = escaped;
                else if (escaped == 'b') c = '\b';
                else if (escaped == 'f') c = '\f';
                else if (escaped == 'n') c = '\n';
                else if (escaped == 'r') c = '\r';
                else if (escaped == 't') c = '\t';
                else if (escaped == 'u') {
                    int code = 0;
                    for (int j = 0; j < 4; j++) {
                        if (position >= text.length()) fail();
                        char hex = text.charAt(position++);
                        int digit = hex >= '0' && hex <= '9' ? hex - '0'
                                : hex >= 'a' && hex <= 'f' ? hex - 'a' + 10
                                : hex >= 'A' && hex <= 'F' ? hex - 'A' + 10 : -1;
                        if (digit < 0) fail();
                        code = (code << 4) | digit;
                    }
                    c = (char) code;
                } else fail();
            }
            result.append(c);
        }
        fail();
        return null;
    }

    private void number() {
        take('-');
        if (!take('0')) digits();
        if (take('.')) digits();
        if (take('e') || take('E')) {
            if (!take('+')) take('-');
            digits();
        }
    }

    private void digits() {
        int start = position;
        while (position < text.length() && text.charAt(position) >= '0' && text.charAt(position) <= '9') position++;
        if (position == start) fail();
    }

    private void literal(String value) {
        if (!text.startsWith(value, position)) fail();
        position += value.length();
    }

    private void space() {
        while (position < text.length()) {
            char c = text.charAt(position);
            if (c != ' ' && c != '\n' && c != '\r' && c != '\t') return;
            position++;
        }
    }

    private char peek() {
        if (position >= text.length()) fail();
        return text.charAt(position);
    }

    private boolean take(char c) {
        if (position < text.length() && text.charAt(position) == c) { position++; return true; }
        return false;
    }

    private void expect(char c) { if (!take(c)) fail(); }
    private static void fail() { throw AuthorizationRules.invalid(); }
}
