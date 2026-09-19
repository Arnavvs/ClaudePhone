package com.claudephone.bridge;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.graphics.Rect;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import org.json.JSONException;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * ClaudePhone Bridge - phase 2 of the project's backend plan.
 *
 * The measurement that motivates this (docs/BENCHMARKS.md):
 *
 *     uiautomator2 dump_hierarchy()   ~260 ms   <- jsonrpc round trip
 *     targeted .info x3               ~678 ms
 *
 * Every one of those crosses a socket to the uiautomator instrumentation and
 * serialises the whole tree as XML. An AccessibilityService already HOLDS the
 * node tree in process, so reading it is a walk over live objects rather than a
 * round trip - and it can be pushed by an EVENT instead of polled for.
 *
 * Two capabilities that adb/uiautomator2 cannot offer at all:
 *   - onAccessibilityEvent fires when the window content changes, so the agent
 *     can sleep until something happens instead of dumping every 250 ms.
 *   - the service survives a reboot once enabled, whereas wireless adb does not.
 *
 * NOTE ON COEXISTENCE: uiautomator2 is a UiAutomation, which is itself a
 * special AccessibilityService. Whether the two can run at the same time is the
 * gating question for this design and is tested, not assumed - see
 * docs/ACCESSIBILITY.md.
 */
public class BridgeService extends AccessibilityService {

    public static final String TAG = "ClaudePhoneBridge";
    public static final int PORT = 8766;
    /** Reported by /health so a client can tell a hardened build from v0.1. */
    public static final String VERSION = "0.2.2";   // 0.2.2: /text reads the field back; E/F/P/H tree flags

    /** Bumped on every content change; /changed long-polls against it. */
    public static final AtomicInteger CHANGES = new AtomicInteger(0);
    public static final Object LOCK = new Object();

    public static volatile String lastPackage = "";
    public static volatile long lastEventAt = 0L;
    public static volatile BridgeService instance = null;

