package com.claudephone.bridge;

import android.graphics.Rect;
import android.util.Log;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.Semaphore;

/**
 * A deliberately tiny HTTP server, bound to loopback only.
 *
 * No framework, no dependencies: the APK has to build with aapt2 + javac + d8
 * and nothing else, so that anyone can rebuild it without Gradle or a network.
 * Arguments arrive as query parameters rather than JSON bodies for the same
 * reason - it removes a parser from the trust path of a service that can tap
 * anything on the screen.
 *
 * SECURITY (v0.2). Bound to 127.0.0.1 keeps the LAN out, but NOT other apps:
 * loopback is shared by every process on the phone. v0.1 answered anyone, so
 * any installed app could read the screen and inject taps. Now:
 *
 *   - every path except /health needs the `X-Bridge-Token` header, whose value
 *     only adb shell or root can read (see TokenProvider). A header, never a
 *     query parameter: a web page can make the browser send a GET with any query
 *     string (an <img> tag is enough), but it cannot attach a custom header
 *     without a CORS preflight this server never approves.
 *   - any request carrying an `Origin` header is refused outright. Browsers add
 *     it to cross-origin requests; our clients (urllib, curl) never send it.
 *   - unauthenticated /health says only that auth is required - no package,
 *     no activity, nothing about what is on screen.
 *   - request lines and headers are size-capped, reads time out, and concurrent
 *     connections are capped, so a local app cannot pin threads or memory.
 */
class BridgeServer extends Thread {

    private static final int MAX_LINE = 8192;
    private static final int MAX_HEADERS = 64;
    private static final int MAX_CONNECTIONS = 32;
    private static final String TOKEN_HEADER = "x-bridge-token";

    private final BridgeService svc;
    private final int port;
    private volatile boolean running = true;
    private ServerSocket socket;
    private final Semaphore slots = new Semaphore(MAX_CONNECTIONS);

    BridgeServer(BridgeService svc, int port) {
        this.svc = svc;
        this.port = port;
        setName("claudephone-bridge-http");
        setDaemon(true);
    }

    void shutdown() {
        running = false;
        try { if (socket != null) socket.close(); } catch (Exception ignored) { }
    }

    @Override
    public void run() {
        try {
            socket = new ServerSocket(port, 16,
                    InetAddress.getByName("127.0.0.1"));
        } catch (Exception e) {
            Log.e(BridgeService.TAG, "bind failed: " + e);
            return;
        }
        // Create the token now rather than on the first client, so a client that
        // reads it via adb before its first request never races a lazy init.
        TokenProvider.token(svc);
        Log.i(BridgeService.TAG, "listening on 127.0.0.1:" + port + " (token auth)");
        while (running) {
            try {
                final Socket c = socket.accept();
                if (!slots.tryAcquire()) {
                    // Over the cap: answer and close without spawning a thread.
                    try {
                        respond(c.getOutputStream(), 503,
                                new JSONObject().put("error", "busy"));
                    } catch (Exception ignored) {
                    } finally {
                        try { c.close(); } catch (Exception ignored) { }
                    }
                    continue;
                }
                // One thread per connection. /changed deliberately BLOCKS for
                // seconds waiting on an accessibility event; on a single
                // threaded server that would stall every other request behind
                // it - including the /tree the caller needs the moment the wait
                // returns.
                Thread t = new Thread(() -> {
                    try {
                        handle(c);
                    } catch (Exception e) {
                        Log.w(BridgeService.TAG, "handler: " + e);
                    } finally {
                        try { c.close(); } catch (Exception ignored) { }
                        slots.release();
                    }
                });
                t.setDaemon(true);
                t.start();
            } catch (Exception e) {
                if (running) Log.w(BridgeService.TAG, "accept: " + e);
            }
        }
    }

    // ---------------- http ----------------

    /** One CRLF- or LF-terminated line, or null at EOF / over the size cap. */
    private static String readLine(InputStream in) throws Exception {
        ByteArrayOutputStream buf = new ByteArrayOutputStream(128);
        int b;
        while ((b = in.read()) != -1) {
            if (b == '\n') break;
            if (b != '\r') buf.write(b);
            if (buf.size() > MAX_LINE) return null;
        }
        if (b == -1 && buf.size() == 0) return null;
        return buf.toString("UTF-8");
    }

