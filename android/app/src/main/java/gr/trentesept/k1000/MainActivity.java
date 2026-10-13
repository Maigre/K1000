// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.BroadcastReceiver;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.pm.ShortcutInfo;
import android.content.pm.ShortcutManager;
import android.graphics.Bitmap;
import android.graphics.Rect;
import android.graphics.drawable.Icon;
import android.net.Network;
import android.net.Uri;
import android.net.http.SslCertificate;
import android.net.http.SslError;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;
import android.text.TextUtils;
import android.util.Log;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.webkit.PermissionRequest;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import android.webkit.ServiceWorkerWebSettings;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
import android.webkit.WebMessage;
import android.webkit.WebMessagePort;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;
import android.window.BackEvent;
import android.window.OnBackAnimationCallback;
import android.window.OnBackInvokedDispatcher;

import com.google.zxing.BarcodeFormat;
import com.google.zxing.ResultPoint;
import com.journeyapps.barcodescanner.BarcodeCallback;
import com.journeyapps.barcodescanner.BarcodeResult;
import com.journeyapps.barcodescanner.DecoratedBarcodeView;
import com.journeyapps.barcodescanner.DefaultDecoderFactory;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.security.cert.X509Certificate;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Date;
import java.util.List;
import java.util.Map;

/**
 * The K1000 app: one activity, one full-screen WebView showing the page the box serves at
 * https://10.42.0.1/v2/ (v1 at https://10.42.0.1/ on THIS PHONE's choice), plus what the page
 * cannot do on its own: join the K1000 Wi-Fi and bind to it (mobile data stays on), trust the
 * K1000 CA for the box only (no CA install in Android settings), keep the screen on, go immersive,
 * keep Back off the page's slide areas, play the phone's own haptics, and answer diagnostics. The
 * page reaches those through a WebMessagePort bridge (android/BRIDGE.md). See docs/09-android.md.
 */
public final class MainActivity extends Activity implements K1000Link.Listener {

    private static final String TAG = "K1000";
    private static final int REQ_CAMERA = 1;
    private static final int REQ_AUDIO = 2;
    private static final int REQ_LOCAL_NETWORK = 3;
    /** Manifest.permission.ACCESS_LOCAL_NETWORK: Android 17 (API 37) and later. */
    private static final String PERM_LOCAL_NETWORK = "android.permission.ACCESS_LOCAL_NETWORK";
    private static final int API_LOCAL_NETWORK = 37;

    private static final String PREFS = "k1000";
    private static final String P_SSID = "ssid";
    private static final String P_PSK = "psk";
    private static final String P_HIDDEN = "hidden";
    private static final String P_MODE = "mode";
    /** THIS PHONE's page: "v2" (the default) or "v1" (the escape hatch). */
    private static final String P_UI = "ui";
    /** The page's last fullscreen request (immersive from the start, no flash of the bars). */
    private static final String P_FULLSCREEN = "fullscreen";

    /**
     * Launcher shortcuts (long-press the K1000 icon). Honoured only through the non-exported
     * alias {@link ShellRules#SHORTCUT_ALIAS}: no other app can switch or reload the page.
     */
    static final String ACTION_UI_V1 = "gr.trentesept.k1000.action.UI_V1";
    static final String ACTION_UI_V2 = "gr.trentesept.k1000.action.UI_V2";
    static final String ACTION_DIAG = "gr.trentesept.k1000.action.DIAG";

    /** Back: a second unanswered Back within this hides K1000; a hold this long hides it at once. */
    private static final long BACK_AGAIN_MS = 2000;
    private static final long BACK_HOLD_MS = 1000;
    /** How long the page has to answer {t:"back"} before the app treats it as "nothing open". */
    private static final long BACK_ANSWER_MS = 400;
    private static final long MENU_MS = 5000;
    private static final long EDGE_TEST_MS = 20000;

    private SharedPreferences prefs;
    private K1000Link link;
    private K1000Trust trust;
    private String trustError;

    private WebView web;
    private View statusPanel;
    private TextView statusTitle;
    private TextView statusText;
    private ProgressBar statusProgress;
    private Button statusRetry;
    private Button statusWifi;
    private Button statusChange;
    private View setupPanel;
    private EditText setupSsid;
    private EditText setupPassword;
    private CheckBox setupSystem;
    private TextView setupError;
    private Button setupCancel;
    private View scanPanel;
    private DecoratedBarcodeView scanView;
    private View aboutPanel;
    private TextView aboutText;
    private View root;
    private View menuPanel;
    private TextView menuTitle;
    private Button menuUi;
    private View diagPanel;
    private TextView diagSummary;
    private TextView diagStatus;
    private TextView diagText;
    private View edgePanel;
    private View edgeLeft;
    private View edgeRight;
    private TextView edgeText;
    private Haptics haptics;

    private boolean started;
    /** The link should be held (credentials known, not declined by the user). */
    private boolean wantLink;
    /** A re-join must wait for the app to be in the foreground (Android refuses it otherwise). */
    private boolean relinkWhenStarted;
    private boolean pageRequested;
    private boolean pageFailed;
    private boolean triedFallback;
    /** This load met a certificate the app refused: that verdict stays on screen. */
    private boolean tlsRefused;
    /** Automatic reloads after a failed page load while the link is up (reset by a good load). */
    private int pageRetries;
    /** The page is loaded from the fallback host (the box's name; a bench build: the box's address). */
    private boolean onFallbackHost;
    /** This box answered 404 at /v2/ (a box without v2 yet): v1 for the rest of this session. */
    private boolean v2Missing;

    // The page bridge (android/BRIDGE.md): one WebMessagePort per page load.
    private WebMessagePort port;
    private int portGen;
    private int handshakes;
    private boolean bridgeReady;
    private List<String> pageCaps = Collections.emptyList();

    // Window: immersive, cutout, screen on, Back off the page's slide areas.
    private boolean fullscreen = true;
    private boolean cutoutToPage;
    private boolean keepAwake = true;
    private double[][] pageRects;
    private android.graphics.Insets cutoutInsets = android.graphics.Insets.NONE;

