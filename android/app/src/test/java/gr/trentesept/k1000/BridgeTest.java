// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

import java.util.List;
import java.util.Map;

/** The page bridge's contract (android/BRIDGE.md): what the app accepts, refuses and sends. */
public class BridgeTest {

    private static Bridge.Msg ok(String json) throws Bridge.Invalid {
        return Bridge.parse(json);
    }

    private static Bridge.Invalid refused(String json) {
        try {
            Bridge.Msg m = Bridge.parse(json);
            fail("accepted " + json + " as " + m.t);
            return null;
        } catch (Bridge.Invalid e) {
            return e;
        }
    }

    @Test
    public void haptics() throws Exception {
        for (Bridge.Haptic h : Bridge.Haptic.values()) {
            assertEquals(h, ok("{\"t\":\"haptic\",\"kind\":\"" + h.wire + "\"}").haptic);
        }
        assertEquals(List.of("tick", "click", "confirm", "reject", "long_press", "confirm2", "reject3"),
                java.util.Arrays.stream(Bridge.Haptic.values()).map(h -> h.wire).collect(java.util.stream.Collectors.toList()));
        assertEquals("haptic", refused("{\"t\":\"haptic\",\"kind\":\"buzz\"}").type);
        refused("{\"t\":\"haptic\"}");
        refused("{\"t\":\"haptic\",\"kind\":1}");
        refused("{\"t\":\"haptic\",\"kind\":\"TICK\"}");
    }

    @Test
    public void flags() throws Exception {
        Bridge.Msg m = ok("{\"t\":\"fullscreen\",\"on\":true}");
        assertTrue(m.on);
        assertFalse(m.cutout);
        m = ok("{\"t\":\"fullscreen\",\"on\":true,\"cutout\":true}");
        assertTrue(m.on && m.cutout);
        assertFalse(ok("{\"t\":\"fullscreen\",\"on\":false}").on);
        assertTrue(ok("{\"t\":\"keep_awake\",\"on\":true}").on);
        assertFalse(ok("{\"t\":\"keep_awake\",\"on\":false,\"extra\":[1,2]}").on);   // unknown fields ignored
        refused("{\"t\":\"fullscreen\"}");
        refused("{\"t\":\"fullscreen\",\"on\":1}");
        refused("{\"t\":\"fullscreen\",\"on\":\"true\"}");
        refused("{\"t\":\"fullscreen\",\"on\":true,\"cutout\":\"yes\"}");
        refused("{\"t\":\"keep_awake\",\"on\":null}");
    }

    @Test
    public void exclusion() throws Exception {
        Bridge.Msg m = ok("{\"t\":\"exclusion\",\"rects\":[[0,563,393,52],[0.5,619,392.5,76]]}");
        assertEquals(2, m.rects.length);
        assertArrayEquals(new double[] {0.5, 619, 392.5, 76}, m.rects[1], 0);
        assertEquals(0, ok("{\"t\":\"exclusion\",\"rects\":[]}").rects.length);   // clears
        refused("{\"t\":\"exclusion\"}");
        refused("{\"t\":\"exclusion\",\"rects\":{}}");
        refused("{\"t\":\"exclusion\",\"rects\":[[0,0,10]]}");
        refused("{\"t\":\"exclusion\",\"rects\":[[0,0,10,10,1]]}");
        refused("{\"t\":\"exclusion\",\"rects\":[[0,0,0,10]]}");          // empty
        refused("{\"t\":\"exclusion\",\"rects\":[[0,0,10,-1]]}");
        refused("{\"t\":\"exclusion\",\"rects\":[[0,\"0\",10,10]]}");
        refused("{\"t\":\"exclusion\",\"rects\":[[0,0,1e9,10]]}");        // out of range
        StringBuilder many = new StringBuilder("{\"t\":\"exclusion\",\"rects\":[");
        for (int i = 0; i <= Bridge.MAX_RECTS; i++) many.append(i > 0 ? "," : "").append("[0,0,1,1]");
        refused(many.append("]}").toString());
    }

    @Test
    public void backAndUi() throws Exception {
        Bridge.Msg m = ok("{\"t\":\"back_handled\",\"id\":7,\"handled\":true}");
        assertEquals(7, m.id);
        assertTrue(m.handled);
        assertFalse(ok("{\"t\":\"back_handled\",\"id\":0,\"handled\":false}").handled);
        refused("{\"t\":\"back_handled\",\"handled\":true}");
        refused("{\"t\":\"back_handled\",\"id\":1.5,\"handled\":true}");
        refused("{\"t\":\"back_handled\",\"id\":-1,\"handled\":true}");
        refused("{\"t\":\"back_handled\",\"id\":1}");
        assertEquals("v1", ok("{\"t\":\"set_ui\",\"ui\":\"v1\"}").ui);
        assertEquals("v2", ok("{\"t\":\"set_ui\",\"ui\":\"v2\"}").ui);
        refused("{\"t\":\"set_ui\",\"ui\":\"v3\"}");
        refused("{\"t\":\"set_ui\"}");
    }

