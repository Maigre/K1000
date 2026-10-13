// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.Proxy;
import java.net.URL;
import java.net.URLConnection;
import java.security.cert.Certificate;
import java.security.cert.CertificateException;
import java.security.cert.X509Certificate;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLException;

/**
 * The network side of {@link BoxWifi}: while the Connect screen is up, it watches the phone's Wi-Fi
 * networks (as the fallback mode does: any Wi-Fi, with or without internet) and, when MainActivity
 * says so, asks the box for its Wi-Fi through the one on the K1000 subnet.
 *
 * <p>The request: {@code Network.openConnection} (that network's sockets and DNS, no proxy), HTTPS
 * only, to the box's two names only, no redirect followed, no cache. TLS must pass twice: the
 * network security config (the K1000 CA for the box's names, nothing else) during the handshake,
 * then {@link K1000Trust} on the leaf the box showed (the CA's name constraints included) before
 * the answer is read. A refused certificate ({@link BoxWifi#certificateRefused}) is final for that
 * network: the other name is not tried. A connection that drops inside TLS (during or after the
 * handshake) is no answer, like any other I/O error: the other name, then RETRY. The app sends
 * nothing but the GET itself.
 *
 * <p>Which answers count (the Connect screen it was asked on) is {@link BoxWifi.Tries}'s: this
 * class only runs the requests, one at a time, and hands every answer back with its token.
 * Listener calls arrive on the main thread, never after {@link #close}.
 */
final class BoxWifiProbe {

    private static final String TAG = "K1000";

    interface Listener {
        /** The Wi-Fi networks or their addresses changed. */
        void onWifiChanged();

        /** The answer of {@code network} to the request {@code token} (every request's, once). */
        void onBoxWifi(int token, Network network, BoxWifi.Result result);
    }

    private final ConnectivityManager cm;
    private final Listener listener;
    private final K1000Trust trust;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newSingleThreadExecutor();
    /** Every Wi-Fi network seen, with its latest link properties (null: none yet). */
    private final Map<Network, LinkProperties> wifi = new LinkedHashMap<>();
    private ConnectivityManager.NetworkCallback callback;
    private boolean closed;

    BoxWifiProbe(Context context, Listener listener, K1000Trust trust) {
        this.cm = context.getSystemService(ConnectivityManager.class);
        this.listener = listener;
        this.trust = trust;
    }

    /** Starts watching the Wi-Fi networks (the Connect screen is up). */
    void watch() {
        if (callback != null) return;
        ConnectivityManager.NetworkCallback cb = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network n) {
                if (callback != this) return;
                if (!wifi.containsKey(n)) wifi.put(n, null);
            }

            @Override
            public void onLinkPropertiesChanged(Network n, LinkProperties lp) {
                if (callback != this) return;
                wifi.put(n, lp);
                listener.onWifiChanged();
            }

