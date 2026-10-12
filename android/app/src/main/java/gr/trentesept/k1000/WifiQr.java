// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

/**
 * The K1000 Wi-Fi credentials: from the box screen's Wi-Fi QR code ({@code WIFI:T:WPA;S:K1000;P:…;;},
 * the ZXing / Android "Share Wi-Fi" format) or typed by hand. Pure Java, so it is unit-tested on
 * the JVM.
 *
 * <p>The box writes the code with {@code box/k1000/ui/qr.py}: {@code \ ; , " :} are escaped with a
 * backslash, fields come in any order. K1000 is a WPA2-PSK network, so a code for an open, WEP or
 * WPA3-only network is refused with a reason the user can read.
 */
public final class WifiQr {

    /** What {@code WifiNetworkSpecifier.Builder} accepts: 1..32 bytes of SSID. */
    public static final int SSID_MAX_BYTES = 32;

    /** The box's default SSID (D-007), offered when the password is typed by hand. */
    public static final String DEFAULT_SSID = "K1000";

    /** Wi-Fi credentials for a WPA2-PSK network. */
    public static final class Credentials {
        public final String ssid;
        public final String passphrase;
        public final boolean hidden;

        public Credentials(String ssid, String passphrase, boolean hidden) {
            this.ssid = ssid;
            this.passphrase = passphrase;
            this.hidden = hidden;
        }
    }

    /** A code or a typed value K1000 cannot use; the message is shown to the user as is. */
    public static final class InvalidException extends Exception {
        public InvalidException(String message) {
            super(message);
        }
    }

    private WifiQr() {
    }

    /** Parses a scanned QR payload. */
    public static Credentials parse(String payload) throws InvalidException {
        if (payload == null) throw new InvalidException("Empty QR code.");
        String text = payload.trim();
        if (!text.regionMatches(true, 0, "WIFI:", 0, 5)) {
            throw new InvalidException("This is not a Wi-Fi QR code. Scan the Wi-Fi code on the K1000 screen (SETUP › Phone).");
        }
        Map<String, String> fields = fields(text.substring(5));
        String type = fields.getOrDefault("T", "").toUpperCase(Locale.ROOT);
        String ssid = fields.get("S");
        String pass = fields.get("P");
        boolean hidden = "true".equalsIgnoreCase(fields.getOrDefault("H", "false"));

        if (type.isEmpty() && pass != null && !pass.isEmpty()) type = "WPA";   // T is optional
        if (!type.equals("WPA") && !type.equals("WPA2")) {
            String what = type.isEmpty() || type.equals("NOPASS") ? "an open network" : type;
            throw new InvalidException("This Wi-Fi code is for " + what + "; K1000 uses WPA2 with a password.");
        }
        return of(ssid, pass, hidden);
    }

    /** Validates typed (or scanned) credentials; the passphrase is used exactly as given. */
    public static Credentials of(String ssid, String passphrase, boolean hidden) throws InvalidException {
        String s = ssid == null ? "" : ssid;
        if (s.isEmpty()) throw new InvalidException("The Wi-Fi name is missing.");
        if (s.getBytes(StandardCharsets.UTF_8).length > SSID_MAX_BYTES) {
            throw new InvalidException("The Wi-Fi name is longer than 32 bytes.");
        }
        checkPassphrase(passphrase);
        return new Credentials(s, passphrase, hidden);
    }

    /** WPA2 passphrase rules ({@code setWpa2Passphrase}): 8 to 63 printable ASCII characters. */
    public static void checkPassphrase(String p) throws InvalidException {
        if (p == null || p.isEmpty()) throw new InvalidException("The Wi-Fi password is missing.");
        if (p.length() < 8) throw new InvalidException("The Wi-Fi password is too short (8 characters at least).");
        if (p.length() > 63) throw new InvalidException("The Wi-Fi password is too long (63 characters at most).");
        for (int i = 0; i < p.length(); i++) {
            char c = p.charAt(i);
            if (c < 0x20 || c > 0x7e) {
                throw new InvalidException("The Wi-Fi password may only use plain letters, digits and symbols.");
            }
        }
    }

    /** Splits {@code K:value;K:value;;} honouring backslash escapes; later duplicates are ignored. */
    static Map<String, String> fields(String body) throws InvalidException {
        Map<String, String> out = new HashMap<>();
        StringBuilder key = new StringBuilder();
        StringBuilder val = new StringBuilder();
        boolean inValue = false;
        boolean quotedStart = false;
        boolean lastWasQuote = false;
        int n = body.length();
        for (int i = 0; i < n; i++) {
            char c = body.charAt(i);
            if (c == '\\') {
                if (i + 1 >= n) throw new InvalidException("The Wi-Fi code ends with a stray backslash.");
                char e = body.charAt(++i);
                (inValue ? val : key).append(e);
                lastWasQuote = false;
                continue;
            }
            if (!inValue) {
                if (c == ':') {
                    inValue = true;
                    quotedStart = false;
                    lastWasQuote = false;
                } else if (c == ';') {
                    if (key.length() == 0) break;   // ";;" = end of the code
                    throw new InvalidException("The Wi-Fi code is malformed (field \"" + key + "\" has no value).");
                } else {
                    key.append(c);
                }
                continue;
            }
            if (c == ';') {
                String k = key.toString().trim().toUpperCase(Locale.ROOT);
                String v = val.toString();
                // An unescaped "…" around the whole value is ZXing's quoted form: drop the quotes.
                if (quotedStart && lastWasQuote && v.length() >= 2) v = v.substring(1, v.length() - 1);
                out.putIfAbsent(k, v);
                key.setLength(0);
                val.setLength(0);
                inValue = false;
                continue;
            }
            if (c == '"') {
                if (val.length() == 0) quotedStart = true;
                lastWasQuote = true;
            } else {
                lastWasQuote = false;
            }
            val.append(c);
        }
        if (inValue) {   // tolerate a code without the final ";;"
            String k = key.toString().trim().toUpperCase(Locale.ROOT);
            String v = val.toString();
            if (quotedStart && lastWasQuote && v.length() >= 2) v = v.substring(1, v.length() - 1);
            out.putIfAbsent(k, v);
        }
        return out;
    }
}
