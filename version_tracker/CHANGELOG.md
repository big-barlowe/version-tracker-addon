# Changelog

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
