# Simple TMF639 v4.0.1 Resource Inventory Web Connector

A lightweight, minimalist (black-and-white) CRUD web application and REST API built to demonstrate **TM Forum TMF639 (Resource Inventory Management v4.0.1)** communication patterns, payload structures, and lifecycle state transitions.

---

## Architecture & Tech Stack

- **Backend API:** Python 3, FastAPI, Pydantic (strict TMF639 schema validation & RFC 7396 JSON Merge Patch)
- **Database:** SQLite (`inventory.db` storing TMF639 JSON documents)
- **Frontend UI:** Vanilla HTML, CSS, and JavaScript (`fetch` API + live TMF639 JSON payload inspector)
- **API Standard:** TM Forum Open API — TMF639 Resource Inventory Management (`v4.0.1` / `v4.0.0.swagger.json`)

---

## Project Structure

```text
Simple-Inventory-web/
├── main.py          # FastAPI backend implementing TMF639 v4 CRUD endpoints
├── index.html       # Plain black-and-white CRUD interface & live JSON inspector
├── seed.py          # Generates randomized TMF639-compliant dummy assets (650–950 records)
└── inventory.db     # Local SQLite database (auto-generated when running seed.py or main.py)
