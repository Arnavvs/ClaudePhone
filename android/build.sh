#!/usr/bin/env bash
# Build the ClaudePhone Bridge APK with aapt2 + javac + d8 + apksigner.
#
# No Gradle, no network, no dependencies. That is deliberate: the APK has to be
# rebuildable by anyone with an Android SDK and a JDK - including on the phone
# itself under Termux - without waiting for Gradle to resolve a dependency graph
# for a service that has none.
#
#   ./build.sh              build (creates a debug keystore on first run)
#   ./build.sh install      build, then install and enable the service
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/bridge"
OUT="$HERE/build"
PKG="com.claudephone.bridge"
SERVICE="$PKG/$PKG.BridgeService"

# --- locate the SDK ---------------------------------------------------------
SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
WHO="${USER:-${USERNAME:-unknown}}"
for c in "$SDK" "$HOME/AppData/Local/Android/Sdk" \
         "/c/Users/$WHO/AppData/Local/Android/Sdk" \
         "$HOME/Android/Sdk" "$HOME/Library/Android/sdk" \
         "/data/data/com.termux/files/usr/opt/android-sdk"; do
  if [ -n "$c" ] && [ -d "$c/platforms" ]; then SDK="$c"; break; fi
done
if [ -z "${SDK:-}" ] || [ ! -d "$SDK" ]; then
  echo "Android SDK not found. Set ANDROID_HOME." >&2; exit 1
fi

# SDK binaries on Windows are native and cannot read the POSIX paths Git Bash
# hands them. cygpath -m yields "C:/..." which both Windows and the JDK accept.
if command -v cygpath >/dev/null 2>&1; then
  w() { cygpath -m "$1"; }
else
  w() { printf '%s' "$1"; }
fi

BT="$(ls -1d "$SDK"/build-tools/* 2>/dev/null | sort -V | tail -1)"
PLATFORM="$(ls -1d "$SDK"/platforms/android-* 2>/dev/null | sort -V | tail -1)"
[ -n "$BT" ] || { echo "no build-tools in $SDK" >&2; exit 1; }
[ -n "$PLATFORM" ] || { echo "no platform in $SDK" >&2; exit 1; }
ANDROID_JAR="$PLATFORM/android.jar"

# Test -f, not -x: Git Bash does not mark .bat files executable.
pick() { for n in "$@"; do [ -f "$BT/$n" ] && { echo "$BT/$n"; return; }; done
         command -v "${1%%.*}" 2>/dev/null || true; }
AAPT2="$(pick aapt2.exe aapt2)"
ZIPALIGN="$(pick zipalign.exe zipalign)"

# d8 and apksigner ship as .bat wrappers on Windows and shell scripts elsewhere,
# but both are just jars. Running the jar through `java` skips a layer of
# platform-specific quoting and behaves identically on every host.
D8_JAR="$BT/lib/d8.jar"
APKSIGNER_JAR="$BT/lib/apksigner.jar"
[ -f "$D8_JAR" ] || { echo "d8.jar missing from $BT/lib" >&2; exit 1; }
[ -f "$APKSIGNER_JAR" ] || { echo "apksigner.jar missing from $BT/lib" >&2; exit 1; }

echo "SDK         $SDK"
echo "build-tools $(basename "$BT")   platform $(basename "$PLATFORM")"

rm -rf "$OUT"; mkdir -p "$OUT/classes" "$OUT/gen"

# --- 1. resources -----------------------------------------------------------
echo "==> aapt2 compile"
"$AAPT2" compile --dir "$(w "$SRC/res")" -o "$(w "$OUT/res.zip")"

echo "==> aapt2 link"
"$AAPT2" link \
  -I "$(w "$ANDROID_JAR")" \
  --manifest "$(w "$SRC/AndroidManifest.xml")" \
  --java "$(w "$OUT/gen")" \
  --min-sdk-version 24 --target-sdk-version 35 \
  -o "$(w "$OUT/base.apk")" \
  "$(w "$OUT/res.zip")"

# --- 2. java ----------------------------------------------------------------
echo "==> javac"
find "$SRC/java" "$OUT/gen" -name '*.java' | while read -r f; do w "$f"; echo; done \
  > "$OUT/sources.txt"
javac -nowarn -source 17 -target 17 \
      -classpath "$(w "$ANDROID_JAR")" \
      -d "$(w "$OUT/classes")" \
      "@$(w "$OUT/sources.txt")"
if [ -z "$(find "$OUT/classes" -name '*.class' -print -quit)" ]; then
  echo "javac produced no classes" >&2; exit 1
fi

# --- 3. dex -----------------------------------------------------------------
echo "==> d8"
find "$OUT/classes" -name '*.class' | while read -r f; do w "$f"; echo; done \
  > "$OUT/classes.txt"
