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

exec python3 -u /app/app.py
