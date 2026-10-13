// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.security.cert.CertPathValidatorException;
import java.security.cert.CertificateException;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.regex.Pattern;

import javax.net.ssl.SSLPeerUnverifiedException;

/**
 * The K1000 Wi-Fi taken from the box itself (app 0.2.1, docs/09-android.md §2): a phone that
 * already joined K1000 from the camera's Wi-Fi QR code needs no scan in the app. On the Connect
 * screen, when a Wi-Fi network on the K1000 subnet reaches the box over the pinned TLS, the app
 * reads {@code GET https://10.42.0.1/api/wifi} through that network:
 * {@code {"v":1,"ssid":"K1000","psk":"…","band":"…","box":"<build id>"}}. The box serves it only on
 * the AP's HTTPS listener, to AP clients: whoever reaches it already knows the password; the pin
 * proves to the app that it is the real box.
 *
 * <p>This class is the contract's pure part (JVM-tested, BoxWifiTest): the URLs, the answer's
 * validation, what each answer means, when to ask, and how often. The network side is
 * {@link BoxWifiProbe}. Credentials come only from here: never over plain HTTP, never from another
 * host, never from an intent.
 */
final class BoxWifi {

    /** The contract's path, on the box's HTTPS listener only. */
    static final String PATH = "/api/wifi";
    /** The contract's version; any other is refused (a newer box format this app does not know). */
    static final int VERSION = 1;
    /** An answer is a few dozen bytes: anything longer is not the box's. */
    static final int MAX_BODY_BYTES = 4096;
    static final int CONNECT_TIMEOUT_MS = 4000;
    static final int READ_TIMEOUT_MS = 4000;
    /** Asks on one network, per Connect screen, when the box does not answer at all. */
    static final int MAX_TRIES = 3;
    /** The informational fields (band, box) are shown in Diagnostics, cut to this. */
    static final int MAX_INFO_CHARS = 64;
    /** A refusal's reason, shown on the Connect screen, cut to this. */
    static final int MAX_WHY_CHARS = 120;

    /** What an answer from {@code /api/wifi} means. */
    enum Verdict {
        /** Valid credentials from the box: stored as a scan stores them, then the app joins (JOIN). */
        ADOPT,
        /** The box's Wi-Fi is the one this app already joins, and the app is on it: nothing changes. */
        SAME,
        /**
         * The box's Wi-Fi is the one this app already has, but the app is not on it (Android's
         * dialog found no K1000, or was cancelled): nothing is taken again (it would only bring the
         * same dialog back); the Connect screen points to the fallback box instead.
         */
        SAME_UNJOINED,
        /** 404: a box without {@code /api/wifi} (older than app 0.2.1's) or without a password file. */
        ABSENT,
        /**
         * Not the K1000 box: its certificate failed the pin (the network security config during
         * the handshake, or K1000Trust after it), or it was not HTTPS at all. Only a certificate
         * failure ({@link #certificateRefused}): a connection that drops during the handshake is
         * RETRY.
         */
        REFUSED,
        /** An answer the app cannot use (status, type, JSON, fields, size). */
        INVALID,
        /**
         * No answer (timeout, refused, closed or reset connection, also inside TLS; 429/503): asked
         * again a little later.
         */
        RETRY
    }

    /** One answer: a verdict, the credentials when there are some, and a reason for the log. */
    static final class Result {
        final Verdict verdict;
        final WifiQr.Credentials creds;
        final String box;
        final String band;
        final String why;

        private Result(Verdict verdict, WifiQr.Credentials creds, String box, String band, String why) {
            this.verdict = verdict;
            this.creds = creds;
            this.box = box;
            this.band = band;
            this.why = why;
        }

        static Result of(Verdict v, String why) {
            return new Result(v, null, "", "", info(why == null ? "" : why, MAX_WHY_CHARS));
        }

        Result as(Verdict v) {
            return new Result(v, creds, box, band, why);
        }

        @Override
        public String toString() {
            // Never the password.
            return verdict + (creds != null ? " ssid=" + creds.ssid + " box=" + box + " band=" + band : "")
                    + (why.isEmpty() ? "" : " (" + why + ")");
        }
    }

