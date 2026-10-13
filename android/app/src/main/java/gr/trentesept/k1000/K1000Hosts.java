// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import java.net.URI;
import java.util.List;
import java.util.Locale;
import java.util.regex.Pattern;

/**
 * The only places this app talks to: the box's HTTPS page at 10.42.0.1 (D-007), and its DNS name
 * on the K1000 network as a fallback (a bench build adds one development host, never a release).
 * Everything else is blocked inside the WebView or handed to the phone's browser. Pure Java
 * (unit-tested on the JVM).
 */
public final class K1000Hosts {

    public static final String BOX_IP = "10.42.0.1";
    public static final String BOX_NAME = "k1000.home.arpa";
    public static final List<String> HOSTS = List.of(BOX_IP, BOX_NAME);

    public static final String URL_PRIMARY = "https://" + BOX_IP + "/";
    public static final String URL_FALLBACK = "https://" + BOX_NAME + "/";

    /** The phone page v2 (app 0.2's default, DESIGN §4.1) and v1 (the escape hatch: THIS PHONE). */
    public static final String PATH_V2 = "/v2/";
    public static final String PATH_V1 = "/";

    /** The K1000 Wi-Fi's IPv4 subnet (the box's AP hands out 10.42.0.x, D-007). */
    public static final String SUBNET = "10.42.0.0/24";

    private static final Pattern HOST_NAME = Pattern.compile(
            "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$");

    /**
     * Bench builds only (BuildConfig.BENCH_HOST): a development host on Thomas's LAN that serves
     * the same page, trusted with its own CA by the bench build's network security config. ""
     * (release and smoke builds) = none: only the two box names exist.
     */
    private static volatile String benchHost = "";

    private K1000Hosts() {
    }

    /** Sets the bench host ("" = none). Only MainActivity of a bench build calls this. */
    static void setBenchHost(String host) {
        String h = normalizeHost(host);
        if (!h.isEmpty() && (!HOST_NAME.matcher(h).matches() || HOSTS.contains(h))) {
            throw new IllegalArgumentException("not a usable bench host: " + host);
        }
        benchHost = h;
    }

    static String benchHost() {
        return benchHost;
    }

    /** The host the page is loaded from first: the box's address (a bench build: the bench host). */
    static String primaryHost() {
        String b = benchHost;
        return b.isEmpty() ? BOX_IP : b;
    }

    /** The host tried when the first does not answer: the box's name (a bench build: the box's address). */
    static String fallbackHost() {
        return benchHost.isEmpty() ? BOX_NAME : BOX_IP;
    }

    /** {@code https://<host><path>}, path being {@link #PATH_V1} or {@link #PATH_V2}. */
    static String pageUrl(String host, String path) {
        return "https://" + host + path;
    }

    /**
     * The subnets the app follows in SYSTEM mode (a Wi-Fi joined in Android's settings): the K1000
     * subnet, and in a bench build the bench host's /24 too (an IPv4 bench host only).
     */
    static List<String> linkSubnets() {
        String b = benchHost;
        byte[] ip = K1000Trust.ipv4Bytes(b);
        if (ip == null) return List.of(SUBNET);
        return List.of(SUBNET, (ip[0] & 0xff) + "." + (ip[1] & 0xff) + "." + (ip[2] & 0xff) + ".0/24");
    }

    /**
     * The origin ("https://host", no port, no slash) of a box URL: the only target the app posts
     * the bridge's port to. Null for anything that is not a box URL.
     */
    static String originOf(String url) {
        if (!isBoxUrl(url)) return null;
        return "https://" + hostOf(url);
    }

    /** The path of a URL ("/" when empty), or "" when it is not a URL. */
    static String pathOf(String url) {
        URI u = parse(url);
        if (u == null) return "";
        String p = u.getRawPath();
        return p == null || p.isEmpty() ? "/" : p;
    }

    /**
     * True when {@code address} (raw bytes, as {@code InetAddress.getAddress()} gives them) is an
     * IPv4 address inside {@code cidr} ("a.b.c.d/len"). IPv6 and malformed input give false.
     */
    public static boolean inSubnet(byte[] address, String cidr) {
        int[] net = parseCidr(cidr);
        if (net == null || address == null || address.length != 4) return false;
        int a = ((address[0] & 0xff) << 24) | ((address[1] & 0xff) << 16) | ((address[2] & 0xff) << 8) | (address[3] & 0xff);
        int mask = net[1] == 0 ? 0 : -1 << (32 - net[1]);
        return (a & mask) == (net[0] & mask);
    }

    /** "a.b.c.d/len" as {address, prefix length}, or null when it is not an IPv4 CIDR. */
    static int[] parseCidr(String cidr) {
        if (cidr == null) return null;
        String[] parts = cidr.trim().split("/", -1);
        if (parts.length != 2) return null;
        String[] octets = parts[0].split("\\.", -1);
        if (octets.length != 4) return null;
        try {
            int a = 0;
            for (String o : octets) {
                if (o.isEmpty() || o.length() > 3) return null;
                int v = Integer.parseInt(o);
                if (v < 0 || v > 255) return null;
                a = (a << 8) | v;
            }
            int len = Integer.parseInt(parts[1]);
            if (parts[1].isEmpty() || len < 0 || len > 32) return null;
            return new int[] {a, len};
        } catch (NumberFormatException e) {
            return null;
        }
    }

    /** Lower-case host without brackets or a trailing dot, or "" when there is none. */
    public static String normalizeHost(String host) {
        if (host == null) return "";
        String h = host.trim().toLowerCase(Locale.ROOT);
        if (h.startsWith("[") && h.endsWith("]")) h = h.substring(1, h.length() - 1);
        while (h.endsWith(".")) h = h.substring(0, h.length() - 1);
        return h;
    }

    public static boolean isBoxHost(String host) {
        String h = normalizeHost(host);
        String b = benchHost;
        return HOSTS.contains(h) || (!b.isEmpty() && b.equals(h));
    }

    /** True for https://10.42.0.1[:443]/… and https://k1000.home.arpa[:443]/… only. */
    public static boolean isBoxUrl(String url) {
        URI u = parse(url);
        if (u == null || u.getRawUserInfo() != null) return false;
        if (!"https".equalsIgnoreCase(u.getScheme())) return false;
        if (u.getPort() != -1 && u.getPort() != 443) return false;
        return isBoxHost(u.getHost());
    }

    /**
     * True when a WebView permission request comes from the box's page. WebView gives the origin
     * as a URL ("https://10.42.0.1/"); it must be exactly a box origin, with no path beyond "/".
     */
    public static boolean isBoxOrigin(String origin) {
        URI u = parse(origin);
        if (u == null) return false;
        String path = u.getRawPath();
        if (path != null && !path.isEmpty() && !path.equals("/")) return false;
        if (u.getRawQuery() != null || u.getRawFragment() != null) return false;
        return isBoxUrl(origin);
    }

    /** The host of a URL, normalized, or "" when it has none. */
    public static String hostOf(String url) {
        URI u = parse(url);
        return u == null ? "" : normalizeHost(u.getHost());
    }

    private static URI parse(String s) {
        if (s == null || s.isEmpty()) return null;
        try {
            return new URI(s);
        } catch (Exception e) {
            return null;
        }
    }
}
