"""HTTP API：坐标校验、曲率上限、可视域计算与点选测高。"""

from __future__ import annotations

import logging
import math

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..geo.coords import parse_coordinates
from ..geo.dem import DEMError, build_provider
from ..geo.horizon import MIN_DISPLAY_RADIUS_M, display_radius_m, horizon_radius_m
from ..geo.lift import IMPOSSIBLE_MESSAGE, solve_min_lift
from ..geo.seamask import apply_sea_level_clamp
from ..geo.viewshed import compute_viewshed
from ..geo.viewshed_gdal import compute_viewshed_gdal
from ..render import viewshed_to_overlay

router = APIRouter(prefix="/api")

logger = logging.getLogger(__name__)

ENGINES = [
    {
        "id": "angular-ray-sweep",
        "name": "自研角向射线扫描",
        "description": "逐方位角追踪视线切线包络，纯 NumPy 矢量化实现，速度较快（推荐）。",
    },
    {
        "id": "gdal-viewshed",
        "name": "GDAL Viewshed",
        "description": "调用 GDAL 官方 gdal_viewshed 工具（曲率已预烘焙），"
                       "可作自研引擎的交叉校验；复杂地形下结果可能偏保守，耗时略长。",
    },
]

DEM_SOURCES = [
    {
        "id": "aws-terrain-tiles",
        "name": "AWS Terrain Tiles（在线）",
        "description": "在线高程瓦片服务（terrarium 编码，免密钥），结果自动本地缓存。",
        "type": "terrarium",
    },
    {
        "id": "offline-geotiff",
        "name": "离线 GeoTIFF 文件",
        "description": "用户自选的本地 GeoTIFF 高程文件（须为 WGS84 经纬度栅格）。",
        "type": "geotiff",
    },
]

# 分析格网规模约束（内存/性能权衡）
NEAR_CELL_M = 30.0
FAR_CELL_M = 90.0
NEAR_RADIUS_LIMIT_M = 50_000.0
MAX_GRID_DIM = 3000
HORIZON_WINDOW_M = 2000.0

# 测高响应中剖面/测地线的抽稀上限（仅影响显示数据量，计算仍用全采样）
MAX_PROFILE_POINTS = 2048
MAX_LINE_VERTICES = 512


def _decimated_indices(n: int, cap: int) -> list[int]:
    """0..n-1 的等距抽稀索引（必含首末点）。"""
    if n <= cap:
        return list(range(n))
    step = (n - 1) / (cap - 1)
    idx = sorted({round(i * step) for i in range(cap)})
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx


class ValidateRequest(BaseModel):
    text: str
    format: str = Field(pattern="^(decimal|dms)$")


