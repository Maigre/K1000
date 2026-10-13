// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertSame;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

import java.net.SocketException;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.security.cert.CertPathValidatorException;
import java.security.cert.CertificateException;
import java.security.cert.CertificateExpiredException;
import java.util.List;

import javax.net.ssl.SSLException;
import javax.net.ssl.SSLHandshakeException;
import javax.net.ssl.SSLPeerUnverifiedException;
import javax.net.ssl.SSLProtocolException;

/** The K1000 Wi-Fi taken from the box (app 0.2.1): GET /api/wifi's validation and the decisions around it. */
public class BoxWifiTest {

    private static final String JSON = "application/json";

    private static byte[] b(String s) {
        return s.getBytes(StandardCharsets.UTF_8);
    }

    private static BoxWifi.Result ok(String body) {
        BoxWifi.Result r = BoxWifi.judge(200, JSON, b(body));
        assertEquals(body + " -> " + r, BoxWifi.Verdict.ADOPT, r.verdict);
        assertNotNull(r.creds);
        return r;
    }

    private static String refused(String body) {
        BoxWifi.Result r = BoxWifi.judge(200, JSON, b(body));
        assertEquals(body + " -> " + r, BoxWifi.Verdict.INVALID, r.verdict);
        assertNull(r.creds);
        return r.why;
    }

    private static String answer(String ssid, String psk) {
        return "{\"v\":1,\"ssid\":" + MiniJson.write(ssid) + ",\"psk\":" + MiniJson.write(psk) + "}";
    }

    /** Exactly the contract's answer. */
    @Test
    public void theBoxAnswer() {
        BoxWifi.Result r = ok("{\"v\":1,\"ssid\":\"K1000\",\"psk\":\"abcd-efgh-jkmn\",\"band\":\"a\",\"box\":\"2026.10.13-e0c0e5a\"}");
        assertEquals("K1000", r.creds.ssid);
        assertEquals("abcd-efgh-jkmn", r.creds.passphrase);
        assertFalse(r.creds.hidden);
        assertEquals("2026.10.13-e0c0e5a", r.box);
        assertEquals("a", r.band);
        // band and box are optional; unknown fields are ignored (a newer box may send more).
        r = ok("{\"psk\":\"abcd-efgh-jkmn\",\"ssid\":\"K1000\",\"v\":1,\"later\":{\"x\":[1,2]}}");
        assertEquals("", r.box);
        assertEquals("", r.band);
        // Non-string informational fields are ignored, long ones cut, control characters masked.
        r = ok("{\"v\":1,\"ssid\":\"K1000\",\"psk\":\"abcd-efgh-jkmn\",\"box\":42,\"band\":\"" + "x".repeat(100) + "\"}");
        assertEquals("", r.box);
        assertEquals(BoxWifi.MAX_INFO_CHARS, r.band.length());
        assertEquals("a?b", BoxWifi.info("a\nb"));
        // A refusal's reason (shown on the Connect screen) is cut short too.
        assertEquals(BoxWifi.MAX_WHY_CHARS, BoxWifi.Result.of(BoxWifi.Verdict.INVALID, "y".repeat(500)).why.length());
        assertEquals("", BoxWifi.Result.of(BoxWifi.Verdict.RETRY, null).why);
    }

    /** The log line never carries the password. */
    @Test
    public void neverThePasswordInTheLog() {
        BoxWifi.Result r = ok(answer("K1000", "secret-pass-1234"));
        assertFalse(r.toString().contains("secret-pass-1234"));
        assertTrue(r.toString().contains("K1000"));
        assertFalse(BoxWifi.compare(r, "JOIN", "K1000", "secret-pass-1234", true).toString().contains("secret-pass-1234"));
    }

    @Test
    public void versionMustBeOne() {
        refused("{\"ssid\":\"K1000\",\"psk\":\"abcd-efgh-jkmn\"}");
        refused("{\"v\":\"1\",\"ssid\":\"K1000\",\"psk\":\"abcd-efgh-jkmn\"}");
        refused("{\"v\":2,\"ssid\":\"K1000\",\"psk\":\"abcd-efgh-jkmn\"}");
        refused("{\"v\":1.5,\"ssid\":\"K1000\",\"psk\":\"abcd-efgh-jkmn\"}");
        refused("{\"v\":true,\"ssid\":\"K1000\",\"psk\":\"abcd-efgh-jkmn\"}");
        ok("{\"v\":1.0,\"ssid\":\"K1000\",\"psk\":\"abcd-efgh-jkmn\"}");   // the same number in JSON
    }

