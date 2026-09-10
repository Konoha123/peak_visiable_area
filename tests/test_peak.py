"""附近制高点搜索核心算法测试（合成格网）。"""

import math

import numpy as np
import pytest

from app.geo.dem import DEMError, ElevationGrid
from app.geo.peak import search_nearby_peak

LAT = 45.0


def make_grid(n: int, cell_m: float, base: float = 0.0) -> ElevationGrid:
    dlon = cell_m / (111_320.0 * math.cos(math.radians(LAT)))
    dlat = cell_m / 111_320.0
    elev = np.full((n, n), base, dtype=np.float32)
    return ElevationGrid(elev, lon_min=-dlon * n / 2, lat_max=LAT + dlat * n / 2,
                         dlon=dlon, dlat=dlat, source="test")


def cell_lon_lat(grid: ElevationGrid, row: int, col: int) -> tuple[float, float]:
    return (grid.lon_min + (col + 0.5) * grid.dlon,
            grid.lat_max - (row + 0.5) * grid.dlat)


class TestSearchNearbyPeak:
    def test_finds_offcenter_max(self) -> None:
        grid = make_grid(201, 100.0)
        grid.elevations[60, 128] = 1234.5
        lon, lat = 0.0, LAT  # 格网几何中心
        cand = search_nearby_peak(grid, lon, lat, 5000.0)
        exp_lon, exp_lat = cell_lon_lat(grid, 60, 128)
        assert cand.lon == pytest.approx(exp_lon, abs=1e-12)
        assert cand.lat == pytest.approx(exp_lat, abs=1e-12)
        assert cand.elevation_m == pytest.approx(1234.5)
        assert cand.is_center_highest is False
        assert cand.distance_m == pytest.approx(math.hypot(4000.0, 2800.0), rel=0.01)
        assert cand.coverage_ratio == pytest.approx(1.0, abs=0.02)

    def test_tie_break_nearest_to_center(self) -> None:
        grid = make_grid(201, 100.0)
        grid.elevations[100, 90] = 800.0   # 距中心 10 格（西）
        grid.elevations[100, 115] = 800.0  # 距中心 15 格（东）
        lon, lat = cell_lon_lat(grid, 100, 100)
        cand = search_nearby_peak(grid, lon, lat, 3000.0)
        exp_lon, exp_lat = cell_lon_lat(grid, 100, 90)
        assert cand.lon == pytest.approx(exp_lon, abs=1e-12)
        assert cand.lat == pytest.approx(exp_lat, abs=1e-12)
        assert cand.distance_m == pytest.approx(1000.0, rel=0.01)

    def test_center_is_highest_returns_center(self) -> None:
        grid = make_grid(101, 100.0)
        grid.elevations[50, 50] = 900.0
        grid.elevations[20, 80] = 700.0
        lon, lat = cell_lon_lat(grid, 50, 50)
        cand = search_nearby_peak(grid, lon, lat, 2000.0)
        assert cand.is_center_highest is True
        assert cand.lon == lon and cand.lat == lat
        assert cand.elevation_m == pytest.approx(900.0)
        assert cand.distance_m == 0.0

    def test_peak_outside_circle_ignored(self) -> None:
        grid = make_grid(201, 100.0)
        grid.elevations[100, 130] = 9999.0  # 距中心 30 格 = 3 km，在半径外
        grid.elevations[100, 110] = 500.0   # 距中心 10 格 = 1 km，在半径内
        cand = search_nearby_peak(grid, 0.0, LAT, 2000.0)
        assert cand.elevation_m == pytest.approx(500.0)

    def test_all_invalid_raises(self) -> None:
        grid = make_grid(51, 100.0)
        grid.elevations[:] = np.nan
        with pytest.raises(DEMError):
            search_nearby_peak(grid, 0.0, LAT, 1000.0)

    def test_nodata_mask_excluded(self) -> None:
        grid = make_grid(101, 100.0)
        grid.elevations[50, 60] = 9999.0
        grid.elevations[30, 40] = 300.0
        grid.nodata_mask = np.zeros(grid.shape, dtype=bool)
        grid.nodata_mask[50, 60] = True  # 最高点被标记无效 → 不参与
        cand = search_nearby_peak(grid, 0.0, LAT, 3000.0)
        assert cand.elevation_m == pytest.approx(300.0)

    def test_partial_coverage_ratio(self) -> None:
        grid = make_grid(101, 100.0)
        grid.elevations[:, 51:] = np.nan  # 圆的右半无效
        grid.elevations[40, 40] = 600.0  # 距中心约 1.4 km，在圆内
        cand = search_nearby_peak(grid, 0.0, LAT, 2000.0)
        assert cand.elevation_m == pytest.approx(600.0)
        assert 0.3 < cand.coverage_ratio < 0.7

    def test_clamped_window_counts_as_uncovered(self) -> None:
        # 离线源窗口被裁剪：格网仅覆盖请求圆的左半 → 覆盖率约一半
        grid = make_grid(101, 100.0)
        grid.elevations[50, 35] = 400.0  # 距中心 1.5 km，在圆内
        half = ElevationGrid(grid.elevations[:, :51], lon_min=grid.lon_min,
                             lat_max=grid.lat_max, dlon=grid.dlon, dlat=grid.dlat,
                             source="clipped")
        cand = search_nearby_peak(half, 0.0, LAT, 2000.0)
        assert cand.elevation_m == pytest.approx(400.0)
        assert 0.3 < cand.coverage_ratio < 0.7
