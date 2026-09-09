import json
from pathlib import Path

import numpy as np
import pytest

from app.geo.dem import ElevationGrid
from app.geo.seamask import apply_sea_level_clamp, compute_land_mask, ensure_land_geojson

LAND_FEATURE = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[10.0, 40.0], [15.0, 40.0], [15.0, 50.0], [10.0, 50.0], [10.0, 40.0]]],
        },
    }],
}


@pytest.fixture()
def land_file(tmp_path) -> Path:
    path = tmp_path / "land.geojson"
    path.write_text(json.dumps(LAND_FEATURE), encoding="utf-8")
    return path


def make_grid(base: float, n: int = 100) -> ElevationGrid:
    dlon, dlat = 0.14, 0.06  # 覆盖 lon 8..22, lat 44..50
    return ElevationGrid(
        elevations=np.full((n, n), base, dtype=np.float32),
        lon_min=8.0, lat_max=50.0, dlon=dlon, dlat=dlat, source="synthetic",
    )


def col_of_lon(grid: ElevationGrid, lon: float) -> int:
    return int(round((lon - grid.lon_min) / grid.dlon - 0.5))


class TestLandMask:
    def test_rasterize_shape_and_values(self, land_file) -> None:
        grid = make_grid(-500.0)
        mask = compute_land_mask(grid, land_file)
        assert mask is not None and mask.shape == grid.shape and mask.dtype == bool
        # 陆地条带 lon [10,15] 覆盖中部列；两端为海
        assert mask[:, col_of_lon(grid, 12.0)].all()
        assert not mask[:, col_of_lon(grid, 9.0)].any()
        assert not mask[:, col_of_lon(grid, 20.0)].any()

    def test_missing_file_returns_none(self, tmp_path) -> None:
        grid = make_grid(-500.0)
        assert compute_land_mask(grid, tmp_path / "nonexistent.geojson") is None


class TestSeaLevelClamp:
    def test_ocean_clamped_inland_preserved(self, land_file) -> None:
        grid = make_grid(-500.0)
        clamped = apply_sea_level_clamp(grid, land_file)
        assert clamped is not grid
        e, c = grid.elevations, clamped.elevations
        assert c[:, col_of_lon(grid, 12.0)] == pytest.approx(-500.0)  # 陆地负高程保留
        assert c[:, col_of_lon(grid, 9.0)] == pytest.approx(0.0)     # 海洋钳制为海平面
        assert c[:, col_of_lon(grid, 20.0)] == pytest.approx(0.0)
        assert e[0, 0] == pytest.approx(-500.0)  # 原格网不被修改

    def test_positive_grid_untouched_without_data(self) -> None:
        # 全非负高程：短路返回，不触发数据获取（离线安全）
        grid = make_grid(10.0)
        assert apply_sea_level_clamp(grid) is grid

    def test_mask_unavailable_keeps_original(self, tmp_path) -> None:
        grid = make_grid(-500.0)
        result = apply_sea_level_clamp(grid, tmp_path / "nonexistent.geojson")
        assert np.array_equal(result.elevations, grid.elevations)

    def test_metadata_preserved(self, land_file) -> None:
        grid = make_grid(-500.0)
        mask = np.zeros(grid.shape, dtype=bool)
        mask[0, 0] = True
        grid.nodata_mask = mask
        clamped = apply_sea_level_clamp(grid, land_file)
        assert clamped.nodata_mask is mask
        assert clamped.lon_min == grid.lon_min and clamped.dlon == grid.dlon
        assert clamped.source == grid.source


def test_env_var_override(tmp_path, monkeypatch) -> None:
    path = tmp_path / "env_land.geojson"
    path.write_text(json.dumps(LAND_FEATURE), encoding="utf-8")
    monkeypatch.setenv("PVA_LAND_GEOJSON", str(path))
    assert ensure_land_geojson() == path


def test_env_var_missing_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PVA_LAND_GEOJSON", str(tmp_path / "nope.geojson"))
    assert ensure_land_geojson() is None