    /** The SSID: non-empty, at most 32 bytes (UTF-8), no control characters. */
    @Test
    public void ssidRules() {
        refused(answer("", "abcd-efgh-jkmn"));
        refused("{\"v\":1,\"psk\":\"abcd-efgh-jkmn\"}");
        refused("{\"v\":1,\"ssid\":null,\"psk\":\"abcd-efgh-jkmn\"}");
        refused("{\"v\":1,\"ssid\":1000,\"psk\":\"abcd-efgh-jkmn\"}");
        ok(answer("x".repeat(32), "abcd-efgh-jkmn"));
        refused(answer("x".repeat(33), "abcd-efgh-jkmn"));
        ok(answer("€".repeat(10), "abcd-efgh-jkmn"));           // 30 bytes
        refused(answer("€".repeat(11), "abcd-efgh-jkmn"));      // 11 characters, 33 bytes
        refused(answer("K1000\n", "abcd-efgh-jkmn"));
        refused(answer("K1000\u0000", "abcd-efgh-jkmn"));
        refused(answer("K1000\u0085", "abcd-efgh-jkmn"));
        assertEquals("K1000 2", ok(answer("K1000 2", "abcd-efgh-jkmn")).creds.ssid);
    }

    /** The password: 8 to 63 printable ASCII characters, exactly as given. */
    @Test
    public void pskRules() {
        refused("{\"v\":1,\"ssid\":\"K1000\"}");
        refused("{\"v\":1,\"ssid\":\"K1000\",\"psk\":12345678}");
        refused(answer("K1000", "1234567"));
        ok(answer("K1000", "12345678"));
        ok(answer("K1000", "x".repeat(63)));
        refused(answer("K1000", "x".repeat(64)));
        refused(answer("K1000", "motdepassé1"));
        refused(answer("K1000", "abcd\tefgh"));
        refused(answer("K1000", "abcd\u007fefgh"));
        assertEquals(" a;b\\\"c:d, ~", ok(answer("K1000", " a;b\\\"c:d, ~")).creds.passphrase);
    }

    /** One JSON object in strict UTF-8, nothing else, no duplicate fields, a small body. */
    @Test
    public void shapeAndSize() {
        refused("");
        refused("[1]");
        refused("\"K1000\"");
        refused("{\"v\":1,\"ssid\":\"K1000\",\"psk\":\"abcd-efgh-jkmn\"} x");
        refused("{\"v\":1,\"ssid\":\"K1000\",\"psk\":\"abcd-efgh-jkmn\",\"psk\":\"other-pass-0000\"}");
        refused("{v:1}");
        // Malformed UTF-8 is refused, not replaced (the SSID would silently change).
        byte[] bad = b(answer("K1000x", "abcd-efgh-jkmn"));
        bad[bad.length - 26] = (byte) 0xff;
        BoxWifi.Result r = BoxWifi.judge(200, JSON, bad);
        assertEquals(BoxWifi.Verdict.INVALID, r.verdict);
        assertEquals("not UTF-8", r.why);
        // Too long: the box's answer is a few dozen bytes.
        String pad = " ".repeat(BoxWifi.MAX_BODY_BYTES);
        assertEquals("answer too long", refused(answer("K1000", "abcd-efgh-jkmn") + pad));
        ok(answer("K1000", "abcd-efgh-jkmn") + " ".repeat(BoxWifi.MAX_BODY_BYTES - 50));
    }

    /** Only a 200 with application/json counts; 404 = an older box; nothing else is taken. */
    @Test
    public void statusAndType() {
        byte[] good = b(answer("K1000", "abcd-efgh-jkmn"));
        assertEquals(BoxWifi.Verdict.ADOPT, BoxWifi.judge(200, "application/json; charset=utf-8", good).verdict);
        assertEquals(BoxWifi.Verdict.ADOPT, BoxWifi.judge(200, "Application/JSON", good).verdict);
        assertEquals(BoxWifi.Verdict.INVALID, BoxWifi.judge(200, "text/html", good).verdict);
        assertEquals(BoxWifi.Verdict.INVALID, BoxWifi.judge(200, "application/jsonp", good).verdict);
        assertEquals(BoxWifi.Verdict.INVALID, BoxWifi.judge(200, null, good).verdict);
        assertEquals(BoxWifi.Verdict.ABSENT, BoxWifi.judge(404, "text/plain", new byte[0]).verdict);
        assertEquals(BoxWifi.Verdict.RETRY, BoxWifi.judge(429, null, new byte[0]).verdict);
        assertEquals(BoxWifi.Verdict.RETRY, BoxWifi.judge(503, null, new byte[0]).verdict);
        // A redirect is never followed (it could point at plain HTTP or another host): not taken.
        for (int s : new int[] {301, 302, 307, 308, 403, 500, 204}) {
            BoxWifi.Result r = BoxWifi.judge(s, JSON, good);
            assertEquals("HTTP " + s, BoxWifi.Verdict.INVALID, r.verdict);
            assertNull(r.creds);
        }
    }

