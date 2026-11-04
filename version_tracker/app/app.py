#!/usr/bin/env python3
"""
Version Tracker — a Home Assistant add-on.

Keeps one list of things that may need updating and shows each of them as an
MQTT `update` entity, so they appear under Settings -> Updates:

  * Projects you add in the web page (GitHub, GitLab or self-hosted GitLab
    releases, or a version you type in by hand).
  * Machines that report themselves over MQTT with the vt-agent script
    (apt / Proxmox packages, plus optional apps and Docker containers).

Nothing is ever installed: the entities have no install command, they are
reminders only.
"""

import base64
import hmac
import json
import logging
import os
import queue
import re
import secrets
import shlex
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

VERSION = "1.4.1"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("VT_DATA_DIR", "/data")
OPTIONS_FILE = os.path.join(DATA_DIR, "options.json")
STORE_FILE = os.path.join(DATA_DIR, "entries.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
STATIC_DIR = os.path.join(APP_DIR, "static")
AGENT_FILE = os.path.join(APP_DIR, "vt-agent.sh")
AGENT_CONF_FILE = os.path.join(APP_DIR, "version-tracker.conf.example")
INSTALL_FILE = os.path.join(APP_DIR, "vt-install.sh")
HTTP_PORT = int(os.environ.get("VT_HTTP_PORT", "8099"))
INSTALL_PORT = int(os.environ.get("VT_INSTALL_LISTEN", "8098"))  # inside the container
INSTALL_HOST_PORT = os.environ.get("VT_INSTALL_PORT", "")        # set under Network in the add-on, if at all
BASE = "version_tracker"
USER_AGENT = "version-tracker-ha-addon/" + VERSION
ICON_CDN = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/webp/"
LOGO_TTL = 7 * 86400


def read_agent_version():
    try:
        with open(AGENT_FILE) as f:
            m = re.search(r'^AGENT_VERSION="([^"]+)"', f.read(), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


AGENT_VERSION = read_agent_version()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("version_tracker")

DEFAULT_OPTIONS = {
    "check_interval_hours": 6,
    "stale_hours": 24,
    "github_token": "",
    "gitlab_tokens": [],
    "discovery_prefix": "homeassistant",
}


def load_options():
    opts = dict(DEFAULT_OPTIONS)
    try:
        with open(OPTIONS_FILE) as f:
            opts.update(json.load(f))
    except FileNotFoundError:
        log.warning("No %s found, using defaults", OPTIONS_FILE)
    return opts


def now_ts():
    return time.time()


def iso(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return s or "item"


# --------------------------------------------------------------------------
# Versions
# --------------------------------------------------------------------------

PRERELEASE_RE = re.compile(r"(alpha|beta|rc\d*|pre|preview|dev|nightly|snapshot)", re.I)
VERSION_LIKE_RE = re.compile(r"^v?\d", re.I)   # "1.2", "v3"; not "latest", "lts" or "pg18"
DATE_STAMP_RE = re.compile(r"^\d{8,}$")        # releases named like 202611031105


class NotFound(LookupError):
    """The project doesn't exist, or has no releases or version tags."""


def clean_tag(tag):
    return re.sub(r"^v(?=\d)", "", str(tag or "").strip(), flags=re.I)


def is_prerelease(tag):
    return bool(PRERELEASE_RE.search(str(tag or "")))


def parse_nums(v):
    m = re.search(r"\d+(?:\.\d+)*", str(v or ""))
    return [int(x) for x in m.group(0).split(".")] if m else None


def compare_versions(a, b):
    """-1, 0, 1, or None if either side has no numeric version."""
    x, y = parse_nums(a), parse_nums(b)
    if x is None or y is None:
        return None
    for i in range(max(len(x), len(y))):
        d = (x[i] if i < len(x) else 0) - (y[i] if i < len(y) else 0)
        if d:
            return -1 if d < 0 else 1
    return 0


def status_for(current, latest):
    if not current:
        return "No current version"
    if clean_tag(current) == clean_tag(latest):
        return "Up to date"
    c = compare_versions(current, latest)
    if c is None:
        return "Check manually"
    return "Update available" if c < 0 else "Up to date"


def highest_tag(names):
    tags = [t for t in names if parse_nums(t) and not is_prerelease(t)]
    if not tags:
        raise NotFound("no releases or version tags")
    best = tags[0]
    for t in tags[1:]:
        if compare_versions(t, best) == 1:
            best = t
    return best


# --------------------------------------------------------------------------
# GitHub / GitLab lookups
# --------------------------------------------------------------------------

def parse_project(link):
    """Turn a link into {provider, scheme, host, path, web, key}."""
    s = str(link or "").strip()
    s = re.sub(r"[?#].*$", "", s).rstrip("/")
    s = re.sub(r"\.git$", "", s, flags=re.I).rstrip("/")
    m = re.match(r"^git@([^:/\s]+):(.+)$", s)  # ssh form, seen in some image labels
    if m:
        s = "https://%s/%s" % (m.group(1), m.group(2))
    m = re.match(r"^gitlab:(.+)$", s, re.I)
    if m:
        s = "https://gitlab.com/" + m.group(1).lstrip("/")
    elif re.match(r"^[\w-]+/[\w.-]+$", s):
        s = "https://github.com/" + s
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    m = re.match(r"^(https?)://([^/\s]+)/(.+)$", s, re.I)
    if not m:
        raise ValueError("not a project link")
    scheme, host, path = m.group(1).lower(), m.group(2).lower(), m.group(3)

    if host in ("github.com", "www.github.com"):
        parts = path.split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError("not a GitHub repo link")
        path = parts[0] + "/" + parts[1]
        return {"provider": "github", "scheme": "https", "host": "github.com", "path": path,
                "web": "https://github.com/" + path, "key": "github.com/" + path.lower()}

    path = path.split("/-/")[0]
    path = re.sub(r"/(releases|tags|tree|blob|commits|issues|merge_requests|wikis)(/.*)?$", "", path, flags=re.I)
    path = path.rstrip("/")
    if len(path.split("/")) < 2:
        raise ValueError("not a GitLab project link")
    return {"provider": "gitlab", "scheme": scheme, "host": host, "path": path,
            "web": "%s://%s/%s" % (scheme, host, path), "key": host + "/" + path.lower()}


def http_get(url, headers):
    req = urllib.request.Request(url, headers=dict(headers, **{"User-Agent": USER_AGENT}))
    host = urllib.parse.urlsplit(url).hostname
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers
    except (urllib.error.URLError, OSError) as e:
        raise LookupError("can't reach %s (%s)" % (host, getattr(e, "reason", e)))


def github_headers(opts):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if opts.get("github_token"):
        headers["Authorization"] = "Bearer " + opts["github_token"]
    return headers


def latest_github(p, opts):
    base = "https://api.github.com/repos/" + p["path"]
    headers = github_headers(opts)

    def fail(code, hdrs):
        if code in (403, 429) and str(hdrs.get("X-RateLimit-Remaining", "")) == "0":
            raise LookupError("GitHub rate limit hit (add a GitHub token in the add-on settings)")
        raise LookupError("GitHub HTTP %s" % code)

    code, body, hdrs = http_get(base + "/releases/latest", headers)
    if code == 200:
        r = json.loads(body)
        return {"version": clean_tag(r.get("tag_name")), "released": r.get("published_at") or "",
                "notes_url": r.get("html_url") or p["web"] + "/releases"}
    if code != 404:
        fail(code, hdrs)

    code, body, hdrs = http_get(base + "/tags?per_page=100", headers)
    if code == 404:
        raise NotFound("repo not found on GitHub")
    if code != 200:
        fail(code, hdrs)
    tag = highest_tag([t.get("name", "") for t in json.loads(body)])
    return {"version": clean_tag(tag), "released": "",
            "notes_url": p["web"] + "/releases/tag/" + urllib.parse.quote(tag, safe="")}


def gitlab_token(opts, host):
    for t in opts.get("gitlab_tokens") or []:
        h = re.sub(r"^https?://", "", str(t.get("host", "")).strip().lower()).split("/")[0]
        if h == host and t.get("token"):
            return t["token"]
    return ""


def gitlab_api(p, opts):
    api = "%s://%s/api/v4/projects/%s" % (p["scheme"], p["host"], urllib.parse.quote(p["path"], safe=""))
    headers = {}
    token = gitlab_token(opts, p["host"])  # only ever sent to the host it was saved for
    if token:
        headers["PRIVATE-TOKEN"] = token
    return api, headers


def latest_gitlab(p, opts):
    api, headers = gitlab_api(p, opts)

    def fail(code):
        if code == 404:
            raise NotFound("project not found on %s (private? add a GitLab token)" % p["host"])
        if code in (401, 403):
            raise LookupError("access denied by %s (check the GitLab token)" % p["host"])
        if code == 429:
            raise LookupError("GitLab rate limit hit (add a token or try later)")
        raise LookupError("GitLab HTTP %s from %s" % (code, p["host"]))

    def as_list(body):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if not isinstance(data, list):
            raise LookupError("%s did not answer like a GitLab server" % p["host"])
        return data

    code, body, _ = http_get(api + "/releases?per_page=50", headers)
    if code != 200:
        fail(code)
    releases = [r for r in as_list(body) if not r.get("upcoming_release") and not is_prerelease(r.get("tag_name"))]
    if releases:
        r = releases[0]
        links = r.get("_links") or {}
        return {"version": clean_tag(r.get("tag_name")), "released": r.get("released_at") or "",
                "notes_url": links.get("self") or p["web"] + "/-/releases/" + urllib.parse.quote(r["tag_name"], safe="")}

    code, body, _ = http_get(api + "/repository/tags?per_page=100&order_by=version", headers)
    if code == 400:  # GitLab before 15.4 can't sort tags by version
        code, body, _ = http_get(api + "/repository/tags?per_page=100", headers)
    if code != 200:
        fail(code)
    tag = highest_tag([t.get("name", "") for t in as_list(body)])
    return {"version": clean_tag(tag), "released": "",
            "notes_url": p["web"] + "/-/tags/" + urllib.parse.quote(tag, safe="")}


def lookup_latest(link, opts):
    p = parse_project(link)
    return latest_github(p, opts) if p["provider"] == "github" else latest_gitlab(p, opts)


# --------------------------------------------------------------------------
# Logos: a picture near the top of the README, else the org / project avatar
# --------------------------------------------------------------------------

README_IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^\s)>]+)[^)]*\)|<img\b([^>]*)>", re.I)
NOT_LOGO_RE = re.compile(
    r"shields?\.io|/shield/|contribute\.design|badgen|badge|/workflows?/|/actions\b|codecov|coveralls|travis-ci|circleci|goreportcard|"
    r"sonarcloud|bestpractices|fossa|snyk|discord|gitter|opencollective|buymeacoffee|ko-fi|liberapay|"
    r"patreon|paypal|sponsor|star-history|contrib\.rocks|weblate|crowdin|deepwiki|trendshift|producthunt|"
    r"repobeats|my\.home-assistant|screenshot|screen-shot|demo|\.gif(\?|$)", re.I)


