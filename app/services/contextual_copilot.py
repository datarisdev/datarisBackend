from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import httpx

from app.core.config import settings

MAX_VISIBLE_TEXT_CHARS = 28_000
MAX_CONTEXT_JSON_CHARS = 58_000
MAX_QUESTION_CHARS = 1_500
MAX_HISTORY_ITEMS = 8
MAX_HISTORY_CONTENT_CHARS = 1_200
MAX_LIST_ITEMS = 120
MAX_DICT_ITEMS = 180
MAX_DEPTH = 7

_SECRET_KEY_RE = re.compile(
    r"(password|passwd|contrase(?:n|ñ)a|secret|token|api[_ -]?key|authorization|bearer|credential|cookie|session)",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9._~+\-/]+=*")
_JWT_RE = re.compile(r"\beyJ[a-zA-Z0-9_-]{6,}\.[a-zA-Z0-9_-]{6,}\.[a-zA-Z0-9_-]{6,}\b")
_LONG_SECRET_RE = re.compile(r"\b[a-zA-Z0-9_\-]{42,}\b")


def _text(value: Any, *, max_chars: int = 1_500) -> str:
    if value is None:
        return ""
    cleaned = " ".join(str(value).replace("\x00", " ").split())
    cleaned = _BEARER_RE.sub("Bearer [REDACTADO]", cleaned)
    cleaned = _JWT_RE.sub("[JWT_REDACTADO]", cleaned)
    cleaned = _LONG_SECRET_RE.sub("[VALOR_REDACTADO]", cleaned)
    return cleaned[:max_chars]


