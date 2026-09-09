import math

import numpy as np
import pytest

from app.geo.dem import DEMError, ElevationGrid
from app.geo.lift import IMPOSSIBLE_MESSAGE, LiftResult, _solve_from_profile, solve_min_lift

R = 6_371_000.0
LAT = 45.0


def make_grid(n: int, cell_m: float, base: float = 0.0) -> tuple[ElevationGrid, float, float]:
    dlon = cell_m / (111_320.0 * math.cos(math.radians(LAT)))
    dlat = cell_m / 111_320.0
    lon_min, lat_max = -dlon * n / 2, LAT + dlat * n / 2
    grid = ElevationGrid(
        elevations=np.full((n, n), base, dtype=np.float32),
        lon_min=lon_min, lat_max=lat_max, dlon=dlon, dlat=dlat, source="synthetic",
    )
    lon0 = lon_min + (n // 2 + 0.5) * dlon
    lat0 = lat_max - (n // 2 + 0.5) * dlat
    return grid, lon0, lat0


def point_at(lon0: float, lat0: float, east_m: float, north_m: float) -> tuple[float, float]:
    dlon = east_m / (111_320.0 * math.cos(math.radians(lat0)))
    dlat = north_m / 111_320.0
    return lon0 + dlon, lat0 + dlat


class TestSolveFromProfile:
    def test_flat_plane_needs_bulge_lift(self) -> None:
        # 平地等高两点因地球凸起需 D²/(2R) 量级抬升（20 km ≈ 31.3 m）
        d = np.linspace(0, 20000, 41)
        e = np.full_like(d, 100.0)
        assert _solve_from_profile(d, e, R) == pytest.approx(20000**2 / (2 * R), rel=0.03)

    def test_mid_wall_analytic(self) -> None:
        total = 20000.0
        d = np.linspace(0, total, 41)
        e = np.zeros_like(d)
        e[20] = 150.0  # 正中 10 km 处 150 m 高墙
        drop = 10000 * 10000 / (2 * R)
        expected = (150.0 + drop) * total / 10000
        assert _solve_from_profile(d, e, R) == pytest.approx(expected, rel=1e-6)

    def test_near_obstacle_amplified(self) -> None:
        total = 20000.0
        d = np.linspace(0, total, 41)
        e_far, e_near = np.zeros_like(d), np.zeros_like(d)
        e_far[20], e_near[2] = 300.0, 300.0  # 远墙 vs 近观测点墙
        assert _solve_from_profile(d, e_near, R) > _solve_from_profile(d, e_far, R)

    def test_zero_distance(self) -> None:
        d = np.array([0.0, 0.0])
        e = np.array([10.0, 10.0])
        assert _solve_from_profile(d, e, R) == 0.0


class TestSolveMinLift:
    def test_flat_grid_bulge_lift(self) -> None:
        grid, lon0, lat0 = make_grid(101, 100.0, base=500.0)
        lon1, lat1 = point_at(lon0, lat0, 5000.0, 0.0)
        res = solve_min_lift(grid, lon0, lat0, lon1, lat1, "geometric")
        assert res.feasible
        assert res.lift_m == pytest.approx(res.distance_m**2 / (2 * R), rel=0.05)

    def test_wall_grid_analytic(self) -> None:
        grid, lon0, lat0 = make_grid(401, 100.0)
        # 观测点西侧，点击点东侧，墙在正中（约 10 km）高 150 m（加宽避免采样抹平）
        obs = point_at(lon0, lat0, -10000.0, 0.0)
        click = point_at(lon0, lat0, 10000.0, 0.0)
        grid.elevations[190:211, 198:203] = 150.0
        res = solve_min_lift(grid, obs[0], obs[1], click[0], click[1], "geometric")
        assert res.feasible
        drop = 10000 * 10000 / (2 * R)
        expected = (150.0 + drop) * 20000 / 10000
        assert res.lift_m == pytest.approx(expected, rel=0.05)
        assert res.observer_elev == pytest.approx(0.0, abs=1.0)
        assert res.clicked_elev == pytest.approx(0.0, abs=1.0)

    def test_antipodal_infeasible(self) -> None:
        grid, lon0, lat0 = make_grid(101, 100.0)
        res = solve_min_lift(grid, 0.0, 0.0, 180.0, 0.0, "geometric")
        assert not res.feasible and res.lift_m is None
        assert res.central_angle_deg == pytest.approx(180.0)

    def test_quarter_earth_infeasible(self) -> None:
        grid, lon0, lat0 = make_grid(101, 100.0)
        res = solve_min_lift(grid, 0.0, 0.0, 0.0, 90.0, "geometric")
        assert not res.feasible
        assert res.central_angle_deg == pytest.approx(90.0)

    def test_path_outside_dem_raises(self) -> None:
        grid, lon0, lat0 = make_grid(101, 100.0)  # 仅 ±5 km
        click = point_at(lon0, lat0, 100000.0, 0.0)
        with pytest.raises(DEMError):
            solve_min_lift(grid, lon0, lat0, click[0], click[1], "geometric")

    def test_same_point_zero(self) -> None:
        grid, lon0, lat0 = make_grid(101, 100.0, base=10.0)
        res = solve_min_lift(grid, lon0, lat0, lon0, lat0, "geometric")
        assert res.feasible and res.lift_m == 0.0


def test_impossible_message_constant() -> None:
    assert IMPOSSIBLE_MESSAGE and isinstance(IMPOSSIBLE_MESSAGE, str)
    _ = LiftResult  # 确认导出
