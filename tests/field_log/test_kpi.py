"""Los indicadores se validan contra la hoja de cálculo original.

Los números de `test_matches_original_spreadsheet_sensitivity` salen del bloque
de sensibilidad del archivo `Bitácora de campo.xlsx` del CDT FIRA: es una
corrida real, con una inversión de 14 156 $/ha, y sirve de caso de oro para
comprobar que la traducción de las fórmulas no cambió el significado de nada.
"""

from __future__ import annotations

import pytest

from app.modules.field_log.kpi import (
    EntryInputSnapshot,
    EntrySnapshot,
    compute_kpis,
    cost_summary,
    economics,
    sustainability,
)
from app.modules.field_log.sensitivity import build_matrix


def _entry(category: str, cost: float = 0.0, **data) -> EntrySnapshot:
    return EntrySnapshot(category=category, cost_per_ha=cost, data=data)


class TestEconomics:
    def test_derived_values_follow_the_spreadsheet_formulas(self):
        entries = [
            _entry("acondicionamiento", 3000.0),
            _entry("siembra", 4000.0),
            _entry("fertilizante", 5000.0),
            _entry("cosecha", 2156.0),
        ]

        result = economics(entries, yield_ton_ha=6.0, price_per_ton=5000.0)

        assert result["investment_per_ha"] == pytest.approx(14156.0)
        assert result["revenue_per_ha"] == pytest.approx(30000.0)
        assert result["profit_per_ha"] == pytest.approx(15844.0)
        assert result["benefit_cost_ratio"] == pytest.approx(30000.0 / 14156.0)
        # Punto de equilibrio: inversión ÷ precio (E7 = H5/D5)
        assert result["break_even_ton_ha"] == pytest.approx(14156.0 / 5000.0)
        # Costo unitario: inversión ÷ rendimiento (K7 = H5/B5)
        assert result["unit_cost_per_ton"] == pytest.approx(14156.0 / 6.0)

    def test_missing_yield_leaves_indicators_undefined_instead_of_zero(self):
        """Sin rendimiento no hay costo unitario: la hoja mostraba #DIV/0!.

        Devolver 0 haría creer que producir sale gratis, que es peor que no
        mostrar nada.
        """
        result = economics([_entry("siembra", 1000.0)], yield_ton_ha=None, price_per_ton=5000.0)

        assert result["unit_cost_per_ton"] is None
        assert result["revenue_per_ha"] is None
        assert result["profit_per_ha"] is None
        assert result["investment_per_ha"] == pytest.approx(1000.0)


class TestCostSummary:
    def test_percentages_add_up_and_every_category_is_present(self):
        entries = [_entry("riego", 2000.0), _entry("plagas", 6000.0), _entry("riego", 2000.0)]

        summary = cost_summary(entries)
        by_key = {item["category"]: item for item in summary["categories"]}

        assert summary["total_cost_per_ha"] == pytest.approx(10000.0)
        assert by_key["riego"]["cost_per_ha"] == pytest.approx(4000.0)
        assert by_key["riego"]["percentage"] == pytest.approx(40.0)
        assert by_key["plagas"]["percentage"] == pytest.approx(60.0)
        # Las diez categorías salen siempre, aunque estén en cero: el resumen
        # de costos de la hoja las lista todas.
        assert len(summary["categories"]) == 10
        assert by_key["cosecha"]["cost_per_ha"] == 0.0

    def test_total_of_zero_does_not_break_percentages(self):
        summary = cost_summary([_entry("riego", 0.0)])
        assert summary["total_cost_per_ha"] == 0.0
        assert all(item["percentage"] is None for item in summary["categories"])


