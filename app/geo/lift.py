"""最小抬升高度求解器（点选测高）。

沿两点测地线按 DEM 分辨率采样地形剖面，一维求根求解点击点所需的最小抬升高度：
对每个中间障碍，视线约束为 e_i + drop_i ≤ e1 + (e2 + x - e1)·(d_i/D)，
其中 drop_i = d_i(D-d_i)/(2R_eff)（有效地球半径，与折射设置联动）。
几何不可行（两点地心角 ≥ 90°，视线恒穿过地球体）返回 feasible=False（兜底字符串场景）。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np

from .dem import DEMError, ElevationGrid
from .horizon import EARTH_RADIUS_M, effective_earth_radius_m

logger = logging.getLogger(__name__)

IMPOSSIBLE_MESSAGE = "无法通过抬升实现通视（地球几何约束）"


@dataclass
class LiftResult:
    feasible: bool
    lift_m: float | None
    observer_elev: float
    clicked_elev: float
    distance_m: float
    central_angle_deg: float
    spacing_m: float
    refraction: str
    # 剖面采样（d, elev），与测高求解所用同一份；DEM 不可覆盖时为 None
    profile: tuple[np.ndarray, np.ndarray] | None = None
    # 测地线顶点 (n, 2)= [lon, lat]，与剖面采样同一组插值点；纯几何、总可产出
    geodesic: np.ndarray | None = None


def _central_angle_rad(lon0: float, lat0: float, lon1: float, lat1: float) -> float:
    la0, la1 = math.radians(lat0), math.radians(lat1)
    dlon = math.radians(lon1 - lon0)
    x = math.sin(la0) * math.sin(la1) + math.cos(la0) * math.cos(la1) * math.cos(dlon)
    return math.acos(min(1.0, max(-1.0, x)))


def _solve_from_profile(d: np.ndarray, elev: np.ndarray, r_eff: float) -> float:
    """由无缺失剖面（首端=观测点，末端=点击点）求最小抬升高度。"""
    e1, e2 = float(elev[0]), float(elev[-1])
    total = float(d[-1])
    if total <= 0.0:
        return 0.0
    lift = 0.0
    for di, ei in zip(d[1:-1], elev[1:-1], strict=True):
        if di <= 0.0 or di >= total:
            continue
        drop = di * (total - di) / (2.0 * r_eff)
        los0 = e1 + (e2 - e1) * (di / total)
        excess = float(ei) + drop - los0
        if excess > 0.0:
            lift = max(lift, excess * total / di)
    return lift


def solve_min_lift(
    grid: ElevationGrid,
    obs_lon: float,
    obs_lat: float,
    click_lon: float,
    click_lat: float,
    refraction: str,
) -> LiftResult:
    omega = _central_angle_rad(obs_lon, obs_lat, click_lon, click_lat)
    if omega >= math.pi / 2 - 1e-12:
        logger.info("测高: 地心角=%.2f° ≥ 90°，几何不可行（兜底字符串）",
                    math.degrees(omega))
        # 测地线为纯几何量、仍可产出（地图连线照常渲染）；剖面仅在 DEM 可覆盖时附带
        spacing = grid.cell_size_m(0.5 * (obs_lat + click_lat))
        d, elev, lons, lats = grid.sample_geodesic_profile(
            obs_lon, obs_lat, click_lon, click_lat, spacing)
        geodesic = np.column_stack((lons, lats))
        covered = not (math.isnan(float(elev[0])) or math.isnan(float(elev[-1]))
                       or bool(np.isnan(elev[1:-1]).any()))
        return LiftResult(
            feasible=False, lift_m=None,
            observer_elev=float("nan"), clicked_elev=float("nan"),
            distance_m=omega * EARTH_RADIUS_M,
            central_angle_deg=math.degrees(omega),
            spacing_m=float(spacing), refraction=refraction,
            profile=(d, elev) if covered else None,
            geodesic=geodesic,
        )

    r_eff = effective_earth_radius_m(refraction)
    spacing = grid.cell_size_m(0.5 * (obs_lat + click_lat))
    d, elev, lons, lats = grid.sample_geodesic_profile(
        obs_lon, obs_lat, click_lon, click_lat, spacing)
    if math.isnan(float(elev[0])) or math.isnan(float(elev[-1])):
        raise DEMError("观测点或点击点落在 DEM 覆盖范围/无数据区域之外")
    if np.isnan(elev[1:-1]).any():
        raise DEMError("两点间地形剖面超出 DEM 覆盖范围，无法计算")

    lift = _solve_from_profile(np.asarray(d, dtype=np.float64),
                               np.asarray(elev, dtype=np.float64), r_eff)
    logger.info(
        "测高: 距离=%.1fm 采样=%d 点(步长 %.0fm) 折射=%s → 可行=%s 抬升=%.1fm "
        "(观测点高程=%.1fm 点击点高程=%.1fm)",
        float(d[-1]), len(d), spacing, refraction, True, lift,
        float(elev[0]), float(elev[-1]),
    )
    return LiftResult(
        feasible=True, lift_m=lift,
        observer_elev=float(elev[0]), clicked_elev=float(elev[-1]),
        distance_m=float(d[-1]),
        central_angle_deg=math.degrees(omega),
        spacing_m=float(spacing), refraction=refraction,
        profile=(d, elev),
        geodesic=np.column_stack((lons, lats)),
    )