    /** The answer could not be used; the message says why (shown in the log and on the Connect screen). */
    static final class Invalid extends Exception {
        Invalid(String message) {
            super(message);
        }
    }

    /** What MainActivity does next on the Connect screen. */
    enum Step {
        /** Nothing (not the Connect screen, the fallback chosen, no K1000 network, already answered…). */
        NONE,
        /** Android 17+: the local network permission first (the request to the box needs it). */
        ASK_LOCAL_NETWORK,
        /** Ask the box for its Wi-Fi, over that network. */
        PROBE
    }

    private BoxWifi() {
    }

    /**
     * The URLs asked, in order: the box's address, then its name (served by the K1000 DNS) when the
     * address does not answer. HTTPS only, the box's two names only: never a bench host.
     */
    static List<String> urls() {
        return List.of("https://" + K1000Hosts.BOX_IP + PATH, "https://" + K1000Hosts.BOX_NAME + PATH);
    }

    /**
     * When to ask the box (pure: MainActivity passes its state).
     *
     * @param trusted       the bundled K1000 CA is usable
     * @param connectScreen the Connect screen is up (first run, or Change K1000 Wi-Fi), nothing over it
     * @param systemChosen  its "I joined K1000 in Android's Wi-Fi settings" box is ticked: the user's
     *                      own choice of the fallback, never overridden (no SYSTEM / JOIN loop)
     * @param foreground    the activity is started (a join is only accepted from the app on screen)
     * @param onK1000       a Wi-Fi network has an address on the K1000 subnet
     * @param due           that network has not given its final answer on this Connect screen
     * @param busy          a request is running
     * @param localNetwork  Android 16 or older, or the local network permission granted
     * @param localAsked    that permission was asked already on this Connect screen (asked once by
     *                      itself per Connect screen; after a refusal, the screen's line asks again
     *                      on a tap)
     */
    static Step next(boolean trusted, boolean connectScreen, boolean systemChosen, boolean foreground,
                     boolean onK1000, boolean due, boolean busy, boolean localNetwork, boolean localAsked) {
        if (!trusted || !connectScreen || systemChosen || !foreground || busy) return Step.NONE;
        if (!onK1000 || !due) return Step.NONE;
        if (!localNetwork) return localAsked ? Step.NONE : Step.ASK_LOCAL_NETWORK;
        return Step.PROBE;
    }

    /** A certificate failure named only in a message (a wrapper that kept the text, not the cause). */
    private static final Pattern CERT_MESSAGE = Pattern.compile(
            "certificate|certpath|trust anchor|chain validation|pin verification", Pattern.CASE_INSENSITIVE);

    /**
     * Whether a failed HTTPS request means "not the K1000 box" (REFUSED, final for that network,
     * the other name not tried): the peer could not be verified ({@code SSLPeerUnverifiedException},
     * e.g. a name the certificate does not carry), or its certificate chain was refused (a
     * {@code CertificateException} or {@code CertPathValidatorException} in the cause chain: the
     * network security config's pin during the handshake). Suppressed exceptions count (the HTTP
     * stack adds an earlier attempt's failure there), and so does a message that names a
     * certificate failure (a wrapper that kept the text, not the cause). Any other
     * {@code SSLException} is a connection that dropped during or after the handshake ("connection
     * closed", "Read error … Connection reset by peer": the edge of Wi-Fi range, the box's service
     * restarting): no answer (RETRY), like any other {@code IOException}.
     */
    static boolean certificateRefused(Throwable t) {
        return certificateRefused(t, 0);
    }

    private static boolean certificateRefused(Throwable t, int depth) {
        for (; t != null && depth < 16; t = t.getCause(), depth++) {
            if (t instanceof SSLPeerUnverifiedException || t instanceof CertificateException
                    || t instanceof CertPathValidatorException) {
                return true;
            }
            String m = t.getMessage();
            if (m != null && CERT_MESSAGE.matcher(m).find()) return true;
            for (Throwable s : t.getSuppressed()) {
                if (certificateRefused(s, depth + 1)) return true;
            }
        }
        return false;
    }

