// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import org.junit.Test;

public class WifiQrTest {

    private static WifiQr.Credentials ok(String payload) throws Exception {
        return WifiQr.parse(payload);
    }

    private static String refused(String payload) {
        try {
            WifiQr.parse(payload);
        } catch (WifiQr.InvalidException e) {
            return e.getMessage();
        }
        fail("accepted: " + payload);
        return null;
    }

    /** Exactly what box/k1000/ui/qr.py wifi_payload("K1000", psk) writes. */
    @Test
    public void boxCode() throws Exception {
        WifiQr.Credentials c = ok("WIFI:T:WPA;S:K1000;P:abcd-efgh-jkmn;;");
        assertEquals("K1000", c.ssid);
        assertEquals("abcd-efgh-jkmn", c.passphrase);
        assertFalse(c.hidden);
    }

    @Test
    public void fieldsInAnyOrderHiddenAndLowerCasePrefix() throws Exception {
        WifiQr.Credentials c = ok("wifi:P:12345678;H:true;S:K1000;T:WPA2;;");
        assertEquals("K1000", c.ssid);
        assertEquals("12345678", c.passphrase);
        assertTrue(c.hidden);
    }

    /** qr.py escapes \ ; , " : with a backslash. */
    @Test
    public void escapes() throws Exception {
        WifiQr.Credentials c = ok("WIFI:T:WPA;S:K\\;1000\\:x;P:a\\\\b\\;c\\,d\\\"e\\:f;;");
        assertEquals("K;1000:x", c.ssid);
        assertEquals("a\\b;c,d\"e:f", c.passphrase);
    }

    @Test
    public void quotedValuesAndMissingTerminator() throws Exception {
        WifiQr.Credentials c = ok("WIFI:S:\"K1000\";T:WPA;P:\"pass word 1\"");
        assertEquals("K1000", c.ssid);
        assertEquals("pass word 1", c.passphrase);
    }

    @Test
    public void typeIsOptionalWithAPassword() throws Exception {
        assertEquals("K1000", ok("WIFI:S:K1000;P:abcdefgh;;").ssid);
    }

    @Test
    public void refusals() {
        assertTrue(refused("https://10.42.0.1/").contains("not a Wi-Fi QR code"));
        assertTrue(refused("WIFI:T:nopass;S:K1000;;").contains("open network"));
        assertTrue(refused("WIFI:T:WEP;S:K1000;P:abcdefgh;;").contains("WEP"));
        assertTrue(refused("WIFI:T:SAE;S:K1000;P:abcdefgh;;").contains("SAE"));
        assertTrue(refused("WIFI:T:WPA;P:abcdefgh;;").contains("name is missing"));
        assertTrue(refused("WIFI:T:WPA;S:K1000;;").contains("password is missing"));
        assertTrue(refused("WIFI:T:WPA;S:K1000;P:short;;").contains("too short"));
        assertTrue(refused("WIFI:T:WPA;S:K1000;P:" + "x".repeat(64) + ";;").contains("too long"));
        assertTrue(refused("WIFI:T:WPA;S:K1000;P:pässwörd;;").contains("plain letters"));
        assertTrue(refused("WIFI:T:WPA;S:" + "é".repeat(17) + ";P:abcdefgh;;").contains("32 bytes"));
        assertTrue(refused("WIFI:T:WPA;S:K1000;P:abcdefgh\\").contains("backslash"));
        assertTrue(refused(null).contains("Empty"));
    }

    @Test
    public void typedCredentials() throws Exception {
        assertEquals("abcd-efgh-jkmn", WifiQr.of("K1000", "abcd-efgh-jkmn", false).passphrase);
        try {
            WifiQr.of("", "abcd-efgh-jkmn", false);
            fail();
        } catch (WifiQr.InvalidException e) {
            assertTrue(e.getMessage().contains("name"));
        }
    }
}
