"""海洋场景一致性：可视域叠加与点选测高在"海面语义"下应当一致。

场景构造（对应阿空加瓜案例的几何本质）：陆上高峰 → 浅海陆架（-150 m）→
深海海沟（-5000 m）。原始 DEM 下海沟格点被前方陆架遮挡（可视域判不可见、
测高需抬升）；海平面钳制后（海洋格点 → 0 m）二者均判可见/抬升 0。
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.geo.dem import ElevationGrid
from app.geo.lift import solve_min_lift
from app.geo.seamask import apply_sea_level_clamp
from app.geo.viewshed import compute_viewshed
from app.main import create_app

LAT = 45.0
N, CELL = 201, 1000.0
DLON = CELL / (111_320.0 * math.cos(math.radians(LAT)))
DLAT = CELL / 111_320.0
PEAK_COL, OCEAN_COL, ROW = 50, 150, 100
LON_MIN, LAT_MAX = 14.0, 45.0 + DLAT * N / 2
PEAK_LON = LON_MIN + (PEAK_COL + 0.5) * DLON
OCEAN_LON = LON_MIN + (OCEAN_COL + 0.5) * DLON
MID_LAT = LAT_MAX - (ROW + 0.5) * DLAT

LAND_FEATURE = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[13.5, 44.0], [15.0, 44.0], [15.0, 46.0], [13.5, 46.0], [13.5, 44.0]]],
        },
    }],
}


class StaticProvider:
    name = "static-ocean"

    def __init__(self, grid: ElevationGrid) -> None:
        self.grid = grid

    def fetch_bbox(self, *args, **kwargs) -> ElevationGrid:
        return self.grid


@pytest.fixture()
def land_file(tmp_path) -> Path:
    path = tmp_path / "land.geojson"
    path.write_text(json.dumps(LAND_FEATURE), encoding="utf-8")
    return path


@pytest.fixture()
def scene() -> ElevationGrid:
    elev = np.zeros((N, N), dtype=np.float32)
    land_edge = 15.0
    shelf_edge = 15.7
    for col in range(N):
        lon = LON_MIN + (col + 0.5) * DLON
        if lon >= shelf_edge:
            elev[:, col] = -5000.0   # 深海海沟
        elif lon >= land_edge:
            elev[:, col] = -150.0    # 浅海陆架
    elev[ROW, PEAK_COL] = 4000.0     # 陆上高峰
    return ElevationGrid(elev, lon_min=LON_MIN, lat_max=LAT_MAX,
                         dlon=DLON, dlat=DLAT, source="static-ocean")


class TestEngineConsistency:
    def test_raw_bathymetry_blocks_trench_cell(self, scene) -> None:
        res = compute_viewshed(scene, PEAK_LON, MID_LAT, "geometric", 120_000.0)
        assert not res.visibility[ROW, OCEAN_COL]

    def test_clamped_ocean_visible(self, scene, land_file) -> None:
        clamped = apply_sea_level_clamp(scene, land_file)
        assert clamped.elevations[ROW, OCEAN_COL] == pytest.approx(0.0)
        res = compute_viewshed(clamped, PEAK_LON, MID_LAT, "geometric", 120_000.0)
        assert res.visibility[ROW, OCEAN_COL]

    def test_lift_consistency(self, scene, land_file) -> None:
        raw_lift = solve_min_lift(scene, PEAK_LON, MID_LAT, OCEAN_LON, MID_LAT, "geometric")
        assert raw_lift.feasible and (raw_lift.lift_m or 0) > 1000.0

        clamped = apply_sea_level_clamp(scene, land_file)
        lift = solve_min_lift(clamped, PEAK_LON, MID_LAT, OCEAN_LON, MID_LAT, "geometric")
        assert lift.feasible and lift.lift_m == pytest.approx(0.0, abs=1.0)


class TestHTTPWiring:
    def _client(self, monkeypatch, land_path) -> TestClient:
        import app.api.routes as routes
        import app.geo.seamask as seamask

        monkeypatch.setattr(routes, "build_provider",
                            lambda config: StaticProvider(_fresh_scene()))
        monkeypatch.setattr(seamask, "ensure_land_geojson", lambda: land_path)
        return TestClient(create_app())

    def test_lift_with_mask(self, monkeypatch, land_file) -> None:
        client = self._client(monkeypatch, land_file)
        body = client.post("/api/lift", json={
            "obs_lon": PEAK_LON, "obs_lat": MID_LAT,
            "click_lon": OCEAN_LON, "click_lat": MID_LAT,
        }).json()
        assert body["feasible"] is True
        assert body["lift_m"] == pytest.approx(0.0, abs=1.0)
        assert body["clicked_elev_m"] == pytest.approx(0.0, abs=1.0)

    def test_lift_without_mask_keeps_bathymetry(self, monkeypatch) -> None:
        client = self._client(monkeypatch, None)
        body = client.post("/api/lift", json={
            "obs_lon": PEAK_LON, "obs_lat": MID_LAT,
            "click_lon": OCEAN_LON, "click_lat": MID_LAT,
        }).json()
        assert body["feasible"] is True
        assert (body["lift_m"] or 0) > 1000.0

    def test_viewshed_with_mask(self, monkeypatch, land_file) -> None:
        client = self._client(monkeypatch, land_file)
        resp = client.post("/api/viewshed", json={
            "lon": PEAK_LON, "lat": MID_LAT, "radius_m": 120_000.0,
        })
        assert resp.status_code == 200
        assert resp.json()["visible_cells"] > 0


def _fresh_scene() -> ElevationGrid:
    elev = np.zeros((N, N), dtype=np.float32)
    for col in range(N):
        lon = LON_MIN + (col + 0.5) * DLON
        if lon >= 15.7:
            elev[:, col] = -5000.0
        elif lon >= 15.0:
            elev[:, col] = -150.0
    elev[ROW, PEAK_COL] = 4000.0
    return ElevationGrid(elev, lon_min=LON_MIN, lat_max=LAT_MAX,
                         dlon=DLON, dlat=DLAT, source="static-ocean")
