"""DEM 高程数据获取与采样。

支持两类数据源（对应【高级设置】中的 DEM 数据源下拉）：
- Terrarium 在线瓦片（AWS Terrain Tiles，无需密钥，本地磁盘缓存）；
- 用户自选离线 GeoTIFF（要求 WGS84 经纬度栅格）。

所有视线/遮挡计算统一使用有效地球半径模型（与折射设置联动），
采样密度由所用 DEM 的分辨率决定。
"""

from __future__ import annotations

import io
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import requests
from PIL import Image

logger = logging.getLogger(__name__)

EARTH_CIRCUMFERENCE_M = 40_075_016.686
TERRARIUM_MAX_ZOOM = 15
WEB_MERCATOR_MAX_LAT = 85.0511287798


class DEMError(Exception):
    """DEM 获取/读取失败（面向用户的中文错误信息）。"""


@dataclass
class ElevationGrid:
    """北向上（第 0 行为最北）、角点式地理配准的高程格网。"""

    elevations: np.ndarray  # (nrows, ncols) float32
    lon_min: float  # 西边缘经度
    lat_max: float  # 北边缘纬度
    dlon: float  # 每列度数（>0）
    dlat: float  # 每行度数（>0，向下递减）
    source: str
    nodata_mask: np.ndarray | None = None  # True 表示无效值

    @property
    def shape(self) -> tuple[int, int]:
        return self.elevations.shape

    def bounds(self) -> tuple[float, float, float, float]:
        nrows, ncols = self.shape
        return (self.lon_min, self.lat_max - self.dlat * nrows,
                self.lon_min + self.dlon * ncols, self.lat_max)

    def cell_size_m(self, lat: float) -> float:
        """近似单元格尺寸（米），取经纬方向平均。"""
        m_per_deg_lat = 111_320.0
        m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat))
        return 0.5 * (self.dlon * m_per_deg_lon + self.dlat * m_per_deg_lat)

    def sample(self, lon: np.ndarray | float, lat: np.ndarray | float) -> np.ndarray | float:
        """双线性采样；越界或无效值返回 nan。"""
        lon_arr = np.asarray(lon, dtype=np.float64)
        lat_arr = np.asarray(lat, dtype=np.float64)
        scalar = lon_arr.ndim == 0
        lon_arr, lat_arr = np.atleast_1d(lon_arr), np.atleast_1d(lat_arr)

        nrows, ncols = self.shape
        col = (lon_arr - self.lon_min) / self.dlon - 0.5
        row = (self.lat_max - lat_arr) / self.dlat - 0.5
        valid = (col >= 0) & (col <= ncols - 1) & (row >= 0) & (row <= nrows - 1)

        c0 = np.clip(np.floor(col).astype(int), 0, ncols - 2)
        r0 = np.clip(np.floor(row).astype(int), 0, nrows - 2)
        c1, r1 = c0 + 1, r0 + 1
        fc = np.clip(col - c0, 0.0, 1.0)
        fr = np.clip(row - r0, 0.0, 1.0)

        e = self.elevations
        v = (e[r0, c0] * (1 - fc) * (1 - fr) + e[r0, c1] * fc * (1 - fr)
             + e[r1, c0] * (1 - fc) * fr + e[r1, c1] * fc * fr)
        if self.nodata_mask is not None:
            bad = (self.nodata_mask[r0, c0] | self.nodata_mask[r0, c1]
                   | self.nodata_mask[r1, c0] | self.nodata_mask[r1, c1])
            v = np.where(bad, np.nan, v)
        v = np.where(valid, v, np.nan)
        return float(v[0]) if scalar else v  # type: ignore[return-value]

    def sample_profile(
        self, lon0: float, lat0: float, lon1: float, lat1: float, spacing_m: float
    ) -> tuple[np.ndarray, np.ndarray]:
        """沿两点大圆测地线按 spacing_m 采样，返回（距起点点距 m, 高程 m）。

        采样密度即所用 DEM 的单元格尺寸（由调用方传入）。
        """
        d, elev, _, _ = self.sample_geodesic_profile(lon0, lat0, lon1, lat1, spacing_m)
        return d, elev

    def sample_geodesic_profile(
        self, lon0: float, lat0: float, lon1: float, lat1: float, spacing_m: float
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """同 sample_profile，但一并返回采样顶点坐标（经度序列, 纬度序列）。

        剖面高程与测地线顶点出自同一组插值点，供剖面显示与地图连线共用。
        """
        r = 6_371_000.0
        p0 = _unit_vector(lon0, lat0)
        p1 = _unit_vector(lon1, lat1)
        dot = float(np.clip(np.dot(p0, p1), -1.0, 1.0))
        omega = math.acos(dot)
        dist = omega * r
        n = max(2, int(dist / spacing_m) + 1)
        t = np.linspace(0.0, 1.0, n)
        if omega < 1e-12:
            points = np.broadcast_to(p0, (n, 3))
        else:
            w0 = np.sin((1.0 - t) * omega) / math.sin(omega)
            w1 = np.sin(t * omega) / math.sin(omega)
            points = w0[:, None] * p0[None, :] + w1[:, None] * p1[None, :]
        lons = np.degrees(np.arctan2(points[:, 1], points[:, 0]))
        lats = np.degrees(np.arcsin(np.clip(points[:, 2], -1.0, 1.0)))
        d = t * dist
        elev = np.asarray(self.sample(lons, lats), dtype=np.float64)
        return d, elev, lons, lats


def _unit_vector(lon: float, lat: float) -> np.ndarray:
    la, lo = math.radians(lat), math.radians(lon)
    return np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])


