import uuid

from app.services.parcel_split_migration import split_multi_feature_parcels

USER_ID = "9833ab3b-0000-0000-0000-000000000001"
OTHER_USER_ID = "9833ab3b-0000-0000-0000-000000000002"
TS = "2026-07-10T00:00:00+00:00"


def _square(lng: float, lat: float, size: float = 0.001):
    return [[
        [lng, lat],
        [lng + size, lat],
        [lng + size, lat + size],
        [lng, lat + size],
        [lng, lat],
    ]]


def _feature(name: str, finca: str, lng: float, lat: float):
    return {
        "type": "Feature",
        "properties": {"Name": name, "FINCA": finca},
        "geometry": {"type": "Polygon", "coordinates": _square(lng, lat)},
    }


def _multi_feature_parcel(parcel_id: str, features):
    return {
        "id": parcel_id,
        "user_id": USER_ID,
        "name": "LOTES SALGADO",
        "area": 253.4843,
        "geometry": {"type": "FeatureCollection", "features": features},
        "geometry_source_crs": "EPSG:32615",
        "file_url": "/api/compat/storage/public/parcels/u/lotes.zip",
        "created_at": "2026-06-16T00:46:45+00:00",
        "updated_at": "2026-06-16T00:46:45+00:00",
    }


def _db(parcels, satellite_images=None, satellite_jobs=None):
    return {
        "users": [],
        "tables": {
            "parcels": parcels,
            "satellite_images": satellite_images or [],
            "satellite_jobs": satellite_jobs or [],
        },
    }


def test_multi_feature_parcel_splits_into_individual_rows():
    parent_id = str(uuid.uuid4())
    db = _db([_multi_feature_parcel(parent_id, [
        _feature("A1", "Cañones", -96.21, 18.59),
        _feature("A2", "", -96.22, 18.59),
        _feature("A3", "", -96.23, 18.59),
    ])])

    assert split_multi_feature_parcels(db, timestamp=TS) is True

    rows = db["tables"]["parcels"]
    assert len(rows) == 3
    assert {row["name"] for row in rows} == {"A1", "A2", "A3"}
    assert all(row["user_id"] == USER_ID for row in rows)
    assert all(row["geometry"]["type"] == "FeatureCollection" for row in rows)
    assert all(len(row["geometry"]["features"]) == 1 for row in rows)
    assert all(row["area"] and row["area"] > 0 for row in rows)
    assert all(row["created_at"] == "2026-06-16T00:46:45+00:00" for row in rows)
    assert all(row["updated_at"] == TS for row in rows)
    # La finca del atributo del shapefile gana; sin atributo hereda el nombre del lote.
    by_name = {row["name"]: row for row in rows}
    assert by_name["A1"]["finca"] == "Cañones"
    assert by_name["A2"]["finca"] == "LOTES SALGADO"


def test_split_ids_are_deterministic():
    parent_id = "47330439-f2d4-45f5-b288-8975e698f34d"
    features = [_feature("A1", "", -96.21, 18.59), _feature("A2", "", -96.22, 18.59)]
    db_a = _db([_multi_feature_parcel(parent_id, features)])
    db_b = _db([_multi_feature_parcel(parent_id, features)])

    split_multi_feature_parcels(db_a, timestamp=TS)
    split_multi_feature_parcels(db_b, timestamp="2026-07-11T00:00:00+00:00")

    ids_a = [row["id"] for row in db_a["tables"]["parcels"]]
    ids_b = [row["id"] for row in db_b["tables"]["parcels"]]
    assert ids_a == ids_b


def test_duplicate_feature_names_get_unique_labels():
    # dedupe_user_parcels colapsa filas con el mismo name por usuario: si dos
    # parcelas quedaran como "A1", una desaparecería de las respuestas del API.
    parent_id = str(uuid.uuid4())
    db = _db([_multi_feature_parcel(parent_id, [
        _feature("A1", "Cañones", -96.21, 18.59),
        _feature("A1", "", -96.22, 18.59),
        _feature("A1", "", -96.23, 18.59),
    ])])

    split_multi_feature_parcels(db, timestamp=TS)

    names = sorted(row["name"] for row in db["tables"]["parcels"])
    assert len(set(names)) == 3
    assert "A1" in names


def test_label_collision_with_existing_user_parcel_is_disambiguated():
    parent_id = str(uuid.uuid4())
    existing = {
        "id": str(uuid.uuid4()),
        "user_id": USER_ID,
        "name": "A1",
        "geometry": {"type": "FeatureCollection", "features": [_feature("A1", "", -96.5, 18.5)]},
    }
    db = _db([existing, _multi_feature_parcel(parent_id, [
        _feature("A1", "", -96.21, 18.59),
        _feature("A2", "", -96.22, 18.59),
    ])])

    split_multi_feature_parcels(db, timestamp=TS)

    names = [row["name"] for row in db["tables"]["parcels"]]
    assert len(names) == len(set(names)) == 3


def test_single_feature_and_plain_geometry_parcels_are_untouched():
    single = {
        "id": str(uuid.uuid4()),
        "user_id": USER_ID,
        "name": "Lote F2",
        "geometry": {"type": "FeatureCollection", "features": [_feature("F2", "", -96.3, 18.6)]},
    }
    polygon = {
        "id": str(uuid.uuid4()),
        "user_id": OTHER_USER_ID,
        "name": "Lote manual",
        "geometry": {"type": "Polygon", "coordinates": _square(-96.4, 18.6)},
    }
    db = _db([single, polygon])

    assert split_multi_feature_parcels(db, timestamp=TS) is False
    assert db["tables"]["parcels"] == [single, polygon]


def test_second_run_is_noop():
    parent_id = str(uuid.uuid4())
    db = _db([_multi_feature_parcel(parent_id, [
        _feature("A1", "", -96.21, 18.59),
        _feature("A2", "", -96.22, 18.59),
    ])])

    assert split_multi_feature_parcels(db, timestamp=TS) is True
    rows_after_first = [dict(row) for row in db["tables"]["parcels"]]
    assert split_multi_feature_parcels(db, timestamp="2026-07-11T00:00:00+00:00") is False
    assert db["tables"]["parcels"] == rows_after_first


def test_stale_satellite_rows_of_split_parent_are_removed():
    parent_id = str(uuid.uuid4())
    other_parcel_id = str(uuid.uuid4())
    stale_image = {"id": "img-1", "parcel_id": parent_id}
    kept_image = {"id": "img-2", "parcel_id": other_parcel_id}
    stale_job = {"id": "job-1", "parcel_id": parent_id}
    db = _db(
        [_multi_feature_parcel(parent_id, [
            _feature("A1", "", -96.21, 18.59),
            _feature("A2", "", -96.22, 18.59),
        ])],
        satellite_images=[stale_image, kept_image],
        satellite_jobs=[stale_job],
    )

    split_multi_feature_parcels(db, timestamp=TS)

    assert db["tables"]["satellite_images"] == [kept_image]
    assert db["tables"]["satellite_jobs"] == []
