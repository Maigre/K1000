// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;
import org.w3c.dom.Element;
import org.w3c.dom.NodeList;

import java.io.File;

import javax.xml.parsers.DocumentBuilderFactory;

/** MainActivity's pure decisions (ShellRules), the fixes of the 0.2 review among them. */
public class ShellRulesTest {

    /** Review 1: a page choice that changes nothing reloads nothing (a set_ui on every ready). */
    @Test
    public void pageChoiceReloadsOnlyWhenThePageChanges() {
        // The S2 page re-sending its choice at each ready: never a reload, however often.
        for (int i = 0; i < 100; i++) {
            assertEquals(ShellRules.UiSwitch.NOTHING, ShellRules.uiSwitch("v2", "v2", "v2"));
        }
        assertEquals(ShellRules.UiSwitch.NOTHING, ShellRules.uiSwitch("v1", "v1", "v1"));
        // The page already shows what is asked, the choice differs: remembered, no reload.
        assertEquals(ShellRules.UiSwitch.REMEMBER, ShellRules.uiSwitch("v2", "v1", "v1"));   // v1 shown (no /v2/ on the box)
        assertEquals(ShellRules.UiSwitch.REMEMBER, ShellRules.uiSwitch("v1", "v2", "v2"));
        // Another page: load it.
        assertEquals(ShellRules.UiSwitch.LOAD, ShellRules.uiSwitch("v2", "v1", "v2"));
        assertEquals(ShellRules.UiSwitch.LOAD, ShellRules.uiSwitch("v1", "v2", "v1"));
        assertEquals(ShellRules.UiSwitch.LOAD, ShellRules.uiSwitch("v2", "v2", "v1"));       // retry v2 after a 404
        assertEquals(ShellRules.UiSwitch.LOAD, ShellRules.uiSwitch("v2", "v2", null));       // no box page on screen
    }

    @Test
    public void pageShown() {
        assertEquals("v2", ShellRules.uiShown("https://10.42.0.1/v2/"));
        assertEquals("v2", ShellRules.uiShown("https://k1000.home.arpa/v2/index.html"));
        assertEquals("v1", ShellRules.uiShown("https://10.42.0.1/"));
        assertEquals("v1", ShellRules.uiShown("https://k1000.home.arpa/"));
        assertEquals("v1", ShellRules.uiShown("https://10.42.0.1/v2"));          // not under /v2/
        assertNull(ShellRules.uiShown(null));
        assertNull(ShellRules.uiShown("about:blank"));
        assertNull(ShellRules.uiShown("https://example.com/v2/"));
    }

    /** Review 1: the shortcuts' actions count only through the non-exported alias. */
    @Test
    public void shortcutActionsOnlyThroughTheAlias() {
        assertTrue(ShellRules.fromShortcut("gr.trentesept.k1000.Shortcut"));
        assertFalse(ShellRules.fromShortcut("gr.trentesept.k1000.MainActivity"));   // exported: anyone
        assertFalse(ShellRules.fromShortcut(null));                                  // an implicit intent
        assertFalse(ShellRules.fromShortcut("gr.trentesept.k1000.Shortcut2"));
        assertFalse(ShellRules.fromShortcut("Shortcut"));
    }

