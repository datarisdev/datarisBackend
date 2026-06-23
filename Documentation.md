## 1️⃣ Final architecture

Frontend (Lovable → later React / Mobile)
   |
   |  HTTPS + JWT
   v
FastAPI Backend (Azure Container Apps)
   |
   |-- Auth & Roles
   |-- Validation
   |-- Business rules
   |-- Analysis workflows
   |
   |-- SQLAlchemy
   v
Cloud SQL (PostgreSQL)
   |
   |-- PostGIS (later, optional)

## 2️⃣ Azure services

| Purpose         | Service                     |
| --------------- | --------------------------- |
| Backend runtime | **Azure Container Apps**               |
| API             | **FastAPI**                 |
| Database        | **Azure Database for PostgreSQL Flexible Server** |
| Auth            | **Firebase Auth**           |
| Secrets         | **Secret Manager**          |
| Files           | **Cloud Storage**           |
| Logs            | **Cloud Logging**           |

# 3️⃣ Postman Testing Flow (all endpoints)

| Action            | Method | URL                      | Body / Headers                                                                                                  | Expected                                                               |
| ----------------- | ------ | ------------------------ | --------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Register          | POST   | `/api/auth/register`     | JSON `{ "email":"demo@x.com", "password":"123", "first_name":"Demo", "company_name":"FarmX", "hectareas": 50 }` | 201 created (user + profile with company info)                         |
| Login             | POST   | `/api/auth/login`        | JSON `{ "email":"demo@x.com","password":"123"}`                                                                 | JWT token                                                              |
| Get current user  | GET    | `/api/users/me`          | Header `Authorization: Bearer <token>`                                                                          | User + profile + roles (profile now includes company_name + hectareas) |
| List users        | GET    | `/api/users/`            | Header `Authorization`                                                                                          | List all users (profiles include company_name + hectareas)             |
| Delete user       | DELETE | `/api/users/{id}`        | Header `Authorization`                                                                                          | User deleted                                                           |
| Get my profile    | GET    | `/api/profiles/me`       | Header `Authorization`                                                                                          | Profile data including `company_name` and `hectareas`                  |
| Update my profile | PUT    | `/api/profiles/me`       | JSON body `{ "first_name": "Demo", "company_name":"FarmX", "hectareas": 60 }`, Header `Authorization`           | Updated profile with new company_name/hectareas                        |
| Assign role       | POST   | `/api/roles/`            | JSON body `{ "user_id": "...", "role": "admin"}`                                                                | Role assigned                                                          |
| Remove role       | DELETE | `/api/roles/`            | JSON body `{ "user_id": "...", "role": "admin"}`                                                                | Role removed                                                           |
| List roles        | GET    | `/api/roles/{user_id}`   | Header `Authorization`                                                                                          | List of roles                                                          |
| Create admin      | POST   | `/api/admin_users/`      | `{ "email": "cto@company.com", "password": "supersecure123" }`                                                  | Admin account created                                                  |
| Login admin       | POST   | `/api/admin_users/login` | `{ "email": "cto@company.com", "password": "supersecure123" }`                                                  | Login successful (JWT)                                                 |

# 4️⃣ Roles + hierarchy (important rule)
| Creator role     | Can create            |
| ---------------- | --------------------- |
| admin            | anyone                |
| supervisor_campo | ❌ none              |
| tecnico          | ❌ none              |
| visualizador     | ❌ none          

Will define rights and uses for each role. For now, only admin


# Satellite design
## Database design (conceptual)
✅ processing_status

This allows async/background processing. Enum values:

- pending → created but not started
- processing → analysis running
- completed → success
- failed → error (store reason later if needed)

## Storage Layout

dataris-satellite/
└── satellite/
    └── {user_id}/
        └── {parcel_id}/
            └── {index_type}/
                └── {image_date}/
                    └── image.tif

## Architecture 
Frontend
   ➡️ POST /satellite/process
   ➡️ returns immediately

Backend
   ➡️ Enqueues background job
   ➡️ Processor writes:
      - bucket objects
      - DB rows

Frontend
   ➡️ GET /satellite/parcel/{id}
   ➡️ receives signed URLs + stats

Frontend
   |
   | POST /satellite/process
   v
API (Azure Container Apps)
   |
   | enqueue task
   v
Cloud Tasks Queue
   |
   | HTTP push
   v
Worker Endpoint (Azure Container Apps)
   |
   | process images day-by-day
   v
Azure Blob Storage + PostgreSQL

## High-level architecture

Frontend
   ↓
Job creation (API)
   ↓
Background ingestion (Celery)
   ↓
Stored results
   ↓
