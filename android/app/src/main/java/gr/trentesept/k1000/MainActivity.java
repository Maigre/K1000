// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import android.Manifest;
import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Network;
import android.net.Uri;
import android.net.http.SslCertificate;
import android.net.http.SslError;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.TextUtils;
import android.util.Log;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.webkit.PermissionRequest;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import android.webkit.ServiceWorkerWebSettings;
import android.webkit.SslErrorHandler;
import android.webkit.WebChromeClient;
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
import java.util.Collections;
import java.util.Date;
import java.util.List;

/**
 * The K1000 app: one activity, one full-screen WebView showing the page the box serves at
 * https://10.42.0.1/ (the same PWA as in Chrome), plus three native things the PWA cannot do on
 * its own: join the K1000 Wi-Fi and bind to it (mobile data stays on), trust the K1000 CA for the
 * box only (no CA install in Android settings), and keep the screen on. See docs/09-android.md.
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
    private String pageUrl = K1000Hosts.URL_PRIMARY;
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
        setContentView(R.layout.activity_main);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        edgeToEdge();

        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        link = new K1000Link(this, this);
        trust = loadTrust();

        bindViews();
        setupWebView();
        setupBack();
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
    protected void onDestroy() {
        main.removeCallbacksAndMessages(null);
        unregisterReceiver(wifiState);
        link.stop();
        if (pendingAudio != null) pendingAudio.deny();
        web.destroy();
        super.onDestroy();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (BuildConfig.SMOKE) smokeCommand(intent);
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

    /** Draw behind the system bars everywhere (Android 15+ always does) and pad the content. */
    @SuppressWarnings("deprecation")   // setDecorFitsSystemWindows: only called below API 35
    private void edgeToEdge() {
        View root = findViewById(R.id.root);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.VANILLA_ICE_CREAM) {
                getWindow().setDecorFitsSystemWindows(false);
            }
            root.setOnApplyWindowInsetsListener((v, insets) -> {
                android.graphics.Insets i = insets.getInsets(WindowInsets.Type.systemBars()
                        | WindowInsets.Type.displayCutout() | WindowInsets.Type.ime());
                v.setPadding(i.left, i.top, i.right, i.bottom);
                return WindowInsets.CONSUMED;
            });
        }
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

    private void loadPage(boolean reload) {
        pageRequested = true;
        pageFailed = false;
        tlsRefused = false;
        if (reload && pageUrl.equals(web.getUrl())) {
            web.reload();
        } else {
            triedFallback = pageUrl.equals(K1000Hosts.URL_FALLBACK);
            web.loadUrl(pageUrl);
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
        public void onPageFinished(WebView view, String url) {
            Log.i(TAG, "page loaded: " + url);
            if (!pageFailed) pageRetries = 0;
            if (K1000Hosts.isBoxUrl(url)) pageUrl = url.startsWith(K1000Hosts.URL_FALLBACK)
                    ? K1000Hosts.URL_FALLBACK : K1000Hosts.URL_PRIMARY;
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
                triedFallback = true;
                pageUrl = K1000Hosts.URL_FALLBACK;
                view.loadUrl(pageUrl);
                return;
            }
            pageFailed = true;
            pageUrl = K1000Hosts.URL_PRIMARY;
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

    private void setupBack() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                    OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::back);
        }
    }

    /**
     * API 29-32 only: there, back gestures still call onBackPressed. From API 33 the manifest's
     * enableOnBackInvokedCallback routes back to the OnBackInvokedCallback registered above.
     */
    @Override
    @SuppressWarnings("deprecation")
    @SuppressLint("GestureBackNavigation")
    public void onBackPressed() {
        back();
    }

    /** Back never closes K1000 mid-show (that would drop the Wi-Fi link): it only hides it. */
    private void back() {
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
        moveTaskToBack(true);
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
