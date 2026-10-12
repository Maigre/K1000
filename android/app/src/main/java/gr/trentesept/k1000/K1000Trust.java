// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import java.io.IOException;
import java.io.InputStream;
import java.security.cert.CertPath;
import java.security.cert.CertPathValidator;
import java.security.cert.CertificateException;
import java.security.cert.CertificateFactory;
import java.security.cert.PKIXParameters;
import java.security.cert.TrustAnchor;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Collections;
import java.util.Date;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * Decides whether a certificate presented by "the box" is really the K1000 box's: an explicit
 * X.509 check against the ONE bundled K1000 CA, for the K1000 host names only, with the CA's
 * name constraints enforced. Nothing else is trusted: not the system CAs, not user CAs.
 *
 * <p>The first line of defence is the app's Network Security Config (res/xml), which WebView
 * honours (docs/09-android.md). This class backs the WebView's {@code onReceivedSslError}: it
 * only ever turns an error into "proceed" when every check below passes, so it can only be
 * stricter than Android's own validation. Pure Java (unit-tested on the JVM with the real CA,
 * the real leaf, and throwaway test CAs).
 *
 * <p>Checks, all required:
 * <ol>
 *   <li>the host is one of the K1000 hosts;</li>
 *   <li>the CA is a CA, is valid now, and carries a critical Name Constraints extension;</li>
 *   <li>the leaf is valid now, is not a CA, was issued by and is signed by the bundled CA;</li>
 *   <li>the leaf may serve TLS (key usage digitalSignature, extended key usage serverAuth);</li>
 *   <li>the leaf names the host in its subjectAltName (IP or DNS; no wildcards);</li>
 *   <li>every name in the leaf (subjectAltName, and a host-like subject CN) lies inside the CA's
 *       permitted subtrees and outside its excluded ones;</li>
 *   <li>and, as a second opinion, the platform's PKIX validator accepts the path with the bundled
 *       CA as the only trust anchor.</li>
 * </ol>
 *
 * <p>The name-constraint check is done here because the Java/Android PKIX validator does not
 * apply a trust anchor's own Name Constraints extension (and refuses constraints passed through
 * {@code TrustAnchor}): K1000TrustTest shows it accepting out-of-constraint leaves on the JVM.
 */
public final class K1000Trust {

    static final String OID_NAME_CONSTRAINTS = "2.5.29.30";
    static final String OID_SERVER_AUTH = "1.3.6.1.5.5.7.3.1";
    static final String OID_ANY_EKU = "2.5.29.37.0";
    private static final int SAN_DNS = 2;
    private static final int SAN_IP = 7;
    private static final Pattern IPV4 = Pattern.compile("^(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)(\\.(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)){3}$");
    private static final Pattern DNS_NAME = Pattern.compile("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$");

    private final X509Certificate ca;
    private final Set<String> hosts;
    private final NameConstraints constraints;

    /**
     * @param ca    the bundled K1000 CA certificate
     * @param hosts the host names this CA may vouch for in this app (K1000Hosts.HOSTS)
     * @throws CertificateException when the CA itself is unusable (not a CA, no name constraints)
     */
    public K1000Trust(X509Certificate ca, Collection<String> hosts) throws CertificateException {
        if (ca == null) throw new CertificateException("no K1000 CA");
        if (ca.getBasicConstraints() < 0) throw new CertificateException("the bundled K1000 CA is not a CA certificate");
        Set<String> critical = ca.getCriticalExtensionOIDs();
        if (critical == null || !critical.contains(OID_NAME_CONSTRAINTS)) {
            throw new CertificateException("the bundled K1000 CA has no critical name constraints");
        }
        this.ca = ca;
        this.constraints = NameConstraints.parse(unwrapOctetString(ca.getExtensionValue(OID_NAME_CONSTRAINTS)));
        if (constraints.permittedDns.isEmpty() && constraints.permittedIp.isEmpty()) {
            throw new CertificateException("the bundled K1000 CA permits no DNS or IP names");
        }
        Set<String> h = new LinkedHashSet<>();
        for (String s : hosts) h.add(K1000Hosts.normalizeHost(s));
        this.hosts = Collections.unmodifiableSet(h);
    }

    /** Reads one PEM or DER X.509 certificate. */
    public static X509Certificate readCertificate(InputStream in) throws CertificateException, IOException {
        try (InputStream s = in) {
            return (X509Certificate) CertificateFactory.getInstance("X.509").generateCertificate(s);
        }
    }

    public X509Certificate ca() {
        return ca;
    }

