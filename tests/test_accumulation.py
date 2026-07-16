"""Tests for pure Duepi accumulation math."""

from __future__ import annotations

import importlib
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def accumulation_module(duepi_test_modules: SimpleNamespace) -> ModuleType:
    """Import the pure helper within the isolated Duepi package fixture."""
    return importlib.import_module("custom_components.duepi.accumulation")


@pytest.mark.parametrize(
    ("power", "expected"),
    [(1, 0.6), (2, 0.9), (3, 1.2), (4, 1.5), (5, 1.8)],
)
def test_pellet_rate_interpolates_between_configured_bounds(
    accumulation_module: ModuleType, power: int, expected: float
) -> None:
    """Power levels use a linear rate between levels one and five."""
    assert accumulation_module.pellet_rate(power, 0.6, 1.8) == pytest.approx(expected)


@pytest.mark.parametrize("power", [None, 0, 6])
def test_pellet_rate_is_zero_without_a_valid_power(
    accumulation_module: ModuleType, power: int | None
) -> None:
    """Unknown or invalid power never invents pellet consumption."""
    assert accumulation_module.pellet_rate(power, 0.6, 1.8) == 0


def test_elapsed_time_is_non_negative_and_capped(
    accumulation_module: ModuleType,
) -> None:
    """Restarts, clock anomalies, and long outages cannot over-count."""
    assert accumulation_module.capped_elapsed_seconds(None, 100, 30) == 0
    assert accumulation_module.capped_elapsed_seconds(100, 90, 30) == 0
    assert accumulation_module.capped_elapsed_seconds(100, 125, 30) == 25
    assert accumulation_module.capped_elapsed_seconds(100, 500, 30) == 60
