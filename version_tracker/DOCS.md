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
| Picture link | Optional icon for Home Assistant |

After updating something, press **✓ Mark as updated** on its row. That sets
the current version to the latest one and clears it from Settings → Updates.

## Adding a machine

On the Version Tracker page, **Add a machine** shows these steps with the
script ready to copy:

1. `apt install -y mosquitto-clients`
2. Save the script as `/usr/local/bin/vt-agent` and `chmod +x` it.
3. Create `/etc/version-tracker.conf` with your broker IP, username and
   password, then `chmod 600` it.
4. Run `vt-agent` once to check it works (`vt-agent --dry-run` shows what it
   would send without sending it).
5. Run it every hour:
   `echo '17 * * * * root /usr/local/bin/vt-agent >/dev/null 2>&1' > /etc/cron.d/version-tracker`

The script refreshes the package lists itself, at most every `REFRESH_HOURS`
(6 by default).

### What a machine reports

- **System**: the Proxmox VE version (when `pve-manager` is installed) or the
  OS version, plus how many apt packages are waiting and which ones. The
  entity shows as outdated when any packages are waiting, e.g.
  `9.1.1 → 9.1.2 (+12 packages)`.
- **Docker containers**: each running container whose image carries an
  `org.opencontainers.image.source` label pointing to GitHub or GitLab, with a
  version label or a version tag. Most linuxserver.io and GHCR images do. For
  any other container, add labels to it:

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

### Machines that stop reporting

If a machine hasn't reported for `stale_hours` (24 by default), it shows as
**Not reporting**, both on the page and in Settings → Updates. That way a
machine that's off, or whose cron job broke, doesn't quietly drop off the
list. Removing a machine on the page clears it, but it comes back the next
time it reports. To retire it for good, delete its cron file first.

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
