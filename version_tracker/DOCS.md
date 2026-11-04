# Version Tracker

Version Tracker keeps one list of everything that might need updating. Each
item becomes an **update entity** in Home Assistant, so whatever is behind
shows up under **Settings → Updates**, next to your Home Assistant updates.
It's only a reminder: nothing gets installed for you.

Two kinds of items can go on the list:

- **Projects** you add on the Version Tracker page: anything released on
  GitHub, GitLab or a self-hosted GitLab. You type the version you run, and
  the add-on looks up the latest release. You can also track something by
  hand, like router firmware, by typing both versions.
- **Machines** that report themselves with the small `vt-agent` script:
  Debian, Ubuntu and Proxmox VE hosts, their pending apt packages, and
  optionally their Docker containers and other apps. Nothing needs to be added
  in Home Assistant or `configuration.yaml`; a machine appears the first time
  it reports.

## Requirements

- An MQTT broker. The **Mosquitto broker** add-on works without any setup.
  For another broker, fill in the `mqtt_*` options.
- The **MQTT integration** in Home Assistant, with discovery left on (the
  default).

## Adding a project

Open **Version Tracker** in the sidebar and choose **Add project**:

| Field | What to enter |
|---|---|
| Name | How it should appear in Home Assistant |
| Current version | The version you run now |
| Project link | e.g. `https://github.com/owner/repo`, `https://gitlab.com/group/project` or a self-hosted GitLab link. `owner/repo` works for GitHub. |
| Latest version | Only for items without a link |
| Picture link | Optional. Leave blank to use the project's logo (see [Pictures](#pictures)). |

After updating something, press **✓ Mark as updated** on its row. That sets
the current version to the latest one and clears it from Settings → Updates.

## Adding a machine

On the Version Tracker page, choose **Add a machine**.

