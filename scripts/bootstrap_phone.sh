#!/data/data/com.termux/files/usr/bin/bash
# ClaudePhone - one-command setup, run INSIDE Termux on the phone.
#
#   curl -sL https://raw.githubusercontent.com/Arnavvs/ClaudePhone/main/scripts/bootstrap_phone.sh | bash
#
# What it does, and why each step is needed:
#   1. Termux packages          - python, adb, git, the API bridge
#   2. adb connect 127.0.0.1    - THE key step. Termux's own uid cannot run
#                                 `input` or `uiautomator`; adb's uid 2000 can.
#                                 Costs one tap on an Android dialog, once ever.
#   3. python deps + install    - uiautomator2 and ClaudePhone itself
#   4. doctor                   - prove the whole chain works before you rely
#                                 on it at 2am
set -u

BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
YELLOW=$'\033[33m'; RESET=$'\033[0m'
say()  { echo "${BOLD}==>${RESET} $*"; }
warn() { echo "${YELLOW}  ! $*${RESET}"; }
die()  { echo "${RED}  x $*${RESET}"; exit 1; }
ok()   { echo "${GREEN}  ok${RESET} $*"; }

REPO="${CLAUDEPHONE_REPO:-https://github.com/Arnavvs/ClaudePhone.git}"
DIR="${CLAUDEPHONE_DIR:-$HOME/ClaudePhone}"
LOOPBACK="${CLAUDEPHONE_LOOPBACK:-127.0.0.1:5555}"

[ -d /data/data/com.termux ] || die "this must run inside Termux on the phone"

# --- 1. packages ------------------------------------------------------------
say "installing Termux packages"
# python-lxml and python-pillow are NOT optional extras: uiautomator2 depends on
# both, and neither compiles from source under Termux. Installing pip's versions
# fails with "Failed to build 'lxml'" / "Failed building wheel for Pillow".
# Termux's prebuilt packages satisfy the dependency so pip skips the build.
pkg install -y python android-tools termux-api git python-lxml python-pillow \
  >/dev/null 2>&1 \
  || die "pkg install failed - run 'pkg update' and try again"
ok "python $(python --version 2>&1 | cut -d' ' -f2), adb present, lxml+pillow prebuilt"

if ! command -v termux-battery-status >/dev/null; then
  warn "termux-api CLI missing. Install the Termux:API *app* from the same"
  warn "source as Termux (GitHub releases - signatures must match) or the"
  warn "phone_* tools will not work."
fi

# --- 2. the privileged shell ------------------------------------------------
say "connecting adb to this phone's own adbd ($LOOPBACK)"
adb start-server >/dev/null 2>&1
adb connect "$LOOPBACK" >/dev/null 2>&1
sleep 2
STATE=$(adb devices | awk -v t="$LOOPBACK" '$1==t {print $2}')

if [ "$STATE" = "unauthorized" ] || [ -z "$STATE" ]; then
  echo
  warn "Android is asking permission for this. Look at your phone screen:"
  warn "  tick 'Always allow this computer for debugging' -> tap Allow"
  echo "${DIM}  (waiting up to 90s...)${RESET}"
  for _ in $(seq 1 45); do
    sleep 2
    adb connect "$LOOPBACK" >/dev/null 2>&1
    STATE=$(adb devices | awk -v t="$LOOPBACK" '$1==t {print $2}')
    [ "$STATE" = "device" ] && break
  done
fi

if [ "$STATE" != "device" ]; then
  echo
  die "adb is '${STATE:-not connected}'.
  If no dialog appeared, adbd is not listening on TCP. Fix with ONE of:
    - Developer options > Wireless debugging  (then re-run; port may differ:
      set CLAUDEPHONE_LOOPBACK=127.0.0.1:<port>)
    - from a USB-attached computer, once:  adb tcpip 5555"
fi
UID_LINE=$(adb -s "$LOOPBACK" shell id 2>/dev/null)
case "$UID_LINE" in
  *"uid=2000(shell)"*) ok "privileged shell available (uid 2000)" ;;
  *) die "connected but got: $UID_LINE" ;;
esac

# --- 3. the code ------------------------------------------------------------
if [ -d "$DIR/.git" ]; then
  say "updating $DIR"; git -C "$DIR" pull --ff-only 2>&1 | tail -1
elif [ -f "$DIR/pyproject.toml" ]; then
  say "using existing $DIR (not a git checkout)"
else
  say "cloning into $DIR"
  git clone --depth 1 "$REPO" "$DIR" >/dev/null 2>&1 || die "clone failed"
fi

say "installing python dependencies"
pip install --quiet --upgrade pip >/dev/null 2>&1
pip install --quiet uiautomator2 >/dev/null 2>&1 || die "uiautomator2 failed to install"
pip install --quiet -e "$DIR" >/dev/null 2>&1 || warn "editable install failed; falling back to PYTHONPATH"
ok "dependencies installed"

# --- 4. config --------------------------------------------------------------
mkdir -p "$HOME/.claudephone"
ENVF="$DIR/.env"
if [ ! -f "$ENVF" ]; then
  cp "$DIR/.env.example" "$ENVF" 2>/dev/null || true
  warn "put your OPENROUTER_API_KEY in $ENVF (or run with --provider local)"
fi

cat > "$HOME/.claudephone/env.sh" <<EOF
export CLAUDEPHONE_ON_DEVICE=1
export CLAUDEPHONE_SERIAL=$LOOPBACK
export PYTHONPATH=$DIR/src:\${PYTHONPATH:-}
[ -f "$ENVF" ] && set -a && . "$ENVF" && set +a
EOF
grep -q 'claudephone/env.sh' "$HOME/.bashrc" 2>/dev/null \
  || echo '[ -f "$HOME/.claudephone/env.sh" ] && . "$HOME/.claudephone/env.sh"' >> "$HOME/.bashrc"
ok "environment written to ~/.claudephone/env.sh"

# --- 5. autostart on boot ---------------------------------------------------
if [ -d "$HOME/.termux/boot" ] || mkdir -p "$HOME/.termux/boot" 2>/dev/null; then
  cat > "$HOME/.termux/boot/claudephone" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
. \$HOME/.claudephone/env.sh
adb connect $LOOPBACK
exec python -m claudephone.cli serve --host 0.0.0.0 --port 8765
EOF
  chmod +x "$HOME/.termux/boot/claudephone"
  ok "boot script installed (needs the Termux:Boot app + auto-start enabled)"
fi

# --- 6. prove it works ------------------------------------------------------
echo
say "running doctor"
# shellcheck disable=SC1090
. "$HOME/.claudephone/env.sh"
python -m claudephone.cli doctor
RC=$?

echo
if [ $RC -eq 0 ]; then
  echo "${GREEN}${BOLD}ClaudePhone is ready.${RESET}"
else
  echo "${YELLOW}Setup finished with warnings - see doctor output above.${RESET}"
fi
cat <<EOF

  ${BOLD}try it${RESET}
    claudephone run "what app is open, and what is on screen?"

  ${BOLD}accept tasks from your laptop${RESET}
    claudephone serve --host 0.0.0.0
    ${DIM}then on the laptop: export CLAUDEPHONE_URL=http://$(
      ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 |
      head -1 || echo PHONE_IP):8765${RESET}

  ${BOLD}note${RESET}
    Wireless adb usually stops listening after a reboot. If the loopback is
    gone, re-enable Wireless debugging and re-run this script.
EOF
exit $RC
