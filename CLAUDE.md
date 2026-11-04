# Version Tracker — Home Assistant add-on

A Home Assistant add-on (called an "app" since HA 2026.2) that keeps one list
of things that may need updating and shows each one as an MQTT `update`
entity, so it appears under **Settings → Updates**. It is a reminder list
only: entities have no `command_topic`, and nothing is ever installed.

The owner runs Home Assistant with the Mosquitto broker add-on, and two
Proxmox VE hosts that used to report through a hand-written
`update_check.sh` plus `mqtt: update:` entries in configuration.yaml. This
add-on replaces that setup.

Repository: https://github.com/big-barlowe/version-tracker-addon (public, so
Home Assistant can add it as an app/add-on repository by URL).

## Layout

```
repository.yaml                 add-on repository manifest (for adding the GitHub repo in HA)
README.md                       GitHub landing page with install steps
version_tracker/
  config.yaml                   add-on manifest: ingress on 8099, optional port 8098, mqtt:want, options
  Dockerfile                    HA base image (BUILD_FROM, default base:3.24) + python3 + paho-mqtt 2.x
  run.sh                        bashio: Mosquitto credentials + installer host port, execs app.py
  translations/en.yaml          labels for the Configuration tab
  DOCS.md / README.md / CHANGELOG.md
  icon.png / logo.png
  app/app.py                    the whole backend (stdlib HTTP server + paho)
  app/static/index.html         the whole GUI (vanilla JS, no build step)
  app/vt-agent.sh               script that runs on each machine (bash, cron)
  app/vt-install.sh             installer template; app.py fills in @LOGIN@, @AGENT@, @CONF@
  app/version-tracker.conf.example   settings template for vt-agent
```

## How it works

- **Projects** (`source: "project"`) are added in the GUI: a name, a current
  version, and a GitHub / GitLab / self-hosted GitLab link, or a latest
  version typed by hand. A worker thread looks up the latest release (GitHub
  `releases/latest`, falling back to tags; GitLab `releases`, skipping
  `upcoming_release`, falling back to tags). Results are cached per project
  for `check_interval_hours`.
- **Machines** (`source: "mqtt"`) publish retained JSON to
  `version_tracker/report/<machine>/<item>`. Items are `system` (apt /
  Proxmox), `docker_<name>`, `app_<name>` and `pkg_<package>` (`PACKAGES`;
  latest is apt's candidate, only sent when `dpkg --compare-versions` says
  it's newer, so `evaluate()` trusts it instead of `compare_versions`). If a report has a `project_url`
  and no `latest_version`, the add-on looks up the latest release itself. An
  empty retained payload removes the item.
- **Containers:** the agent reports every running container (`image`,
  `image_created`, version label or tag). With no `project_url`,
  `guess_project()` derives one from the image name (`url_guessed`); a
  `NotFound` lookup drops the guess (`guess_failed`, retried weekly). When
  the tag isn't version-like or releases are date stamps,
  `built_after_release()` compares image build date with release date.
- **Agent version:** `AGENT_VERSION` in vt-agent.sh, sent with the `system`
  item; the GUI flags machines whose agent differs from the bundled one.
  Bump it whenever vt-agent.sh changes.
- For every entry the add-on publishes:
  - discovery: `homeassistant/update/version_tracker_<id>/config`
  - state: `version_tracker/entity/<id>/state` (JSON: installed_version,
    latest_version, title, release_summary up to 255 chars, release_url)
  - attributes: `version_tracker/entity/<id>/attributes`
  - availability: `version_tracker/status` (LWT)

  Publishing skips payloads that haven't changed. Everything is re-sent when
  `homeassistant/status` goes `online`.
- `evaluate()` in app.py decides the status and the `latest_version` that HA
  compares against. Pending apt packages make it e.g. `9.1.2 (+3 packages)`.
  A stale machine gets `<installed> (not reporting)`, so it still shows in
  Updates.
- **Pinning** stores the installed version as the `pinned` override. While
  `current` still equals it, "Update available" / "Check manually" become
  "Pinned" and HA gets `latest_version` = installed; the pin lapses on any
  version change.
- **Pictures:** `picture_for()` uses the entry's own picture, else
  `found_picture` (first non-badge image near the top of the README, else
  the GitHub org / GitLab project avatar; looked up in `check_entry`, at most
  weekly per project), else a dashboard-icons picture by kind for machines.
- **Ignored** ids and **overrides** (notes, `pinned`; for reported entries
  also picture and url) live in `/data/settings.json`, not on the entry, so they
  survive the agent clearing and re-reporting an item. `Tracker.merged()`
  lays overrides over an entry; `publish_entry`, `view`, `check_entry` and
  `check_all` all work on the merged entry. Ignored entries aren't published.
- **Machine setup:** the login machines use (broker, port, user, password)
  is saved from the GUI to `/data/settings.json` (mode 600). `agent_conf()`
  and `installer()` build the settings file and `vt-install.sh` from it.
  The GUI wraps the installer in a heredoc that writes it to a file before
  running it, so its questions read the keyboard, not the rest of the paste.
  If a host port is set for 8098/tcp under Network, a second listener serves
  only `GET /install/<secret>` for a `curl | bash` one-liner.
- GitLab tokens are stored per host and only sent to that host.
- Entries persist in `/data/entries.json`. Options come from `/data/options.json`.

## Running and testing locally

app.py honours `VT_DATA_DIR` (default `/data`), `VT_HTTP_PORT` (default 8099),
`VT_INSTALL_PORT` (host port for the installer; empty = off) and
`MQTT_HOST` / `MQTT_PORT` / `MQTT_USERNAME` / `MQTT_PASSWORD` / `MQTT_SSL`.
To run it outside Home Assistant:

```bash
mkdir -p /tmp/vtdata && echo '{}' > /tmp/vtdata/options.json
pip install "paho-mqtt>=2.1,<3"
VT_DATA_DIR=/tmp/vtdata MQTT_HOST=<broker> python3 version_tracker/app/app.py
# GUI on http://localhost:8099
```

The agent has a dry-run mode, and takes its settings and state paths from the
environment:

```bash
VT_CONF=./test.conf VT_STATE_DIR=/tmp/vtstate bash version_tracker/app/vt-agent.sh --dry-run
```

## Test status

Tested so far, in a sandbox without network access:

- **Backend:** run with a stand-in paho module that recorded publishes and
  injected messages, and with mocked GitHub/GitLab HTTP responses. Covered
  statuses, discovery and state payloads, deletion clearing retained topics,
  report removal, HA-restart republish, change-only publishing, and API
  validation.
- **Agent:** run against shims for apt-get, dpkg-query, apt-cache, docker
  and mosquitto_pub. Covered Proxmox detection, package counts, Docker label
  handling, and clearing removed containers.
- **GUI:** screenshotted in light, dark and phone widths.

Tested with Docker on an arm64 Mac (2026-11-04):

- **Image:** newer Supervisors don't pass `BUILD_FROM` (install failed with
  "base name ($BUILD_FROM) should not be blank"), so the Dockerfile defaults
  to the multi-arch `ghcr.io/home-assistant/base:3.24`. Built with no build
  args for amd64 and arm64 (1.0.1). Earlier built from `aarch64-base` and run
  with its real `/init` + `run.sh`. A mock Supervisor answered
  `/services/mqtt`, and `bashio::services mqtt` handed the credentials over.
  Note: s6 `/init` drops `docker run -e` variables for the CMD, so to run the
  image by hand without `run.sh`, use `--entrypoint python3`.
