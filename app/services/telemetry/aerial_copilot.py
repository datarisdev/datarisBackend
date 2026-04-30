from __future__ import annotations

import json
import os
import math
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import settings


Number = int | float


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return default
        return value
    except Exception:
        return default


def _round(value: Any, ndigits: int = 2) -> float:
    return round(_num(value), ndigits)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _pct(part: float, total: float) -> float:
    return (part / total * 100.0) if total else 0.0


def _severity(score: float) -> str:
    if score >= 90:
        return "excelente"
    if score >= 80:
        return "bueno"
    if score >= 65:
        return "atención"
    return "crítico"


def _action_from_metrics(coverage: float, uncovered_ha: float, overlap_ha: float, total_ha: float) -> str:
    uncovered_pct = _pct(uncovered_ha, total_ha)
    overlap_pct = _pct(overlap_ha, total_ha)
    if coverage >= 94 and uncovered_pct <= 4 and overlap_pct <= 3:
        return "Cerrar operación con revisión visual de bordes"
    if uncovered_pct <= 8 and overlap_pct <= 5:
        return "Reaplicación parcial focalizada"
    if uncovered_pct <= 15:
        return "Inspección técnica y reaplicación parcial priorizada"
    return "No cerrar operación sin revisión; evaluar repetición por zonas"


def _score(analysis: Dict[str, Any], parcels: List[Dict[str, Any]]) -> int:
    total_ha = _num(analysis.get("totalHa"))
    coverage = _num(analysis.get("totalCoveredPct"))
    uncovered_pct = _pct(_num(analysis.get("totalUncoveredHa")), total_ha)
    overlap_pct = _pct(_num(analysis.get("overlapHa")), total_ha)
    speed_min, speed_max = _range(analysis.get("speedRange"))
    alt_min, alt_max = _range(analysis.get("altitudeRange"))
    avg_alt = _num(analysis.get("avgAltitude"))

    parcel_penalty = 0.0
    if parcels:
        weak = [p for p in parcels if _num(p.get("coveredPct")) < 90]
        very_weak = [p for p in parcels if _num(p.get("coveredPct")) < 80]
        parcel_penalty = min(15.0, len(weak) * 3.0 + len(very_weak) * 4.0)

    score = 100.0
    score -= max(0.0, 95.0 - coverage) * 1.15
    score -= max(0.0, uncovered_pct - 3.0) * 1.6
    score -= max(0.0, overlap_pct - 2.0) * 1.2
    score -= parcel_penalty

    if speed_max and speed_min and speed_max - speed_min > 45:
        score -= min(8.0, (speed_max - speed_min - 45.0) / 6.0)
    if alt_max and alt_min and alt_max - alt_min > 25:
        score -= min(8.0, (alt_max - alt_min - 25.0) / 5.0)
    if avg_alt and (avg_alt < 6 or avg_alt > 20):
        score -= 4.0

    return int(round(_clamp(score, 0, 100)))


def _range(value: Any) -> Tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _num(value[0]), _num(value[1])
    return 0.0, 0.0


