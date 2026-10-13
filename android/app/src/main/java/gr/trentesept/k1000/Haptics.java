// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import android.content.Context;
import android.media.AudioAttributes;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.os.VibrationAttributes;
import android.os.VibrationEffect;
import android.os.Vibrator;
import android.provider.Settings;
import android.view.HapticFeedbackConstants;
import android.view.View;

/**
 * The page's haptics (bridge {@code {t:"haptic", kind}}, DESIGN §5.5) on Android's own haptic
 * feedback: {@code View.performHapticFeedback} with the system's constants, so the phone's tuned
 * effects apply. When the system did not play one (and touch feedback is on), a predefined
 * {@link VibrationEffect} with the touch usage plays instead.
 *
 * <p>The phone's *Touch feedback* setting: the texture kinds (tick, click, long_press) follow it,
 * off means silent. The answers (confirm, confirm2, reject, reject3: the box's ack of a mute, a
 * refusal, no answer, link lost) cannot be turned off (DESIGN §5.5), so with Touch feedback off
 * they play through the vibrator with the ALARM usage, which that setting does not silence (the
 * phone's master vibration switch and its alarm-vibration setting still do). Never
 * FLAG_IGNORE_GLOBAL_SETTING (Android 13 ignores it for apps anyway).
 *
 * <table>
 *   <tr><th>kind</th><th>Android</th></tr>
 *   <tr><td>tick</td><td>CLOCK_TICK (a detent; at most 25 per second, extra ones dropped)</td></tr>
 *   <tr><td>click</td><td>VIRTUAL_KEY (EFFECT_CLICK: a landmark, a sticky stop)</td></tr>
 *   <tr><td>confirm</td><td>CONFIRM (API 30+; VIRTUAL_KEY before)</td></tr>
 *   <tr><td>reject</td><td>REJECT (API 30+; LONG_PRESS before)</td></tr>
 *   <tr><td>long_press</td><td>LONG_PRESS (a hold arms)</td></tr>
 *   <tr><td>confirm2</td><td>CONFIRM, then CONFIRM 140 ms later (the box acked a mute)</td></tr>
 *   <tr><td>reject3</td><td>REJECT three times, 180 ms apart (no answer, link lost)</td></tr>
 * </table>
 */
final class Haptics {

    /** Ticks at least this far apart: 1000 / 40 = 25 per second at most (DESIGN §5.5). */
    static final long TICK_MIN_MS = 40;

    /** How a kind plays, given the Touch feedback setting. */
    enum Route {
        /** performHapticFeedback (a touch-usage vibration if the system played nothing). */
        SYSTEM,
        /** Touch feedback is off and this is an answer: the vibrator, ALARM usage. */
        ALARM,
        /** Touch feedback is off and this is texture: nothing, as the user asked. */
        NONE
    }

    /** The kinds that answer the user: never silenced by Touch feedback (DESIGN §5.5). */
    static boolean isAnswer(Bridge.Haptic kind) {
        return kind == Bridge.Haptic.CONFIRM || kind == Bridge.Haptic.CONFIRM2
                || kind == Bridge.Haptic.REJECT || kind == Bridge.Haptic.REJECT3;
    }

    static Route route(Bridge.Haptic kind, boolean touchFeedbackOn) {
        if (touchFeedbackOn) return Route.SYSTEM;
        return isAnswer(kind) ? Route.ALARM : Route.NONE;
    }

    /** The detent rate cap: a tick within TICK_MIN_MS of the last one played is dropped. */
    static final class TickGate {
        private long last = Long.MIN_VALUE;

        boolean allow(long nowMs) {
            if (last != Long.MIN_VALUE && nowMs - last < TICK_MIN_MS) return false;
            last = nowMs;
            return true;
        }
    }

    private final View view;
    private final Vibrator vibrator;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final TickGate ticks = new TickGate();
    /** Played / dropped counters, for the Diagnostics panel. */
    int played;
    int dropped;
    int fallbacks;
    int alarms;

