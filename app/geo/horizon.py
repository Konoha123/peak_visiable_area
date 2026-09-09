"""地平线（曲率上限）范围计算。

观测有效高度取观测点 DEM 高程，地平线半径 d = sqrt(2 * R_eff * h)；
折射模型经设置项联动：纯几何 R 或标准大气折射有效半径 ≈ 7/6 R。
半径过小时兜底为预设最小显示半径（与地图最小显示范围共用）。
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0

REFRACTION_GEOMETRIC = "geometric"
REFRACTION_STANDARD = "standard"

REFRACTION_FACTORS: dict[str, float] = {
    REFRACTION_GEOMETRIC: 1.0,
    REFRACTION_STANDARD: 7.0 / 6.0,
}

MIN_DISPLAY_RADIUS_M = 5000.0


def effective_earth_radius_m(refraction: str) -> float:
    try:
        return EARTH_RADIUS_M * REFRACTION_FACTORS[refraction]
    except KeyError:
        raise ValueError(f"未知折射模型：{refraction}") from None


def horizon_radius_m(elevation_m: float, refraction: str) -> float:
    """由观测点绝对高程计算地平线圆半径（米）；高程非正时返回 0。"""
    if elevation_m <= 0.0:
        return 0.0
    return math.sqrt(2.0 * effective_earth_radius_m(refraction) * elevation_m)


def display_radius_m(elevation_m: float, refraction: str) -> tuple[float, bool]:
    """返回（显示半径, 是否触发最小显示半径兜底）。"""
    radius = horizon_radius_m(elevation_m, refraction)
    if radius < MIN_DISPLAY_RADIUS_M:
        return MIN_DISPLAY_RADIUS_M, True
    return radius, False
