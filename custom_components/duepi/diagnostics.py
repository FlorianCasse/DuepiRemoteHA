"""Diagnostics support for Duepi Pellet Stove."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
from .coordinator import DISCONNECT_GRACE_PERIOD, DuepiCoordinator

# Identifying keys in the raw cloud device data; everything else is kept so
# fields the integration does not read yet stay visible.
_CLOUD_DEVICE_REDACT_KEYS = {
    "_id",
    "univocalID",
    "deviceId",
    "deviceID",
    "owner",
    "user",
    "userId",
    "email",
    "name",
    "deviceName",
    "mac",
    "macAddress",
    "serial",
    "serialNumber",
    "ip",
    "ssid",
    "token",
    "password",
}


def _safe_error_name(error: BaseException | None) -> str | None:
    """Return an error category without exposing error details."""
    return type(error).__name__ if error is not None else None


def _serialize_datetime(value: object) -> object:
    """Serialize a datetime-like diagnostic value when available."""
    return value.isoformat() if hasattr(value, "isoformat") else value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: DuepiCoordinator = entry.runtime_data
    last_successful_update = coordinator.last_successful_update_time
    elapsed = coordinator.disconnect_elapsed_seconds
    client = getattr(coordinator, "client", None)
    cloud_device = getattr(client, "last_device_json", None)

    return {
        "config": async_redact_data(
            {
                "scan_interval": entry.options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                ),
                **entry.data,
            },
            set(entry.data),
        ),
        "state": asdict(coordinator.data) if coordinator.data else None,
        "cloud_device": (
            async_redact_data(cloud_device, _CLOUD_DEVICE_REDACT_KEYS)
            if cloud_device is not None
            else None
        ),
        "last_update_success": coordinator.last_update_success,
        "last_successful_update": (
            _serialize_datetime(last_successful_update)
        ),
        "last_seen": _serialize_datetime(getattr(coordinator, "last_seen", None)),
        "last_error": _safe_error_name(
            getattr(coordinator, "last_exception", None)
        ),
        "connectivity": {
            "raw_online": coordinator.raw_online,
            "filtered_online": coordinator.filtered_online,
            "disconnect_elapsed_seconds": elapsed,
            "grace_period_seconds": DISCONNECT_GRACE_PERIOD,
        },
    }
