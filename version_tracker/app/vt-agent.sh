#!/bin/bash
# vt-agent — reports what needs updating on this machine to the Version
# Tracker add-on in Home Assistant, over MQTT. It never installs anything.
#
# What it reports:
#   * apt packages waiting to be installed (Debian, Ubuntu, Proxmox VE)
#   * the Proxmox VE version, when pve-manager is installed
#   * every running Docker container, with its version, image and when the
#     image was built; the add-on finds the project from the image's labels
#     or its name, and looks up the latest release
#   * any apps you list in APPS in the settings file
#
# Settings:  /etc/version-tracker.conf   (or: VT_CONF=/path/to/file vt-agent)
# Usage:     vt-agent             report now
#            vt-agent --dry-run   show what would be sent, send nothing

set -u
export LC_ALL=C
AGENT_VERSION="1.2.0"

DRY=0
case "${1:-}" in
  --dry-run|-n) DRY=1 ;;
  --help|-h) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac

CONF="${VT_CONF:-/etc/version-tracker.conf}"
BROKER=""; PORT=1883; MQTT_USER=""; MQTT_PASS=""
MACHINE="$(hostname -s)"; PICTURE=""; REFRESH_HOURS=6; DOCKER=auto
TOPIC_BASE="version_tracker"; APPS=()
STATE_DIR="${VT_STATE_DIR:-/var/lib/version-tracker}"

if [ -r "$CONF" ]; then
  # shellcheck source=/dev/null
  . "$CONF"
else
  echo "vt-agent: settings file $CONF not found" >&2; exit 1
fi
if [ "$DRY" = 0 ]; then
  [ -n "$BROKER" ] || { echo "vt-agent: set BROKER in $CONF" >&2; exit 1; }
  command -v mosquitto_pub >/dev/null ||
    { echo "vt-agent: mosquitto_pub is missing (apt install mosquitto-clients)" >&2; exit 1; }
fi

slugify() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '_'; }

json() {  # print a string as a quoted JSON string
  local s=$1
  s=${s//\\/\\\\}; s=${s//\"/\\\"}; s=${s//$'\t'/ }; s=${s//$'\r'/}; s=${s//$'\n'/ }
  printf '"%s"' "$s"
}

MACHINE_ID="$(slugify "$MACHINE")"
mkdir -p "$STATE_DIR"
SENT="$STATE_DIR/sent.new"
: > "$SENT"
FAILED=0
COUNT=0

MQTT_ARGS=(-h "$BROKER" -p "$PORT" -q 1)
[ -n "$MQTT_USER" ] && MQTT_ARGS+=(-u "$MQTT_USER")
[ -n "$MQTT_PASS" ] && MQTT_ARGS+=(-P "$MQTT_PASS")

publish() {  # publish <item> <json payload>
  local topic="$TOPIC_BASE/report/$MACHINE_ID/$1"
  if [ "$DRY" = 1 ]; then
    printf '%s\n  %s\n' "$topic" "$2"
  elif ! mosquitto_pub "${MQTT_ARGS[@]}" -r -t "$topic" -m "$2"; then
    echo "vt-agent: could not publish $1 to $BROKER:$PORT" >&2
    FAILED=1
    return
  fi
  echo "$1" >> "$SENT"
  COUNT=$((COUNT + 1))
}

keep() { echo "$1" >> "$SENT"; }  # don't clear an item this run, but don't report it either

# ---------------------------------------------------------------------------
# System packages and OS / Proxmox version
# ---------------------------------------------------------------------------
packages=""; pending=0; security=0
if command -v apt-get >/dev/null; then
  stamp="$STATE_DIR/apt-refreshed"
  if [ "$DRY" = 0 ] && [ "${REFRESH_HOURS:-0}" -gt 0 ] &&
     [ -z "$(find "$stamp" -mmin "-$((REFRESH_HOURS * 60))" 2>/dev/null)" ]; then
    if apt-get update -qq >/dev/null 2>&1; then touch "$stamp"
    else echo "vt-agent: apt-get update failed, using the package lists already on disk" >&2; fi
  fi
  sim="$(apt-get -s -o Debug::NoLocking=1 dist-upgrade 2>/dev/null)"
  packages="$(printf '%s\n' "$sim" | awk '/^Inst /{print $2}')"
  pending=$(printf '%s\n' "$sim" | grep -c '^Inst ')
  security=$(printf '%s\n' "$sim" | grep '^Inst ' | grep -ci 'security')
fi

os_id="$(. /etc/os-release 2>/dev/null; echo "${ID:-linux}")"
os_name="$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-Linux}")"
if dpkg-query -W -f='${Status}' pve-manager 2>/dev/null | grep -q 'ok installed'; then
  kind="proxmox"; title="Proxmox VE"
  installed="$(dpkg-query -W -f='${Version}' pve-manager)"
  latest="$(apt-cache policy pve-manager 2>/dev/null | awk '/Candidate:/{print $2}')"
  case "$latest" in ""|"(none)") latest="$installed" ;; esac