            @Override
            public void onLost(Network n) {
                if (callback != this) return;
                wifi.remove(n);
                listener.onWifiChanged();
            }
        };
        callback = cb;
        NetworkRequest req = new NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build();
        try {
            cm.registerNetworkCallback(req, cb, main);
        } catch (RuntimeException e) {
            callback = null;
            Log.w(TAG, "box wifi: cannot watch the Wi-Fi networks: " + e.getMessage());
        }
    }

    /** Stops watching (the Connect screen closed). A running request still answers (Tries decides). */
    void unwatch() {
        ConnectivityManager.NetworkCallback cb = callback;
        callback = null;
        wifi.clear();
        if (cb != null) {
            try {
                cm.unregisterNetworkCallback(cb);
            } catch (IllegalArgumentException e) {
                // already gone
            }
        }
    }

    /** The first Wi-Fi network with an IPv4 address on one of {@code subnets}, or null. */
    Network onSubnet(List<String> subnets) {
        for (Map.Entry<Network, LinkProperties> e : wifi.entrySet()) {
            if (K1000Link.onSubnet(e.getValue(), subnets)) return e.getKey();
        }
        return null;
    }

    /** Asks the box through {@code n}; the answer comes to the listener with {@code token}. */
    void ask(Network n, int token) {
        if (closed) return;
        io.execute(() -> {
            BoxWifi.Result r;
            try {
                r = fetch(n);
            } catch (RuntimeException e) {
                // e.g. a network gone mid-request: never a crash of the app, asked again later.
                Log.w(TAG, "box wifi: request failed: " + e);
                r = BoxWifi.Result.of(BoxWifi.Verdict.RETRY, e.getClass().getSimpleName());
            }
            final BoxWifi.Result answer = r;
            main.post(() -> {
                if (!closed) listener.onBoxWifi(token, n, answer);
            });
        });
    }

    void close() {
        closed = true;
        unwatch();
        io.shutdownNow();
        main.removeCallbacksAndMessages(null);
    }

    /** The request itself (background thread). */
    private BoxWifi.Result fetch(Network n) {
        IOException last = null;
        for (String url : BoxWifi.urls()) {
            String host = K1000Hosts.hostOf(url);
            HttpsURLConnection h = null;
            try {
                URLConnection uc = n.openConnection(new URL(url), Proxy.NO_PROXY);
                if (!(uc instanceof HttpsURLConnection)) return BoxWifi.Result.of(BoxWifi.Verdict.REFUSED, "not HTTPS");
                h = (HttpsURLConnection) uc;
                h.setInstanceFollowRedirects(false);
                h.setUseCaches(false);
                h.setConnectTimeout(BoxWifi.CONNECT_TIMEOUT_MS);
                h.setReadTimeout(BoxWifi.READ_TIMEOUT_MS);
                h.setRequestProperty("Accept", "application/json");
                h.setRequestProperty("Cache-Control", "no-store");
                h.connect();
                // The handshake passed the network security config; now the explicit K1000 check.
                X509Certificate leaf = leafOf(h.getServerCertificates());
                try {
                    if (trust == null || leaf == null) throw new CertificateException("no certificate to check");
                    trust.verify(new X509Certificate[] {leaf}, host, new Date());
                } catch (CertificateException e) {
                    Log.w(TAG, "box wifi: TLS refused " + host + " (" + e.getMessage() + ")");
                    return BoxWifi.Result.of(BoxWifi.Verdict.REFUSED, String.valueOf(e.getMessage()));
                }
                int status = h.getResponseCode();
                byte[] body = status == 200 ? read(h.getInputStream()) : new byte[0];
                return BoxWifi.judge(status, h.getContentType(), body);
            } catch (SSLException e) {
                Log.i(TAG, "box wifi: TLS failure at " + host + ": " + BoxWifi.chain(e, 600));
                if (BoxWifi.certificateRefused(e)) {
                    // The pin failed during the handshake: not the box. Never try the other name.
                    Log.w(TAG, "box wifi: TLS refused " + host + " (" + e.getMessage() + ")");
                    return BoxWifi.Result.of(BoxWifi.Verdict.REFUSED, String.valueOf(e.getMessage()));
                }
                // The connection dropped inside TLS (closed or reset by the peer, during or after
                // the handshake): no answer, as any I/O error (review 2026-10-13: it read as a fake box).
                last = e;
                Log.i(TAG, "box wifi: " + host + " did not answer (" + e + ")");
            } catch (IOException e) {
                last = e;
                Log.i(TAG, "box wifi: " + host + " did not answer (" + e + ")");
            } finally {
                if (h != null) h.disconnect();
            }
        }
        return BoxWifi.Result.of(BoxWifi.Verdict.RETRY, last == null ? "no answer" : last.getClass().getSimpleName());
    }

    private static X509Certificate leafOf(Certificate[] chain) {
        return chain != null && chain.length > 0 && chain[0] instanceof X509Certificate ? (X509Certificate) chain[0] : null;
    }

    /** At most MAX_BODY_BYTES + 1 bytes: one more than allowed is enough to refuse it. */
    private static byte[] read(InputStream in) throws IOException {
        try (InputStream i = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[1024];
            int left = BoxWifi.MAX_BODY_BYTES + 1;
            for (int k; left > 0 && (k = i.read(buf, 0, Math.min(buf.length, left))) > 0; ) {
                out.write(buf, 0, k);
                left -= k;
            }
            return out.toByteArray();
        }
    }
}