def html_attr(attrs, name):
    m = re.search(r"\b%s\s*=\s*[\"']([^\"']*)[\"']" % name, attrs, re.I)
    return m.group(1).strip() if m else ""


def readme_logo(text, base, name="", root=""):
    """The likeliest logo among the first pictures of a README, skipping badges.
    A picture named after the project wins (linuxserver.io READMEs start with
    their own logo, then the app's), then one called logo or icon."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)[:6000]
    found = []
    for m in README_IMG_RE.finditer(text):
        if m.group(3) is not None:
            src, alt = html_attr(m.group(3), "src"), html_attr(m.group(3), "alt") + " " + html_attr(m.group(3), "title")
        else:
            src, alt = m.group(2).strip(), m.group(1)
        if not src or NOT_LOGO_RE.search(src) or NOT_LOGO_RE.search(alt):
            continue
        if src.startswith("/") and not src.startswith("//") and root:
            src = root + src.lstrip("/")  # relative to the repo, not the web server
        url = urllib.parse.urljoin(base, src)
        url = re.sub(r"^(https://github\.com/[^/]+/[^/]+)/blob/", r"\1/raw/", url)
        if url.startswith(("http://", "https://")):
            found.append((alt, url))
        if len(found) == 6:
            break
    name = re.sub(r"^docker-|[-_.]?docker$", "", name.lower())
    for pattern in ([re.escape(name)] if len(name) > 2 else []) + [r"logo|icon|brand"]:
        for alt, url in found:
            if re.search(pattern, alt + " " + url.rsplit("/", 1)[-1], re.I):
                return url
    return found[0][1] if found else ""


def logo_github(p, opts):
    headers = github_headers(opts)
    code, body, _ = http_get("https://api.github.com/repos/%s/readme" % p["path"], headers)
    if code == 200:
        r = json.loads(body)
        text = base64.b64decode(r.get("content") or "").decode("utf-8", "replace")
        base = r.get("download_url") or p["web"] + "/raw/HEAD/" + (r.get("path") or "")
        path = r.get("path") or ""
        root = base[: -len(path)] if path and base.endswith(path) else ""
        url = readme_logo(text, base, p["path"].split("/")[1], root)
        if url:
            return url
    elif code != 404:
        raise LookupError("GitHub HTTP %s" % code)
    # No picture in the README: use the organisation's avatar (not a person's).
    code, body, _ = http_get("https://api.github.com/users/" + p["path"].split("/")[0], headers)
    if code == 200:
        u = json.loads(body)
        if u.get("type") == "Organization" and u.get("avatar_url"):
            return u["avatar_url"]
    return ""


def logo_gitlab(p, opts):
    api, headers = gitlab_api(p, opts)
    root = "%s://%s/" % (p["scheme"], p["host"])
    code, body, _ = http_get(api, headers)
    if code != 200:
        raise LookupError("GitLab HTTP %s" % code)
    proj = json.loads(body)
    if proj.get("avatar_url"):
        return urllib.parse.urljoin(root, proj["avatar_url"])
    branch, readme = proj.get("default_branch") or "", proj.get("readme_url") or ""
    marker = "/-/blob/%s/" % branch
    if branch and marker in readme:
        path = readme.split(marker, 1)[1]
        code, text, _ = http_get("%s/repository/files/%s/raw?ref=%s" % (
            api, urllib.parse.quote(path, safe=""), urllib.parse.quote(branch, safe="")), headers)
        if code == 200:
            root = "%s/-/raw/%s/" % (p["web"], branch)
            url = readme_logo(text, root + path, p["path"].rsplit("/", 1)[-1], root)
            if url:
                return url
    ns = proj.get("namespace") or {}
    if ns.get("kind") == "group" and ns.get("avatar_url"):
        return urllib.parse.urljoin(root, ns["avatar_url"])
    return ""


def guess_project(image):
    """A likely source repo for a container image that has no source label,
    e.g. ghcr.io/owner/app -> github.com/owner/app, redis -> github.com/redis/redis.
    The add-on drops the guess if the repo doesn't exist or has no releases."""
    ref = re.sub(r"@.*$", "", str(image or "").strip())
    ref = re.sub(r":[^/:]*$", "", ref)  # the tag
    parts = [x for x in ref.split("/") if x]
    if not parts or ref.startswith("sha256"):
        return ""
    registry = "docker.io"
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, parts = parts[0].lower(), parts[1:]
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io") and (len(parts) == 1 or parts[0] == "library"):
        return "https://github.com/%s/%s" % (parts[-1], parts[-1])
    if registry == "lscr.io" and len(parts) == 2 and parts[0] == "linuxserver":
        return "https://github.com/linuxserver/docker-" + parts[1]
    if registry == "registry.gitlab.com" and len(parts) >= 2:
        return "https://gitlab.com/%s/%s" % (parts[0], parts[1])
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io", "ghcr.io", "quay.io") and len(parts) >= 2:
        return "https://github.com/%s/%s" % (parts[0], parts[1])
    return ""


