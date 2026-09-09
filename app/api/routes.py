"""HTTP API：坐标校验、曲率上限、可视域计算与点选测高。"""

from __future__ import annotations

import math

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..geo.coords import parse_coordinates
from ..geo.dem import DEMError, build_provider
from ..geo.horizon import MIN_DISPLAY_RADIUS_M, display_radius_m, horizon_radius_m
from ..geo.lift import IMPOSSIBLE_MESSAGE, solve_min_lift
from ..geo.viewshed import compute_viewshed
from ..geo.viewshed_gdal import compute_viewshed_gdal
from ..render import viewshed_to_overlay

router = APIRouter(prefix="/api")

ENGINES = [
    {
        "id": "angular-ray-sweep",
        "name": "自研角向射线扫描",
        "description": "逐方位角追踪视线切线包络，纯 NumPy 矢量化实现，速度较快（推荐）。",
    },
    {
        "id": "gdal-viewshed",
        "name": "GDAL Viewshed",
        "description": "调用 GDAL 内置可视域算法，可作为自研引擎结果的交叉校验（需 GDAL 环境）。",
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


@router.post("/validate")
def validate_coords(req: ValidateRequest) -> dict:
    res = parse_coordinates(req.text, req.format)
    return {"ok": res.ok, "lon": res.lon, "lat": res.lat, "message": res.message}


@router.post("/horizon")
def horizon(req: HorizonRequest) -> dict:
    provider = build_provider(req.dem_source)
    dlat = HORIZON_WINDOW_M / 111_320.0
    dlon = HORIZON_WINDOW_M / (111_320.0 * math.cos(math.radians(req.lat)))
    try:
        grid = provider.fetch_bbox(req.lon - dlon, req.lat - dlat, req.lon + dlon, req.lat + dlat,
                                   NEAR_CELL_M)
        elev = grid.sample(req.lon, req.lat)
        if elev is None or math.isnan(float(elev)):
            raise DEMError("观测点处无 DEM 高程数据")
    except DEMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    radius, fallback = display_radius_m(float(elev), req.refraction)
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
    return provider, grid


@router.post("/viewshed")
def viewshed(req: ViewshedRequest) -> dict:
    try:
        _, grid = _fetch_analysis_grid(req.lon, req.lat, req.radius_m, req.dem_source)
        compute = compute_viewshed if req.engine == "angular-ray-sweep" else compute_viewshed_gdal
        result = compute(grid, req.lon, req.lat, req.refraction, req.radius_m)
    except DEMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    data_uri, bounds = viewshed_to_overlay(result)
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
        res = solve_min_lift(grid, req.obs_lon, req.obs_lat, req.click_lon, req.click_lat,
                             req.refraction)
    except DEMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "feasible": res.feasible,
        "lift_m": res.lift_m,
        "message": None if res.feasible else IMPOSSIBLE_MESSAGE,
        "observer_elev_m": None if math.isnan(res.observer_elev) else res.observer_elev,
        "clicked_elev_m": None if math.isnan(res.clicked_elev) else res.clicked_elev,
        "distance_m": res.distance_m,
        "central_angle_deg": round(res.central_angle_deg, 4),
        "refraction": res.refraction,
    }
