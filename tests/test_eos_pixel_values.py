from app.services.eos import service
from app.services.eos.visualization import classify_index_value, legend_for_index, render_params_for_index


PARCEL = {
    "type": "Polygon",
    "coordinates": [[[-90.0, 14.0], [-89.9, 14.0], [-89.9, 14.1], [-90.0, 14.1], [-90.0, 14.0]]],
}


def test_render_palette_and_legend_share_absolute_thresholds():
    legend = legend_for_index("NDVI")
    params = render_params_for_index("NDVI")

    assert legend is not None
    assert params["COLORS"].split(",") == [color.removeprefix("#") for color in legend["colors"]]
    assert [float(value) for value in params["THRESHOLDS"].split(",")] == legend["thresholds"]
    assert len(legend["colors"]) == len(legend["thresholds"]) + 1
    assert legend["min"] == -1.0
    assert legend["max"] == 1.0


def test_point_value_uses_eos_value_and_matching_classification(monkeypatch):
    service._cached_point_value.cache_clear()
    monkeypatch.setattr(service.client, "fetch_point_value", lambda *_args, **_kwargs: 0.63781234)

    result = service.point_value(
        PARCEL,
        index="NDVI",
        lat=14.05,
        lon=-89.95,
        view_ids=["S2/15/P/XS/2026/7/19/0"],
    )

    assert result["available"] is True
    assert result["value"] == 0.63781234
    assert result["provider"] == "EOSDA Point Value API"
    assert result["date"] == "2026-07-19"
    assert result["label"] == "Vegetación alta"
    assert result["range"] == {"min": 0.6, "max": 0.8}
    assert result["color"] == classify_index_value("NDVI", result["value"])["color"]


def test_point_value_rejects_coordinates_outside_owned_parcel(monkeypatch):
    service._cached_point_value.cache_clear()
    called = False

    def fake_point(*_args, **_kwargs):
        nonlocal called
        called = True
        return 0.5

    monkeypatch.setattr(service.client, "fetch_point_value", fake_point)
    result = service.point_value(
        PARCEL,
        index="NDVI",
        lat=15.0,
        lon=-89.95,
        view_ids=["S2/15/P/XS/2026/7/19/0"],
    )

    assert result["available"] is False
    assert "fuera del lote" in result["reason"]
    assert called is False