    @SuppressWarnings("deprecation")   // Vibrator service: VibratorManager is API 31+
    Haptics(Context context, View view) {
        this.view = view;
        this.vibrator = context.getSystemService(Vibrator.class);
    }

    /**
     * The phone's Touch feedback setting (Settings › Sound & vibration › Vibration & haptics). The
     * setting is deprecated for writing (API 33); reading it is still how an app sees it.
     */
    @SuppressWarnings("deprecation")
    static boolean systemEnabled(Context c) {
        return Settings.System.getInt(c.getContentResolver(), Settings.System.HAPTIC_FEEDBACK_ENABLED, 1) != 0;
    }

    /** Plays {@code kind}; false when it was dropped (tick rate) or nothing played. */
    boolean play(Bridge.Haptic kind) {
        switch (kind) {
            case TICK: {
                if (!ticks.allow(SystemClock.uptimeMillis())) {
                    dropped++;
                    return false;
                }
                return perform(kind, HapticFeedbackConstants.CLOCK_TICK, VibrationEffect.EFFECT_TICK);
            }
            case CLICK:
                return perform(kind, HapticFeedbackConstants.VIRTUAL_KEY, VibrationEffect.EFFECT_CLICK);
            case CONFIRM:
                return confirm();
            case REJECT:
                return reject();
            case LONG_PRESS:
                return perform(kind, HapticFeedbackConstants.LONG_PRESS, VibrationEffect.EFFECT_HEAVY_CLICK);
            case CONFIRM2: {
                boolean ok = confirm();
                main.postDelayed(this::confirm, 140);
                return ok;
            }
            case REJECT3: {
                boolean ok = reject();
                main.postDelayed(this::reject, 180);
                main.postDelayed(this::reject, 360);
                return ok;
            }
            default:
                return false;
        }
    }

    private boolean confirm() {
        int c = Build.VERSION.SDK_INT >= Build.VERSION_CODES.R ? HapticFeedbackConstants.CONFIRM : HapticFeedbackConstants.VIRTUAL_KEY;
        return perform(Bridge.Haptic.CONFIRM, c, VibrationEffect.EFFECT_CLICK);
    }

    private boolean reject() {
        int c = Build.VERSION.SDK_INT >= Build.VERSION_CODES.R ? HapticFeedbackConstants.REJECT : HapticFeedbackConstants.LONG_PRESS;
        return perform(Bridge.Haptic.REJECT, c, VibrationEffect.EFFECT_DOUBLE_CLICK);
    }

    private boolean perform(Bridge.Haptic kind, int constant, int effect) {
        switch (route(kind, systemEnabled(view.getContext()))) {
            case NONE:
                return false;   // Touch feedback is off: respected for texture
            case ALARM:
                if (!vibrate(effect, true)) return false;
                alarms++;
                return true;
            default:
                break;
        }
        if (view.performHapticFeedback(constant)) {
            played++;
            return true;
        }
        // The system played nothing although Touch feedback is on (the view not ready).
        if (!vibrate(effect, false)) return false;
        fallbacks++;
        return true;
    }

    /** A predefined effect on the vibrator: the touch usage, or the alarm usage for an answer. */
    private boolean vibrate(int effect, boolean alarm) {
        if (vibrator == null || !vibrator.hasVibrator()) return false;
        VibrationEffect e = VibrationEffect.createPredefined(effect);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            vibrator.vibrate(e, VibrationAttributes.createForUsage(
                    alarm ? VibrationAttributes.USAGE_ALARM : VibrationAttributes.USAGE_TOUCH));
        } else {
            vibrateLegacy(e, alarm);
        }
        return true;
    }

    @SuppressWarnings("deprecation")   // vibrate(effect, AudioAttributes): the API 29-32 way
    private void vibrateLegacy(VibrationEffect e, boolean alarm) {
        vibrator.vibrate(e, new AudioAttributes.Builder()
                .setUsage(alarm ? AudioAttributes.USAGE_ALARM : AudioAttributes.USAGE_ASSISTANCE_SONIFICATION)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build());
    }

    void cancel() {
        main.removeCallbacksAndMessages(null);
    }
}