class HorizonRequest(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    refraction: str = Field(default="geometric", pattern="^(geometric|standard)$")
    dem_source: dict = Field(default_factory=lambda: {"type": "terrarium"})


class ViewshedRequest(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    refraction: str = Field(default="geometric", pattern="^(geometric|standard)$")
    engine: str = Field(default="angular-ray-sweep", pattern="^(angular-ray-sweep|gdal-viewshed)$")
    radius_m: float = Field(gt=0, le=1_000_000)
    dem_source: dict = Field(default_factory=lambda: {"type": "terrarium"})


class LiftRequest(BaseModel):
    obs_lon: float = Field(ge=-180, le=180)
    obs_lat: float = Field(ge=-90, le=90)
    click_lon: float = Field(ge=-180, le=180)
    click_lat: float = Field(ge=-90, le=90)
    refraction: str = Field(default="geometric", pattern="^(geometric|standard)$")
    dem_source: dict = Field(default_factory=lambda: {"type": "terrarium"})


@router.get("/engines")
def list_engines() -> list[dict]:
    return ENGINES


@router.get("/dem-sources")
def list_dem_sources() -> list[dict]:
    return DEM_SOURCES


class BasemapRequest(BaseModel):
    path: str


@router.post("/basemap")
def basemap(req: BasemapRequest) -> dict:
    """离线底图：读取 GeoTIFF 渲染为叠加图像。"""
    import base64
    import io

    import numpy as np
    from PIL import Image

    from ..geo.dem import GeoTIFFProvider

    try:
        grid = GeoTIFFProvider(path=req.path).fetch_bbox(-180, -90, 180, 90, 90.0)
    except DEMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    arr = grid.elevations.astype(np.float64)
    valid = np.isfinite(arr)
    scale = max(1, int(np.ceil(max(arr.shape) / 2048)))
    kept = (arr.shape[0] // scale * scale, arr.shape[1] // scale * scale)
    arr_s = arr[:kept[0]:scale, :kept[1]:scale]
    valid_s = valid[:kept[0]:scale, :kept[1]:scale]

    lo, hi = np.nanpercentile(arr_s[valid_s], [2, 98]) if valid_s.any() else (0.0, 1.0)
    t = np.clip((arr_s - lo) / max(hi - lo, 1e-6), 0, 1)
    rgba = np.zeros((*t.shape, 4), dtype=np.uint8)
    rgba[..., 0] = (40 + t * 200).astype(np.uint8)
    rgba[..., 1] = (90 + t * 110).astype(np.uint8)
    rgba[..., 2] = (150 - t * 100).astype(np.uint8)
    rgba[..., 3] = np.where(valid_s, 255, 0).astype(np.uint8)

    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    west = grid.lon_min
    east = grid.lon_min + grid.dlon * kept[1]
    north = grid.lat_max
    south = grid.lat_max - grid.dlat * kept[0]
    return {"image": data_uri, "bounds": [west, south, east, north]}


@router.post("/validate")
def validate_coords(req: ValidateRequest) -> dict:
    res = parse_coordinates(req.text, req.format)
    logger.info("校验: format=%s text=%r → ok=%s lon=%s lat=%s",
                req.format, req.text[:60], res.ok, res.lon, res.lat)
    return {"ok": res.ok, "lon": res.lon, "lat": res.lat, "message": res.message}


@router.post("/horizon")
def horizon(req: HorizonRequest) -> dict:
    provider = build_provider(req.dem_source)
    dlat = HORIZON_WINDOW_M / 111_320.0
    dlon = HORIZON_WINDOW_M / (111_320.0 * math.cos(math.radians(req.lat)))
    try:
        grid = provider.fetch_bbox(req.lon - dlon, req.lat - dlat, req.lon + dlon, req.lat + dlat,
                                   NEAR_CELL_M)
        grid = apply_sea_level_clamp(grid)
        elev = grid.sample(req.lon, req.lat)
        if elev is None or math.isnan(float(elev)):
            raise DEMError("观测点处无 DEM 高程数据")
    except DEMError as exc:
        logger.warning("地平线计算失败(%s): %s", req.dem_source, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    radius, fallback = display_radius_m(float(elev), req.refraction)
    logger.info("地平线: 观测点(%.5f, %.5f) 高程=%.1fm 折射=%s → 半径=%.1fkm 兜底=%s",
                req.lon, req.lat, float(elev), req.refraction, radius / 1000, fallback)
    return {
        "elevation_m": float(elev),
        "horizon_radius_m": horizon_radius_m(float(elev), req.refraction),
        "display_radius_m": radius,
        "min_display_radius_m": MIN_DISPLAY_RADIUS_M,
        "fallback": fallback,
        "refraction": req.refraction,
        "dem_source": getattr(provider, "name", req.dem_source.get("type", "")),
    }


def _analysis_cell_m(radius_m: float) -> float:
    base = NEAR_CELL_M if radius_m <= NEAR_RADIUS_LIMIT_M else FAR_CELL_M
    return max(base, 2.0 * radius_m / MAX_GRID_DIM)


def _fetch_analysis_grid(req_lon: float, req_lat: float, radius_m: float, dem_source: dict):
    provider = build_provider(dem_source)
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * math.cos(math.radians(req_lat)))
    grid = provider.fetch_bbox(req_lon - dlon, req_lat - dlat, req_lon + dlon, req_lat + dlat,
                               _analysis_cell_m(radius_m))
    return provider, apply_sea_level_clamp(grid)


@router.post("/viewshed")
def viewshed(req: ViewshedRequest) -> dict:
    logger.info("可视域请求: 观测点(%.5f, %.5f) 引擎=%s 半径=%.1fkm 折射=%s DEM=%s",
                req.lon, req.lat, req.engine, req.radius_m / 1000, req.refraction,
                req.dem_source)
    try:
        _, grid = _fetch_analysis_grid(req.lon, req.lat, req.radius_m, req.dem_source)
        compute = compute_viewshed if req.engine == "angular-ray-sweep" else compute_viewshed_gdal
        result = compute(grid, req.lon, req.lat, req.refraction, req.radius_m)
    except DEMError as exc:
        logger.warning("可视域计算失败: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    data_uri, bounds = viewshed_to_overlay(result)
    logger.info("可视域响应: 可见=%d 格网=%dx%d 图层=%dKB",
                int(result.visibility.sum()), grid.shape[0], grid.shape[1],
                len(data_uri) // 1024)
    return {
        "image": data_uri,
        "bounds": list(bounds),
        "cell_size_m": grid.cell_size_m(req.lat),
        "grid_shape": list(grid.shape),
        "radius_m": req.radius_m,
        "elevation_m": result.observer_elev,
        "engine": result.engine,
        "refraction": result.refraction,
        "visible_cells": int(result.visibility.sum()),
        "elapsed_s": round(result.elapsed_s, 3),
        "dem_source": grid.source,
    }


@router.post("/lift")
def lift(req: LiftRequest) -> dict:
    margin_m = 1000.0
    lon_min, lon_max = sorted((req.obs_lon, req.click_lon))
    lat_min, lat_max = sorted((req.obs_lat, req.click_lat))
    dlon_m = margin_m / (111_320.0 * math.cos(math.radians(req.obs_lat)))
    dlat_m = margin_m / 111_320.0
    provider = build_provider(req.dem_source)
    try:
        grid = provider.fetch_bbox(lon_min - dlon_m, lat_min - dlat_m,
                                   lon_max + dlon_m, lat_max + dlat_m, NEAR_CELL_M)
        grid = apply_sea_level_clamp(grid)
        res = solve_min_lift(grid, req.obs_lon, req.obs_lat, req.click_lon, req.click_lat,
                             req.refraction)
    except DEMError as exc:
        logger.warning("测高失败: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    profile_json = None
    if res.profile is not None:
        d_arr, e_arr = res.profile
        idx = _decimated_indices(len(d_arr), MAX_PROFILE_POINTS)
        profile_json = {
            "dist_m": [round(float(d_arr[i]), 1) for i in idx],
            "elev_m": [round(float(e_arr[i]), 1) for i in idx],
        }
    geodesic_json = None
    if res.geodesic is not None:
        idx = _decimated_indices(len(res.geodesic), MAX_LINE_VERTICES)
        geodesic_json = [[round(float(res.geodesic[i, 0]), 6),
                          round(float(res.geodesic[i, 1]), 6)] for i in idx]

    logger.info("测高响应: 可行=%s 抬升=%s 距离=%.1fm 剖面=%s 测地线=%d 点",
                res.feasible,
                f"{res.lift_m:.1f}m" if res.lift_m is not None else "N/A",
                res.distance_m,
                f"{len(profile_json['dist_m'])} 点" if profile_json else "无",
                len(geodesic_json) if geodesic_json else 0)
    return {
        "feasible": res.feasible,
        "lift_m": res.lift_m,
        "message": None if res.feasible else IMPOSSIBLE_MESSAGE,
        "observer_elev_m": None if math.isnan(res.observer_elev) else res.observer_elev,
        "clicked_elev_m": None if math.isnan(res.clicked_elev) else res.clicked_elev,
        "distance_m": res.distance_m,
        "central_angle_deg": round(res.central_angle_deg, 4),
        "refraction": res.refraction,
        "profile": profile_json,
        "geodesic": geodesic_json,
    }
