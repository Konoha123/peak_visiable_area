"""可视域结果的 PNG 渲染（半透明叠加图层，供 Leaflet ImageOverlay 使用）。"""

from __future__ import annotations

import base64
import io

import numpy as np
from PIL import Image

from .geo.viewshed import ViewshedResult

MAX_IMAGE_PX = 2048
VISIBLE_COLOR = (230, 126, 34)  # 半透明橙色填充
VISIBLE_ALPHA = 120


def viewshed_to_overlay(result: ViewshedResult) -> tuple[str, tuple[float, float, float, float]]:
    """渲染可视域为 RGBA PNG，返回 (data URI, 边界 [west, south, east, north])。"""
    vis = result.visibility
    nrows, ncols = vis.shape
    scale = 1
    if max(nrows, ncols) > MAX_IMAGE_PX:
        scale = int(np.ceil(max(nrows, ncols) / MAX_IMAGE_PX))
    kept_rows = nrows // scale * scale
    kept_cols = ncols // scale * scale
    vis_s = vis[:kept_rows:scale, :kept_cols:scale]

    arr = np.zeros((kept_rows // scale, kept_cols // scale, 4), dtype=np.uint8)
    arr[vis_s] = (*VISIBLE_COLOR, VISIBLE_ALPHA)
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGBA").save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    g = result.grid
    west = g.lon_min
    east = g.lon_min + g.dlon * kept_cols
    north = g.lat_max
    south = g.lat_max - g.dlat * kept_rows
    return data_uri, (west, south, east, north)
