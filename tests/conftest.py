import os
import uuid

import pytest
from sqlalchemy import create_engine, types as sqltypes
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DISABLE_AZURE_BLOB_STORAGE", "true")

from app.models.base import Base
import app.models  # noqa: F401 - registra todos los modelos en Base.metadata

# Shim exclusivo de tests: en Postgres real (psycopg2), pasar un string a una
# columna UUID(as_uuid=True) funciona porque Postgres castea texto -> uuid
# implícitamente (así lo hace el resto del código, p.ej. FieldNote.user_id =
# current_user["id"]). El motor genérico sqlalchemy.types.Uuid usado para
# SQLite en estos tests exige un objeto uuid.UUID real. Se parchea aquí,
# solo en el proceso de test, para aceptar strings igual que Postgres.
_original_bind_processor = sqltypes.Uuid.bind_processor


def _string_tolerant_bind_processor(self, dialect):
    processor = _original_bind_processor(self, dialect)
    if processor is None:
        return processor

    def process(value):
        if isinstance(value, str):
            value = uuid.UUID(value)
        return processor(value)

    return process


sqltypes.Uuid.bind_processor = _string_tolerant_bind_processor

# Solo se crean las tablas que el módulo ml_training realmente usa en tests.
# Otras tablas del monolito (parcels, satellite_images, ...) usan tipos
# específicos de PostgreSQL (JSONB) que SQLite no puede compilar, y no son
# relevantes para este módulo: SQLite no aplica FKs por defecto, así que no
# hace falta crear "users" para insertar filas con un user_id arbitrario.
_ML_TRAINING_TABLE_NAMES = {
    "user_roles",
    "ml_training_projects",
    "ml_datasets",
    "ml_dataset_files",
    "ml_training_jobs",
    "ml_model_versions",
    "ml_model_artifacts",
    "ml_training_audit_logs",
    "ml_training_limits",
}


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    tables = [Base.metadata.tables[name] for name in _ML_TRAINING_TABLE_NAMES]
    Base.metadata.create_all(engine, tables=tables)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def user_a_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def user_b_id() -> str:
    return str(uuid.uuid4())