    /** HTTPS to the box's own two names only: never plain HTTP, never a bench host. */
    @Test
    public void onlyTheBoxOverHttps() {
        assertEquals(List.of("https://10.42.0.1/api/wifi", "https://k1000.home.arpa/api/wifi"), BoxWifi.urls());
        K1000Hosts.setBenchHost("192.168.8.54");
        try {
            assertEquals(List.of("https://10.42.0.1/api/wifi", "https://k1000.home.arpa/api/wifi"), BoxWifi.urls());
        } finally {
            K1000Hosts.setBenchHost("");
        }
        for (String u : BoxWifi.urls()) {
            assertTrue(u, K1000Hosts.isBoxUrl(u));
            assertTrue(u, u.startsWith("https://"));
        }
    }

    /** What the app already stores decides between taking it and changing nothing. */
    @Test
    public void sameOrNew() {
        BoxWifi.Result r = ok(answer("K1000", "abcd-efgh-jkmn"));
        // The app is on K1000 itself and the Connect screen was opened: nothing re-joins.
        assertEquals(BoxWifi.Verdict.SAME, BoxWifi.compare(r, "JOIN", "K1000", "abcd-efgh-jkmn", true).verdict);
        // First run, a new password on the box, another SSID, or the fallback mode before (its box
        // unticked on the Connect screen): taken, JOIN from then on, joined or not.
        for (boolean joined : new boolean[] {true, false}) {
            assertSame(r, BoxWifi.compare(r, "", null, null, joined));
            assertEquals(BoxWifi.Verdict.ADOPT, BoxWifi.compare(r, "JOIN", "K1000", "old-password-1", joined).verdict);
            assertEquals(BoxWifi.Verdict.ADOPT, BoxWifi.compare(r, "JOIN", "K1000-B", "abcd-efgh-jkmn", joined).verdict);
            assertEquals(BoxWifi.Verdict.ADOPT, BoxWifi.compare(r, "SYSTEM", "K1000", "abcd-efgh-jkmn", joined).verdict);
            // Anything but ADOPT passes through untouched.
            BoxWifi.Result absent = BoxWifi.judge(404, null, new byte[0]);
            assertSame(absent, BoxWifi.compare(absent, "JOIN", "K1000", "abcd-efgh-jkmn", joined));
        }
    }

    /**
     * Review 2026-10-13 (3): the app took the box's Wi-Fi, Android's dialog said "No devices found",
     * Cancel, Change K1000 Wi-Fi: the box gives the same Wi-Fi, but the app is NOT on it. Not SAME
     * (whose line says the app already joins it): SAME_UNJOINED, nothing taken again (the same
     * dialog would come back), a final answer, and its own line pointing to the fallback box.
     */
    @Test
    public void sameButNotJoined() {
        BoxWifi.Result r = ok(answer("K1000", "abcd-efgh-jkmn"));
        BoxWifi.Result c = BoxWifi.compare(r, "JOIN", "K1000", "abcd-efgh-jkmn", false);
        assertEquals(BoxWifi.Verdict.SAME_UNJOINED, c.verdict);
        assertFalse(c.toString(), c.toString().contains("abcd-efgh-jkmn"));
        BoxWifi.Tries t = new BoxWifi.Tries();
        assertTrue(t.ended(t.started("net-a")));
        assertEquals(-1, t.answered("net-a", c.verdict));
        assertFalse(t.due("net-a"));
    }

