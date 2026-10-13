// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.After;
import org.junit.Test;

import java.util.List;

public class K1000HostsTest {

    @Test
    public void boxUrls() {
        assertTrue(K1000Hosts.isBoxUrl("https://10.42.0.1/"));
        assertTrue(K1000Hosts.isBoxUrl("https://10.42.0.1:443/app.js?x=1"));
        assertTrue(K1000Hosts.isBoxUrl("https://K1000.home.arpa/"));
        assertTrue(K1000Hosts.isBoxUrl("https://k1000.home.arpa./sw.js"));
        assertFalse(K1000Hosts.isBoxUrl("http://10.42.0.1/"));            // never cleartext
        assertFalse(K1000Hosts.isBoxUrl("https://10.42.0.1:5005/"));      // CamillaGUI port
        assertFalse(K1000Hosts.isBoxUrl("https://10.42.0.2/"));
        assertFalse(K1000Hosts.isBoxUrl("https://evil.k1000.home.arpa/"));
        assertFalse(K1000Hosts.isBoxUrl("https://k1000.home.arpa.evil.example/"));
        assertFalse(K1000Hosts.isBoxUrl("https://user@10.42.0.1/"));
        assertFalse(K1000Hosts.isBoxUrl("wss://10.42.0.1/ws"));
        assertFalse(K1000Hosts.isBoxUrl("file:///sdcard/x.html"));
        assertFalse(K1000Hosts.isBoxUrl("not a url"));
        assertFalse(K1000Hosts.isBoxUrl(null));
    }

    @Test
    public void origins() {
        assertTrue(K1000Hosts.isBoxOrigin("https://10.42.0.1/"));
        assertTrue(K1000Hosts.isBoxOrigin("https://10.42.0.1"));
        assertTrue(K1000Hosts.isBoxOrigin("https://k1000.home.arpa/"));
        assertFalse(K1000Hosts.isBoxOrigin("https://10.42.0.1/page"));
        assertFalse(K1000Hosts.isBoxOrigin("http://10.42.0.1/"));
        assertFalse(K1000Hosts.isBoxOrigin("https://example.com/"));
        assertFalse(K1000Hosts.isBoxOrigin("null"));
    }

    private static byte[] ip(int a, int b, int c, int d) {
        return new byte[] {(byte) a, (byte) b, (byte) c, (byte) d};
    }

    /** Fallback mode follows a Wi-Fi network only when its address is on the K1000 subnet. */
    @Test
    public void k1000Subnet() {
        assertTrue(K1000Hosts.inSubnet(ip(10, 42, 0, 17), K1000Hosts.SUBNET));
        assertTrue(K1000Hosts.inSubnet(ip(10, 42, 0, 255), K1000Hosts.SUBNET));
        assertFalse(K1000Hosts.inSubnet(ip(10, 42, 1, 17), K1000Hosts.SUBNET));
        assertFalse(K1000Hosts.inSubnet(ip(10, 43, 0, 17), K1000Hosts.SUBNET));
        assertFalse(K1000Hosts.inSubnet(ip(192, 168, 1, 23), K1000Hosts.SUBNET));   // home Wi-Fi
        assertFalse(K1000Hosts.inSubnet(ip(10, 0, 2, 16), K1000Hosts.SUBNET));      // emulator Wi-Fi
        assertFalse(K1000Hosts.inSubnet(new byte[16], K1000Hosts.SUBNET));          // IPv6
        assertFalse(K1000Hosts.inSubnet(null, K1000Hosts.SUBNET));
        // other prefixes (the smoke build points the filter at the emulator's Wi-Fi)
        assertTrue(K1000Hosts.inSubnet(ip(10, 0, 2, 16), "10.0.2.0/24"));
        assertTrue(K1000Hosts.inSubnet(ip(192, 168, 239, 5), "192.168.232.0/21"));
        assertFalse(K1000Hosts.inSubnet(ip(192, 168, 240, 5), "192.168.232.0/21"));
        assertTrue(K1000Hosts.inSubnet(ip(10, 42, 0, 1), "10.42.0.1/32"));
        assertFalse(K1000Hosts.inSubnet(ip(10, 42, 0, 2), "10.42.0.1/32"));
        assertTrue(K1000Hosts.inSubnet(ip(8, 8, 8, 8), "0.0.0.0/0"));
        for (String bad : new String[] {"", "10.42.0.0", "10.42.0/24", "10.42.0.0/33", "10.42.0.0/",
                "10.42.0.256/24", "10.42.0.-1/24", "10.42..0/24", "a.b.c.d/24", "10.42.0.0/24/1"}) {
            assertFalse(bad, K1000Hosts.inSubnet(ip(10, 42, 0, 1), bad));
        }
    }

