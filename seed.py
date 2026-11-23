import json
import random
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "inventory.db"
BASE = "/tmf-api/resourceInventoryManagement/v4"

TOTAL_ASSETS = random.randint(650, 950)  # Random count above 600 (and <= 1,000)

CATEGORIES = ["Metro-E Router", "ODP Box", "Optical Core Switch", "Base Transceiver Station", "GPON OLT"]
PURPOSES = ["Backhaul Transport", "Last-Mile Access", "Core Aggregation", "Enterprise Leased Line", "Disaster Recovery"]
CITIES = [
    ("Jakarta Data Center", "-6.2088, 106.8456"),
    ("Bandung Regional Hub", "-6.9175, 107.6191"),
    ("Surabaya POP", "-7.2575, 112.7521"),
    ("Medan Node", "3.5952, 98.6722"),
    ("Makassar Gateway", "-5.1477, 119.4327"),
]
STATUSES = ["available", "reserved", "standby", "alarm", "suspended"]

db = sqlite3.connect(DB_PATH)
db.execute("CREATE TABLE IF NOT EXISTS resource (id TEXT PRIMARY KEY, doc TEXT)")
db.execute("DELETE FROM resource")

for i in range(1, TOTAL_ASSETS + 1):
    rid = str(uuid.uuid4())
    cat = random.choice(CATEGORIES)
    city, gps = random.choice(CITIES)
    year = random.randint(2020, 2026)
    price = round(random.uniform(500.0, 25000.0), 2)
    age = max(1, 2026 - year)
    depr_value = round(max(price * 0.1, price - (price * 0.15 * age)), 2)

    doc = {
        "id": rid,
        "href": f"{BASE}/resource/{rid}",
        "name": f"{cat.split()[0]}-{i:04d}",
        "category": cat,
        "description": f"Standardized TMF639 physical asset #{i:04d} deployed for {city}.",
        "resourceVersion": "1.0",
        "administrativeState": "unlocked",
        "operationalState": "enable",
        "resourceStatus": random.choice(STATUSES),
        "usageState": random.choice(["idle", "active", "busy"]),
        "startOperatingDate": f"{year}-01-15T00:00:00Z",
        "place": [
            {
                "id": f"LOC-{i % 5 + 1}",
                "name": city,
                "role": "installationSite",
                "@referredType": "GeographicSite",
            }
        ],
        "note": [
            {
                "id": f"NOTE-{i}",
                "author": "System Seeder",
                "date": datetime.now(timezone.utc).isoformat(),
                "text": f"Commissioned in {year}; routine inspection passed.",
            }
        ],
        "attachment": [
            {
                "id": f"ATT-{i}",
                "attachmentType": "image",
                "name": f"device_{i:04d}.jpg",
                "mimeType": "image/jpeg",
                "url": f"https://example.com/assets/device_{i:04d}.jpg",
            }
        ],
        "resourceCharacteristic": [
            {"name": "serialNumber", "valueType": "string", "value": f"SN-TMF-{100000 + i}"},
            {"name": "purpose", "valueType": "string", "value": random.choice(PURPOSES)},
            {"name": "gpsLocation", "valueType": "string", "value": gps},
            {"name": "price", "valueType": "number", "value": price},
            {"name": "currentDepreciationValue", "valueType": "number", "value": depr_value},
            {"name": "year", "valueType": "integer", "value": year},
        ],
        "@type": "PhysicalResource",
        "@baseType": "Resource",
    }
    db.execute("INSERT INTO resource VALUES (?, ?)", (rid, json.dumps(doc)))

db.commit()
print(f"Successfully seeded {TOTAL_ASSETS} TMF639 assets into inventory.db")