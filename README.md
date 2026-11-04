# Version Tracker — Home Assistant app/add-on

Shows new GitHub/GitLab releases, and machines with pending apt, Proxmox or
Docker updates, in Home Assistant's **Settings → Updates**. It's a reminder
list only; nothing is installed for you.

## Install

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fbig-barlowe%2Fversion-tracker-addon)

Or add it by hand:

1. In Home Assistant, go to **Settings → Apps → App store** (called
   **Add-ons → Add-on store** before 2026.2), open the **⋮** menu and choose
   **Repositories**.
2. Add `https://github.com/big-barlowe/version-tracker-addon`.
3. Install **Version Tracker**, start it, and open it from the sidebar.

You also need the **Mosquitto broker** app and the **MQTT** integration.
Full setup, including the `vt-agent` script for your machines, is in
[version_tracker/DOCS.md](version_tracker/DOCS.md).
