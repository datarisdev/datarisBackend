# Imagen de entrenamiento de Dataris — Laboratorio de IA.
#
# Separada por completo de la imagen del backend FastAPI (ver ../Dockerfile):
# esta imagen solo se ejecuta dentro de un Azure ML Compute Cluster GPU bajo
# demanda (min_instances=0), nunca en Azure Container Apps ni en el proceso
# principal del backend. Versión de base e imagen CUDA fijadas explícitamente.
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

COPY train.py predict.py tiling.py validate_dataset.py export_model.py create_manifest.py smoke_test.py ./

# Azure ML sobreescribe el comando real con el que arma
# training_recipes.build_train_command() en el backend; este CMD por
# defecto solo sirve para `docker run <imagen> --help` y para el smoke test
# local documentado en README.md.
CMD ["python", "train.py", "--help"]
