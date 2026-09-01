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

### El cliente nuevo nace con su portal

Lo anterior resuelve a quien YA tiene fincas en Graniot. Pero el cliente que el
equipo da de alta en el panel no es responsable de ninguna: **sus lotes los carga
Dataris después**. Sin cuenta embebida propia, el destino de sincronización
degradaba a la cuenta de servicio, el autosync se plantaba (exige cuenta propia)
y sus lotes no salían de Dataris.

Por eso el panel de administración crea la cuenta antes de que haga falta:

- **Al dar de alta al usuario** (`POST /api/compat/admin/users/manual`) se
  encarga el alta de su portal en segundo plano, para no atar la creación del
  cliente al tiempo de respuesta de Graniot.
- **Al cargarle lotes** (`POST /api/compat/admin/parcels/{upload,manual}`) se
  asegura primero su cuenta y después se suben: así el lote se crea dentro del
  portal de su dueño, en una finca con el nombre de la finca del lote.
- El alias de esa cuenta se deriva de su correo de Dataris (`dataris-embed+u…@`)
  en vez del id numérico de plataforma, que todavía no existe. Es estable —el
  mismo correo reconstruye siempre el mismo alias— y propio de cada persona, para
  que dos clientes sin fincas no acaben compartiendo portal. Si más adelante esa
  persona aparece en el censo con fincas, la reconciliación se las asigna a esa
  misma cuenta.
- Dentro de la cuenta del cliente, un lote sin finca propia ya no cae en una
  finca llamada «Dataris»: se usa el nombre de su empresa (o el suyo), que es lo
  que su portal muestra como sección.

Nada de esto puede tumbar el alta ni la carga: si Graniot falla, queda el motivo
registrado en los logs (`dataris.compat.embed_provision.*`) y el lote sigue en
Dataris, listo para reintentar con `POST /api/graniot/embed/links`.

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
- **Sin fincas no se crea cuenta al abrir el mapa**: para quien solo pasa por el
  módulo Satélite, un portal vacío es peor que el fallback. El alta sin fincas se
  reserva a lo que pide el panel (alta de cliente y carga de lotes), donde esa
  cuenta es justo el destino que sus lotes necesitan.
- Los lotes que Dataris sube van a la misma cuenta que el usuario ve. Si aún no
  tiene cuenta embebida pero sí es usuario de plataforma, se suben con su
  `client_id` numérico, así que ya no hacen falta las cuentas de la sección API.

## Endpoints

| Método | Ruta | Quién | Para qué |
| ------ | ---- | ----- | -------- |
| `GET` | `/api/graniot/embed` | usuario | Su portal (`source`: `personal` o `service`) |
| `GET` | `/api/graniot/embed/links` | admin | Quién tiene portal propio y quién no, y por qué |
| `POST` | `/api/graniot/embed/links` | admin | Crear o reparar el portal de un usuario (idempotente) |
| `POST` | `/api/graniot/embed/reconcile` | admin | Cuadrar el mapa con los lotes de Dataris (ensayo por defecto) |
| `DELETE` | `/api/graniot/embed/links/{correo}` | admin | Deshacer el vínculo (`?delete_account=`, `?unlink_farms=`) |
| `GET` | `/api/graniot/accounts` | admin | Diagnóstico de cuentas embebidas |
| `GET` | `/api/graniot/parcels/sync-target` | usuario | A qué cuenta van sus lotes |

`POST /api/graniot/embed/links` acepta `platform_email` cuando la persona usa en
Graniot un correo distinto del que tiene en Dataris, y crea el portal aunque no
tenga fincas todavía; con `require_farms: true` se recupera la conducta estricta,
que falla si esa persona no es responsable de ninguna.

### Cuadrar a quien ya tenía lotes

Los clientes cuyos lotes se cargaron **antes** de que existiera su portal siguen
descuadrados: el listado de lotes dice una cosa y el mapa enseña otra, porque el
lote nunca salió de Dataris (sin cuenta propia el destino degradaba a la de
servicio y el autosync se plantaba) o acabó en una cuenta que no es la suya.

`GET /api/graniot/embed/links` lo dice de un vistazo: cada usuario lleva
`local_parcels`, `synced_parcels`, `parcels_pending_upload`,
`parcels_in_other_account` y `parcels_out_of_place`, y la respuesta trae la lista
`needs_reconcile` con quiénes están descuadrados.

`POST /api/graniot/embed/reconcile` los cuadra:

| Campo | Por defecto | Para qué |
| ----- | ----------- | -------- |
| `dry_run` | `true` | Solo informa. **Hay que pedir `false` expresamente para que toque algo.** |
| `user_email` / `user_id` | — | Cuadrar a una sola persona |
| `limit_users` | `25` | Usuarios por llamada |
| `max_parcels_per_user` | `50` | Lotes por usuario y llamada; el informe devuelve `remaining` |
| `move_misplaced` | `false` | Mover los lotes que están en otra cuenta (los borra allí y los recrea) |

Se puede repetir sin miedo: un lote ya subido a la cuenta correcta no se vuelve a
tocar, y los fallos por lote se acumulan en `failed` sin detener a los demás.
Mover lotes es lo único que borra algo en Graniot, y por eso va tras una opción
aparte.

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
`tests/test_graniot_cuadre_lotes_portal.py` cubre el diagnóstico del descuadre y
el cuadre: ensayo que no toca nada, alta del portal más subida de lo que falta,
lotes en otra cuenta que no se mueven sin pedirlo, el troceado en pasadas y que
un lote que falla no detiene a los demás.
`tests/test_graniot_embed_alta_desde_admin.py` cubre el cliente nuevo: alias
propio y estable, alta al crear el usuario y al cargarle lotes, destino de sus
lotes, nombre de su finca y que un fallo de Graniot no rompa ninguna de las dos
operaciones.