    @Test
    public void hosts() {
        assertEquals("10.42.0.1", K1000Hosts.hostOf("https://10.42.0.1/x"));
        assertEquals("k1000.home.arpa", K1000Hosts.hostOf("https://K1000.HOME.ARPA./"));
        assertEquals("", K1000Hosts.hostOf("::"));
        assertTrue(K1000Hosts.isBoxHost("[10.42.0.1]"));
    }

    @After
    public void noBenchHost() {
        K1000Hosts.setBenchHost("");
    }

    /** App 0.2 loads v2 at /v2/; v1 at / is the escape hatch. The bridge port goes to the origin only. */
    @Test
    public void pagesAndOrigins() {
        assertEquals("https://10.42.0.1/v2/", K1000Hosts.pageUrl(K1000Hosts.primaryHost(), K1000Hosts.PATH_V2));
        assertEquals("https://k1000.home.arpa/", K1000Hosts.pageUrl(K1000Hosts.fallbackHost(), K1000Hosts.PATH_V1));
        assertEquals("https://10.42.0.1", K1000Hosts.originOf("https://10.42.0.1/v2/"));
        assertEquals("https://k1000.home.arpa", K1000Hosts.originOf("https://K1000.home.arpa./"));
        assertNull(K1000Hosts.originOf("https://example.com/v2/"));
        assertNull(K1000Hosts.originOf("http://10.42.0.1/v2/"));
        assertNull(K1000Hosts.originOf("https://10.42.0.1:8443/"));
        assertNull(K1000Hosts.originOf("data:text/html,x"));
        assertEquals("/v2/", K1000Hosts.pathOf("https://10.42.0.1/v2/?x=1#y"));
        assertEquals("/", K1000Hosts.pathOf("https://10.42.0.1"));
        assertEquals("", K1000Hosts.pathOf("::"));
        assertEquals(List.of(K1000Hosts.SUBNET), K1000Hosts.linkSubnets());
    }

    /** Bench builds only: one development host joins the box's two names; release has none. */
    @Test
    public void benchHost() {
        assertFalse(K1000Hosts.isBoxUrl("https://192.168.8.54/v2/"));
        K1000Hosts.setBenchHost("192.168.8.54");
        assertTrue(K1000Hosts.isBoxUrl("https://192.168.8.54/v2/"));
        assertTrue(K1000Hosts.isBoxOrigin("https://192.168.8.54/"));
        assertTrue(K1000Hosts.isBoxUrl("https://10.42.0.1/"));               // the box stays the box
        assertFalse(K1000Hosts.isBoxUrl("https://192.168.8.55/"));
        assertEquals("192.168.8.54", K1000Hosts.primaryHost());
        assertEquals("10.42.0.1", K1000Hosts.fallbackHost());
        assertEquals("https://192.168.8.54", K1000Hosts.originOf("https://192.168.8.54/v2/"));
        assertEquals(List.of(K1000Hosts.SUBNET, "192.168.8.0/24"), K1000Hosts.linkSubnets());
        K1000Hosts.setBenchHost("Bench.Lan.");
        assertEquals("bench.lan", K1000Hosts.benchHost());
        assertEquals(List.of(K1000Hosts.SUBNET), K1000Hosts.linkSubnets());  // a name: no subnet to follow
        for (String bad : new String[] {"10.42.0.1", "k1000.home.arpa", "a b", "-x.lan", "x/y", "*.lan", "[::1]x"}) {
            try {
                K1000Hosts.setBenchHost(bad);
                fail("accepted bench host " + bad);
            } catch (IllegalArgumentException expected) {
                // refused
            }
        }
        K1000Hosts.setBenchHost("");
        assertFalse(K1000Hosts.isBoxUrl("https://bench.lan/"));
        assertEquals("10.42.0.1", K1000Hosts.primaryHost());
        assertEquals("k1000.home.arpa", K1000Hosts.fallbackHost());
    }
}
