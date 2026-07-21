# datarisBackend

Backend FastAPI de Dataris.

## Copilotos agro con Azure OpenAI

Los copilotos contextual y de aplicaciones aéreas usan la Responses API. El
navegador nunca recibe credenciales: envía contexto saneado y hasta dos imágenes
optimizadas; el backend elimina campos sensibles y geometrías crudas antes de
invocar el modelo. Los KPIs calculados por Dataris siguen siendo la fuente
canónica y la visión se usa como evidencia complementaria.

Configuración recomendada en Azure Container Apps:

```dotenv
AZURE_OPENAI_ENDPOINT=https://<recurso>.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=<deployment-general>
AZURE_OPENAI_CONTEXTUAL_DEPLOYMENT=<deployment-multimodal>
AZURE_OPENAI_AERIAL_DEPLOYMENT=<deployment-structured-output>
AZURE_OPENAI_TOKEN_SCOPE=https://ai.azure.com/.default
AZURE_OPENAI_IMAGE_DETAIL=original
```

La identidad administrada del Container App necesita únicamente el rol
`Cognitive Services OpenAI User` sobre el recurso Azure OpenAI. Una API key es
compatible mediante `AZURE_OPENAI_API_KEY`, pero no es el mecanismo recomendado.
Si Azure OpenAI no está configurado o falla temporalmente, ambos endpoints
devuelven el diagnóstico determinístico local para mantener continuidad.

Pruebas:

```bash
python -m pytest -q
```
