package com.claudephone.bridge;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.Binder;
import android.os.Bundle;
import android.os.Process;

import java.security.MessageDigest;
import java.security.SecureRandom;

/**
 * Hands out the bridge's auth token - to adb, and to nothing else.
 *
 * Why the bridge needs a token at all: it listens on 127.0.0.1, and loopback is
 * reachable by EVERY app on the phone, not just Termux. Without a token any
 * installed app - Instagram included - could GET /tree (read whatever is on
 * screen, SMS and 2FA codes too) and GET /tap (inject gestures). Google's ARTEMIS
 * helper and droidrun's Portal both ship a token for exactly this reason.
 *
 * Why a ContentProvider gated on the caller's uid, rather than a manifest
 * permission: the only callers we trust are the adb shell (uid 2000) and root.
 * Every legitimate client already has one of those - the laptop through adb, and
 * Termux through its loopback adb connection - so both can run
 *
 *     content query --uri content://com.claudephone.bridge.auth/token
 *
 * while an ordinary app gets a SecurityException. Checking the uid in code avoids
 * depending on an OEM leaving a particular signature permission granted to the
 * shell package, which is not something realme or Samsung guarantee.
 *
 * The token is generated once, persisted in this app's private preferences, and
 * survives service restarts and APK upgrades, so the laptop and the phone read
 * the SAME token instead of overwriting each other's. `content call ... rotate`
 * replaces it.
 */
public class TokenProvider extends ContentProvider {

    static final String AUTHORITY = "com.claudephone.bridge.auth";
    private static final String PREFS = "bridge_auth";
    private static final String KEY = "token";
    private static volatile String cached = null;

    @Override
    public boolean onCreate() {
        return true;
    }

    /** The current token, creating it on first use. */
    static synchronized String token(Context ctx) {
        if (cached != null) return cached;
        SharedPreferences p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        String t = p.getString(KEY, null);
        if (t == null || t.length() < 32) {
            t = fresh();
            p.edit().putString(KEY, t).commit();
        }
        cached = t;
        return t;
    }

    static synchronized String rotate(Context ctx) {
        String t = fresh();
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
           .edit().putString(KEY, t).commit();
        cached = t;
        return t;
    }

    /** Constant-time comparison, so response timing leaks nothing about the token. */
    static boolean matches(Context ctx, String presented) {
        if (presented == null) return false;
        try {
            byte[] a = token(ctx).getBytes("UTF-8");
            byte[] b = presented.trim().getBytes("UTF-8");
            return MessageDigest.isEqual(a, b);
        } catch (Exception e) {
            return false;
        }
    }

    private static String fresh() {
        byte[] b = new byte[24];
        new SecureRandom().nextBytes(b);
        StringBuilder sb = new StringBuilder(48);
        for (byte x : b) sb.append(String.format("%02x", x & 0xff));
        return sb.toString();
    }

    /** adb shell (2000) and root (0) only. The app's own uid is allowed for completeness. */
    private void enforceCaller() {
        int uid = Binder.getCallingUid();
        // 2000 = AID_SHELL. A literal, not Process.SHELL_UID, which is API 29+.
        if (uid == 2000 || uid == 0 || uid == Process.myUid()) return;
        throw new SecurityException("bridge token is only available to adb shell");
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        enforceCaller();
        MatrixCursor c = new MatrixCursor(new String[]{"token"});
        c.addRow(new Object[]{token(getContext())});
        return c;
    }

    @Override
    public Bundle call(String method, String arg, Bundle extras) {
        enforceCaller();
        Bundle out = new Bundle();
        if ("rotate".equals(method)) {
            out.putString("token", rotate(getContext()));
        } else {
            out.putString("error", "unknown method: " + method);
        }
        return out;
    }

    @Override
    public String getType(Uri uri) { return null; }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("read-only");
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("read-only");
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection,
                      String[] selectionArgs) {
        throw new UnsupportedOperationException("read-only");
    }
}