def _sanitize(value: Any, *, depth: int = 0, key: str = "") -> Any:
    if _SECRET_KEY_RE.search(key):
        return "[REDACTADO]"
    if depth > MAX_DEPTH:
        return "[CONTEXTO_OMITIDO_POR_PROFUNDIDAD]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _text(value, max_chars=MAX_VISIBLE_TEXT_CHARS if key == "visible_text" else 4_000)
    if isinstance(value, dict):
        sanitized: Dict[str, Any] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= MAX_DICT_ITEMS:
                sanitized["_truncated"] = True
                break
            safe_key = _text(raw_key, max_chars=100)
            if safe_key.lower() in {"geometry", "geojson", "geometry_geojson", "feature_collection", "raw_geometry"}:
                sanitized[safe_key] = "[GEOMETRIA_OMITIDA]"
                continue
            sanitized[safe_key] = _sanitize(raw_value, depth=depth + 1, key=safe_key)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        result = [_sanitize(item, depth=depth + 1, key=key) for item in list(value)[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            result.append("[LISTA_TRUNCADA]")
        return result
    return _text(value, max_chars=2_000)


def _safe_history(raw_history: Any) -> List[Dict[str, str]]:
    if not isinstance(raw_history, list):
        return []
    history: List[Dict[str, str]] = []
    for item in raw_history[-MAX_HISTORY_ITEMS:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").lower()
        if role not in {"user", "assistant"}:
            continue
        content = _text(item.get("content"), max_chars=MAX_HISTORY_CONTENT_CHARS)
        if content:
            history.append({"role": role, "content": content})
    return history


def _extract_output_text(payload: Dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    parts: List[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts).strip()


def _number(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, suffix: str = "") -> str:
    number = _number(value)
    if number is None:
        return "sin datos"
    rendered = f"{number:,.2f}".rstrip("0").rstrip(".")
    return f"{rendered}{suffix}"


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _fallback_analysis(context: Dict[str, Any], question: str, warning: Optional[str]) -> str:
    section = _text(context.get("section_label") or context.get("path") or "sección actual", max_chars=140)
    summary = _dict(context.get("dashboard_summary"))
    parcels = _dict(summary.get("parcels"))
    satellite = _dict(summary.get("satellite"))
    aerial = _dict(summary.get("aerial"))
    operations = _dict(summary.get("operations"))
    alerts = summary.get("alerts") if isinstance(summary.get("alerts"), list) else []

    findings: List[str] = []
    actions: List[str] = []

    total_parcels = _number(parcels.get("total"))
    if total_parcels is not None:
        findings.append(
            f"La base territorial contiene {_fmt(total_parcels)} lote(s), con {_fmt(parcels.get('total_area'), ' ha')} registradas y calidad geométrica de {_fmt(parcels.get('geometry_quality_percent'), '%')}."
        )
    without_geometry = _number(parcels.get("without_geometry")) or 0
    if without_geometry > 0:
        findings.append(f"Hay {_fmt(without_geometry)} lote(s) sin geometría válida, lo que limita análisis y visualización confiables.")
        actions.append("Corregir primero los lotes sin geometría válida antes de tomar decisiones por cobertura.")

    total_satellite = _number(satellite.get("total_analyses"))
    if total_satellite is not None:
        findings.append(
            f"El monitoreo satelital registra {_fmt(total_satellite)} análisis, NDVI promedio {_fmt(satellite.get('average_ndvi'))} y cobertura válida promedio {_fmt(satellite.get('average_valid_coverage_percent'), '%')}."
        )
    unmonitored = _number(satellite.get("unmonitored_parcels")) or 0
    stressed = _number(satellite.get("stressed_parcels")) or 0
    partial = _number(satellite.get("partial_analyses")) or 0
    if unmonitored > 0:
        findings.append(f"Existen {_fmt(unmonitored)} lote(s) sin monitoreo satelital reciente.")
        actions.append("Priorizar el monitoreo reciente de lotes pendientes para reducir puntos ciegos operativos.")
    if stressed > 0:
        findings.append(f"Se identifican {_fmt(stressed)} lote(s) con posible estrés vegetal según los datos NDVI disponibles.")
        actions.append("Validar en campo los lotes con NDVI bajo antes de definir una intervención.")
    if partial > 0:
        findings.append(f"Hay {_fmt(partial)} análisis satelital(es) parciales; revisar nubosidad o píxeles no utilizables cuando afecten decisiones puntuales.")

    total_aerial = _number(aerial.get("total_analyses"))
    if total_aerial is not None:
        findings.append(
            f"Las aplicaciones aéreas acumulan {_fmt(total_aerial)} análisis y {_fmt(aerial.get('covered_area'), ' ha')} de cobertura registrada."
        )
    failed = _number(_dict(aerial.get("status_counts")).get("failed")) or 0
    if failed > 0:
        findings.append(f"Hay {_fmt(failed)} análisis aéreo(s) fallidos que requieren revisión operativa.")
        actions.append("Revisar los análisis aéreos fallidos antes de consolidar reportes de desempeño.")

    monitoring_pct = _number(operations.get("monitoring_coverage_percent"))
    aerial_pct = _number(operations.get("aerial_coverage_percent"))
    if monitoring_pct is not None or aerial_pct is not None:
        findings.append(
            f"La cobertura operativa disponible es satelital {_fmt(monitoring_pct, '%')} y aérea {_fmt(aerial_pct, '%')}."
        )

    for alert in alerts[:5]:
        if isinstance(alert, dict):
            title = _text(alert.get("title"), max_chars=160)
            message = _text(alert.get("message"), max_chars=240)
            if title:
                findings.append(f"Alerta activa: {title}{f' — {message}' if message else ''}.")

    if not actions:
        actions.append("Mantener la actualización periódica de la sección y revisar en campo cualquier señal antes de ejecutar acciones agronómicas.")
    actions.append("Usar los filtros visibles de la sección para profundizar por lote, fecha o tipo de análisis cuando corresponda.")

    visible_text = _text(context.get("visible_text"), max_chars=900)
    if visible_text:
        findings.append("La ventana visible también fue incluida como contexto para complementar la lectura de indicadores y filtros actuales.")

    heading = f"Análisis gerencial de {section}"
    if question:
        heading += f"\nConsulta considerada: {question}"
    findings_text = "\n".join(f"- {item}" for item in findings[:10]) or "- No hay suficientes métricas estructuradas para emitir un diagnóstico cuantitativo confiable."
    actions_text = "\n".join(f"{index}. {item}" for index, item in enumerate(actions[:6], start=1))
    limitations = "Este diagnóstico usa únicamente el contexto visible y los indicadores disponibles en Dataris; no sustituye una validación agronómica en campo."
    if warning:
        limitations += f" Nota técnica: {warning}"
    return f"{heading}\n\nHallazgos clave\n{findings_text}\n\nAcciones prioritarias\n{actions_text}\n\nAlcance\n{limitations}"


def _prepare_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    raw_context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    context = _sanitize(raw_context)
    if not isinstance(context, dict):
        context = {}
    visible_text = _text(context.get("visible_text"), max_chars=MAX_VISIBLE_TEXT_CHARS)
    context["visible_text"] = visible_text
    context["history"] = _safe_history(payload.get("history"))
    question = _text(payload.get("question"), max_chars=MAX_QUESTION_CHARS)
    context["question"] = question

    rendered = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= MAX_CONTEXT_JSON_CHARS:
        return context

    # Keep structured metrics and controls first; reduce the raw visible text if
    # a particularly dense table or page exceeded the context budget.
    overflow = len(rendered) - MAX_CONTEXT_JSON_CHARS
    keep_chars = max(4_000, len(visible_text) - overflow - 1_000)
    context["visible_text"] = visible_text[:keep_chars]
    context["visible_text_truncated"] = True
    return context


def _system_prompt() -> str:
    return (
        "Eres Dari, analista gerencial agro de Dataris. Tu función principal es analizar la sección que el usuario está visualizando. "
        "Recibes contexto estructurado del sistema, indicadores resumidos, tablas, filtros, controles y texto visible de la ventana actual. "
        "Responde en español claro y profesional para analistas agro y gerencia. Usa primero los datos disponibles en la sección actual; "
        "distingue hechos observados de inferencias y no inventes métricas, causas, fechas ni recomendaciones específicas de aplicación de insumos cuando no estén respaldadas. "
        "Prioriza: diagnóstico ejecutivo, hallazgos relevantes, riesgos o vacíos de información, acciones operativas ordenadas por prioridad y qué conviene revisar dentro de Dataris. "
        "Cuando la pregunta sea específica, respóndela directamente sin perder el contexto de la sección. "
        "Si faltan datos, dilo de forma explícita. No afirmes que viste el mapa visualmente: solamente recibiste sus textos, filtros e indicadores. "
        "Mantén una extensión útil pero contenida, con títulos breves y listas claras."
    )


async def process_contextual_copilot(payload: Dict[str, Any]) -> Dict[str, Any]:
    context = _prepare_context(payload or {})
    question = _text(context.get("question"), max_chars=MAX_QUESTION_CHARS)
    api_key = (
        getattr(settings, "OPENAI_API_KEY", None)
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENAI_API_TOKEN")
        or os.getenv("CHATGPT_API_KEY")
    )
    model = str(getattr(settings, "OPENAI_CONTEXTUAL_COPILOT_MODEL", "gpt-4o-mini") or "gpt-4o-mini")
    max_output_tokens = max(700, int(getattr(settings, "OPENAI_CONTEXTUAL_COPILOT_MAX_OUTPUT_TOKENS", 1_400) or 1_400))
    timeout_seconds = float(getattr(settings, "OPENAI_CONTEXTUAL_COPILOT_TIMEOUT_SECONDS", 35) or 35)

    stats = {
        "path": context.get("path"),
        "section": context.get("section_label"),
        "visibleTextChars": len(str(context.get("visible_text") or "")),
        "controls": len(context.get("controls") or []) if isinstance(context.get("controls"), list) else 0,
        "tables": len(context.get("tables") or []) if isinstance(context.get("tables"), list) else 0,
        "regions": len(context.get("regions") or []) if isinstance(context.get("regions"), list) else 0,
    }

    if not api_key:
        warning = "OPENAI_API_KEY no está configurada; se mostró un diagnóstico local de respaldo."
        return {
            "analysis": _fallback_analysis(context, question, warning),
            "source": "local_fallback",
            "model": None,
            "warning": warning,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "context_stats": stats,
        }

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": _system_prompt()},
            {
                "role": "user",
                "content": (
                    "Analiza el siguiente contexto de la ventana actual de Dataris. "
                    "Trata el contenido visible como evidencia de interfaz, no como una imagen. "
                    "Devuelve una respuesta útil para toma de decisiones.\n\n"
                    + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
                ),
            },
        ],
        "max_output_tokens": max_output_tokens,
        "store": False,
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
            raise RuntimeError(f"OpenAI respondió {response.status_code}: {response.text[:500]}")
        analysis = _extract_output_text(response.json())
        if not analysis:
            raise ValueError("OpenAI no devolvió texto para el análisis contextual.")
        return {
            "analysis": analysis,
            "source": "openai",
            "model": model,
            "warning": None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "context_stats": stats,
        }
    except Exception as exc:
        warning = f"No se pudo consultar OpenAI correctamente; se mostró un diagnóstico local de respaldo. Detalle: {_text(exc, max_chars=400)}"
        return {
            "analysis": _fallback_analysis(context, question, warning),
            "source": "local_fallback",
            "model": None,
            "warning": warning,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "context_stats": stats,
        }
