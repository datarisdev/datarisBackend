"""Bitácora de campo alimentada desde AgtechApps.

Cubre las dos mitades:

* la **traducción** de una respuesta de DigiForms a una labor calculable
  (categorías, números tecleados en campo, clave de ciclo), y
* el **cálculo**, verificado contra los números reales de la hoja del CDT:
  una inversión de 14 156 $/ha con 4.5 t/ha a 4 450 $/t da 5 869 $/ha de
  utilidad, y la fila de 5.0 t/ha de la matriz de sensibilidad da 8 094.

Los cálculos no se reimplementan: se comprueba que el conector entrega a
`app.modules.field_log.kpi` exactamente lo que esas funciones esperan, porque
ahí es donde se rompería la cadena sin que nadie lo note.
"""

from __future__ import annotations

import os
import tempfile

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-bitacora-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers.compat_sig import (  # noqa: E402
    _match_parcel_by_name,
    _parcel_name_index,
    _persist_field_log_records,
)
from app.services.digiforms_field_log import (  # noqa: E402
    FIELD_LOG_CYCLE_FORM_TYPE,
    FIELD_LOG_ENTRY_FORM_TYPE,
    FIELD_LOG_PHENOLOGY_FORM_TYPE,
    cycle_key,
    cycle_sheet_from_row,
    entry_from_row,
    group_by_cycle,
    normalize_category,
    phenology_from_row,
    to_number,
)

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}

CICLO = {"Ciclo": "PV 2026", "Validacion": "V-01", "Parcela": "Lote 3", "Sector": "Norte"}


def _labor(categoria: str, concepto: str, cantidad, costo, **extra) -> dict:
    """Una respuesta de AgtechApps del formulario de labores."""
    return {**CICLO, "Categoria": categoria, "Concepto": concepto, "Cantidad": cantidad,
            "CostoUnitario": costo, "EmpresaSolicitante": "CDT", **extra}


# ── Traducción ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "valor, esperado",
    [
        # Las etiquetas exactas del desplegable de AgtechApps.
        ("1. Acondicionamiento", "acondicionamiento"),
        ("3. Riegos", "riego"),
        ("10. Cosecha", "cosecha"),
        # Variantes de quien escribe a mano o cambia el desplegable.
        ("Manejo de plagas", "plagas"),
        ("  fertilizacion  ", "fertilizante"),
        ("8", "foliar"),
        ("Aplicación foliar", "foliar"),
        # Lo que no es una categoría no se convierte en una cualquiera.
        ("", None),
        ("no aplica", None),
    ],
)
def test_la_categoria_se_reconoce_por_numero_etiqueta_o_sinonimo(valor, esperado):
    assert normalize_category(valor) == esperado


@pytest.mark.parametrize(
    "valor, esperado",
    [
        ("1,250.50", 1250.50),   # miles con coma, decimal con punto
        ("1.250,50", 1250.50),   # al revés, como se escribe en México
        ("12 ha", 12.0),         # unidad pegada al número
        ("$ 4,450", 4450.0),
        ("0", 0.0),              # cero capturado no es "sin capturar"
        ("", None),
        ("  ", None),
        ("s/d", None),
    ],
)
def test_los_numeros_tecleados_en_campo_se_entienden(valor, esperado):
    assert to_number(valor) == esperado


def test_la_labor_llega_con_su_costo_ya_calculado():
    """`costo_ha = cantidad × costo unitario` es la columna G de la hoja."""
    entry = entry_from_row(_labor("4. Fertilizante", "Urea", "250", "12.50", UnidadesN="115"))
    assert entry["categoria"] == "fertilizante"
    assert entry["costo_ha"] == pytest.approx(3125.0)
    assert entry["data"]["n_units"] == pytest.approx(115.0)
    assert entry["cycle_key"] == cycle_key(CICLO)


