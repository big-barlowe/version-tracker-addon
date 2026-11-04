#!/bin/bash
# vt-install — installs (or updates) vt-agent on this machine, so it reports
# its updates to the Version Tracker add-on in Home Assistant. The add-on
# builds this script with your MQTT login and the agent already inside.
#
# Run it as root. It asks for the name to show in Home Assistant and whether
# to report every hour; to skip the questions, set them up front:
#   VT_NAME=pve1 VT_SCHEDULE=yes bash vt-install.sh
#
# Running it again updates the agent and the login, and keeps your other
# settings (APPS, PICTURE, ...) in /etc/version-tracker.conf. Settings that
# newer versions added are appended there, commented out, with how to use them.

set -eu
export LC_ALL=C

@LOGIN@
CONF=/etc/version-tracker.conf
AGENT=/usr/local/bin/vt-agent
CRON=/etc/cron.d/version-tracker

say() { printf '%s\n' "$*"; }
die() { printf 'vt-install: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run this as root (or with sudo)"
[ -n "$BROKER" ] || die "no MQTT broker set; save the login on the Version Tracker page first"

TTY=0
if { : </dev/tty; } 2>/dev/null; then TTY=1; fi
ask() {  # ask <question> <default>: the answer, or the default without a terminal
  local a=""
  if [ "$TTY" = 1 ]; then read -r -p "$1 [$2]: " a </dev/tty || a=""; fi
  printf '%s' "${a:-$2}"
}

# --- Questions first, so nothing else reads what's typed ----------------------
current=""
if [ -r "$CONF" ]; then
  current="$( (MACHINE=""; . "$CONF"; printf '%s' "$MACHINE") 2>/dev/null || true)"
fi
name="${VT_NAME:-$(ask "Name for this machine in Home Assistant" "${current:-$(hostname -s)}")}"
[ -n "$name" ] || die "the name can't be empty"
schedule="${VT_SCHEDULE:-$(ask "Report every hour? (y/n)" y)}"

# --- MQTT client ------------------------------------------------------------
if ! command -v mosquitto_pub >/dev/null; then
  command -v apt-get >/dev/null || die "install mosquitto_pub (mosquitto-clients), then run this again"
  say "Installing mosquitto-clients..."
  export DEBIAN_FRONTEND=noninteractive
  if ! apt-get install -y -qq mosquitto-clients </dev/null >/dev/null 2>&1; then
    apt-get update -qq </dev/null >/dev/null 2>&1 || true
    apt-get install -y -qq mosquitto-clients </dev/null >/dev/null || die "could not install mosquitto-clients"
  fi
fi

# --- Settings file ----------------------------------------------------------
# Sets the login and name, and leaves every other line alone.
apply() {
  VT_KV="$(printf 'BROKER=%q\nPORT=%q\nMQTT_USER=%q\nMQTT_PASS=%q\nMACHINE=%q\n' \
    "$BROKER" "$PORT" "$MQTT_USER" "$MQTT_PASS" "$name")" awk '
    BEGIN {
      n = split(ENVIRON["VT_KV"], kv, "\n")
      for (i = 1; i <= n; i++) if (kv[i] != "") { k = kv[i]; sub(/=.*/, "", k); line[k] = kv[i]; order[++m] = k }
    }
    match($0, /^#?[A-Z_]+=/) {
      k = substr($0, 1, RLENGTH - 1); sub(/^#/, "", k)
      if (k in line) { if (!(k in done)) print line[k]; done[k] = 1; next }
    }
    { print }
    END { for (i = 1; i <= m; i++) if (!(order[i] in done)) print line[order[i]] }'
}

# Prints the sections of the template (comment lines plus a setting, up to a
# blank line) whose settings the file doesn't mention, set or commented out.
missing() {  # missing <file> <template>
  awk '
    function key(s) { if (!match(s, /^#?[A-Z_]+=/)) return ""; s = substr(s, 1, RLENGTH - 1); sub(/^#/, "", s); return s }
    function flush(   i, k, show) {
      show = 0
      for (i = 1; i <= n; i++) if ((k = key(blk[i])) != "") { if (k in have) { n = 0; return } show = 1 }
      if (show) {
        if (!added++) print "\n# --- Added by vt-install: settings you can use (remove the # to turn one on) ---"
        print ""; for (i = 1; i <= n; i++) print (blk[i] ~ /^[A-Z_]+=/ ? "#" : "") blk[i]
      }
      n = 0
    }
    FNR == NR { if ((k = key($0)) != "") have[k] = 1; next }
    /^[ \t]*$/ { flush(); next }
    { blk[++n] = $0 }
    END { flush() }' "$1" "$2"
}

tmp="$(mktemp)"; tmpl="$(mktemp)"
trap 'rm -f "$tmp" "$tmpl"' EXIT
cat > "$tmpl" <<'VT_CONF_EOF'
@CONF@
VT_CONF_EOF
if [ -r "$CONF" ]; then
  cp -p "$CONF" "$CONF.bak"
  apply < "$CONF" > "$tmp"
  new="$(missing "$tmp" "$tmpl")"
  [ -z "$new" ] || printf '%s\n' "$new" >> "$tmp"
  say "Updated $CONF (the old one is in $CONF.bak)"
  [ -z "$new" ] || say "  added new settings, commented out, at the end of it"
else
  apply < "$tmpl" > "$tmp"
  say "Wrote $CONF"
fi
install -m 600 "$tmp" "$CONF"

# --- Agent ------------------------------------------------------------------
mkdir -p "$(dirname "$AGENT")"
cat > "$AGENT.new" <<'VT_AGENT_EOF'
@AGENT@
VT_AGENT_EOF
chmod 755 "$AGENT.new"
mv -f "$AGENT.new" "$AGENT"
say "Installed $AGENT"

# --- Schedule ---------------------------------------------------------------
case "$schedule" in
  [Yy]*)
    if [ -d /etc/cron.d ]; then
      printf '# Reports this machine'"'"'s updates to Home Assistant (Version Tracker)\n%s * * * * root %s >/dev/null 2>&1\n' \
        "$((RANDOM % 60))" "$AGENT" > "$CRON"
      chmod 644 "$CRON"
      say "Scheduled: $CRON"
      command -v cron >/dev/null || command -v crond >/dev/null ||
        say "Note: cron doesn't seem to be installed (apt install cron), so the schedule won't run yet."
    else
      say "Note: no /etc/cron.d here, so run $AGENT every hour some other way."
    fi ;;
  *)
    rm -f "$CRON"
    say "Not scheduled. Run $AGENT whenever you want to report." ;;
esac

# --- First report -----------------------------------------------------------
say "Sending the first report..."
"$AGENT" || die "the report failed; check that $BROKER:$PORT is reachable and the login is right"
say "Done. $name shows up on the Version Tracker page within a few seconds."