class TestSustainability:
    def test_water_energy_and_active_ingredient_per_ton(self):
        entries = [
            _entry("riego", 1000.0, m3=3000, kwh=1200),
            _entry("riego", 1000.0, m3=1500, kwh=600),
            _entry("acondicionamiento", 500.0, diesel_l=45),
            _entry("malezas", 800.0, ia_grams=720),
            _entry("plagas", 600.0, ia_grams=360),
            _entry("enfermedades", 400.0, ia_grams=180),
            _entry("fertilizante", 2000.0, n_units=240, p_units=60, k_units=30),
        ]

        result = sustainability(entries, yield_ton_ha=6.0)

        assert result["water_m3_per_ha"] == pytest.approx(4500.0)
        assert result["water_footprint_m3_per_ton"] == pytest.approx(750.0)
        assert result["energy_kwh_per_ton"] == pytest.approx(300.0)
        assert result["diesel_l_per_ha"] == pytest.approx(45.0)
        assert result["herbicide_ia_g_per_ton"] == pytest.approx(120.0)
        assert result["insecticide_ia_g_per_ton"] == pytest.approx(60.0)
        assert result["fungicide_ia_g_per_ton"] == pytest.approx(30.0)
        assert result["nitrogen_kg_per_ton"] == pytest.approx(40.0)
        assert result["npk_formula"] == "240-60-30"

    def test_products_and_row_totals_are_both_counted(self):
        """El i.a. se puede capturar como total de la labor o por producto.

        Quien detalle la mezcla no debe tener que repetir además el total, y
        quien solo apunte el total tampoco pierde el indicador.
        """
        entries = [
            EntrySnapshot(
                category="plagas",
                cost_per_ha=500.0,
                data={"ia_grams": 100},
                inputs=(
                    EntryInputSnapshot(ia_grams=50),
                    EntryInputSnapshot(ia_grams=25),
                ),
            )
        ]

        result = sustainability(entries, yield_ton_ha=5.0)
        assert result["insecticide_ia_g_per_ha"] == pytest.approx(175.0)

    def test_derives_active_ingredient_only_from_matching_categories(self):
        entries = [
            _entry("malezas", 0.0, ia_grams=500),
            _entry("plagas", 0.0, ia_grams=300),
        ]
        result = sustainability(entries, yield_ton_ha=1.0)

        assert result["herbicide_ia_g_per_ha"] == pytest.approx(500.0)
        assert result["insecticide_ia_g_per_ha"] == pytest.approx(300.0)
        assert result["fungicide_ia_g_per_ha"] == pytest.approx(0.0)


class TestSensitivityMatrix:
    def test_matches_original_spreadsheet_values(self):
        """Comprobado contra el bloque de sensibilidad del archivo del CDT."""
        matrix = build_matrix(
            investment_per_ha=14156.0,
            yield_ton_ha=6.0,
            price_per_ton=5050.0,
            yield_values=[4.5, 5.0, 9.0],
            price_values=[4450.0, 5650.0],
        )

        cells = {
            (row["yield_ton_ha"], cell["price_per_ton"]): cell["profit_per_ha"]
            for row in matrix["rows"]
            for cell in row["cells"]
        }

        assert cells[(4.5, 4450.0)] == pytest.approx(5869.0)
        assert cells[(5.0, 4450.0)] == pytest.approx(8094.0)
        assert cells[(9.0, 5650.0)] == pytest.approx(36694.0)

    def test_current_scenario_is_flagged_once(self):
        matrix = build_matrix(
            investment_per_ha=10000.0,
            yield_ton_ha=6.0,
            price_per_ton=5000.0,
            yield_values=[5.0, 6.0],
            price_values=[4000.0, 5000.0],
        )

        flagged = [
            (row["yield_ton_ha"], cell["price_per_ton"])
            for row in matrix["rows"]
            for cell in row["cells"]
            if cell["is_current"]
        ]
        assert flagged == [(6.0, 5000.0)]

    def test_break_even_points(self):
        matrix = build_matrix(
            investment_per_ha=12000.0, yield_ton_ha=6.0, price_per_ton=4000.0
        )
        assert matrix["break_even_price_per_ton"] == pytest.approx(2000.0)
        assert matrix["break_even_yield_ton_ha"] == pytest.approx(3.0)


class TestComputeKpis:
    def test_budget_usage_is_reported_when_a_budget_exists(self):
        entries = [_entry("siembra", 4000.0), _entry("riego", 1000.0)]

        result = compute_kpis(
            entries, yield_ton_ha=5.0, price_per_ton=5000.0, budget_per_ha=10000.0
        )

        assert result["budget_usage_percentage"] == pytest.approx(50.0)
        assert result["entry_count"] == 2

    def test_text_values_from_imported_spreadsheets_do_not_break_the_sum(self):
        """Las hojas importadas traen texto en celdas numéricas.

        Un '#DIV/0!' o un 'n/a' heredado no puede tumbar el cálculo del ciclo
        entero.
        """
        entries = [
            EntrySnapshot(category="riego", cost_per_ha=1000.0, data={"m3": "n/a"}),
            EntrySnapshot(category="riego", cost_per_ha=500.0, data={"m3": "1,200"}),
        ]

        result = compute_kpis(entries, yield_ton_ha=4.0, price_per_ton=1000.0)

        assert result["economics"]["investment_per_ha"] == pytest.approx(1500.0)
        assert result["sustainability"]["water_m3_per_ha"] == pytest.approx(1200.0)