def _compact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    analysis = payload.get("analysis") or {}
    operation = payload.get("operation") or {}
    options = payload.get("options") or {}

    parcels_raw = analysis.get("parcelResults") or []
    parcels: List[Dict[str, Any]] = []
    for p in parcels_raw:
        parcels.append({
            "name": _text(p.get("name"), "Parcela"),
            "totalHa": _round(p.get("totalHa")),
            "coveredHa": _round(p.get("coveredHa")),
            "coveredPct": _round(p.get("coveredPct"), 1),
            "uncoveredHa": _round(p.get("uncoveredHa")),
            "overlapHa": _round(p.get("overlapHa")),
            "avgAltitude": _round(p.get("avgAltitude"), 1),
            "avgSpeed": _round(p.get("avgSpeed"), 1),
            "uniqueLines": int(_num(p.get("uniqueLines"))),
            "volume": _round(p.get("volume"), 1),
            "hasUncoveredGeom": bool(p.get("uncoveredGeom")),
            "hasOverlapGeom": bool(p.get("overlapGeom")),
        })

    parcels.sort(key=lambda p: (p["coveredPct"], -p["uncoveredHa"], -p["overlapHa"]))

    speed_min, speed_max = _range(analysis.get("speedRange"))
    alt_min, alt_max = _range(analysis.get("altitudeRange"))
    total_ha = _round(analysis.get("totalHa"))
    covered_ha = _round(analysis.get("totalCoveredHa"))
    uncovered_ha = _round(analysis.get("totalUncoveredHa"))
    overlap_ha = _round(analysis.get("overlapHa"))
    coverage = _round(analysis.get("totalCoveredPct"), 1)

    return {
        "operation": {
            "aircraftType": _text(operation.get("aircraftType"), "helicopter"),
            "pilot": _text(operation.get("piloto") or operation.get("pilot"), ""),
            "date": _text(operation.get("fechaAplicacion") or operation.get("date"), ""),
            "product": _text(operation.get("producto") or operation.get("product"), ""),
            "dose": _text(operation.get("dosis") or operation.get("dose"), ""),
            "swathWidthM": _round(operation.get("anchoFaja") or operation.get("swathWidthM") or options.get("swathWidthM"), 1),
            "notes": _text(operation.get("observaciones") or operation.get("notes"), "")[:700],
        },
        "globalMetrics": {
            "totalHa": total_ha,
            "coveredHa": covered_ha,
            "coveragePct": coverage,
            "uncoveredHa": uncovered_ha,
            "uncoveredPct": _round(_pct(uncovered_ha, total_ha), 1),
            "overlapHa": overlap_ha,
            "overlapPct": _round(_pct(overlap_ha, total_ha), 1),
            "efficiencyPct": _round(analysis.get("efficiency") or coverage, 1),
            "avgAltitudeM": _round(analysis.get("avgAltitude"), 1),
            "altitudeRangeM": [_round(alt_min, 1), _round(alt_max, 1)],
            "avgSpeedKph": _round(analysis.get("avgSpeed"), 1),
            "speedRangeKph": [_round(speed_min, 1), _round(speed_max, 1)],
            "totalLines": int(_num(analysis.get("totalLines"))),
            "totalVolume": _round(analysis.get("totalVolume"), 1),
            "parcelCount": len(parcels),
        },
        "parcelsByRisk": parcels[:80],
        "geometryFlags": {
            "hasParcelGeometry": bool(analysis.get("parcelas")),
            "hasAppliedUnion": bool(analysis.get("sprOnUnion")),
            "hasOverlapGeometry": bool(analysis.get("overlapGeom")),
            "hasSprOnTracks": bool(analysis.get("sprOnTracks")),
            "hasSprOffTracks": bool(analysis.get("sprOffTracks")),
        },
    }


def _critical_parcels(compact: Dict[str, Any]) -> List[Dict[str, Any]]:
    total_ha = _num(compact["globalMetrics"].get("totalHa"))
    rows = []
    for p in compact.get("parcelsByRisk", []):
        coverage = _num(p.get("coveredPct"))
        uncovered = _num(p.get("uncoveredHa"))
        overlap = _num(p.get("overlapHa"))
        risk = 0.0
        risk += max(0.0, 94.0 - coverage) * 1.2
        risk += uncovered * 2.2
        risk += overlap * 1.3
        reason_parts = []
        if coverage < 90:
            reason_parts.append("cobertura baja")
        if uncovered > 0:
            reason_parts.append(f"{uncovered:.2f} ha sin cubrir")
        if overlap > 0:
            reason_parts.append(f"{overlap:.2f} ha sobre-aplicadas")
        if _num(p.get("avgSpeed")) > 0 and _num(compact["globalMetrics"].get("avgSpeedKph")) > 0:
            if abs(_num(p.get("avgSpeed")) - _num(compact["globalMetrics"].get("avgSpeedKph"))) > 8:
                reason_parts.append("velocidad distinta al promedio")
        rows.append({
            "parcelName": p.get("name"),
            "priority": "alta" if risk >= 12 or coverage < 88 else "media" if risk >= 6 or coverage < 94 else "baja",
            "riskScore": round(_clamp(risk, 0, 100), 1),
            "issue": ", ".join(reason_parts) or "sin anomalías relevantes",
            "recommendedAction": "Marcar en mapa, validar borde y reaplicar solo polígonos azules" if uncovered > 0 else "Revisar solape y ajustar separación de pasadas",
            "hectaresToReview": round(uncovered + overlap, 2),
        })
    rows.sort(key=lambda r: ({"alta": 0, "media": 1, "baja": 2}.get(r["priority"], 3), -r["riskScore"]))
    return rows[:8]



def _median(values: List[float]) -> float:
    clean = sorted(v for v in values if v > 0 and math.isfinite(v))
    if not clean:
        return 0.0
    middle = len(clean) // 2
    if len(clean) % 2:
        return clean[middle]
    return (clean[middle - 1] + clean[middle]) / 2.0


