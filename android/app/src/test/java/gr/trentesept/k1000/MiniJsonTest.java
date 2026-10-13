// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class MiniJsonTest {

    private static Object p(String s) throws MiniJson.ParseException {
        return MiniJson.parse(s, 4);
    }

    @Test
    public void values() throws Exception {
        assertEquals(Boolean.TRUE, p("true"));
        assertEquals(Boolean.FALSE, p(" false "));
        assertNull(p("null"));
        assertEquals(12.0, p("12"));
        assertEquals(-0.5, p("-0.5"));
        assertEquals(1500.0, p("1.5e3"));
        assertEquals(0.0, p("0"));
        assertEquals("a\"b\\c/\n\t\u00e9", p("\"a\\\"b\\\\c\\/\\n\\t\\u00e9\""));
        assertEquals(List.of(1.0, "x", true), p("[1, \"x\", true]"));
        Object o = p("{\"t\":\"haptic\",\"kind\":\"tick\",\"n\":[1,2]}");
        assertTrue(o instanceof Map);
        Map<?, ?> m = (Map<?, ?>) o;
        assertEquals("haptic", m.get("t"));
        assertEquals(List.of(1.0, 2.0), m.get("n"));
        assertEquals(Map.of(), p("{}"));
        assertEquals(List.of(), p("[ ]"));
    }

    @Test
    public void refusals() {
        for (String bad : new String[] {"", " ", "{", "}", "[1,]", "{\"a\":1,}", "{a:1}", "'x'", "01", "1.", ".5",
                "-", "1e", "+1", "NaN", "Infinity", "1e999", "tru", "nul", "\"abc", "\"\\x\"", "\"\\u12\"",
                "\"a\u0001b\"", "{\"a\":1}x", "[1] [2]", "{\"a\":1,\"a\":2}", "[[[[[1]]]]]",
                "{\"a\":{\"b\":{\"c\":{\"d\":{}}}}}"}) {
            try {
                Object v = p(bad);
                fail("accepted " + bad + " as " + v);
            } catch (MiniJson.ParseException expected) {
                // refused
            }
        }
    }

    @Test
    public void depthLimit() throws Exception {
        p("[[[[1]]]]");                                         // 4 deep: the limit
        p("{\"a\":{\"b\":{\"c\":{}}}}");
        try {
            p("[[[[[1]]]]]");
            fail("5 deep accepted");
        } catch (MiniJson.ParseException expected) {
            // refused
        }
    }

    @Test
    public void writing() throws Exception {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("t", "native");
        m.put("v", 1);
        m.put("d", 2.5);
        m.put("whole", 3.0);
        m.put("nan", Double.NaN);
        m.put("caps", List.of("a", "b"));
        m.put("px", new int[] {1, 2});
        m.put("ok", true);
        m.put("none", null);
        m.put("s", "q\"\\\n" + (char) 0x2028 + (char) 1);
        String json = MiniJson.write(m);
        assertEquals("{\"t\":\"native\",\"v\":1,\"d\":2.5,\"whole\":3,\"nan\":null,\"caps\":[\"a\",\"b\"],"
                + "\"px\":[1,2],\"ok\":true,\"none\":null,\"s\":\"q\\\"\\\\\\n\\u2028\\u0001\"}", json);
        // and it reads back
        Map<?, ?> back = (Map<?, ?>) p(json);
        assertEquals("q\"\\\n" + (char) 0x2028 + (char) 1, back.get("s"));
        assertEquals(Arrays.asList(1.0, 2.0), back.get("px"));
    }
}
