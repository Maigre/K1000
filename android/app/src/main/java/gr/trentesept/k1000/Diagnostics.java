// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.pm.PackageInfo;
import android.graphics.Rect;
import android.media.AudioManager;
import android.media.MicrophoneInfo;
import android.os.Build;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.util.DisplayMetrics;
import android.view.DisplayCutout;
import android.view.View;
import android.view.WindowInsets;
import android.webkit.WebView;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * What the phone can do, read at the bench in a minute (DESIGN §4.2 "Diagnostics panel"): the
 * WebView build, Android, the model, the unprocessed mic source, the microphones, the haptic
 * hardware, the gesture areas and what the page excluded from them. The same fields go to the page
 * ({@code {t:"diag"}}, android/BRIDGE.md) and to the app's own Diagnostics panel.
 */
final class Diagnostics {

    private Diagnostics() {
    }

    @SuppressWarnings("deprecation")   // Vibrator service: VibratorManager is API 31+
    static Map<String, Object> collect(Context c, View root, WebView web) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("app", BuildConfig.VERSION_NAME);
        d.put("build", BuildConfig.BUILD_TYPE);
        PackageInfo wv = WebView.getCurrentWebViewPackage();
        d.put("webview_package", wv == null ? "" : wv.packageName);
        d.put("webview_version", wv == null ? "" : wv.versionName);
        d.put("sdk", Build.VERSION.SDK_INT);
        d.put("release", Build.VERSION.RELEASE);
        d.put("security_patch", Build.VERSION.SECURITY_PATCH);
        d.put("model", Build.MANUFACTURER + " " + Build.MODEL);
        d.put("device", Build.DEVICE);