    /**
     * Review 2026-10-13 (1): only a refused certificate is "not the K1000 box" (REFUSED, final, the
     * other name not tried). A connection that drops inside TLS, during or after the handshake (the
     * edge of Wi-Fi range, the box's service restarting), is no answer: RETRY, like any I/O error.
     */
    @Test
    public void onlyACertificateFailureIsAFakeBox() {
        // Android's own pin (network security config) refusing the chain during the handshake:
        // SSLHandshakeException <- CertificateException <- CertPathValidatorException.
        SSLHandshakeException pin = new SSLHandshakeException(
                "java.security.cert.CertPathValidatorException: Trust anchor for certification path not found.");
        pin.initCause(new CertificateException(new CertPathValidatorException("Trust anchor for certification path not found.")));
        assertTrue(BoxWifi.certificateRefused(pin));
        // Only the validator's exception, deeper down; an expired certificate; a name mismatch.
        SSLHandshakeException deep = new SSLHandshakeException("Handshake failed");
        deep.initCause(new IllegalStateException(new CertPathValidatorException("not signed by the K1000 CA")));
        assertTrue(BoxWifi.certificateRefused(deep));
        SSLHandshakeException expired = new SSLHandshakeException("expired");
        expired.initCause(new CertificateExpiredException("NotAfter: Mon Oct 12 2026"));
        assertTrue(BoxWifi.certificateRefused(expired));
        assertTrue(BoxWifi.certificateRefused(new SSLPeerUnverifiedException("Hostname 10.42.0.1 not verified")));
        // A wrapper that kept only the text; an earlier attempt's pin failure, suppressed under a
        // later failure (the HTTP stack's retry).
        assertTrue(BoxWifi.certificateRefused(new SSLHandshakeException(
                "java.security.cert.CertPathValidatorException: Trust anchor for certification path not found.")));
        assertTrue(BoxWifi.certificateRefused(new SSLHandshakeException("Chain validation failed")));
        SSLProtocolException later = new SSLProtocolException("INAPPROPRIATE_FALLBACK");
        later.addSuppressed(pin);
        assertTrue(BoxWifi.certificateRefused(later));
        String line = BoxWifi.chain(later, 600);
        assertTrue(line, line.startsWith("javax.net.ssl.SSLProtocolException: INAPPROPRIATE_FALLBACK [suppressed "
                + "javax.net.ssl.SSLHandshakeException: java.security.cert.CertPathValidatorException"));
        assertEquals(40, BoxWifi.chain(later, 40).length());

        // Dropped connections: not a fake box.
        assertFalse(BoxWifi.certificateRefused(new SSLException("Connection closed by peer")));
        assertFalse(BoxWifi.certificateRefused(new SSLHandshakeException("connection closed")));
        SSLHandshakeException eof = new SSLHandshakeException("connection closed");
        eof.initCause(new java.io.EOFException("connection closed"));
        assertFalse(BoxWifi.certificateRefused(eof));
        assertFalse(BoxWifi.certificateRefused(new SSLException(
                "Read error: ssl=0x7b2c: I/O error during system call, Connection reset by peer",
                new SocketException("Connection reset by peer"))));
        SSLHandshakeException aborted = new SSLHandshakeException("Handshake failed");
        aborted.initCause(new SSLProtocolException(
                "SSL handshake aborted: ssl=0x7b2c: I/O error during system call, Connection reset by peer"));
        assertFalse(BoxWifi.certificateRefused(aborted));
        assertFalse(BoxWifi.certificateRefused(new SSLException("Read timed out", new SocketTimeoutException())));
        assertFalse(BoxWifi.certificateRefused(new SSLProtocolException("WRONG_VERSION_NUMBER")));
        assertFalse(BoxWifi.certificateRefused(null));
        // A cause loop ends.
        SSLException a = new SSLException("a");
        SSLException b = new SSLException("b");
        a.initCause(b);
        b.initCause(a);
        assertFalse(BoxWifi.certificateRefused(a));
    }

    /** When MainActivity asks the box. */
    @Test
    public void whenToAsk() {
        // trusted, connect screen, fallback ticked, foreground, on K1000, due, busy, local net, asked
        assertEquals(BoxWifi.Step.PROBE, BoxWifi.next(true, true, false, true, true, true, false, true, false));
        assertEquals(BoxWifi.Step.PROBE, BoxWifi.next(true, true, false, true, true, true, false, true, true));
        // Each condition alone stops it.
        assertEquals(BoxWifi.Step.NONE, BoxWifi.next(false, true, false, true, true, true, false, true, false));
        assertEquals(BoxWifi.Step.NONE, BoxWifi.next(true, false, false, true, true, true, false, true, false));
        assertEquals(BoxWifi.Step.NONE, BoxWifi.next(true, true, false, false, true, true, false, true, false));
        assertEquals(BoxWifi.Step.NONE, BoxWifi.next(true, true, false, true, false, true, false, true, false));
        assertEquals(BoxWifi.Step.NONE, BoxWifi.next(true, true, false, true, true, false, false, true, false));
        assertEquals(BoxWifi.Step.NONE, BoxWifi.next(true, true, false, true, true, true, true, true, false));
        // The fallback ticked is the user's choice: never asked over it (no SYSTEM / JOIN loop).
        assertEquals(BoxWifi.Step.NONE, BoxWifi.next(true, true, true, true, true, true, false, true, false));
        // Android 17+: the local network permission first, asked once by itself.
        assertEquals(BoxWifi.Step.ASK_LOCAL_NETWORK, BoxWifi.next(true, true, false, true, true, true, false, false, false));
        assertEquals(BoxWifi.Step.NONE, BoxWifi.next(true, true, false, true, true, true, false, false, true));
        // Not on K1000 (home Wi-Fi, no Wi-Fi): no permission prompt either.
        assertEquals(BoxWifi.Step.NONE, BoxWifi.next(true, true, false, true, false, true, false, false, false));
    }

