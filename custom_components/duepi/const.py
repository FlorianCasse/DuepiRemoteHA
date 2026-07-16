"""Constants for the Duepi Pellet Stove integration."""

DOMAIN = "duepi"
PLATFORMS = ["climate", "sensor", "binary_sensor", "number"]

# Config entry data keys (CONF_EMAIL and CONF_PASSWORD come from homeassistant.const)
CONF_DEVICE_ID = "device_id"

# Options keys
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEFAULT_POWER = "default_power"
CONF_DEFAULT_TEMPERATURE = "default_temperature"
CONF_ECO_POWER = "eco_power"
CONF_ECO_TEMPERATURE = "eco_temperature"
CONF_PELLET_KG_PER_HOUR_MIN = "pellet_kg_per_hour_min"
CONF_PELLET_KG_PER_HOUR_MAX = "pellet_kg_per_hour_max"

# Defaults
DEFAULT_SCAN_INTERVAL = 30  # seconds
DEFAULT_POWER = 5
DEFAULT_TEMPERATURE = 25
DEFAULT_ECO_POWER = 1
DEFAULT_ECO_TEMPERATURE = 18
DEFAULT_PELLET_KG_PER_HOUR_MIN = 0.6
DEFAULT_PELLET_KG_PER_HOUR_MAX = 1.8
MIN_TEMPERATURE = 5
MAX_TEMPERATURE = 40
MIN_POWER = 1
MAX_POWER = 5

# Climate presets
PRESET_NONE = "none"
PRESET_ECO = "eco"
PRESET_COMFORT = "comfort"

# API URLs
URL_BASE = "https://dpremoteiot.com"
URL_LOGIN = f"{URL_BASE}/login"
URL_DASHBOARD = f"{URL_BASE}/dashboard"
URL_SET_SETTINGS = f"{URL_BASE}/devices/setSettings"