        AudioManager am = c.getSystemService(AudioManager.class);
        d.put("unprocessed_supported", am != null
                && "true".equalsIgnoreCase(am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED)));
        d.put("output_sample_rate", am == null ? "" : String.valueOf(am.getProperty(AudioManager.PROPERTY_OUTPUT_SAMPLE_RATE)));
        d.put("mics", microphones(am));

        Vibrator v = c.getSystemService(Vibrator.class);
        d.put("has_vibrator", v != null && v.hasVibrator());
        d.put("has_amplitude_control", v != null && v.hasAmplitudeControl());
        d.put("system_haptics", Haptics.systemEnabled(c));
        d.put("haptic_primitives", primitives(v));
        d.put("haptic_effects", effects(v));

        DisplayMetrics dm = c.getResources().getDisplayMetrics();
        float s = dm.density;
        d.put("density", (double) s);
        d.put("screen_css", Math.round(dm.widthPixels / s) + "x" + Math.round(dm.heightPixels / s));
        if (web != null) d.put("viewport_css", Math.round(web.getWidth() / s) + "x" + Math.round(web.getHeight() / s));
        d.putAll(insets(root, s));
        // Who keeps the screen on: the window's flag, and any view asking for it (a view's
        // keepScreenOn overrides the window flag the page turns off).
        if (root != null) {
            List<Object> keepers = new ArrayList<>();
            collectKeepers(root.getRootView(), keepers);
            d.put("keep_screen_on_views", keepers);
        }
        if (web != null) {
            List<Object> rects = new ArrayList<>();
            for (Rect r : web.getSystemGestureExclusionRects()) {
                rects.add(List.of(Bridge.toCss(r.left, s), Bridge.toCss(r.top, s),
                        Bridge.toCss(r.width(), s), Bridge.toCss(r.height(), s)));
            }
            d.put("exclusion_rects", rects);
        }
        return d;
    }

    private static void collectKeepers(View v, List<Object> out) {
        if (v.getKeepScreenOn()) out.add(v.getClass().getName() + (v.getVisibility() == View.VISIBLE ? "" : " (hidden)"));
        if (v instanceof android.view.ViewGroup) {
            android.view.ViewGroup g = (android.view.ViewGroup) v;
            for (int i = 0; i < g.getChildCount(); i++) collectKeepers(g.getChildAt(i), out);
        }
    }

    private static List<Object> microphones(AudioManager am) {
        List<Object> out = new ArrayList<>();
        if (am == null) return out;
        try {
            for (MicrophoneInfo m : am.getMicrophones()) {
                Map<String, Object> e = new LinkedHashMap<>();
                e.put("id", m.getId());
                e.put("desc", m.getDescription());
                e.put("type", m.getType());
                e.put("address", m.getAddress());
                e.put("location", location(m.getLocation()));
                e.put("directionality", directionality(m.getDirectionality()));
                e.put("sensitivity_db", known(m.getSensitivity(), MicrophoneInfo.SENSITIVITY_UNKNOWN));
                e.put("max_spl", known(m.getMaxSpl(), MicrophoneInfo.SPL_UNKNOWN));
                e.put("min_spl", known(m.getMinSpl(), MicrophoneInfo.SPL_UNKNOWN));
                out.add(e);
            }
        } catch (IOException | RuntimeException e) {
            out.add("error: " + e.getMessage());
        }
        return out;
    }

    private static Object known(float v, float unknown) {
        return v == unknown ? null : (Object) (double) v;
    }

    private static String location(int l) {
        switch (l) {
            case MicrophoneInfo.LOCATION_MAINBODY:
                return "mainbody";
            case MicrophoneInfo.LOCATION_MAINBODY_MOVABLE:
                return "mainbody_movable";
            case MicrophoneInfo.LOCATION_PERIPHERAL:
                return "peripheral";
            default:
                return "unknown";
        }
    }

    private static String directionality(int dir) {
        switch (dir) {
            case MicrophoneInfo.DIRECTIONALITY_OMNI:
                return "omni";
            case MicrophoneInfo.DIRECTIONALITY_BI_DIRECTIONAL:
                return "bidirectional";
            case MicrophoneInfo.DIRECTIONALITY_CARDIOID:
                return "cardioid";
            case MicrophoneInfo.DIRECTIONALITY_HYPER_CARDIOID:
                return "hypercardioid";
            case MicrophoneInfo.DIRECTIONALITY_SUPER_CARDIOID:
                return "supercardioid";
            default:
                return "unknown";
        }
    }

    /** Composition primitives the vibrator supports (API 30+; empty before). */
    @SuppressLint("WrongConstant")   // every id below is a Composition.PRIMITIVE_* constant
    private static Map<String, Object> primitives(Vibrator v) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (v == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return out;
        List<String> names = new ArrayList<>(List.of("click", "quick_rise", "slow_rise", "quick_fall", "tick"));
        List<Integer> ids = new ArrayList<>(List.of(VibrationEffect.Composition.PRIMITIVE_CLICK,
                VibrationEffect.Composition.PRIMITIVE_QUICK_RISE, VibrationEffect.Composition.PRIMITIVE_SLOW_RISE,
                VibrationEffect.Composition.PRIMITIVE_QUICK_FALL, VibrationEffect.Composition.PRIMITIVE_TICK));
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            names.addAll(List.of("thud", "spin", "low_tick"));
            ids.addAll(List.of(VibrationEffect.Composition.PRIMITIVE_THUD, VibrationEffect.Composition.PRIMITIVE_SPIN,
                    VibrationEffect.Composition.PRIMITIVE_LOW_TICK));
        }
        int[] a = new int[ids.size()];
        for (int i = 0; i < a.length; i++) a[i] = ids.get(i);
        boolean[] ok = v.arePrimitivesSupported(a);
        for (int i = 0; i < a.length; i++) out.put(names.get(i), ok[i]);
        return out;
    }

    /** Predefined effects the vibrator says it supports: "yes", "no" or "unknown" (API 30+). */
    @SuppressLint("WrongConstant")   // every id below is a VibrationEffect.EFFECT_* constant
    private static Map<String, Object> effects(Vibrator v) {
        Map<String, Object> out = new LinkedHashMap<>();
        if (v == null || Build.VERSION.SDK_INT < Build.VERSION_CODES.R) return out;
        String[] names = {"click", "double_click", "heavy_click", "tick"};
        int[] ids = {VibrationEffect.EFFECT_CLICK, VibrationEffect.EFFECT_DOUBLE_CLICK,
                VibrationEffect.EFFECT_HEAVY_CLICK, VibrationEffect.EFFECT_TICK};
        int[] r = v.areEffectsSupported(ids);
        for (int i = 0; i < ids.length; i++) {
            out.put(names[i], r[i] == Vibrator.VIBRATION_EFFECT_SUPPORT_YES ? "yes"
                    : r[i] == Vibrator.VIBRATION_EFFECT_SUPPORT_NO ? "no" : "unknown");
        }
        return out;
    }

    /** The gesture areas (where a swipe is Back or Home) and the cutout, in CSS px. */
    @SuppressWarnings("deprecation")   // the API 29 inset getters
    private static Map<String, Object> insets(View root, float s) {
        Map<String, Object> out = new LinkedHashMap<>();
        WindowInsets wi = root == null ? null : root.getRootWindowInsets();
        if (wi == null) return out;
        int[] g;
        int[] mg;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            android.graphics.Insets a = wi.getInsets(WindowInsets.Type.systemGestures());
            android.graphics.Insets b = wi.getInsets(WindowInsets.Type.mandatorySystemGestures());
            g = new int[] {a.left, a.top, a.right, a.bottom};
            mg = new int[] {b.left, b.top, b.right, b.bottom};
        } else {
            android.graphics.Insets a = wi.getSystemGestureInsets();
            android.graphics.Insets b = wi.getMandatorySystemGestureInsets();
            g = new int[] {a.left, a.top, a.right, a.bottom};
            mg = new int[] {b.left, b.top, b.right, b.bottom};
        }
        out.put("gesture_insets", cssLtrb(g, s));
        out.put("mandatory_gesture_insets", cssLtrb(mg, s));
        DisplayCutout dc = wi.getDisplayCutout();
        if (dc != null) {
            out.put("cutout_safe", cssLtrb(new int[] {dc.getSafeInsetLeft(), dc.getSafeInsetTop(),
                    dc.getSafeInsetRight(), dc.getSafeInsetBottom()}, s));
            List<Object> rects = new ArrayList<>();
            for (Rect r : dc.getBoundingRects()) {
                rects.add(List.of(Bridge.toCss(r.left, s), Bridge.toCss(r.top, s),
                        Bridge.toCss(r.width(), s), Bridge.toCss(r.height(), s)));
            }
            out.put("cutout_rects", rects);
        }
        return out;
    }

    private static Map<String, Object> cssLtrb(int[] ltrb, float s) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("left", Bridge.toCss(ltrb[0], s));
        m.put("top", Bridge.toCss(ltrb[1], s));
        m.put("right", Bridge.toCss(ltrb[2], s));
        m.put("bottom", Bridge.toCss(ltrb[3], s));
        return m;
    }

    /** One "key: value" line per field, for the panel (and the clipboard). */
    static String text(Map<String, Object> d) {
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, Object> e : d.entrySet()) {
            Object v = e.getValue();
            if (v instanceof List && !((List<?>) v).isEmpty() && ((List<?>) v).get(0) instanceof Map) {
                sb.append(e.getKey()).append(":\n");
                for (Object o : (List<?>) v) sb.append("  ").append(MiniJson.write(o)).append('\n');
            } else {
                sb.append(e.getKey()).append(": ")
                        .append(v instanceof String ? v : MiniJson.write(v)).append('\n');
            }
        }
        return sb.toString().trim();
    }

    /** The headline the panel opens with: the answers the bench wants first. */
    static String summary(Map<String, Object> d) {
        return String.format(Locale.ROOT, "%s · Android %s (API %s) · WebView %s\nUNPROCESSED mic: %s · haptics: %s",
                d.get("model"), d.get("release"), d.get("sdk"), d.get("webview_version"),
                Boolean.TRUE.equals(d.get("unprocessed_supported")) ? "yes" : "no",
                Boolean.TRUE.equals(d.get("system_haptics")) ? "on" : "OFF in Android settings");
    }
}
