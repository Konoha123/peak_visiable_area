"""GDAL Viewshed 引擎适配层（备选/校验实现，经【高级设置】切换）。

观测高与目标高均取 DEM 地面（相对地面偏移 0）。
实测 GDAL 3.13 Python 绑定的 ViewshedGenerate 忽略曲率系数位置参数，
故将有效地球半径（与折射设置联动：纯几何 R / 7/6 R）预烘焙进输入 DEM：
elev' = elev - dist² / (2·R_eff)，随后 GDAL 的纯地形视线判定等价于曲率修正后的判定。
"""

from __future__ import annotations

import math
import time

import numpy as np

from .dem import DEMError, ElevationGrid
from .horizon import effective_earth_radius_m
from .viewshed import M_PER_DEG_LAT, ViewshedResult

ENGINE_NAME = "gdal-viewshed"


def compute_viewshed_gdal(
    grid: ElevationGrid,
    obs_lon: float,
    obs_lat: float,
    refraction: str,
    max_radius_m: float,
) -> ViewshedResult:
    try:
        from osgeo import gdal
    except ImportError as exc:
        raise DEMError("当前环境未安装 GDAL，无法使用 GDAL Viewshed 引擎") from exc
    gdal.UseExceptions()

    t0 = time.perf_counter()
    h_obs = grid.sample(obs_lon, obs_lat)
    if h_obs is None or math.isnan(float(h_obs)):
        raise DEMError("观测点落在 DEM 无数据区域或 DEM 覆盖范围之外")

    r_eff = effective_earth_radius_m(refraction)
    m_lon = M_PER_DEG_LAT * math.cos(math.radians(obs_lat))

    nrows, ncols = grid.shape
    col_c = np.arange(ncols)[None, :]
    row_c = np.arange(nrows)[:, None]
    ofx = (obs_lon - grid.lon_min) / grid.dlon - 0.5
    ofy = (grid.lat_max - obs_lat) / grid.dlat - 0.5
    x_m = (col_c - ofx) * grid.dlon * m_lon
    y_m = (ofy - row_c) * grid.dlat * M_PER_DEG_LAT
    dist = np.hypot(x_m, y_m)

    adjusted = grid.elevations.astype(np.float32) - (dist * dist / (2.0 * r_eff)).astype(np.float32)
    if grid.nodata_mask is not None:
        adjusted[grid.nodata_mask] = np.float32("nan")

    driver = gdal.GetDriverByName("MEM")
    ds = driver.Create("", ncols, nrows, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((grid.lon_min, grid.dlon, 0.0, grid.lat_max, 0.0, -grid.dlat))
    band = ds.GetRasterBand(1)
    band.WriteArray(adjusted)
    if grid.nodata_mask is not None:
        band.SetNoDataValue(float("nan"))

    max_dist_ground = max_radius_m / m_lon  # 地面坐标为经纬度（度）
    out = gdal.ViewshedGenerate(
        band, "MEM", "", [],
        obs_lon, obs_lat,
        0.0, 0.0,          # 观测/目标相对地面高均为 0（观测点高程已在 DEM 中）
        1, 0, 0, 0,        # 可见=1，不可见/超限/nodata=0
        1.0, 1, max_dist_ground,
    )
    arr = np.asarray(out.GetRasterBand(1).ReadAsArray())
    # GDAL 会将输出裁剪到最大距离圆包围盒，需映射回输入格网范围
    ogt = out.GetGeoTransform()
    row0 = int(round((grid.lat_max - ogt[3]) / grid.dlat))
    col0 = int(round((ogt[0] - grid.lon_min) / grid.dlon))
    visibility = np.zeros(grid.shape, dtype=bool)
    h, w = arr.shape
    r_dst, c_dst = max(0, row0), max(0, col0)
    r_src, c_src = max(0, -row0), max(0, -col0)
    hh = min(h - r_src, nrows - r_dst)
    ww = min(w - c_src, ncols - c_dst)
    if hh > 0 and ww > 0:
        region = arr[r_src:r_src + hh, c_src:c_src + ww] == 1
        visibility[r_dst:r_dst + hh, c_dst:c_dst + ww] = region
    if grid.nodata_mask is not None:
        visibility &= ~grid.nodata_mask
    # GDAL 的最大距离按坐标度各向同性裁剪，需再按米制约半径收紧
    visibility &= dist <= max_radius_m
    out, ds = None, None

    return ViewshedResult(
        visibility=visibility, grid=grid,
        observer_lon=obs_lon, observer_lat=obs_lat, observer_elev=float(h_obs),
        engine=ENGINE_NAME, refraction=refraction, radius_m=float(max_radius_m),
        ray_count=0, step_m=float(grid.cell_size_m(obs_lat)),
        elapsed_s=time.perf_counter() - t0,
    )
