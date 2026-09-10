"""附近制高点搜索：圆形范围内 DEM 最大高程格网点求解。

制高点定义（交互流程第 10 条）：圆形范围内 DEM 最大高程所在格网单元中心；
不做山头显著性（prominence）过滤；同高平局取距中心最近格点；
中心自身即最高时返回中心原坐标并标记 is_center_highest。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .dem import DEMError, ElevationGrid

M_PER_DEG_LAT = 111_320.0


@dataclass
class PeakCandidate:
    """搜索结果：候选格网单元中心（“中心即最高”时为中心原坐标）。"""

    lon: float
    lat: float
    elevation_m: float
    distance_m: float
    coverage_ratio: float
    is_center_highest: bool


def search_nearby_peak(
    grid: ElevationGrid, lon: float, lat: float, radius_m: float
) -> PeakCandidate:
    """在以 (lon, lat) 为中心、radius_m 为半径的圆形范围内搜索最高高程格网点。

    - 圆内无效值（NaN / nodata 掩膜）格点不参与求最大；
    - 距离按局部等距圆柱近似（半径 ≤ 25 km 误差可忽略）；
    - coverage_ratio = 圆内有效格点数 / 圆面积折算的格点数期望——对离线源
      窗口被裁剪的情形，缺失格点自然计入未覆盖；
    - 圆内无任何有效格点按 DEM 取数失败处理（DEMError）。
    """
    nrows, ncols = grid.shape
    m_per_deg_lon = M_PER_DEG_LAT * math.cos(math.radians(lat))
    cell_lons = grid.lon_min + (np.arange(ncols) + 0.5) * grid.dlon
    cell_lats = grid.lat_max - (np.arange(nrows) + 0.5) * grid.dlat
    dx = (cell_lons[None, :] - lon) * m_per_deg_lon
    dy = (cell_lats[:, None] - lat) * M_PER_DEG_LAT
    dist2 = dx * dx + dy * dy
    in_circle = dist2 <= radius_m * radius_m

    valid = np.isfinite(grid.elevations)
    if grid.nodata_mask is not None:
        valid &= ~grid.nodata_mask
    cand_mask = in_circle & valid
    if not cand_mask.any():
        raise DEMError("搜索范围内无有效 DEM 高程数据")

    cell_area = grid.dlon * m_per_deg_lon * grid.dlat * M_PER_DEG_LAT
    expected_n = math.pi * radius_m * radius_m / max(cell_area, 1e-9)
    coverage_ratio = min(1.0, float(cand_mask.sum()) / max(expected_n, 1.0))

    max_elev = float(grid.elevations[cand_mask].max())
    tied = cand_mask & (grid.elevations == max_elev)
    row, col = np.unravel_index(np.argmin(np.where(tied, dist2, np.inf)), dist2.shape)

    # 中心所在格网单元即最高单元 → “当前点已是附近制高点”
    center_col = math.floor((lon - grid.lon_min) / grid.dlon)
    center_row = math.floor((grid.lat_max - lat) / grid.dlat)
    if (row, col) == (center_row, center_col):
        return PeakCandidate(lon, lat, max_elev, 0.0, coverage_ratio, True)

    return PeakCandidate(
        lon=float(grid.lon_min + (col + 0.5) * grid.dlon),
        lat=float(grid.lat_max - (row + 0.5) * grid.dlat),
        elevation_m=max_elev,
        distance_m=math.sqrt(float(dist2[row, col])),
        coverage_ratio=coverage_ratio,
        is_center_highest=False,
    )
