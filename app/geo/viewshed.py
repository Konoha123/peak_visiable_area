"""自研角向射线扫描（切线法）Viewshed 引擎。

以观测点为圆心沿 M 个方位角发射射线，逐距离步进维护最大视线切线角
（考虑有效地球半径对应的地形下沉 d²/(2R_eff)，与折射设置联动）；
射线间未覆盖的格点按最近射线的切线包络判定。

复杂度 O(M×L)（≈ 2×格点数），矢量化分块实现，满足同步秒级计算。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from .dem import DEMError, ElevationGrid
from .horizon import effective_earth_radius_m

M_PER_DEG_LAT = 111_320.0
MAX_PROFILE_SAMPLES = 4000
MEMORY_BUDGET_BYTES = 64_000_000
RAY_CHUNK = 256
ENGINE_NAME = "angular-ray-sweep"


@dataclass
class ViewshedResult:
    visibility: np.ndarray  # bool (nrows, ncols)
    grid: ElevationGrid
    observer_lon: float
    observer_lat: float
    observer_elev: float
    engine: str
    refraction: str
    radius_m: float
    ray_count: int
    step_m: float
    elapsed_s: float


def _bilinear(e: np.ndarray, rows: np.ndarray, cols: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """按浮点行列坐标双线性采样；返回（值, 有效掩码）。"""
    nrows, ncols = e.shape
    valid = (cols >= 0) & (cols <= ncols - 1) & (rows >= 0) & (rows <= nrows - 1)
    c0 = np.clip(np.floor(cols).astype(np.int32), 0, ncols - 2)
    r0 = np.clip(np.floor(rows).astype(np.int32), 0, nrows - 2)
    fc = np.clip(cols - c0, 0.0, 1.0)
    fr = np.clip(rows - r0, 0.0, 1.0)
    v = (e[r0, c0] * (1 - fc) * (1 - fr) + e[r0, c0 + 1] * fc * (1 - fr)
         + e[r0 + 1, c0] * (1 - fc) * fr + e[r0 + 1, c0 + 1] * fc * fr)
    return v, valid


def compute_viewshed(
    grid: ElevationGrid,
    obs_lon: float,
    obs_lat: float,
    refraction: str,
    max_radius_m: float,
) -> ViewshedResult:
    t0 = time.perf_counter()

    h_obs = grid.sample(obs_lon, obs_lat)
    if h_obs is None or math.isnan(float(h_obs)):
        raise DEMError("观测点落在 DEM 无数据区域或 DEM 覆盖范围之外")

    r_eff = effective_earth_radius_m(refraction)
    cell = grid.cell_size_m(obs_lat)
    step = max(cell, max_radius_m / MAX_PROFILE_SAMPLES)
    n_steps = max(2, int(math.ceil(max_radius_m / step)))
    m_lon = M_PER_DEG_LAT * math.cos(math.radians(obs_lat))

    ofx = (obs_lon - grid.lon_min) / grid.dlon - 0.5
    ofy = (grid.lat_max - obs_lat) / grid.dlat - 0.5

    ray_cap = max(360, min(8640, int(MEMORY_BUDGET_BYTES // (4 * n_steps))))
    n_rays = max(360, min(ray_cap, int(math.ceil(2 * math.pi * max_radius_m / (1.5 * cell)))))

    phi_run = np.full((n_rays, n_steps), -np.inf, dtype=np.float32)
    vis = np.zeros(grid.shape, dtype=bool)
    visited = np.zeros(grid.shape, dtype=bool)
    e = grid.elevations
    nrows, ncols = grid.shape

    d_row = (np.arange(1, n_steps + 1, dtype=np.float32) * np.float32(step))[None, :]
    for s in range(0, n_rays, RAY_CHUNK):
        idx = np.arange(s, min(s + RAY_CHUNK, n_rays))
        theta = ((idx + 0.5) * (2 * math.pi / n_rays)).astype(np.float32)[:, None]
        px = np.cos(theta) * d_row
        py = np.sin(theta) * d_row
        cols = np.float32(ofx) + px / np.float32(m_lon * grid.dlon)
        rows = np.float32(ofy) - py / np.float32(M_PER_DEG_LAT * grid.dlat)

        vals, ok = _bilinear(e, rows, cols)
        ok &= d_row <= np.float32(max_radius_m)
        phi = np.where(ok, (vals - np.float32(h_obs)) / d_row - d_row / np.float32(2 * r_eff),
                       -np.inf)
        run = np.maximum.accumulate(phi, axis=1)
        phi_run[idx] = run
        prev = np.concatenate(
            [np.full((len(idx), 1), -np.inf, dtype=np.float32), run[:, :-1]], axis=1
        )
        vis_ray = (phi >= prev) & ok

        ri = np.rint(rows).astype(np.int32)
        ci = np.rint(cols).astype(np.int32)
        inb = ok & (ri >= 0) & (ri < nrows) & (ci >= 0) & (ci < ncols)
        visited[ri[inb], ci[inb]] = True
        sel = vis_ray & inb
        vis[ri[sel], ci[sel]] = True

    # 射线间未覆盖格点：按最近射线在同一距离处的切线包络判定
    col_c = np.arange(ncols)[None, :]
    row_c = np.arange(nrows)[:, None]
    x_m = (col_c - ofx) * grid.dlon * m_lon
    y_m = (ofy - row_c) * grid.dlat * M_PER_DEG_LAT
    dist = np.sqrt(x_m * x_m + y_m * y_m)
    with np.errstate(divide="ignore", invalid="ignore"):
        phi_c = (e - h_obs) / np.where(dist > 0, dist, 1.0) - dist / (2.0 * r_eff)
    j = np.mod(np.rint(np.arctan2(y_m, x_m) * (n_rays / (2 * math.pi))).astype(np.int64), n_rays)
    # 按精确距离对射线包络做线性插值（而非取最近步），避免边缘整步偏差
    kf = dist / step - 1.0
    k0 = np.clip(np.floor(kf).astype(np.int64), 0, n_steps - 1)
    k1 = np.clip(k0 + 1, 0, n_steps - 1)
    w = np.clip(kf - k0, 0.0, 1.0).astype(np.float32)
    phi_lim = (1 - w) * phi_run[j, k0] + w * phi_run[j, k1]
    target = (~visited) & (dist <= max_radius_m) & (dist > 0.5 * step) & np.isfinite(e)
    if grid.nodata_mask is not None:
        target &= ~grid.nodata_mask
    vis |= target & np.isfinite(phi_lim) & (phi_c >= phi_lim - 1e-6)

    # 观测点所在格点本身可见
    vis[int(round(ofy)), int(round(ofx))] = True
    if grid.nodata_mask is not None:
        vis &= ~grid.nodata_mask

    return ViewshedResult(
        visibility=vis, grid=grid,
        observer_lon=obs_lon, observer_lat=obs_lat, observer_elev=float(h_obs),
        engine=ENGINE_NAME, refraction=refraction, radius_m=float(max_radius_m),
        ray_count=n_rays, step_m=float(step), elapsed_s=time.perf_counter() - t0,
    )
