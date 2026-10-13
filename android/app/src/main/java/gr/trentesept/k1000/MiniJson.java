// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * A small, strict JSON reader and writer for the page bridge (android/BRIDGE.md). Pure Java, so
 * the bridge's parsing is unit-tested on the JVM (Android's org.json is a stub there).
 *
 * <p>Reading gives {@code Map<String, Object>} (insertion order), {@code List<Object>},
 * {@code String}, {@code Double}, {@code Boolean} or {@code null}. Input is bounded by the caller
 * (Bridge.MAX_MESSAGE_CHARS) and by a nesting limit here; anything malformed is refused whole.
 */
final class MiniJson {

    static final class ParseException extends Exception {
        ParseException(String message) {
            super(message);
        }
    }

    private final String s;
    private final int maxDepth;
    private int pos;

    private MiniJson(String s, int maxDepth) {
        this.s = s;
        this.maxDepth = maxDepth;
    }

    /** Parses exactly one JSON value (surrounding white space allowed, nothing else). */
    static Object parse(String text, int maxDepth) throws ParseException {
        if (text == null) throw new ParseException("no data");
        MiniJson p = new MiniJson(text, maxDepth);
        p.ws();
        Object v = p.value(0);
        p.ws();
        if (p.pos != text.length()) throw new ParseException("trailing data at " + p.pos);
        return v;
    }

    private Object value(int depth) throws ParseException {
        if (pos >= s.length()) throw new ParseException("unexpected end");
        char c = s.charAt(pos);
        switch (c) {
            case '{':
                return object(depth + 1);
            case '[':
                return array(depth + 1);
            case '"':
                return string();
            case 't':
                word("true");
                return Boolean.TRUE;
            case 'f':
                word("false");
                return Boolean.FALSE;
            case 'n':
                word("null");
                return null;
            default:
                if (c == '-' || (c >= '0' && c <= '9')) return number();
                throw new ParseException("unexpected '" + c + "' at " + pos);
        }
    }

    private Map<String, Object> object(int depth) throws ParseException {
        if (depth > maxDepth) throw new ParseException("nested too deep");
        pos++;   // {
        Map<String, Object> m = new LinkedHashMap<>();
        ws();
        if (peek() == '}') {
            pos++;
            return m;
        }
        while (true) {
            ws();
            if (peek() != '"') throw new ParseException("expected a key at " + pos);
            String k = string();
            ws();
            expect(':');
            ws();
            if (m.containsKey(k)) throw new ParseException("duplicate key " + k);
            m.put(k, value(depth));
            ws();
            char c = next();
            if (c == '}') return m;
            if (c != ',') throw new ParseException("expected , or } at " + (pos - 1));
        }
    }

    private List<Object> array(int depth) throws ParseException {
        if (depth > maxDepth) throw new ParseException("nested too deep");
        pos++;   // [
        List<Object> a = new ArrayList<>();
        ws();
        if (peek() == ']') {
            pos++;
            return a;
        }
        while (true) {
            ws();
            a.add(value(depth));
            ws();
            char c = next();
            if (c == ']') return a;
            if (c != ',') throw new ParseException("expected , or ] at " + (pos - 1));
        }
    }

