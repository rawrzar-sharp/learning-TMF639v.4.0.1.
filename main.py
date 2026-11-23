from __future__ import annotations

import copy
import datetime as dt
import glob
import json
import os
import sqlite3
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from generated_models import (
    Error,
    EventSubscription,
    EventSubscriptionInput,
    ResourceAdministrativeStateType,
    ResourceOperationalStateType,
    ResourceStatusType,
    ResourceUsageStateType,
)

API_PREFIX = "/tmf-api/resourceInventoryManagement/v4"
RESOURCE_BASE_PATH = f"{API_PREFIX}/resource"

VALID_RESOURCE_STATUS = {e.value for e in ResourceStatusType}
VALID_USAGE_STATE = {e.value for e in ResourceUsageStateType}
VALID_ADMIN_STATE = {e.value for e in ResourceAdministrativeStateType}
VALID_OPERATIONAL_STATE = {e.value for e in ResourceOperationalStateType}


def resolve_db_path() -> str:
    env_path = os.getenv("DB_PATH")
    if env_path:
        return env_path
    for candidate in ("tmf639.db", "inventory.db", "resources.db", "resource_inventory.db"):
        if os.path.exists(candidate):
            return candidate
    for pattern in ("*.db", "*.sqlite", "*.sqlite3"):
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return "tmf639.db"


