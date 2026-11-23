"""
TMF639 Resource Inventory Management (v4.0.1)
Run:  uvicorn main:app --reload
Web page:  http://127.0.0.1:8000/        API docs:  http://127.0.0.1:8000/docs
"""
import json
import sqlite3
import uuid
from enum import Enum
from http import HTTPStatus
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

# ---------------------------------------------------------------- 1. Setup
BASE = "/tmf-api/resourceInventoryManagement/v4"      # official TMF639 base path
HERE = Path(__file__).parent

app = FastAPI(title="Resource Inventory Management", version="4.0.1")

db = sqlite3.connect(HERE / "inventory.db", check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS resource (id TEXT PRIMARY KEY, doc TEXT)")


# ------------------------------------------------ 2. Enumerations (Resource Model)
class AdminState(str, Enum):
    locked = "locked"
    unlocked = "unlocked"
    shutdown = "shutdown"


class OperState(str, Enum):
    enable = "enable"
    disable = "disable"


class Status(str, Enum):
    standby = "standby"
    alarm = "alarm"
    available = "available"
    reserved = "reserved"
    unknown = "unknown"
    suspended = "suspended"


class Usage(str, Enum):
    idle = "idle"
    active = "active"
    busy = "busy"


# ------------------------------------- 3. Sub-classes (the boxes around Resource)
class _Base(BaseModel):
    model_config = ConfigDict(populate_by_name=True)


class Characteristic(_Base):
    id: str | None = None
    name: str
    value: Any
    valueType: str | None = None


class Note(_Base):
    id: str | None = None
    author: str | None = None
    date: str | None = None
    text: str


class Place(_Base):                       # RelatedPlaceRefOrValue
    id: str
    href: str | None = None
    name: str | None = None
    role: str | None = None
    referred: str | None = Field(None, alias="@referredType")


class RelatedParty(_Base):
    id: str
    href: str | None = None
    name: str | None = None
    role: str | None = None
    referred: str | None = Field(None, alias="@referredType")


class Attachment(_Base):                  # AttachmentRefOrValue
    id: str | None = None
    href: str | None = None
    attachmentType: str | None = None
    name: str | None = None
    description: str | None = None
    mimeType: str | None = None
    content: str | None = None            # base64 text
    url: str | None = None
    referred: str | None = Field(None, alias="@referredType")


class ResourceRef(_Base):
    id: str
    href: str | None = None
    name: str | None = None
    referred: str | None = Field(None, alias="@referredType")


class ResourceRelationship(_Base):
    relationshipType: str | None = None
    resource: ResourceRef | None = None


class ResourceSpecificationRef(_Base):
    id: str
    href: str | None = None
    name: str | None = None
    version: str | None = None
    referred: str | None = Field(None, alias="@referredType")


# -------------------------------------------------- 4. The Resource itself
class ResourceCreate(_Base):
    name: str = Field(min_length=1)
    category: str | None = None
    description: str | None = None
    resourceVersion: str | None = None
    administrativeState: AdminState | None = None
    operationalState: OperState | None = None
    resourceStatus: Status | None = None
    usageState: Usage | None = None
    startOperatingDate: str | None = None
    endOperatingDate: str | None = None
    activationFeature: list[dict] = []
    attachment: list[Attachment] = []
    note: list[Note] = []
    place: list[Place] = []
    relatedParty: list[RelatedParty] = []
    resourceCharacteristic: list[Characteristic] = []
    resourceRelationship: list[ResourceRelationship] = []
    resourceSpecification: ResourceSpecificationRef | None = None
    type_: str = Field("Resource", alias="@type")
    baseType: str | None = Field(None, alias="@baseType")
    schemaLocation: str | None = Field(None, alias="@schemaLocation")


def to_doc(model: ResourceCreate) -> dict:
    doc = model.model_dump(by_alias=True, exclude_none=True, mode="json")
    return {k: v for k, v in doc.items() if v != []}      # do not store empty lists


# ---------------------------------------------------- 5. TMF-style error format
def tmf_error(status: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": str(status),
            "reason": HTTPStatus(status).phrase,
            "message": message,
            "status": str(status),
            "@type": "Error",
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc):
    return tmf_error(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    parts = []
    for e in exc.errors():
        where = ".".join(str(p) for p in e["loc"] if p != "body")
        parts.append(f"{where}: {e['msg']}")
    return tmf_error(400, "; ".join(parts))


# ------------------------------------------------------- 6. Helper functions
def merge(target: dict, patch: dict) -> dict:
    """JSON Merge Patch (RFC 7396): only change what was sent; null removes."""
    for k, v in patch.items():
        if v is None:
            target.pop(k, None)
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            merge(target[k], v)
        else:
            target[k] = v
    return target


def get_doc(id: str) -> dict:
    row = db.execute("SELECT doc FROM resource WHERE id=?", (id,)).fetchone()
    if not row:
        raise HTTPException(404, f"Resource '{id}' not found")
    return json.loads(row[0])


def serial_of(doc: dict) -> str | None:
    for c in doc.get("resourceCharacteristic", []):
        if c.get("name") == "serialNumber" and str(c.get("value", "")).strip():
            return str(c["value"]).strip().lower()
    return None


def check_unique(doc: dict, ignore_id: str | None = None) -> None:
    """The serial number is the 'unique' field: no two assets may share it."""
    serial = serial_of(doc)
    if not serial:
        return
    for rid, raw in db.execute("SELECT id, doc FROM resource"):
        if rid != ignore_id and serial_of(json.loads(raw)) == serial:
            name = json.loads(raw).get("name", rid)
            raise HTTPException(409, f"Serial number '{serial}' is already used by '{name}'")


# --------------------------------------------------- 7. The five CRUD endpoints
@app.post(f"{BASE}/resource", status_code=201)                 # CREATE
def create(body: ResourceCreate):
    doc = to_doc(body)
    check_unique(doc)
    doc["id"] = str(uuid.uuid4())
    doc["href"] = f"{BASE}/resource/{doc['id']}"
    db.execute("INSERT INTO resource VALUES (?,?)", (doc["id"], json.dumps(doc)))
    db.commit()
    return doc


@app.get(f"{BASE}/resource")                                   # LIST
def list_resources(
    response: Response,
    fields: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=1000),
):
    rows = [json.loads(r[0]) for r in db.execute("SELECT doc FROM resource ORDER BY rowid")]
    page = rows[offset: offset + limit]
    if fields:
        keep = set(fields.split(",")) | {"id", "href"}
        page = [{k: v for k, v in d.items() if k in keep} for d in page]
    response.headers["X-Result-Count"] = str(len(page))
    response.headers["X-Total-Count"] = str(len(rows))
    return page


@app.get(f"{BASE}/resource/{{id}}")                            # RETRIEVE
def retrieve(id: str, fields: str | None = None):
    doc = get_doc(id)
    if fields:
        keep = set(fields.split(",")) | {"id", "href"}
        doc = {k: v for k, v in doc.items() if k in keep}
    return doc


@app.patch(f"{BASE}/resource/{{id}}")                          # PARTIAL UPDATE
def patch(id: str, body: dict = Body(...)):
    if "id" in body or "href" in body:
        raise HTTPException(400, "id and href cannot be patched")
    merged = merge(get_doc(id), body)
    try:                                   # re-check the result against the model
        clean = to_doc(ResourceCreate.model_validate(merged))
    except ValidationError as e:
        msg = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())
        raise HTTPException(400, msg)
    check_unique(clean, ignore_id=id)
    clean["id"] = id
    clean["href"] = f"{BASE}/resource/{id}"
    db.execute("UPDATE resource SET doc=? WHERE id=?", (json.dumps(clean), id))
    db.commit()
    return clean


@app.delete(f"{BASE}/resource/{{id}}", status_code=204)        # DELETE
def delete(id: str):
    get_doc(id)
    db.execute("DELETE FROM resource WHERE id=?", (id,))
    db.commit()


# ------------------------------------------------------------ 8. The web page
@app.get("/", include_in_schema=False)
def home():
    return FileResponse(HERE / "index.html")