def lookup_logo(link, opts):
    p = parse_project(link)
    return logo_github(p, opts) if p["provider"] == "github" else logo_gitlab(p, opts)


# --------------------------------------------------------------------------
# Entries
# --------------------------------------------------------------------------
#
# Every entry has: id, source ("project" or "mqtt"), name, current, url,
# latest_manual (typed by hand, or sent by an agent), found_* (from a
# GitHub/GitLab lookup, including found_picture and logo_checked), picture.
# MQTT entries also carry machine, kind, topic, pending, security, packages,
# last_seen, and for containers image, image_created and url_guessed.
# What's set on the page for an entry (notes; for reported entries also a
# picture and link) is kept in Settings and laid over it by Tracker.merged().

KIND_LABELS = {"proxmox": "Proxmox", "debian": "Debian", "ubuntu": "Ubuntu", "docker": "Docker", "app": "App",
               "package": "Package"}
KIND_ICONS = {"proxmox": "proxmox", "docker": "docker", "debian": "debian-linux", "ubuntu": "ubuntu-linux"}


def effective_latest(e):
    return e.get("latest_manual") or e.get("found_version") or ""


def generic_picture(e):
    if e.get("source") != "mqtt":
        return ""
    if e.get("kind") == "package":
        pkg = e.get("package") or ""
        icon = ("proxmox-backup-server" if pkg.startswith("proxmox-backup") else
                "proxmox" if pkg.startswith(("proxmox-", "pve-")) else KIND_ICONS.get(e.get("os") or "", "linux"))
        return ICON_CDN + icon + ".webp"
    icon = KIND_ICONS.get(e.get("kind") or "") or ("linux" if is_system(e) else "")
    return ICON_CDN + icon + ".webp" if icon else ""


def picture_for(e):
    """A picture set by hand or by the agent, else the project's logo, else a generic one for machines."""
    pic = e.get("picture") or e.get("found_picture") or generic_picture(e)
    return pic if pic.startswith(("http://", "https://")) else ""


def is_system(e):
    return (e.get("topic") or "").endswith("/system")


def iso_ts(text):
    """Seconds since 1970 for an ISO time like 2026-04-30T10:00:00Z (UTC), or None."""
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def built_after_release(e, latest):
    """For a container whose tag isn't a version (latest, lts, pg18) or whose
    project names releases by date: is its image newer than the latest release?
    None when that can't be told."""
    if not e.get("image_created") or e.get("latest_manual"):
        return None
    if VERSION_LIKE_RE.match(e.get("current") or "") and not DATE_STAMP_RE.match(clean_tag(latest)):
        return None
    built, released = iso_ts(e["image_created"]), iso_ts(e.get("found_released"))
    if built is None or released is None:
        return None
    return built >= released - 86400  # images are often built just before the release is published


def source_label(e):
    if e.get("source") == "mqtt":
        return KIND_LABELS.get(e.get("kind") or "", "Machine")
    if e.get("url"):
        try:
            return "GitHub" if parse_project(e["url"])["provider"] == "github" else "GitLab"
        except ValueError:
            return "Link"
    return "Manual"