    /** How often one network is asked on one Connect screen. */
    @Test
    public void tries() {
        BoxWifi.Tries t = new BoxWifi.Tries();
        Object a = "net-a";
        Object b = "net-b";
        assertFalse(t.due(null));
        assertTrue(t.due(a));
        // No answer: asked again 2 s, then 4 s later; after the third try, not again.
        t.started(a);
        assertEquals(2000, t.answered(a, BoxWifi.Verdict.RETRY));
        assertTrue(t.due(a));
        t.started(a);
        assertEquals(4000, t.answered(a, BoxWifi.Verdict.RETRY));
        t.started(a);
        assertEquals(-1, t.answered(a, BoxWifi.Verdict.RETRY));
        assertFalse(t.due(a));
        // Another network starts over; a final answer ends it at once.
        assertTrue(t.due(b));
        t.started(b);
        assertEquals(-1, t.answered(b, BoxWifi.Verdict.ABSENT));
        assertFalse(t.due(b));
        // A late answer from a network that is no longer the current one changes nothing.
        assertTrue(t.due(a));
        t.started(a);
        assertEquals(-1, t.answered(b, BoxWifi.Verdict.ADOPT));
        assertTrue(t.due(a));
        assertEquals(1, t.tries());
        // The Connect screen shown again: asked again.
        t.reset();
        assertTrue(t.due(b));
        for (BoxWifi.Verdict v : new BoxWifi.Verdict[] {BoxWifi.Verdict.ADOPT, BoxWifi.Verdict.SAME,
                BoxWifi.Verdict.SAME_UNJOINED, BoxWifi.Verdict.ABSENT, BoxWifi.Verdict.REFUSED,
                BoxWifi.Verdict.INVALID}) {
            t.reset();
            t.started(a);
            assertEquals(v.name(), -1, t.answered(a, v));
            assertFalse(v.name(), t.due(a));
        }
    }

    /**
     * Review 2026-10-13 (2): Change K1000 Wi-Fi › Cancel › Change K1000 Wi-Fi while the box is slow.
     * The new screen must not wait for a Wi-Fi change to ask, and an answer to the old screen must
     * not count against the new screen's tries (it made "did not answer" final after one try).
     */
    @Test
    public void anAnswerToAnEarlierConnectScreen() {
        BoxWifi.Tries t = new BoxWifi.Tries();
        Object a = "net-a";
        t.reset();                                  // Change K1000 Wi-Fi
        assertTrue(t.due(a));
        int first = t.started(a);
        assertTrue(t.busy());
        t.reset();                                  // Cancel (closeSetup)
        t.reset();                                  // Change K1000 Wi-Fi again (showSetup)
        // The new screen waits for the running request (one at a time)...
        assertTrue(t.busy());
        assertEquals(BoxWifi.Step.NONE, BoxWifi.next(true, true, false, true, true, t.due(a), t.busy(), true, false));
        // ...whose answer does not count: dropped, and then this screen asks at once, its try 1.
        assertFalse(t.ended(first));
        assertFalse(t.busy());
        assertEquals(BoxWifi.Step.PROBE, BoxWifi.next(true, true, false, true, true, t.due(a), t.busy(), true, false));
        int second = t.started(a);
        assertEquals(1, t.tries());
        assertTrue(t.ended(second));
        assertEquals(2000, t.answered(a, BoxWifi.Verdict.RETRY));   // no answer: asked again, not final

        // The screen shown again over itself while a request runs (e.g. an unusable code scanned):
        // that request's no answer is not this screen's, which starts its own count.
        assertTrue(t.due(a));
        int third = t.started(a);
        assertEquals(2, t.tries());
        t.reset();
        assertFalse(t.ended(third));
        assertTrue(t.due(a));
        t.started(a);
        assertEquals(1, t.tries());

        // Only the running request's token ends it.
        assertFalse(t.ended(0));
        assertFalse(t.ended(third));
        assertTrue(t.busy());
    }
}
