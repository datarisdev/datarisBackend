# DigiForms Data API: FormId interno y respuestas dinámicas

## Corrección aplicada

- La URL base canónica es `https://d.interlinksoft.net/DigiformsData/api`.
- Basic Auth usa exactamente `{ClientId}/{UserId}` sin espacios.
- El formulario se configura con el identificador interno mostrado en `FFormEdit.aspx?FormId=...`, no con el Id corto del listado.
- La primera sincronización usa rango de fechas cuando el cursor es `0`.
- Las siguientes sincronizaciones usan el último `ResponseId` persistido.
- Se conservan todos los campos dinámicos y el JSON original por respuesta en `sig_digiforms_raw_submissions`.
- Las respuestas HTTP comprimidas se aceptan con `gzip, deflate, br`.

## Listado de formularios (`GET api/form/{clientId}`)

Documentado por el proveedor en *Documentacion_WebService_FormGet.pdf* (jul 2026). Permite ofrecer los
formularios por nombre en lugar de pedir que alguien copie un identificador.

```
GET https://d.interlinksoft.net/DigiformsData/api/form/{clientId}
Authorization: Basic base64({ClientId}/{Usuario}:{contraseña})
```

Respuesta: `{"Forms": [{Id, Description, Title, ValidFrom, ValidTo, Status, Category, ReferenceId, IsPublic}]}`.

Detalles del contrato que hay que respetar al parsear:

- `IsPublic` llega como **cadena** (`"false"`), no como booleano.
- `Status` es un **StatusId**, no un texto legible.
- `ValidFrom`/`ValidTo` usan `yyyy-MM-dd HH:mm:ss`.
- **400** significa que el `clientId` de la ruta no coincide con el del usuario autenticado; **401**, un
  problema de suscripción (el proveedor lo decide por el texto del mensaje).

⚠ **Pendiente de confirmar contra un cliente real**: el PDF dice que `Id` sale de la columna `FormId`, pero su
ejemplo devuelve `"101"`, que se parece al Id corto que esta misma nota advierte que NO sirve para
`results/GetAll`. Por eso cada formulario importado se puede comprobar desde la interfaz: esa comprobación hace
un `results/GetAll` real y deja ver si el identificador del listado es utilizable.

## Variables globales

```env
DIGIFORMS_DATA_BASE_URL=https://d.interlinksoft.net/DigiformsData/api
```

Las credenciales se configuran por empresa desde Extensiones → DigiFormsApp, donde también se importa el
catálogo de formularios y se enlaza cada uno con una plantilla de Reportes de Campo.