    private void handle(Socket c) throws Exception {
        // Short read timeout: a request is one line plus a few headers. The long
        // wait in /changed happens after the request is read, so it is unaffected.
        c.setSoTimeout(5000);
        InputStream in = new BufferedInputStream(c.getInputStream(), 8192);
        OutputStream os = c.getOutputStream();

        String line = readLine(in);
        if (line == null) return;
        String[] parts = line.split(" ");
        if (parts.length < 2) {
            respond(os, 400, new JSONObject().put("error", "bad request line"));
            return;
        }

        Map<String, String> headers = new HashMap<>();
        for (int n = 0; ; n++) {
            String h = readLine(in);
            if (h == null) {
                respond(os, 431, new JSONObject().put("error", "headers too large"));
                return;
            }
            if (h.isEmpty()) break;
            if (n >= MAX_HEADERS) {
                respond(os, 431, new JSONObject().put("error", "too many headers"));
                return;
            }
            int colon = h.indexOf(':');
            if (colon > 0) {
                headers.put(h.substring(0, colon).trim().toLowerCase(Locale.ROOT),
                            h.substring(colon + 1).trim());
            }
        }

        String target = parts[1];
        String path = target;
        Map<String, String> q = new HashMap<>();
        int qm = target.indexOf('?');
        if (qm >= 0) {
            path = target.substring(0, qm);
            for (String kv : target.substring(qm + 1).split("&")) {
                int eq = kv.indexOf('=');
                if (eq > 0) {
                    q.put(URLDecoder.decode(kv.substring(0, eq), "UTF-8"),
                          URLDecoder.decode(kv.substring(eq + 1), "UTF-8"));
                }
            }
        }

        if (headers.containsKey("origin")) {
            respond(os, 403, new JSONObject().put("error", "browser origin refused"));
            return;
        }

        boolean authed = TokenProvider.matches(svc, headers.get(TOKEN_HEADER));
        if (!authed) {
            if (path.equals("/health")) {
                respond(os, 200, new JSONObject().put("ok", true)
                                                 .put("auth", "required"));
            } else {
                respond(os, 401, new JSONObject().put("error", "unauthorized"));
            }
            return;
        }

        JSONObject body;
        int status = 200;
        try {
            body = route(path, q);
            if (body.has("error") && body.optBoolean("_not_found")) {
                body.remove("_not_found");
                status = 404;
            }
        } catch (Exception e) {
            body = new JSONObject();
            body.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
            status = 500;
        }
        respond(os, status, body);
    }

    private static void respond(OutputStream os, int status, JSONObject body)
            throws Exception {
        byte[] out = body.toString().getBytes("UTF-8");
        String reason;
        switch (status) {
            case 200: reason = "OK"; break;
            case 400: reason = "Bad Request"; break;
            case 401: reason = "Unauthorized"; break;
            case 403: reason = "Forbidden"; break;
            case 404: reason = "Not Found"; break;
            case 431: reason = "Request Header Fields Too Large"; break;
            case 503: reason = "Service Unavailable"; break;
            default:  reason = "Error";
        }
        os.write(("HTTP/1.1 " + status + " " + reason + "\r\n"
                + "Content-Type: application/json\r\n"
                + "Content-Length: " + out.length + "\r\nConnection: close\r\n\r\n")
                .getBytes("UTF-8"));
        os.write(out);
        os.flush();
    }

    private int intArg(Map<String, String> q, String k, int dflt) {
        try { return Integer.parseInt(q.get(k)); } catch (Exception e) { return dflt; }
    }

