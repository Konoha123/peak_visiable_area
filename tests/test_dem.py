import io
import math

import numpy as np
import pytest
from PIL import Image

from app.geo.dem import (
    DEMError,
    ElevationGrid,
    GeoTIFFProvider,
    TerrariumProvider,
    _decode_terrarium,
    build_provider,
)


def encode_terrarium(elev: np.ndarray) -> np.ndarray:
    v = np.clip(elev + 32768.0, 0.0, 256.0**2 - 1.0 / 256.0)
    r = np.floor(v / 256.0)
    rem = v - r * 256.0
    g = np.floor(rem)
    b = np.clip(np.round((rem - g) * 256.0), 0, 255)
    return np.stack([r, g, b], axis=-1).astype(np.uint8)


def synthetic_elev(gx: np.ndarray | int, gy: np.ndarray | int) -> np.ndarray | int:
    """以全局像素坐标（z 层级无关）定义的确定性整值高程场（保持低于 32768 m）。"""
    return 100 + 1 * np.asarray(gx) + 2 * np.asarray(gy)


def make_synthetic_fetch(call_log: list[tuple[int, int, int]]):
    def fetch(url: str, timeout: float) -> bytes:
        parts = url.split("/")
        z, x, y = int(parts[-3]), int(parts[-2]), int(parts[-1].split(".")[0])
        call_log.append((z, x, y))
        gy, gx = np.mgrid[0:256, 0:256]
        img = encode_terrarium(synthetic_elev(x * 256 + gx, y * 256 + gy))
        buf = io.BytesIO()
        Image.fromarray(img).save(buf, format="PNG")
        return buf.getvalue()

    return fetch


@pytest.fixture()
def log() -> list:
    return []


@pytest.fixture()
def provider(tmp_path, log) -> TerrariumProvider:
    return TerrariumProvider(cache_dir=tmp_path / "cache", _fetch=make_synthetic_fetch(log))


