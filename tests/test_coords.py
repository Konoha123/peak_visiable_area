import pytest

from app.geo.coords import parse_coordinates, parse_decimal, parse_dms


class TestDecimal:
    def test_basic(self) -> None:
        r = parse_decimal("116.397, 39.909")
        assert r.ok and r.lon == pytest.approx(116.397) and r.lat == pytest.approx(39.909)

    def test_negative_and_separators(self) -> None:
        for text in ("-73.9857，40.7484", "-73.9857;40.7484", "-73.9857 40.7484"):
            r = parse_decimal(text)
            assert r.ok and r.lon == pytest.approx(-73.9857) and r.lat == pytest.approx(40.7484)

    def test_lon_out_of_range(self) -> None:
        assert not parse_decimal("200.0, 30.0").ok

    def test_lat_out_of_range(self) -> None:
        assert not parse_decimal("116.397, 93.5").ok

    def test_lat_first_is_rejected_by_range(self) -> None:
        assert not parse_decimal("39.909, 116.397").ok

    def test_non_numeric(self) -> None:
        assert not parse_decimal("abc, def").ok

    def test_wrong_token_count(self) -> None:
        assert not parse_decimal("116.397").ok
        assert not parse_decimal("116.397, 39.9, 100.0").ok
        assert not parse_decimal("").ok


class TestDMS:
    def test_basic_with_symbols(self) -> None:
        r = parse_dms('116°23\'50.4"E 39°54\'32.3"N')
        assert r.ok
        assert r.lon == pytest.approx(116 + 23 / 60 + 50.4 / 3600)
        assert r.lat == pytest.approx(39 + 54 / 60 + 32.3 / 3600)

    def test_order_swapped_ok(self) -> None:
        r = parse_dms('39°54\'32.3"N 116°23\'50.4"E')
        assert r.ok and r.lon == pytest.approx(116.397333) and r.lat == pytest.approx(39.908972)

    def test_west_south_negate(self) -> None:
        r = parse_dms('W 73°59\'08.5" N 40°44\'54.4"')
        assert r.ok and r.lon == pytest.approx(-(73 + 59 / 60 + 8.5 / 3600)) and r.lat > 0

    def test_chinese_hemisphere(self) -> None:
        r = parse_dms("东经116°23'50\"E 北纬39°54'32\"N")
        assert r.ok and r.lon == pytest.approx(116.397222) and r.lat == pytest.approx(39.908889)

    def test_degrees_only(self) -> None:
        r = parse_dms("116.5E 39.9N")
        assert r.ok and r.lon == pytest.approx(116.5) and r.lat == pytest.approx(39.9)

    def test_leading_hemisphere(self) -> None:
        r = parse_dms("N 39°54'32\" E 116°23'50\"")
        assert r.ok and r.lat == pytest.approx(39.908889)

    def test_missing_hemisphere(self) -> None:
        assert not parse_dms('116°23\'50"E 39°54\'32"').ok

    def test_duplicate_same_axis(self) -> None:
        assert not parse_dms("116E 117E").ok

    def test_minutes_out_of_range(self) -> None:
        assert not parse_dms("116°61'E 39°54'N").ok

    def test_seconds_out_of_range(self) -> None:
        assert not parse_dms('116°23\'60"E 39°54\'N').ok

    def test_unrecognized_chars(self) -> None:
        assert not parse_dms('116°23\'50"E 39°54\'32"N abc').ok

    def test_conflicting_hemisphere(self) -> None:
        assert not parse_dms('W116°23\'50"E 39°54\'N').ok


def test_dispatch_unknown_format() -> None:
    assert not parse_coordinates("116, 39", "foo").ok


def test_dispatch_decimal_and_dms() -> None:
    assert parse_coordinates("116.397, 39.909", "decimal").ok
    assert parse_coordinates("116.397E 39.909N", "dms").ok
