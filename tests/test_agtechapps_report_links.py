"""Flujo completo: formulario de AgtechApps enlazado a una plantilla de Reportes.

Cubre las dos mitades de la integración:

* la configuración por HTTP (catálogo, propuesta de mapeo, vínculo, aislamiento
  entre empresas), y
* el aterrizaje de una respuesta real de AgtechApps como envío de reporte
  georreferenciado, que se ejercita en directo porque depende de un servicio
  externo que no se puede llamar desde un test.

Se ejecuta con un almacén compat vacío para partir del `default_db()` sembrado,
igual que el resto de smoke tests del sistema de compatibilidad.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")
os.environ["DATARIS_COMPAT_PERSISTENCE"] = "file"
os.environ["DATARIS_COMPAT_STORAGE_DIR"] = tempfile.mkdtemp(prefix="dataris-compat-agtech-")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.api.routers import compat  # noqa: E402
from app.api.routers.compat_sig import _persist_report_submissions  # noqa: E402
from app.services.digiforms_data_api import extract_form_rows  # noqa: E402
from app.services.digiforms_report_links import (  # noqa: E402
    report_form_type_for,
    suggest_field_map,
    values_from_response,
)

SUPERADMIN = {"email": "admin@dataris.local", "password": "admin123456"}

# Plantilla mínima con un campo simple, una evaluación y una valoración: es
# suficiente para comprobar que cada tipo de bloque se rellena con su propio
# formato de clave.
TEMPLATE_SCHEMA = {
    "brand": {"name": "Visita de campo"},
    "header": [{"name": "folio", "label": "Folio", "type": "text"}],
    "tabs": [
        {
            "id": "general",
            "label": "General",
            "blocks": [
                {
                    "kind": "field-grid",
                    "id": "datos",
                    "fields": [
                        {"name": "productor", "label": "Productor", "type": "text"},
                        {"name": "observaciones", "label": "Observaciones", "type": "textarea"},
                    ],
                },
                {
                    "kind": "eval-table",
                    "id": "ev",
                    "scale": "bueno-regular-malo-na",
                    "rows": [{"key": "riego", "label": "Estado del riego"}],
                },
                {"kind": "rating", "id": "cal", "label": "Calificación general"},
            ],
        }
    ],
}


@pytest.fixture(scope="module")
def client() -> TestClient:
    # Sin el gestor de contexto a propósito: entrar en él dispara el arranque de
    # la aplicación, que abre una conexión SQLAlchemy a Postgres. El sistema de
    # compatibilidad que se prueba aquí no la usa (ver
    # [[project-dataris-arquitectura-usuarios]]).
    return TestClient(app)


def _token(client: TestClient, credentials: dict) -> str:
    response = client.post("/api/compat/auth/sign-in", json=credentials)
    assert response.status_code == 200, response.text
    return response.json()["data"]["session"]["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def superadmin_token(client: TestClient) -> str:
    return _token(client, SUPERADMIN)


@pytest.fixture(scope="module")
def template_id(client: TestClient, superadmin_token: str) -> str:
    response = client.post(
        "/api/compat/reports/templates",
        headers=_auth(superadmin_token),
        json={
            "key": "visita-campo-test",
            "name": "Reporte de visita",
            "version": 1,
            "schema": TEMPLATE_SCHEMA,
            "catalogs": {},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["id"]


@pytest.fixture(scope="module")
def connected_company(client: TestClient, superadmin_token: str) -> str:
    """Deja la empresa del superadmin con credenciales de AgtechApps guardadas."""
    response = client.put(
        "/api/compat/extensions/digiforms/company-config",
        headers=_auth(superadmin_token),
        json={"client_id": "178", "api_user": "api", "api_password": "secreto-de-prueba"},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]["connection"]["company_id"]


def test_catalogo_registra_formulario_y_lo_lista(client, superadmin_token, connected_company, template_id):
    """Un formulario registrado se puede elegir después por nombre."""
    response = client.post(
        "/api/compat/extensions/digiforms/forms",
        headers=_auth(superadmin_token),
        json={"form_id": "FORM-VISITA-001", "name": "Visita de campo (AgtechApps)"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["name"] == "Visita de campo (AgtechApps)"

    listing = client.get("/api/compat/extensions/digiforms/forms", headers=_auth(superadmin_token))
    assert listing.status_code == 200
    data = listing.json()["data"]
    assert [item["form_id"] for item in data["forms"]] == ["FORM-VISITA-001"]
    # El mismo endpoint ofrece las plantillas enlazables, que es lo que necesita
    # el desplegable de la configuración.
    assert any(item["key"] == "visita-campo-test" for item in data["report_templates"])


def test_formulario_sin_nombre_es_rechazado(client, superadmin_token, connected_company):
    response = client.post(
        "/api/compat/extensions/digiforms/forms",
        headers=_auth(superadmin_token),
        json={"form_id": "FORM-SIN-NOMBRE"},
    )
    assert response.status_code == 400
    assert "nombre" in response.json()["detail"].lower()


def test_propuesta_de_mapeo_cruza_campos_por_etiqueta(client, superadmin_token, connected_company, template_id):
    """La propuesta empareja las preguntas de AgtechApps con la plantilla."""
    response = client.post(
        "/api/compat/extensions/digiforms/report-links/suggest",
        headers=_auth(superadmin_token),
        json={
            "form_id": "FORM-VISITA-001",
            "report_template_id": template_id,
            "discovered_fields": ["Folio", "Productor", "Observaciones", "Estado del riego", "Terminal"],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["field_map"]["folio"] == "Folio"
    assert data["field_map"]["productor"] == "Productor"
    assert data["field_map"]["eval.ev.riego"] == "Estado del riego"
    # Una pregunta que no corresponde a ningún campo queda señalada en vez de
    # colarse en un campo cualquiera.
    assert "Terminal" in data["unused_api_fields"]


def test_vinculo_se_guarda_y_aparece_listado(client, superadmin_token, connected_company, template_id):
    response = client.post(
        "/api/compat/extensions/digiforms/report-links",
        headers=_auth(superadmin_token),
        json={
            "form_id": "FORM-VISITA-001",
            "report_template_id": template_id,
            "field_map": {"folio": "Folio", "productor": "Productor", "eval.ev.riego": "Estado del riego"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["form_type"] == report_form_type_for("FORM-VISITA-001")

    listing = client.get("/api/compat/extensions/digiforms/report-links", headers=_auth(superadmin_token))
    links = listing.json()["data"]["links"]
    assert len(links) == 1
    assert links[0]["report_template_key"] == "visita-campo-test"
    assert links[0]["form_name"] == "Visita de campo (AgtechApps)"


def test_no_se_puede_enlazar_un_formulario_fuera_del_catalogo(client, superadmin_token, connected_company, template_id):
    response = client.post(
        "/api/compat/extensions/digiforms/report-links",
        headers=_auth(superadmin_token),
        json={"form_id": "FORM-QUE-NO-EXISTE", "report_template_id": template_id},
    )
    assert response.status_code == 404


def test_formulario_enlazado_no_se_borra_del_catalogo(client, superadmin_token, connected_company):
    listing = client.get("/api/compat/extensions/digiforms/forms", headers=_auth(superadmin_token))
    row_id = next(item["id"] for item in listing.json()["data"]["forms"] if item["form_id"] == "FORM-VISITA-001")
    response = client.delete(f"/api/compat/extensions/digiforms/forms/{row_id}", headers=_auth(superadmin_token))
    assert response.status_code == 400
    assert "vínculo" in response.json()["detail"].lower()


def test_usuario_sin_sesion_no_ve_la_configuracion(client):
    assert client.get("/api/compat/extensions/digiforms/forms").status_code == 401


def test_superadmin_ve_las_plantillas_de_cualquier_empresa(client, superadmin_token, template_id):
    """Un superadmin de DATARIS administra los formularios de todos los clientes.

    Antes veía el listado vacío si las plantillas pertenecían a otra empresa,
    porque el filtro por empresa se le aplicaba igual que a un usuario normal.
    """
    with compat.LOCK:
        db = compat.read_db()
        compat.table(db, "report_templates").append({
            "id": str(uuid.uuid4()),
            "key": "plantilla-de-otro-cliente",
            "name": "Plantilla de otro cliente",
            "version": 1,
            "company_id": "otra-empresa-cualquiera",
            "schema": {"tabs": []},
            "catalogs": {},
        })
        compat.write_db(db)

    response = client.post("/api/compat/tables/report_templates/query", headers=_auth(superadmin_token), json={})
    claves = [row.get("key") for row in response.json()["data"]]
    assert "plantilla-de-otro-cliente" in claves


def test_los_envios_de_otra_empresa_siguen_siendo_privados(client, superadmin_token):
    """La excepción del superadmin llega a las plantillas, no a los datos de campo."""
    with compat.LOCK:
        db = compat.read_db()
        compat.table(db, "report_submissions").append({
            "id": str(uuid.uuid4()),
            "template_id": "cualquiera",
            "template_key": "plantilla-de-otro-cliente",
            "company_id": "otra-empresa-cualquiera",
            "user_id": "usuario-de-otra-empresa",
            "values": {"secreto": "dato de campo del cliente"},
        })
        compat.write_db(db)

    response = client.post("/api/compat/tables/report_submissions/query", headers=_auth(superadmin_token), json={})
    empresas = {str(row.get("company_id") or "") for row in response.json()["data"]}
    assert "otra-empresa-cualquiera" not in empresas


# ---------------------------------------------------------------------------
# Listado de formularios del proveedor (GET api/form/{clientId})
# ---------------------------------------------------------------------------


# Respuesta tal cual la documenta el proveedor, con sus rarezas: IsPublic es una
# cadena y Status es un identificador, no un texto.
RESPUESTA_LISTADO = {
    "Forms": [
        {
            "Id": "101",
            "Description": "Formulario de inspección diaria",
            "Title": "Inspección Diaria",
            "ValidFrom": "2026-01-01 00:00:00",
            "ValidTo": "2026-12-31 23:59:59",
            "Status": "1",
            "Category": "Operaciones",
            "ReferenceId": "REF-101",
            "IsPublic": "false",
        }
    ]
}


def test_listado_del_proveedor_se_normaliza():
    formularios = extract_form_rows(RESPUESTA_LISTADO)
    assert len(formularios) == 1
    form = formularios[0]
    assert form["form_id"] == "101"
    # El título es lo que se enseña en el desplegable.
    assert form["name"] == "Inspección Diaria"
    assert form["category"] == "Operaciones"
    assert form["valid_to"] == "2026-12-31 23:59:59"
    # "false" como cadena no puede acabar siendo verdadero.
    assert form["is_public"] is False


def test_listado_tolera_lista_suelta_y_campos_ausentes():
    formularios = extract_form_rows([{"Id": "77"}, {"sin": "id"}])
    assert len(formularios) == 1
    # Sin título ni descripción, el nombre cae al identificador antes que quedar vacío.
    assert formularios[0]["name"] == "77"


def test_listado_vacio_no_revienta():
    assert extract_form_rows({}) == []
    assert extract_form_rows(None) == []


def test_importar_catalogo_desde_el_proveedor(client, superadmin_token, connected_company, monkeypatch):
    """El catálogo se llena solo desde AgtechApps, sin teclear identificadores."""
    async def fake_get_forms(self, client_id=None):
        return extract_form_rows(RESPUESTA_LISTADO)

    monkeypatch.setattr("app.services.digiforms_data_api.DigiformsDataAPI.get_forms", fake_get_forms)

    response = client.post("/api/compat/extensions/digiforms/forms/import", headers=_auth(superadmin_token), json={})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["received"] == 1
    assert data["created"] == 1
    importado = next(item for item in data["forms"] if item["form_id"] == "101")
    assert importado["name"] == "Inspección Diaria"
    assert importado["source"] == "agtechapps_catalog"


def test_importar_dos_veces_no_duplica_ni_pisa_lo_ya_sabido(client, superadmin_token, connected_company, monkeypatch):
    """Reimportar actualiza; lo que Dataris averiguó del formulario se conserva."""
    async def fake_get_forms(self, client_id=None):
        return extract_form_rows(RESPUESTA_LISTADO)

    monkeypatch.setattr("app.services.digiforms_data_api.DigiformsDataAPI.get_forms", fake_get_forms)

    # Simula que ese formulario ya había sido comprobado contra la Data API.
    with compat.LOCK:
        db = compat.read_db()
        fila = next(row for row in compat.table(db, "digiforms_forms") if row.get("form_id") == "101")
        fila["discovered_fields"] = ["Folio", "Productor"]
        fila["verification_status"] = "ok"
        compat.write_db(db)

    response = client.post("/api/compat/extensions/digiforms/forms/import", headers=_auth(superadmin_token), json={})
    data = response.json()["data"]
    assert data["created"] == 0
    assert data["updated"] == 1
    importado = next(item for item in data["forms"] if item["form_id"] == "101")
    assert importado["discovered_fields"] == ["Folio", "Productor"]
    assert importado["verification_status"] == "ok"


def test_error_del_proveedor_se_explica(client, superadmin_token, connected_company, monkeypatch):
    """Un 400 del proveedor llega al operador como una causa, no como un número."""
    from app.services.digiforms_data_api import DigiformsDataAPIError

    async def fake_get_forms(self, client_id=None):
        raise DigiformsDataAPIError("AgtechApps rechazó la petición: el ClientId no coincide", status_code=400)

    monkeypatch.setattr("app.services.digiforms_data_api.DigiformsDataAPI.get_forms", fake_get_forms)

    response = client.post("/api/compat/extensions/digiforms/forms/import", headers=_auth(superadmin_token), json={})
    assert response.status_code == 502
    assert "ClientId" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Aterrizaje de respuestas
# ---------------------------------------------------------------------------


PARCELA = {
    "id": "parcela-1",
    "name": "Lote Norte",
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[-96.25, 18.57], [-96.17, 18.57], [-96.17, 18.62], [-96.25, 18.62], [-96.25, 18.57]]],
    },
}


def _respuesta_agtechapps(response_id: int, lat: float, lng: float) -> dict:
    return {
        "ResponseId": response_id,
        "Folio": f"F-{response_id}",
        "Productor": "Juan Pérez",
        "Estado del riego": "Bueno",
        "Latitud": lat,
        "Longitud": lng,
        "State": "0",
    }


def test_valores_se_traducen_al_formato_del_renderer():
    """Cada tipo de bloque recibe la clave y el valor que el renderer espera."""
    field_map = {"folio": "Folio", "productor": "Productor", "eval.ev.riego": "Estado del riego"}
    values, missing = values_from_response(TEMPLATE_SCHEMA, field_map, _respuesta_agtechapps(1, 18.6, -96.2))
    assert values["folio"] == "F-1"
    assert values["productor"] == "Juan Pérez"
    # "Bueno" es la etiqueta visible; el renderer guarda la clave de la escala.
    assert values["eval.ev.riego"] == "b"
    assert missing == []


def test_valor_fuera_de_escala_se_conserva_como_observacion():
    values, _ = values_from_response(
        TEMPLATE_SCHEMA,
        {"eval.ev.riego": "Estado del riego"},
        {"Estado del riego": "Se reparó ayer"},
    )
    assert values["eval.ev.riego.obs"] == "Se reparó ayer"


def test_mapeo_ambiguo_no_se_adivina():
    """Ante dos candidatos igual de plausibles se prefiere no mapear."""
    mapping = suggest_field_map(
        {"tabs": [{"id": "t", "blocks": [{"kind": "field-grid", "id": "g", "fields": [{"name": "fecha", "label": "Fecha"}]}]}]},
        ["Fecha Inicio", "Fecha Finalizacion"],
    )
    assert "fecha" not in mapping


def test_respuestas_aterrizan_como_envios_georreferenciados(client, superadmin_token, connected_company, template_id):
    """El corazón del flujo: de respuesta de AgtechApps a reporte en el SIG."""
    form_id = "FORM-VISITA-001"
    form_type = report_form_type_for(form_id)
    with compat.LOCK:
        db = compat.read_db()
        actor = next(row["id"] for row in db["users"] if row["email"] == SUPERADMIN["email"])

        run = _persist_report_submissions(
            db=db,
            user_id=actor,
            company_id=connected_company,
            form_type=form_type,
            form_id=form_id,
            # Un punto dentro del lote y otro fuera de cualquier parcela.
            response_rows=[
                _respuesta_agtechapps(101, 18.60, -96.20),
                _respuesta_agtechapps(102, 10.00, -80.00),
            ],
            image_rows=[],
            parcels=[PARCELA],
            cursor_before=0,
            sync_mode="initial_date_range",
        )
        compat.write_db(db)

    assert run["imported_rows"] == 2
    assert run["outside_registered_parcel_rows"] == 1
    # El cursor avanza al ResponseId más alto para que la próxima pasada sea
    # incremental y no reimporte lo ya traído.
    assert run["cursor_after"] == 102

    with compat.LOCK:
        db = compat.read_db()
        submissions = [row for row in compat.table(db, "report_submissions") if row.get("digiforms_form_id") == form_id]

    assert len(submissions) == 2
    dentro = next(row for row in submissions if row["external_response_id"] == "101")
    assert dentro["parcel_id"] == "parcela-1"
    assert dentro["template_key"] == "visita-campo-test"
    assert dentro["values"]["productor"] == "Juan Pérez"
    assert dentro["status"] == "submitted"

    fuera = next(row for row in submissions if row["external_response_id"] == "102")
    assert fuera["parcel_id"] is None
    assert fuera["outside_registered_parcel"] is True


def test_resincronizar_actualiza_en_lugar_de_duplicar(client, superadmin_token, connected_company, template_id):
    """La misma respuesta traída dos veces no crea un reporte repetido."""
    form_id = "FORM-VISITA-001"
    with compat.LOCK:
        db = compat.read_db()
        actor = next(row["id"] for row in db["users"] if row["email"] == SUPERADMIN["email"])
        antes = len([r for r in compat.table(db, "report_submissions") if r.get("digiforms_form_id") == form_id])

        run = _persist_report_submissions(
            db=db,
            user_id=actor,
            company_id=connected_company,
            form_type=report_form_type_for(form_id),
            form_id=form_id,
            response_rows=[_respuesta_agtechapps(101, 18.60, -96.20)],
            image_rows=[],
            parcels=[PARCELA],
            cursor_before=100,
            sync_mode="incremental_response_id",
        )
        compat.write_db(db)
        despues = len([r for r in compat.table(db, "report_submissions") if r.get("digiforms_form_id") == form_id])

    assert run["updated_rows"] == 1
    assert run["imported_rows"] == 0
    assert antes == despues


def test_capa_sig_solo_devuelve_los_reportes_con_coordenadas(client, superadmin_token):
    response = client.get("/api/compat/sig-agricola/report-records", headers=_auth(superadmin_token))
    assert response.status_code == 200, response.text
    rows = response.json()["data"]
    assert rows, "el reporte georreferenciado debería alimentar la capa del SIG"
    assert all(row["lat"] is not None and row["lng"] is not None for row in rows)
    assert all(row["template_name"] == "Reporte de visita" for row in rows)


def test_reporte_sin_coordenadas_se_guarda_pero_no_va_al_mapa(client, superadmin_token, connected_company, template_id):
    """Un reporte sin GPS no se pierde: existe en Reportes aunque no se pinte."""
    form_id = "FORM-VISITA-001"
    with compat.LOCK:
        db = compat.read_db()
        actor = next(row["id"] for row in db["users"] if row["email"] == SUPERADMIN["email"])
        run = _persist_report_submissions(
            db=db,
            user_id=actor,
            company_id=connected_company,
            form_type=report_form_type_for(form_id),
            form_id=form_id,
            response_rows=[{"ResponseId": 200, "Folio": "F-200", "Productor": "Sin GPS", "State": "0"}],
            image_rows=[],
            parcels=[PARCELA],
            cursor_before=150,
            sync_mode="incremental_response_id",
        )
        compat.write_db(db)

    assert run["imported_rows"] == 1
    assert run["rows_without_location"] == 1

    en_mapa = client.get("/api/compat/sig-agricola/report-records", headers=_auth(superadmin_token)).json()["data"]
    assert all(row["external_response_id"] != "200" for row in en_mapa)

    with compat.LOCK:
        db = compat.read_db()
        guardado = next(
            row for row in compat.table(db, "report_submissions") if row.get("external_response_id") == "200"
        )
    assert guardado["values"]["folio"] == "F-200"