    private BridgeServer server;

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        instance = this;
        Log.i(TAG, "connected; starting http on 127.0.0.1:" + PORT);
        if (server == null) {
            server = new BridgeServer(this, PORT);
            server.start();
        }
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        if (event == null) return;
        int t = event.getEventType();
        if (t == AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED
                || t == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
                || t == AccessibilityEvent.TYPE_VIEW_SCROLLED) {
            CharSequence p = event.getPackageName();
            if (p != null) lastPackage = p.toString();
            lastEventAt = System.currentTimeMillis();
            CHANGES.incrementAndGet();
            // Wake anything blocked in /changed. This is the whole point of the
            // service: the agent stops polling and starts being told.
            synchronized (LOCK) { LOCK.notifyAll(); }
        }
    }

    @Override
    public void onInterrupt() { }

    @Override
    public boolean onUnbind(android.content.Intent intent) {
        if (server != null) { server.shutdown(); server = null; }
        instance = null;
        return super.onUnbind(intent);
    }

    // ---------------- actions ----------------

    /** The active window's root, or null if nothing is focused. */
    public AccessibilityNodeInfo root() {
        return getRootInActiveWindow();
    }

    // ---------------- windows ----------------
    //
    // Reading only getRootInActiveWindow() - which is all this service did before
    // v0.2 - misses everything drawn in OTHER windows: the keyboard, system alert
    // windows, chat heads, the notification shade, and any overlay that sits on
    // top of the app and swallows taps. A dump would look perfectly normal while
    // every tap landed on something else. The windows list says what is on top.

    static String windowType(int t) {
        switch (t) {
            case AccessibilityWindowInfo.TYPE_APPLICATION:          return "application";
            case AccessibilityWindowInfo.TYPE_INPUT_METHOD:         return "input_method";
            case AccessibilityWindowInfo.TYPE_SYSTEM:               return "system";
            case AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY: return "accessibility_overlay";
            case AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER: return "split_screen_divider";
            default:                                                return "other_" + t;
        }
    }

    /** Windows on the default display, topmost first. Never null. */
    public List<AccessibilityWindowInfo> windowsTopFirst() {
        List<AccessibilityWindowInfo> ws;
        try {
            ws = new ArrayList<>(getWindows());
        } catch (Exception e) {
            ws = new ArrayList<>();
        }
        Collections.sort(ws, (a, b) -> Integer.compare(b.getLayer(), a.getLayer()));
        return ws;
    }

    static String rootPackage(AccessibilityWindowInfo w) {
        try {
            AccessibilityNodeInfo r = w.getRoot();
            if (r != null && r.getPackageName() != null) {
                return r.getPackageName().toString();
            }
        } catch (Exception ignored) { }
        return "";
    }

    /**
     * rootPackage() with a per-request cache. Each lookup is a binder call into
     * the window's node tree; one request may ask about the same window several
     * times (the summary, the foreground rule, the tap target).
     */
    static String pkgOf(AccessibilityWindowInfo w, java.util.Map<Integer, String> pkgs) {
        if (pkgs == null) return rootPackage(w);
        String p = pkgs.get(w.getId());
        if (p == null) {
            p = rootPackage(w);
            pkgs.put(w.getId(), p);
        }
        return p;
    }

    /** Screen height, for telling a status/navigation bar from a real overlay. */
    int screenHeight() {
        return getResources().getDisplayMetrics().heightPixels;
    }

    /**
     * True for the always-present thin system bars. Those are above the app in
     * z-order on every screen, so reporting them as obstructions would make the
     * signal useless.
     */
    boolean isBar(AccessibilityWindowInfo w, Rect b) {
        if (w.getType() != AccessibilityWindowInfo.TYPE_SYSTEM) return false;
        int h = Math.max(1, screenHeight());
        int height = b.height();
        boolean top = b.top <= 0 && height <= h * 0.10;
        boolean bottom = b.bottom >= h - 2 && height <= h * 0.12;
        return top || bottom;
    }

    /**
     * Which app is really in the foreground, and how we know.
     *
     * An overlay also produces TYPE_WINDOW_STATE_CHANGED, so the package of the
     * last event is not evidence of what the user sees. Prefer the active
     * application window, then the focused one, and say which rule answered - an
     * "unknown" with a reason is debuggable, an empty string is not.
     */
    public String[] foreground(List<AccessibilityWindowInfo> ws,
                               java.util.Map<Integer, String> pkgs) {
        for (AccessibilityWindowInfo w : ws) {
            if (w.getType() == AccessibilityWindowInfo.TYPE_APPLICATION && w.isActive()) {
                String p = pkgOf(w, pkgs);
                if (!p.isEmpty()) return new String[]{p, "active_app_window"};
            }
        }
        for (AccessibilityWindowInfo w : ws) {
            if (w.getType() == AccessibilityWindowInfo.TYPE_APPLICATION && w.isFocused()) {
                String p = pkgOf(w, pkgs);
                if (!p.isEmpty()) return new String[]{p, "focused_app_window"};
            }
        }
        AccessibilityNodeInfo r = root();
        if (r != null && r.getPackageName() != null) {
            return new String[]{r.getPackageName().toString(), "active_root_fallback"};
        }
        if (ws.isEmpty()) return new String[]{"", "unknown:no_windows_reported"};
        return new String[]{"", "unknown:no_application_window"};
    }

    /** The topmost window whose bounds contain (x, y), or null. */
    public AccessibilityWindowInfo windowAt(List<AccessibilityWindowInfo> ws, int x, int y) {
        Rect b = new Rect();
        for (AccessibilityWindowInfo w : ws) {
            if (w.getType() == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY) continue;
            w.getBoundsInScreen(b);
            if (b.contains(x, y)) return w;
        }
        return null;
    }

    public boolean tap(float x, float y, int durationMs) {
        Path p = new Path();
        p.moveTo(x, y);
        GestureDescription.Builder b = new GestureDescription.Builder();
        b.addStroke(new GestureDescription.StrokeDescription(p, 0,
                Math.max(1, durationMs)));
        return dispatchGesture(b.build(), null, null);
    }

    public boolean swipe(float x1, float y1, float x2, float y2, int durationMs) {
        Path p = new Path();
        p.moveTo(x1, y1);
        p.lineTo(x2, y2);
        GestureDescription.Builder b = new GestureDescription.Builder();
        b.addStroke(new GestureDescription.StrokeDescription(p, 0,
                Math.max(1, durationMs)));
        return dispatchGesture(b.build(), null, null);
    }

    public boolean globalAction(String name) {
        int a;
        switch (name == null ? "" : name.toLowerCase()) {
            case "back":         a = GLOBAL_ACTION_BACK; break;
            case "home":         a = GLOBAL_ACTION_HOME; break;
            case "recents":      a = GLOBAL_ACTION_RECENTS; break;
            case "notifications":a = GLOBAL_ACTION_NOTIFICATIONS; break;
            case "quicksettings":a = GLOBAL_ACTION_QUICK_SETTINGS; break;
            case "lock":         a = GLOBAL_ACTION_LOCK_SCREEN; break;
            default: return false;
        }
        return performGlobalAction(a);
    }

    /** The field's own text: "" while it is only showing its hint. */
    static String fieldText(AccessibilityNodeInfo n) {
        if (Build.VERSION.SDK_INT >= 26 && n.isShowingHintText()) return "";
        CharSequence t = n.getText();
        return t == null ? "" : t.toString();
    }

    /**
     * Set the input-focused field's text, then READ IT BACK (B10).
     *
     * performAction(ACTION_SET_TEXT) can return true and change nothing - a
     * residual focus node, an app that rejects programmatic text - so "ok" is
     * decided by what the field holds afterwards, re-read from the live node,
     * not by the action's return value. Unicode goes through untouched, which
     * `adb shell input text` cannot do.
     */
    public JSONObject setText(String text, boolean append) {
        JSONObject r = new JSONObject();
        try {
            r.put("channel", "set_text");
            r.put("ok", false);
            AccessibilityNodeInfo root = root();
            if (root == null) { r.put("error", "no active window"); return r; }
            AccessibilityNodeInfo f = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
            if (f == null) {
                r.put("error", "no input-focused field; tap the field first");
                return r;
            }
            JSONObject field = new JSONObject();
            String rid = f.getViewIdResourceName();
            if (rid != null) field.put("id", rid.substring(rid.indexOf('/') + 1));
            CharSequence cls = f.getClassName();
            if (cls != null) field.put("cls", cls.toString()
                    .substring(cls.toString().lastIndexOf('.') + 1));
            CharSequence pkg = f.getPackageName();
            if (pkg != null) field.put("pkg", pkg.toString());
            field.put("editable", f.isEditable());
            field.put("password", f.isPassword());
            field.put("visible", f.isVisibleToUser());
            r.put("field", field);
            if (!f.isEditable()) {
                r.put("error", "the focused node is not an editable field");
                return r;
            }
            String before = fieldText(f);
            String want = (append ? before : "") + (text == null ? "" : text);
            Bundle args = new Bundle();
            args.putCharSequence(
                    AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, want);
            boolean acted = f.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
            r.put("acted", acted);
            // Apps apply text asynchronously; give the node up to 0.8 s to hold it.
            String got = fieldText(f);
            long deadline = System.currentTimeMillis() + 800;
            while (!want.equals(got) && System.currentTimeMillis() < deadline) {
                try { Thread.sleep(60); } catch (InterruptedException e) { break; }
                f.refresh();
                got = fieldText(f);
            }
            r.put("before", before);
            r.put("readback", got);
            boolean matched = want.equals(got);
            r.put("matched", matched);
            // A password field reads back masked; it cannot be verified here.
            r.put("ok", acted && (matched || f.isPassword()));
        } catch (JSONException e) {
            Log.w(TAG, "setText", e);
        }
        return r;
    }
}