def test_los_tres_formularios_comparten_la_clave_de_ciclo():
    """Labor, ficha y fenología se agrupan aunque cambie la forma de escribir."""
    labor = entry_from_row(_labor("2. Siembra", "Siembra", "1", "3000"))
    ficha = cycle_sheet_from_row({**CICLO, "Ciclo": "pv 2026", "Superficie": "5"})
    feno = phenology_from_row({**CICLO, "Validacion": " V-01 ", "Etapa": "V6"})
    assert labor["cycle_key"] == ficha["cycle_key"] == feno["cycle_key"]


# ── Cálculo ───────────────────────────────────────────────────────────────────
# Reparto de la inversión de la corrida real de la hoja: 14 156 $/ha en total.
LABORES_DEL_CICLO = [
    _labor("1. Acondicionamiento", "Subsoleo", "1", "2000", LitrosDiesel="45"),
    _labor("2. Siembra", "Siembra mecanizada", "1", "1956", LitrosDiesel="18"),
    _labor("3. Riegos", "Riego rodado", "4", "300", KwhRiego="620", M3Riego="4200"),
    _labor("4. Fertilizante", "Urea", "250", "12", UnidadesN="115", UnidadesP="0", UnidadesK="0"),
    _labor("5. Manejo de malezas", "Herbicida preemergente", "1", "1200", GramosIA="960"),
    _labor("6. Manejo de plagas", "Insecticida", "1", "900", GramosIA="240"),
    _labor("7. Manejo de enfermedades", "Fungicida", "1", "600", GramosIA="150"),
    _labor("8. Aplicaciones foliares", "Foliar 1", "1", "700", Aplicacion="1"),
    _labor("9. Diversos", "Vigilancia", "1", "800", ),
    _labor("10. Cosecha", "Trilla", "1", "1800", Rendimiento="4.5"),
]

FICHA = {**CICLO, "Superficie": "5", "Cultivo": "Maiz", "Hibrido": "P-4082",
         "TipoLabranza": "Conservacion", "Rendimiento": "4.5", "PrecioVenta": "4450"}


def _informe():
    entries = [entry_from_row(row) for row in LABORES_DEL_CICLO]
    sheets = [cycle_sheet_from_row(FICHA)]
    phenology = [phenology_from_row({**CICLO, "Etapa": "V6", "Fecha": "2026-06-10"})]
    reports = group_by_cycle(entries=entries, sheets=sheets, phenology=phenology)
    assert len(reports) == 1
    return reports[0]


def test_la_cabecera_economica_reproduce_la_hoja():
    """F5, H5, J5, K5, E7 y K7 del formato original."""
    economics = _informe()["kpis"]["economics"]
    assert economics["investment_per_ha"] == pytest.approx(14156.0)
    assert economics["revenue_per_ha"] == pytest.approx(20025.0)      # 4.5 × 4450
    assert economics["profit_per_ha"] == pytest.approx(5869.0)        # el número del Excel
    assert economics["benefit_cost_ratio"] == pytest.approx(20025 / 14156)
    assert economics["break_even_ton_ha"] == pytest.approx(14156 / 4450)
    assert economics["unit_cost_per_ton"] == pytest.approx(14156 / 4.5)


def test_los_indicadores_tecnicos_salen_de_los_campos_del_formulario():
    """Los `RESULTADOS TECNICOS` (F10 a F16) sin que nadie los teclee."""
    tecnicos = _informe()["kpis"]["sustainability"]
    assert tecnicos["water_m3_per_ha"] == pytest.approx(4200.0)
    assert tecnicos["water_footprint_m3_per_ton"] == pytest.approx(4200 / 4.5)
    assert tecnicos["energy_kwh_per_ton"] == pytest.approx(620 / 4.5)
    assert tecnicos["diesel_l_per_ha"] == pytest.approx(63.0)         # 45 + 18
    assert tecnicos["herbicide_ia_g_per_ton"] == pytest.approx(960 / 4.5)
    assert tecnicos["insecticide_ia_g_per_ton"] == pytest.approx(240 / 4.5)
    assert tecnicos["fungicide_ia_g_per_ton"] == pytest.approx(150 / 4.5)
    assert tecnicos["nitrogen_kg_per_ton"] == pytest.approx(115 / 4.5)


