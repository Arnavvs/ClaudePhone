package com.claudephone.bridge;

import android.graphics.Rect;
import android.util.Log;
import android.view.accessibility.AccessibilityNodeInfo;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.util.HashMap;
import java.util.Map;

/**
 * A deliberately tiny HTTP server, bound to loopback only.
 *
 * No framework, no dependencies: the APK has to build with aapt2 + javac + d8
 * and nothing else, so that anyone can rebuild it without Gradle or a network.
 * Arguments arrive as query parameters rather than JSON bodies for the same
 * reason - it removes a parser from the trust path of a service that can tap
 * anything on the screen.
 *
 * Bound to 127.0.0.1 so it is reachable from Termux but not from the LAN.
 */
class BridgeServer extends Thread {

    private final BridgeService svc;
    private final int port;
    private volatile boolean running = true;
    private ServerSocket socket;

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
        Log.i(BridgeService.TAG, "listening on 127.0.0.1:" + port);
        while (running) {
            try {
                final Socket c = socket.accept();
                // One thread per connection. /changed deliberately BLOCKS for
                // seconds waiting on an accessibility event; on a single
                // threaded server that would stall every other request behind
                // it - including the /tree the caller needs the moment the wait
                // returns. Connections here are few and short-lived, so a
                // thread each is the simplest correct answer.
                Thread t = new Thread(() -> {
                    try {
                        handle(c);
                    } catch (Exception e) {
                        Log.w(BridgeService.TAG, "handler: " + e);
                    } finally {
                        try { c.close(); } catch (Exception ignored) { }
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

    private void handle(Socket c) throws Exception {
        c.setSoTimeout(60000);
        BufferedReader in = new BufferedReader(
                new InputStreamReader(c.getInputStream()), 8192);
        String line = in.readLine();
        if (line == null) return;
        String[] parts = line.split(" ");
        if (parts.length < 2) return;
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
        while (true) {  // drain headers
            String h = in.readLine();
            if (h == null || h.isEmpty()) break;
        }

        JSONObject body;
        try {
            body = route(path, q);
        } catch (Exception e) {
            body = new JSONObject();
            body.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        byte[] out = body.toString().getBytes("UTF-8");
        OutputStream os = c.getOutputStream();
        os.write(("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
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
                r.put("service", "claudephone-bridge");
                r.put("changes", BridgeService.CHANGES.get());
                r.put("last_package", BridgeService.lastPackage);
                r.put("last_event_ms_ago", BridgeService.lastEventAt == 0 ? -1
                        : System.currentTimeMillis() - BridgeService.lastEventAt);
                break;
            }
            case "/tree": {
                AccessibilityNodeInfo root = svc.root();
                JSONArray els = new JSONArray();
                if (root != null) {
                    walk(root, "", els, intArg(q, "limit", 300),
                         q.containsKey("all"));
                    r.put("package", String.valueOf(root.getPackageName()));
                }
                r.put("elements", els);
                r.put("count", els.length());
                r.put("changes", BridgeService.CHANGES.get());
                break;
            }
            case "/changed": {
                // Long-poll: block until the content actually changes. This is
                // what replaces a 250 ms polling loop.
                int since = intArg(q, "since", -1);
                int timeout = intArg(q, "timeout", 10000);
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
            case "/tap":
                r.put("ok", svc.tap(intArg(q, "x", 0), intArg(q, "y", 0),
                                    intArg(q, "ms", 50)));
                break;
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
                r.put("paths", new JSONArray()
                        .put("/health").put("/tree").put("/changed")
                        .put("/tap").put("/swipe").put("/key").put("/text"));
        }
        r.put("ms", (System.nanoTime() - t0) / 1000000.0);
        return r;
    }

    // ---------------- tree ----------------

    /**
     * Flatten the live node tree into the same compact element shape the Python
     * side already consumes from uiautomator2, so this backend is a swap rather
     * than a second format to support:
     *
     *   {"i":12, "id":"like_count", "anchor":"...", "text":"...",
     *    "c":[x,y], "f":"CS"}
     */
    private void walk(AccessibilityNodeInfo n, String anchor, JSONArray out,
                      int limit, boolean keepAll) throws Exception {
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
            if (f.length() > 0) e.put("f", f.toString());
            out.put(e);
        }

        int kids = n.getChildCount();
        for (int i = 0; i < kids; i++) {
            walk(n.getChild(i), childAnchor, out, limit, keepAll);
        }
    }
}