    /**
     * A failure's cause chain on one line, for the log ("SSLHandshakeException: … <- Certificate
     * Exception: …", suppressed ones in brackets), cut to {@code max} characters. Never a password:
     * the request carries none.
     */
    static String chain(Throwable t, int max) {
        StringBuilder sb = new StringBuilder();
        for (int depth = 0; t != null && depth < 8 && sb.length() < max; t = t.getCause(), depth++) {
            if (depth > 0) sb.append(" <- ");
            sb.append(t.getClass().getName()).append(": ").append(t.getMessage());
            for (Throwable s : t.getSuppressed()) {
                sb.append(" [suppressed ").append(s.getClass().getName()).append(": ").append(s.getMessage()).append(']');
            }
        }
        return sb.length() > max ? sb.substring(0, max) : sb.toString();
    }

    /**
     * What a box answer means. Only called after the TLS handshake passed the network security
     * config AND {@link K1000Trust} for that host.
     *
     * @param status      the HTTP status
     * @param contentType the Content-Type header (null when absent)
     * @param body        the body (empty unless 200), at most {@link #MAX_BODY_BYTES} + 1 bytes
     */
    static Result judge(int status, String contentType, byte[] body) {
        if (status == 404) return Result.of(Verdict.ABSENT, "no " + PATH + " on this box");
        if (status == 429 || status == 503) return Result.of(Verdict.RETRY, "HTTP " + status);
        if (status != 200) return Result.of(Verdict.INVALID, "HTTP " + status);
        try {
            if (!isJson(contentType)) throw new Invalid("not JSON (" + contentType + ")");
            return parse(body);
        } catch (Invalid e) {
            return Result.of(Verdict.INVALID, e.getMessage());
        }
    }

    /** {@code application/json}, parameters allowed (charset), nothing else. */
    static boolean isJson(String contentType) {
        if (contentType == null) return false;
        String t = contentType.split(";", 2)[0].trim().toLowerCase(Locale.ROOT);
        return t.equals("application/json");
    }

