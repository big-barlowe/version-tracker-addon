#!/usr/bin/with-contenv bashio
# Pass the MQTT broker details from the Mosquitto broker add-on (if installed)
# to the app. Settings in the add-on's Configuration tab take priority.

if bashio::services.available "mqtt"; then
  MQTT_HOST="$(bashio::services mqtt 'host')"
  MQTT_PORT="$(bashio::services mqtt 'port')"
  MQTT_USERNAME="$(bashio::services mqtt 'username')"
  MQTT_PASSWORD="$(bashio::services mqtt 'password')"
  MQTT_SSL="$(bashio::services mqtt 'ssl')"
  export MQTT_HOST MQTT_PORT MQTT_USERNAME MQTT_PASSWORD MQTT_SSL
  bashio::log.info "MQTT broker from Home Assistant: ${MQTT_HOST}:${MQTT_PORT}"
elif ! bashio::config.has_value 'mqtt_host'; then
  bashio::log.warning "No MQTT broker found. Install the Mosquitto broker add-on, or set mqtt_host in the Configuration tab."
fi

# The host port for the one-line machine installer, if one is set under
# Network (bashio renamed addon.* to app.* in 2026).
if declare -F bashio::app.port >/dev/null; then
  VT_INSTALL_PORT="$(bashio::app.port 8098 2>/dev/null || true)"
else
  VT_INSTALL_PORT="$(bashio::addon.port 8098 2>/dev/null || true)"
fi
[ "${VT_INSTALL_PORT}" = "null" ] && VT_INSTALL_PORT=""
export VT_INSTALL_PORT

exec python3 -u /app/app.py
