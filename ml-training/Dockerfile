# Imagen de entrenamiento de Dataris — Laboratorio de IA.
#
# Separada por completo de la imagen del backend FastAPI (ver ../Dockerfile):
# esta imagen solo se ejecuta como execution bajo demanda de un Azure
# Container App Job de CPU (plan Consumption, ver
# datarisInfra/ml_training_job.tf), nunca en el proceso principal del
# backend. Versión de base fijada explícitamente. La base sigue trayendo
# CUDA porque conserva el mismo stack de PyTorch validado; el Container App
# Job la corre sobre CPU (sin GPU asignada), ver entrypoint.py.
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# libgl1/libglib2.0-0: requeridos por opencv-python-headless para decodificar
# imágenes; el resto son utilidades mínimas de compilación para wheels sin
# binarios prearmados para esta combinación de Python/CUDA.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.py train.py predict.py tiling.py validate_dataset.py export_model.py create_manifest.py smoke_test.py ./

# El backend (training_job_client.submit_command_job) sobreescribe el
# command/args reales de cada execution para invocar entrypoint.py, que
# descarga inputs de Blob Storage, corre train.py/predict.py, y sube la
# salida; este CMD por defecto solo sirve para `docker run <imagen> --help`
# y para el smoke test local documentado en README.md.
CMD ["python", "train.py", "--help"]
