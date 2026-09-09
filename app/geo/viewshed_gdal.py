"""GDAL Viewshed 引擎适配层（备选/校验实现，经【高级设置】切换）。

实现说明（GDAL 3.13 实测）：
- Python 绑定 ViewshedGenerate 的位置参数（观测高/曲率系数等）存在错位失效；
- gdal_viewshed 工具的 -cc 曲率系数同样失效，且 -md 最大距离参数有误杀近距离格点的缺陷。

因此本适配层采用：子进程调用 gdal_viewshed + 将有效地球半径（与折射设置联动）
预烘焙进输入 DEM（elev' = elev - dist²/(2·R_eff)）+ 弃用 -cc/-md，
按米制约半径自行裁剪。复杂地形（如观测点位于局部洼地）下 GDAL 参考平面算法
可能较自研引擎保守，结果仅作交叉校验参考。
"""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

from .dem import DEMError, ElevationGrid
from .horizon import effective_earth_radius_m
from .viewshed import M_PER_DEG_LAT, ViewshedResult

ENGINE_NAME = "gdal-viewshed"
GDAL_VIEWSHED_BIN = "gdal_viewshed"


def compute_viewshed_gdal(
    grid: ElevationGrid,
    obs_lon: float,
    obs_lat: float,
    refraction: str,
    max_radius_m: float,
) -> ViewshedResult:
    if shutil.which(GDAL_VIEWSHED_BIN) is None:
        raise DEMError("未找到 gdal_viewshed 工具（需经 conda 安装 GDAL）")

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
    nodata_value = -9999.0
    if grid.nodata_mask is not None:
        adjusted[grid.nodata_mask] = np.float32(nodata_value)

    tmp_in = Path(tempfile.mktemp(suffix=".tif"))
    tmp_out = Path(tempfile.mktemp(suffix=".tif"))
    try:
        drv = gdal.GetDriverByName("GTiff")
        ds = drv.Create(str(tmp_in), ncols, nrows, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((grid.lon_min, grid.dlon, 0.0, grid.lat_max, 0.0, -grid.dlat))
        band = ds.GetRasterBand(1)
        band.WriteArray(adjusted)
        if grid.nodata_mask is not None:
            band.SetNoDataValue(nodata_value)
        ds = None

        cmd = [GDAL_VIEWSHED_BIN, "-ox", str(obs_lon), "-oy", str(obs_lat),
               "-oz", "0", "-f", "GTiff", str(tmp_in), str(tmp_out)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise DEMError(f"gdal_viewshed 执行失败：{proc.stderr.strip()[-200:]}")

        out_ds = gdal.Open(str(tmp_out))
        arr = np.asarray(out_ds.GetRasterBand(1).ReadAsArray())
        ogt = out_ds.GetGeoTransform()
        out_ds = None

        row0 = int(round((grid.lat_max - ogt[3]) / grid.dlat))
        col0 = int(round((ogt[0] - grid.lon_min) / grid.dlon))
        visibility = np.zeros(grid.shape, dtype=bool)
        h, w = arr.shape
        r_dst, c_dst = max(0, row0), max(0, col0)
        r_src, c_src = max(0, -row0), max(0, -col0)
        hh = min(h - r_src, nrows - r_dst)
        ww = min(w - c_src, ncols - c_dst)
        if hh > 0 and ww > 0:
            region = arr[r_src:r_src + hh, c_src:c_src + ww] == 255
            visibility[r_dst:r_dst + hh, c_dst:c_dst + ww] = region
    finally:
        for f in (tmp_in, tmp_out):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    if grid.nodata_mask is not None:
        visibility &= ~grid.nodata_mask
    visibility &= dist <= max_radius_m

    return ViewshedResult(
        visibility=visibility, grid=grid,
        observer_lon=obs_lon, observer_lat=obs_lat, observer_elev=float(h_obs),
        engine=ENGINE_NAME, refraction=refraction, radius_m=float(max_radius_m),
        ray_count=0, step_m=float(grid.cell_size_m(obs_lat)),
        elapsed_s=time.perf_counter() - t0,
    )
