from __future__ import annotations

import json
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

    evidence = [
        f"Cobertura global {coverage:.1f}% sobre {total_ha:.2f} ha.",
        f"Área sin cubrir {uncovered:.2f} ha ({_pct(uncovered, total_ha):.1f}%).",
        f"Sobre-aplicación {overlap:.2f} ha ({_pct(overlap, total_ha):.1f}%).",
        f"Velocidad {gm.get('avgSpeedKph', 0):.1f} km/h con rango {speed_min:.0f}-{speed_max:.0f} km/h.",
        f"Altitud {gm.get('avgAltitudeM', 0):.1f} m con rango {alt_min:.0f}-{alt_max:.0f} m.",
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
        if "peor" in q or "crítica" in q or "critica" in q or "prioridad" in q:
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
        "technicalFindings", "probableCauses", "actionPlan", "businessImpact", "dataQualityWarnings", "evidence",
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


async def _call_openai(compact: Dict[str, Any], question: Optional[str]) -> Dict[str, Any]:
    api_key = getattr(settings, "OPENAI_API_KEY", None)
    if not api_key:
        return _fallback_report(compact, question, None)

    model = getattr(settings, "OPENAI_AERIAL_COPILOT_MODEL", "gpt-4.1-mini")
    max_output_tokens = int(getattr(settings, "OPENAI_AERIAL_COPILOT_MAX_OUTPUT_TOKENS", 1400) or 1400)
    timeout_seconds = float(getattr(settings, "OPENAI_AERIAL_COPILOT_TIMEOUT_SECONDS", 25) or 25)

    system = (
        "Eres el Copiloto de Aplicación Aérea de Dataris. Analizas vuelos de riego/fumigación con helicóptero. "
        "Debes ser preciso, útil para gerencia y operaciones, y no inventar datos. "
        "Usa únicamente el JSON recibido. No digas que viste geometrías si solo recibiste banderas. "
        "Prioriza acciones concretas: cerrar, inspeccionar, reaplicar parcialmente o revisar operación. "
        "Responde en español profesional."
    )
    user_payload = {
        "question": (question or "Genera diagnóstico completo del vuelo, recomendaciones, riesgos y valor de negocio.")[:700],
        "flight": compact,
        "rules": [
            "No pidas repetir todo si basta una reaplicación parcial según hectáreas sin cubrir.",
            "Incluye evidencia numérica exacta de KPIs recibidos.",
            "Marca advertencias cuando falte volumen, dosis o costo.",
            "criticalZones debe usar nombres reales de parcelas del JSON.",
        ],
    }

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
                "name": "aerial_copilot_report",
                "schema": _RESPONSE_SCHEMA,
                "strict": True,
            }
        },
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        if response.status_code >= 400:
            return _fallback_report(compact, question, f"OpenAI respondió {response.status_code}: {response.text[:300]}")
        output_text = _extract_output_text(response.json())
        parsed = json.loads(output_text)
        parsed["source"] = "openai"
        parsed["model"] = model
        parsed["tokenOptimization"] = {
            "rawGeometrySentToOpenAI": False,
            "parcelRowsSent": len(compact.get("parcelsByRisk", [])),
            "strategy": "Se enviaron únicamente KPIs, ranking por parcela y banderas geométricas; las geometrías completas se quedan en Dataris.",
        }
        parsed["aiWarning"] = None
        return parsed
    except Exception as exc:
        return _fallback_report(compact, question, f"No se pudo consultar OpenAI: {exc}")


async def process_aerial_copilot(payload: Dict[str, Any]) -> Dict[str, Any]:
    compact = _compact_payload(payload)
    question = payload.get("question")
    if question is not None:
        question = str(question)[:700]
    result = await _call_openai(compact, question)
    result["compactInput"] = compact
    return result