else
  kind="$os_id"; title="$os_name"
  if [ "$os_id" = "debian" ] && [ -r /etc/debian_version ]; then
    installed="$(cat /etc/debian_version)"
  else
    installed="$(. /etc/os-release 2>/dev/null; echo "${VERSION_ID:-unknown}")"
  fi
  latest="$installed"
fi

pkg_json="$(printf '%s\n' "$packages" | awk 'NF && n < 40 { printf "%s\"%s\"", (n++ ? "," : ""), $1 }')"
publish system "{\"machine\":$(json "$MACHINE"),\"name\":$(json "$MACHINE"),\"kind\":$(json "$kind"),\
\"title\":$(json "$title"),\"installed_version\":$(json "$installed"),\"latest_version\":$(json "$latest"),\
\"pending\":${pending:-0},\"security\":${security:-0},\"packages\":[$pkg_json],\"picture\":$(json "$PICTURE"),\
\"agent_version\":\"$AGENT_VERSION\"}"

# ---------------------------------------------------------------------------
# Apps listed in the settings file:  "Name|installed version|project link"
# ---------------------------------------------------------------------------
for app in "${APPS[@]}"; do
  IFS='|' read -r a_name a_ver a_url <<< "$app"
  [ -n "$a_name" ] || continue
  item="app_$(slugify "$a_name")"
  if [ -z "$a_ver" ]; then
    echo "vt-agent: no version found for $a_name, skipping it this time" >&2
    keep "$item"; continue
  fi
  publish "$item" "{\"machine\":$(json "$MACHINE"),\"name\":$(json "$a_name"),\"kind\":\"app\",\
\"installed_version\":$(json "$a_ver"),\"project_url\":$(json "$a_url")}"
done

# ---------------------------------------------------------------------------
# Docker containers
#   Labels you can put on a container (docker run -l / compose "labels:"),
#   though a project link can also be set on the Version Tracker page:
#     version_tracker.url=https://github.com/owner/repo   where releases come from
#     version_tracker.name=Nice name                      name in Home Assistant
#     version_tracker.ignore=true                         don't report it
# ---------------------------------------------------------------------------
version_like() { case "$1" in [0-9]*|[vV][0-9]*) return 0 ;; esac; return 1; }

if [ "$DOCKER" != 0 ] && command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  fmt='{{.Image}}|{{.Config.Image}}|{{index .Config.Labels "org.opencontainers.image.version"}}|{{index .Config.Labels "org.opencontainers.image.source"}}|{{index .Config.Labels "version_tracker.url"}}|{{index .Config.Labels "version_tracker.name"}}|{{index .Config.Labels "version_tracker.ignore"}}'
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    info="$(docker inspect -f "$fmt" "$c" 2>/dev/null | sed 's/<no value>//g')"
    IFS='|' read -r image_id image ver src vt_url vt_name vt_ignore <<< "$info"
    [ "$vt_ignore" = "true" ] && continue
    url="${vt_url:-$src}"
    tag="${image%@*}"; tag="${tag##*/}"
    case "$tag" in *:*) tag="${tag##*:}" ;; *) tag="latest" ;; esac
    # Prefer whichever of the version label and the tag looks like a version;
    # otherwise send what there is (e.g. "latest") and the add-on compares
    # the image's build date with the latest release instead.
    if ! version_like "$ver" && { version_like "$tag" || [ -z "$ver" ]; }; then ver="$tag"; fi
    created="$(docker image inspect -f '{{.Created}}' "$image_id" 2>/dev/null)"
    publish "docker_$(slugify "$c")" "{\"machine\":$(json "$MACHINE"),\"name\":$(json "${vt_name:-$c}"),\
\"kind\":\"docker\",\"installed_version\":$(json "$ver"),\"project_url\":$(json "$url"),\
\"image\":$(json "$image"),\"image_created\":$(json "$created")}"
  done < <(docker ps --format '{{.Names}}')
fi

# ---------------------------------------------------------------------------
# Clear items reported last time but not this time (e.g. a removed container),
# so they disappear from Home Assistant too.
# ---------------------------------------------------------------------------
if [ "$DRY" = 1 ]; then
  rm -f "$SENT"
  exit 0
fi
if [ "$FAILED" = 0 ]; then
  if [ -f "$STATE_DIR/sent" ]; then
    while IFS= read -r old; do
      [ -n "$old" ] || continue
      if ! grep -qxF "$old" "$SENT"; then
        mosquitto_pub "${MQTT_ARGS[@]}" -r -n -t "$TOPIC_BASE/report/$MACHINE_ID/$old" &&
          echo "vt-agent: removed $old (no longer on this machine)"
      fi
    done < "$STATE_DIR/sent"
  fi
  mv "$SENT" "$STATE_DIR/sent"
  echo "vt-agent: reported $COUNT item(s) for $MACHINE to $BROKER"
fi
exit "$FAILED"