    /**
     * Verifies the server's certificate (the leaf first; anything after it is ignored: only the
     * bundled CA is ever an anchor) for {@code host} at time {@code now}.
     *
     * @throws CertificateException with a short reason when the certificate must be refused
     */
    public void verify(X509Certificate[] chain, String host, Date now) throws CertificateException {
        if (chain == null || chain.length == 0 || chain[0] == null) throw new CertificateException("no server certificate");
        X509Certificate leaf = chain[0];
        String h = K1000Hosts.normalizeHost(host);
        if (!hosts.contains(h)) throw new CertificateException("host " + h + " is not a K1000 host");

        ca.checkValidity(now);
        leaf.checkValidity(now);
        if (!leaf.getIssuerX500Principal().equals(ca.getSubjectX500Principal())) {
            throw new CertificateException("not issued by the K1000 CA");
        }
        try {
            leaf.verify(ca.getPublicKey());
        } catch (Exception e) {
            throw new CertificateException("not signed by the K1000 CA");
        }
        if (leaf.getBasicConstraints() >= 0) throw new CertificateException("the server certificate is a CA");
        boolean[] ku = leaf.getKeyUsage();
        if (ku != null && !(ku.length > 0 && ku[0])) throw new CertificateException("key usage forbids TLS");
        List<String> eku = leaf.getExtendedKeyUsage();
        if (eku != null && !eku.contains(OID_SERVER_AUTH) && !eku.contains(OID_ANY_EKU)) {
            throw new CertificateException("extended key usage forbids TLS server use");
        }

        List<String> dnsNames = new ArrayList<>();
        List<byte[]> ipAddrs = new ArrayList<>();
        Collection<List<?>> sans = leaf.getSubjectAlternativeNames();
        if (sans != null) {
            for (List<?> san : sans) {
                int type = (Integer) san.get(0);
                Object value = san.get(1);
                if (type == SAN_DNS) {
                    dnsNames.add(((String) value).toLowerCase(Locale.ROOT));
                } else if (type == SAN_IP) {
                    byte[] a = ipv4Bytes((String) value);
                    if (a == null) throw new CertificateException("unsupported IP name " + value);
                    ipAddrs.add(a);
                } else {
                    throw new CertificateException("unexpected name type " + type + " in the server certificate");
                }
            }
        }
        if (dnsNames.isEmpty() && ipAddrs.isEmpty()) throw new CertificateException("the server certificate names no host");

        // The host must be named (no wildcards, no common-name fallback).
        byte[] hostIp = ipv4Bytes(h);
        boolean named = false;
        if (hostIp != null) {
            for (byte[] a : ipAddrs) named |= Arrays.equals(a, hostIp);
        } else {
            for (String d : dnsNames) named |= d.equals(h);
        }
        if (!named) throw new CertificateException("the server certificate does not name " + h);

        // Every name must sit inside the CA's constraints (including a host-like subject CN).
        for (String d : dnsNames) constraints.checkDns(d);
        for (byte[] a : ipAddrs) constraints.checkIp(a);
        String cn = commonName(leaf);
        if (cn != null) {
            String c = cn.toLowerCase(Locale.ROOT);
            byte[] cnIp = ipv4Bytes(c);
            if (cnIp != null) constraints.checkIp(cnIp);
            else if (c.contains(".") && DNS_NAME.matcher(c).matches()) constraints.checkDns(c);
        }

        // Second opinion: the platform's PKIX validator with the bundled CA as the only anchor
        // (signature, validity, CA basic constraints and key usage; not the anchor's own name
        // constraints, which it ignores: they were checked above).
        try {
            CertificateFactory cf = CertificateFactory.getInstance("X.509");
            CertPath path = cf.generateCertPath(Collections.singletonList(leaf));
            PKIXParameters params = new PKIXParameters(Collections.singleton(new TrustAnchor(ca, null)));
            params.setRevocationEnabled(false);
            params.setDate(now);
            CertPathValidator.getInstance("PKIX").validate(path, params);
        } catch (Exception e) {
            throw new CertificateException("PKIX validation failed: " + e.getMessage());
        }
    }

    // ------------------------------------------------------------------------------------------

    static byte[] ipv4Bytes(String s) {
        if (s == null || !IPV4.matcher(s).matches()) return null;
        String[] p = s.split("\\.");
        byte[] out = new byte[4];
        for (int i = 0; i < 4; i++) out[i] = (byte) Integer.parseInt(p[i]);
        return out;
    }

    private static String commonName(X509Certificate cert) {
        // RFC 2253 form, e.g. "CN=k1000.home.arpa,O=…"; the CN of a server leaf has no commas.
        String dn = cert.getSubjectX500Principal().getName();
        for (String part : dn.split(",")) {
            String t = part.trim();
            if (t.regionMatches(true, 0, "CN=", 0, 3)) return t.substring(3);
        }
        return null;
    }

    static byte[] unwrapOctetString(byte[] ext) throws CertificateException {
        if (ext == null) throw new CertificateException("missing extension");
        Der d = new Der(ext, 0, ext.length);
        Der.Tlv t = d.next();
        if (t.tag != 0x04 || d.hasMore()) throw new CertificateException("malformed extension");
        return Arrays.copyOfRange(ext, t.start, t.end);
    }

    /** The CA's NameConstraints, DNS and IPv4 subtrees (RFC 5280 §4.2.1.10). */
    static final class NameConstraints {
        final List<String> permittedDns = new ArrayList<>();
        final List<byte[]> permittedIp = new ArrayList<>();   // 8 bytes: address + mask
        final List<String> excludedDns = new ArrayList<>();
        final List<byte[]> excludedIp = new ArrayList<>();

