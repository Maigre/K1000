// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;

import java.io.FileInputStream;
import java.io.InputStream;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.time.Instant;
import java.util.Date;

import org.junit.Test;

/**
 * The explicit K1000 TLS check, against the REAL bundled CA and the REAL box leaf (both public
 * certificates), a look-alike CA with the same name, and throwaway name-constrained test CAs
 * (tools/make_test_certs.sh).
 */
public class K1000TrustTest {

    // The K1000 CA is valid from 2026-10-12 15:18 UTC, its leaf from 2026-10-11.
    private static final Date NOW = Date.from(Instant.parse("2026-10-13T12:00:00Z"));

    private static X509Certificate cert(String name) throws Exception {
        InputStream in = K1000TrustTest.class.getResourceAsStream("/certs/" + name);
        assertNotNull("missing test certificate " + name, in);
        return K1000Trust.readCertificate(in);
    }

    /** The CA the app actually bundles (res/raw), read from the source tree. */
    private static X509Certificate bundledCa() throws Exception {
        return K1000Trust.readCertificate(new FileInputStream("src/main/res/raw/k1000_ca.pem"));
    }

    private static K1000Trust real() throws Exception {
        return new K1000Trust(bundledCa(), K1000Hosts.HOSTS);
    }

    private static K1000Trust test() throws Exception {
        return new K1000Trust(cert("testca.pem"), K1000Hosts.HOSTS);
    }

    private static String refused(K1000Trust t, String leaf, String host, Date now) throws Exception {
        try {
            t.verify(new X509Certificate[] {cert(leaf)}, host, now);
        } catch (CertificateException e) {
            return e.getMessage();
        }
        fail(leaf + " accepted for " + host);
        return null;
    }

    @Test
    public void realLeafAcceptedForBothBoxNames() throws Exception {
        K1000Trust t = real();
        t.verify(new X509Certificate[] {cert("k1000-leaf.pem")}, "10.42.0.1", NOW);
        t.verify(new X509Certificate[] {cert("k1000-leaf.pem")}, "k1000.home.arpa", NOW);
        t.verify(new X509Certificate[] {cert("k1000-leaf.pem"), bundledCa()}, "K1000.home.arpa.", NOW);
    }

    @Test
    public void realLeafRefusedForOtherHostsAndOutsideItsDates() throws Exception {
        K1000Trust t = real();
        assertTrue(refused(t, "k1000-leaf.pem", "10.42.0.2", NOW).contains("not a K1000 host"));
        assertTrue(refused(t, "k1000-leaf.pem", "example.com", NOW).contains("not a K1000 host"));
        // The leaf expires on 2029-01-14 (docs/06-phone.md); before 2026-10-11 it is not yet valid.
        refused(t, "k1000-leaf.pem", "10.42.0.1", Date.from(Instant.parse("2029-02-01T00:00:00Z")));
        refused(t, "k1000-leaf.pem", "10.42.0.1", Date.from(Instant.parse("2026-10-01T00:00:00Z")));
    }

    @Test
    public void lookAlikeCaRefused() throws Exception {
        // Same issuer name, same constraints, another key: the signature check catches it.
        assertTrue(refused(real(), "wrongca-leaf.pem", "10.42.0.1", NOW).contains("not signed by the K1000 CA"));
    }

    @Test
    public void otherCasLeavesRefused() throws Exception {
        assertTrue(refused(real(), "testca-good.pem", "10.42.0.1", NOW).contains("not issued by the K1000 CA"));
    }

    @Test
    public void testCaGoodLeafAccepted() throws Exception {
        test().verify(new X509Certificate[] {cert("testca-good.pem")}, "10.42.0.1", NOW);
        test().verify(new X509Certificate[] {cert("testca-good.pem")}, "k1000.home.arpa", NOW);
    }

    @Test
    public void nameConstraintsEnforced() throws Exception {
        assertTrue(refused(test(), "testca-extra-ip.pem", "10.42.0.1", NOW).contains("10.43.0.1 is outside"));
        assertTrue(refused(test(), "testca-extra-dns.pem", "10.42.0.1", NOW).contains("evil.example is outside"));
        assertTrue(refused(test(), "testca-cn-outside.pem", "10.42.0.1", NOW).contains("evil.example is outside"));
    }

