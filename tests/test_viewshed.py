import math

import numpy as np
import pytest

from app.geo.dem import DEMError, ElevationGrid
from app.geo.viewshed import ENGINE_NAME, compute_viewshed
from app.geo.viewshed_gdal import compute_viewshed_gdal

LAT = 45.0


def make_grid(n: int, cell_m: float, base: float = 0.0) -> tuple[ElevationGrid, float, float]:
    dlon = cell_m / (111_320.0 * math.cos(math.radians(LAT)))
    dlat = cell_m / 111_320.0
    lon_min = -dlon * n / 2
    lat_max = LAT + dlat * n / 2
    grid = ElevationGrid(
        elevations=np.full((n, n), base, dtype=np.float32),
        lon_min=lon_min, lat_max=lat_max, dlon=dlon, dlat=dlat, source="synthetic",
    )
    lon0 = lon_min + (n // 2 + 0.5) * dlon
    lat0 = lat_max - (n // 2 + 0.5) * dlat
    return grid, lon0, lat0


@pytest.fixture()
def flat_raised():
    """平坦地面 1000 m + 中心观测点 1500 m（离地 500 m）。"""
    grid, lon0, lat0 = make_grid(201, 1000.0, base=1000.0)
    grid.elevations[100, 100] = 1500.0
    return grid, lon0, lat0


@pytest.fixture()
def wall_scene():
    """平地、观测点离地 50 m、正东 5 km 处 200 m 高墙（行 95..105）。"""
    grid, lon0, lat0 = make_grid(201, 100.0)
    grid.elevations[100, 100] = 50.0
    grid.elevations[95:106, 105] = 200.0
    return grid, lon0, lat0


class TestAngularRaySweep:
    def test_horizon_radius_on_flat_plane(self, flat_raised) -> None:
        grid, lon0, lat0 = flat_raised
        res = compute_viewshed(grid, lon0, lat0, "geometric", 120_000.0)
        # 500 m 离地对应几何地平线约 79.8 km
        assert res.visibility[100, 160]
        assert not res.visibility[100, 200]
        assert res.visibility[30, 100]  # 正北 70 km 可见
        assert res.engine == ENGINE_NAME

    def test_wall_occlusion(self, wall_scene) -> None:
        grid, lon0, lat0 = wall_scene
        res = compute_viewshed(grid, lon0, lat0, "geometric", 9000.0)
        vis = res.visibility
        assert vis[100, 103]          # 墙前可见
        assert not vis[100, 107]      # 正后方阴影
        assert not vis[95:106, 110].any()  # 墙带正后方全部遮挡
        assert vis[80, 107]           # 斜向绕过墙体可见
        assert vis[100, 90]           # 反方向无遮挡

    def test_symmetry_without_obstacle(self) -> None:
        grid, lon0, lat0 = make_grid(201, 100.0, base=10.0)
        grid.elevations[100, 100] = 200.0
        res = compute_viewshed(grid, lon0, lat0, "geometric", 8000.0)
        vis = res.visibility
        assert vis[100, 130] == vis[100, 70]
        assert vis[70, 100] == vis[130, 100]

    def test_refraction_extends_visibility(self) -> None:
        grid, lon0, lat0 = make_grid(201, 1000.0, base=1000.0)
        grid.elevations[100, 100] = 1500.0
        geo = compute_viewshed(grid, lon0, lat0, "geometric", 120_000.0)
        std = compute_viewshed(grid, lon0, lat0, "standard", 120_000.0)
        # 折射抬升地平线（约 84.6 km vs 79.8 km），可见格点更多
        assert std.visibility.sum() > geo.visibility.sum()

    def test_observer_outside_grid(self) -> None:
        grid, _, _ = make_grid(51, 1000.0)
        with pytest.raises(DEMError, match="无数据区域"):
            compute_viewshed(grid, 50.0, 50.0, "geometric", 5000.0)

    def test_fast_on_large_grid(self) -> None:
        grid, lon0, lat0 = make_grid(501, 200.0, base=500.0)
        grid.elevations[250, 250] = 3000.0
        res = compute_viewshed(grid, lon0, lat0, "geometric", 60_000.0)
        assert res.elapsed_s < 10.0
        assert res.visibility.sum() > 0


gdal = pytest.importorskip("osgeo.gdal", reason="GDAL 不可用时跳过")


class TestGDALViewshed:
    def test_wall_occlusion(self, wall_scene) -> None:
        grid, lon0, lat0 = wall_scene
        res = compute_viewshed_gdal(grid, lon0, lat0, "geometric", 9000.0)
        vis = res.visibility
        assert vis[100, 103]
        assert not vis[100, 107]
        assert vis[80, 107]

    def test_engines_agree_on_flat_scene(self, flat_raised) -> None:
        grid, lon0, lat0 = flat_raised
        own = compute_viewshed(grid, lon0, lat0, "geometric", 120_000.0)
        gda = compute_viewshed_gdal(grid, lon0, lat0, "geometric", 120_000.0)
        agree = (own.visibility == gda.visibility).mean()
        assert agree > 0.98  # 无遮挡平坦场景两引擎应几乎一致

    def test_engines_wall_scene_loose_agreement(self, wall_scene) -> None:
        # GDAL 参考平面算法对角向阴影偏宽（算法特性），一致性阈值放宽
        grid, lon0, lat0 = wall_scene
        own = compute_viewshed(grid, lon0, lat0, "geometric", 9000.0)
        gda = compute_viewshed_gdal(grid, lon0, lat0, "geometric", 9000.0)
        agree = (own.visibility == gda.visibility).mean()
        assert agree > 0.8
