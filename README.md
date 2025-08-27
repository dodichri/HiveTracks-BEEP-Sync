
# HiveTracks–BEEP Sync

Synchronize inspection records from **HiveTracks** to the **BEEP** platform with:

- 🔁 **Idempotent imports** using a local **SQLite** database
- 🧭 **Externalized mappings** in `data/mappings.json`
- 🔐 Credentials loaded from a local **`.env`** file
- 📦 **Offline mode** (work from saved JSON files)
- 📊 A clean **tqdm** progress bar for uploads

> This project is the refactor of `hivetracks-to-BEEP.py` → **`HiveTracks-BEEP-Sync.py`**.

---

## ✨ What’s inside

- **`HiveTracks-BEEP-Sync.py`** – main script
- **`data/mappings.json`** – all transformation rules (no code changes needed)
- **SQLite DB (`beep_sync.db`)** – stores imported HiveTracks `record_id`s to prevent duplicates
- **`.env`** – stores `HIVETRACKS_*` and `BEEP_*` credentials (never commit this)

> The legacy BEEP item **`1499`** that used to carry the HiveTracks record ID is **no longer sent**. The SQLite DB is now the source of truth for de‑duplication.

---

## 🧰 Requirements

- Python **3.9+**
- `requests`, `python-dotenv`, `tqdm` (see `requirements.txt`)

Install:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## 🔐 Configure credentials (.env)

Create a file named **`.env`** in the project root:

```ini
# HiveTracks credentials
HIVETRACKS_EMAIL=you@example.com
HIVETRACKS_PASSWORD=your-hivetracks-password

# BEEP credentials
BEEP_EMAIL=you@example.com
BEEP_PASSWORD=your-beep-password
```

> If you prefer not to use `python-dotenv`, export these variables in your shell before running the script.

---

## 🗺️ Configure mappings (`data/mappings.json`)

All transformation logic is externalized. Example structure:

```json
{
  "field_map": { "744": "inspectionCoveredFrames", "615": "inspectionTemperature" },
  "enums": { "inspectionPopulation": { "Strong": { "1349": true }, "Weak": { "1349": false } } },
  "stages_flags": { "field": "inspectionBroodStages", "flags": { "Queen": "399", "Eggs": "270" } },
  "feeding_rules": [ { "if_type": "Pollen patty", "set": { "888": "855" } } ],
  "checklist_fallback": "TBD"
}
```

- `field_map`: direct copy from HiveTracks field → BEEP item id
- `enums`: value mapping per source field (turns labels into BEEP item/value pairs)
- `stages_flags`: array membership → boolean BEEP items
- `feeding_rules`: conditional rules based on feed type/notes
- `checklist_fallback`: checklist name to use when `type` doesn’t match

---

## 🚀 Usage

From the project root:

```bash
# Preview payloads without uploading (fetch from APIs)
python HiveTracks-BEEP-Sync.py --dry-run

# Upload to BEEP with a progress bar
python HiveTracks-BEEP-Sync.py --upload
```

### CLI options

```
--dry-run            Do not upload; write preview JSON (beep-import-preview.json)
--upload             Upload transformed records to BEEP
--log-file           Path to log file (default: script-log.txt)
--db-path            SQLite DB path (default: beep_sync.db)
--mappings-file      Path to mappings.json (default: data/mappings.json)
```

---

## 🗃️ Database (SQLite)

A single table stores imported record IDs to block duplicates on subsequent runs:

```sql
CREATE TABLE IF NOT EXISTS imported_records (
  record_id   TEXT PRIMARY KEY,
  action_date TEXT,
  checklist_id TEXT,
  created_at  TEXT DEFAULT (datetime('now'))
);
```

Inspect quickly:

```bash
sqlite3 beep_sync.db ".tables"
sqlite3 beep_sync.db "SELECT COUNT(*) FROM imported_records;"
```

> Coming from the old script and worried about historical imports? Consider a one‑time seeding step (see **Migration**).

---

## 🧪 Dry‑run preview

When using `--dry-run`, the script writes **`beep-import-preview.json`** so you can inspect exactly what would be sent to BEEP.

---

## 🐧 Running on Raspberry Pi (cron example)

```bash
crontab -e
```
Add a line (runs daily at 02:30):

```
30 2 * * * cd /home/pi/HiveTracks-BEEP-Sync && /usr/bin/python3 HiveTracks-BEEP-Sync.py --upload >> script-log.txt 2>&1
```

> Ensure your environment variables are available to cron (use `.env` with `python-dotenv`, or export them inside a small wrapper script that cron calls).

---

## 🛡️ Security

- Keep **`.env`** out of version control (already in `.gitignore`).
- Use app‑specific passwords or API tokens where possible.
- Logs may include dates and checklist IDs—avoid logging secrets.

---

## 🤝 Contributing

Issues and PRs are welcome—especially improvements to `mappings.json` examples, seeding helpers, and additional validation.

---

## 📜 License

MIT (add a `LICENSE` file to the repo if it’s not present).