    // Back: page overlays first, then "Back again" (or a hold) hides K1000.
    private long backSeq;
    private long backPendingId = -1;
    private boolean backPendingHold;
    private long lastTopBack;
    private long backStartedAt;
    private boolean backKeyHoldFired;
    private boolean edgeTest;
    private int edgeBacks;
    private long edgeEndsAt;
    private PermissionRequest pendingAudio;
    private boolean localNetworkAsked;
    /** The activity was paused since the last request: Android's connect dialog was shown. */
    private boolean pausedSinceRequest;
    private int autoRetries;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final BroadcastReceiver wifiState = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            int st = intent.getIntExtra(WifiManager.EXTRA_WIFI_STATE, WifiManager.WIFI_STATE_UNKNOWN);
            if (st == WifiManager.WIFI_STATE_ENABLED && wantLink && started && !link.hasNetwork()
                    && mode() == K1000Link.Mode.JOIN) {
                Log.i(TAG, "Wi-Fi on: asking for K1000 again");
                startLink(true);
            }
        }
    };

    // ------------------------------------------------------------------------------- lifecycle

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // A bench build also treats its development host as the box (never a release: "").
        K1000Hosts.setBenchHost(BuildConfig.BENCH_HOST);
        setContentView(R.layout.activity_main);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        fullscreen = prefs.getBoolean(P_FULLSCREEN, true);
        setKeepAwake(true);
        edgeToEdge();
        applyWindowMode();

        link = new K1000Link(this, this);
        trust = loadTrust();

        bindViews();
        haptics = new Haptics(this, root);
        setupWebView();
        setupBack();
        setupShortcuts();
        IntentFilter wifiFilter = new IntentFilter(WifiManager.WIFI_STATE_CHANGED_ACTION);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(wifiState, wifiFilter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(wifiState, wifiFilter);
        }

        if (BuildConfig.SMOKE) {
            if (!prefs.contains(P_MODE)) {
                // The emulator has no K1000 access point: follow its own Wi-Fi instead.
                prefs.edit().putString(P_MODE, K1000Link.Mode.SYSTEM.name()).apply();
            }
            smokeSetup(getIntent());
        }
        if (trust == null) {
            showStatus(R.string.st_broken_title, getString(R.string.st_broken_text, trustError), false, false);
        } else if (hasLinkConfig()) {
            wantLink = true;
            showStatus(R.string.st_joining_title, getString(R.string.st_joining_text), true, false);
        } else {
            showSetup(true);
        }
        handleAction(getIntent());
    }

    @Override
    protected void onStart() {
        super.onStart();
        started = true;
    }

    @Override
    protected void onResume() {
        super.onResume();
        web.onResume();
        if (scanPanel.getVisibility() == View.VISIBLE) scanView.resume();
        // (Re)join here, with the activity on top: Android accepts a Wi-Fi request with a
        // specifier only from the app in the foreground.
        if (wantLink && trust != null && setupPanel.getVisibility() != View.VISIBLE && localNetworkAllowed()) {
            if (!link.isActive() || relinkWhenStarted) startLink();
            else if (link.isUp() && !link.boundNetworkAlive()) onLinkLost();
            else if (link.isBlocked()) link.retryBind();
        }
        relinkWhenStarted = false;
    }

    @Override
    protected void onPause() {
        pausedSinceRequest = true;
        if (scanPanel.getVisibility() == View.VISIBLE) scanView.pause();
        web.onPause();
        super.onPause();
    }

    @Override
    protected void onStop() {
        started = false;
        super.onStop();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        // A permission or Wi-Fi dialog shows the bars again: hide them once K1000 has focus back.
        if (hasFocus) applyWindowMode();
    }

    @Override
    protected void onDestroy() {
        main.removeCallbacksAndMessages(null);
        haptics.cancel();
        closePort();
        unregisterReceiver(wifiState);
        link.stop();
        if (pendingAudio != null) pendingAudio.deny();
        web.destroy();
        super.onDestroy();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleAction(intent);
        if (BuildConfig.SMOKE) smokeCommand(intent);
    }

    /**
     * A launcher shortcut: the page choice (remembered), or the Diagnostics panel. Only through the
     * shortcuts' own alias (not exported): the same action sent to MainActivity, which is exported
     * for the launcher, comes from another app and is ignored.
     */
    private void handleAction(Intent intent) {
        String a = intent == null ? null : intent.getAction();
        if (a == null || !(a.equals(ACTION_UI_V1) || a.equals(ACTION_UI_V2) || a.equals(ACTION_DIAG))) return;
        ComponentName c = intent.getComponent();
        if (!ShellRules.fromShortcut(c == null ? null : c.getClassName())) {
            Log.w(TAG, "ignored " + a + ": not from K1000's own launcher shortcut");
            return;
        }
        ShortcutManager sm = getSystemService(ShortcutManager.class);
        if (sm != null) sm.reportShortcutUsed(a.equals(ACTION_DIAG) ? "diag" : a.equals(ACTION_UI_V1) ? "ui_v1" : "ui_v2");
        switch (a) {
            case ACTION_UI_V1:
                switchUi("v1");
                break;
            case ACTION_UI_V2:
                switchUi("v2");
                break;
            case ACTION_DIAG:
                main.post(this::showDiag);
                break;
            default:
                break;
        }
    }

    @Override
    public boolean dispatchTouchEvent(MotionEvent ev) {
        // A finger on the app's menu keeps it open; a finger on the page does not (the menu
        // covers the page's status strip: it closes 5 s after it was last touched itself).
        if (menuPanel != null && menuPanel.getVisibility() == View.VISIBLE && touches(menuPanel, ev)) {
            main.removeCallbacks(hideMenuLater);
            main.postDelayed(hideMenuLater, MENU_MS);
        }
        return super.dispatchTouchEvent(ev);
    }

    /** The event's first pointer is on {@code v} (screen coordinates). */
    private static boolean touches(View v, MotionEvent ev) {
        int[] at = new int[2];
        v.getLocationOnScreen(at);
        return ShellRules.inside(at[0], at[1], v.getWidth(), v.getHeight(), ev.getRawX(), ev.getRawY());
    }

    // ---------------------------------------------------------------------------------- views

    private void bindViews() {
        web = findViewById(R.id.web);
        statusPanel = findViewById(R.id.status_panel);
        statusTitle = findViewById(R.id.status_title);
        statusText = findViewById(R.id.status_text);
        statusProgress = findViewById(R.id.status_progress);
        statusRetry = findViewById(R.id.status_retry);
        statusWifi = findViewById(R.id.status_wifi);
        statusChange = findViewById(R.id.status_change);
        setupPanel = findViewById(R.id.setup_panel);
        setupSsid = findViewById(R.id.setup_ssid);
        setupPassword = findViewById(R.id.setup_password);
        setupSystem = findViewById(R.id.setup_system);
        setupError = findViewById(R.id.setup_error);
        setupCancel = findViewById(R.id.setup_cancel);
        scanPanel = findViewById(R.id.scan_panel);
        scanView = findViewById(R.id.scan_view);
        findViewById(R.id.scan_cancel).setOnClickListener(v -> stopScan());
        aboutPanel = findViewById(R.id.about_panel);
        aboutText = findViewById(R.id.about_text);
        findViewById(R.id.setup_about).setOnClickListener(v -> showAbout());
        findViewById(R.id.about_close).setOnClickListener(v -> aboutPanel.setVisibility(View.GONE));
        root = findViewById(R.id.root);

        // The app's own menu (Back at the page's top level). Switching needs a hold.
        menuPanel = findViewById(R.id.menu_panel);
        menuTitle = findViewById(R.id.menu_title);
        menuUi = findViewById(R.id.menu_ui);
        findViewById(R.id.menu_hide).setOnClickListener(v -> hideApp());
        findViewById(R.id.menu_diag).setOnClickListener(v -> showDiag());
        findViewById(R.id.menu_reload).setOnClickListener(v -> {
            hideMenu();
            retry();
        });
        menuUi.setOnClickListener(v -> toast(R.string.toast_hold));
        menuUi.setOnLongClickListener(v -> {
            switchUi(isV1() ? "v2" : "v1");
            return true;
        });
        View wifi = findViewById(R.id.menu_wifi);
        wifi.setOnClickListener(v -> toast(R.string.toast_hold));
        wifi.setOnLongClickListener(v -> {
            hideMenu();
            showSetup(false);
            return true;
        });

        diagPanel = findViewById(R.id.diag_panel);
        diagSummary = findViewById(R.id.diag_summary);
        diagStatus = findViewById(R.id.diag_status);
        diagText = findViewById(R.id.diag_text);
        findViewById(R.id.diag_close).setOnClickListener(v -> hideDiag());
        findViewById(R.id.diag_haptics).setOnClickListener(v -> testHaptics());
        findViewById(R.id.diag_edge).setOnClickListener(v -> startEdgeTest());
        findViewById(R.id.diag_copy).setOnClickListener(v -> copyDiag());

        edgePanel = findViewById(R.id.edge_panel);
        edgeLeft = findViewById(R.id.edge_left);
        edgeRight = findViewById(R.id.edge_right);
        edgeText = findViewById(R.id.edge_text);
        findViewById(R.id.edge_end).setOnClickListener(v -> endEdgeTest());
        // The bands' exclusion follows their size (the bars come and go during the test).
        for (View b : new View[] {edgeLeft, edgeRight}) {
            b.addOnLayoutChangeListener((v, l, t, r, bo, ol, ot, or, ob) -> {
                if (edgeTest) v.setSystemGestureExclusionRects(List.of(new Rect(0, 0, r - l, bo - t)));
            });
        }

        statusRetry.setOnClickListener(v -> retry());
        statusWifi.setOnClickListener(v -> openWifiPanel());
        statusChange.setOnClickListener(v -> showSetup(false));
        findViewById(R.id.setup_scan).setOnClickListener(v -> startScan());
        findViewById(R.id.setup_join).setOnClickListener(v -> joinTyped());
        setupCancel.setOnClickListener(v -> closeSetup());
        setupSystem.setOnCheckedChangeListener((b, on) -> {
            setupPassword.setEnabled(!on);
            setupSsid.setEnabled(!on);
            findViewById(R.id.setup_scan).setEnabled(!on);
        });
    }

    /**
     * Android 11+: draw behind the system bars everywhere (Android 15+ always does) and into the
     * display cutout's short edges, then pad the content: by the bars when they show, by the cutout
     * unless the page said it keeps clear of it itself ({t:"fullscreen", cutout:true}), by the
     * keyboard. Android 10 has no such padding here: it keeps the default cutout mode, which keeps
     * a full-screen window out of the cutout (ShellRules.cutoutShortEdges).
     */
    @SuppressWarnings("deprecation")   // setDecorFitsSystemWindows: only called below API 35
    private void edgeToEdge() {
        View r = findViewById(R.id.root);
        if (ShellRules.cutoutShortEdges(Build.VERSION.SDK_INT)) {
            WindowManager.LayoutParams lp = getWindow().getAttributes();
            lp.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            getWindow().setAttributes(lp);
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.VANILLA_ICE_CREAM) {
                getWindow().setDecorFitsSystemWindows(false);
            }
            r.setOnApplyWindowInsetsListener((v, insets) -> {
                int types = WindowInsets.Type.ime();
                if (!ShellRules.barsHidden(fullscreen, edgeTest)) types |= WindowInsets.Type.systemBars();
                if (!cutoutToPage) types |= WindowInsets.Type.displayCutout();
                android.graphics.Insets i = insets.getInsets(types);
                v.setPadding(i.left, i.top, i.right, i.bottom);
                android.graphics.Insets c = insets.getInsets(WindowInsets.Type.displayCutout());
                if (!c.equals(cutoutInsets)) {
                    cutoutInsets = c;
                    sendInsets();
                }
                return WindowInsets.CONSUMED;
            });
        }
    }

    /**
     * Immersive (bars hidden, shown for a moment by a swipe from the edge) or not: the page's
     * choice, except that the Edge test shows the bars for its 20 s.
     */
    @SuppressWarnings("deprecation")   // systemUiVisibility: the API 29 way only
    private void applyWindowMode() {
        Window w = getWindow();
        boolean hide = ShellRules.barsHidden(fullscreen, edgeTest);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            WindowInsetsController c = w.getInsetsController();
            if (c == null) return;
            if (hide) {
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                c.hide(WindowInsets.Type.systemBars());
            } else {
                c.show(WindowInsets.Type.systemBars());
            }
        } else {
            int flags = View.SYSTEM_UI_FLAG_LAYOUT_STABLE;
            if (hide) {
                flags |= View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_FULLSCREEN | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN;
            }
            w.getDecorView().setSystemUiVisibility(flags);
        }
        if (root != null) root.requestApplyInsets();
    }

    private void setFullscreen(boolean on, boolean cutout) {
        boolean take = ShellRules.pageTakesCutout(Build.VERSION.SDK_INT, on, cutout);
        boolean changed = on != fullscreen || take != cutoutToPage;
        fullscreen = on;
        cutoutToPage = take;
        prefs.edit().putBoolean(P_FULLSCREEN, on).apply();
        if (changed) Log.i(TAG, "window: immersive " + on + ", page under the cutout " + cutoutToPage);
        applyWindowMode();
        sendInsets();
    }

    private void setKeepAwake(boolean on) {
        keepAwake = on;
        if (on) getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        else getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    /** The page's exclusion rects (CSS px) on the WebView, in its pixels: Back stays off them. */
    private void applyExclusion() {
        List<Rect> list = new ArrayList<>();
        double[][] r = pageRects;
        if (r != null) {
            float d = getResources().getDisplayMetrics().density;
            for (int[] q : Bridge.toPixels(r, d, web.getWidth(), web.getHeight())) {
                list.add(new Rect(q[0], q[1], q[2], q[3]));
            }
        }
        web.setSystemGestureExclusionRects(list);
        if (BuildConfig.DEBUG && r != null) Log.d(TAG, "bridge: exclusion px " + list);
    }

    /** {t:"insets"}: what the page must keep clear itself (the cutout, once it draws under it). */
    private void sendInsets() {
        if (!bridgeReady) return;
        float d = getResources().getDisplayMetrics().density;
        android.graphics.Insets c = cutoutToPage ? cutoutInsets : android.graphics.Insets.NONE;
        send(Bridge.insets(Bridge.toCss(c.top, d), Bridge.toCss(c.right, d), Bridge.toCss(c.bottom, d),
                Bridge.toCss(c.left, d)));
    }

    private void toast(int text) {
        Toast.makeText(this, text, Toast.LENGTH_SHORT).show();
    }

    private void showStatus(int title, CharSequence text, boolean busy, boolean canRetry) {
        statusTitle.setText(title);
        statusText.setText(text);
        statusProgress.setVisibility(busy ? View.VISIBLE : View.GONE);
        statusRetry.setVisibility(canRetry ? View.VISIBLE : View.GONE);
        statusWifi.setVisibility(wifiOff() ? View.VISIBLE : View.GONE);
        statusChange.setVisibility(trust != null ? View.VISIBLE : View.GONE);
        statusPanel.setVisibility(View.VISIBLE);
    }

    private void hideStatus() {
        statusPanel.setVisibility(View.GONE);
    }

    private void showSetup(boolean first) {
        setupSsid.setText(prefs.getString(P_SSID, WifiQr.DEFAULT_SSID));
        setupPassword.setText("");
        setupSystem.setChecked(K1000Link.Mode.SYSTEM.name().equals(prefs.getString(P_MODE, "")));
        setupError.setVisibility(View.GONE);
        setupCancel.setVisibility(first ? View.GONE : View.VISIBLE);
        setupPanel.setVisibility(View.VISIBLE);
    }

    private void closeSetup() {
        hideKeyboard();
        setupPanel.setVisibility(View.GONE);
    }

    /** Version, licence, source, and the licences of everything the APK carries (assets/licenses). */
    private void showAbout() {
        hideKeyboard();
        if (aboutText.length() == 0) {
            StringBuilder sb = new StringBuilder(getString(R.string.about_header, BuildConfig.VERSION_NAME));
            for (String f : new String[] {"NOTICE.txt", "GPL-3.0.txt", "Apache-2.0.txt"}) {
                sb.append("\n\n");
                try (InputStream in = getAssets().open("licenses/" + f)) {
                    ByteArrayOutputStream out = new ByteArrayOutputStream();
                    byte[] buf = new byte[8192];
                    for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
                    sb.append(new String(out.toByteArray(), StandardCharsets.UTF_8));
                } catch (IOException e) {
                    sb.append(f).append(": ").append(e.getMessage());
                }
            }
            aboutText.setText(sb);
        }
        aboutPanel.setVisibility(View.VISIBLE);
    }

    private void setupError(String message) {
        setupError.setText(message);
        setupError.setVisibility(View.VISIBLE);
    }

    private void hideKeyboard() {
        InputMethodManager imm = getSystemService(InputMethodManager.class);
        if (imm != null) imm.hideSoftInputFromWindow(setupPanel.getWindowToken(), 0);
    }

    // ------------------------------------------------------------------------- setup and join

    private boolean hasLinkConfig() {
        String mode = prefs.getString(P_MODE, "");
        if (K1000Link.Mode.SYSTEM.name().equals(mode)) return true;
        return K1000Link.Mode.JOIN.name().equals(mode) && prefs.contains(P_SSID) && prefs.contains(P_PSK);
    }

    private K1000Link.Mode mode() {
        return K1000Link.Mode.SYSTEM.name().equals(prefs.getString(P_MODE, ""))
                ? K1000Link.Mode.SYSTEM : K1000Link.Mode.JOIN;
    }

    /**
     * The QR scanner, inside this activity: ZXing Android Embedded's camera view (Apache-2.0),
     * without its CaptureActivity (which needs AndroidX). Camera permission asked here.
     */
    private void startScan() {
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[] {Manifest.permission.CAMERA}, REQ_CAMERA);
            return;
        }
        hideKeyboard();
        scanView.getBarcodeView().setDecoderFactory(
                new DefaultDecoderFactory(Collections.singletonList(BarcodeFormat.QR_CODE)));
        scanView.setStatusText(getString(R.string.scan_prompt));
        scanView.decodeSingle(new BarcodeCallback() {
            @Override
            public void barcodeResult(BarcodeResult result) {
                stopScan();
                onScanned(result.getText());
            }

            @Override
            public void possibleResultPoints(List<ResultPoint> resultPoints) {
                // not used
            }
        });
        scanPanel.setVisibility(View.VISIBLE);
        scanView.resume();
    }

    private void stopScan() {
        if (scanPanel.getVisibility() != View.VISIBLE) return;
        scanView.pause();
        scanPanel.setVisibility(View.GONE);
    }

    private void onScanned(String payload) {
        try {
            save(WifiQr.parse(payload));
        } catch (WifiQr.InvalidException e) {
            showSetup(!hasLinkConfig());
            setupError(e.getMessage());
        }
    }

    private void joinTyped() {
        if (setupSystem.isChecked()) {
            prefs.edit().putString(P_MODE, K1000Link.Mode.SYSTEM.name()).apply();
            beginLink();
            return;
        }
        try {
            WifiQr.Credentials c = WifiQr.of(setupSsid.getText().toString().trim(),
                    setupPassword.getText().toString().trim(), false);
            save(c);
        } catch (WifiQr.InvalidException e) {
            setupError(e.getMessage());
        }
    }

    private void save(WifiQr.Credentials c) {
        // App-private storage only (no backup, no device transfer: res/xml/data_extraction_rules).
        prefs.edit()
                .putString(P_SSID, c.ssid)
                .putString(P_PSK, c.passphrase)
                .putBoolean(P_HIDDEN, c.hidden)
                .putString(P_MODE, K1000Link.Mode.JOIN.name())
                .apply();
        beginLink();
    }

    private void beginLink() {
        closeSetup();
        wantLink = true;
        link.release();
        if (localNetworkAllowed(true)) startLink();
    }

    private void startLink() {
        startLink(false);
    }

    /** @param quiet keep the current status (a rejoin) instead of showing "Joining K1000…" */
    private void startLink(boolean quiet) {
        K1000Link.Mode m = mode();
        WifiQr.Credentials c = null;
        if (m == K1000Link.Mode.JOIN) {
            c = new WifiQr.Credentials(prefs.getString(P_SSID, WifiQr.DEFAULT_SSID),
                    prefs.getString(P_PSK, ""), prefs.getBoolean(P_HIDDEN, false));
        }
        if (!link.isUp() && !quiet) {
            showStatus(R.string.st_joining_title, getString(m == K1000Link.Mode.JOIN
                    ? R.string.st_joining_text : R.string.st_joining_system_text), true, false);
        }
        pausedSinceRequest = false;
        try {
            link.start(m, c);
        } catch (RuntimeException e) {
            // e.g. a passphrase Android refuses, or too many outstanding requests
            Log.w(TAG, "cannot request the K1000 network", e);
            wantLink = false;
            showStatus(R.string.st_not_connected_title, getString(R.string.st_request_failed, e.getMessage()), false, true);
        }
    }

    private void retry() {
        if (trust == null) return;
        pageRetries = 0;
        if (!localNetworkAllowed(true)) return;
        wantLink = hasLinkConfig();
        if (!wantLink) {
            showSetup(true);
            return;
        }
        autoRetries = 0;
        if (link.isUp()) {
            hideStatus();
            loadPage(true);
        } else if (link.isBlocked()) {
            link.retryBind();
        } else {
            startLink();
        }
    }

    private boolean wifiOff() {
        WifiManager wm = getSystemService(WifiManager.class);
        return wm != null && !wm.isWifiEnabled();
    }

    private void openWifiPanel() {
        try {
            startActivity(new Intent(Settings.Panel.ACTION_WIFI));
        } catch (ActivityNotFoundException e) {
            startActivity(new Intent(Settings.ACTION_WIFI_SETTINGS));
        }
    }

    // ------------------------------------------------------------------------ link callbacks

    @Override
    public void onLinkUp(Network network) {
        autoRetries = 0;
        if (!pageRequested || pageFailed) {
            loadPage(pageRequested);
        } else {
            // The page reconnects by itself; tell it now instead of at its next back-off tick.
            web.evaluateJavascript("window.dispatchEvent(new Event('online'))", null);
        }
        hideStatus();
    }

    @Override
    public void onLinkBlocked() {
        // Not onLinkUp: unbound, the page would go over the VPN or mobile data. The link retries
        // the binding by itself every few seconds.
        showStatus(R.string.st_blocked_title, getString(R.string.st_blocked_text), true, true);
    }

    @Override
    public void onLinkLost() {
        boolean join = mode() == K1000Link.Mode.JOIN;
        if (join && wifiOff()) {
            // Asking now would be refused at once; the Wi-Fi receiver asks again when it is on.
            showStatus(R.string.st_not_connected_title, getString(R.string.st_wifi_off_text), false, true);
            return;
        }
        showStatus(R.string.st_not_connected_title, getString(join ? R.string.st_lost_text
                : R.string.st_lost_system_text), true, true);
        if (join) {
            // Ask again right away: with the approval remembered, Android rejoins K1000 by itself
            // as soon as a scan sees it (every ~10 s, screen on), without a dialog.
            if (started) startLink(true);
            else relinkWhenStarted = true;
        }
    }

    @Override
    public void onLinkUnavailable() {
        if (!started) {
            relinkWhenStarted = true;
            return;
        }
        if (mode() == K1000Link.Mode.JOIN && wifiOff()) {
            showStatus(R.string.st_not_connected_title, getString(R.string.st_wifi_off_text), false, true);
            return;   // the Wi-Fi receiver asks again when Wi-Fi is on
        }
        if (!pausedSinceRequest && autoRetries < 5) {
            // Refused without Android's dialog ever showing (not a "Cancel"): try again shortly.
            autoRetries++;
            showStatus(R.string.st_not_connected_title, getString(R.string.st_lost_text), true, true);
            main.postDelayed(() -> {
                if (started && wantLink && !link.isActive()) startLink(true);
            }, 3000L * autoRetries);
            return;
        }
        wantLink = false;   // declined in the dialog (or failing): no dialog loop, wait for Retry
        showStatus(R.string.st_not_connected_title, getString(R.string.st_unavailable_text), false, true);
    }

    // ----------------------------------------------------------------------------- the page

    private boolean isV1() {
        return "v1".equals(prefs.getString(P_UI, "v2"));
    }

    /** The page path: v2 unless THIS PHONE chose v1 (or this box has no v2 yet). */
    private String uiPath() {
        return v2Missing || isV1() ? K1000Hosts.PATH_V1 : K1000Hosts.PATH_V2;
    }

    private String pageUrl() {
        return K1000Hosts.pageUrl(onFallbackHost ? K1000Hosts.fallbackHost() : K1000Hosts.primaryHost(), uiPath());
    }

    private void loadPage(boolean reload) {
        pageRequested = true;
        pageFailed = false;
        tlsRefused = false;
        String url = pageUrl();
        if (reload && url.equals(web.getUrl())) {
            web.reload();
        } else {
            triedFallback = onFallbackHost;
            web.loadUrl(url);
        }
    }

    /**
     * THIS PHONE's page choice, remembered: "v1" (the escape hatch) or "v2". With that page on
     * screen already, nothing reloads: a page that re-sends its choice at each {t:"ready"}, or a
     * shortcut to the page in use, must not reload it mid-show.
     */
    private void switchUi(String ui) {
        String saved = prefs.getString(P_UI, "v2");
        ShellRules.UiSwitch what = ShellRules.uiSwitch(saved, ui, ShellRules.uiShown(web.getUrl()));
        hideMenu();
        if (what == ShellRules.UiSwitch.NOTHING) {
            Log.i(TAG, "page " + ui + " chosen: already shown, nothing to do");
            return;
        }
        prefs.edit().putString(P_UI, ui).apply();
        toast("v1".equals(ui) ? R.string.toast_ui_v1 : R.string.toast_ui_v2);
        if (what == ShellRules.UiSwitch.REMEMBER) {
            Log.i(TAG, "page " + ui + " chosen: already shown, remembered, no reload");
            return;
        }
        v2Missing = false;
        if ("v1".equals(ui)) cutoutToPage = false;   // v1 does not know about the cutout
        Log.i(TAG, "page " + ui + " chosen: " + pageUrl());
        applyWindowMode();
        if (trust != null && link.isUp()) {
            closePort();
            loadPage(false);
        } else {
            pageRequested = false;   // loaded when K1000 is up
        }
    }

    @SuppressLint("SetJavaScriptEnabled")   // the page is the box's own, over pinned TLS
    private void setupWebView() {
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setGeolocationEnabled(false);
        s.setSupportMultipleWindows(false);
        s.setJavaScriptCanOpenWindowsAutomatically(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setUserAgentString(s.getUserAgentString() + " K1000-Android/" + BuildConfig.VERSION_NAME);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            s.setAlgorithmicDarkeningAllowed(false);   // the page is dark already
        }
        // The page has its own long-press ("notch here"): no text selection, no extra buzz.
        web.setLongClickable(false);
        web.setOnLongClickListener(v -> true);
        web.setHapticFeedbackEnabled(false);
        web.setBackgroundColor(0xff000000);

        web.setWebViewClient(new BoxClient());
        web.setWebChromeClient(new BoxChrome());
        // The page's exclusion rects are in its viewport: re-apply them when the WebView resizes.
        web.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> {
            if (pageRects != null && (r - l != or - ol || b - t != ob - ot)) applyExclusion();
        });

        // The page's service worker (offline shell): same rules as the page.
        ServiceWorkerController swc = ServiceWorkerController.getInstance();
        ServiceWorkerWebSettings sws = swc.getServiceWorkerWebSettings();
        sws.setAllowContentAccess(false);
        sws.setAllowFileAccess(false);
        swc.setServiceWorkerClient(new ServiceWorkerClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) {
                return blockIfForeign(request);
            }
        });
    }

    /** Anything that is not the box gets an empty 403 inside the WebView. */
    static WebResourceResponse blockIfForeign(WebResourceRequest request) {
        Uri u = request.getUrl();
        String scheme = u.getScheme() == null ? "" : u.getScheme();
        if (scheme.equals("data") || scheme.equals("blob")) return null;
        if (K1000Hosts.isBoxUrl(u.toString())) return null;
        return new WebResourceResponse("text/plain", "utf-8", 403, "Not K1000",
                Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
    }

    private final class BoxClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            String url = request.getUrl().toString();
            if (K1000Hosts.isBoxUrl(url)) return false;
            String scheme = request.getUrl().getScheme();
            if ("https".equalsIgnoreCase(scheme) || "http".equalsIgnoreCase(scheme)) {
                // Not the box: the phone's browser, on the phone's normal network.
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, request.getUrl())
                            .addCategory(Intent.CATEGORY_BROWSABLE));
                } catch (ActivityNotFoundException e) {
                    Log.w(TAG, "no browser for " + url);
                }
            }
            return true;
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            return blockIfForeign(request);
        }

        @Override
        public void onPageStarted(WebView view, String url, Bitmap favicon) {
            // A new document: the previous one's port, overlays and exclusions are gone with it.
            closePort();
            bridgeReady = false;
            pageCaps = Collections.emptyList();
            main.removeCallbacks(backTimeout);
            backPendingId = -1;
            pageRects = null;
            applyExclusion();
            setKeepAwake(true);   // each page asks for itself; the screen stays on until it does
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            Log.i(TAG, "page loaded: " + url);
            if (!pageFailed) pageRetries = 0;
            if (K1000Hosts.isBoxUrl(url)) {
                onFallbackHost = K1000Hosts.hostOf(url).equals(K1000Hosts.fallbackHost());
                if (!pageFailed) {
                    handshakes = 0;
                    startBridge();
                }
            }
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (!request.isForMainFrame()) return;
            String url = request.getUrl().toString();
            Log.i(TAG, "page HTTP " + response.getStatusCode() + " for " + url);
            if (response.getStatusCode() == 404 && !v2Missing && K1000Hosts.isBoxUrl(url)
                    && K1000Hosts.pathOf(url).startsWith(K1000Hosts.PATH_V2)) {
                // A box without the v2 page yet: show v1 for this session (the choice stays v2).
                Log.i(TAG, "no " + K1000Hosts.PATH_V2 + " on this box: the classic page (v1) for now");
                v2Missing = true;
                toast(R.string.toast_no_v2);
                main.post(() -> loadPage(false));
            }
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (!request.isForMainFrame()) return;
            Log.i(TAG, "page error " + error.getErrorCode() + " " + error.getDescription() + " for " + request.getUrl());
            if (tlsRefused) {
                // After a refused certificate (newer WebViews report the cancelled load too):
                // keep "Not the K1000 box" on screen, and do not try the box's name instead.
                pageFailed = true;
                return;
            }
            if (!triedFallback && error.getErrorCode() != ERROR_FAILED_SSL_HANDSHAKE) {
                // 10.42.0.1 did not answer: try the box's name once (served by the K1000 DNS).
                // (A bench build: its bench host did not answer, try the box's address.)
                triedFallback = true;
                onFallbackHost = true;
                view.loadUrl(pageUrl());
                return;
            }
            pageFailed = true;
            onFallbackHost = false;
            if (link.isUp()) {
                showStatus(R.string.st_not_connected_title,
                        getString(R.string.st_page_error_text, String.valueOf(error.getDescription())), pageRetries < 5, true);
                if (pageRetries < 5) {
                    // A just-joined network can drop the first connections (and with no default
                    // network WebView reports them as ERR_INTERNET_DISCONNECTED): try again.
                    pageRetries++;
                    main.postDelayed(() -> {
                        if (pageFailed && !tlsRefused && link.isUp()) {
                            hideStatus();
                            loadPage(true);
                        }
                    }, 2000L * pageRetries);
                }
            }
        }

        @Override
        @SuppressLint("WebViewClientOnReceivedSslError")   // proceeds only on a passed K1000Trust check
        public void onReceivedSslError(WebView view, SslErrorHandler handler, SslError error) {
            // Only reached when Android's own validation (the network security config, which
            // trusts the K1000 CA for the K1000 hosts only) refused the certificate. Proceed only
            // when the explicit check against the bundled CA passes; otherwise cancel.
            String host = K1000Hosts.hostOf(error.getUrl());
            if (!K1000Hosts.isBoxHost(host)) {
                // Another host (only reachable outside shouldInterceptRequest, e.g. a WebSocket):
                // the app trusts no CA for it. Refuse quietly; the box's page stays as it is.
                Log.w(TAG, "TLS: refused a connection to another host (" + host + ")");
                handler.cancel();
                return;
            }
            X509Certificate leaf = leafOf(error.getCertificate());
            try {
                if (trust == null) throw new java.security.cert.CertificateException(trustError);
                if (leaf == null) throw new java.security.cert.CertificateException("no certificate");
                trust.verify(new X509Certificate[] {leaf}, host, new Date());
                Log.w(TAG, "TLS: platform error " + error.getPrimaryError() + " for " + host
                        + ", but the K1000 CA check passed: proceeding");
                handler.proceed();
            } catch (Exception e) {
                Log.w(TAG, "TLS: refused " + host + " (" + e.getMessage() + ")");
                tlsRefused = true;
                handler.cancel();
                pageFailed = true;
                showStatus(R.string.st_tls_title, getString(R.string.st_tls_text, e.getMessage()), false, true);
            }
        }
    }

    private static X509Certificate leafOf(SslCertificate c) {
        return c == null ? null : c.getX509Certificate();
    }

    private final class BoxChrome extends WebChromeClient {
        @Override
        public void onPermissionRequest(PermissionRequest request) {
            // Microphone only, only for the box's page (the phone-mic spectrum); never camera,
            // MIDI or protected media.
            String origin = String.valueOf(request.getOrigin());
            boolean audioOnly = request.getResources().length > 0;
            for (String r : request.getResources()) {
                audioOnly &= PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(r);
            }
            if (!audioOnly || !K1000Hosts.isBoxOrigin(origin)) {
                Log.i(TAG, "denied page permission " + TextUtils.join(",", request.getResources()) + " for " + origin);
                request.deny();
                return;
            }
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
                request.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                return;
            }
            if (pendingAudio != null) pendingAudio.deny();
            pendingAudio = request;
            requestPermissions(new String[] {Manifest.permission.RECORD_AUDIO}, REQ_AUDIO);
        }

        @Override
        public void onPermissionRequestCanceled(PermissionRequest request) {
            if (request == pendingAudio) pendingAudio = null;
        }

        @Override
        public boolean onConsoleMessage(android.webkit.ConsoleMessage m) {
            if (BuildConfig.DEBUG) Log.d(TAG, "console: " + m.message() + " (" + m.sourceId() + ":" + m.lineNumber() + ")");
            return true;
        }
    }

    // --------------------------------------------------------------- permissions and results

    /**
     * Android 17+: the page is on the local network, which needs the user's permission. Asked
     * once by itself; after that only from Retry (a refused request resumes the activity, and
     * asking again from onResume would loop).
     */
    private boolean localNetworkAllowed() {
        return localNetworkAllowed(!localNetworkAsked);
    }

    private boolean localNetworkAllowed(boolean ask) {
        if (Build.VERSION.SDK_INT < API_LOCAL_NETWORK) return true;
        if (checkSelfPermission(PERM_LOCAL_NETWORK) == PackageManager.PERMISSION_GRANTED) return true;
        showStatus(R.string.st_permission_title, getString(R.string.st_permission_text), false, true);
        if (ask) {
            localNetworkAsked = true;
            requestPermissions(new String[] {PERM_LOCAL_NETWORK}, REQ_LOCAL_NETWORK);
        }
        return false;
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        boolean granted = grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED;
        if (requestCode == REQ_CAMERA) {
            if (granted) startScan();
            else setupError(getString(R.string.err_no_camera));
        } else if (requestCode == REQ_AUDIO) {
            PermissionRequest r = pendingAudio;
            pendingAudio = null;
            if (r != null) {
                if (granted) r.grant(new String[] {PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                else r.deny();
            }
        } else if (requestCode == REQ_LOCAL_NETWORK) {
            if (granted) {
                if (wantLink) startLink();
            } else if (!shouldShowRequestPermissionRationale(PERM_LOCAL_NETWORK)) {
                // "Don't ask again": only the app's settings page can grant it now.
                statusRetry.setOnClickListener(v -> {
                    statusRetry.setOnClickListener(w -> retry());
                    startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                            Uri.fromParts("package", getPackageName(), null)));
                });
            }
        }
    }

    // ------------------------------------------------------------------------------- back key

    /**
     * Back is K1000's own: the system's default (finish) never runs. API 34+: the predictive back
     * callback also tells how long a back gesture was held. API 33: the plain callback. API 29-32:
     * the BACK key itself (dispatchKeyEvent), held or not.
     */
    private void setupBack() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT,
                    new OnBackAnimationCallback() {
                        @Override
                        public void onBackStarted(BackEvent e) {
                            backStartedAt = SystemClock.uptimeMillis();
                        }

                        @Override
                        public void onBackCancelled() {
                            backStartedAt = 0;
                        }

                        @Override
                        public void onBackInvoked() {
                            boolean hold = backStartedAt > 0 && SystemClock.uptimeMillis() - backStartedAt >= BACK_HOLD_MS;
                            backStartedAt = 0;
                            back(hold);
                        }
                    });
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    OnBackInvokedDispatcher.PRIORITY_DEFAULT, () -> back(false));
        }
    }

    private final Runnable backKeyHold = () -> {
        backKeyHoldFired = true;
        back(true);
    };

    /** API 29-32: a BACK key held 1 s hides K1000 (at the page's top level); a press is Back. */
    @Override
    public boolean dispatchKeyEvent(KeyEvent e) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU && e.getKeyCode() == KeyEvent.KEYCODE_BACK) {
            if (e.getAction() == KeyEvent.ACTION_DOWN) {
                if (e.getRepeatCount() == 0) {
                    backKeyHoldFired = false;
                    main.postDelayed(backKeyHold, BACK_HOLD_MS);
                }
                return true;
            }
            if (e.getAction() == KeyEvent.ACTION_UP) {
                main.removeCallbacks(backKeyHold);
                if (!backKeyHoldFired && !e.isCanceled()) back(false);
                backKeyHoldFired = false;
                return true;
            }
        }
        return super.dispatchKeyEvent(e);
    }

    /** API 29-32 fallback (the BACK key goes through dispatchKeyEvent above). */
    @Override
    @SuppressWarnings("deprecation")
    @SuppressLint("GestureBackNavigation")
    public void onBackPressed() {
        back(false);
    }

    /**
     * Back never closes K1000 mid-show (that would drop the Wi-Fi link): the app's own panels
     * first, then the page's overlays ({t:"back"}, android/BRIDGE.md), and only at the page's top
     * level: the app's menu with "Back again: hide". A second Back within 2 s, or a 1 s hold,
     * hides K1000 (moveTaskToBack, never finish).
     */
    private void back(boolean hold) {
        if (edgeTest) {
            edgeBacks++;
            Log.i(TAG, "edge test: Back fired (" + edgeBacks + ")");
            updateEdgeText();
            return;
        }
        if (aboutPanel.getVisibility() == View.VISIBLE) {
            aboutPanel.setVisibility(View.GONE);
            return;
        }
        if (scanPanel.getVisibility() == View.VISIBLE) {
            stopScan();
            return;
        }
        if (setupPanel.getVisibility() == View.VISIBLE && hasLinkConfig()) {
            closeSetup();
            return;
        }
        if (diagPanel.getVisibility() == View.VISIBLE) {
            hideDiag();
            return;
        }
        if (menuPanel.getVisibility() == View.VISIBLE) {
            if (hold || (lastTopBack > 0 && SystemClock.uptimeMillis() - lastTopBack < BACK_AGAIN_MS)) hideApp();
            else hideMenu();
            return;
        }
        if (backPendingId >= 0) return;   // the page has not answered the previous Back yet
        if (bridgeReady && pageCaps.contains("back") && statusPanel.getVisibility() != View.VISIBLE) {
            backPendingId = ++backSeq;
            backPendingHold = hold;
            send(Bridge.back(backPendingId));
            main.postDelayed(backTimeout, BACK_ANSWER_MS);
            return;
        }
        topLevelBack(hold);
    }

    private final Runnable backTimeout = () -> {
        if (backPendingId < 0) return;
        Log.i(TAG, "bridge: the page did not answer back " + backPendingId);
        backPendingId = -1;
        topLevelBack(backPendingHold);
    };

    private void onBackAnswer(long id, boolean handled) {
        if (id != backPendingId) return;
        main.removeCallbacks(backTimeout);
        backPendingId = -1;
        Log.i(TAG, "back: the page " + (handled ? "closed an overlay" : "is at its top level"));
        if (!handled) topLevelBack(backPendingHold);
    }

    private void topLevelBack(boolean hold) {
        long now = SystemClock.uptimeMillis();
        if (hold || (lastTopBack > 0 && now - lastTopBack < BACK_AGAIN_MS)) {
            hideApp();
            return;
        }
        lastTopBack = now;
        showMenu();
    }

    private void hideApp() {
        lastTopBack = 0;
        hideMenu();
        Log.i(TAG, "back: K1000 hidden (moveTaskToBack)");
        moveTaskToBack(true);
    }

    // ------------------------------------------------------------------- the app's own menu

    private final Runnable hideMenuLater = this::hideMenu;

    /** The top of the screen only: the page's footer (MUTE) stays visible and touchable. */
    private void showMenu() {
        menuTitle.setText(getString(R.string.menu_title, BuildConfig.VERSION_NAME));
        menuUi.setText(isV1() ? R.string.menu_ui_v2 : R.string.menu_ui_v1);
        menuPanel.setVisibility(View.VISIBLE);
        Log.i(TAG, "back: the app menu (Back again: hide)");
        main.removeCallbacks(hideMenuLater);
        main.postDelayed(hideMenuLater, MENU_MS);
    }

    private void hideMenu() {
        main.removeCallbacks(hideMenuLater);
        if (menuPanel != null) menuPanel.setVisibility(View.GONE);
    }

    // ------------------------------------------------------------------------ diagnostics

    private Map<String, Object> diagnostics() {
        Map<String, Object> d = Diagnostics.collect(this, root, web);
        d.put("page", String.valueOf(web.getUrl()));
        d.put("page_choice", prefs.getString(P_UI, "v2") + (v2Missing ? " (no /v2/ on this box: v1 shown)" : ""));
        d.put("bridge", bridgeReady ? "ready " + pageCaps : port != null ? "handshake sent, no answer" : "none");
        d.put("fullscreen", fullscreen);
        d.put("cutout_to_page", cutoutToPage);
        d.put("keep_awake", keepAwake);
        d.put("window_keep_screen_on", (getWindow().getAttributes().flags & WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON) != 0);
        d.put("link", link.isUp() ? "up (" + link.mode() + ")" : link.isBlocked() ? "blocked" : "down");
        d.put("haptics_count", haptics.played + " played, " + haptics.dropped + " dropped (rate), "
                + haptics.fallbacks + " via the vibrator, " + haptics.alarms + " answers via the alarm vibration (Touch feedback off)");
        if (!BuildConfig.BENCH_HOST.isEmpty()) d.put("bench_host", BuildConfig.BENCH_HOST);
        return d;
    }

    private void showDiag() {
        hideMenu();
        Map<String, Object> d = diagnostics();
        diagSummary.setText(Diagnostics.summary(d));
        diagText.setText(Diagnostics.text(d));
        diagPanel.setVisibility(View.VISIBLE);
    }

    private void hideDiag() {
        diagPanel.setVisibility(View.GONE);
    }

    private void copyDiag() {
        ClipboardManager cb = getSystemService(ClipboardManager.class);
        if (cb == null) return;
        cb.setPrimaryClip(ClipData.newPlainText("K1000 diagnostics", Diagnostics.text(diagnostics())));
        toast(R.string.toast_copied);
    }

    /** Every haptic kind once, named on screen while it plays (a detent run for tick). */
    private void testHaptics() {
        long t = 0;
        for (Bridge.Haptic k : Bridge.Haptic.values()) {
            int n = k == Bridge.Haptic.TICK ? 8 : 1;
            for (int i = 0; i < n; i++) {
                final boolean first = i == 0;
                main.postDelayed(() -> {
                    boolean ok = haptics.play(k);
                    if (first) diagStatus.setText(getString(R.string.diag_haptic_now, k.wire, ok ? "" : getString(R.string.diag_haptic_none)));
                }, t);
                t += k == Bridge.Haptic.TICK ? 60 : 0;
            }
            t += 900;
        }
        main.postDelayed(() -> diagStatus.setText(R.string.diag_haptic_done), t);
    }

    /**
     * Two amber bands excluded from Back, as the page's slide areas are: swipe in and see. The bars
     * show for the test: immersive keeps every edge swipe from Back (it only shows the bars), so a
     * test run with the bars hidden would pass with broken rects too.
     */
    private void startEdgeTest() {
        hideDiag();
        edgeTest = true;
        applyWindowMode();
        Log.i(TAG, "edge test: started, bars shown");
        edgeBacks = 0;
        edgeEndsAt = SystemClock.uptimeMillis() + EDGE_TEST_MS;
        WindowInsets wi = root.getRootWindowInsets();
        int w = (int) (32 * getResources().getDisplayMetrics().density);
        if (wi != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            android.graphics.Insets g = wi.getInsets(WindowInsets.Type.systemGestures());
            w = Math.max(w, Math.max(g.left, g.right));
        }
        for (View b : new View[] {edgeLeft, edgeRight}) {
            b.getLayoutParams().width = w;
            b.requestLayout();
        }
        edgePanel.setVisibility(View.VISIBLE);
        edgePanel.post(() -> {
            for (View b : new View[] {edgeLeft, edgeRight}) {
                b.setSystemGestureExclusionRects(List.of(new Rect(0, 0, b.getWidth(), b.getHeight())));
            }
        });
        updateEdgeText();
    }

    private final Runnable edgeTick = this::updateEdgeText;

    private void updateEdgeText() {
        main.removeCallbacks(edgeTick);
        if (!edgeTest) return;
        long left = edgeEndsAt - SystemClock.uptimeMillis();
        if (left <= 0) {
            endEdgeTest();
            return;
        }
        edgeText.setText(getString(R.string.edge_text, (int) Math.ceil(left / 1000.0), edgeBacks));
        main.postDelayed(edgeTick, 500);
    }

    private void endEdgeTest() {
        if (!edgeTest) return;
        edgeTest = false;
        main.removeCallbacks(edgeTick);
        for (View b : new View[] {edgeLeft, edgeRight}) b.setSystemGestureExclusionRects(Collections.emptyList());
        edgePanel.setVisibility(View.GONE);
        applyWindowMode();   // the page's own choice again (immersive by default)
        Log.i(TAG, "edge test: ended, Back count " + edgeBacks);
        showDiag();
        diagStatus.setText(getString(R.string.edge_result, edgeBacks));
    }

    // ---------------------------------------------------------------------- the page bridge

    /**
     * A fresh WebMessageChannel for the page now on screen: one end stays here, the other goes to
     * the page with the handshake {t:"native"}, posted to the box's origin only (never "*"). A page
     * that has not said {t:"ready"} gets the handshake again after 1.5 s and 4.5 s (a late
     * listener), on a new channel each time.
     */
    private void startBridge() {
        closePort();
        String origin = K1000Hosts.originOf(web.getUrl());
        if (origin == null || pageFailed) return;
        WebMessagePort[] ch;
        try {
            ch = web.createWebMessageChannel();
        } catch (RuntimeException e) {
            Log.w(TAG, "bridge: no message channel: " + e.getMessage());
            return;
        }
        final int gen = ++portGen;
        port = ch[0];
        port.setWebMessageCallback(new WebMessagePort.WebMessageCallback() {
            @Override
            public void onMessage(WebMessagePort p, WebMessage message) {
                onPageMessage(gen, message == null ? null : message.getData());
            }
        }, main);
        String ui = K1000Hosts.pathOf(web.getUrl()).startsWith(K1000Hosts.PATH_V2) ? "v2" : "v1";
        web.postWebMessage(new WebMessage(Bridge.hello(BuildConfig.VERSION_NAME, BuildConfig.BUILD_TYPE, ui),
                new WebMessagePort[] {ch[1]}), Uri.parse(origin));
        handshakes++;
        Log.i(TAG, "bridge: handshake " + handshakes + " posted to " + origin);
        if (handshakes < 3) {
            main.postDelayed(() -> {
                if (gen == portGen && !bridgeReady) startBridge();
            }, handshakes == 1 ? 1500 : 3000);
        }
    }

    private void closePort() {
        portGen++;
        WebMessagePort p = port;
        port = null;
        if (p != null) {
            try {
                p.close();
            } catch (RuntimeException e) {
                // already closed
            }
        }
    }

    private void send(String json) {
        WebMessagePort p = port;
        if (p == null) return;
        try {
            p.postMessage(new WebMessage(json));
        } catch (RuntimeException e) {
            Log.w(TAG, "bridge: send failed: " + e.getMessage());
        }
    }

    /** A message from the page (main thread). Only the current page's port is heard. */
    private void onPageMessage(int gen, String data) {
        if (gen != portGen || !K1000Hosts.isBoxUrl(web.getUrl())) return;
        Bridge.Msg m;
        try {
            m = Bridge.parse(data);
        } catch (Bridge.Invalid e) {
            Log.w(TAG, "bridge: refused " + (e.type.isEmpty() ? "a message" : e.type) + ": " + e.getMessage());
            send(Bridge.error(e.type, e.getMessage()));
            return;
        }
        if (!"haptic".equals(m.t) || BuildConfig.DEBUG) Log.i(TAG, "bridge: " + Bridge.describe(m));
        switch (m.t) {
            case "ready":
                bridgeReady = true;
                pageCaps = m.caps;
                sendInsets();
                break;
            case "haptic": {
                boolean ok = haptics.play(m.haptic);
                if (BuildConfig.DEBUG) Log.d(TAG, "bridge: haptic " + m.haptic.wire + " played=" + ok);
                break;
            }
            case "fullscreen":
                setFullscreen(m.on, m.cutout);
                break;
            case "keep_awake":
                setKeepAwake(m.on);
                break;
            case "exclusion":
                pageRects = m.rects;
                applyExclusion();
                break;
            case "diag":
                send(Bridge.diag(diagnostics()));
                if (m.show) showDiag();
                break;
            case "back_handled":
                onBackAnswer(m.id, m.handled);
                break;
            case "set_ui":
                switchUi(m.ui);
                break;
            default:
                break;
        }
    }

    // ------------------------------------------------------------------ launcher shortcuts

    /** Long-press the K1000 icon: the page choice and Diagnostics, without opening the page. */
    private void setupShortcuts() {
        ShortcutManager sm = getSystemService(ShortcutManager.class);
        if (sm == null) return;
        try {
            sm.setDynamicShortcuts(List.of(
                    shortcut("ui_v2", R.string.sc_v2_short, R.string.sc_v2_long, ACTION_UI_V2),
                    shortcut("ui_v1", R.string.sc_v1_short, R.string.sc_v1_long, ACTION_UI_V1),
                    shortcut("diag", R.string.sc_diag_short, R.string.sc_diag_long, ACTION_DIAG)));
        } catch (RuntimeException e) {
            Log.w(TAG, "launcher shortcuts not set: " + e.getMessage());
        }
    }

    private ShortcutInfo shortcut(String id, int shortLabel, int longLabel, String action) {
        return new ShortcutInfo.Builder(this, id)
                .setShortLabel(getString(shortLabel))
                .setLongLabel(getString(longLabel))
                .setIcon(Icon.createWithResource(this, R.mipmap.ic_launcher))
                .setIntent(new Intent(action).setClassName(this, ShellRules.SHORTCUT_ALIAS))
                .build();
    }

    // --------------------------------------------------------------------------------- trust

    private K1000Trust loadTrust() {
        try (InputStream in = getResources().openRawResource(R.raw.k1000_ca)) {
            return new K1000Trust(K1000Trust.readCertificate(in), K1000Hosts.HOSTS);
        } catch (Exception e) {
            trustError = String.valueOf(e.getMessage());
            Log.e(TAG, "bundled K1000 CA unusable", e);
            return null;
        }
    }

    // ------------------------------------------------------------------------- smoke test only

    /** The launching intent may point the link at the emulator's subnet before it starts. */
    private void smokeSetup(Intent intent) {
        String saved = prefs.getString("smoke_subnet", null);
        if (saved != null) link.smokeSubnet(saved);
        if (intent != null && "subnet".equals(intent.getStringExtra("smoke"))) smokeCommand(intent);
    }

    private void smokeCommand(Intent intent) {
        String cmd = intent.getStringExtra("smoke");
        if (cmd == null) return;
        Log.i(TAG, "smoke: " + cmd);
        switch (cmd) {
            case "unbind":
                link.smokeUnbind();
                break;
            case "rebind":
                link.smokeRebind();
                break;
            case "reload":
                loadPage(true);
                break;
            case "ui":
                switchUi("v1".equals(intent.getStringExtra("v")) ? "v1" : "v2");
                break;
            case "diag":
                showDiag();
                break;
            case "hidepanels":
                hideDiag();
                hideMenu();
                break;
            case "edgetest":
                startEdgeTest();
                break;
            case "edgeend":
                endEdgeTest();
                break;
            case "subnet": {
                // Fallback mode follows a Wi-Fi only on this subnet (the emulator is not 10.42.0.x).
                String cidr = intent.getStringExtra("cidr");
                prefs.edit().putString("smoke_subnet", cidr).apply();
                link.smokeSubnet(cidr);
                break;
            }
            case "refusebind":
                link.smokeRefuseBind(true);
                break;
            case "allowbind":
                link.smokeRefuseBind(false);
                break;
            case "qr":
                // As if this payload had been scanned (the join flow against an emulated K1000 AP).
                try {
                    WifiQr.parse(intent.getStringExtra("payload"));
                } catch (WifiQr.InvalidException e) {
                    Log.i(TAG, "smoke: qr refused: " + e.getMessage());
                }
                onScanned(intent.getStringExtra("payload"));
                break;
            case "trustcheck": {
                // Runs the explicit K1000 check on THIS device's crypto with the page's certificate.
                X509Certificate leaf = leafOf(web.getCertificate());
                try {
                    trust.verify(new X509Certificate[] {leaf}, K1000Hosts.hostOf(web.getUrl()), new Date());
                    Log.i(TAG, "smoke: trustcheck PASS for " + web.getUrl());
                } catch (Exception e) {
                    Log.i(TAG, "smoke: trustcheck FAIL for " + web.getUrl() + ": " + e.getMessage());
                }
                break;
            }
            default:
                break;
        }
    }
}