    private String string() throws ParseException {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= s.length()) throw new ParseException("unterminated string");
            char c = s.charAt(pos++);
            if (c == '"') return sb.toString();
            if (c < 0x20) throw new ParseException("control character in a string");
            if (c != '\\') {
                sb.append(c);
                continue;
            }
            if (pos >= s.length()) throw new ParseException("unterminated escape");
            char e = s.charAt(pos++);
            switch (e) {
                case '"': sb.append('"'); break;
                case '\\': sb.append('\\'); break;
                case '/': sb.append('/'); break;
                case 'b': sb.append('\b'); break;
                case 'f': sb.append('\f'); break;
                case 'n': sb.append('\n'); break;
                case 'r': sb.append('\r'); break;
                case 't': sb.append('\t'); break;
                case 'u': {
                    if (pos + 4 > s.length()) throw new ParseException("short \\u escape");
                    int v = 0;
                    for (int i = 0; i < 4; i++) {
                        int d = Character.digit(s.charAt(pos++), 16);
                        if (d < 0) throw new ParseException("bad \\u escape");
                        v = (v << 4) | d;
                    }
                    sb.append((char) v);
                    break;
                }
                default:
                    throw new ParseException("bad escape \\" + e);
            }
        }
    }

    private Double number() throws ParseException {
        int start = pos;
        if (peek() == '-') pos++;
        if (peek() == '0') {
            pos++;
        } else if (isDigit(peek())) {
            while (isDigit(peek())) pos++;
        } else {
            throw new ParseException("bad number at " + start);
        }
        if (peek() == '.') {
            pos++;
            if (!isDigit(peek())) throw new ParseException("bad number at " + start);
            while (isDigit(peek())) pos++;
        }
        if (peek() == 'e' || peek() == 'E') {
            pos++;
            if (peek() == '+' || peek() == '-') pos++;
            if (!isDigit(peek())) throw new ParseException("bad number at " + start);
            while (isDigit(peek())) pos++;
        }
        double d = Double.parseDouble(s.substring(start, pos));
        if (Double.isInfinite(d) || Double.isNaN(d)) throw new ParseException("number out of range");
        return d;
    }

    private static boolean isDigit(char c) {
        return c >= '0' && c <= '9';
    }

    private void word(String w) throws ParseException {
        if (!s.startsWith(w, pos)) throw new ParseException("unexpected token at " + pos);
        pos += w.length();
    }

    private void ws() {
        while (pos < s.length()) {
            char c = s.charAt(pos);
            if (c != ' ' && c != '\t' && c != '\n' && c != '\r') return;
            pos++;
        }
    }

    private char peek() {
        return pos < s.length() ? s.charAt(pos) : '\0';
    }

    private char next() throws ParseException {
        if (pos >= s.length()) throw new ParseException("unexpected end");
        return s.charAt(pos++);
    }

    private void expect(char c) throws ParseException {
        if (next() != c) throw new ParseException("expected " + c + " at " + (pos - 1));
    }

    // ------------------------------------------------------------------------------- writing

    /**
     * Writes a value: Map (String keys), List or Object[], int[]/long[]/double[]/boolean[],
     * CharSequence, Number (integral values without a fraction; NaN and infinities as null),
     * Boolean, null. Anything else is written as its toString() string.
     */
    static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        write(sb, v);
        return sb.toString();
    }

    private static void write(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof Boolean) {
            sb.append(((Boolean) v) ? "true" : "false");
        } else if (v instanceof Integer || v instanceof Long || v instanceof Short || v instanceof Byte) {
            sb.append(((Number) v).longValue());
        } else if (v instanceof Number) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) sb.append("null");
            else if (d == Math.rint(d) && Math.abs(d) < 1e15) sb.append((long) d);
            else sb.append(d);
        } else if (v instanceof CharSequence) {
            quote(sb, v.toString());
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) sb.append(',');
                first = false;
                quote(sb, String.valueOf(e.getKey()));
                sb.append(':');
                write(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Iterable) {
            sb.append('[');
            Iterator<?> it = ((Iterable<?>) v).iterator();
            while (it.hasNext()) {
                write(sb, it.next());
                if (it.hasNext()) sb.append(',');
            }
            sb.append(']');
        } else if (v instanceof Object[]) {
            List<Object> l = new ArrayList<>();
            for (Object o : (Object[]) v) l.add(o);
            write(sb, l);
        } else if (v instanceof int[]) {
            List<Object> l = new ArrayList<>();
            for (int o : (int[]) v) l.add(o);
            write(sb, l);
        } else if (v instanceof long[]) {
            List<Object> l = new ArrayList<>();
            for (long o : (long[]) v) l.add(o);
            write(sb, l);
        } else if (v instanceof double[]) {
            List<Object> l = new ArrayList<>();
            for (double o : (double[]) v) l.add(o);
            write(sb, l);
        } else if (v instanceof boolean[]) {
            List<Object> l = new ArrayList<>();
            for (boolean o : (boolean[]) v) l.add(o);
            write(sb, l);
        } else {
            quote(sb, v.toString());
        }
    }

    private static void quote(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    // Control characters, and U+2028/U+2029 (line breaks to some JavaScript parsers).
                    if (c < 0x20 || c == 0x2028 || c == 0x2029) sb.append(String.format(Locale.ROOT, "\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        sb.append('"');
    }
}
