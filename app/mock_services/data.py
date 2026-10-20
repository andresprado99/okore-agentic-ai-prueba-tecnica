"""Datos de ejemplo de los servicios simulados (nada de esto son datos reales de OKORE)."""

CLAIMS = {
    "EXP-10234": {"claim_id": "EXP-10234", "status": "REPAIRING", "vehicle": {"plate": "1234ABC", "brand": "BMW", "model": "X1"},
                  "workshop_id": "T-228", "missing_documents": ["PHOTO_PLATE"], "last_update": "2026-09-22T10:32:00"},
    "EXP-10567": {"claim_id": "EXP-10567", "status": "CLOSED", "vehicle": {"plate": "5678XYZ", "brand": "Seat", "model": "Ibiza"},
                  "workshop_id": "T-101", "missing_documents": [], "last_update": "2026-08-30T16:05:00"},
    # Existe, pero ningún usuario de prueba tiene permiso sobre él (sirve para probar la autorización).
    "EXP-20000": {"claim_id": "EXP-20000", "status": "REPAIRING", "vehicle": {"plate": "9999ZZZ", "brand": "Audi", "model": "A3"},
                  "workshop_id": "T-228", "missing_documents": ["PHOTO_PLATE"], "last_update": "2026-09-30T09:00:00"},
}

WORKSHOPS = {
    "T-228": {"workshop_id": "T-228", "name": "Talleres Norte", "contact": "norte@example.com", "status": "ACTIVE", "channels": ["EMAIL", "PORTAL"]},
    "T-101": {"workshop_id": "T-101", "name": "Taller Centro", "contact": "centro@example.com", "status": "ACTIVE", "channels": ["EMAIL"]},
}

SEED_EVENTS = [
    ("EXP-10234", "CLAIM_OPENED", "2026-09-15", "Expediente abierto"),
    ("EXP-10234", "VEHICLE_RECEIVED", "2026-09-18", "Vehículo recibido en el taller T-228"),
    ("EXP-10234", "REPAIR_STARTED", "2026-09-22", "Reparación iniciada"),
    ("EXP-10567", "CLAIM_OPENED", "2026-08-10", "Expediente abierto"),
    ("EXP-10567", "CLAIM_CLOSED", "2026-08-30", "Expediente cerrado"),
]