    @Test
    public void readyAndDiag() throws Exception {
        Bridge.Msg m = ok("{\"t\":\"ready\",\"v\":1,\"caps\":[\"back\"]}");
        assertEquals(1, m.version);
        assertEquals(List.of("back"), m.caps);
        assertEquals(List.of(), ok("{\"t\":\"ready\"}").caps);
        refused("{\"t\":\"ready\",\"caps\":\"back\"}");
        refused("{\"t\":\"ready\",\"caps\":[1]}");
        refused("{\"t\":\"ready\",\"v\":0}");
        assertFalse(ok("{\"t\":\"diag\"}").show);
        assertTrue(ok("{\"t\":\"diag\",\"show\":true}").show);
    }

    @Test
    public void refusedWhole() {
        assertEquals("", refused(null).type);
        assertEquals("", refused("").type);
        assertEquals("", refused("not json").type);
        assertEquals("", refused("[\"haptic\"]").type);
        assertEquals("", refused("{\"kind\":\"tick\"}").type);
        assertEquals("", refused("{\"t\":5}").type);
        assertEquals("open_url", refused("{\"t\":\"open_url\",\"url\":\"https://example.com/\"}").type);
        assertEquals("eval", refused("{\"t\":\"eval\"}").type);
        StringBuilder big = new StringBuilder("{\"t\":\"haptic\",\"kind\":\"tick\",\"pad\":\"");
        while (big.length() <= Bridge.MAX_MESSAGE_CHARS) big.append('x');
        refused(big.append("\"}").toString());
        refused("{\"t\":\"haptic\",\"kind\":\"tick\",\"deep\":[[[[1]]]]}");   // nesting limit
    }

    /** CSS px rects → view px: Pixel 4a density 2.75 (440 dpi), rounded outward, clipped. */
    @Test
    public void toPixels() {
        double[][] css = {{0, 563, 393, 52}, {10.1, 20.1, 0.2, 0.2}, {-20, -20, 30, 30}, {380, 840, 50, 50},
                {500, 10, 10, 10}};
        int[][] px = Bridge.toPixels(css, 2.75, 1080, 2340);
        assertEquals(4, px.length);                                     // the off-screen one is gone
        assertArrayEquals(new int[] {0, 1548, 1080, 1692}, px[0]);                // 1081 clipped to the view
        assertArrayEquals(new int[] {27, 55, 29, 56}, px[1]);              // floor / ceil: never smaller
        assertArrayEquals(new int[] {0, 0, 28, 28}, px[2]);                // clipped at the top left
        assertArrayEquals(new int[] {1045, 2310, 1080, 2340}, px[3]);      // clipped at the bottom right
        assertEquals(0, Bridge.toPixels(css, 0, 1080, 2340).length);
        assertEquals(0, Bridge.toPixels(css, 2.75, 0, 2340).length);
        assertEquals(0, Bridge.toPixels(null, 2.75, 1080, 2340).length);
        assertEquals(10.2, Bridge.toCss(28, 2.75), 1e-9);                  // 10.18 to 0.1
        assertEquals(0.0, Bridge.toCss(28, 0), 0);
    }

    @Test
    public void appMessages() throws Exception {
        Map<?, ?> hello = (Map<?, ?>) MiniJson.parse(Bridge.hello("0.2.0", "release", "v2"), 4);
        assertEquals("native", hello.get("t"));
        assertEquals((double) Bridge.VERSION, hello.get("v"));
        assertEquals("0.2.0", hello.get("app"));
        assertEquals("v2", hello.get("ui"));
        assertEquals(Bridge.CAPS, hello.get("caps"));
        assertTrue(Bridge.CAPS.containsAll(List.of("haptic", "fullscreen", "exclusion", "keep_awake", "diag", "back")));
        assertEquals("{\"t\":\"back\",\"id\":3}", Bridge.back(3));
        assertEquals("{\"t\":\"err\",\"m\":\"haptic\",\"why\":\"x \\\"y\\\"\"}", Bridge.error("haptic", "x \"y\""));
        assertEquals("{\"t\":\"insets\",\"top\":28.4,\"right\":0,\"bottom\":0,\"left\":0}", Bridge.insets(28.4, 0, 0, 0));
        assertEquals("{\"t\":\"diag\",\"sdk\":33}", Bridge.diag(Map.of("sdk", 33)));
    }
}