def _tile_x(lon: float, z: int) -> float:
    return (lon + 180.0) / 360.0 * (2 ** z)


def _tile_y(lat: float, z: int) -> float:
    la = math.radians(max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, lat)))
    return (1.0 - math.asinh(math.tan(la)) / math.pi) / 2.0 * (2 ** z)


def _tile_lon(x: int, z: int) -> float:
    return x / (2 ** z) * 360.0 - 180.0


def _tile_lat(y: int, z: int) -> float:
    n = math.pi * (1.0 - 2.0 * y / (2 ** z))
    return math.degrees(math.atan(math.sinh(n)))


def _decode_terrarium(data: bytes) -> np.ndarray:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    arr = np.asarray(img, dtype=np.float32)
    return arr[..., 0] * 256.0 + arr[..., 1] + arr[..., 2] / 256.0 - 32768.0


def _default_fetch(url: str, timeout: float) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


@dataclass
class TerrariumProvider:
    """AWS Terrain Tiles（terrarium 编码 PNG）在线 DEM 提供者。"""

    url_template: str = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
    cache_dir: Path | None = None
    timeout: float = 20.0
    max_workers: int = 16
    name: str = "aws-terrain-tiles"
    _fetch: object = field(default=None, repr=False)  # callable(url, timeout) -> bytes，可注入

    def __post_init__(self) -> None:
        if self.cache_dir is None:
            self.cache_dir = Path.home() / ".cache" / "pva" / "dem" / "terrarium"

    def _select_zoom(self, lat_mid: float, target_cell_m: float) -> int:
        """按目标单元格尺寸（米/像素）就近选择缩放级别（0..15）。"""
        m_per_deg = EARTH_CIRCUMFERENCE_M * math.cos(math.radians(lat_mid)) / 360.0
        z = round(math.log2(m_per_deg * 360.0 / 256.0 / target_cell_m))
        return max(0, min(TERRARIUM_MAX_ZOOM, z))

    def fetch_bbox(
        self, lon_min: float, lat_min: float, lon_max: float, lat_max: float, target_cell_m: float
    ) -> ElevationGrid:
        lat_min_c = max(-WEB_MERCATOR_MAX_LAT, lat_min)
        lat_max_c = min(WEB_MERCATOR_MAX_LAT, lat_max)
        lat_mid = 0.5 * (lat_min_c + lat_max_c)
        z = self._select_zoom(lat_mid, target_cell_m)
        n = 2 ** z
        x0 = max(0, min(n - 1, int(math.floor(_tile_x(lon_min, z)))))
        x1 = max(0, min(n - 1, int(math.floor(_tile_x(lon_max, z)))))
        y0 = max(0, min(n - 1, int(math.floor(_tile_y(lat_max_c, z)))))
        y1 = max(0, min(n - 1, int(math.floor(_tile_y(lat_min_c, z)))))
        tiles = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
        logger.info(
            "DEM[%s] 获取: bbox=(%.4f, %.4f, %.4f, %.4f) target=%.0fm → z=%d 瓦片数=%d",
            self.name, lon_min, lat_min, lon_max, lat_max, target_cell_m, z, len(tiles),
        )
        t0 = time.perf_counter()
        arrays = self._load_tiles(tiles, z)
        logger.info("DEM[%s] 瓦片加载完成: %d 块, 耗时 %.2fs", self.name, len(arrays),
                    time.perf_counter() - t0)
        merc = np.vstack([np.hstack([arrays[(x, y)] for x in range(x0, x1 + 1)])
                          for y in range(y0, y1 + 1)])
        # 墨卡托行距随纬度变化：重采样为均匀纬度行（ElevationGrid 合同要求）
        nrows = merc.shape[0]
        north = _tile_lat(y0, z)
        south = _tile_lat(y1 + 1, z)
        dlat_u = (north - south) / nrows
        lat_centers = north - (np.arange(nrows) + 0.5) * dlat_u
        merc_y = ((1.0 - np.arcsinh(np.tan(np.radians(lat_centers))) / np.pi)
                  / 2.0 * (2 ** z) * 256.0)
        # 像素值锚定其中心（行 r 代表 merc 坐标 r+0.5 处的场值）
        frac = merc_y - y0 * 256.0 - 0.5
        r0 = np.clip(np.floor(frac).astype(np.int64), 0, nrows - 2)
        w = np.clip(frac - r0, 0.0, 1.0)[:, None]
        uniform = (merc[r0, :] * (1.0 - w) + merc[r0 + 1, :] * w).astype(np.float32)
        dlon = 360.0 / (2 ** z * 256)
        return ElevationGrid(
            elevations=uniform,
            lon_min=_tile_lon(x0, z), lat_max=north,
            dlon=dlon, dlat=dlat_u, source=self.name,
        )

    def _load_tiles(
        self, tiles: list[tuple[int, int]], z: int
    ) -> dict[tuple[int, int], np.ndarray]:
        def load(xy: tuple[int, int]) -> tuple[tuple[int, int], np.ndarray]:
            x, y = xy
            cache_file = self.cache_dir / str(z) / str(x) / f"{y}.png"  # type: ignore[union-attr]
            if cache_file.exists():
                logger.debug("DEM[%s] 命中缓存: z=%d x=%d y=%d", self.name, z, x, y)
                return xy, _decode_terrarium(cache_file.read_bytes())
            url = self.url_template.format(z=z, x=x, y=y)
            try:
                if self._fetch is not None:
                    data = self._fetch(url, self.timeout)  # type: ignore[misc]
                else:
                    data = _default_fetch(url, self.timeout)
            except Exception as exc:  # noqa: BLE001
                logger.warning("DEM[%s] 瓦片获取失败: %s", self.name, url)
                raise DEMError(f"DEM 在线获取失败（{url}）：{exc}") from exc
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(data)
            return xy, _decode_terrarium(data)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            return dict(pool.map(load, tiles))


