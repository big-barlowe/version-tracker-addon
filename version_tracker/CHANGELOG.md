# Changelog

## 1.0.1

- Fix installing on newer Home Assistant versions, which no longer pass a
  base image to the build ("base name ($BUILD_FROM) should not be blank").

## 1.0.0

- First version: projects (GitHub, GitLab, self-hosted GitLab, manual),
  self-reporting machines over MQTT, Docker containers and apps through
  `vt-agent`, and update entities created through MQTT discovery.