def test_el_resumen_de_costos_reparte_el_cien_por_ciento():
    """B110:F122, el bloque `RESUMEN DE COSTOS` con su porcentaje por bloque."""
    costs = _informe()["kpis"]["costs"]
    assert costs["total_cost_per_ha"] == pytest.approx(14156.0)
    porcentajes = {item["category"]: item["percentage"] for item in costs["categories"]}
    assert porcentajes["fertilizante"] == pytest.approx(100 * 3000 / 14156)
    assert sum(item["percentage"] for item in costs["categories"]) == pytest.approx(100.0)


def test_los_pies_de_bloque_cierran_cada_seccion():
    """D29, D50/F50, D58/59/60, D70/82/88: las sumas al pie de cada bloque."""
    blocks = _informe()["kpis"]["blocks"]
    assert blocks["riego"]["kwh_per_ha"] == pytest.approx(620.0)
    assert blocks["riego"]["m3_per_ha"] == pytest.approx(4200.0)
    assert blocks["fertilizante"]["n_units"] == pytest.approx(115.0)
    assert blocks["malezas"]["ia_grams"] == pytest.approx(960.0)
    assert blocks["cosecha"]["rendimiento_ton_ha"] == pytest.approx(4.5)


def test_la_matriz_de_sensibilidad_da_los_valores_de_la_hoja():
    """La celda de 5.0 t/ha a 4 450 $/t vale 8 094 en el Excel original."""
    matriz = _informe()["sensitivity"]
    fila = next(row for row in matriz["rows"] if row["yield_ton_ha"] == pytest.approx(5.0))
    celda = next(c for c in fila["cells"] if c["price_per_ton"] == pytest.approx(4450.0))
    assert celda["profit_per_ha"] == pytest.approx(8094.0)
    # El escenario actual queda señalado para leer el margen de un vistazo.
    actual = [c for row in matriz["rows"] for c in row["cells"] if c["is_current"]]
    assert len(actual) == 1
    assert actual[0]["profit_per_ha"] == pytest.approx(5869.0)


def test_una_labor_sin_categoria_reconocida_no_ensucia_el_resumen():
    """Se conserva y se señala, pero no se suma a un bloque que no le toca."""
    entries = [entry_from_row(row) for row in LABORES_DEL_CICLO]
    entries.append(entry_from_row(_labor("Trámite bancario", "Gestión", "1", "5000")))
    report = group_by_cycle(entries=entries, sheets=[cycle_sheet_from_row(FICHA)], phenology=[])[0]
    assert report["kpis"]["economics"]["investment_per_ha"] == pytest.approx(14156.0)
    assert [item["concepto"] for item in report["unclassified_entries"]] == ["Gestión"]
    assert report["entry_count"] == 11


def test_sin_ficha_de_ciclo_el_rendimiento_se_reconstruye_de_la_cosecha():
    """El técnico todavía no cerró el ciclo: los indicadores no se quedan vacíos."""
    entries = [entry_from_row(row) for row in LABORES_DEL_CICLO]
    report = group_by_cycle(entries=entries, sheets=[], phenology=[])[0]
    assert report["has_cycle_sheet"] is False
    assert report["kpis"]["economics"]["yield_ton_ha"] == pytest.approx(4.5)
    # Sin precio no hay ingreso, y eso es un hueco, no un cero.
    assert report["kpis"]["economics"]["revenue_per_ha"] is None
    assert report["kpis"]["sustainability"]["water_footprint_m3_per_ton"] == pytest.approx(4200 / 4.5)


