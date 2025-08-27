#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
HiveTracks-BEEP-Sync.py
Refactored to:
  - Externalize mappings in JSON (see data/mappings.json)
  - Track imported HiveTracks record IDs in SQLite
  - Load credentials from .env
  - Keep CLI for mode flags only (import-from-file, dry-run, upload, etc.)

NOTE:
- The SQLite DB prevents re-imports by storing HiveTracks record IDs after a successful upload.
- The '1499' BEEP item used previously to carry the HiveTracks record id is no longer sent.

Author: (refactor for Christian)
"""

import argparse
import json
import logging
import os
import sys
import time
import sqlite3
from datetime import datetime
from typing import Dict, Any, List, Tuple

import requests
from tqdm import tqdm

# ---------- .env loader ----------
def load_env():
    """
    Loads environment variables from a .env file if python-dotenv is available.
    Falls back silently if not installed; in that case ensure vars are set in the environment.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except Exception:
        # No hard failure; user can still export env vars manually.
        pass

def require_env(name: str, default: str = None) -> str:
    val = os.getenv(name, default)
    if val is None:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

# ---------- Logging ----------
def setup_logger(log_file: str):
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format="%(asctime)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

def log(message: str):
    logging.info(message)

# ---------- CLI ----------
def parse_args():
    parser = argparse.ArgumentParser(description="Sync HiveTracks records to BEEP")
    parser.add_argument("--import-from-file", action="store_true", help="Read source/target data from local JSON files in ./data")
    parser.add_argument("--dry-run", action="store_true", help="Do not upload—write a preview JSON with transformed payloads")
    parser.add_argument("--upload", action="store_true", help="Upload transformed records to BEEP")
    parser.add_argument("--log-file", default="script-log.txt")
    parser.add_argument("--db-path", default="beep_sync.db", help="SQLite DB path for imported record IDs")
    parser.add_argument("--mappings-file", default=os.path.join("data", "mappings.json"))
    return parser.parse_args()

# ---------- SQLite ----------
def init_db(db_path: str):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS imported_records (
            record_id TEXT PRIMARY KEY,
            action_date TEXT,
            checklist_id TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()
    return conn

def get_imported_ids(conn) -> set:
    cur = conn.cursor()
    cur.execute("SELECT record_id FROM imported_records")
    return {row[0] for row in cur.fetchall()}

def mark_imported(conn, record_id: str, action_date: str, checklist_id: str):
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO imported_records (record_id, action_date, checklist_id) VALUES (?, ?, ?)",
        (record_id, action_date, checklist_id)
    )
    conn.commit()

