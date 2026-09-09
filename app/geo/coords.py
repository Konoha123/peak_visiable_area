"""坐标输入解析：支持十进制度与度分秒（DMS）两种格式，输出严格校验结果。

约定：十进制度格式为“经度, 纬度”两个数值；DMS 格式须包含经度、纬度各一个分量，
每个分量带方位字母（N/S/E/W，支持中文 北/南/东/西），先后顺序不限（依方位字母区分）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEMISPHERE_MAP = {
    "n": "N", "北": "N",
    "s": "S", "南": "S",
    "e": "E", "东": "E",
    "w": "W", "西": "W",
}

_LON_HEMIS = ("E", "W")
_LAT_HEMIS = ("N", "S")

_DMS_COMPONENT_RE = re.compile(
    r"""(?P<h1>(?:[NSEW]|北|南|东|西)(?:经|纬)?)?\s*
        (?P<deg>\d+(?:\.\d+)?)\s*(?:°|º|度|[dD])?\s*
        (?:(?P<min>\d+(?:\.\d+)?)\s*(?:'|′|’|分|[mM])\s*
            (?:(?P<sec>\d+(?:\.\d+)?)\s*(?:"|″|”|秒|[sS]))?
        )?(?P<h2>[NSEW]|北|南|东|西)?""",
    re.VERBOSE | re.IGNORECASE,
)

_DMS_EXAMPLE = "例如 116°23'50.4\"E 39°54'32.3\"N"


@dataclass
class ParseResult:
    ok: bool
    lon: float | None = None
    lat: float | None = None
    message: str = ""


def _err(message: str) -> ParseResult:
    return ParseResult(ok=False, message=message)


def parse_decimal(text: str) -> ParseResult:
    parts = [p for p in re.split(r"[,;，；\s]+", text.strip()) if p]
    if len(parts) != 2:
        return _err("十进制度格式应为两个数值：经度, 纬度（例如 116.397, 39.909）")
    try:
        lon_raw, lat_raw = float(parts[0]), float(parts[1])
    except ValueError:
        return _err("存在无法识别为数值的内容，请检查输入")
    if not -180.0 <= lon_raw <= 180.0:
        return _err(f"经度 {lon_raw} 超出范围 [-180, 180]")
    if not -90.0 <= lat_raw <= 90.0:
        return _err(f"纬度 {lat_raw} 超出范围 [-90, 90]")
    return ParseResult(ok=True, lon=lon_raw, lat=lat_raw, message="校验成功")


def _match_component(match: re.Match) -> tuple[str, float] | None:
    """从单个 DMS 匹配中提取方位与十进度数；非法时返回 None。"""

    def _hemi(raw: str | None) -> str | None:
        if not raw:
            return None
        return HEMISPHERE_MAP.get(raw.rstrip("经纬").lower())

    h1, h2 = _hemi(match.group("h1")), _hemi(match.group("h2"))
    if h1 and h2 and h1 != h2:
        return None
    hemi = h1 or h2
    if not hemi:
        return None
    deg = float(match.group("deg"))
    minutes = float(match.group("min")) if match.group("min") else 0.0
    seconds = float(match.group("sec")) if match.group("sec") else 0.0
    if not 0 <= minutes < 60 or not 0 <= seconds < 60:
        return None
    value = deg + minutes / 60.0 + seconds / 3600.0
    if hemi in ("S", "W"):
        value = -value
    return hemi, value


def parse_dms(text: str) -> ParseResult:
    matches = list(_DMS_COMPONENT_RE.finditer(text))
    covered = re.sub(r"\s+", "", "".join(m.group(0) for m in matches))
    if covered != re.sub(r"\s+", "", text):
        return _err(f"输入包含无法识别的字符，请检查 DMS 格式（{_DMS_EXAMPLE}）")
    if len(matches) != 2:
        return _err("DMS 格式应为经度、纬度两个分量，且各自带方位字母（N/S/E/W）")

    parsed = [_match_component(m) for m in matches]
    if any(p is None for p in parsed):
        msg = f"DMS 分量非法：需要方位字母（N/S/E/W），分、秒须在 [0, 60) 内（{_DMS_EXAMPLE}）"
        return _err(msg)

    lon_vals = [v for h, v in parsed if h in _LON_HEMIS]  # type: ignore[union-attr]
    lat_vals = [v for h, v in parsed if h in _LAT_HEMIS]  # type: ignore[union-attr]
    if len(lon_vals) != 1 or len(lat_vals) != 1:
        return _err("需要经度（E/W）与纬度（N/S）分量各一个")
    lon, lat = lon_vals[0], lat_vals[0]
    if not -180.0 <= lon <= 180.0:
        return _err(f"经度 {lon} 超出范围 [-180, 180]")
    if not -90.0 <= lat <= 90.0:
        return _err(f"纬度 {lat} 超出范围 [-90, 90]")
    return ParseResult(ok=True, lon=lon, lat=lat, message="校验成功")


def parse_coordinates(text: str, fmt: str) -> ParseResult:
    if fmt == "decimal":
        return parse_decimal(text)
    if fmt == "dms":
        return parse_dms(text)
    return _err(f"未知坐标格式：{fmt}")