- **MQTT:** paho-mqtt 2.1 against Mosquitto 2.1 with password auth.
- **Lookups:** live GitHub (releases, and the tags fallback) and gitlab.com,
  plus the not-found error.
- **Agent:** real run in a `debian:12` container with real apt and
  `mosquitto_pub`, including clearing a removed app.
- **Home Assistant 2026.10.4** (container, MQTT integration): all entities
  appeared as `update.*` with the right on/off state; mark-updated, delete
  (entity and device removed), agent-cleared items, LWT → `unavailable`, and
  add-on restart all behaved correctly, with no warnings in either log.

1.2.0: the owner's real container images run on local Docker (redis,
pgvector, metube, portainer-ce, ddns-updater, immich postgres), reported by
the agent through the Docker socket: link guessing, failed-guess drop,
date-based status, overrides kept through re-report and clear + re-report,
notes and pictures in HA attributes, GUI screenshots.

1.4.0 (`PACKAGES`): agent in an amd64 `debian:12` container with Proxmox's
real pbs-client repo, `proxmox-backup-client` 3.4.6-1 installed and 3.4.7-1
available; not-installed package skipped, removed entry cleared, pin then
upgrade, epoch/revision-only bump sent to HA. Not yet run on a real PBS host.

1.3.0 (pinning), add-on against Mosquitto in Docker: pin/unpin through the
API on a container, a Proxmox host with pending packages and a project;
HA state and attributes; pin kept through clear + re-report and restart;
lapsed on a version change and on mark-updated; GUI screenshotted (light,
dark, 400px). Not yet seen in a real Home Assistant.

1.1.0, same setup (mock Supervisor also answering `/addons/self/info`):

- `run.sh` got the installer port through `bashio::app.port`.
- Logo lookup against real READMEs (Immich, Jellyfin, linuxserver images,
  Uptime Kuma, Traefik, AdGuard, pi-hole, Nextcloud, GitLab projects); HA
  received the pictures as `entity_picture`.
- Installer in fresh `debian:12` containers: the `curl | bash` one-liner
  with no terminal (defaults), and the paste block answering prompts in a
  real pty, over an existing settings file (APPS/PICTURE kept, `.bak`
  written). The MQTT password had a space, quotes, `$` and `@`.
- Ignoring: removed from HA, stayed hidden after re-report, after an empty
  report plus re-report, and after an add-on restart; un-ignoring restored it.
- GUI screenshotted with Playwright (light, dark, 400px).

**Not yet tested for real:**
- installing through the Supervisor on a real HA OS / Supervised system
  (ingress panel, Configuration tab, real `/services/mqtt` and
  `/addons/self/info`, the Network port setting)
- self-hosted GitLab and GitLab tokens
- vt-agent on a real Proxmox host (pve-manager detection), and Docker
  container reporting

## Conventions

- No build step and no frontend framework; keep the GUI a single HTML file.
- Backend dependencies: Python stdlib plus paho-mqtt only. Keep paho 1.x
  compatibility (see `start_mqtt` and `on_disconnect`).
- Bump `version` in config.yaml and add a CHANGELOG entry for each release;
  HA only offers an update when the version changes.
- Escape all MQTT-sourced text in the GUI (`esc()`), and only use `http(s)`
  URLs in links and images (`safeUrl()`).