1. **MQTT login for your machines.** Enter the broker address (usually Home
   Assistant's IP), port, and a username and password, and press **Save
   login**. With the Mosquitto broker app, any Home Assistant user works, so
   you can create one just for this (e.g. `vt-agent`) under **Settings →
   People**. The login is stored in the app and built into the install script.
2. **Install on the machine.** Press **Copy install script**, open a root
   shell on the machine (on Proxmox, the node's **Shell**), paste it and press
   Enter. The script:
   - asks what to call the machine in Home Assistant (default: its hostname)
     and whether to report every hour,
   - installs `mosquitto-clients` if it's missing,
   - writes `/etc/version-tracker.conf` (`chmod 600`) and
     `/usr/local/bin/vt-agent`,
   - adds `/etc/cron.d/version-tracker` if you said yes, and
   - sends the first report, so the machine shows up straight away.

   Running it again on the same machine updates the agent and the login, and
   keeps your other settings (`APPS`, `PICTURE`, ...); the previous file is
   saved as `/etc/version-tracker.conf.bak`. To skip the questions, run it as
   `VT_NAME=pve1 VT_SCHEDULE=yes bash vt-install.sh`.

### One-line install

To get a `curl ... | bash` command instead of a pasted script, open the
app's **Configuration** tab, set a port for **Machine installer** under
**Network** (e.g. 8098), save and restart the app. **Add a machine** then
shows a command like:

```bash
curl -fsSL http://192.168.1.10:8098/install/<secret> | bash
```

That port only serves the install script, and only at that secret link.
Because the script contains the MQTT login, treat the link like a password;
**New link** makes a new one and stops the old one working. Leave the port
empty to turn it off.

### By hand

**Set it up by hand instead** (at the bottom of the dialog) shows the agent,
a filled-in settings file and the cron line to copy separately. You can also
run `vt-agent --dry-run` on a machine at any time to see what it would send.

The script refreshes the package lists itself, at most every `REFRESH_HOURS`
(6 by default).

### What a machine reports

- **System**: the Proxmox VE version (when `pve-manager` is installed) or the
  OS version, plus how many apt packages are waiting and which ones. The
  entity shows as outdated when any packages are waiting, e.g.
  `9.1.1 → 9.1.2 (+12 packages)`.
- **Docker containers**: every running container, with its version, image
  and when the image was built. Where releases come from, in this order:
  1. a project link you set on the page (✎ on its row),
  2. the image's `org.opencontainers.image.source` label (most linuxserver.io
     and many GHCR images have one), or your own `version_tracker.url` label,
  3. a guess from the image name: `ghcr.io/owner/app` and Docker Hub
     `owner/app` → `github.com/owner/app`, `redis` → `github.com/redis/redis`,
     `lscr.io/linuxserver/app` → `github.com/linuxserver/docker-app`. A guess
     is marked "guessed from the image", and is dropped if that repo doesn't
     exist or has no releases.

  Without any of these it shows **No project link** until you add one. The
  version is the image's version label or its tag. When the tag isn't a
  version (`latest`, `lts`, `pg18`), or the project names its releases by
  date, the image's build date is compared with the latest release instead:
  built after the release counts as up to date.

  Labels you can add to a container:

  ```yaml
  labels:
    version_tracker.url: https://github.com/owner/repo   # where releases come from
    version_tracker.name: Nice name                      # optional
    version_tracker.ignore: "true"                       # don't report this one
  ```

  When a container is removed, the next report removes its entity too.
- **Apps** listed in the settings file, as `"Name|installed version|project link"`.
  Use a command to fill in the version so it stays current, for example:

  ```bash
  APPS=(
    "Jellyfin|$(jellyfin --version 2>/dev/null | awk '{print $NF}')|https://github.com/jellyfin/jellyfin"
  )
  ```

### Updating the agent

When a new version of the app changes the agent, machines running the old
one show **Agent out of date** on the page. Run the install script (or
one-line command) on them again; it keeps the name and settings.

### Machines that stop reporting

If a machine hasn't reported for `stale_hours` (24 by default), it shows as
**Not reporting**, both on the page and in Settings → Updates. That way a
machine that's off, or whose cron job broke, doesn't quietly drop off the
list. Removing a machine on the page clears it, but it comes back the next
time it reports. To retire it for good, delete its cron file first.

## Ignoring items

Some things don't need tracking, such as the database and cache containers
that come with an app (an Immich stack's `immich_redis` and
`immich_postgres`, for example). Press the crossed-out eye on a row to ignore
it. An ignored item disappears from the list and from Settings → Updates, and
stays ignored when its machine reports it again. The **Ignored** tab lists
them; press the eye there to track one again.

To stop a container being reported at all, give it the
`version_tracker.ignore: "true"` label instead.

## Pinning a version

To stay on the version you have, even though a newer one is out, press the
pin on its row. While it stays on that version:

- its status is **Pinned** (yellow) instead of "Update available", and it
  leaves the **Needs attention** tab;
- Home Assistant shows it as up to date, so it isn't offered under
  Settings → Updates. The entity's `pinned_version` and `available_version`
  attributes say what's going on;
- the page still shows the latest version, its release date and the release
  notes link.

Unlike ignoring, the item stays in the list and keeps being checked. The pin
is kept when the machine reports again, and lifts by itself once the item is
on a different version (you updated it, or marked a project as updated).
Press the pin again to unpin.

## Pictures and notes

Press ✎ on any row to set a picture link or write notes, e.g. "HP EliteDesk
G3 in the closet" for a Proxmox host. On things a machine reports you can
also set the project link. These are kept when the machine reports again,
and the notes also appear as a `notes` attribute in Home Assistant.

Every item gets a picture, both on the page and in Home Assistant:

1. a picture link you set (with ✎, or `PICTURE` in a machine's settings),
2. otherwise the project's logo: the first picture near the top of its README
   that isn't a badge (one named after the project, or called logo or icon,
   is preferred), or else the GitHub organisation's or GitLab project's
   avatar. Logos are looked up once a week.
3. otherwise a generic picture: Docker, Proxmox, Debian, Ubuntu or Linux.

## Options

| Option | Default | Meaning |
|---|---|---|
| `check_interval_hours` | 6 | How often to look up the latest releases |
| `stale_hours` | 24 | When to flag a machine as not reporting |
| `github_token` | | Optional read-only token. Without one, GitHub allows 60 checks an hour. |
| `gitlab_tokens` | | Optional, for private GitLab projects: one `host` + `token` (read_api) per server. Each token is only sent to its own server. |
| `discovery_prefix` | homeassistant | MQTT discovery prefix |
| `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` | | Only for a broker other than the Mosquitto add-on |

## MQTT topics

Machines publish retained JSON to `version_tracker/report/<machine>/<item>`.
You can publish your own reports there from any device. Only
`installed_version` is required; the other fields are optional:

```json
{
  "machine": "NAS",
  "name": "NAS",
  "installed_version": "7.2.1",
  "latest_version": "7.2.2",
  "pending": 0,
  "project_url": "https://github.com/owner/repo",
  "release_url": "https://example.com/release-notes",
  "picture": "https://example.com/icon.png"
}
```

An empty retained message on that topic removes the item.

## Moving from the configuration.yaml setup

Once a machine reports through `vt-agent`, remove its old `mqtt: update:`
entry from `configuration.yaml`, along with its old update-check cron job.
Otherwise it shows up twice.
