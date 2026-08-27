"""Bitácora de Campo.

Registro del ciclo productivo de una parcela: labores costeadas, insumos
aplicados, fenología y los indicadores derivados (económicos y de
sostenibilidad) que antes se llevaban a mano en una hoja de cálculo.

El modelo nace de la bitácora del CDT FIRA Villadiego, pero el formato
concreto de esa hoja es solo una *plantilla* (ver `templates.py`): el módulo
sirve igual a un centro de investigación que a un productor de caña.

Desde que la bitácora se alimenta de AgtechApps, el cálculo (`kpi.py`,
`sensitivity.py`) se usa también desde el sistema de compatibilidad, que no
tiene ORM. Este `__init__` **no** importa los modelos por eso: hacerlo obligaba
a cargar `app.models` para leer diez constantes, y como `app.models` importa a
su vez `field_log.models`, el orden de carga decidía si el import funcionaba o
reventaba con un circular. Los modelos se importan de `field_log.models` y el
vocabulario de `field_log.catalog`, cada uno por su nombre completo.
"""