def _advanced_diagnostics(compact: Dict[str, Any], worst: List[Dict[str, Any]]) -> Dict[str, Any]:
    gm = compact["globalMetrics"]
    parcels = compact.get("parcelsByRisk", [])
    total_ha = _num(gm.get("totalHa"))
    uncovered = _num(gm.get("uncoveredHa"))
    overlap = _num(gm.get("overlapHa"))
    coverage = _num(gm.get("coveragePct"))
    risk_ha = uncovered + overlap
    speed_min, speed_max = _range(gm.get("speedRangeKph"))
    alt_min, alt_max = _range(gm.get("altitudeRangeM"))
    avg_speed = _num(gm.get("avgSpeedKph"))
    avg_alt = _num(gm.get("avgAltitudeM"))

    ranked_risk = []
    for parcel in parcels:
        parcel_risk = _num(parcel.get("uncoveredHa")) + _num(parcel.get("overlapHa"))
        if parcel_risk > 0:
            ranked_risk.append((parcel.get("name") or "Parcela", parcel_risk))
    ranked_risk.sort(key=lambda item: item[1], reverse=True)
    top_one_share = _pct(ranked_risk[0][1], risk_ha) if ranked_risk and risk_ha else 0.0
    top_three_share = _pct(sum(v for _, v in ranked_risk[:3]), risk_ha) if risk_ha else 0.0

    speed_volatility = _pct(speed_max - speed_min, avg_speed) if avg_speed else 0.0
    altitude_volatility = _pct(alt_max - alt_min, avg_alt) if avg_alt else 0.0

    densities = []
    for parcel in parcels:
        area = _num(parcel.get("totalHa"))
        lines = _num(parcel.get("uniqueLines"))
        if area > 0 and lines > 0:
            densities.append(lines / area)
    density_median = _median(densities)
    density_min = min(densities) if densities else 0.0
    density_max = max(densities) if densities else 0.0
    density_spread = _pct(density_max - density_min, density_median) if density_median else 0.0

    if coverage >= 94 and _pct(uncovered, total_ha) <= 6:
        closure = "Media-alta: se puede cerrar si campo valida las zonas azules y el criterio agronómico acepta la subcobertura residual."
    elif coverage >= 90:
        closure = "Media: no conviene cerrar sin revisar las parcelas con mayor concentración de riesgo."
    else:
        closure = "Baja: la cobertura global exige revisión técnica antes de cerrar la operación."

    concentration = (
        f"El riesgo operativo se concentra en {ranked_risk[0][0]} ({top_one_share:.1f}% del riesgo ha) "
        f"y las primeras 3 parcelas acumulan {top_three_share:.1f}%."
        if ranked_risk
        else "No hay concentración relevante de riesgo por parcela."
    )

    speed_stability = (
        f"Variación alta de velocidad: {speed_min:.0f}-{speed_max:.0f} km/h ({speed_volatility:.1f}% del promedio). Cruza estos tramos contra zonas sin cubrir."
        if speed_volatility > 45
        else f"Velocidad relativamente estable: rango equivalente a {speed_volatility:.1f}% del promedio."
    )
    altitude_stability = (
        f"Variación alta de altitud: {alt_min:.0f}-{alt_max:.0f} m ({altitude_volatility:.1f}% del promedio). Puede afectar deriva, ancho efectivo y uniformidad."
        if altitude_volatility > 180
        else f"Altitud sin señal crítica por índice relativo: variación {altitude_volatility:.1f}% del promedio."
    )
    pattern_uniformity = (
        f"La densidad de líneas por hectárea varía {density_spread:.1f}% entre parcelas; esto puede explicar diferencias de cobertura que no se ven solo con el promedio global."
        if density_spread > 25
        else f"La densidad de líneas por hectárea se mantiene razonablemente uniforme ({density_spread:.1f}% de dispersión)."
    )

    overlap_to_gap = (overlap / uncovered) if uncovered else 0.0
    if overlap_to_gap > 0.45:
        decision_risk = f"Hay {overlap_to_gap:.2f} ha sobre-aplicadas por cada ha sin cubrir: el problema parece más de alineación/solape que de falta de vuelo."
    elif uncovered > 0:
        decision_risk = f"Hay {overlap_to_gap:.2f} ha sobre-aplicadas por cada ha sin cubrir: prioriza completar huecos antes que repetir áreas ya aplicadas."
    else:
        decision_risk = "No hay huecos reportados; la decisión debe enfocarse en validar solapes y calidad de datos."

    return {
        "closureConfidence": closure,
        "concentrationMessage": concentration,
        "speedStability": speed_stability,
        "altitudeStability": altitude_stability,
        "patternUniformity": pattern_uniformity,
        "decisionRisk": decision_risk,
        "_metrics": {
            "riskHa": round(risk_ha, 2),
            "topOneRiskSharePct": round(top_one_share, 1),
            "topThreeRiskSharePct": round(top_three_share, 1),
            "speedVolatilityPct": round(speed_volatility, 1),
            "altitudeVolatilityPct": round(altitude_volatility, 1),
            "lineDensitySpreadPct": round(density_spread, 1),
            "overlapToGapRatio": round(overlap_to_gap, 2),
        },
    }