def evaluate(e, opts):
    """Work out status and what Home Assistant should show for an entry."""
    current = e.get("current") or ""
    latest = effective_latest(e)
    pending = int(e.get("pending") or 0)
    stale = (e.get("source") == "mqtt" and
             now_ts() - (e.get("last_seen") or 0) > float(opts["stale_hours"]) * 3600)
    pinned = bool(current) and e.get("pinned") == current  # a pin lapses once the version changes

    if stale:
        status = "Not reporting"
    elif e.get("url") and not latest and not e.get("latest_manual"):
        status = "Error" if e.get("error") else "Checking"
    elif latest and e.get("kind") == "package":
        status = "Up to date" if latest == current else "Update available"  # the agent asked apt which is newer
    elif latest:
        status = status_for(current, latest)
        by_date = built_after_release(e, latest)
        if by_date is not None:
            status = "Up to date" if by_date else "Update available"
    elif e.get("source") == "mqtt" and e.get("kind") in ("docker", "app"):
        status = "No project link"
    else:
        status = "Up to date"
    if not stale and pending > 0:
        status = "Update available"
    if pinned and status in ("Update available", "Check manually"):
        status = "Pinned"  # Home Assistant is told it's up to date (ha_latest = current)

    # The version string Home Assistant compares against the installed one.
    if status == "Not reporting":
        ha_latest = (current or "?") + " (not reporting)"
    elif status == "Update available":
        newer = latest and clean_tag(latest) != clean_tag(current) and (
            e.get("kind") == "package" or compare_versions(current, latest) != 0)
        ha_latest = latest if newer else current
        if pending:
            ha_latest += " (+%d package%s)" % (pending, "" if pending == 1 else "s")
    elif status == "Check manually":
        ha_latest = latest
    else:
        ha_latest = current

    return {"status": status, "latest": latest, "ha_latest": ha_latest, "stale": stale,
            "source_label": source_label(e), "by_date": latest and built_after_release(e, latest) is not None,
            "pinned": pinned}


