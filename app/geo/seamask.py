"""陆/海掩膜与海平面钳制。

DEM 负高程同时混装海底地形（bathymetry）与低于海平面的内陆水面/洼地（死海、
吐鲁番等），高程数值本身无法区分。引入 Natural Earth 50m 陆地多边形（公有领域）
作为陆/海判定：仅对**海洋**格点将高程钳制为海平面 0 m；内陆负高程保留原值。

数据首次使用时自动下载并缓存到 ~/.cache/pva/assets/；亦可用环境变量
PVA_LAND_GEOJSON 指定离线 GeoJSON 文件。掩膜不可用时保持原始高程（不钳制）
并记录告警——退化为此前行为，不阻断计算。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import numpy as np

from .dem import ElevationGrid

logger = logging.getLogger(__name__)

LAND_GEOJSON_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_50m_land.geojson"
)
LAND_GEOJSON_ENV = "PVA_LAND_GEOJSON"


def _default_land_path() -> Path:
    return Path.home() / ".cache" / "pva" / "assets" / "ne_50m_land.geojson"


def _download_land_data(dest: Path) -> bool:
    import requests

    try:
        resp = requests.get(LAND_GEOJSON_URL, timeout=120)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("陆海掩膜数据下载失败（%s）：%s", LAND_GEOJSON_URL, exc)
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    logger.info("陆海掩膜数据已下载: %s（%d KB）", dest, len(resp.content) // 1024)
    return True


def ensure_land_geojson() -> Path | None:
    """获取陆地多边形 GeoJSON 路径（环境变量 → 缓存 → 在线下载）。"""
    env = os.environ.get(LAND_GEOJSON_ENV)
    if env:
        path = Path(env)
        if path.exists():
            return path
        logger.warning("%s 指定的陆海掩膜文件不存在：%s", LAND_GEOJSON_ENV, env)
        return None
    cached = _default_land_path()
    if cached.exists():
        return cached
    return cached if _download_land_data(cached) else None


def compute_land_mask(grid: ElevationGrid, land_geojson: Path) -> np.ndarray | None:
    """将陆地多边形栅格化到与 grid 同构的布尔掩膜（True=陆地）；失败返回 None。"""
    try:
        from osgeo import gdal
    except ImportError:
        logger.warning("未安装 GDAL，无法生成陆海掩膜")
        return None
    gdal.UseExceptions()
    vec = None
    target = None
    try:
        vec = gdal.OpenEx(str(land_geojson), gdal.OF_VECTOR)
        layer = vec.GetLayer(0)
        nrows, ncols = grid.shape
        target = gdal.GetDriverByName("MEM").Create("", ncols, nrows, 1, gdal.GDT_Byte)
        target.SetGeoTransform(
            (grid.lon_min, grid.dlon, 0.0, grid.lat_max, 0.0, -grid.dlat)
        )
        if gdal.RasterizeLayer(target, [1], layer, burn_values=[1]) != 0:
            raise RuntimeError("RasterizeLayer 返回错误码")
        return np.asarray(target.GetRasterBand(1).ReadAsArray()) > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("陆海掩膜栅格化失败：%s", exc)
        return None
    finally:
        vec, target = None, None


def apply_sea_level_clamp(
    grid: ElevationGrid, land_geojson: Path | None = None
) -> ElevationGrid:
    """返回海洋格点高程钳制为 0 m 的新格网。

    内陆负高程保留；无负高程格点时直接原样返回（不触发数据获取）；
    掩膜不可用时保持原始高程并记录告警。
    """
    sea = grid.elevations < 0
    if not bool(sea.any()):
        return grid
    data_file = land_geojson if land_geojson is not None else ensure_land_geojson()
    if data_file is None:
        logger.warning("陆海掩膜不可用，负高程格点保持原样（未钳制海平面）")
        return grid
    land = compute_land_mask(grid, data_file)
    if land is None:
        return grid
    ocean = sea & ~land
    if not bool(ocean.any()):
        return grid
    clamped = grid.elevations.copy()
    clamped[ocean] = 0.0
    logger.info(
        "海平面钳制: 海洋负高程 %d 格 → 0m（内陆负高程 %d 格保留，格网共 %d 格）",
        int(ocean.sum()), int((sea & land).sum()), grid.elevations.size,
    )
    return ElevationGrid(
        elevations=clamped, lon_min=grid.lon_min, lat_max=grid.lat_max,
        dlon=grid.dlon, dlat=grid.dlat, source=grid.source, nodata_mask=grid.nodata_mask,
    )