def _hidden_insights(compact: Dict[str, Any], worst: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    gm = compact["globalMetrics"]
    diagnostics = _advanced_diagnostics(compact, worst)
    metrics = diagnostics.get("_metrics", {})
    total_ha = _num(gm.get("totalHa"))
    uncovered = _num(gm.get("uncoveredHa"))
    overlap = _num(gm.get("overlapHa"))
    coverage = _num(gm.get("coveragePct"))
    insights: List[Dict[str, Any]] = []

    top_zone = worst[0] if worst else None
    if top_zone:
        insights.append({
            "title": "Riesgo concentrado, no distribuido",
            "severity": "alta" if metrics.get("topOneRiskSharePct", 0) >= 35 else "media",
            "metric": f"{metrics.get('topOneRiskSharePct', 0):.1f}% del riesgo en la principal parcela crítica",
            "insight": (
                f"No parece necesario revisar todo el bloque con la misma intensidad. La prioridad real está en "
                f"{top_zone.get('parcelName')} por {top_zone.get('issue')}."
            ),
            "action": "Usar el copiloto para marcar esa parcela en el mapa y enviar al equipo únicamente los polígonos críticos.",
        })

    speed_vol = _num(metrics.get("speedVolatilityPct"))
    if speed_vol > 0:
        insights.append({
            "title": "Volatilidad de velocidad como proxy de uniformidad",
            "severity": "alta" if speed_vol > 55 else "media" if speed_vol > 35 else "baja",
            "metric": f"Rango de velocidad equivalente al {speed_vol:.1f}% del promedio",
            "insight": (
                "Aunque la velocidad media se vea aceptable, el rango puede esconder tramos con dosis efectiva distinta, "
                "especialmente si el caudal no compensó los cambios."
            ),
            "action": "Cruzar tramos de menor/mayor velocidad con capas Sin cubrir y Sobre-aplicado antes de cerrar la operación.",
        })

    alt_vol = _num(metrics.get("altitudeVolatilityPct"))
    if alt_vol > 0:
        insights.append({
            "title": "Altitud variable puede cambiar el ancho efectivo",
            "severity": "alta" if alt_vol > 220 else "media" if alt_vol > 150 else "baja",
            "metric": f"Variación relativa de altitud {alt_vol:.1f}%",
            "insight": (
                "El promedio de altitud no muestra toda la historia: picos altos o bajos pueden provocar deriva, "
                "franjas débiles o exceso en bordes."
            ),
            "action": "Revisar si los picos de altitud coinciden con cabeceras, giros o cambios de relieve.",
        })

    density_spread = _num(metrics.get("lineDensitySpreadPct"))
    if density_spread > 0:
        insights.append({
            "title": "Densidad de líneas por hectárea",
            "severity": "media" if density_spread > 25 else "baja",
            "metric": f"Dispersión estimada {density_spread:.1f}%",
            "insight": (
                "Dos parcelas pueden tener buena cobertura global, pero distinta densidad de pasadas por hectárea. "
                "Esa diferencia ayuda a explicar por qué una parcela queda con más huecos."
            ),
            "action": "Comparar líneas/ha de la parcela con peor cobertura contra la parcela con mejor cobertura para ajustar separación de pasadas.",
        })

    ratio = _num(metrics.get("overlapToGapRatio"))
    if uncovered > 0:
        insights.append({
            "title": "Balance entre huecos y solape",
            "severity": "media" if ratio > 0.25 else "baja",
            "metric": f"{ratio:.2f} ha sobre-aplicadas por cada ha sin cubrir",
            "insight": (
                "Este indicador separa dos problemas distintos: si sube el solape, el patrón necesita alineación; "
                "si domina el hueco, falta completar zonas específicas."
            ),
            "action": "No repetir el vuelo completo: planificar re-aplicación parcial de huecos y ajustar patrón para reducir solape en la próxima operación.",
        })

    if _num(gm.get("totalVolume")) <= 0:
        insights.append({
            "title": "Falta el dato que convierte operación en costo",
            "severity": "media",
            "metric": "Volumen aplicado no disponible",
            "insight": (
                "El análisis puede priorizar hectáreas, pero todavía no puede calcular dosis real, desperdicio de producto "
                "ni costo exacto de sobre-aplicación."
            ),
            "action": "Integrar caudal/volumen y costo por producto para que el copiloto estime ahorro, desperdicio y costo de re-aplicación.",
        })

    if not insights:
        insights.append({
            "title": "Operación sin señales ocultas críticas",
            "severity": "baja",
            "metric": f"Cobertura {coverage:.1f}% sobre {total_ha:.2f} ha",
            "insight": "Los indicadores derivados no muestran anomalías fuertes más allá de la revisión normal de bordes.",
            "action": "Cerrar con revisión visual estándar y guardar este vuelo como referencia histórica.",
        })

    return insights[:6]


def _fallback_report(compact: Dict[str, Any], question: Optional[str] = None, ai_warning: Optional[str] = None) -> Dict[str, Any]:
    gm = compact["globalMetrics"]
    parcels = compact.get("parcelsByRisk", [])
    score = _score({
        "totalHa": gm.get("totalHa"),
        "totalCoveredPct": gm.get("coveragePct"),
        "totalUncoveredHa": gm.get("uncoveredHa"),
        "overlapHa": gm.get("overlapHa"),
        "avgAltitude": gm.get("avgAltitudeM"),
        "avgSpeed": gm.get("avgSpeedKph"),
        "speedRange": gm.get("speedRangeKph"),
        "altitudeRange": gm.get("altitudeRangeM"),
    }, parcels)
    total_ha = _num(gm.get("totalHa"))
    uncovered = _num(gm.get("uncoveredHa"))
    overlap = _num(gm.get("overlapHa"))
    coverage = _num(gm.get("coveragePct"))
    speed_min, speed_max = _range(gm.get("speedRangeKph"))
    alt_min, alt_max = _range(gm.get("altitudeRangeM"))
    worst = _critical_parcels(compact)
    diagnostics = _advanced_diagnostics(compact, worst)
    hidden_insights = _hidden_insights(compact, worst)
    diagnostic_metrics = diagnostics.get("_metrics", {})

    evidence = [
        f"Cobertura global {coverage:.1f}% sobre {total_ha:.2f} ha.",
        f"Área sin cubrir {uncovered:.2f} ha ({_pct(uncovered, total_ha):.1f}%).",
        f"Sobre-aplicación {overlap:.2f} ha ({_pct(overlap, total_ha):.1f}%).",
        f"Velocidad {gm.get('avgSpeedKph', 0):.1f} km/h con rango {speed_min:.0f}-{speed_max:.0f} km/h.",
        f"Altitud {gm.get('avgAltitudeM', 0):.1f} m con rango {alt_min:.0f}-{alt_max:.0f} m.",
        f"Índice de volatilidad de velocidad {diagnostic_metrics.get('speedVolatilityPct', 0):.1f}% y altitud {diagnostic_metrics.get('altitudeVolatilityPct', 0):.1f}%.",
        f"Dispersión de líneas por hectárea {diagnostic_metrics.get('lineDensitySpreadPct', 0):.1f}%.",
    ]

    causes = []
    if speed_max - speed_min > 40:
        causes.append("Variación amplia de velocidad: puede explicar franjas con subcobertura o solape si la dosis/caudal no compensó los cambios.")
    if alt_max - alt_min > 25:
        causes.append("Variación amplia de altitud: revisar estabilidad en cambios de relieve o maniobras de giro.")
    if overlap > total_ha * 0.02:
        causes.append("Sobre-aplicación localizada: normalmente se asocia a giros, bordes o separación menor entre pasadas.")
    if uncovered > total_ha * 0.04:
        causes.append("Área sin cubrir relevante: revisar bordes, pasadas faltantes y continuidad de SprOn.")
    if not causes:
        causes.append("No se observan señales críticas; la revisión debe enfocarse en bordes y cierre de lote.")

    data_quality = []
    if _num(gm.get("totalVolume")) <= 0:
        data_quality.append("No hay volumen aplicado disponible; no se puede validar dosis real ni costo de producto con precisión.")
    if not compact["geometryFlags"].get("hasOverlapGeometry") and overlap > 0:
        data_quality.append("Hay área sobre-aplicada calculada, pero no se recibió geometría detallada de sobreposición para marcarla con máxima precisión.")
    if not data_quality:
        data_quality.append("Los datos principales son suficientes para diagnóstico post-vuelo y priorización de parcelas.")

    answer = None
    if question:
        q = question.strip().lower()
        if "no se ve" in q or "simple vista" in q or "oculto" in q or "oculta" in q:
            first_insight = hidden_insights[0] if hidden_insights else None
            answer = (
                f"Lo más importante que no se ve a simple vista es: {first_insight['title']}. "
                f"{first_insight['insight']} Acción: {first_insight['action']}"
                if first_insight else "No detecté señales ocultas críticas con los KPIs recibidos."
            )
        elif "peor" in q or "crítica" in q or "critica" in q or "prioridad" in q:
            first = worst[0] if worst else None
            answer = f"La prioridad principal es {first['parcelName']} por {first['issue']}." if first else "No hay parcelas críticas destacables con los datos disponibles."
        elif "reaplicar" in q or "re aplicación" in q or "reaplicación" in q:
            answer = f"Conviene reaplicar de forma parcial las zonas sin cubrir: {uncovered:.2f} ha, no todo el bloque de {total_ha:.2f} ha. Prioriza las parcelas con mayor área azul."
        elif "velocidad" in q:
            answer = f"La velocidad media fue {gm.get('avgSpeedKph', 0):.1f} km/h y el rango fue {speed_min:.0f}-{speed_max:.0f} km/h. Una amplitud alta debe cruzarse contra zonas sin cubrir y sobre-aplicadas."
        elif "altitud" in q or "altura" in q:
            answer = f"La altitud media fue {gm.get('avgAltitudeM', 0):.1f} m y el rango fue {alt_min:.0f}-{alt_max:.0f} m. Revisa si los picos coinciden con bordes o cambios de dirección."
        else:
            answer = "Con los datos actuales, la recomendación principal es revisar las zonas sin cubrir, validar sobre-aplicación en bordes y cerrar solo si el criterio operativo acepta la cobertura lograda."

    return {
        "source": "fallback" if not ai_warning else "fallback_with_ai_error",
        "model": None,
        "qualityScore": score,
        "verdict": _severity(score),
        "recommendedAction": _action_from_metrics(coverage, uncovered, overlap, total_ha),
        "executiveSummary": (
            f"La operación alcanzó {coverage:.1f}% de cobertura. Se detectaron {uncovered:.2f} ha sin cubrir y "
            f"{overlap:.2f} ha sobre-aplicadas. El score operativo es {score}/100, por lo que la acción sugerida es: "
            f"{_action_from_metrics(coverage, uncovered, overlap, total_ha)}."
        ),
        "answer": answer,
        "criticalZones": worst,
        "technicalFindings": [
            {"title": "Cobertura", "severity": "alta" if coverage < 90 else "media" if coverage < 94 else "baja", "detail": evidence[0]},
            {"title": "Sin cubrir", "severity": "alta" if _pct(uncovered, total_ha) > 8 else "media" if uncovered > 0 else "baja", "detail": evidence[1]},
            {"title": "Sobre-aplicación", "severity": "alta" if _pct(overlap, total_ha) > 5 else "media" if overlap > 0 else "baja", "detail": evidence[2]},
            {"title": "Velocidad y altitud", "severity": "media" if (speed_max - speed_min > 40 or alt_max - alt_min > 25) else "baja", "detail": f"{evidence[3]} {evidence[4]}"},
        ],
        "probableCauses": causes[:5],
        "hiddenInsights": hidden_insights,
        "operationalDiagnostics": {k: v for k, v in diagnostics.items() if not k.startswith("_")},
        "actionPlan": [
            {"step": 1, "title": "Validar mapa", "detail": "Activar capa Sin cubrir y revisar continuidad de bordes y cabeceras.", "owner": "Operaciones"},
            {"step": 2, "title": "Reaplicar solo lo necesario", "detail": f"Planificar reaplicación parcial sobre {uncovered:.2f} ha en lugar de repetir {total_ha:.2f} ha.", "owner": "Piloto / Campo"},
            {"step": 3, "title": "Ajustar patrón", "detail": "Revisar separación de líneas, giros y estabilidad de velocidad/altitud para la siguiente operación.", "owner": "Supervisor"},
        ],
        "businessImpact": {
            "hectaresAtRisk": round(uncovered, 2),
            "avoidableReflightHa": round(max(total_ha - uncovered, 0), 2),
            "overAppliedHa": round(overlap, 2),
            "message": "Configura costo por hectárea y costo de producto para convertir estas hectáreas en impacto económico exacto.",
        },
        "dataQualityWarnings": data_quality,
        "evidence": evidence,
        "tokenOptimization": {
            "rawGeometrySentToOpenAI": False,
            "parcelRowsSent": len(parcels),
            "strategy": "Se enviaron únicamente KPIs, ranking por parcela y banderas geométricas; las geometrías completas se quedan en Dataris.",
        },
        "aiWarning": ai_warning,
    }


_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "qualityScore": {"type": "integer"},
        "verdict": {"type": "string"},
        "recommendedAction": {"type": "string"},
        "executiveSummary": {"type": "string"},
        "answer": {"type": ["string", "null"]},
        "criticalZones": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "parcelName": {"type": "string"},
                    "priority": {"type": "string"},
                    "riskScore": {"type": "number"},
                    "issue": {"type": "string"},
                    "recommendedAction": {"type": "string"},
                    "hectaresToReview": {"type": "number"},
                },
                "required": ["parcelName", "priority", "riskScore", "issue", "recommendedAction", "hectaresToReview"],
            },
        },
        "technicalFindings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["title", "severity", "detail"],
            },
        },
        "probableCauses": {"type": "array", "items": {"type": "string"}},
        "hiddenInsights": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string"},
                    "metric": {"type": "string"},
                    "insight": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["title", "severity", "metric", "insight", "action"],
            },
        },
        "operationalDiagnostics": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "closureConfidence": {"type": "string"},
                "concentrationMessage": {"type": "string"},
                "speedStability": {"type": "string"},
                "altitudeStability": {"type": "string"},
                "patternUniformity": {"type": "string"},
                "decisionRisk": {"type": "string"},
            },
            "required": ["closureConfidence", "concentrationMessage", "speedStability", "altitudeStability", "patternUniformity", "decisionRisk"],
        },
        "actionPlan": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "step": {"type": "integer"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "owner": {"type": "string"},
                },
                "required": ["step", "title", "detail", "owner"],
            },
        },
        "businessImpact": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "hectaresAtRisk": {"type": "number"},
                "avoidableReflightHa": {"type": "number"},
                "overAppliedHa": {"type": "number"},
                "message": {"type": "string"},
            },
            "required": ["hectaresAtRisk", "avoidableReflightHa", "overAppliedHa", "message"],
        },
        "dataQualityWarnings": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "qualityScore", "verdict", "recommendedAction", "executiveSummary", "answer", "criticalZones",
        "technicalFindings", "probableCauses", "hiddenInsights", "operationalDiagnostics", "actionPlan",
        "businessImpact", "dataQualityWarnings", "evidence",
    ],
}


