"""Reglas del servicio que no dependen de la base de datos."""

from __future__ import annotations

import pytest

from app.modules.field_log.service import _resolve_cost
from app.modules.field_log.templates import (
    DEFAULT_LABOR_STANDARDS,
    get_system_template,
    list_system_templates,
)


class TestResolveCost:
    def test_quantity_times_unit_cost_wins_over_the_client_value(self):
        """El cliente no decide el costo cuando hay cantidad y precio unitario.

        Si el móvil manda un total desactualizado tras editar la cantidad, el
        servidor no puede quedarse con el número viejo.
        """
        assert _resolve_cost(3, 250, 999999) == pytest.approx(750.0)

    def test_closed_cost_labor_keeps_the_provided_total(self):
        # Una renta de maquinaria o un servicio contratado no tiene cantidad.
        assert _resolve_cost(None, None, 4500) == pytest.approx(4500.0)

    def test_missing_everything_is_zero_not_none(self):
        assert _resolve_cost(None, None, None) == 0.0

    def test_partial_data_falls_back_to_the_provided_total(self):
        assert _resolve_cost(5, None, 300) == pytest.approx(300.0)


class TestTemplates:
    def test_every_system_template_covers_the_ten_categories(self):
        for template in list_system_templates():
            keys = [category["key"] for category in template["categories"]]
            assert len(keys) == 10
            assert keys[0] == "acondicionamiento"
            assert keys[-1] == "cosecha"

    def test_unknown_template_key_falls_back_instead_of_failing(self):
        """Una bitácora con plantilla inexistente se sigue pudiendo abrir."""
        template = get_system_template("plantilla-que-no-existe")
        assert template["key"] == "generica"

    def test_maize_template_carries_the_v_r_scale(self):
        template = get_system_template("cdt-fira-maiz")
        codes = [stage["code"] for stage in template["phenology_stages"]]

        assert codes[0] == "germinacion"
        assert "v11" in codes
        assert "r3" in codes

    def test_sugarcane_template_has_its_own_stages(self):
        codes = [stage["code"] for stage in get_system_template("cana-azucar")["phenology_stages"]]
        assert "ahijamiento" in codes
        assert "v11" not in codes

    def test_categories_that_need_products_are_flagged(self):
        template = get_system_template("generica")
        by_key = {category["key"]: category for category in template["categories"]}

        assert by_key["plagas"]["supports_inputs"] is True
        assert by_key["fertilizante"]["supports_inputs"] is True
        assert by_key["acondicionamiento"]["supports_inputs"] is False

    def test_irrigation_declares_the_fields_the_kpis_need(self):
        """Sin kWh y m³ en riego no hay huella hídrica ni energía por tonelada."""
        template = get_system_template("generica")
        by_key = {category["key"]: category for category in template["categories"]}
        irrigation_fields = {field["name"] for field in by_key["riego"]["fields"]}

        assert {"kwh", "m3"} <= irrigation_fields

    def test_labor_standards_match_the_original_sheet(self):
        by_name = {item["labor_name"]: item["hours_per_ha"] for item in DEFAULT_LABOR_STANDARDS}

        assert by_name["Subsoleo"] == pytest.approx(2.15)
        assert by_name["Siembra"] == pytest.approx(3.9)
        assert by_name["Aplicación foliar"] == pytest.approx(1.2)