    /** Validates a 200 answer's body: strict UTF-8, one JSON object, v 1, a usable SSID and password. */
    static Result parse(byte[] body) throws Invalid {
        if (body == null || body.length == 0) throw new Invalid("empty answer");
        if (body.length > MAX_BODY_BYTES) throw new Invalid("answer too long");
        String text;
        try {
            text = StandardCharsets.UTF_8.newDecoder()
                    .onMalformedInput(CodingErrorAction.REPORT)
                    .onUnmappableCharacter(CodingErrorAction.REPORT)
                    .decode(ByteBuffer.wrap(body)).toString();
        } catch (CharacterCodingException e) {
            throw new Invalid("not UTF-8");
        }
        Object o;
        try {
            o = MiniJson.parse(text, 4);
        } catch (MiniJson.ParseException e) {
            throw new Invalid("bad JSON: " + e.getMessage());
        }
        if (!(o instanceof Map)) throw new Invalid("not a JSON object");
        Map<?, ?> m = (Map<?, ?>) o;
        Object v = m.get("v");
        if (!(v instanceof Double) || (Double) v != VERSION) throw new Invalid("version " + MiniJson.write(v) + ", not " + VERSION);
        Object ssid = m.get("ssid");
        Object psk = m.get("psk");
        if (!(ssid instanceof String)) throw new Invalid("no ssid");
        if (!(psk instanceof String)) throw new Invalid("no psk");
        String s = (String) ssid;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c < 0x20 || c == 0x7f || (c >= 0x80 && c < 0xa0)) throw new Invalid("the Wi-Fi name has a control character");
        }
        WifiQr.Credentials c;
        try {
            // The same rules as a scanned or typed one: 1..32 bytes of name, 8..63 printable ASCII.
            c = WifiQr.of(s, (String) psk, false);
        } catch (WifiQr.InvalidException e) {
            throw new Invalid(e.getMessage());
        }
        return new Result(Verdict.ADOPT, c, info(m.get("box")), info(m.get("band")), "");
    }

    /** An informational string field: printable, cut short; "" when absent or not a string. */
    static String info(Object v) {
        return info(v, MAX_INFO_CHARS);
    }

    static String info(Object v, int max) {
        if (!(v instanceof String)) return "";
        StringBuilder sb = new StringBuilder();
        for (char c : ((String) v).toCharArray()) {
            if (sb.length() >= max) break;
            sb.append(c < 0x20 || c == 0x7f ? '?' : c);
        }
        return sb.toString();
    }

    /**
     * An ADOPT answer against what the app already stores: the same Wi-Fi in JOIN mode changes
     * nothing (a Connect screen opened while the app is on K1000 itself must not re-join it).
     * When the app is not on it ({@code joined} false: Android's dialog found no K1000, or was
     * cancelled), it is SAME_UNJOINED: still nothing taken, but the Connect screen must not claim
     * the app is on K1000.
     *
     * @param joined the app's own K1000 link is up
     */
    static Result compare(Result r, String storedMode, String storedSsid, String storedPsk, boolean joined) {
        if (r.verdict != Verdict.ADOPT || r.creds == null) return r;
        boolean same = "JOIN".equals(storedMode) && Objects.equals(r.creds.ssid, storedSsid)
                && Objects.equals(r.creds.passphrase, storedPsk);
        if (!same) return r;
        return r.as(joined ? Verdict.SAME : Verdict.SAME_UNJOINED);
    }

    /**
     * How often one network is asked on one Connect screen: until it gives a final answer
     * (ADOPT, SAME, SAME_UNJOINED, ABSENT, REFUSED, INVALID), or {@link #MAX_TRIES} times when it
     * does not answer (RETRY, 2 then 4 s apart). A new network, or the Connect screen shown again,
     * starts over.
     *
     * <p>It also knows the one running request, and on which Connect screen it was asked: a
     * request still running when the screen closes or is shown again ({@link #reset}) keeps the
     * next one waiting ({@link #busy}) until it ends, and its answer then does not count
     * ({@link #ended} false): MainActivity drops it and asks again for the screen now up, with
     * that screen's own count. (Review 2026-10-13: such an answer used to be dropped with nothing
     * asking again until the Wi-Fi changed, and, when the screen was shown again over itself, a
     * no answer counted against the new count made it final after one try.)
     */
    static final class Tries {
        private Object network;
        private int tries;
        private boolean done;
        /** The Connect screen's number, bumped by {@link #reset}. */
        private int screen;
        /** The running request's token (0: none), and the Connect screen it was asked on. */
        private int running;
        private int runningScreen;
        private int lastToken;

        /**
         * The Connect screen was shown again, or closed: every network may be asked again, and the
         * answer of a request still running will not count.
         */
        void reset() {
            screen++;
            network = null;
            tries = 0;
            done = false;
        }

        /** A request is running (the next one waits for it to end). */
        boolean busy() {
            return running != 0;
        }

        /** True when {@code n} may be asked now (another network than last time starts over). */
        boolean due(Object n) {
            if (n == null) return false;
            if (!n.equals(network)) {
                network = n;
                tries = 0;
                done = false;
            }
            return !done && tries < MAX_TRIES;
        }

        /** A request to {@code n} starts: its token, for {@link #ended}. */
        int started(Object n) {
            due(n);
            tries++;
            lastToken = lastToken == Integer.MAX_VALUE ? 1 : lastToken + 1;
            running = lastToken;
            runningScreen = screen;
            return running;
        }

        /**
         * The request {@code token} ended (its answer arrived). True when that answer counts: it
         * was asked on the Connect screen now up. False when the screen was closed or shown again
         * since: the answer is dropped, and the box is asked again ({@link #due} is true again).
         */
        boolean ended(int token) {
            if (token == 0 || token != running) return false;
            running = 0;
            return runningScreen == screen;
        }

        /**
         * The answer of {@code n}: the delay before asking again (ms), or -1 when it is final or
         * the tries are used up. An answer for another network than the current one changes nothing.
         */
        long answered(Object n, Verdict v) {
            if (n == null || !n.equals(network)) return -1;
            if (v != Verdict.RETRY) {
                done = true;
                return -1;
            }
            if (tries >= MAX_TRIES) {
                done = true;
                return -1;
            }
            return 2000L * tries;
        }

        int tries() {
            return tries;
        }
    }
}