        static NameConstraints parse(byte[] der) throws CertificateException {
            NameConstraints nc = new NameConstraints();
            Der outer = new Der(der, 0, der.length);
            Der.Tlv seq = outer.next();
            if (seq.tag != 0x30 || outer.hasMore()) throw new CertificateException("malformed name constraints");
            Der subtrees = new Der(der, seq.start, seq.end);
            while (subtrees.hasMore()) {
                Der.Tlv st = subtrees.next();
                boolean permitted;
                if (st.tag == 0xA0) permitted = true;
                else if (st.tag == 0xA1) permitted = false;
                else throw new CertificateException("malformed name constraints");
                Der list = new Der(der, st.start, st.end);
                while (list.hasMore()) {
                    Der.Tlv gs = list.next();
                    if (gs.tag != 0x30) throw new CertificateException("malformed general subtree");
                    Der fields = new Der(der, gs.start, gs.end);
                    Der.Tlv base = fields.next();
                    if (fields.hasMore()) throw new CertificateException("name constraints with minimum/maximum are not supported");
                    byte[] v = Arrays.copyOfRange(der, base.start, base.end);
                    if (base.tag == 0x82) {   // dNSName
                        String name = new String(v, java.nio.charset.StandardCharsets.US_ASCII).toLowerCase(Locale.ROOT);
                        (permitted ? nc.permittedDns : nc.excludedDns).add(name);
                    } else if (base.tag == 0x87) {   // iPAddress
                        if (v.length == 8) (permitted ? nc.permittedIp : nc.excludedIp).add(v);
                        else if (v.length != 32) throw new CertificateException("malformed IP name constraint");
                        // IPv6 subtrees (32 bytes) are kept out: the K1000 leaf has no IPv6 name,
                        // and any IPv6 name in a leaf is refused above.
                    }
                    // Other name types (directoryName, email, URI): no leaf name of those types
                    // is accepted anyway.
                }
            }
            return nc;
        }

        void checkDns(String name) throws CertificateException {
            for (String ex : excludedDns) {
                if (dnsWithin(name, ex)) throw new CertificateException(name + " is excluded by the K1000 CA");
            }
            for (String p : permittedDns) {
                if (dnsWithin(name, p)) return;
            }
            throw new CertificateException(name + " is outside the K1000 CA's names");
        }

        void checkIp(byte[] addr) throws CertificateException {
            for (byte[] ex : excludedIp) {
                if (ipWithin(addr, ex)) throw new CertificateException(ipText(addr) + " is excluded by the K1000 CA");
            }
            for (byte[] p : permittedIp) {
                if (ipWithin(addr, p)) return;
            }
            throw new CertificateException(ipText(addr) + " is outside the K1000 CA's addresses");
        }

        static boolean dnsWithin(String name, String constraint) {
            if (constraint.isEmpty()) return true;
            if (constraint.startsWith(".")) return name.endsWith(constraint);
            return name.equals(constraint) || name.endsWith("." + constraint);
        }

        static boolean ipWithin(byte[] addr, byte[] subtree) {
            if (addr.length * 2 != subtree.length) return false;
            for (int i = 0; i < addr.length; i++) {
                byte mask = subtree[addr.length + i];
                if ((addr[i] & mask) != (subtree[i] & mask)) return false;
            }
            return true;
        }

        static String ipText(byte[] a) {
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < a.length; i++) {
                if (i > 0) sb.append('.');
                sb.append(a[i] & 0xff);
            }
            return sb.toString();
        }
    }

    /** A minimal DER reader: definite lengths, single-byte tags (all NameConstraints needs). */
    static final class Der {
        static final class Tlv {
            final int tag;
            final int start;
            final int end;

            Tlv(int tag, int start, int end) {
                this.tag = tag;
                this.start = start;
                this.end = end;
            }
        }

        private final byte[] b;
        private int pos;
        private final int end;

        Der(byte[] b, int start, int end) {
            this.b = b;
            this.pos = start;
            this.end = end;
        }

        boolean hasMore() {
            return pos < end;
        }

        Tlv next() throws CertificateException {
            if (pos + 2 > end) throw new CertificateException("truncated DER");
            int tag = b[pos++] & 0xff;
            if ((tag & 0x1f) == 0x1f) throw new CertificateException("multi-byte DER tags are not supported");
            int len = b[pos++] & 0xff;
            if (len >= 0x80) {
                int n = len & 0x7f;
                if (n == 0 || n > 3) throw new CertificateException("unsupported DER length");
                len = 0;
                for (int i = 0; i < n; i++) {
                    if (pos >= end) throw new CertificateException("truncated DER");
                    len = (len << 8) | (b[pos++] & 0xff);
                }
            }
            if (len < 0 || pos + len > end) throw new CertificateException("truncated DER");
            Tlv t = new Tlv(tag, pos, pos + len);
            pos += len;
            return t;
        }
    }
}