class TestTerrarium:
    def test_decode_roundtrip(self) -> None:
        elev = np.array([[100.0, -100.0], [0.25, 12345.6]])
        buf = io.BytesIO()
        Image.fromarray(encode_terrarium(elev)).save(buf, format="PNG")
        decoded = _decode_terrarium(buf.getvalue())
        assert np.allclose(decoded, elev, atol=1 / 256.0)

    def test_zoom_clamped(self, provider: TerrariumProvider) -> None:
        assert provider._select_zoom(41.0, 1e7) == 0
        assert provider._select_zoom(41.0, 1.0) == 15

    def test_zoom_selection(self, provider: TerrariumProvider) -> None:
        # lat=41 时每度约 84000 m：target 90 m -> z=10（mpp≈115 m 最近）
        assert provider._select_zoom(41.0, 90.0) == 10

    def test_fetch_bbox_values_and_stitch(self, provider: TerrariumProvider) -> None:
        grid = provider.fetch_bbox(10.0, 40.0, 12.0, 42.0, 3000.0)
        assert grid.source == "aws-terrain-tiles"
        z = provider._select_zoom(41.0, 3000.0)
        assert grid.shape[0] == ((grid.shape[0] + 255) // 256) * 256  # 按瓦片行拼接
        dlon = 360.0 / (2**z * 256)
        # 在格网内随机取像素中心，验证拼接与解码自洽
        rng = np.random.default_rng(42)
        rows = rng.integers(1, grid.shape[0] - 1, size=50)
        cols = rng.integers(1, grid.shape[1] - 1, size=50)
        vals = grid.elevations[rows, cols]
        x0 = min(t[1] for t in log)
        y0 = min(t[2] for t in log)
        gxs, gys = x0 * 256 + cols, y0 * 256 + rows
        assert np.allclose(vals, synthetic_elev(gxs, gys), atol=0.05)
        # 采样经纬度（由格网自描述构造）应取到同一像素值
        lons = grid.lon_min + (cols + 0.5) * grid.dlon
        lats = grid.lat_max - (rows + 0.5) * grid.dlat
        assert np.allclose(grid.sample(lons, lats), vals, atol=0.01)
        assert grid.dlon == pytest.approx(dlon)

    def test_cache_prevents_refetch(self, provider: TerrariumProvider, log: list) -> None:
        provider.fetch_bbox(10.0, 40.0, 10.5, 40.5, 3000.0)
        first = len(log)
        assert first > 0
        provider.fetch_bbox(10.0, 40.0, 10.5, 40.5, 3000.0)
        assert len(log) == first

    def test_fetch_error_wrapped(self, tmp_path, log) -> None:
        def failing(url: str, timeout: float) -> bytes:
            raise OSError("boom")

        p = TerrariumProvider(cache_dir=tmp_path / "c", _fetch=failing)
        with pytest.raises(DEMError, match="DEM 在线获取失败"):
            p.fetch_bbox(10.0, 40.0, 10.2, 40.2, 3000.0)


def _write_geotiff(path, elevations: np.ndarray, nodata: float | None, epsg: int | None) -> None:
    from osgeo import gdal, osr

    h, w = elevations.shape
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, w, h, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((10.0, 0.01, 0.0, 45.0, 0.0, -0.01))
    if epsg is not None:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(epsg)
        ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    band.WriteArray(elevations.astype(np.float32))
    ds = None


@pytest.fixture()
def tif_file(tmp_path):
    path = str(tmp_path / "dem.tif")
    elev = 1000.0 + np.arange(100 * 100, dtype=np.float64).reshape(100, 100)
    elev[0, 0] = -9999.0
    _write_geotiff(path, elev, -9999.0, 4326)
    return path, elev


class TestGeoTIFF:
    def test_read_window_and_sample(self, tif_file) -> None:
        path, elev = tif_file
        p = GeoTIFFProvider(path=path)
        grid = p.fetch_bbox(10.2, 44.2, 11.5, 44.8, 1000.0)
        assert grid.source == "offline-geotiff"
        # 取窗口内部的全局像素 (row 50, col 60) 中心（位于请求 bbox 内）
        lon = 10.0 + (60 + 0.5) * 0.01
        lat = 45.0 - (50 + 0.5) * 0.01
        assert grid.sample(lon, lat) == pytest.approx(elev[50, 60])

    def test_nodata_sample_is_nan(self, tif_file) -> None:
        path, _ = tif_file
        grid = GeoTIFFProvider(path=path).fetch_bbox(9.9, 44.9, 11.0, 45.1, 1000.0)
        assert math.isnan(grid.sample(10.005, 44.995))  # (0,0) 像素中心为 nodata

    def test_projected_crs_rejected(self, tmp_path) -> None:
        path = str(tmp_path / "proj.tif")
        _write_geotiff(path, np.zeros((10, 10)), None, 3857)
        with pytest.raises(DEMError, match="EPSG:4326"):
            GeoTIFFProvider(path=path).fetch_bbox(0, 0, 1, 1, 1000.0)

    def test_disjoint_bbox(self, tif_file) -> None:
        path, _ = tif_file
        with pytest.raises(DEMError, match="不相交"):
            GeoTIFFProvider(path=path).fetch_bbox(50.0, 10.0, 51.0, 11.0, 1000.0)

    def test_missing_file(self) -> None:
        with pytest.raises(DEMError, match="无法打开"):
            GeoTIFFProvider(path="/nonexistent/dem.tif").fetch_bbox(0, 0, 1, 1, 1000.0)


class TestGridSampling:
    def test_bilinear_and_out_of_range(self) -> None:
        e = np.array([[0.0, 10.0], [20.0, 30.0]], dtype=np.float32)
        g = ElevationGrid(e, lon_min=0.0, lat_max=2.0, dlon=1.0, dlat=1.0, source="t")
        assert g.sample(0.5, 1.5) == pytest.approx(0.0)  # 中心 (row0,col0)
        assert g.sample(1.5, 1.5) == pytest.approx(10.0)
        assert g.sample(1.0, 1.0) == pytest.approx(15.0)  # 四点均值
        assert math.isnan(g.sample(-5.0, 0.0))
        assert math.isnan(g.sample(0.0, 9.0))

    def test_nodata_propagates_nan(self) -> None:
        e = np.array([[0.0, 10.0], [20.0, 30.0]], dtype=np.float32)
        g = ElevationGrid(e, 0.0, 2.0, 1.0, 1.0, "t", nodata_mask=np.zeros((2, 2), bool))
        g.nodata_mask[0, 1] = True
        assert math.isnan(g.sample(1.5, 1.5))

    def test_sample_profile_flat(self) -> None:
        g = ElevationGrid(np.full((50, 50), 500.0, dtype=np.float32),
                          lon_min=0.0, lat_max=1.0, dlon=0.02, dlat=0.02, source="t")
        d, elev = g.sample_profile(0.1, 0.1, 0.5, 0.6, 2000.0)
        assert len(d) == len(elev) >= 2
        assert np.allclose(elev, 500.0)
        assert d[0] == 0.0 and d[-1] == pytest.approx(d[-1], abs=1.0)


class TestFactory:
    def test_terrarium_default(self) -> None:
        assert isinstance(build_provider({"type": "terrarium"}), TerrariumProvider)

    def test_geotiff_requires_path(self) -> None:
        with pytest.raises(DEMError, match="文件路径"):
            build_provider({"type": "geotiff"})

    def test_unknown_type(self) -> None:
        with pytest.raises(DEMError, match="未知 DEM 数据源"):
            build_provider({"type": "foo"})