@dataclass
class GeoTIFFProvider:
    """离线 GeoTIFF DEM（要求 WGS84 经纬度栅格）。"""

    path: str
    name: str = "offline-geotiff"

    def fetch_bbox(
        self, lon_min: float, lat_min: float, lon_max: float, lat_max: float, target_cell_m: float
    ) -> ElevationGrid:
        try:
            from osgeo import gdal, osr
        except ImportError as exc:
            raise DEMError("未安装 GDAL，无法读取离线 GeoTIFF") from exc
        gdal.UseExceptions()

        try:
            ds = gdal.Open(self.path)
        except (RuntimeError, OSError) as exc:
            raise DEMError(f"无法打开离线 DEM 文件：{self.path}（{exc}）") from exc
        if ds is None:
            raise DEMError(f"无法打开离线 DEM 文件：{self.path}")
        gt = ds.GetGeoTransform()
        if gt[1] <= 0 or gt[5] >= 0:
            raise DEMError("离线 DEM 文件须为北向上（north-up）的经纬度栅格")
        srs = osr.SpatialReference(wkt=ds.GetProjection())
        if srs.IsProjected():
            raise DEMError("离线 DEM 文件须为 WGS84 经纬度（EPSG:4326）栅格，当前为投影坐标系")

        band = ds.GetRasterBand(1)
        w, h = ds.RasterXSize, ds.RasterYSize
        logger.info("DEM[%s] GeoTIFF 读取: path=%s 请求窗口=(%.4f, %.4f, %.4f, %.4f)",
                    self.name, self.path, lon_min, lat_min, lon_max, lat_max)
        c0 = max(0, int(math.floor((lon_min - gt[0]) / gt[1])))
        c1 = min(w, int(math.ceil((lon_max - gt[0]) / gt[1])))
        r0 = max(0, int(math.floor((gt[3] - lat_max) / (-gt[5]))))
        r1 = min(h, int(math.ceil((gt[3] - lat_min) / (-gt[5]))))
        if c1 <= c0 or r1 <= r0:
            raise DEMError("所选范围与离线 DEM 文件不相交")

        arr = band.ReadAsArray(c0, r0, c1 - c0, r1 - r0).astype(np.float32)
        nodata = band.GetNoDataValue()
        mask = None
        if nodata is not None:
            mask = arr == np.float32(nodata)
            arr = arr.copy()
            arr[mask] = np.nan
        grid = ElevationGrid(
            elevations=arr,
            lon_min=gt[0] + c0 * gt[1], lat_max=gt[3] - r0 * (-gt[5]),
            dlon=gt[1], dlat=-gt[5], source=self.name, nodata_mask=mask,
        )
        ds = None
        return grid


def build_provider(config: dict) -> TerrariumProvider | GeoTIFFProvider:
    kind = config.get("type")
    if kind == "terrarium":
        return TerrariumProvider()
    if kind == "geotiff":
        path = config.get("path", "")
        if not path:
            raise DEMError("使用离线 GeoTIFF 需提供文件路径")
        return GeoTIFFProvider(path=path)
    raise DEMError(f"未知 DEM 数据源类型：{kind}")