def _extract_output_text(response_json: Dict[str, Any]) -> str:
    if isinstance(response_json.get("output_text"), str):
        return response_json["output_text"]
    chunks: List[str] = []
    for item in response_json.get("output", []) or []:
        for content in item.get("content", []) or []:
            if isinstance(content.get("text"), str):
                chunks.append(content["text"])
    return "\n".join(chunks).strip()


def _ai_enrichment_schema() -> Dict[str, Any]:
    """Schema pequeño para que OpenAI enriquezca el diagnóstico sin generar un JSON enorme.

    La mayor parte de cálculos duros se mantiene determinística en Dataris. OpenAI solo redacta
    conclusiones, señales ocultas y plan de acción. Esto baja tokens y evita respuestas truncadas.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "recommendedAction": {"type": "string"},
            "executiveSummary": {"type": "string"},
            "answer": {"type": ["string", "null"]},
            "probableCauses": {"type": "array", "items": {"type": "string"}},
            "hiddenInsights": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "severity": {"type": "string"},
                        "metric": {"type": "string"},
                        "insight": {"type": "string"},
                        "action": {"type": "string"},
                    },
                    "required": ["title", "severity", "metric", "insight", "action"],
                },
            },
            "operationalDiagnostics": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "closureConfidence": {"type": "string"},
                    "concentrationMessage": {"type": "string"},
                    "speedStability": {"type": "string"},
                    "altitudeStability": {"type": "string"},
                    "patternUniformity": {"type": "string"},
                    "decisionRisk": {"type": "string"},
                },
                "required": [
                    "closureConfidence", "concentrationMessage", "speedStability",
                    "altitudeStability", "patternUniformity", "decisionRisk",
                ],
            },
            "actionPlan": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "step": {"type": "integer"},
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                        "owner": {"type": "string"},
                    },
                    "required": ["step", "title", "detail", "owner"],
                },
            },
            "managementNote": {"type": "string"},
        },
        "required": [
            "recommendedAction", "executiveSummary", "answer", "probableCauses", "hiddenInsights",
            "operationalDiagnostics", "actionPlan", "managementNote",
        ],
    }


def _openai_context(compact: Dict[str, Any], question: Optional[str], base: Dict[str, Any]) -> Dict[str, Any]:
    """Construye un contexto compacto para OpenAI sin geometrías pesadas."""
    return {
        "question": (question or "Genera diagnóstico ejecutivo del vuelo, señales ocultas y plan de acción.")[:700],
        "flight": compact,
        "deterministicBaseline": {
            "qualityScore": base.get("qualityScore"),
            "verdict": base.get("verdict"),
            "recommendedAction": base.get("recommendedAction"),
            "criticalZones": base.get("criticalZones", [])[:6],
            "technicalFindings": base.get("technicalFindings", [])[:4],
            "operationalDiagnostics": base.get("operationalDiagnostics", {}),
            "dataQualityWarnings": base.get("dataQualityWarnings", []),
            "businessImpact": base.get("businessImpact", {}),
            "tokenOptimization": base.get("tokenOptimization", {}),
        },
        "instructions": [
            "No recalcules geometrías; interpreta los KPIs recibidos.",
            "No inventes volumen, dosis, clima ni costo si no aparecen en el JSON.",
            "Haz hallazgos no obvios: concentración del riesgo, estabilidad, densidad de líneas, balance hueco/solape y decisión de cierre.",
            "Sé breve: máximo 4 causas, máximo 4 señales ocultas y máximo 3 acciones.",
            "Usa nombres reales de parcelas y evidencia numérica exacta.",
            "Si la pregunta del usuario es específica, answer debe responder esa pregunta; si no, answer puede ser null.",
        ],
    }


def _merge_openai_enrichment(base: Dict[str, Any], ai: Dict[str, Any], model: str, compact: Dict[str, Any]) -> Dict[str, Any]:
    """Combina cálculos locales confiables con redacción/criterio de OpenAI."""
    merged = dict(base)
    for key in (
        "recommendedAction",
        "executiveSummary",
        "answer",
        "probableCauses",
        "hiddenInsights",
        "operationalDiagnostics",
        "actionPlan",
    ):
        value = ai.get(key)
        if value not in (None, "", [], {}):
            merged[key] = value

    note = ai.get("managementNote")
    if note:
        evidence = list(merged.get("evidence") or [])
        evidence.append(f"Nota IA: {str(note)[:350]}")
        merged["evidence"] = evidence[:10]

    merged["source"] = "openai"
    merged["model"] = model
    merged["aiWarning"] = None
    merged["tokenOptimization"] = {
        "rawGeometrySentToOpenAI": False,
        "parcelRowsSent": len(compact.get("parcelsByRisk", [])),
        "strategy": "Se enviaron únicamente KPIs, ranking por parcela, diagnóstico local y banderas geométricas; las geometrías completas se quedan en Dataris.",
    }
    return merged


async def _call_openai(compact: Dict[str, Any], question: Optional[str]) -> Dict[str, Any]:
    api_key = (
        getattr(settings, "OPENAI_API_KEY", None)
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENAI_API_TOKEN")
        or os.getenv("CHATGPT_API_KEY")
    )

    base_report = _fallback_report(compact, question, None)

    if not api_key:
        return _fallback_report(
            compact,
            question,
            "OPENAI_API_KEY no está configurada en el backend. El resultado mostrado es diagnóstico local determinístico, no una respuesta de OpenAI.",
        )

    model = getattr(settings, "OPENAI_AERIAL_COPILOT_MODEL", "gpt-4.1-mini")
    configured_tokens = int(getattr(settings, "OPENAI_AERIAL_COPILOT_MAX_OUTPUT_TOKENS", 1800) or 1800)
    # El schema anterior era demasiado grande y podía truncarse con 1400 tokens.
    # Este schema es menor, pero se deja un piso prudente para evitar JSON incompleto.
    max_output_tokens = max(1800, configured_tokens)
    timeout_seconds = float(getattr(settings, "OPENAI_AERIAL_COPILOT_TIMEOUT_SECONDS", 25) or 25)

    system = (
        "Eres el Copiloto IA de Aplicación Aérea de Dataris. Analizas vuelos de riego/fumigación con helicóptero. "
        "Dataris ya calculó geometrías y KPIs; tú debes enriquecer el diagnóstico con criterio operativo, gerencial y agronómico. "
        "No inventes datos. No digas que viste geometrías si solo recibiste banderas. "
        "Sé preciso, accionable, profesional y conciso. Responde en español. "
        "Devuelve solo el JSON del schema solicitado."
    )
    user_payload = _openai_context(compact, question, base_report)

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))},
        ],
        "max_output_tokens": max_output_tokens,
        "store": False,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "aerial_copilot_enrichment",
                "schema": _ai_enrichment_schema(),
                "strict": True,
            }
        },
    }

    async def _post_and_parse(token_budget: int) -> Dict[str, Any]:
        request_body = dict(body)
        request_body["max_output_tokens"] = token_budget
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_body,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI respondió {response.status_code}: {response.text[:500]}")
        response_json = response.json()
        if response_json.get("status") == "incomplete":
            details = response_json.get("incomplete_details") or {}
            reason = details.get("reason") or "respuesta incompleta"
            raise ValueError(f"OpenAI devolvió respuesta incompleta: {reason}")
        output_text = _extract_output_text(response_json)
        if not output_text:
            raise ValueError("OpenAI no devolvió texto estructurado para analizar.")
        return json.loads(output_text)

    try:
        try:
            parsed = await _post_and_parse(max_output_tokens)
        except (json.JSONDecodeError, ValueError) as first_error:
            # Un retry controlado: solo ocurre cuando la respuesta quedó incompleta/truncada.
            retry_tokens = min(max(max_output_tokens * 2, 2600), 3600)
            if retry_tokens <= max_output_tokens:
                raise first_error
            parsed = await _post_and_parse(retry_tokens)
        return _merge_openai_enrichment(base_report, parsed, model, compact)
    except Exception as exc:
        return _fallback_report(compact, question, f"No se pudo consultar OpenAI correctamente: {exc}")


async def process_aerial_copilot(payload: Dict[str, Any]) -> Dict[str, Any]:
    compact = _compact_payload(payload)
    question = payload.get("question")
    if question is not None:
        question = str(question)[:700]
    result = await _call_openai(compact, question)
    result["compactInput"] = compact
    return result