def build_summary(e, ev, opts):
    machine = e.get("machine") or e.get("name")
    if ev["stale"]:
        seen = datetime.fromtimestamp(e.get("last_seen") or 0, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return "No report from %s since %s. Check that it is on and its vt-agent cron job is running." % (machine, seen)
    if ev["status"] == "Pinned":
        avail = ev["latest"] if ev["latest"] and ev["latest"] != e.get("current") else ""
        pending = int(e.get("pending") or 0)
        return "Pinned at %s%s.%s" % (e.get("current"), "; %s is available" % avail if avail else "",
                                      " %d package%s waiting." % (pending, "" if pending == 1 else "s") if pending else "")
    if e.get("summary"):
        return e["summary"]
    pending = int(e.get("pending") or 0)
    if pending:
        sec = int(e.get("security") or 0)
        head = "%d package%s waiting on %s" % (pending, "" if pending == 1 else "s", machine)
        if sec:
            head += " (%d security)" % sec
        pkgs = e.get("packages") or []
        return head + (": " + ", ".join(pkgs) if pkgs else ".")
    if e.get("error"):
        return "Last check failed: " + e["error"]
    if ev["status"] == "No project link":
        return "No project link for %s, so its releases aren't checked. Add one on the Version Tracker page." % e.get("name")
    if ev["by_date"]:
        return "The %s image was built %s; %s was released %s." % (
            e.get("name"), e["image_created"][:10], ev["latest"], (e.get("found_released") or "")[:10])
    if ev["status"] == "Update available" and ev["latest"]:
        when = (e.get("found_released") or "")[:10]
        return "%s %s is available%s." % (e.get("name"), ev["latest"], " (released %s)" % when if when else "")
    return ""


def truncate(s, n=255):
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "\u2026"


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.entries = {}
        try:
            with open(path) as f:
                for e in json.load(f):
                    self.entries[e["id"]] = e
        except FileNotFoundError:
            pass
        except (ValueError, KeyError) as err:
            log.error("Could not read %s (%s); starting empty", path, err)

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(list(self.entries.values()), f, indent=1)
            os.replace(tmp, self.path)

    def get(self, eid):
        with self.lock:
            e = self.entries.get(eid)
            return dict(e) if e else None

    def all(self):
        with self.lock:
            return [dict(e) for e in self.entries.values()]

    def put(self, e):
        with self.lock:
            self.entries[e["id"]] = e
            self.save()

    def update(self, eid, **fields):
        with self.lock:
            if eid not in self.entries:
                return None
            self.entries[eid].update(fields)
            self.save()
            return dict(self.entries[eid])

    def delete(self, eid):
        with self.lock:
            e = self.entries.pop(eid, None)
            self.save()
            return e


class Settings:
    """Things set on the web page: the MQTT login machines use, the secret in
    the one-line installer link, ignored entries, and per-entry overrides
    (notes, a pinned version, and a picture and link for reported entries). They're kept here
    rather than on the entry, so they survive an agent clearing an item and
    reporting it again."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"agent": {"broker": "", "port": 1883, "username": "", "password": ""},
                     "install_token": "", "ignored": [], "overrides": {}}
        try:
            with open(path) as f:
                saved = json.load(f)
            self.data["agent"].update(saved.get("agent") or {})
            self.data["install_token"] = saved.get("install_token") or ""
            self.data["ignored"] = list(saved.get("ignored") or [])
            self.data["overrides"] = dict(saved.get("overrides") or {})
        except FileNotFoundError:
            pass
        except (ValueError, AttributeError) as err:
            log.error("Could not read %s (%s); starting with defaults", path, err)
        if not self.data["install_token"]:
            self.new_token()

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def agent(self):
        with self.lock:
            return dict(self.data["agent"])

    def set_agent(self, data):
        broker = str(data.get("broker") or "").strip()
        if not re.match(r"^[A-Za-z0-9._:\[\]-]+$", broker):
            raise ValueError("Enter the broker's IP address or host name")
        try:
            port = int(data.get("port") or 1883)
            assert 0 < port < 65536
        except (ValueError, AssertionError):
            raise ValueError("Port must be a number from 1 to 65535")
        with self.lock:
            self.data["agent"] = {"broker": broker, "port": port, "username": str(data.get("username") or "").strip(),
                                  "password": str(data.get("password") or "")}
            self.save()

    def token(self):
        with self.lock:
            return self.data["install_token"]

    def new_token(self):
        with self.lock:
            self.data["install_token"] = secrets.token_urlsafe(18)
            self.save()

    def is_ignored(self, eid):
        with self.lock:
            return eid in self.data["ignored"]

    def ignored(self):
        with self.lock:
            return list(self.data["ignored"])

    def override(self, eid):
        with self.lock:
            return dict(self.data["overrides"].get(eid) or {})

    def set_override(self, eid, **fields):
        """Change some of an entry's overrides; empty values are dropped."""
        with self.lock:
            o = dict(self.data["overrides"].get(eid) or {}, **fields)
            o = {k: v for k, v in o.items() if v}
            if o:
                self.data["overrides"][eid] = o
            else:
                self.data["overrides"].pop(eid, None)
            self.save()

    def forget(self, eid):
        with self.lock:
            self.data["overrides"].pop(eid, None)
            if eid in self.data["ignored"]:
                self.data["ignored"].remove(eid)
            self.save()

    def set_ignored(self, eid, ignored):
        with self.lock:
            if ignored == (eid in self.data["ignored"]):
                return
            if ignored:
                self.data["ignored"].append(eid)
            else:
                self.data["ignored"].remove(eid)
            self.save()


# --------------------------------------------------------------------------
# The tracker: MQTT, lookups, scheduling
# --------------------------------------------------------------------------

class Tracker:
    def __init__(self, opts):
        self.opts = opts
        self.store = Store(STORE_FILE)
        self.settings = Settings(SETTINGS_FILE)
        self.prefix = (opts.get("discovery_prefix") or "homeassistant").strip("/")
        self.published = {}          # entry id -> last published payloads
        self.cache = {}              # project key -> (time, result or error)
        self.logos = {}              # project key -> (time, picture link)
        self.cache_lock = threading.Lock()
        self.jobs = queue.Queue()
        self.queued = set()
        self.last_full_check = 0
        self.connected = False
        self.broker = ""
        self.client = None

    # ---- MQTT ----

    def mqtt_settings(self):
        o = self.opts
        if o.get("mqtt_host"):
            return {"host": o["mqtt_host"], "port": int(o.get("mqtt_port") or 1883),
                    "username": o.get("mqtt_username") or "", "password": o.get("mqtt_password") or "",
                    "ssl": False}
        host = os.environ.get("MQTT_HOST", "")
        return {"host": host, "port": int(os.environ.get("MQTT_PORT") or 1883),
                "username": os.environ.get("MQTT_USERNAME", ""), "password": os.environ.get("MQTT_PASSWORD", ""),
                "ssl": os.environ.get("MQTT_SSL", "false").lower() == "true"}

    def start_mqtt(self):
        s = self.mqtt_settings()
        if not s["host"]:
            log.error("No MQTT broker found. Install the Mosquitto broker add-on, or set mqtt_host in the add-on settings.")
            return
        self.broker = "%s:%s" % (s["host"], s["port"])
        client_id = "version-tracker-" + uuid.uuid4().hex[:6]
        if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt 2.x
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        else:                                    # paho-mqtt 1.x
            c = mqtt.Client(client_id=client_id)
        if s["username"]:
            c.username_pw_set(s["username"], s["password"])
        if s["ssl"]:
            c.tls_set()
        c.will_set(BASE + "/status", "offline", retain=True)
        c.on_connect = self.on_connect
        c.on_disconnect = self.on_disconnect
        c.on_message = self.on_message
        c.reconnect_delay_set(2, 60)
        self.client = c
        c.connect_async(s["host"], s["port"], keepalive=60)
        c.loop_start()

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = reason_code.is_failure if hasattr(reason_code, "is_failure") else reason_code != 0
        if failed:
            log.error("MQTT connection refused: %s (check the broker username and password)", reason_code)
            return
        log.info("Connected to MQTT broker %s", self.broker)
        self.connected = True
        client.publish(BASE + "/status", "online", retain=True)
        client.subscribe([(BASE + "/report/#", 1), (self.prefix + "/status", 1)])
        self.published.clear()
        self.publish_all()
        for eid in self.settings.ignored():  # in case it was ignored while the broker was away
            self.unpublish(eid)

    def on_disconnect(self, client, userdata, *args):  # signature differs between paho 1.x and 2.x
        self.connected = False
        log.warning("Disconnected from MQTT broker, retrying")

    def on_message(self, client, userdata, msg):
        try:
            if msg.topic == self.prefix + "/status":
                if msg.payload.decode(errors="replace") == "online":
                    log.info("Home Assistant restarted, re-sending entities")
                    self.published.clear()
                    self.publish_all()
                return
            suffix = msg.topic[len(BASE + "/report/"):]
            if suffix:
                self.on_report(suffix, msg.payload.decode("utf-8", "replace"))
        except Exception:
            log.exception("Error handling message on %s", msg.topic)

    def pub(self, topic, payload, retain=True):
        if self.client and self.connected:
            self.client.publish(topic, payload, qos=1, retain=retain)

    # ---- Home Assistant entities ----

    def topics(self, eid):
        return {"config": "%s/update/%s_%s/config" % (self.prefix, BASE, eid),
                "state": "%s/entity/%s/state" % (BASE, eid),
                "attrs": "%s/entity/%s/attributes" % (BASE, eid)}

    def discovery(self, e, t):
        if e.get("source") == "mqtt":
            machine = e.get("machine") or e.get("name")
            device = {"identifiers": ["%s_machine_%s" % (BASE, slug(machine))], "name": machine,
                      "manufacturer": "Version Tracker", "model": "Self-reporting machine"}
            name = None if e.get("topic", "").endswith("/system") else e.get("name")
        else:
            device = {"identifiers": ["%s_%s" % (BASE, e["id"])], "name": e["name"],
                      "manufacturer": "Version Tracker", "model": source_label(e) + " release"
                      if e.get("url") else "Manual entry"}
            if e.get("url"):
                try:
                    device["configuration_url"] = parse_project(e["url"])["web"]
                except ValueError:
                    pass
            name = None
        cfg = {"name": name, "unique_id": "%s_%s" % (BASE, e["id"]),
               "state_topic": t["state"], "json_attributes_topic": t["attrs"],
               "availability_topic": BASE + "/status", "device": device,
               "origin": {"name": "Version Tracker", "sw_version": VERSION}}
        picture = picture_for(e)
        if picture:
            cfg["entity_picture"] = picture
        return cfg

    def merged(self, e):
        """The entry as shown: with the notes, picture and link set on the page."""
        if not e:
            return e
        e = dict(e)
        o = self.settings.override(e["id"])
        if o.get("picture"):
            e["picture"] = o["picture"]
        if o.get("url"):
            e.update(url=o["url"], url_guessed=False, url_set=True)
        e["notes"] = o.get("notes", "")
        e["pinned"] = o.get("pinned", "")
        return e

    def publish_entry(self, e):
        if self.settings.is_ignored(e["id"]):
            return
        e = self.merged(e)
        t = self.topics(e["id"])
        ev = evaluate(e, self.opts)
        state = {"installed_version": e.get("current") or "unknown", "latest_version": ev["ha_latest"] or e.get("current") or "unknown",
                 "title": e.get("title") or e.get("name"), "release_summary": truncate(build_summary(e, ev, self.opts))}
        url = e.get("found_notes_url") or e.get("release_url") or e.get("url")
        if url and url.startswith(("http://", "https://")):
            state["release_url"] = url
        attrs = {"status": ev["status"], "source": ev["source_label"], "project_url": e.get("url") or None,
                 "machine": e.get("machine") or None, "last_checked": iso(e.get("checked")) or None,
                 "last_seen": iso(e.get("last_seen")) or None}
        if e.get("notes"):
            attrs["notes"] = e["notes"]
        if ev["pinned"]:
            attrs.update(pinned_version=e.get("current"), available_version=ev["latest"] or None)
        if e.get("source") == "mqtt":
            attrs.update(pending_packages=int(e.get("pending") or 0), security_updates=int(e.get("security") or 0))
        payloads = (json.dumps(self.discovery(e, t)), json.dumps(state), json.dumps(attrs))
        if self.published.get(e["id"]) == payloads or not self.connected:
            return
        self.pub(t["config"], payloads[0])
        self.pub(t["attrs"], payloads[2])
        self.pub(t["state"], payloads[1])
        self.published[e["id"]] = payloads

    def unpublish(self, eid):
        for topic in self.topics(eid).values():
            self.pub(topic, "")
        self.published.pop(eid, None)

    def publish_all(self):
        for e in self.store.all():
            self.publish_entry(e)

    # ---- Reports from machines ----

    def on_report(self, suffix, payload):
        eid = "m_" + slug(suffix)
        if not payload.strip():  # the agent cleared this item (e.g. a removed container)
            if self.store.delete(eid):
                log.info("Removed %s (cleared by its agent)", suffix)
                self.unpublish(eid)
            return
        try:
            data = json.loads(payload)
            assert isinstance(data, dict)
        except (ValueError, AssertionError):
            log.warning("Ignoring report on %s: not a JSON object", suffix)
            return
        installed = str(data.get("installed_version") or "").strip()
        if not installed:
            log.warning("Ignoring report on %s: no installed_version", suffix)
            return

        def num(v):
            try:
                return max(0, int(v))
            except (TypeError, ValueError):
                return 0

        machine = str(data.get("machine") or suffix.split("/")[0])
        e = self.store.get(eid) or {"id": eid, "source": "mqtt", "created": now_ts()}
        url = str(data.get("project_url") or "").strip()
        if url:
            try:
                parse_project(url)
            except ValueError:
                url = ""
        image = str(data.get("image") or "")
        guessed = ""
        if not url and image:
            guessed = guess_project(image)
            if guessed == e.get("guess_failed") and now_ts() - (e.get("guess_failed_at") or 0) < LOGO_TTL:
                guessed = ""  # tried last time: no such repo, or no releases
            url = guessed
        if url != e.get("url"):
            e.update(found_version="", found_released="", found_notes_url="", error="", checked=0,
                     found_picture="", logo_checked=0)
        e.update(topic=suffix, machine=machine, name=str(data.get("name") or machine),
                 title=str(data.get("title") or "") or None, kind=str(data.get("kind") or ""),
                 current=installed, latest_manual=str(data.get("latest_version") or "").strip(),
                 url=url, pending=num(data.get("pending")), security=num(data.get("security")),
                 packages=[str(p) for p in (data.get("packages") or [])][:50],
                 picture=str(data.get("picture") or ""), release_url=str(data.get("release_url") or ""),
                 summary=str(data.get("summary") or ""), last_seen=now_ts(), url_guessed=bool(guessed),
                 image=image, image_created=str(data.get("image_created") or ""),
                 package=str(data.get("package") or ""), os=str(data.get("os") or ""),
                 agent_version=str(data.get("agent_version") or ""))
        self.store.put(e)
        self.publish_entry(e)
        e = self.merged(e)
        if e.get("url") and not e.get("latest_manual"):
            self.enqueue(eid)

    # ---- Lookups ----

    def enqueue(self, eid, force=False):
        if eid in self.queued:
            return
        self.queued.add(eid)
        self.jobs.put((eid, force))

    def worker(self):
        while True:
            eid, force = self.jobs.get()
            self.queued.discard(eid)
            try:
                self.check_entry(eid, force)
            except Exception:
                log.exception("Check failed for %s", eid)

    def check_entry(self, eid, force=False):
        e = self.merged(self.store.get(eid))
        if not e or not e.get("url"):
            return
        try:
            key = parse_project(e["url"])["key"]
        except ValueError as err:
            self.store.update(eid, error=str(err), checked=now_ts())
            self.publish_entry(self.store.get(eid))
            return
        ttl = max(1.0, float(self.opts["check_interval_hours"])) * 3600 - 120
        with self.cache_lock:
            hit = self.cache.get(key)
        if force or not hit or now_ts() - hit[0] > ttl:
            try:
                result = lookup_latest(e["url"], self.opts)
            except NotFound as err:
                result = {"error": str(err), "not_found": True}
            except (LookupError, ValueError) as err:
                result = {"error": str(err)}
            except Exception as err:  # malformed JSON etc.
                result = {"error": "unexpected answer (%s)" % err.__class__.__name__}
            hit = (now_ts(), result)
            with self.cache_lock:
                self.cache[key] = hit
        result = hit[1]
        if result.get("not_found") and e.get("url_guessed"):
            log.info("No releases at %s (guessed from the image of %s); add a link on the page", e["url"], e["name"])
            e = self.store.update(eid, url="", url_guessed=False, guess_failed=e["url"], guess_failed_at=now_ts(),
                                  error="", checked=hit[0])
            if e:
                self.publish_entry(e)
            return
        if "error" in result:
            fields = {"error": result["error"]}
        else:
            fields = {"error": "", "found_version": result["version"], "found_released": result["released"],
                      "found_notes_url": result["notes_url"]}
        fields["checked"] = hit[0]
        if not e.get("picture") and now_ts() - (e.get("logo_checked") or 0) > LOGO_TTL:
            fields.update(self.find_logo(key, e["url"]))
        e = self.store.update(eid, **fields)
        if e:
            self.publish_entry(e)

    def find_logo(self, key, url):
        """found_picture and logo_checked for a project, looked up at most once a week."""
        with self.cache_lock:
            hit = self.logos.get(key)
        if not hit or now_ts() - hit[0] > LOGO_TTL:
            try:
                hit = (now_ts(), lookup_logo(url, self.opts))
            except Exception as err:  # only a picture, so try again in an hour
                log.info("Could not look up a logo for %s (%s)", key, err)
                hit = (now_ts() - LOGO_TTL + 3600, "")
            with self.cache_lock:
                self.logos[key] = hit
        return {"found_picture": hit[1], "logo_checked": hit[0]}

    def check_all(self, force=False):
        self.last_full_check = now_ts()
        for e in map(self.merged, self.store.all()):
            if e.get("url") and not e.get("latest_manual"):
                self.enqueue(e["id"], force)

    def scheduler(self):
        while True:
            interval = max(1.0, float(self.opts["check_interval_hours"])) * 3600
            if now_ts() - self.last_full_check >= interval:
                self.check_all()
            self.publish_all()  # picks up machines that stopped reporting
            time.sleep(60)

    # ---- Changes from the web page ----

    def add_or_edit(self, data, eid=None):
        if eid and (self.store.get(eid) or {}).get("source") == "mqtt":
            return self.edit_reported(eid, data)
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("Name is required")
        url = str(data.get("url") or "").strip()
        if url:
            parse_project(url)  # raises ValueError with a readable message
        latest = "" if url else str(data.get("latest") or "").strip()
        if not url and not latest:
            raise ValueError("Add a project link, or type the latest version by hand")
        picture = str(data.get("picture") or "").strip()
        if picture and not picture.startswith(("http://", "https://")):
            raise ValueError("Picture must be an http(s) link")
        notes = str(data.get("notes") or "").strip()[:1000]
        fields = {"name": name, "current": str(data.get("current") or "").strip(), "url": url,
                  "latest_manual": latest, "picture": picture}
        if eid:
            old = self.store.get(eid)
            if not old or old.get("source") != "project":
                raise KeyError(eid)
            if old.get("url") != url:
                fields.update(found_version="", found_released="", found_notes_url="", error="", checked=0,
                              found_picture="", logo_checked=0)
            e = self.store.update(eid, **fields)
        else:
            e = dict(fields, id="p_" + uuid.uuid4().hex[:8], source="project", created=now_ts())
            self.store.put(e)
        self.settings.set_override(e["id"], notes=notes)
        self.publish_entry(e)
        if url:
            self.enqueue(e["id"])
        return e

    def edit_reported(self, eid, data):
        """Picture, link and notes for an entry a machine reports. They're kept
        apart from what the machine sends, so the next report doesn't undo them."""
        picture = str(data.get("picture") or "").strip()
        if picture and not picture.startswith(("http://", "https://")):
            raise ValueError("Picture must be an http(s) link")
        url = str(data.get("url") or "").strip()
        if url:
            parse_project(url)
        old = self.settings.override(eid)
        self.settings.set_override(eid, picture=picture, url=url, notes=str(data.get("notes") or "").strip()[:1000])
        if old.get("url", "") != url:
            self.store.update(eid, found_version="", found_released="", found_notes_url="", error="", checked=0,
                              found_picture="", logo_checked=0, guess_failed="")
        e = self.merged(self.store.get(eid))
        self.publish_entry(e)
        if e.get("url") and not e.get("latest_manual"):
            self.enqueue(eid, force=True)
        return e

    def mark_updated(self, eid):
        e = self.store.get(eid)
        if not e or e.get("source") != "project":
            raise KeyError(eid)
        latest = effective_latest(e)
        if not latest:
            raise ValueError("No latest version known yet")
        e = self.store.update(eid, current=latest)
        self.publish_entry(e)
        return e

    def remove(self, eid):
        e = self.store.delete(eid)
        if not e:
            raise KeyError(eid)
        self.unpublish(eid)
        if e.get("source") == "mqtt" and e.get("topic"):
            self.pub(BASE + "/report/" + e["topic"], "")  # clear the retained report too
        else:
            self.settings.forget(eid)  # a machine item keeps its settings, as it may come back
        return e

    def set_ignored(self, eid, ignored):
        e = self.store.get(eid)
        if not e:
            raise KeyError(eid)
        self.settings.set_ignored(eid, ignored)
        if ignored:
            self.unpublish(eid)  # gone from Home Assistant too
        else:
            self.publish_entry(e)
        return e

    def set_pinned(self, eid, pinned):
        """Pin an entry at its installed version, so a newer one isn't offered in Home Assistant."""
        e = self.store.get(eid)
        if not e:
            raise KeyError(eid)
        if pinned and not e.get("current"):
            raise ValueError("Nothing to pin: no installed version is known")
        self.settings.set_override(eid, pinned=e["current"] if pinned else "")
        self.publish_entry(e)
        return e

    # ---- Machine setup ----

    def agent_conf(self):
        """The settings file for vt-agent, with the saved broker login filled in."""
        with open(AGENT_CONF_FILE) as f:
            text = f.read()
        a = self.settings.agent()
        if a["broker"]:
            for key, value in (("BROKER", a["broker"]), ("PORT", a["port"]),
                               ("MQTT_USER", a["username"]), ("MQTT_PASS", a["password"])):
                line = "%s=%s" % (key, shlex.quote(str(value)))
                text = re.sub(r"(?m)^%s=.*$" % key, lambda m: line, text, count=1)
        return text

    def installer(self):
        """vt-install.sh with the broker login, the agent and its settings file built in."""
        a = self.settings.agent()
        with open(INSTALL_FILE) as f:
            text = f.read()
        with open(AGENT_FILE) as f:
            agent = f.read().rstrip("\n")
        login = "BROKER=%s; PORT=%s; MQTT_USER=%s; MQTT_PASS=%s" % tuple(
            shlex.quote(str(v)) for v in (a["broker"], a["port"], a["username"], a["password"]))
        for marker, value in (("@LOGIN@", login), ("@AGENT@", agent), ("@CONF@", self.agent_conf().rstrip("\n"))):
            text = re.sub(r"(?m)^.*%s.*$" % marker, lambda m: value, text, count=1)
        return text

    def view(self, e):
        o = self.settings.override(e["id"])
        e = self.merged(e)
        ev = evaluate(e, self.opts)
        return {"id": e["id"], "source": e.get("source"), "source_label": ev["source_label"],
                "name": e.get("name"), "machine": e.get("machine") or "", "kind": e.get("kind") or "",
                "current": e.get("current") or "", "latest": ev["latest"], "status": ev["status"],
                "pending": int(e.get("pending") or 0), "security": int(e.get("security") or 0),
                "packages": e.get("packages") or [], "url": e.get("url") or "",
                "project_web": self._web(e.get("url")), "notes_url": e.get("found_notes_url") or e.get("release_url") or "",
                "released": e.get("found_released") or "", "checked": iso(e.get("checked")),
                "last_seen": iso(e.get("last_seen")), "error": e.get("error") or "",
                "picture": picture_for(e), "fallback_picture": generic_picture(e),
                "picture_manual": o.get("picture", "") if e.get("source") == "mqtt" else e.get("picture") or "",
                "url_manual": o.get("url", ""), "url_guessed": bool(e.get("url_guessed")), "notes": e.get("notes") or "",
                "image": e.get("image") or "", "package": e.get("package") or "", "image_created": e.get("image_created") or "", "by_date": bool(ev["by_date"]),
                "is_system": is_system(e), "agent_version": e.get("agent_version") or "",
                "ignored": self.settings.is_ignored(e["id"]), "pinned": bool(ev["pinned"]),
                "latest_manual": e.get("latest_manual") if e.get("source") == "project" else "",
                "summary": build_summary(e, ev, self.opts)}

    @staticmethod
    def _web(url):
        try:
            return parse_project(url)["web"] if url else ""
        except ValueError:
            return ""

    def info(self):
        return {"version": VERSION, "mqtt_connected": self.connected, "broker": self.broker,
                "report_topic": BASE + "/report/<machine>/<item>", "stale_hours": self.opts["stale_hours"],
                "check_interval_hours": self.opts["check_interval_hours"],
                "github_token": bool(self.opts.get("github_token")), "agent_version": AGENT_VERSION,
                "last_full_check": iso(self.last_full_check)}

    def machine_setup(self):
        return {"agent": self.settings.agent(), "install_port": int(INSTALL_HOST_PORT) if INSTALL_HOST_PORT else None,
                "install_token": self.settings.token()}


# --------------------------------------------------------------------------
# Web page and JSON API (served through Home Assistant ingress)
# --------------------------------------------------------------------------

class BaseHandler(BaseHTTPRequestHandler):
    server_version = "VersionTracker/" + VERSION

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if not ctype.startswith("image") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def route(self):
        return [p for p in urllib.parse.urlsplit(self.path).path.split("/") if p]


def make_handler(tracker):
    class Handler(BaseHandler):
        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                data = json.loads(self.rfile.read(n))
            except ValueError:
                raise ValueError("Request body is not JSON")
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object")
            return data

        def handle_api(self, method):
            parts = self.route()
            try:
                if parts in ([], ["index.html"]) and method == "GET":
                    with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                        return self.send(200, f.read(), "text/html")
                if parts[:1] != ["api"]:
                    return self.send(404, {"error": "not found"})
                p = parts[1:]
                if p == ["entries"] and method == "GET":
                    return self.send(200, {"entries": [tracker.view(e) for e in tracker.store.all()],
                                           "info": tracker.info()})
                if p == ["entries"] and method == "POST":
                    return self.send(201, tracker.view(tracker.add_or_edit(self.body())))
                if len(p) == 2 and p[0] == "entries" and method == "PUT":
                    return self.send(200, tracker.view(tracker.add_or_edit(self.body(), p[1])))
                if len(p) == 2 and p[0] == "entries" and method == "DELETE":
                    tracker.remove(p[1])
                    return self.send(200, {"ok": True})
                if len(p) == 3 and p[0] == "entries" and p[2] == "mark-updated" and method == "POST":
                    return self.send(200, tracker.view(tracker.mark_updated(p[1])))
                if len(p) == 3 and p[0] == "entries" and p[2] == "ignore" and method == "POST":
                    return self.send(200, tracker.view(tracker.set_ignored(p[1], bool(self.body().get("ignored", True)))))
                if len(p) == 3 and p[0] == "entries" and p[2] == "pin" and method == "POST":
                    return self.send(200, tracker.view(tracker.set_pinned(p[1], bool(self.body().get("pinned", True)))))
                if len(p) == 3 and p[0] == "entries" and p[2] == "check" and method == "POST":
                    if not tracker.store.get(p[1]):
                        raise KeyError(p[1])
                    tracker.enqueue(p[1], force=True)
                    return self.send(202, {"ok": True})
                if p == ["check"] and method == "POST":
                    tracker.check_all(force=True)
                    return self.send(202, {"ok": True})
                if p == ["agent"] and method == "GET":
                    with open(AGENT_FILE, "rb") as f:
                        return self.send(200, f.read(), "text/x-shellscript",
                                         {"Content-Disposition": 'attachment; filename="vt-agent"'})
                if p == ["agent-conf"] and method == "GET":
                    return self.send(200, tracker.agent_conf(), "text/plain",
                                     {"Content-Disposition": 'attachment; filename="version-tracker.conf"'})
                if p == ["install.sh"] and method == "GET":
                    return self.send(200, tracker.installer(), "text/x-shellscript",
                                     {"Content-Disposition": 'attachment; filename="vt-install.sh"'})
                if p == ["machine-setup"] and method == "GET":
                    return self.send(200, tracker.machine_setup())
                if p == ["machine-setup"] and method == "PUT":
                    tracker.settings.set_agent(self.body())
                    return self.send(200, tracker.machine_setup())
                if p == ["machine-setup", "new-link"] and method == "POST":
                    tracker.settings.new_token()
                    return self.send(200, tracker.machine_setup())
                return self.send(404, {"error": "not found"})
            except KeyError:
                return self.send(404, {"error": "That entry no longer exists"})
            except ValueError as err:
                return self.send(400, {"error": str(err)})
            except Exception:
                log.exception("API error")
                return self.send(500, {"error": "Internal error, see the add-on log"})

        def do_GET(self):
            self.handle_api("GET")

        def do_POST(self):
            self.handle_api("POST")

        def do_PUT(self):
            self.handle_api("PUT")

        def do_DELETE(self):
            self.handle_api("DELETE")

    return Handler


def make_install_handler(tracker):
    """The only thing on the optional LAN port: GET /install/<secret>, for the
    one-line installer. The GUI and API stay behind Home Assistant ingress."""
    class InstallHandler(BaseHandler):
        def do_GET(self):
            p = self.route()
            ok = (len(p) == 2 and p[0] == "install" and tracker.settings.agent()["broker"] and
                  hmac.compare_digest(p[1].encode(), tracker.settings.token().encode()))
            if not ok:
                return self.send(404, "Not found. Copy the current command from the Version Tracker page.\n", "text/plain")
            log.info("Machine installer downloaded by %s", self.client_address[0])
            self.send(200, tracker.installer(), "text/x-shellscript")

    return InstallHandler


def main():
    opts = load_options()
    tracker = Tracker(opts)
    log.info("Version Tracker %s starting with %d entries", VERSION, len(tracker.store.entries))
    tracker.start_mqtt()
    threading.Thread(target=tracker.worker, daemon=True).start()
    threading.Thread(target=tracker.scheduler, daemon=True).start()
    if INSTALL_HOST_PORT:
        installer = ThreadingHTTPServer(("0.0.0.0", INSTALL_PORT), make_install_handler(tracker))
        threading.Thread(target=installer.serve_forever, daemon=True).start()
        log.info("Machine installer available on port %s", INSTALL_HOST_PORT)
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), make_handler(tracker))
    log.info("Web page listening on port %d", HTTP_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
