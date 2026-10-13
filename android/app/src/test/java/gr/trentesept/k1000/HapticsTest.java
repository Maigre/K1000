// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.util.ArrayList;
import java.util.List;

/** The haptics' pure rules (Haptics.route, Haptics.TickGate), the fixes of the 0.2 review. */
public class HapticsTest {

    /** Review 2: with Touch feedback off, the answers still play (DESIGN §5.5), texture does not. */
    @Test
    public void answersSurviveTouchFeedbackOff() {
        for (Bridge.Haptic k : Bridge.Haptic.values()) {
            assertEquals(k.wire, Haptics.Route.SYSTEM, Haptics.route(k, true));
        }
        assertEquals(Haptics.Route.ALARM, Haptics.route(Bridge.Haptic.CONFIRM, false));
        assertEquals(Haptics.Route.ALARM, Haptics.route(Bridge.Haptic.CONFIRM2, false));   // the mute ack
        assertEquals(Haptics.Route.ALARM, Haptics.route(Bridge.Haptic.REJECT, false));
        assertEquals(Haptics.Route.ALARM, Haptics.route(Bridge.Haptic.REJECT3, false));    // no answer, link lost
        assertEquals(Haptics.Route.NONE, Haptics.route(Bridge.Haptic.TICK, false));
        assertEquals(Haptics.Route.NONE, Haptics.route(Bridge.Haptic.CLICK, false));
        assertEquals(Haptics.Route.NONE, Haptics.route(Bridge.Haptic.LONG_PRESS, false));
    }

    /** Review 6: at most 25 ticks in any second, and not fewer when the page sends more. */
    @Test
    public void tickCapIs25PerSecond() {
        assertEquals(1000, Haptics.TICK_MIN_MS * 25);
        Haptics.TickGate g = new Haptics.TickGate();
        List<Long> played = new ArrayList<>();
        for (long t = 0; t < 5000; t++) {          // the page asking every millisecond
            if (g.allow(t)) played.add(t);
        }
        int max = 0;
        int min = Integer.MAX_VALUE;
        for (long start = 0; start <= 4000; start++) {
            int n = 0;
            for (long p : played) if (p >= start && p < start + 1000) n++;
            max = Math.max(max, n);
            min = Math.min(min, n);
        }
        assertEquals("never more than 25 in a second", 25, max);
        assertEquals("never fewer either, under a flood", 25, min);
    }

    @Test
    public void tickBurstIsDroppedNotQueued() {
        Haptics.TickGate g = new Haptics.TickGate();
        int n = 0;
        for (int i = 0; i < 12; i++) if (g.allow(1000)) n++;   // 12 at once (the smoke test's burst)
        assertEquals(1, n);
        assertFalse(g.allow(1039));
        assertTrue(g.allow(1040));
        assertTrue(new Haptics.TickGate().allow(0));           // the very first tick plays
        assertTrue(new Haptics.TickGate().allow(-5));
    }
}