    private JSONObject route(String path, Map<String, String> q) throws Exception {
        JSONObject r = new JSONObject();
        long t0 = System.nanoTime();

        switch (path) {
            case "/health": {
                r.put("ok", true);
                r.put("auth", "ok");
                r.put("service", "claudephone-bridge");
                r.put("version", BridgeService.VERSION);
                r.put("changes", BridgeService.CHANGES.get());
                r.put("last_package", BridgeService.lastPackage);
                r.put("last_event_ms_ago", BridgeService.lastEventAt == 0 ? -1
                        : System.currentTimeMillis() - BridgeService.lastEventAt);
                break;
            }
            case "/tree": {
                AccessibilityNodeInfo root = svc.root();
                JSONArray els = new JSONArray();
                int limit = intArg(q, "limit", 300);
                boolean keepAll = q.containsKey("all");
                // Elements of the ACTIVE window come first and are numbered exactly
                // as in v0.1, so every index-based consumer keeps working.
                if (root != null) {
                    walk(root, "", els, limit, keepAll, null);
                    r.put("package", String.valueOf(root.getPackageName()));
                }
                List<AccessibilityWindowInfo> ws = svc.windowsTopFirst();
                describeWindows(ws, r);
                // Opt-in: append the other windows' elements, each tagged with the
                // window it came from, e.g. a system dialog drawn over the app.
                if ("all".equals(q.get("windows"))) {
                    for (AccessibilityWindowInfo w : ws) {
                        if (w.isActive()) continue;
                        int type = w.getType();
                        if (type == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY) continue;
                        AccessibilityNodeInfo wr;
                        try { wr = w.getRoot(); } catch (Exception e) { wr = null; }
                        if (wr == null) continue;
                        JSONObject tag = new JSONObject()
                                .put("w", BridgeService.windowType(type))
                                .put("wid", w.getId());
                        walk(wr, "", els, limit, keepAll, tag);
                    }
                }
                r.put("elements", els);
                r.put("count", els.length());
                r.put("changes", BridgeService.CHANGES.get());
                break;
            }
            case "/windows": {
                describeWindows(svc.windowsTopFirst(), r);
                break;
            }
            case "/changed": {
                // Long-poll: block until the content actually changes. This is
                // what replaces a 250 ms polling loop.
                int since = intArg(q, "since", -1);
                int timeout = Math.min(intArg(q, "timeout", 10000), 60000);
                long deadline = System.currentTimeMillis() + timeout;
                synchronized (BridgeService.LOCK) {
                    while (BridgeService.CHANGES.get() <= since
                            && System.currentTimeMillis() < deadline) {
                        long wait = deadline - System.currentTimeMillis();
                        if (wait > 0) BridgeService.LOCK.wait(wait);
                    }
                }
                int now = BridgeService.CHANGES.get();
                r.put("changes", now);
                r.put("changed", now > since);
                r.put("package", BridgeService.lastPackage);
                break;
            }
            case "/tap": {
                int x = intArg(q, "x", 0), y = intArg(q, "y", 0);
                // Say what the tap will actually hit BEFORE dispatching it: if a
                // keyboard or an overlay is on top, the caller learns that the tap
                // went there, instead of seeing "ok" and a screen that never moved.
                r.put("lands_on", landsOn(svc.windowsTopFirst(), x, y));
                r.put("ok", svc.tap(x, y, intArg(q, "ms", 50)));
                break;
            }
            case "/swipe":
                r.put("ok", svc.swipe(intArg(q, "x1", 0), intArg(q, "y1", 0),
                                      intArg(q, "x2", 0), intArg(q, "y2", 0),
                                      intArg(q, "ms", 250)));
                break;
            case "/key":
                r.put("ok", svc.globalAction(q.get("name")));
                break;
            case "/text":
                r.put("ok", svc.setText(q.get("value")));
                break;
            default:
                r.put("error", "no such path: " + path);
                r.put("_not_found", true);
                r.put("paths", new JSONArray()
                        .put("/health").put("/tree").put("/windows").put("/changed")
                        .put("/tap").put("/swipe").put("/key").put("/text"));
        }
        r.put("ms", (System.nanoTime() - t0) / 1000000.0);
        return r;
    }

    // ---------------- windows ----------------

    /**
     * Adds windows (topmost first), foreground + reason, ime_visible and
     * obstructions to `r`.
     *
     * An obstruction is a window above the active one that is not a thin status
     * or navigation bar: a keyboard, a system alert, a chat head, a volume panel.
     * Anything a tap aimed at the app could hit instead.
     */
    private void describeWindows(List<AccessibilityWindowInfo> ws, JSONObject r)
            throws Exception {
        JSONArray list = new JSONArray();
        JSONArray obstructions = new JSONArray();
        boolean ime = false;
        int activeLayer = Integer.MIN_VALUE;
        for (AccessibilityWindowInfo w : ws) {
            if (w.isActive()) { activeLayer = w.getLayer(); break; }
        }
        Rect b = new Rect();
        Map<Integer, String> pkgs = new HashMap<>();
        for (AccessibilityWindowInfo w : ws) {
            w.getBoundsInScreen(b);
            String type = BridgeService.windowType(w.getType());
            JSONObject o = new JSONObject()
                    .put("id", w.getId())
                    .put("type", type)
                    .put("layer", w.getLayer())
                    .put("pkg", BridgeService.pkgOf(w, pkgs))
                    .put("active", w.isActive())
                    .put("focused", w.isFocused())
                    .put("b", new JSONArray().put(b.left).put(b.top)
                                             .put(b.right).put(b.bottom));
            if (w.getType() == AccessibilityWindowInfo.TYPE_INPUT_METHOD) ime = true;
            list.put(o);

            boolean above = activeLayer != Integer.MIN_VALUE && w.getLayer() > activeLayer;
            int t = w.getType();
            if (above && !w.isActive()
                    && t != AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY
                    && t != AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER
                    && !svc.isBar(w, b)) {
                obstructions.put(o);
            }
        }
        String[] fg = svc.foreground(ws, pkgs);
        r.put("windows", list);
        r.put("foreground", fg[0]);
        r.put("foreground_reason", fg[1]);
        r.put("foreground_known", !fg[1].startsWith("unknown"));
        r.put("ime_visible", ime);
        r.put("obstructions", obstructions);
    }

