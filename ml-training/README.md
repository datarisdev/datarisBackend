# Imagen de entrenamiento — Laboratorio de IA (Dataris)

Imagen Docker independiente de `datarisBackend`, usada exclusivamente como
execution bajo demanda de un Azure Container App Job de CPU (plan
Consumption, tope real de 2 vCPU/4Gi por ejecución — ver
`datarisInfra/ml_training_job.tf`) para ejecutar entrenamientos de visión
por computadora con PyTorch + Ultralytics. Nunca se ejecuta dentro del
proceso FastAPI principal.

Reemplazó a un diseño original sobre Azure ML Compute Cluster GPU, que nunca
llegó a funcionar por un bloqueo real de cuota en la suscripción de Azure
(ver `app/modules/ml_training/training_job_client.py` para el detalle).

## Contenido

- `Dockerfile` — imagen base `pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime` (versión fijada).
- `requirements.txt` — dependencias con versión fijada (Ultralytics, OpenCV, Pillow, PyYAML, NumPy, Azure SDKs).
- `entrypoint.py` — wrapper que descarga los inputs de Blob Storage a `/mnt/<nombre>/` antes de correr `train.py`/`predict.py`, sube `progress.json` en vivo mientras corren, y sube `/mnt/output/` al terminar (éxito o error). Azure Container Apps Jobs, a diferencia de Azure ML, no monta carpetas de blob automáticamente.
- `train.py` — entrenamiento real. Solo acepta parámetros validados por argparse (mismos rangos que `TrainingJobConfig` en el backend). No ejecuta comandos ni código arbitrario.
- `validate_dataset.py` — validación de estructura YOLO (standalone, sin depender del paquete `app` del backend).
- `export_model.py` — exporta un checkpoint `.pt` a ONNX (opcional).
- `create_manifest.py` — genera `manifest.json` con trazabilidad completa (receta, config, métricas, versiones de dependencias, artefactos).
- `smoke_test.py` — pruebas locales sin GPU (imports, validación de dataset sintético, generación de manifest, rechazo de argumentos fuera de rango).

## Build local (sin publicar en ACR)

```bash
cd datarisBackend/ml-training
docker build -t dataris-ml-training:local .
```

**No publicar esta imagen en el ACR de Dataris sin aprobación explícita** (ver reglas del módulo de entrenamiento). El build local solo sirve para verificar que la imagen compila y pasa el smoke test.

## Smoke test (sin GPU)

Dentro del contenedor o en un entorno local con las dependencias instaladas:

```bash
pip install -r requirements.txt
python smoke_test.py
```

o dentro de Docker:

```bash
docker run --rm dataris-ml-training:local python smoke_test.py
```

El smoke test **no entrena un modelo real** (requeriría GPU y tiempo); valida que el pipeline completo (imports, validación de dataset, generación de manifest, validación de argumentos de `train.py`) funciona correctamente.

## Comando real de entrenamiento

El backend genera el comando exacto en `app/modules/ml_training/training_recipes.py::build_train_command()` y lo envía envuelto en `entrypoint.py` como una execution del Container App Job (`app/modules/ml_training/training_job_client.py::submit_command_job`). Ejemplo del comando real que corre `entrypoint.py` (las rutas `/mnt/*` las llena/drena el wrapper, no Azure):

```bash
python train.py \
  --dataset-path /mnt/data \
  --task detection \
  --recipe ultralytics_yolo_detection \
  --model yolo11n.pt \
  --epochs 50 \
  --imgsz 640 \
  --batch 16 \
  --lr 0.01 \
  --patience 20 \
  --val-split 0.2 \
  --seed 0 \
  --augment 1 \
  --project-id <uuid> \
  --job-id <uuid> \
  --output-path /mnt/output
```

## Artefactos generados

Al finalizar con éxito, `train.py` escribe en `--output-path`:

- `best.pt`, `last.pt` — pesos del modelo.
- `metrics.json` — métricas finales de Ultralytics.
- `config.json` — configuración de entrenamiento usada.
- `data.yaml` — copia del data.yaml del dataset.
- `manifest.json` — trazabilidad completa (ver `create_manifest.py`).

Todos estos archivos son recogidos por el backend (`artifact_service.py`) y subidos a Azure Blob Storage privado; nunca quedan expuestos públicamente.

## Variables/; secretos requeridos en producción

Esta imagen no lee `ROBOFLOW_API_KEY` ni secretos de Key Vault directamente: el backend ya resuelve el dataset a una ruta de Blob Storage antes de crear el job. `entrypoint.py` necesita acceso de lectura/escritura directo a Blob Storage (`BLOB_CONTAINER_NAME`, `AZURE_STORAGE_ACCOUNT_URL`, `AZURE_CLIENT_ID`, `BLOB_INPUTS_JSON`, `BLOB_OUTPUT_PREFIX` — ver `training_job_client.build_blob_io_env`), usando la Managed Identity del Container App Job (ver `datarisInfra/ml_training_identity.tf`).
