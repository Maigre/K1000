// SPDX-License-Identifier: GPL-3.0-or-later
package gr.trentesept.k1000;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.wifi.WifiNetworkSpecifier;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import java.net.Inet4Address;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The phone's link to the box: the K1000 Wi-Fi, and this process bound to it.
 *
 * <p>{@link Mode#JOIN} (the default) asks Android for the K1000 network itself with a
 * {@link WifiNetworkSpecifier} (SSID + WPA2 passphrase from the box's QR code). Android shows its
 * "connect to K1000" dialog the first time and remembers the approval for this app and network,
 * so later joins (after a dropout, or the next gig) need no tap. Such a network never carries
 * internet and never becomes the phone's default: mobile data stays on for everything else.
 *
 * <p>{@link Mode#SYSTEM} is the fallback: the user joins K1000 in Android's Wi-Fi settings, and
 * the app follows a Wi-Fi network only once it has an IPv4 address on the K1000 subnet
 * ({@link K1000Hosts#SUBNET}). Any other Wi-Fi (home, venue: what Android joins by itself while
 * the box is off) is ignored, and losing such a network does not touch the K1000 link.
 *
 * <p>Either way the whole process is bound to the network ({@code bindProcessToNetwork}), so the
 * WebView (whose network stack runs in this process) reaches 10.42.0.1 over K1000. When the
 * network is lost the binding is kept on purpose: the page's reconnection attempts then fail at
 * once instead of leaking over mobile data, until K1000 is back.
 *
 * <p>Android can refuse the binding (a VPN that apps may not bypass): K1000 is then
 * {@link #isBlocked() blocked}, the listener hears {@link Listener#onLinkBlocked()} instead of
 * {@link Listener#onLinkUp}, and the binding is tried again every few seconds, on every
 * capability change of the network and on {@link #retryBind()}.
 *
 * <p>All callbacks arrive on the main thread.
 */
final class K1000Link {

    private static final String TAG = "K1000";
    private static final long BIND_RETRY_MS = 3000L;

    enum Mode { JOIN, SYSTEM }

    interface Listener {
        /** The K1000 network is up and this process is bound to it. */
        void onLinkUp(Network network);

        /** The K1000 network went away (out of range, Wi-Fi off, box off). */
        void onLinkLost();

        /** The join was declined in Android's dialog, or could not be made. */
        void onLinkUnavailable();

        /** K1000 is up, but Android refused to bind this process to it (a VPN, typically). */
        void onLinkBlocked();
    }

    private final ConnectivityManager cm;
    private final Listener listener;
    private final Handler main = new Handler(Looper.getMainLooper());
    private final Runnable bindRetry = this::retryBind;

    private ConnectivityManager.NetworkCallback callback;
    private Mode mode;
    /** The accepted K1000 network (bound to this process, or blocked). */
    private Network current;
    /** {@link #current} is up but the binding was refused. */
    private boolean blocked;
    /** The network this process is bound to, as far as Android said yes (it may be gone). */
    private Network bound;
    /** SYSTEM mode: every Wi-Fi network seen, with its latest link properties (null: none yet). */
    private final Map<Network, LinkProperties> candidates = new LinkedHashMap<>();
    /**
     * The subnets a SYSTEM-mode network must be on: K1000's (a bench build adds the bench host's;
     * the smoke build points it at the emulator's Wi-Fi).
     */
    private List<String> subnets = K1000Hosts.linkSubnets();
    /** Smoke build only: binds "fail" as under a VPN that apps may not bypass. */
    private boolean smokeBindRefused;

    K1000Link(Context context, Listener listener) {
        this.cm = context.getSystemService(ConnectivityManager.class);
        this.listener = listener;
    }

    boolean isActive() {
        return callback != null;
    }

    /** K1000 is up and this process is bound to it: the page can reach the box. */
    boolean isUp() {
        return current != null && !blocked;
    }

    /** K1000 is up, but Android does not let this process use it. */
    boolean isBlocked() {
        return current != null && blocked;
    }

    /** K1000 is up (usable or blocked). */
    boolean hasNetwork() {
        return current != null;
    }

    Network network() {
        return current;
    }

    Mode mode() {
        return mode;
    }

    /** (Re)starts the link; any previous request is released first (the binding is kept). */
    void start(Mode m, WifiQr.Credentials creds) {
        release();
        mode = m;
        NetworkRequest.Builder rb = new NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
        Callback cb = new Callback();
        callback = cb;
        if (m == Mode.JOIN) {
            WifiNetworkSpecifier.Builder sb = new WifiNetworkSpecifier.Builder()
                    .setSsid(creds.ssid)
                    .setWpa2Passphrase(creds.passphrase);
            if (creds.hidden) sb.setIsHiddenSsid(true);
            rb.setNetworkSpecifier(sb.build());
            Log.i(TAG, "requesting the K1000 Wi-Fi (" + creds.ssid + ")");
            cm.requestNetwork(rb.build(), cb, main);
        } else {
            Log.i(TAG, "following the Wi-Fi joined in Android settings, on " + subnets + " only");
            cm.registerNetworkCallback(rb.build(), cb, main);
        }
    }

    /** Releases the request (Android then leaves a network it joined for us). Keeps the binding. */
    void release() {
        ConnectivityManager.NetworkCallback cb = callback;
        callback = null;
        current = null;
        blocked = false;
        candidates.clear();
        main.removeCallbacks(bindRetry);
        if (cb != null) {
            try {
                cm.unregisterNetworkCallback(cb);
            } catch (IllegalArgumentException e) {
                // already released
            }
        }
    }

    /** Releases everything, the process binding included (the app is going away). */
    void stop() {
        release();
        unbind();
    }

    /** True when the network we are bound to is still alive. */
    boolean boundNetworkAlive() {
        return bound != null && cm.getNetworkCapabilities(bound) != null;
    }

    void unbind() {
        if (bound != null || cm.getBoundNetworkForProcess() != null) {
            cm.bindProcessToNetwork(null);
            Log.i(TAG, "process unbound");
        }
        bound = null;
    }

    /** Blocked: try the binding again now (resume, Retry, the periodic retry). */
    void retryBind() {
        main.removeCallbacks(bindRetry);
        if (callback == null || current == null || !blocked) return;
        if (bind(current)) {
            blocked = false;
            Log.i(TAG, "K1000 link up (" + mode + "), binding allowed again");
            listener.onLinkUp(current);
        } else {
            main.postDelayed(bindRetry, BIND_RETRY_MS);
        }
    }

    /** True when this process is now bound to {@code n}. */
    private boolean bind(Network n) {
        if (n.equals(cm.getBoundNetworkForProcess()) && !smokeBindRefused) {
            bound = n;
            return true;
        }
        boolean ok = !smokeBindRefused && cm.bindProcessToNetwork(n);
        // On a refusal Android keeps the previous binding (if any): requests keep failing fast.
        bound = ok ? n : cm.getBoundNetworkForProcess();
        Log.i(TAG, "process bound to network " + n + ": " + ok);
        return ok;
    }

    /** {@code n} becomes the K1000 network: bind, then tell the listener up or blocked. */
    private void accept(Network n) {
        current = n;
        main.removeCallbacks(bindRetry);
        if (bind(n)) {
            blocked = false;
            Log.i(TAG, "K1000 link up (" + mode + "), K1000 address: " + onSubnet(cm.getLinkProperties(n)));
            listener.onLinkUp(n);
        } else {
            blocked = true;
            Log.w(TAG, "K1000 network up (" + mode + ") but Android refused to bind this app to it (VPN?)");
            listener.onLinkBlocked();
            main.postDelayed(bindRetry, BIND_RETRY_MS);
        }
    }

    private void lose() {
        current = null;
        blocked = false;
        main.removeCallbacks(bindRetry);
        Log.i(TAG, "K1000 link lost");
        listener.onLinkLost();
    }

    /** SYSTEM mode: the first candidate on the K1000 subnet, or null. */
    private Network firstOnSubnet() {
        for (Map.Entry<Network, LinkProperties> e : candidates.entrySet()) {
            if (onSubnet(e.getValue())) return e.getKey();
        }
        return null;
    }

    /** SYSTEM mode: take a qualifying network when there is none, drop one that stopped qualifying. */
    private void reconsider() {
        ConnectivityManager.NetworkCallback cb = callback;
        if (current != null && !onSubnet(candidates.get(current))) {
            Log.i(TAG, "network " + current + " left the K1000 subnet");
            lose();
        }
        if (current == null && callback == cb && cb != null) {
            Network n = firstOnSubnet();
            if (n != null) accept(n);
        }
    }

    private boolean onSubnet(LinkProperties lp) {
        return onSubnet(lp, subnets);
    }

    /** True when {@code lp} has an IPv4 address on one of {@code subnets} (also BoxWifiProbe's test). */
    static boolean onSubnet(LinkProperties lp, List<String> subnets) {
        if (lp == null) return false;
        for (LinkAddress la : lp.getLinkAddresses()) {
            if (!(la.getAddress() instanceof Inet4Address)) continue;
            for (String subnet : subnets) {
                if (K1000Hosts.inSubnet(la.getAddress().getAddress(), subnet)) return true;
            }
        }
        return false;
    }

    // ------------------------------------------------------------------------- smoke test only

    /** Drop / restore the binding while the link stays up. */
    void smokeUnbind() {
        cm.bindProcessToNetwork(null);
        bound = null;
        Log.i(TAG, "smoke: process unbound");
    }

    void smokeRebind() {
        if (current != null) bind(current);
    }

    /** Point SYSTEM mode at another subnet (the emulator's Wi-Fi is not 10.42.0.0/24). */
    void smokeSubnet(String cidr) {
        subnets = List.of(cidr);
        Log.i(TAG, "smoke: K1000 subnet is now " + cidr);
        if (callback != null && mode == Mode.SYSTEM) reconsider();
    }

    /** Make binds fail (as under a VPN that apps may not bypass), or work again. */
    void smokeRefuseBind(boolean refuse) {
        smokeBindRefused = refuse;
        Log.i(TAG, "smoke: binding refused: " + refuse);
    }

    private final class Callback extends ConnectivityManager.NetworkCallback {
        @Override
        public void onAvailable(Network network) {
            if (callback != this) return;
            if (mode == Mode.JOIN) {
                // The specifier matched K1000's SSID and passphrase: this is the box's network.
                accept(network);
            } else if (!candidates.containsKey(network)) {
                // Its addresses follow in onLinkPropertiesChanged (Android 8+ sends them next).
                candidates.put(network, null);
            }
        }

        @Override
        public void onLinkPropertiesChanged(Network network, LinkProperties lp) {
            if (callback != this || mode != Mode.SYSTEM) return;
            boolean was = onSubnet(candidates.get(network));
            candidates.put(network, lp);
            if (!was && onSubnet(lp)) Log.i(TAG, "Wi-Fi network " + network + " is on the K1000 subnet");
            else if (current == null && !onSubnet(lp)) Log.i(TAG, "ignoring Wi-Fi network " + network + ": not on " + subnets);
            reconsider();
        }

        @Override
        public void onCapabilitiesChanged(Network network, NetworkCapabilities caps) {
            if (callback != this) return;
            if (blocked && network.equals(current)) retryBind();
        }

        @Override
        public void onLost(Network network) {
            if (callback != this) return;
            candidates.remove(network);
            if (!network.equals(current)) return;
            lose();
            // SYSTEM mode: another Wi-Fi network already on the K1000 subnet takes over.
            if (callback == this && mode == Mode.SYSTEM && current == null) {
                Network n = firstOnSubnet();
                if (n != null) accept(n);
            }
        }

        @Override
        public void onUnavailable() {
            if (callback != this) return;
            callback = null;
            current = null;
            blocked = false;
            main.removeCallbacks(bindRetry);
            Log.i(TAG, "K1000 link unavailable (declined or not found)");
            listener.onLinkUnavailable();
        }
    }
}