    /** Review 1: the alias exists in the manifest, is not exported, and targets MainActivity. */
    @Test
    public void manifestDeclaresTheAliasNotExported() throws Exception {
        File f = new File("src/main/AndroidManifest.xml");
        if (!f.exists()) f = new File("app/src/main/AndroidManifest.xml");
        assertTrue("manifest not found from " + new File(".").getAbsolutePath(), f.exists());
        DocumentBuilderFactory dbf = DocumentBuilderFactory.newInstance();
        dbf.setNamespaceAware(true);
        dbf.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true);
        NodeList aliases = dbf.newDocumentBuilder().parse(f).getElementsByTagName("activity-alias");
        String ns = "http://schemas.android.com/apk/res/android";
        Element alias = null;
        for (int i = 0; i < aliases.getLength(); i++) {
            Element e = (Element) aliases.item(i);
            if (ShellRules.SHORTCUT_ALIAS.equals("gr.trentesept.k1000" + e.getAttributeNS(ns, "name"))) alias = e;
        }
        assertNotNull("no <activity-alias> for " + ShellRules.SHORTCUT_ALIAS, alias);
        assertEquals("false", alias.getAttributeNS(ns, "exported"));
        assertEquals(".MainActivity", alias.getAttributeNS(ns, "targetActivity"));
        assertEquals("an alias with an intent filter could be reached by other apps",
                0, alias.getElementsByTagName("intent-filter").getLength());
    }

    /** Review 3: only a touch on the menu itself keeps it open. */
    @Test
    public void touchOnTheMenuOnly() {
        // The menu: full width at the top, 1080 × 470 px from (0, 0).
        assertTrue(ShellRules.inside(0, 0, 1080, 470, 0, 0));
        assertTrue(ShellRules.inside(0, 0, 1080, 470, 1079.5f, 469.5f));
        assertFalse(ShellRules.inside(0, 0, 1080, 470, 540, 470));      // just below: the page
        assertFalse(ShellRules.inside(0, 0, 1080, 470, 540, 1500));     // the tiles
        assertFalse(ShellRules.inside(0, 0, 1080, 470, -1, 10));
        assertFalse(ShellRules.inside(0, 0, 0, 0, 0, 0));               // not laid out yet
        assertTrue(ShellRules.inside(40, 120, 100, 50, 40, 120));
        assertFalse(ShellRules.inside(40, 120, 100, 50, 140, 120));
    }

    /** Review 4: the Edge test shows the bars, whatever the page asked; the page's choice after. */
    @Test
    public void edgeTestShowsTheBars() {
        assertTrue(ShellRules.barsHidden(true, false));
        assertFalse(ShellRules.barsHidden(true, true));
        assertFalse(ShellRules.barsHidden(false, false));
        assertFalse(ShellRules.barsHidden(false, true));
    }

    /** Review 5: Android 10 (no cutout padding there) never draws under the cutout. */
    @Test
    public void cutoutOnlyWhereTheAppPadsForIt() {
        assertFalse(ShellRules.cutoutShortEdges(29));
        assertTrue(ShellRules.cutoutShortEdges(30));
        assertTrue(ShellRules.cutoutShortEdges(33));
        assertFalse(ShellRules.pageTakesCutout(29, true, true));
        assertTrue(ShellRules.pageTakesCutout(33, true, true));
        assertFalse(ShellRules.pageTakesCutout(33, true, false));
        assertFalse(ShellRules.pageTakesCutout(33, false, true));
    }

    /**
     * 0.2.2, the phone mic on the v2 page: the microphone alone, for the box's page alone. Camera,
     * MIDI, protected media, the microphone with any of them, and every other origin are refused.
     */
    @Test
    public void micOnlyForTheBoxPage() {
        // The constant is WebView's own (inlined from android.webkit.PermissionRequest).
        assertEquals(android.webkit.PermissionRequest.RESOURCE_AUDIO_CAPTURE, ShellRules.AUDIO_CAPTURE);
        String mic = ShellRules.AUDIO_CAPTURE;
        String cam = android.webkit.PermissionRequest.RESOURCE_VIDEO_CAPTURE;
        String midi = android.webkit.PermissionRequest.RESOURCE_MIDI_SYSEX;
        String drm = android.webkit.PermissionRequest.RESOURCE_PROTECTED_MEDIA_ID;
        String[] audio = {mic};
        K1000Hosts.setBenchHost("");
        // WebView gives the origin as "scheme://host[:port]/".
        assertTrue(ShellRules.grantMic("https://10.42.0.1/", audio));
        assertTrue(ShellRules.grantMic("https://k1000.home.arpa/", audio));
        assertTrue(ShellRules.grantMic("https://10.42.0.1", audio));
        assertTrue(ShellRules.grantMic("https://10.42.0.1:443/", audio));
        assertTrue(ShellRules.grantMic("https://K1000.HOME.ARPA/", audio));
        assertTrue(ShellRules.grantMic("https://10.42.0.1/", new String[] {mic, mic}));
        // Never anything but the microphone, never the microphone together with something else.
        assertFalse(ShellRules.grantMic("https://10.42.0.1/", new String[] {cam}));
        assertFalse(ShellRules.grantMic("https://10.42.0.1/", new String[] {mic, cam}));
        assertFalse(ShellRules.grantMic("https://10.42.0.1/", new String[] {cam, mic}));
        assertFalse(ShellRules.grantMic("https://10.42.0.1/", new String[] {midi}));
        assertFalse(ShellRules.grantMic("https://10.42.0.1/", new String[] {mic, drm}));
        assertFalse(ShellRules.grantMic("https://10.42.0.1/", new String[] {"android.webkit.resource.FUTURE"}));
        assertFalse(ShellRules.grantMic("https://10.42.0.1/", new String[] {mic, null}));
        assertFalse(ShellRules.grantMic("https://10.42.0.1/", new String[0]));
        assertFalse(ShellRules.grantMic("https://10.42.0.1/", null));
        // Never another origin: plain http, another port, another host, look-alikes, opaque ones.
        for (String o : new String[] {"http://10.42.0.1/", "https://10.42.0.1:8443/", "https://10.42.0.2/",
                "https://10.42.0.1.evil.example/", "https://evil.example/", "https://k1000.home.arpa.evil.example/",
                "https://user@10.42.0.1/", "https://192.168.8.54/", "null", "", "data:text/html,x", "file:///",
                "about:blank", "https://10.42.0.1/?x", null}) {
            assertFalse(String.valueOf(o), ShellRules.grantMic(o, audio));
        }
        // A bench build: its development host too (and still the box).
        K1000Hosts.setBenchHost("192.168.8.54");
        try {
            assertTrue(ShellRules.grantMic("https://192.168.8.54/", audio));
            assertTrue(ShellRules.grantMic("https://10.42.0.1/", audio));
            assertFalse(ShellRules.grantMic("https://192.168.8.55/", audio));
            assertFalse(ShellRules.grantMic("https://192.168.8.54/", new String[] {cam}));
        } finally {
            K1000Hosts.setBenchHost("");
        }
    }
}