On-demand calculations (API)

# Index theory

## **1️⃣ Índices simples (ya muy parecidos a lo que tienes)**

Estas usan solo bandas que ya estás descargando (rojo, NIR, SWIR, verde):

### **NDVI** – Normalized Difference Vegetation Index

Ya lo tienes:
[
\text{NDVI} = \frac{NIR - Red}{NIR + Red}
]

### **NDWI** – Normalized Difference Water Index

* Para vegetación y agua, normalmente se usa verde y NIR:
  [
  \text{NDWI} = \frac{Green - NIR}{Green + NIR}
  ]
* Verde en Sentinel-2 es **B03** (10 m).
* En tu función, tendrías que abrir `green_url = item.assets["B03"].href` y recortarlo como haces con las otras bandas.

### **LCI** – Leaf Chlorophyll Index (simple versión)

* Fórmula sencilla usando NIR y rojo:
  [
  \text{LCI} = \frac{NIR}{Red} - 1
  ]
* Ya tienes ambas bandas, así que solo un `arr = nir / red - 1`.

---

## **2️⃣ Índices más complejos**

Estos requieren **fórmulas físicas o empíricas**, normalmente derivadas de papers de teledetección.

### **CWC – Canopy Water Content (contenido de agua de la hoja)**

* Expresado en mg/cm².
* Fórmula común (Ceccato et al., 2001):
  [
  CWC = LAI \times LWC
  ]
  Donde:
* `LAI` → Leaf Area Index (m² hoja / m² suelo)
* `LWC` → Leaf Water Content (g/cm² hoja)
* **Otra aproximación directa usando NDWI** (simplificada):
  [
  CWC \approx \frac{NDWI \times \rho_{leaf}}{1 - NDWI}
  ]
* Para Sentinel-2 se pueden usar bandas SWIR (B11 o B12) porque absorben agua en las hojas.
* Necesitas **calibración con datos de campo** para ser exacto, pero un primer cálculo puede ser:

```python
CWC = (NDVI * SWIR) * factor  # factor depende de calibración
```

### **LAI – Leaf Area Index**

* Medida de metros cuadrados de hoja por metro cuadrado de suelo.
* Índices sencillos:
  [
  LAI = -\frac{\ln(\frac{NIR - Red}{NIR + Red})}{k}
  ]
* `k` es el coeficiente de extinción de la hoja (~0.5 a 0.8 para vegetación típica).
* Otra forma usando NDVI:

```python
LAI = -np.log((0.69 - NDVI) / 0.59) / 0.91  # según Chen 1996
```

* El resultado es adimensional (m²/m²).

### **LCC – Leaf Chlorophyll Content**

* Se puede estimar usando bandas rojo y NIR o con la **Índice de clorofila a nivel de hoja**:
  [
  LCC = \frac{NIR}{RedEdge} - 1
  ]
* Sentinel-2 tiene **RedEdge**: B05 (705 nm), B06 (740 nm), B07 (783 nm).
* Una fórmula típica:

```python
LCC = (B05 / B04) - 1
```

* El resultado es relativo, pero se puede calibrar a mg/cm² con ensayos de campo.

---
> ⚠️ Nota: LAI, CWC y LCC son **estimaciones físicas** y pueden requerir calibración con datos de campo para obtener valores exactos en mg/cm² o m²/m². Para exploración inicial, los valores relativos están bien.

Below I’ll do **three things**:

1. Define **proper normalization ranges** for each index
2. Implement **professional normalize_* functions** for
   `["NDVI", "NDWI", "LCI", "LAI", "LCC", "CWC"]`
3. Refactor your **normalization dispatch** so it scales cleanly

---

## 1️⃣ Expected value ranges (important)

These are **standard, defensible ranges** used in remote sensing dashboards:

| Index    | Raw meaning            | Typical raw range     | Normalize strategy |
| -------- | ---------------------- | --------------------- | ------------------ |
| **NDVI** | Vegetation vigor       | `[-1, 1]`             | Linear             |
| **NDWI** | Water content          | `[-1, 1]`             | Linear             |
| **LCI**  | Relative chlorophyll   | `[0, ~3]`             | Clamp + linear     |
| **LAI**  | Leaf area (m²/m²)      | `[0, ~6]`             | Clamp + linear     |
| **LCC**  | Chlorophyll (relative) | `[0, ~80]` (relative) | Clamp + linear     |
| **CWC**  | Canopy water content   | `[0, ~1]` (relative)  | Clamp              |

> ⚠️ Important:
> LAI, LCC, CWC are **biophysical estimates**, not bounded indices like NDVI.
> We normalize for **visualization**, not physics.