DB_PATH = resolve_db_path()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS resource (
                id TEXT PRIMARY KEY,
                doc TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hub_subscription (
                id TEXT PRIMARY KEY,
                callback TEXT NOT NULL,
                query TEXT
            )
            """
        )
        # If `resource` is empty but legacy `resources(id, data)` exists, migrate rows seamlessly
        count = conn.execute("SELECT COUNT(*) FROM resource").fetchone()[0]
        if count == 0:
            legacy = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='resources'"
            ).fetchone()
            if legacy:
                rows = conn.execute("SELECT id, data FROM resources").fetchall()
                for row in rows:
                    conn.execute(
                        "INSERT OR IGNORE INTO resource (id, doc) VALUES (?, ?)",
                        (row["id"], row["data"]),
                    )
        conn.commit()


def tmf_error(status_code: int, reason: str, message: str) -> JSONResponse:
    err = Error(
        code=str(status_code),
        reason=reason,
        message=message,
        status=str(status_code),
    )
    return JSONResponse(
        status_code=status_code,
        content=err.model_dump(by_alias=True, exclude_none=True),
    )


def extract_serial_number(doc: dict[str, Any]) -> str | None:
    chars = doc.get("resourceCharacteristic", [])
    by_name = {c.get("name"): c.get("value") for c in chars}

    if "price" in by_name and "purchasePrice" not in by_name:
        chars.append({"name": "purchasePrice", "valueType": "number", "value": by_name["price"]})
    if "year" in by_name and "purchaseYear" not in by_name:
        chars.append({"name": "purchaseYear", "valueType": "number", "value": by_name["year"]})
    if not isinstance(chars, list):
        return None
    for item in chars:
        if isinstance(item, dict) and item.get("name") == "serialNumber":
            val = item.get("value")
            if isinstance(val, dict) and "root" in val:
                val = val["root"]
            if val is not None and str(val).strip() != "":
                return str(val).strip()
    return None


def validate_date_field(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid ISO date string.")
    raw = value.strip()
    try:
        if len(raw) == 10:
            dt.date.fromisoformat(raw)
        else:
            dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be a valid ISO-8601 date or datetime.",
        ) from exc
    return raw


def validate_and_normalize_resource(
    doc: dict[str, Any],
    resource_id: str,
    conn: sqlite3.Connection,
    is_update: bool = False,
) -> dict[str, Any]:
    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail="Asset name is required.")
    doc["name"] = name.strip()

    doc["id"] = resource_id
    doc["href"] = f"{RESOURCE_BASE_PATH}/{resource_id}"
    doc.setdefault("@type", "Resource")
    doc.setdefault("@baseType", "Resource")

    enum_checks = [
        ("resourceStatus", VALID_RESOURCE_STATUS),
        ("usageState", VALID_USAGE_STATE),
        ("administrativeState", VALID_ADMIN_STATE),
        ("operationalState", VALID_OPERATIONAL_STATE),
    ]
    for field_name, allowed in enum_checks:
        val = doc.get(field_name)
        if val is not None and val not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {field_name}: '{val}'. Allowed: {', '.join(sorted(allowed))}.",
            )

    for date_field in ("startOperatingDate", "endOperatingDate"):
        if doc.get(date_field) is not None:
            doc[date_field] = validate_date_field(doc[date_field], date_field)

    # Normalize `place` (TMF639 v4.0.1 list of RelatedPlaceRefOrValue)
    if "place" in doc and doc["place"] is not None:
        places = doc["place"] if isinstance(doc["place"], list) else [doc["place"]]
        normalized_places = []
        for idx, p in enumerate(places, start=1):
            if not isinstance(p, dict):
                raise HTTPException(status_code=422, detail="Each place entry must be an object.")
            pid = p.get("id") or f"LOC-{idx}"
            normalized_places.append(
                {
                    "id": pid,
                    "href": p.get("href") or f"/tmf-api/geographicSiteManagement/v4/geographicSite/{pid}",
                    "name": p.get("name"),
                    "role": p.get("role") or "installationSite",
                    "@referredType": p.get("@referredType") or "GeographicSite",
                }
            )
        doc["place"] = normalized_places

    # Enforce unique serialNumber across inventory
    serial = extract_serial_number(doc)
    if serial:
        rows = conn.execute("SELECT id, doc FROM resource WHERE id != ?", (resource_id,)).fetchall()
        for row in rows:
            existing = json.loads(row["doc"])
            existing_serial = extract_serial_number(existing)
            if existing_serial and existing_serial.lower() == serial.lower():
                raise HTTPException(
                    status_code=409,
                    detail=f"Serial number '{serial}' is already assigned to asset '{existing.get('name', row['id'])}'.",
                )

    return doc


def apply_merge_patch(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """RFC 7396 JSON Merge Patch implementation."""
    result = copy.deepcopy(target)
    for key, value in patch.items():
        if key in ("id", "href"):
            continue
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = apply_merge_patch(result[key], value)
        else:
            result[key] = value
    return result


def project_fields(doc: dict[str, Any], fields: str | None) -> dict[str, Any]:
    """TMF639 attribute selection while always retaining `id` and `href` for UI actions."""
    if not fields:
        return doc
    wanted = {f.strip() for f in fields.split(",") if f.strip()}
    if not wanted:
        return doc
    wanted.update({"id", "href"})
    return {k: v for k, v in doc.items() if k in wanted}


def matches_filters(doc: dict[str, Any], filters: dict[str, str]) -> bool:
    for key, expected in filters.items():
        actual = doc.get(key)
        if actual is None:
            return False
        if isinstance(actual, (str, int, float, bool)):
            if str(actual) != expected:
                return False
        else:
            return False
    return True


app = FastAPI(
    title="TMF639 Resource Inventory Management API",
    version="4.0.1",
    description="TMF639 v4 Resource Inventory Management backend with SQLite persistence.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count", "X-Result-Count", "Location"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    reasons = {
        400: "Bad Request",
        404: "Not Found",
        409: "Conflict",
        415: "Unsupported Media Type",
        422: "Unprocessable Entity",
    }
    reason = reasons.get(exc.status_code, "Error")
    message = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
    return tmf_error(exc.status_code, reason, message)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = "; ".join(
        f"{'.'.join(str(loc) for loc in err.get('loc', []))}: {err.get('msg', 'invalid')}"
        for err in exc.errors()
    )
    return tmf_error(422, "Validation Error", details or "Invalid request payload.")


@app.get("/", include_in_schema=False)
def serve_frontend() -> Response:
    for filename in ("index_2.html", "index.html"):
        if os.path.exists(filename):
            return FileResponse(filename, media_type="text/html")
    return HTMLResponse(
        "<h1>TMF639 Resource Inventory API</h1><p>Place <code>index_2.html</code> in the working directory or visit <a href='/docs'>/docs</a>.</p>"
    )


@app.get(RESOURCE_BASE_PATH)
def list_resources(
    request: Request,
    response: Response,
    fields: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=10000),
) -> list[dict[str, Any]]:
    reserved = {"fields", "offset", "limit"}
    filters = {k: v for k, v in request.query_params.items() if k not in reserved and v != ""}

    with get_conn() as conn:
        rows = conn.execute("SELECT doc FROM resource ORDER BY rowid DESC").fetchall()

    matched: list[dict[str, Any]] = []
    for row in rows:
        doc = json.loads(row["doc"])
        if matches_filters(doc, filters):
            matched.append(doc)

    total_count = len(matched)
    page = matched[offset : offset + limit]
    projected = [project_fields(item, fields) for item in page]

    response.headers["X-Total-Count"] = str(total_count)
    response.headers["X-Result-Count"] = str(len(projected))
    return projected


@app.get(f"{RESOURCE_BASE_PATH}/{{resource_id}}")
def retrieve_resource(
    resource_id: str,
    fields: str | None = Query(default=None),
) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT doc FROM resource WHERE id = ?", (resource_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Resource '{resource_id}' not found.")
    return project_fields(json.loads(row["doc"]), fields)


@app.post(RESOURCE_BASE_PATH, status_code=status.HTTP_201_CREATED)
async def create_resource(request: Request, response: Response) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON.") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")

    resource_id = str(payload.get("id") or uuid.uuid4())
    with get_conn() as conn:
        exists = conn.execute("SELECT 1 FROM resource WHERE id = ?", (resource_id,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail=f"Resource '{resource_id}' already exists.")

        doc = validate_and_normalize_resource(payload, resource_id, conn, is_update=False)
        conn.execute(
            "INSERT INTO resource (id, doc) VALUES (?, ?)",
            (resource_id, json.dumps(doc)),
        )
        conn.commit()

    response.headers["Location"] = doc["href"]
    return doc


@app.patch(f"{RESOURCE_BASE_PATH}/{{resource_id}}")
async def patch_resource(resource_id: str, request: Request) -> dict[str, Any]:
    try:
        patch_body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Patch body must be valid JSON.") from exc

    if not isinstance(patch_body, dict):
        raise HTTPException(status_code=400, detail="Patch body must be a JSON object.")

    with get_conn() as conn:
        row = conn.execute("SELECT doc FROM resource WHERE id = ?", (resource_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Resource '{resource_id}' not found.")

        existing_doc = json.loads(row["doc"])
        merged = apply_merge_patch(existing_doc, patch_body)
        updated_doc = validate_and_normalize_resource(merged, resource_id, conn, is_update=True)

        conn.execute(
            "UPDATE resource SET doc = ? WHERE id = ?",
            (json.dumps(updated_doc), resource_id),
        )
        conn.commit()

    return updated_doc


@app.delete(f"{RESOURCE_BASE_PATH}/{{resource_id}}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resource(resource_id: str) -> Response:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM resource WHERE id = ?", (resource_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Resource '{resource_id}' not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(f"{API_PREFIX}/hub", response_model=EventSubscription, status_code=status.HTTP_201_CREATED)
def register_listener(subscription: EventSubscriptionInput, response: Response) -> EventSubscription:
    sub_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO hub_subscription (id, callback, query) VALUES (?, ?, ?)",
            (sub_id, subscription.callback, subscription.query),
        )
        conn.commit()
    response.headers["Location"] = f"{API_PREFIX}/hub/{sub_id}"
    return EventSubscription(id=sub_id, callback=subscription.callback, query=subscription.query)


@app.delete(f"{API_PREFIX}/hub/{{hub_id}}", status_code=status.HTTP_204_NO_CONTENT)
def unregister_listener(hub_id: str) -> Response:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM hub_subscription WHERE id = ?", (hub_id,))
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"Subscription '{hub_id}' not found.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)