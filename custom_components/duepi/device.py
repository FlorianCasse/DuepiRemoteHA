"""Device info helper for Duepi integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def build_device_info(device_id: str) -> DeviceInfo:
    """Build a DeviceInfo for a Duepi stove."""
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name="Duepi Pellet Stove",
        manufacturer="Duepi",
        model="Remote WiFi Module",
        configuration_url="https://dpremoteiot.com/dashboard",
    )
