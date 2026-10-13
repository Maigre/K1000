// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * The page bridge's message contract (android/BRIDGE.md): what the box's page may ask of the app
 * over the WebMessagePort, validated here before MainActivity acts on it, and the app's own
 * messages to the page. Pure Java (unit-tested on the JVM).
 *
 * <p>Every message is one JSON object as a string, with its type in {@code t}. Unknown fields are
 * ignored (a newer page may send more); an unknown type, a wrong field type, a missing required
 * field or an oversized message is refused whole ({@link Invalid}) and the page hears
 * {@code {t:"err"}}.
 */
final class Bridge {

    /** The bridge protocol version the app speaks (sent as {@code v} in the handshake). */
    static final int VERSION = 1;
    /** What this app can do for the page (the handshake's {@code caps}). */
    static final List<String> CAPS = Collections.unmodifiableList(List.of(
            "haptic", "fullscreen", "exclusion", "keep_awake", "diag", "back", "set_ui", "insets"));

    static final int MAX_MESSAGE_CHARS = 8192;
    static final int MAX_DEPTH = 4;
    static final int MAX_RECTS = 16;
    static final int MAX_CAPS = 32;
    /** Any coordinate beyond this many CSS px is refused (a phone is ~400 × 900 CSS px). */
    static final double MAX_CSS_PX = 20000;

    /** The page's haptic vocabulary (DESIGN §5.5), mapped to Android effects by Haptics. */
    enum Haptic {
        TICK("tick"), CLICK("click"), CONFIRM("confirm"), REJECT("reject"), LONG_PRESS("long_press"),
        CONFIRM2("confirm2"), REJECT3("reject3");

        final String wire;

        Haptic(String wire) {
            this.wire = wire;
        }

        static Haptic of(String s) {
            for (Haptic h : values()) {
                if (h.wire.equals(s)) return h;
            }
            return null;
        }
    }

    /** A refused message: {@link #type} is its {@code t} when there was one, else "". */
    static final class Invalid extends Exception {
        final String type;

        Invalid(String type, String message) {
            super(message);
            this.type = type == null ? "" : type;
        }
    }

    /** A validated page-to-app message. Only the fields of its type are set. */
    static final class Msg {
        final String t;
        /** ready: the page's bridge version; back_handled: -; others: unused. */
        int version;
        /** ready: what the page can do for the app (e.g. "back"). */
        List<String> caps = Collections.emptyList();
        Haptic haptic;
        /** fullscreen, keep_awake. */
        boolean on;
        /** fullscreen: the page draws under the display cutout itself (it reads {t:"insets"}). */
        boolean cutout;
        /** exclusion: [x, y, w, h] in CSS px, in the WebView's viewport. Empty = none. */
        double[][] rects;
        /** back_handled: the id of the {t:"back"} it answers. */
        long id = -1;
        boolean handled;
        /** set_ui: "v1" or "v2". */
        String ui;
        /** diag: also open the app's own Diagnostics panel. */
        boolean show;

        Msg(String t) {
            this.t = t;
        }
    }

    private Bridge() {
    }

    // ----------------------------------------------------------------------------- page → app