    /**
     * Why K1000Trust checks the name constraints itself: the platform PKIX validator (here the
     * JVM's; Android's is the same OpenJDK code) ignores the trust anchor's own Name Constraints,
     * and refuses constraints handed to it through TrustAnchor.
     */
    @Test
    public void pkixAloneIgnoresTheAnchorsNameConstraints() throws Exception {
        X509Certificate ca = cert("testca.pem");
        java.security.cert.PKIXParameters p = new java.security.cert.PKIXParameters(
                java.util.Collections.singleton(new java.security.cert.TrustAnchor(ca, null)));
        p.setRevocationEnabled(false);
        p.setDate(NOW);
        java.security.cert.CertificateFactory cf = java.security.cert.CertificateFactory.getInstance("X.509");
        java.security.cert.CertPathValidator v = java.security.cert.CertPathValidator.getInstance("PKIX");
        // Accepted by PKIX alone although 10.43.0.1 is outside the CA's constraints ...
        v.validate(cf.generateCertPath(java.util.List.of(cert("testca-extra-ip.pem"))), p);
        // ... and refused by K1000Trust (nameConstraintsEnforced). Handing the constraints to
        // the TrustAnchor does not help: PKIX refuses them.
        byte[] nc = K1000Trust.unwrapOctetString(ca.getExtensionValue(K1000Trust.OID_NAME_CONSTRAINTS));
        try {
            java.security.cert.PKIXParameters q = new java.security.cert.PKIXParameters(
                    java.util.Collections.singleton(new java.security.cert.TrustAnchor(ca, nc)));
            q.setRevocationEnabled(false);
            v.validate(cf.generateCertPath(java.util.List.of(cert("testca-good.pem"))), q);
            fail("this JVM now applies TrustAnchor name constraints: K1000Trust could use them");
        } catch (java.security.InvalidAlgorithmParameterException e) {
            assertTrue(e.getMessage().contains("name constraints"));
        }
    }

    @Test
    public void hostMustBeNamedExactly() throws Exception {
        assertTrue(refused(test(), "testca-other-ip.pem", "10.42.0.1", NOW).contains("does not name 10.42.0.1"));
        assertTrue(refused(test(), "testca-other-ip.pem", "10.42.0.5", NOW).contains("not a K1000 host"));
        assertTrue(refused(test(), "testca-wildcard.pem", "k1000.home.arpa", NOW).contains("does not name"));
    }

    @Test
    public void leafMustBeATlsServerCertificate() throws Exception {
        assertTrue(refused(test(), "testca-is-ca.pem", "10.42.0.1", NOW).contains("is a CA"));
        assertTrue(refused(test(), "testca-clientauth.pem", "10.42.0.1", NOW).contains("server use"));
    }

    @Test
    public void unconstrainedCaIsNeverUsed() throws Exception {
        try {
            new K1000Trust(cert("unconstrained-ca.pem"), K1000Hosts.HOSTS);
            fail("an unconstrained CA was accepted as the K1000 anchor");
        } catch (CertificateException e) {
            assertTrue(e.getMessage().contains("no critical name constraints"));
        }
        try {
            new K1000Trust(cert("k1000-leaf.pem"), K1000Hosts.HOSTS);
            fail("a leaf was accepted as the K1000 anchor");
        } catch (CertificateException e) {
            assertTrue(e.getMessage().contains("not a CA"));
        }
    }

    @Test
    public void bundledCaIsTheNameConstrainedK1000Ca() throws Exception {
        K1000Trust.NameConstraints nc = K1000Trust.NameConstraints.parse(
                K1000Trust.unwrapOctetString(bundledCa().getExtensionValue(K1000Trust.OID_NAME_CONSTRAINTS)));
        assertTrue(nc.permittedDns.size() == 1 && nc.permittedDns.get(0).equals("k1000.home.arpa"));
        assertTrue(nc.permittedIp.size() == 1);
        assertTrue(java.util.Arrays.equals(nc.permittedIp.get(0),
                new byte[] {10, 42, 0, 0, (byte) 255, (byte) 255, (byte) 255, 0}));
        assertTrue(nc.excludedDns.isEmpty() && nc.excludedIp.isEmpty());
    }

    @Test
    public void noChainNoHost() throws Exception {
        try {
            real().verify(new X509Certificate[0], "10.42.0.1", NOW);
            fail();
        } catch (CertificateException e) {
            assertTrue(e.getMessage().contains("no server certificate"));
        }
    }
}