    private JSONObject landsOn(List<AccessibilityWindowInfo> ws, int x, int y)
            throws Exception {
        JSONObject o = new JSONObject();
        AccessibilityWindowInfo w = svc.windowAt(ws, x, y);
        if (w == null) {
            o.put("type", "none");
            return o;
        }
        Rect b = new Rect();
        w.getBoundsInScreen(b);
        o.put("type", BridgeService.windowType(w.getType()))
         .put("pkg", BridgeService.rootPackage(w))
         .put("layer", w.getLayer())
         .put("active", w.isActive())
         .put("bar", svc.isBar(w, b));
        // "covered" = the tap goes somewhere other than the window being read.
        o.put("covered", !w.isActive());
        return o;
    }

    // ---------------- tree ----------------

    /**
     * Flatten the live node tree into the same compact element shape the Python
     * side already consumes from uiautomator2, so this backend is a swap rather
     * than a second format to support:
     *
     *   {"i":12, "id":"like_count", "anchor":"...", "text":"...",
     *    "c":[x,y], "f":"CS"}
     *
     * Flag `h` (v0.2) marks a node the system reports as not visible to the user,
     * e.g. scrolled out of its container - tapping its centre hits something else.
     * `tag`, when set, is merged into every element (window type and id).
     */
    private void walk(AccessibilityNodeInfo n, String anchor, JSONArray out,
                      int limit, boolean keepAll, JSONObject tag) throws Exception {
        if (n == null || out.length() >= limit) return;

        String rid = n.getViewIdResourceName();
        String shortRid = rid == null ? ""
                : (rid.contains(":id/") ? rid.substring(rid.indexOf(":id/") + 4) : rid);
        String text = n.getText() == null ? "" : n.getText().toString().trim();
        String desc = n.getContentDescription() == null ? ""
                : n.getContentDescription().toString().trim();
        String cls = n.getClassName() == null ? "" : n.getClassName().toString();
        String shortCls = cls.contains(".")
                ? cls.substring(cls.lastIndexOf('.') + 1) : cls;

        boolean hasValue = !text.isEmpty() || !desc.isEmpty();
        boolean interactive = n.isClickable() || n.isScrollable();
        boolean meaningful = hasValue || interactive || !shortRid.isEmpty();

        String childAnchor = shortRid.isEmpty() ? anchor : shortRid;

        if (keepAll || meaningful) {
            Rect b = new Rect();
            n.getBoundsInScreen(b);
            JSONObject e = new JSONObject();
            e.put("i", out.length());
            if (!shortRid.isEmpty()) e.put("id", shortRid);
            if (!anchor.isEmpty() && !anchor.equals(shortRid)) e.put("anchor", anchor);
            if (!text.isEmpty()) e.put("text", text.length() > 300
                    ? text.substring(0, 300) : text);
            if (!desc.isEmpty() && !desc.equals(text)) e.put("desc",
                    desc.length() > 300 ? desc.substring(0, 300) : desc);
            if (!shortCls.isEmpty()) e.put("cls", shortCls);
            e.put("c", new JSONArray().put(b.centerX()).put(b.centerY()));
            // Full bounds too: the Python side builds real ui.Element
            // objects from this, and some extractors reason about size
            // and position, not just the tap centre.
            e.put("b", new JSONArray().put(b.left).put(b.top)
                                      .put(b.right).put(b.bottom));
            StringBuilder f = new StringBuilder();
            if (n.isClickable())  f.append("C");
            if (n.isScrollable()) f.append("S");
            if (n.isSelected())   f.append("*");
            if (n.isChecked())    f.append("x");
            if (!n.isVisibleToUser()) f.append("h");
            if (f.length() > 0) e.put("f", f.toString());
            if (tag != null) {
                e.put("w", tag.get("w"));
                e.put("wid", tag.get("wid"));
            }
            out.put(e);
        }

        int kids = n.getChildCount();
        for (int i = 0; i < kids; i++) {
            walk(n.getChild(i), childAnchor, out, limit, keepAll, tag);
        }
    }
}
