import math

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.geo.dem import ElevationGrid
from app.main import create_app

LAT = 45.0
R = 6_371_000.0


class StaticProvider:
    """返回预置合成格网的假 DEM 提供者（测试注入用）。"""

    name = "static-test"

    def __init__(self, grid: ElevationGrid) -> None:
        self.grid = grid

    def fetch_bbox(self, lon_min, lat_min, lon_max, lat_max, target_cell_m) -> ElevationGrid:
        return self.grid


def make_grid(n: int, cell_m: float, base: float = 0.0) -> ElevationGrid:
    dlon = cell_m / (111_320.0 * math.cos(math.radians(LAT)))
    dlat = cell_m / 111_320.0
    elev = np.full((n, n), base, dtype=np.float32)
    return ElevationGrid(elev, lon_min=-dlon * n / 2, lat_max=LAT + dlat * n / 2,
                         dlon=dlon, dlat=dlat, source="static-test")


def center_lon_lat(grid: ElevationGrid) -> tuple[float, float]:
    nrows, ncols = grid.shape
    return (grid.lon_min + (ncols // 2 + 0.5) * grid.dlon,
            grid.lat_max - (nrows // 2 + 0.5) * grid.dlat)


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    app = create_app()
    return TestClient(app)


@pytest.fixture()
def peak_client(monkeypatch) -> TestClient:
    """中央 4000 m 山峰（离地 4000 m）的静态 DEM。"""
    grid = make_grid(201, 1000.0, base=0.0)
    grid.elevations[100, 100] = 4000.0
    provider = StaticProvider(grid)
    import app.api.routes as routes
    monkeypatch.setattr(routes, "build_provider", lambda config: provider)
    return TestClient(create_app())


class TestValidate:
    def test_ok(self, client: TestClient) -> None:
        resp = client.post("/api/validate", json={"text": "116.397, 39.909", "format": "decimal"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] and body["message"] == "校验成功"
        assert body["lon"] == pytest.approx(116.397)

    def test_invalid(self, client: TestClient) -> None:
        resp = client.post("/api/validate", json={"text": "abc", "format": "decimal"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_empty_text_invalid(self, client: TestClient) -> None:
        resp = client.post("/api/validate", json={"text": "   ", "format": "decimal"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_picked_coordinate_style_decimal(self, client: TestClient) -> None:
        # 地图点选模式的确认路径：只读框的六位小数十进制度字符串应直接校验通过
        resp = client.post("/api/validate",
                           json={"text": "138.727400, 35.360600", "format": "decimal"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["lon"] == pytest.approx(138.7274)
        assert body["lat"] == pytest.approx(35.3606)

    def test_picked_style_wrapped_longitude(self, client: TestClient) -> None:
        # 前端点选经度归一化后的跨日期变更线坐标（西经区间）
        resp = client.post("/api/validate",
                           json={"text": "-179.500000, 65.123456", "format": "decimal"})
        assert resp.json()["ok"] is True


class TestHorizon:
    def test_peak_horizon(self, peak_client: TestClient) -> None:
        lon, lat = center_lon_lat(StaticProvider(make_grid(201, 1000.0)).grid)
        resp = peak_client.post("/api/horizon", json={"lon": lon, "lat": lat})
        assert resp.status_code == 200
        body = resp.json()
        assert body["elevation_m"] == pytest.approx(4000.0, abs=1.0)
        assert body["fallback"] is False
        assert body["display_radius_m"] == pytest.approx(math.sqrt(2 * R * 4000), rel=1e-3)

    def test_low_elevation_fallback(self, monkeypatch) -> None:
        grid = make_grid(51, 100.0, base=1.0)  # 1 m 高程 → 兜底 5 km
        import app.api.routes as routes
        monkeypatch.setattr(routes, "build_provider", lambda config: StaticProvider(grid))
        c = TestClient(create_app())
        lon, lat = center_lon_lat(grid)
        body = c.post("/api/horizon", json={"lon": lon, "lat": lat}).json()
        assert body["fallback"] is True and body["display_radius_m"] == 5000.0


class TestViewshed:
    def test_own_engine(self, peak_client: TestClient) -> None:
        grid = make_grid(201, 1000.0)
        lon, lat = center_lon_lat(grid)
        resp = peak_client.post("/api/viewshed", json={
            "lon": lon, "lat": lat, "engine": "angular-ray-sweep", "radius_m": 90000.0,
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["image"].startswith("data:image/png;base64,")
        assert len(body["bounds"]) == 4
        assert body["visible_cells"] > 0
        assert body["engine"] == "angular-ray-sweep"
        assert body["elevation_m"] == pytest.approx(4000.0, abs=1.0)

    def test_gdal_engine(self, peak_client: TestClient) -> None:
        grid = make_grid(201, 1000.0)
        lon, lat = center_lon_lat(grid)
        resp = peak_client.post("/api/viewshed", json={
            "lon": lon, "lat": lat, "engine": "gdal-viewshed", "radius_m": 90000.0,
        })
        assert resp.status_code == 200
        assert resp.json()["visible_cells"] > 0

    def test_radius_validation(self, client: TestClient) -> None:
        resp = client.post("/api/viewshed", json={"lon": 0, "lat": 0, "radius_m": -1})
        assert resp.status_code == 422


class TestLift:
    def test_wall_lift(self, monkeypatch) -> None:
        grid = make_grid(401, 100.0)
        grid.elevations[190:211, 198:203] = 150.0
        import app.api.routes as routes
        monkeypatch.setattr(routes, "build_provider", lambda config: StaticProvider(grid))
        c = TestClient(create_app())
        lon, lat = center_lon_lat(grid)
        dlon = 10000.0 / (111_320.0 * math.cos(math.radians(LAT)))
        body = c.post("/api/lift", json={
            "obs_lon": lon - dlon, "obs_lat": lat, "click_lon": lon + dlon, "click_lat": lat,
        }).json()
        assert body["feasible"] is True
        drop = 10000 * 10000 / (2 * R)
        assert body["lift_m"] == pytest.approx((150.0 + drop) * 2.0, rel=0.05)
        # 剖面与测地线随响应附带（同一份采样）
        profile, geodesic = body["profile"], body["geodesic"]
        assert profile is not None and geodesic is not None
        assert len(profile["dist_m"]) == len(profile["elev_m"]) >= 2
        assert profile["dist_m"][0] == 0.0
        assert profile["dist_m"][-1] == pytest.approx(body["distance_m"], abs=0.2)
        assert geodesic[0] == pytest.approx([lon - dlon, lat], abs=1e-4)
        assert geodesic[-1] == pytest.approx([lon + dlon, lat], abs=1e-4)

    def test_infeasible_message(self, monkeypatch) -> None:
        grid = make_grid(51, 100.0)
        import app.api.routes as routes
        monkeypatch.setattr(routes, "build_provider", lambda config: StaticProvider(grid))
        c = TestClient(create_app())
        body = c.post("/api/lift", json={
            "obs_lon": 0.0, "obs_lat": 0.0, "click_lon": 180.0, "click_lat": 0.0,
        }).json()
        assert body["feasible"] is False
        assert body["lift_m"] is None
        assert "无法通过抬升实现通视" in body["message"]
        # 几何不可行：测地线仍返回（连线渲染），剖面因 DEM 不覆盖为 None
        assert body["geodesic"] is not None
        assert body["geodesic"][0] == pytest.approx([0.0, 0.0], abs=1e-4)
        assert body["geodesic"][-1] == pytest.approx([180.0, 0.0], abs=1e-4)
        assert body["profile"] is None

    def test_decimated_indices(self) -> None:
        from app.api.routes import MAX_PROFILE_POINTS, _decimated_indices
        assert _decimated_indices(10, 100) == list(range(10))
        idx = _decimated_indices(5000, MAX_PROFILE_POINTS)
        assert len(idx) <= MAX_PROFILE_POINTS + 1
        assert idx[0] == 0 and idx[-1] == 4999
        assert all(b > a for a, b in zip(idx, idx[1:], strict=True))


def test_engine_and_source_lists(client: TestClient) -> None:
    engines = client.get("/api/engines").json()
    assert {e["id"] for e in engines} == {"angular-ray-sweep", "gdal-viewshed"}
    assert all("description" in e for e in engines)
    sources = client.get("/api/dem-sources").json()
    assert {s["id"] for s in sources} == {"aws-terrain-tiles", "offline-geotiff"}
