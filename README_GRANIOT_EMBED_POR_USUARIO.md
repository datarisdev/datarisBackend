# Mapa embebido de Graniot: el portal propio de cada cliente

## El problema

El módulo **Satélite** embebe el portal de Graniot (`embed.graniot.com/?auth_id=…`).
Hasta ahora Dataris solo sabía servir el portal de las cuentas que aparecen en
`GET /api/accounts/` de Graniot — tres — y cualquier otro cliente terminaba
mirando el portal de la cuenta de servicio, con fincas que no son suyas.

## Por qué pasaba (respuesta de Graniot, 4 ago 2026)

> En nuestra plataforma mantenemos separados los dos tipos de usuarios: por un
> lado los usuarios de plataforma (los que acceden directamente a la aplicación)
> y por otro los usuarios dedicados al mapa embebido. El endpoint
> `/api/accounts/` solo devuelve estos últimos, por eso veis únicamente las 3
> cuentas de la sección de API.
>
> A día de hoy, la única forma de ver las fincas de un usuario de plataforma en
> el mapa embebido es crear un nuevo usuario de tipo embebido y asignarle las
> fincas de ese usuario de plataforma. Estas cuentas embebidas solo se pueden dar
> de alta vía API, con un POST al mismo endpoint `/api/accounts/`.
>
> — Andrei Jizdan, CTO de Graniot

## Cómo lo resuelve Dataris

Los dos padrones se cruzan solos, sin pedir nada al comercial de Graniot:

1. **Censo de dueños de finca.** `GET /api/company/farms/` devuelve todas las
   fincas de la empresa (412, frente a las 19 de `/api/farms/`) y cada una con
   sus `responsibles`: correo, nombre e **id numérico**. Ese id es justo lo que
   Graniot acepta como `client_id` para actuar en nombre de esa persona.
2. **Alta de la cuenta embebida.** `POST /api/accounts/` con un alias
   (`dataris-embed+{uid}@dataris.es`), porque Graniot rechaza repetir el correo
   de un usuario de plataforma existente. El alias lleva dentro el id numérico,
   así que el vínculo se puede reconstruir mirando solo `/api/accounts/`.
3. **Asignación de sus fincas.** `POST /api/farms/{id}/managers/` por cada finca
   de esa persona, con el cuerpo que Graniot exige: `[{"account_id": "acc-…"}]`.
   Es lo que hace que su portal las muestre, y se comprueba en la respuesta que
   la cuenta quedó entre los gestores.
4. **Vínculo guardado** en la tabla `graniot_embed_links` del almacén compat, con
   las fincas sincronizadas y cuándo. A partir de ahí el portal se resuelve por
   vínculo, sin depender de que los correos coincidan.

Todo ocurre al abrir el módulo Satélite. Si algo falla —Graniot caído, la persona
no consta como responsable de ninguna finca, el alta no se completa a tiempo— se
sirve el portal de servicio de siempre: **el mapa nunca se queda esperando**.

### Detalles que importan

- **Presupuesto de tiempo** (`GRANIOT_EMBED_PROVISION_TIMEOUT_SECONDS`, 12 s): un
  cliente con muchas fincas necesita muchas llamadas. Pasado ese tiempo el alta
  continúa en segundo plano y esa carga usa el portal compartido; la siguiente
  ya encuentra el suyo.
- **Candado por usuario**: dos pestañas abiertas a la vez no dan de alta dos
  cuentas.
- **Fincas que aparecen después**: cada vínculo se revisa pasado
  `GRANIOT_EMBED_FARM_SYNC_TTL_SECONDS` (6 h) en segundo plano, y subir un lote a
  una finca que el vínculo no conocía lo marca para revisar de inmediato.
- **Nunca retira accesos ajenos**: la lista del alta suma gestores sin tocar a
  los que ya estaban y repetirla no duplica nada (verificado contra la API), así
  que el aprovisionamiento se puede repetir sin miedo. La baja se pide con esa
  misma lista, por `DELETE`.
- **Dar de baja una cuenta embebida no la borra: la desactiva.** A partir de ahí
  Graniot no la lista en `/api/accounts/` ni deja volver a darla de alta con ese
  correo (`…already exists but is deactivated`), y solo se puede reactivar o
  borrar del todo desde la aplicación de Graniot. Por eso el alta prueba hasta
  cuatro alias (`…+{uid}@`, `…+{uid}-1@`, …) antes de rendirse, y por eso
  conviene no usar `?delete_account=true` salvo que haga falta de verdad.
- **Sin fincas no se crea cuenta**: un portal vacío es peor que el fallback.
- Los lotes que Dataris sube van a la misma cuenta que el usuario ve. Si aún no
  tiene cuenta embebida pero sí es usuario de plataforma, se suben con su
  `client_id` numérico, así que ya no hacen falta las cuentas de la sección API.

## Endpoints

| Método | Ruta | Quién | Para qué |
| ------ | ---- | ----- | -------- |
| `GET` | `/api/graniot/embed` | usuario | Su portal (`source`: `personal` o `service`) |
| `GET` | `/api/graniot/embed/links` | admin | Quién tiene portal propio y quién no, y por qué |
| `POST` | `/api/graniot/embed/links` | admin | Crear o reparar el portal de un usuario (idempotente) |
| `DELETE` | `/api/graniot/embed/links/{correo}` | admin | Deshacer el vínculo (`?delete_account=`, `?unlink_farms=`) |
| `GET` | `/api/graniot/accounts` | admin | Diagnóstico de cuentas embebidas |
| `GET` | `/api/graniot/parcels/sync-target` | usuario | A qué cuenta van sus lotes |

`POST /api/graniot/embed/links` acepta `platform_email` cuando la persona usa en
Graniot un correo distinto del que tiene en Dataris.

## Interruptores

| Variable | Valor por defecto | Efecto |
| -------- | ----------------- | ------ |
| `GRANIOT_EMBED_PER_USER_ENABLED` | `true` | `false` fuerza el portal de servicio para todos |
| `GRANIOT_EMBED_AUTOPROVISION_ENABLED` | `true` | `false` deja el comportamiento anterior (solo cuentas ya existentes) |
| `GRANIOT_EMBED_ALIAS_TEMPLATE` | `dataris-embed+{uid}@dataris.es` | Correo de las cuentas embebidas creadas |
| `GRANIOT_EMBED_ALIAS_MAX_ATTEMPTS` | `4` | Alias alternativos a probar si el correo está quemado |
| `GRANIOT_EMBED_PROVISION_TIMEOUT_SECONDS` | `12` | Espera máxima del alta con el usuario delante |
| `GRANIOT_EMBED_FARM_SYNC_TTL_SECONDS` | `21600` | Cada cuánto se revisan las fincas de un portal |
| `GRANIOT_COMPANY_FARMS_CACHE_TTL_SECONDS` | `900` | Caché del censo de dueños de finca |

## Pruebas

`tests/test_graniot_embed_provisioning.py` cubre el censo, el alias, el alta, la
idempotencia, el fallo por finca, la concurrencia, el presupuesto de tiempo, la
reconciliación y el destino de los lotes. `tests/test_graniot_embed.py` mantiene
el fallback y la renovación del token.