    static Msg parse(String data) throws Invalid {
        if (data == null || data.isEmpty()) throw new Invalid(null, "empty message");
        if (data.length() > MAX_MESSAGE_CHARS) throw new Invalid(null, "message too long");
        Object root;
        try {
            root = MiniJson.parse(data, MAX_DEPTH);
        } catch (MiniJson.ParseException e) {
            throw new Invalid(null, "not JSON: " + e.getMessage());
        }
        if (!(root instanceof Map)) throw new Invalid(null, "not a JSON object");
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) root;
        Object tv = m.get("t");
        if (!(tv instanceof String)) throw new Invalid(null, "no message type t");
        String t = (String) tv;
        Msg msg = new Msg(t);
        switch (t) {
            case "ready":
                msg.version = m.containsKey("v") ? intField(m, t, "v", 1, 1000) : 1;
                msg.caps = stringList(m, t, "caps");
                break;
            case "haptic": {
                String k = stringField(m, t, "kind");
                msg.haptic = Haptic.of(k);
                if (msg.haptic == null) throw new Invalid(t, "unknown haptic kind " + k);
                break;
            }
            case "fullscreen":
                msg.on = boolField(m, t, "on");
                msg.cutout = m.containsKey("cutout") && boolField(m, t, "cutout");
                break;
            case "keep_awake":
                msg.on = boolField(m, t, "on");
                break;
            case "exclusion":
                msg.rects = rects(m, t);
                break;
            case "diag":
                msg.show = m.containsKey("show") && boolField(m, t, "show");
                break;
            case "back_handled":
                msg.id = intField(m, t, "id", 0, Integer.MAX_VALUE);
                msg.handled = boolField(m, t, "handled");
                break;
            case "set_ui": {
                String ui = stringField(m, t, "ui");
                if (!ui.equals("v1") && !ui.equals("v2")) throw new Invalid(t, "ui must be v1 or v2");
                msg.ui = ui;
                break;
            }
            default:
                throw new Invalid(t, "unknown message type");
        }
        return msg;
    }

    private static boolean boolField(Map<String, Object> m, String t, String k) throws Invalid {
        Object v = m.get(k);
        if (!(v instanceof Boolean)) throw new Invalid(t, k + " must be true or false");
        return (Boolean) v;
    }

    private static String stringField(Map<String, Object> m, String t, String k) throws Invalid {
        Object v = m.get(k);
        if (!(v instanceof String)) throw new Invalid(t, k + " must be a string");
        return (String) v;
    }

    private static int intField(Map<String, Object> m, String t, String k, int lo, int hi) throws Invalid {
        Object v = m.get(k);
        if (!(v instanceof Double)) throw new Invalid(t, k + " must be a number");
        double d = (Double) v;
        if (d != Math.rint(d) || d < lo || d > hi) throw new Invalid(t, k + " must be a whole number in " + lo + ".." + hi);
        return (int) d;
    }

    private static List<String> stringList(Map<String, Object> m, String t, String k) throws Invalid {
        if (!m.containsKey(k)) return Collections.emptyList();
        Object v = m.get(k);
        if (!(v instanceof List)) throw new Invalid(t, k + " must be a list");
        List<?> l = (List<?>) v;
        if (l.size() > MAX_CAPS) throw new Invalid(t, k + ": too many entries");
        List<String> out = new ArrayList<>();
        for (Object o : l) {
            if (!(o instanceof String) || ((String) o).length() > 64) throw new Invalid(t, k + " must hold short strings");
            out.add((String) o);
        }
        return Collections.unmodifiableList(out);
    }

    private static double[][] rects(Map<String, Object> m, String t) throws Invalid {
        Object v = m.get("rects");
        if (!(v instanceof List)) throw new Invalid(t, "rects must be a list of [x, y, w, h]");
        List<?> l = (List<?>) v;
        if (l.size() > MAX_RECTS) throw new Invalid(t, "at most " + MAX_RECTS + " rects");
        double[][] out = new double[l.size()][];
        for (int i = 0; i < l.size(); i++) {
            Object r = l.get(i);
            if (!(r instanceof List) || ((List<?>) r).size() != 4) throw new Invalid(t, "rect " + i + " must be [x, y, w, h]");
            double[] q = new double[4];
            for (int j = 0; j < 4; j++) {
                Object n = ((List<?>) r).get(j);
                if (!(n instanceof Double)) throw new Invalid(t, "rect " + i + " must hold numbers");
                q[j] = (Double) n;
                if (Math.abs(q[j]) > MAX_CSS_PX) throw new Invalid(t, "rect " + i + " is out of range");
            }
            if (q[2] <= 0 || q[3] <= 0) throw new Invalid(t, "rect " + i + " must have a positive width and height");
            out[i] = q;
        }
        return out;
    }

    // ------------------------------------------------------------------- geometry (CSS → px)

    /**
     * The page's rectangles ({@code [x, y, w, h]} in CSS px, relative to the WebView's viewport)
     * as view pixel rectangles {@code [left, top, right, bottom]}: scaled by {@code scale} (the
     * display density; the page's viewport is width=device-width at scale 1), rounded outward, and
     * clipped to the view ({@code viewW} × {@code viewH} px). Rectangles left empty are dropped.
     */
    static int[][] toPixels(double[][] css, double scale, int viewW, int viewH) {
        List<int[]> out = new ArrayList<>();
        if (css == null || !(scale > 0) || viewW <= 0 || viewH <= 0) return new int[0][];
        for (double[] r : css) {
            int left = clamp(Math.floor(r[0] * scale), viewW);
            int top = clamp(Math.floor(r[1] * scale), viewH);
            int right = clamp(Math.ceil((r[0] + r[2]) * scale), viewW);
            int bottom = clamp(Math.ceil((r[1] + r[3]) * scale), viewH);
            if (right > left && bottom > top) out.add(new int[] {left, top, right, bottom});
        }
        return out.toArray(new int[0][]);
    }

    private static int clamp(double v, int max) {
        if (v < 0) return 0;
        if (v > max) return max;
        return (int) v;
    }

    /** Pixels to CSS px for the page, rounded to 0.1 (insets, gesture areas). */
    static double toCss(int px, double scale) {
        if (!(scale > 0)) return 0;
        return Math.round(px / scale * 10.0) / 10.0;
    }

    // ----------------------------------------------------------------------------- app → page

    /** The handshake: {t:"native", v, app, build, caps, ui}. */
    static String hello(String appVersion, String build, String ui) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("t", "native");
        m.put("v", VERSION);
        m.put("app", appVersion);
        m.put("build", build);
        m.put("caps", CAPS);
        m.put("ui", ui);
        return MiniJson.write(m);
    }

    static String back(long id) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("t", "back");
        m.put("id", id);
        return MiniJson.write(m);
    }

    static String error(String type, String why) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("t", "err");
        m.put("m", type);
        m.put("why", why);
        return MiniJson.write(m);
    }

    /** {t:"insets", top, right, bottom, left} in CSS px: what the page must keep clear. */
    static String insets(double top, double right, double bottom, double left) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("t", "insets");
        m.put("top", top);
        m.put("right", right);
        m.put("bottom", bottom);
        m.put("left", left);
        return MiniJson.write(m);
    }

    /** {t:"diag", ...the fields...}. */
    static String diag(Map<String, Object> fields) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("t", "diag");
        m.putAll(fields);
        return MiniJson.write(m);
    }

    /** Short form for logs: never the whole message (it is the page's data). */
    static String describe(Msg m) {
        switch (m.t) {
            case "haptic":
                return "haptic " + m.haptic.wire;
            case "fullscreen":
                return String.format(Locale.ROOT, "fullscreen on=%b cutout=%b", m.on, m.cutout);
            case "keep_awake":
                return "keep_awake on=" + m.on;
            case "exclusion":
                return "exclusion " + m.rects.length + " rect(s)";
            case "back_handled":
                return "back_handled id=" + m.id + " handled=" + m.handled;
            case "set_ui":
                return "set_ui " + m.ui;
            case "ready":
                return "ready v=" + m.version + " caps=" + m.caps;
            default:
                return m.t;
        }
    }
}