java -cp "$(w "$D8_JAR")" com.android.tools.r8.D8 \
     --min-api 24 --lib "$(w "$ANDROID_JAR")" \
     --output "$(w "$OUT")" "@$(w "$OUT/classes.txt")"

# --- 4. package -------------------------------------------------------------
echo "==> package"
cp "$OUT/base.apk" "$OUT/unsigned.apk"
# aapt2 emits an APK with resources but no code; classes.dex has to be added to
# the archive by hand. `zip` is absent from Git Bash and many minimal images, so
# fall back to the JDK's own `jar` (always present - we already require javac),
# then to Python (already a ClaudePhone dependency). No new tools either way.
if command -v zip >/dev/null 2>&1; then
  ( cd "$OUT" && zip -q -u unsigned.apk classes.dex )
elif command -v jar >/dev/null 2>&1; then
  ( cd "$OUT" && jar uf unsigned.apk classes.dex )
else
  PY="$(command -v python3 || command -v python)"
  [ -n "$PY" ] || { echo "need one of: zip, jar, python" >&2; exit 1; }
  "$PY" -c "import zipfile,sys;\
z=zipfile.ZipFile(sys.argv[1],'a',zipfile.ZIP_DEFLATED);\
z.write(sys.argv[2],'classes.dex');z.close()" \
    "$(w "$OUT/unsigned.apk")" "$(w "$OUT/classes.dex")"
fi

# --- 5. sign ----------------------------------------------------------------
KS="$HERE/debug.keystore"
if [ ! -f "$KS" ]; then
  echo "==> creating debug keystore (first run only)"
  # keytool ships with the JDK but is frequently not on PATH even when java is -
  # on Windows, `java` is usually Oracle's javapath shim, which contains java
  # and nothing else. Ask the JVM itself where its home is; that works on every
  # platform and needs no hardcoded paths.
  KEYTOOL="$(command -v keytool || true)"
  if [ -z "$KEYTOOL" ]; then
    JH="${JAVA_HOME:-}"
    if [ -z "$JH" ]; then
      JH="$(java -XshowSettings:properties -version 2>&1 \
            | sed -n 's/^ *java\.home = *//p' | head -1)"
    fi
    for cand in "$JH/bin/keytool" "$JH/bin/keytool.exe"; do
      [ -f "$cand" ] && KEYTOOL="$cand" && break
    done
  fi
  if [ -z "$KEYTOOL" ] || { [ ! -f "$KEYTOOL" ] && [ ! -x "$KEYTOOL" ]; }; then
    echo "keytool not found; set JAVA_HOME to a JDK" >&2; exit 1
  fi
  "$KEYTOOL" -genkeypair -keystore "$(w "$KS")" \
          -storepass android -keypass android \
          -alias claudephone -keyalg RSA -keysize 2048 -validity 10000 \
          -dname "CN=ClaudePhone Bridge, OU=dev, O=ClaudePhone, C=US"
fi

echo "==> zipalign + sign"
"$ZIPALIGN" -f -p 4 "$(w "$OUT/unsigned.apk")" "$(w "$OUT/aligned.apk")"
java -jar "$(w "$APKSIGNER_JAR")" sign \
     --ks "$(w "$KS")" --ks-pass pass:android --key-pass pass:android \
     --out "$(w "$OUT/bridge.apk")" "$(w "$OUT/aligned.apk")"

echo
echo "built: $OUT/bridge.apk  ($(du -h "$OUT/bridge.apk" | cut -f1))"

# --- 6. optional install ----------------------------------------------------
if [ "${1:-}" = "install" ]; then
  ADB="${ADB_PATH:-$SDK/platform-tools/adb}"
  [ -f "$ADB" ] || ADB="${ADB}.exe"
  [ -f "$ADB" ] || ADB="$(command -v adb)"
  echo "==> installing"
  "$ADB" install -r "$(w "$OUT/bridge.apk")"
  echo "==> enabling the accessibility service"
  # Enabling normally means tapping through Settings > Accessibility. uid 2000
  # can write the secure setting directly, which is why this is scriptable.
  CUR="$("$ADB" shell settings get secure enabled_accessibility_services | tr -d '\r\n')"
  case "$CUR" in
    *"$SERVICE"*)  NEW="$CUR" ;;
    null|"")       NEW="$SERVICE" ;;
    *)             NEW="$CUR:$SERVICE" ;;
  esac
  "$ADB" shell settings put secure enabled_accessibility_services "$NEW"
  "$ADB" shell settings put secure accessibility_enabled 1
  sleep 3
  echo "==> enabled services now:"
  "$ADB" shell settings get secure enabled_accessibility_services
fi