# ---------- Files ----------
def load_json_file(path: str) -> Any:
    if not os.path.exists(path):
        log(f"File not found: {path}")
        raise FileNotFoundError(f"Missing file: {path}")
    log(f"Loaded file: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------- API helpers ----------
def get_beep_token(email: str, password: str) -> str:
    response = requests.post("https://api.beep.nl/api/login", data={
        "email": email,
        "password": password
    }, timeout=30)
    response.raise_for_status()
    token = response.json().get("api_token")
    if not token:
        raise Exception("No BEEP token received")
    return token

def get_hivetracks_token(email: str, password: str) -> str:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    body = {
        "json": {
            "email": email,
            "password": password
        }
    }
    response = requests.post("https://pro.hivetracks.com/api/trpc/auth.signin", headers=headers, json=body, timeout=30)
    response.raise_for_status()
    return response.json()["result"]["data"]["json"]["tokens"]["accessToken"]

def get_beep_data(url: str, token: str) -> Any:
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()

def get_page(url: str, token: str, page: int, page_size: int = 10) -> Any:
    headers = {
        "Content-Type": "application/json",
        "Cookie": f"access_token={token}"
    }
    input_payload = {
        "json": {"skip": page * page_size, "q": None},
        "meta": {"values": {"q": ["undefined"]}}
    }
    encoded_input = requests.utils.quote(json.dumps(input_payload))
    full_url = f"{url}?input={encoded_input}"
    response = requests.get(full_url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()["result"]["data"]["json"]["results"]

def get_hivetracks_records(token: str, page_size: int = 10) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    page = 0
    while True:
        log(f"Fetching HiveTracks page {page}")
        page_data = get_page("https://pro.hivetracks.com/api/trpc/admin.record.paginatedList", token, page, page_size)
        if not page_data:
            break
        records.extend(page_data)
        page += 1
    return records

def get_hivetracks_hives(token: str) -> Any:
    return get_page("https://pro.hivetracks.com/api/trpc/admin.hive.idList", token, 0)

# ---------- Mappings ----------
def load_mappings(path: str) -> Dict[str, Any]:
    m = load_json_file(path)
    # Basic validation / defaults
    m.setdefault("field_map", {})
    m.setdefault("enums", {})
    m.setdefault("stages_flags", {"field": "inspectionBroodStages", "flags": {}})
    m.setdefault("feeding_rules", [])
    m.setdefault("checklist_fallback", "TBD")
    return m

# ---------- Transformation ----------
def transform_records(
    records: List[Dict[str, Any]],
    beep_hives: Dict[str, Any],
    beep_checklists: Dict[str, Any],
    mappings: Dict[str, Any]
) -> List[Tuple[Dict[str, Any], str, str]]:
    """
    Returns a list of tuples: (beep_payload, hivetracks_record_id, checklist_id)
    """
    hive_map = {h["name"]: h["id"] for h in beep_hives.get("hives", []) if h.get("name") and h.get("id")}
    checklist_map = {c["name"]: c["id"] for c in beep_checklists.get("checklists", []) if c.get("name") and c.get("id")}
    checklist_fallback_name = mappings.get("checklist_fallback", "TBD")

    out: List[Tuple[Dict[str, Any], str, str]] = []

    for r in records:
        src_id = r.get("id")
        items: Dict[str, Any] = {}

        # 1) Direct field_map: {beep_item_id: hivetracks_field}
        for beep_item_id, ht_field in mappings.get("field_map", {}).items():
            if ht_field in r and r.get(ht_field) is not None:
                items[beep_item_id] = r.get(ht_field)

        # 2) Enums map: {ht_field: {ht_value: {beep_item_id: value, ...}}}
        for ht_field, value_map in mappings.get("enums", {}).items():
            ht_val = r.get(ht_field)
            if ht_val in value_map:
                for b_item, b_val in value_map[ht_val].items():
                    items[b_item] = b_val

        # 3) Stage flags (array membership -> boolean flags)
        stages_cfg = mappings.get("stages_flags", {})
        stages_field = stages_cfg.get("field", "inspectionBroodStages")
        stage_flags = stages_cfg.get("flags", {})
        stages = r.get(stages_field, []) or []
        for label, beep_item_id in stage_flags.items():
            if label in stages:
                items[str(beep_item_id)] = True

        # 4) Feeding rules (type / other text heuristics)
        food_type = r.get("feedBeesFoodType")
        food_other = r.get("feedBeesFoodTypeOther") or ""
        for rule in mappings.get("feeding_rules", []):
            if "if_type" in rule and food_type == rule["if_type"]:
                items.update(rule.get("set", {}))
            if "if_other_contains_any" in rule and any(x in food_other for x in rule["if_other_contains_any"]):
                items.update(rule.get("set", {}))
            if "if_other_contains" in rule and rule["if_other_contains"] in food_other:
                items.update(rule.get("set", {}))

        # 5) Hive IDs from Hive name(s)
        hive_ids = [hive_map.get(h["name"]) for h in r.get("hives", []) if hive_map.get(h["name"])]

        # 6) Checklist resolution
        rec_type = r.get("type")
        checklist_id = checklist_map.get(rec_type) or checklist_map.get(checklist_fallback_name)

        # 7) Reminder if checklist is missing
        reminder = None if rec_type in checklist_map else f"Missing: {r.get('type')}/{r.get('typeOther')}"

        # 8) Date normalization
        # input example "2024-05-11T12:34:56.789Z" -> "YYYY-MM-DDTHH:MM:SSZ"
        action_date = datetime.strptime(r["actionDate"], "%Y-%m-%dT%H:%M:%S.%fZ").strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = {
            "date": action_date,
            "checklist_id": checklist_id,
            "reminder": reminder,
            "notes": r.get("notes"),
            "hive_ids": hive_ids,
            "items": items
        }

        # IMPORTANT: we DO NOT set item '1499' anymore. We track src_id in SQLite.
        out.append((payload, str(src_id), str(checklist_id)))
    return out

# ---------- Upload ----------
def upload_records(transformed: List[Tuple[Dict[str, Any], str, str]], beep_token: str, conn):
    headers = {"Authorization": f"Bearer {beep_token}"}

    for payload, src_id, checklist_id in tqdm(
        transformed,
        desc="Uploading",
        unit="rec",
        dynamic_ncols=True,
        leave=True,
        disable=not sys.stdout.isatty()
    ):
        try:
            response = requests.post(
                "https://api.beep.nl/api/inspections/store",
                headers=headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            log(f"Uploaded inspection for date {payload.get('date')}")
            # Record success in DB
            mark_imported(conn, src_id, payload.get("date"), checklist_id)
        except Exception as e:
            log(f"Failed to upload inspection for date {payload.get('date')}: {e}")
        time.sleep(0.75)  # be polite to the API

# ---------- Main ----------
def main():
    load_env()
    args = parse_args()
    setup_logger(args.log_file)

    # Required credentials now come from environment
    HIVETRACKS_EMAIL = require_env("HIVETRACKS_EMAIL")
    HIVETRACKS_PASSWORD = require_env("HIVETRACKS_PASSWORD")
    BEEP_EMAIL = require_env("BEEP_EMAIL")
    BEEP_PASSWORD = require_env("BEEP_PASSWORD")

    # Init DB
    conn = init_db(args.db_path)

    try:
        if args.import_from_file:
            log("Importing data from files")
            hivetracks_records = load_json_file(os.path.join("data", "hivetracks-records.json"))
            beep_checklists = load_json_file(os.path.join("data", "beep-checklists.json"))
            beep_hives = load_json_file(os.path.join("data", "beep-hives.json"))
            # beep_inspections file is no longer used for dedup; DB is the source of truth.
        else:
            log("Importing data from APIs")
            ht_token = get_hivetracks_token(HIVETRACKS_EMAIL, HIVETRACKS_PASSWORD)
            hivetracks_records = get_hivetracks_records(ht_token)

            beep_token = get_beep_token(BEEP_EMAIL, BEEP_PASSWORD)
            beep_checklists = get_beep_data("https://api.beep.nl/api/inspections/lists", beep_token)
            beep_hives = get_beep_data("https://api.beep.nl/api/hives", beep_token)

        # Filter out records already imported (via SQLite)
        imported_ids = get_imported_ids(conn)
        records_to_import = [r for r in hivetracks_records if str(r.get("id")) not in imported_ids]

        mappings = load_mappings(args.mappings_file)
        transformed = transform_records(records_to_import, beep_hives, beep_checklists, mappings)

        if args.dry_run:
            preview_file = "beep-import-preview.json"
            with open(preview_file, "w", encoding="utf-8") as f:
                json.dump([p for (p, _src, _chk) in transformed], f, indent=2, ensure_ascii=False)
            log(f"Dry run complete. Preview saved to {preview_file}")
            print(f"✅ Dry run complete. Preview saved to {preview_file}")
            return

        if args.upload:
            log("Uploading records to BEEP API")
            beep_token = get_beep_token(BEEP_EMAIL, BEEP_PASSWORD)
            upload_records(transformed, beep_token, conn)

        print("✅ Transformation complete.")
        print(f"Records processed: {len(hivetracks_records)}")
        print(f"Records eligible for import (not in DB): {len(records_to_import)}")
        print(f"Records transformed: {len(transformed)}")
        log(f"Script completed successfully. Processed: {len(hivetracks_records)}, eligible: {len(records_to_import)}, transformed: {len(transformed)}")

    except Exception as e:
        log(f"ERROR: {e}")
        print(f"❌ ERROR: {e}")
        sys.exit(1)
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
