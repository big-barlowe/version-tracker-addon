# Changelog

## 1.4.1

- Re-running the install script now adds any settings your
  `/etc/version-tracker.conf` doesn't have yet (such as `PACKAGES`) at the
  end, commented out, with a line on how to use each. Your own settings are
  left as they are.

## 1.4.0

- Report chosen apt packages as their own items with the new `PACKAGES`
  setting, e.g. `PACKAGES=("proxmox-backup-server|Proxmox Backup Server")`.
  The latest version comes from apt, so it works for anything in the
  machine's repositories. Agent 1.4.0: re-run the install script, then add
  the line to `/etc/version-tracker.conf`.

## 1.3.0

- Pin an item at its installed version with the pin on its row. While it
  stays on that version, a newer release shows as **Pinned** (yellow) instead
  of "Update available", and Home Assistant doesn't offer the update. The
  latest version and release notes still show on the page. The pin is kept
  when the machine reports again, and lifts by itself once the item is on a
  different version.

## 1.2.0

- Every running Docker container is reported now. Where an image has no
  source label, the project is guessed from the image name (e.g.
  `ghcr.io/owner/app` → `github.com/owner/app`), and you can set the link
  yourself. Re-run the install script on your machines to get the new agent;
  the page flags machines whose agent is out of date.
- Containers tagged `latest`, `lts` and the like are compared by image build
  date against the latest release, instead of showing "Check manually".
- Edit (✎) on every row: set a picture, notes, and for reported containers
  and apps a project link. They're kept when the machine reports again.
- Fixed logos linked from the repo root in a README (e.g. immich-kiosk), and
  a missing picture now falls back to the generic icon.
- Fixed hidden fields in dialogs still showing.

## 1.1.0

- **Add a machine** now stores the MQTT login your machines use, and gives a
  ready-to-paste install script that asks for the machine's name, offers to
  report every hour and sends the first report. Optionally, set a port for
  **Machine installer** under Network to get a one-line `curl` command.
- Ignore items you don't need (e.g. an app's database container): they're
  hidden from the list and from Home Assistant, and listed under **Ignored**.
- Pictures for everything: the logo from the project's README or its
  organisation, or a generic Docker / Proxmox / Debian / Ubuntu / Linux icon.

## 1.0.1

- Fix installing on newer Home Assistant versions, which no longer pass a
  base image to the build ("base name ($BUILD_FROM) should not be blank").

## 1.0.0

- First version: projects (GitHub, GitLab, self-hosted GitLab, manual),
  self-reporting machines over MQTT, Docker containers and apps through
  `vt-agent`, and update entities created through MQTT discovery.
