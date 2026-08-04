"""Cuentas embebidas de Graniot: quién tiene fincas y cómo darle su portal.

Graniot mantiene **dos padrones de usuarios separados** (confirmado por su CTO):

- **Usuarios de plataforma**: los que entran en app.graniot.com. Son los dueños
  de las fincas. No hay ningún endpoint que los liste como tales.
- **Usuarios de mapa embebido**: los únicos que ``/api/accounts/`` devuelve y los
  únicos que pueden abrir ``embed.graniot.com``. Solo se dan de alta por API.

Por eso el módulo Satélite solo sabía servir el portal de las 3 cuentas de la
sección API: el resto de clientes de Dataris son usuarios de plataforma y ahí no
aparecen. La única vía para que alguien vea sus fincas en el mapa embebido es
crear una cuenta embebida y **asignarle las fincas de su usuario de plataforma**.

Este módulo aporta las dos piezas que faltaban para automatizar eso:

1. ``fetch_company_farms`` / ``index_platform_users``: ``/api/company/farms/``
   sí devuelve cada finca con sus ``responsibles`` (email + id numérico), así que
   de una sola llamada sale el padrón completo de usuarios de plataforma con
   fincas y el ``client_id`` numérico con el que actuar en su nombre.
2. ``create_embed_account`` / ``link_farm_to_account``: alta de la cuenta
   embebida y asignación de las fincas de esa persona.

Todas las funciones son puras respecto al almacenamiento: reciben el cliente ya
configurado y no tocan la base de datos de Dataris.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from app.services.graniot_client import GraniotAPIError, GraniotClient

# Máximo de páginas al recorrer un listado paginado de Graniot. Leer solo la
# primera página escondería fincas (y con ellas, usuarios sin portal).
MAX_PAGES = 40

# Cuerpo de POST /api/farms/{id}/managers/. Graniot no lo documenta, pero su
# propio error lo dicta: "The request has to have a list like
# [{"account_id": "manager1"}, {"account_id": "manager2"}]".
def _managers_body(account_ids: Iterable[Any]) -> List[Dict[str, Any]]:
    return [{"account_id": str(value)} for value in account_ids if str(value or "").strip()]


def items(payload: Any) -> List[Dict[str, Any]]:
    """Elementos de una respuesta de Graniot (lista plana o paginada DRF)."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("results", "data", "items", "features"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


async def fetch_company_farms(
    client: GraniotClient,
    *,
    operation: str = "graniot-company-farms",
) -> List[Dict[str, Any]]:
    """Fincas de la empresa con sus responsables.

    ``/api/company/farms/`` ve *todas* las fincas de la empresa de la API key
    (412 frente a las 19 de ``/api/farms/``) y, a diferencia de este, cada finca
    trae ``responsibles`` con ``id`` numérico, ``email`` y ``name``.
    """
    farms: List[Dict[str, Any]] = []
    seen_paths: set[str] = set()
    path: Optional[str] = "/api/company/farms/"

    for page in range(MAX_PAGES):
        if not path or path in seen_paths:
            break
        seen_paths.add(path)
        payload = await client.get(
            path,
            include_client_id=False,
            debug_context={"operation": operation, "page": page + 1},
        )
        farms.extend(items(payload))
        next_url = payload.get("next") if isinstance(payload, dict) else None
        path = str(next_url) if next_url else None

    deduped: Dict[str, Dict[str, Any]] = {}
    for farm in farms:
        deduped.setdefault(str(farm.get("id") or farm.get("key") or len(deduped)), farm)
    return list(deduped.values())


