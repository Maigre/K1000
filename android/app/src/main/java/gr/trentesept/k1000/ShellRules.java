// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

/**
 * MainActivity's decisions that need no Android: which intents may switch the page, when a page
 * choice reloads, where a touch lands, when the bars hide, when the page may draw under the
 * camera cutout. Pure Java, unit-tested on the JVM (ShellRulesTest).
 */
final class ShellRules {

    /**
     * The launcher shortcuts' target (AndroidManifest.xml: {@code <activity-alias .Shortcut>},
     * not exported). Shortcuts run as K1000 itself, so a non-exported target works, and no other
     * app can reach it: an intent naming MainActivity (exported, for the launcher) with a
     * shortcut's action is another app's, and is ignored.
     */
    static final String SHORTCUT_ALIAS = "gr.trentesept.k1000.Shortcut";

    /** What a page choice (set_ui, a shortcut, the menu) does. */
    enum UiSwitch {
        /** That page is on screen and already the choice: no reload, no toast. */
        NOTHING,
        /** That page is on screen already: remember the new choice, no reload. */
        REMEMBER,
        /** Remember the choice and load that page. */
        LOAD
    }

    private ShellRules() {
    }

    /**
     * @param saved  the remembered choice, "v1" or "v2"
     * @param wanted the choice asked for, "v1" or "v2"
     * @param shown  the page on screen: "v1", "v2", or null when no box page is shown
     */
    static UiSwitch uiSwitch(String saved, String wanted, String shown) {
        if (wanted.equals(shown)) return wanted.equals(saved) ? UiSwitch.NOTHING : UiSwitch.REMEMBER;
        return UiSwitch.LOAD;
    }

    /** The page a URL shows: "v2" under /v2/ on the box, "v1" elsewhere on the box, else null. */
    static String uiShown(String url) {
        if (!K1000Hosts.isBoxUrl(url)) return null;
        return K1000Hosts.pathOf(url).startsWith(K1000Hosts.PATH_V2) ? "v2" : "v1";
    }

    /** A shortcut's action counts only when it came through the non-exported alias. */
    static boolean fromShortcut(String componentClass) {
        return SHORTCUT_ALIAS.equals(componentClass);
    }

    /** The screen point (x, y) lies on a view at screen (left, top), w × h px. */
    static boolean inside(int left, int top, int w, int h, float x, float y) {
        return w > 0 && h > 0 && x >= left && x < left + w && y >= top && y < top + h;
    }

    /**
     * The system bars are hidden: the page's fullscreen choice, except during the Edge test.
     * Immersive keeps Back off every edge swipe (the first swipe only shows the bars), so with the
     * bars hidden that test would pass even with broken exclusion rects.
     */
    static boolean barsHidden(boolean fullscreen, boolean edgeTest) {
        return fullscreen && !edgeTest;
    }

    /**
     * The window extends into the cutout's short edges only where the app pads for the cutout
     * itself (its insets listener, Android 11 / API 30 and later). Android 10 keeps the default
     * mode: a full-screen window is kept out of the cutout there, as in 0.1.
     */
    static boolean cutoutShortEdges(int sdk) {
        return sdk >= 30;
    }

    /** The page draws under the cutout ({t:"fullscreen", cutout:true}): Android 11+ only. */
    static boolean pageTakesCutout(int sdk, boolean fullscreen, boolean cutout) {
        return fullscreen && cutout && cutoutShortEdges(sdk);
    }
}