# ── Cruce con las parcelas ────────────────────────────────────────────────────
PARCELAS = [
    {"id": "p-1", "name": "0311 (NUEVA LINDA (PILAR))"},
    {"id": "p-2", "name": "0312 (NUEVA LINDA (PILAR))"},
    {"id": "p-3", "name": "Lote 3"},
]


def test_el_lote_se_reconoce_por_su_codigo_o_su_nombre():
    index = _parcel_name_index(PARCELAS)
    assert _match_parcel_by_name("0311", index) == "p-1"
    assert _match_parcel_by_name("Lote 3", index) == "p-3"
    assert _match_parcel_by_name("  lote 3 ", index) == "p-3"


def test_un_nombre_ambiguo_no_se_asigna_a_nadie():
    """Antes que colgar la labor del lote equivocado, se deja sin lote."""
    index = _parcel_name_index(PARCELAS)
    assert _match_parcel_by_name("NUEVA LINDA (PILAR)", index) is None
    assert _match_parcel_by_name("", index) is None


# ── Aterrizaje y API ──────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def client() -> TestClient:
    # Sin gestor de contexto: entrar en él arranca la app y abre SQLAlchemy
    # contra Postgres, que el sistema compat no necesita.
    return TestClient(app)


def _token(client: TestClient) -> str:
    response = client.post("/api/compat/auth/sign-in", json=SUPERADMIN)
    assert response.status_code == 200, response.text
    return response.json()["data"]["session"]["access_token"]


@pytest.fixture(scope="module")
def token(client: TestClient) -> str:
    return _token(client)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def empresa_conectada(client: TestClient, token: str) -> str:
    response = client.put(
        "/api/compat/extensions/digiforms/company-config",
        headers=_auth(token),
        json={"client_id": "180", "api_user": "admin", "api_password": "secreto-de-prueba"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["connection"]["company_id"]


def test_los_tres_formularios_se_enlazan_de_una_vez(client, token, empresa_conectada):
    response = client.post(
        "/api/compat/extensions/digiforms/field-log-links",
        headers=_auth(token),
        json={"entry_form_id": "2534", "cycle_form_id": "2533", "phenology_form_id": "2532"},
    )
    assert response.status_code == 200, response.text

    listing = client.get("/api/compat/extensions/digiforms/field-log-links", headers=_auth(token))
    links = {item["form_type"]: item for item in listing.json()["data"]["links"]}
    assert links[FIELD_LOG_ENTRY_FORM_TYPE]["form_id"] == "2534"
    assert links[FIELD_LOG_CYCLE_FORM_TYPE]["form_id"] == "2533"
    assert links[FIELD_LOG_PHENOLOGY_FORM_TYPE]["form_id"] == "2532"
    assert all(item["is_enabled"] for item in links.values())


def _sincroniza(form_type: str, form_id: str, rows: list, company_id: str, user_id: str, parcelas=()):
    from app.api.routers import compat

    with compat.LOCK:
        db = compat.read_db()
        run = _persist_field_log_records(
            db=db,
            user_id=user_id,
            company_id=company_id,
            form_type=form_type,
            form_id=form_id,
            response_rows=rows,
            image_rows=[],
            parcels=list(parcelas),
            cursor_before=0,
            sync_mode="initial_date_range",
        )
        compat.write_db(db)
    return run


def test_una_respuesta_sincronizada_se_ve_calculada_y_en_el_mapa(client, token, empresa_conectada):
    """El recorrido completo: se llena el formulario y sale en el mapa y en los KPIs."""
    perfil = client.get("/api/compat/auth/user", headers=_auth(token))
    user_id = perfil.json()["data"]["user"]["id"] if perfil.status_code == 200 else None
    if not user_id:
        user_id = client.post("/api/compat/auth/sign-in", json=SUPERADMIN).json()["data"]["user"]["id"]

    parcelas = [{
        "id": "parcela-lote-3",
        "name": "Lote 3",
        "user_id": user_id,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-91.81, 14.40], [-91.80, 14.40], [-91.80, 14.41], [-91.81, 14.41], [-91.81, 14.40]]],
        },
    }]
    labores = [
        {**row, "IdRespuesta": str(1000 + index), "GeoLocalizacion": "-91.8067679,14.4050676"}
        for index, row in enumerate(LABORES_DEL_CICLO)
    ]
    run = _sincroniza(FIELD_LOG_ENTRY_FORM_TYPE, "2534", labores, empresa_conectada, user_id, parcelas)
    assert run["imported_rows"] == len(LABORES_DEL_CICLO)
    assert run["unclassified_category_rows"] == 0
    assert run["unmatched_parcel_rows"] == 0
    _sincroniza(FIELD_LOG_CYCLE_FORM_TYPE, "2533", [{**FICHA, "IdRespuesta": "2000"}], empresa_conectada, user_id, parcelas)

    # Volver a sincronizar la misma respuesta la actualiza, no la duplica.
    repetido = _sincroniza(FIELD_LOG_ENTRY_FORM_TYPE, "2534", labores, empresa_conectada, user_id, parcelas)
    assert repetido["imported_rows"] == 0
    assert repetido["updated_rows"] == len(LABORES_DEL_CICLO)

    mapa = client.get("/api/compat/sig-agricola/field-log-records", headers=_auth(token))
    assert mapa.status_code == 200, mapa.text
    puntos = mapa.json()["data"]
    assert len(puntos) == len(LABORES_DEL_CICLO)
    assert all(punto["parcel_id"] == "parcela-lote-3" for punto in puntos)
    assert all(punto["parcel_match_source"] == "lote" for punto in puntos)

    bitacoras = client.get("/api/compat/field-log/cycles", headers=_auth(token))
    assert bitacoras.status_code == 200, bitacoras.text
    data = bitacoras.json()["data"]
    assert data["is_configured"] is True
    ciclo = next(item for item in data["cycles"] if item["parcela"] == "Lote 3")
    assert ciclo["kpis"]["economics"]["investment_per_ha"] == pytest.approx(14156.0)
    assert ciclo["kpis"]["economics"]["profit_per_ha"] == pytest.approx(5869.0)
    assert ciclo["superficie_ha"] == pytest.approx(5.0)
    assert ciclo["cultivo"] == "Maiz"

    detalle = client.get(f"/api/compat/field-log/cycles/{ciclo['cycle_key']}", headers=_auth(token))
    assert detalle.status_code == 200, detalle.text
    assert len(detalle.json()["data"]["entries"]) == len(LABORES_DEL_CICLO)