def index_platform_users(farms: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Padrón ``email -> {user_id, name, farms}`` a partir de las fincas.

    El ``user_id`` es el id numérico de Graniot, que es justo lo que su API
    acepta como ``client_id`` para actuar en nombre de esa persona (verificado:
    con él se listan sus fincas, mientras que el ``acc-<uuid>`` da HTTP 500).
    """
    index: Dict[str, Dict[str, Any]] = {}
    for farm in farms:
        farm_id = farm.get("id")
        if farm_id is None:
            continue
        farm_entry = {"id": farm_id, "name": farm.get("name"), "area": farm.get("area")}
        for responsible in farm.get("responsibles") or []:
            if not isinstance(responsible, dict):
                continue
            email = normalize_email(responsible.get("email"))
            if not email or "@" not in email:
                continue
            entry = index.setdefault(email, {
                "email": email,
                "user_id": responsible.get("id"),
                "name": responsible.get("name"),
                "farms": [],
            })
            if entry.get("user_id") is None:
                entry["user_id"] = responsible.get("id")
            if not any(existing["id"] == farm_id for existing in entry["farms"]):
                entry["farms"].append(farm_entry)
    return index


def embed_alias(email: str, user_id: Any, template: str, *, attempt: int = 0) -> str:
    """Correo con el que se da de alta la cuenta embebida de una persona.

    Graniot rechaza dar de alta una cuenta embebida con el correo de un usuario
    de plataforma existente (``An account with this email already exists``), así
    que la cuenta embebida necesita un correo propio. El alias lleva dentro el id
    numérico del usuario de plataforma para que el vínculo se pueda reconstruir
    mirando solo ``/api/accounts/``, aunque se pierda el registro local.

    ``attempt`` añade un sufijo para cuando el alias ya está quemado: dar de baja
    una cuenta embebida en Graniot **no la borra, la desactiva**, y a partir de
    ahí ese correo ni se lista ni se puede volver a dar de alta por API
    ("...already exists but is deactivated"). Sin un alias alternativo, ese
    usuario se quedaría sin portal para siempre.
    """
    local, _, domain = normalize_email(email).partition("@")
    alias = template.format(
        uid=str(user_id or "").strip() or "na",
        email=normalize_email(email),
        local=local or "usuario",
        domain=domain or "dataris.es",
    )
    if attempt <= 0:
        return alias
    alias_local, _, alias_domain = alias.partition("@")
    return f"{alias_local}-{attempt}@{alias_domain}" if alias_domain else f"{alias}-{attempt}"


def platform_user_id_from_alias(account_email: Any, template: str) -> Optional[str]:
    """Id numérico del usuario de plataforma codificado en un alias de embed."""
    prefix, _, suffix = template.partition("{uid}")
    value = normalize_email(account_email)
    prefix = prefix.format(uid="", email="", local="", domain="").lower()
    suffix = suffix.format(uid="", email="", local="", domain="").lower()
    if not prefix or not value.startswith(prefix) or not value.endswith(suffix):
        return None
    uid = value[len(prefix):len(value) - len(suffix) if suffix else None]
    # Los alias de reintento llevan "-2", "-3"… detrás del id.
    uid = uid.split("-")[0]
    return uid if uid.isdigit() else None


def alias_already_taken(exc: Exception) -> bool:
    """¿Graniot rechazó el alta porque ese correo ya está cogido?

    Cubre la cuenta activa (bastaría con releer ``/api/accounts/``) y la
    desactivada, que no aparece en ningún listado y solo se puede reactivar o
    borrar desde la aplicación de Graniot.
    """
    return "already exists" in str(exc or "").lower()


async def create_embed_account(
    client: GraniotClient,
    account_email: str,
    *,
    operation: str = "graniot-create-embed-account",
) -> Dict[str, Any]:
    """Da de alta una cuenta de mapa embebido y devuelve la cuenta creada.

    El alta solo acepta el correo: pasarle las fincas no da error pero se ignora
    (verificado contra la API real), así que se asignan después una a una.
    """
    created = await client.post(
        "/api/accounts/",
        json_body={"account_email": account_email},
        params=None,
        debug_context={"operation": operation, "account_email": account_email},
    )
    if isinstance(created, list):
        created = next(
            (
                item
                for item in items(created)
                if normalize_email(item.get("account_email")) == normalize_email(account_email)
            ),
            None,
        ) or (items(created)[0] if items(created) else None)
    if not isinstance(created, dict) or not created.get("account_email"):
        raise GraniotAPIError(502, "Graniot no devolvió la cuenta embebida creada", created)
    return created


async def farm_managers(
    client: GraniotClient,
    farm_id: Any,
    *,
    operation: str = "graniot-farm-managers",
) -> List[Dict[str, Any]]:
    """Gestores de una finca (``account_id`` = ``acc-…`` o el correo de la persona)."""
    try:
        payload = await client.get(
            f"/api/farms/{farm_id}/managers/",
            include_client_id=False,
            debug_context={"operation": operation, "farm_id": farm_id},
        )
    except GraniotAPIError as exc:
        if exc.status_code in {404, 405}:
            # Fincas antiguas sin gestores: no es un error, simplemente no tiene.
            return []
        raise
    return items(payload)


def account_is_manager(payload: Any, account_id: str) -> bool:
    """¿Aparece esa cuenta entre los gestores devueltos por Graniot?"""
    wanted = str(account_id or "").strip()
    return any(str(item.get("account_id") or "").strip() == wanted for item in items(payload))


async def link_farm_to_account(
    client: GraniotClient,
    farm_id: Any,
    account_id: str,
    *,
    owner_client_id: Optional[str] = None,
    operation: str = "graniot-link-farm",
) -> Any:
    """Asigna una finca a una cuenta embebida (la hace visible en su portal).

    Verificado contra la API real: la lista **suma** gestores sin tocar a los que
    ya estaban, repetirla no duplica nada, y Graniot responde con la lista
    completa de gestores de la finca, así que la respuesta sirve para comprobar
    que el alta surtió efecto.
    """
    return await client.post(
        f"/api/farms/{farm_id}/managers/",
        json_body=_managers_body([account_id]),
        params={"client_id": owner_client_id} if owner_client_id else None,
        debug_context={"operation": operation, "farm_id": farm_id, "account_id": account_id},
    )


async def unlink_farm_from_account(
    client: GraniotClient,
    farm_id: Any,
    account_id: str,
    *,
    operation: str = "graniot-unlink-farm",
) -> bool:
    """Retira una finca de una cuenta embebida. Devuelve si Graniot lo aceptó.

    Se pide con la misma lista con la que se dio de alta (Graniot responde 204).
    """
    try:
        await client.delete(
            f"/api/farms/{farm_id}/managers/",
            json_body=_managers_body([account_id]),
            debug_context={"operation": operation, "farm_id": farm_id, "account_id": account_id},
        )
        return True
    except GraniotAPIError as exc:
        if exc.status_code in {404, 410}:
            # Ya no estaba asignada: el resultado es el que se buscaba.
            return True
        raise
