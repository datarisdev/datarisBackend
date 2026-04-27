# scripts/seed_platform_modules.py
from datetime import datetime
from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.models.platform_module import PlatformModule

modules = [
    {
        "name": "mapeo",
        "description": "Análisis de mapeo y cosecha",
        "icon": "Map",
        "is_active": True,
    },
    {
        "name": "telemetria",
        "description": "Telemetría de maquinaria",
        "icon": "Gauge",
        "is_active": True,
    },
    {
        "name": "aplicaciones_aereas",
        "description": "Gestión de aplicaciones aéreas",
        "icon": "Plane",
        "is_active": True,
    },
    {
        "name": "sig_agricola",
        "description": "Sistema de información geográfica",
        "icon": "Layers",
        "is_active": True,
    },
    {
        "name": "alertas",
        "description": "Sistema de alertas",
        "icon": "Bell",
        "is_active": True,
    },
    {
        "name": "tareas",
        "description": "Gestión de tareas",
        "icon": "CheckSquare",
        "is_active": True,
    },
    {
        "name": "analytics",
        "description": "Dashboard analítico",
        "icon": "BarChart3",
        "is_active": True,
    },
    {
        "name": "satelite",
        "description": "Monitoreo satelital NDVI",
        "icon": "Satellite",
        "is_active": True,
    },
]

def seed_platform_modules():
    db: Session = SessionLocal()
    try:
        for m in modules:
            existing = db.query(PlatformModule).filter(PlatformModule.name == m["name"]).first()
            if not existing:
                module = PlatformModule(**m)
                db.add(module)
        db.commit()
        print("Platform modules seeded successfully!")
    finally:
        db.close()

if __name__ == "__main__":
    seed_platform_modules()
