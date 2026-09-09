import math

import pytest

from app.geo.horizon import (
    EARTH_RADIUS_M,
    display_radius_m,
    effective_earth_radius_m,
    horizon_radius_m,
)


def test_geometric_horizon_radius() -> None:
    r = horizon_radius_m(8000.0, "geometric")
    assert r == pytest.approx(math.sqrt(2 * EARTH_RADIUS_M * 8000.0))
    assert r == pytest.approx(319_275.8, rel=1e-4)


def test_refraction_increases_radius() -> None:
    geo = horizon_radius_m(5000.0, "geometric")
    std = horizon_radius_m(5000.0, "standard")
    assert std == pytest.approx(geo * math.sqrt(7 / 6))
    assert effective_earth_radius_m("standard") == pytest.approx(7 / 6 * EARTH_RADIUS_M)


@pytest.mark.parametrize("elev", [0.0, -100.0])
def test_nonpositive_elevation(elev: float) -> None:
    assert horizon_radius_m(elev, "geometric") == 0.0


def test_fallback_to_min_display_radius() -> None:
    radius, fallback = display_radius_m(10.0, "geometric")
    assert radius == 5000.0 and fallback is True
    radius, fallback = display_radius_m(0.0, "geometric")
    assert radius == 5000.0 and fallback is True


def test_no_fallback_for_high_peak() -> None:
    radius, fallback = display_radius_m(5000.0, "geometric")
    assert radius == pytest.approx(252_408.6, rel=1e-4) and fallback is False


def test_invalid_refraction_model() -> None:
    with pytest.raises(ValueError, match="未知折射模型"):
        horizon_radius_m(100.0, "foo")
