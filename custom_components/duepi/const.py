"""Constants for the Duepi Pellet Stove integration."""

DOMAIN = "duepi"
PLATFORMS = ["climate", "sensor", "binary_sensor", "number"]

# Config entry data keys
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_DEVICE_ID = "device_id"

# Options keys
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEFAULT_POWER = "default_power"
CONF_DEFAULT_TEMPERATURE = "default_temperature"

# Defaults
DEFAULT_SCAN_INTERVAL = 120  # seconds
DEFAULT_POWER = 5
DEFAULT_TEMPERATURE = 25
MIN_TEMPERATURE = 0
MAX_TEMPERATURE = 35
MIN_POWER = 1
MAX_POWER = 5

# API URLs
URL_BASE = "https://dpremoteiot.com"
URL_LOGIN = f"{URL_BASE}/login"
URL_DASHBOARD = f"{URL_BASE}/dashboard"
URL_SET_SETTINGS = f"{URL_BASE}/devices/setSettings"
