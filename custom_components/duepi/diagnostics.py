"""Diagnostics support for Duepi Pellet Stove."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DEVICE_ID, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN
from .coordinator import DuepiCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: DuepiCoordinator = entry.runtime_data

    return {
        "config": {
            "device_id": entry.data.get(CONF_DEVICE_ID),
            "scan_interval": entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        },
        "state": asdict(coordinator.data) if coordinator.data else None,
        "last_update_success": coordinator.last_update_success,
    }
