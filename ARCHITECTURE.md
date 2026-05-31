# Dataris Backend Architecture

## Target pattern

Dataris backend should remain a modular FastAPI monolith while the product is still evolving quickly. This gives Cloud Run simple deployment and scaling, but keeps business logic isolated by domain.

Each module should expose a thin router and keep business logic in `app/modules/<moduleName>`.

## Current applied structure

- `app/api/router_registry.py`: single registry for API routers.
- `app/api/routers/analysis_history.py`: thin compatibility router for the work area endpoints.
- `app/modules/work_area/service.py`: domain service that builds work area responses.

## Module shape

New modules should follow this shape:

```text
app/modules/<module_name>/
  service.py
  schemas.py
  repository.py
  policies.py
  __init__.py

app/api/routers/<module_name>.py
```

For aerial applications, drone-specific changes should be isolated under:

```text
app/modules/aerial_applications/
  service.py
  repository.py
  aircraft/
    drones.py
    helicopters.py
    shared.py
```

## Rules

- Routers validate HTTP inputs and delegate to module services.
- Services own business workflows and can call repositories or external clients.
- Repositories isolate storage details and query optimization.
- Shared helpers can stay in `app/utils`, but domain-specific helpers belong inside the module.
- Keep old routers as compatibility facades when public endpoint names change.
