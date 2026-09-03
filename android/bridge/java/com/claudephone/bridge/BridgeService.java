package com.claudephone.bridge;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.os.Bundle;
import android.util.Log;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

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

    /** Type into the focused editable node. */
    public boolean setText(String text) {
        AccessibilityNodeInfo r = root();
        if (r == null) return false;
        AccessibilityNodeInfo focused =
                r.findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        if (focused == null) return false;
        Bundle args = new Bundle();
        args.putCharSequence(
                AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                text == null ? "" : text);
        return focused.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args);
    }
}
