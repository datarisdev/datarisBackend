"""Bitácora de Campo.

Registro del ciclo productivo de una parcela: labores costeadas, insumos
aplicados, fenología y los indicadores derivados (económicos y de
sostenibilidad) que antes se llevaban a mano en una hoja de cálculo.

El modelo nace de la bitácora del CDT FIRA Villadiego, pero el formato
concreto de esa hoja es solo una *plantilla* (ver `templates.py`): el módulo
sirve igual a un centro de investigación que a un productor de caña.
"""

from app.modules.field_log.models import (  # noqa: F401
    CropCycle,
    FieldLogEntry,
    FieldLogEntryInput,
    FieldLogLaborStandard,
    FieldLogTemplate,
    PhenologyRecord,
)
