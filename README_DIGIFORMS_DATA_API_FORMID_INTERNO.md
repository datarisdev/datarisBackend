# DigiForms Data API: FormId interno y respuestas dinámicas

## Corrección aplicada

- La URL base canónica es `https://d.interlinksoft.net/DigiformsData/api`.
- Basic Auth usa exactamente `{ClientId}/{UserId}` sin espacios.
- El formulario se configura con el identificador interno mostrado en `FFormEdit.aspx?FormId=...`, no con el Id corto del listado.
- La primera sincronización usa rango de fechas cuando el cursor es `0`.
- Las siguientes sincronizaciones usan el último `ResponseId` persistido.
- Se conservan todos los campos dinámicos y el JSON original por respuesta en `sig_digiforms_raw_submissions`.
- Las respuestas HTTP comprimidas se aceptan con `gzip, deflate, br`.

## Variables globales

```env
DIGIFORMS_DATA_BASE_URL=https://d.interlinksoft.net/DigiformsData/api
```

Las credenciales y FormId se configuran por empresa desde Extensiones → DigiFormsApp.