def test_una_labor_sin_gps_se_guarda_pero_no_se_pinta(client, token, empresa_conectada):
    """Perder una labor por falta de señal falsearía la inversión del ciclo."""
    user_id = client.post("/api/compat/auth/sign-in", json=SUPERADMIN).json()["data"]["user"]["id"]
    sin_gps = [{**CICLO, "Ciclo": "PV 2026 SIN GPS", "IdRespuesta": "3001",
                "Categoria": "9. Diversos", "Concepto": "Jornal", "Cantidad": "1",
                "CostoUnitario": "500", "Location": "-1, -1"}]
    run = _sincroniza(FIELD_LOG_ENTRY_FORM_TYPE, "2534", sin_gps, empresa_conectada, user_id)
    assert run["imported_rows"] == 1
    assert run["rows_without_location"] == 1

    puntos = client.get("/api/compat/sig-agricola/field-log-records", headers=_auth(token)).json()["data"]
    assert all(punto["ciclo"] != "PV 2026 SIN GPS" for punto in puntos)

    ciclos = client.get("/api/compat/field-log/cycles", headers=_auth(token)).json()["data"]["cycles"]
    huerfano = next(item for item in ciclos if item["ciclo"] == "PV 2026 SIN GPS")
    assert huerfano["kpis"]["economics"]["investment_per_ha"] == pytest.approx(500.0)
    assert huerfano["located_entry_count"] == 0
