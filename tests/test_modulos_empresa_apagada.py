"""Apagar un módulo a una empresa se lo quita a todos sus usuarios.

Cubre los módulos que «no se sincronizaban» al apagarlos por empresa: las
extensiones (DigiformsApp) se seguían concediendo a quien tuviera una solicitud
aprobada o una fila propia en `true`, porque bastaba con cualquier concesión.
Ahora una decisión explícita en `false`, de la empresa o de la persona, gana.

Incluye también el catálogo agrupado como el menú lateral (secciones,
submódulos y pantallas incluidas), que el panel usa para listar los módulos.
"""
from __future__ import annotations

from app.api.routers import compat_extensions, me_access, module_access_admin
from app.services import module_catalog

COMPANY_ID = "empresa-apagada"
USER_ID = "usuario-con-solicitud"


def _db(*, company_digiforms: bool | None = False, user_digiforms: bool | None = True) -> dict:
    company_modules = [
        {"company_id": COMPANY_ID, "module_id": "satelite", "is_enabled": True, "is_active": True},
    ]
    if company_digiforms is not None:
        company_modules.append(
            {"company_id": COMPANY_ID, "module_id": "digiforms", "is_enabled": company_digiforms, "is_active": company_digiforms}
        )
    user_modules = []
    if user_digiforms is not None:
        user_modules.append(
            {"user_id": USER_ID, "module_id": "digiforms", "is_enabled": user_digiforms, "is_active": user_digiforms}
        )
    return {
        "users": [{"id": USER_ID, "email": "persona@cliente-final.com"}],
        "tables": {
            "platform_modules": [
                {"id": "dashboard", "name": "Centro de control", "is_active": True},
                {"id": "satelite", "name": "Monitoreo satelital", "is_active": True},
                {"id": "digiforms", "name": "DigiformsApp", "is_active": True},
            ],
            "company_modules": company_modules,
            "user_modules": user_modules,
            "extension_requests": [
                {
                    "id": "solicitud-1",
                    "extension_id": "digiforms",
                    "status": "approved",
                    "company_id": COMPANY_ID,
                    "requested_by_user_id": USER_ID,
                },
            ],
        },
    }


def _effective(db: dict) -> list[str]:
    return me_access._effective_module_ids(
        db,
        active_modules=db["tables"]["platform_modules"],
        user_id=USER_ID,
        admin_user_id=None,
        company_id=COMPANY_ID,
    )


def test_la_empresa_apagada_gana_a_la_solicitud_aprobada_y_al_ajuste_propio():
    db = _db(company_digiforms=False, user_digiforms=True)

    assert compat_extensions.extension_enabled_for(db, COMPANY_ID, USER_ID, "digiforms") is False
    effective = _effective(db)
    assert "digiforms" not in effective
    # Lo demás del paquete sigue intacto.
    assert "satelite" in effective


def test_sin_decision_de_la_empresa_la_solicitud_aprobada_sigue_valiendo():
    db = _db(company_digiforms=None, user_digiforms=None)

    assert compat_extensions.extension_enabled_for(db, COMPANY_ID, USER_ID, "digiforms") is True
    assert "digiforms" in _effective(db)


def test_el_usuario_apagado_no_recupera_la_extension_por_su_solicitud():
    db = _db(company_digiforms=True, user_digiforms=False)

    assert compat_extensions.extension_enabled_for(db, COMPANY_ID, USER_ID, "digiforms") is False
    assert "digiforms" not in _effective(db)


def test_el_panel_explica_que_lo_apago_la_empresa():
    db = _db(company_digiforms=False, user_digiforms=True)
    rows = {row["module_id"]: row for row in module_access_admin._catalog_rows(db)}
    assert "digiforms" in rows

    disabled = module_access_admin._company_disabled(db, COMPANY_ID)
    assert disabled == {"digiforms"}


def test_el_catalogo_se_agrupa_como_el_menu_lateral():
    groups = {group_id for group_id, _ in module_catalog.MODULE_GROUPS}
    for spec in module_catalog.MODULE_SPECS:
        assert spec.group in groups, spec.id
        if spec.parent:
            assert spec.parent in module_catalog.SPECS_BY_ID, spec.id
    for item in module_catalog.DERIVED_MODULES:
        assert item["group"] in groups, item["id"]

    specs = module_catalog.SPECS_BY_ID
    # Mismos nombres que el menú lateral de la plataforma.
    assert specs["dashboard"].name == "Centro de control"
    assert specs["alertas"].name == "Alertas operativas"
    assert specs["personal"].name == "Personal de campo"
    # Aplicaciones aéreas no tiene entrada propia: se abre desde Telemetría.
    assert specs["aplicaciones-aereas"].parent == "telemetria"

    card = module_access_admin._module_card("telemetria", {"id": "telemetria", "is_active": True})
    assert card["group"] == module_catalog.GROUP_MAQUINARIA
    assert card["parent"] is None
    assert card["includes"] == ["Cosecha mecánica"]
