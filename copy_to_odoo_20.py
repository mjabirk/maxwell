#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

# =============================================================================
# PATHS & CREDENTIAL LOADER (.env is ignored by .gitignore)
# =============================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = SCRIPT_DIR / ".env"
DEFAULT_PASSWORDS_PATH = SCRIPT_DIR / ".user_passwords.json"  # ignored by .gitignore
STATE_DB_PATH = str(SCRIPT_DIR / "db.sqlite3")  # ignored by .gitignore

BATCH_SIZE = 100
ATTACHMENT_BATCH_SIZE = 5
HTTP_TIMEOUT = 90
MAX_RETRIES = 5

ENV_TEMPLATE = """# Source Server (Odoo 19)
ODOO19_URL=https://odoo19.example.com
ODOO19_DB=odoo19_db
ODOO19_API_KEY=CHANGE_ME

# Destination Server (Odoo 20 - ensure RPC scope is enabled)
ODOO20_URL=https://odoo20.example.com
ODOO20_DB=odoo20_db
ODOO20_API_KEY=CHANGE_ME
"""

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(SCRIPT_DIR / "migration.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("odoo_migrator")


def load_credentials(env_path: Path) -> Tuple[Dict[str, str], Dict[str, str]]:
    if not env_path.exists():
        env_path.write_text(ENV_TEMPLATE, encoding="utf-8")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
        log.error(
            "Created template '%s' (git-ignored). Fill in both server credentials and re-run.",
            env_path,
        )
        sys.exit(1)

    values: Dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        values[key] = val

    required = [
        "ODOO19_URL", "ODOO19_DB", "ODOO19_API_KEY",
        "ODOO20_URL", "ODOO20_DB", "ODOO20_API_KEY",
    ]
    missing = [k for k in required if not values.get(k) or values[k] == "CHANGE_ME"]
    if missing:
        log.error("Missing or placeholder values in %s: %s", env_path, ", ".join(missing))
        sys.exit(1)

    src_cfg = {
        "url": values["ODOO19_URL"],
        "db": values["ODOO19_DB"],
        "api_key": values["ODOO19_API_KEY"],
        "pg_dsn": values.get("ODOO19_PG_DSN") or os.environ.get("ODOO19_PG_DSN") or "",
    }
    dst_cfg = {
        "url": values["ODOO20_URL"],
        "db": values["ODOO20_DB"],
        "api_key": values["ODOO20_API_KEY"],
        "pg_dsn": values.get("ODOO20_PG_DSN") or os.environ.get("ODOO20_PG_DSN") or "",
        "default_user_password": (
            values.get("ODOO20_DEFAULT_USER_PASSWORD")
            or os.environ.get("ODOO20_DEFAULT_USER_PASSWORD")
            or ""
        ),
    }
    return src_cfg, dst_cfg


# =============================================================================
# SQLITE CHECKPOINT / RESUME STORE
# =============================================================================
class StateStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=DELETE;")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS id_map (
                model TEXT NOT NULL,
                src_id INTEGER NOT NULL,
                dst_id INTEGER NOT NULL,
                meta TEXT,
                PRIMARY KEY (model, src_id)
            );
            CREATE INDEX IF NOT EXISTS idx_id_map_dst ON id_map (model, dst_id);

            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phase TEXT NOT NULL,
                model TEXT NOT NULL,
                src_id INTEGER NOT NULL,
                error TEXT NOT NULL,
                resolved INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_errors_unique
                ON errors (phase, model, src_id);

            CREATE TABLE IF NOT EXISTS phase_progress (
                phase TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_passwords (
                src_id INTEGER PRIMARY KEY,
                login TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                applied_dst_id INTEGER
            );
            """
        )
        self.conn.commit()

    def reset(self):
        self.conn.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.conn = sqlite3.connect(self.db_path)
        self._init_schema()

    def get_dst(self, model: str, src_id: Optional[int]) -> Optional[int]:
        if not src_id:
            return None
        row = self.conn.execute(
            "SELECT dst_id FROM id_map WHERE model = ? AND src_id = ?",
            (model, int(src_id)),
        ).fetchone()
        return row[0] if row else None

    def get_src(self, model: str, dst_id: Optional[int]) -> Optional[int]:
        if not dst_id:
            return None
        row = self.conn.execute(
            "SELECT src_id FROM id_map WHERE model = ? AND dst_id = ?",
            (model, int(dst_id)),
        ).fetchone()
        return row[0] if row else None

    def get_all_map(self, model: str) -> Dict[int, int]:
        rows = self.conn.execute(
            "SELECT src_id, dst_id FROM id_map WHERE model = ?", (model,)
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def set_map(self, model: str, src_id: int, dst_id: int, meta: Optional[dict] = None):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO id_map (model, src_id, dst_id, meta)
            VALUES (?, ?, ?, ?)
            """,
            (model, int(src_id), int(dst_id), json.dumps(meta) if meta else None),
        )
        self.conn.commit()

    def log_error(self, phase: str, model: str, src_id: int, error: str):
        self.conn.execute(
            """
            INSERT INTO errors (phase, model, src_id, error, resolved)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(phase, model, src_id) DO UPDATE SET
                error = excluded.error,
                resolved = 0,
                created_at = CURRENT_TIMESTAMP
            """,
            (phase, model, int(src_id), str(error)[:4000]),
        )
        self.conn.commit()

    def resolve_error(self, phase: str, model: str, src_id: int):
        self.conn.execute(
            "UPDATE errors SET resolved = 1 WHERE phase = ? AND model = ? AND src_id = ?",
            (phase, model, int(src_id)),
        )
        self.conn.commit()

    def get_unresolved_errors(self, phase: Optional[str] = None) -> List[Tuple[str, str, int, str]]:
        if phase:
            return self.conn.execute(
                "SELECT phase, model, src_id, error FROM errors WHERE resolved = 0 AND phase = ?",
                (phase,),
            ).fetchall()
        return self.conn.execute(
            "SELECT phase, model, src_id, error FROM errors WHERE resolved = 0"
        ).fetchall()

    def set_phase(self, phase: str, status: str):
        self.conn.execute(
            """
            INSERT OR REPLACE INTO phase_progress (phase, status, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            """,
            (phase, status),
        )
        self.conn.commit()

    def save_user_password(self, src_id: int, login: str, password_hash: str):
        self.conn.execute(
            """
            INSERT INTO user_passwords (src_id, login, password_hash)
            VALUES (?, ?, ?)
            ON CONFLICT(src_id) DO UPDATE SET
                login = excluded.login,
                password_hash = excluded.password_hash
            """,
            (int(src_id), str(login), str(password_hash)),
        )
        self.conn.commit()

    def mark_password_applied(self, src_id: int, dst_id: int):
        self.conn.execute(
            "UPDATE user_passwords SET applied_dst_id = ? WHERE src_id = ?",
            (int(dst_id), int(src_id)),
        )
        self.conn.commit()

    def get_user_password(self, src_id: int, login: Optional[str] = None) -> Tuple[Optional[str], Optional[int]]:
        row = self.conn.execute(
            "SELECT password_hash, applied_dst_id FROM user_passwords WHERE src_id = ?",
            (int(src_id),),
        ).fetchone()
        if row:
            return row[0], row[1]
        if login:
            row = self.conn.execute(
                "SELECT password_hash, applied_dst_id FROM user_passwords WHERE login = ?",
                (str(login),),
            ).fetchone()
            if row:
                return row[0], row[1]
        return None, None

    def get_all_user_passwords(self) -> Dict[int, str]:
        rows = self.conn.execute(
            "SELECT src_id, password_hash FROM user_passwords"
        ).fetchall()
        return {r[0]: r[1] for r in rows}


# =============================================================================
# ODOO JSON-2 API CLIENT WITH AUTOMATIC RETRIES
# =============================================================================
class OdooJSON2Client:
    def __init__(self, url: str, db: str, api_key: str, label: str = "Odoo"):
        self.base_url = url.rstrip("/")
        self.db = db
        self.label = label
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"bearer {api_key}",
                "X-Odoo-Database": db,
                "Content-Type": "application/json",
                "User-Agent": "odoo19-to-20-accounting-migrator/1.0",
            }
        )
        self._fields_cache: Dict[str, Dict[str, Any]] = {}

    def call(
        self,
        model: str,
        method: str,
        payload: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}/json/2/{model}/{method}"
        body = dict(payload or {})
        if context is not None:
            merged_ctx = dict(body.get("context") or {})
            merged_ctx.update(context)
            body["context"] = merged_ctx

        last_err = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self.session.post(url, json=body, timeout=HTTP_TIMEOUT)
                if resp.status_code in (429, 502, 503, 504):
                    wait = min(2 ** attempt, 30)
                    log.warning(
                        "[%s] HTTP %s on %s.%s (attempt %d/%d), retrying in %ss...",
                        self.label, resp.status_code, model, method, attempt, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue

                if resp.status_code >= 400:
                    try:
                        err_json = resp.json()
                        msg = err_json.get("message") or err_json.get("debug") or resp.text
                    except Exception:
                        msg = resp.text
                    raise RuntimeError(
                        f"[{self.label}] {model}.{method} HTTP {resp.status_code}: {msg}"
                    )

                return resp.json()
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                last_err = exc
                wait = min(2 ** attempt, 30)
                log.warning(
                    "[%s] Network error on %s.%s (attempt %d/%d): %s. Retrying in %ss...",
                    self.label, model, method, attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)

        raise RuntimeError(f"[{self.label}] {model}.{method} failed after {MAX_RETRIES} retries: {last_err}")

    def check_connection(self) -> Dict[str, Any]:
        resp = self.session.get(f"{self.base_url}/web/version", timeout=20)
        resp.raise_for_status()
        version_info = resp.json()
        # Verify auth + RPC scope with a lightweight search
        self.call("res.company", "search", {"domain": [], "limit": 1})
        return version_info

    LEGACY_TZ_ALIASES = {
        "Asia/Calcutta": "Asia/Kolkata",
        "Asia/Katmandu": "Asia/Kathmandu",
        "Asia/Saigon": "Asia/Ho_Chi_Minh",
        "Asia/Rangoon": "Asia/Yangon",
        "Asia/Dacca": "Asia/Dhaka",
        "Europe/Kiev": "Europe/Kyiv",
    }

    def fields_get(self, model: str) -> Dict[str, Any]:
        if model not in self._fields_cache:
            try:
                self._fields_cache[model] = self.call(
                    model,
                    "fields_get",
                    {"attributes": ["type", "relation", "readonly", "store", "required", "selection"]},
                )
            except Exception:
                self._fields_cache[model] = {}
        return self._fields_cache[model]

    def has_field(self, model: str, field_name: str) -> bool:
        return field_name in self.fields_get(model)

    def filter_existing_fields(self, model: str, fields: List[str]) -> List[str]:
        meta = self.fields_get(model)
        return [f for f in fields if f in meta]

    def sanitize_vals(self, model: str, vals: Dict[str, Any]) -> Dict[str, Any]:
        """Filter unknown fields, validate selections, and sanitize nested x2many create/write dicts."""
        meta = self.fields_get(model)
        clean: Dict[str, Any] = {}
        for k, v in vals.items():
            fmeta = meta.get(k)
            if not fmeta:
                continue
            ftype = fmeta.get("type")
            if ftype in ("one2many", "many2many") and fmeta.get("relation") and isinstance(v, list):
                rel_model = fmeta["relation"]
                new_cmds = []
                for cmd in v:
                    if isinstance(cmd, (list, tuple)) and len(cmd) == 3 and cmd[0] in (0, 1) and isinstance(cmd[2], dict):
                        new_cmds.append([cmd[0], cmd[1], self.sanitize_vals(rel_model, cmd[2])])
                    else:
                        new_cmds.append(cmd)
                clean[k] = new_cmds
                continue
            if ftype == "selection" and isinstance(v, str) and fmeta.get("selection"):
                allowed = {opt[0] for opt in fmeta["selection"]}
                cand = self.LEGACY_TZ_ALIASES.get(v, v) if k == "tz" else v
                if cand not in allowed:
                    log.warning("Invalid selection value %r for %s.%s; normalizing.", v, model, k)
                    if fmeta.get("required"):
                        cand = "Asia/Qatar" if "Asia/Qatar" in allowed else fmeta["selection"][0][0]
                    else:
                        cand = False
                clean[k] = cand
                continue
            clean[k] = v
        return clean

    def search(
        self,
        model: str,
        domain: list,
        offset: int = 0,
        limit: Optional[int] = None,
        order: Optional[str] = None,
        context: Optional[dict] = None,
    ) -> List[int]:
        payload: Dict[str, Any] = {"domain": domain, "offset": offset}
        if limit is not None:
            payload["limit"] = limit
        if order:
            payload["order"] = order
        return self.call(model, "search", payload, context=context)

    def search_read(
        self,
        model: str,
        domain: list,
        fields: List[str],
        offset: int = 0,
        limit: Optional[int] = None,
        order: Optional[str] = None,
        context: Optional[dict] = None,
    ) -> List[Dict[str, Any]]:
        valid_fields = self.filter_existing_fields(model, fields)
        payload: Dict[str, Any] = {"domain": domain, "fields": valid_fields, "offset": offset}
        if limit is not None:
            payload["limit"] = limit
        if order:
            payload["order"] = order
        return self.call(model, "search_read", payload, context=context)

    def read(
        self,
        model: str,
        ids: List[int],
        fields: List[str],
        context: Optional[dict] = None,
    ) -> List[Dict[str, Any]]:
        if not ids:
            return []
        valid_fields = self.filter_existing_fields(model, fields)
        return self.call(model, "read", {"ids": ids, "fields": valid_fields}, context=context)

    def create(self, model: str, vals: Dict[str, Any], context: Optional[dict] = None) -> int:
        # Strip fields that do not exist on target Odoo 20 model (including nested x2many dicts)
        clean_vals = self.sanitize_vals(model, vals)
        res = self.call(model, "create", {"vals_list": [clean_vals]}, context=context)
        return res[0] if isinstance(res, list) else int(res)

    def write(self, model: str, ids: List[int], vals: Dict[str, Any], context: Optional[dict] = None):
        clean_vals = self.sanitize_vals(model, vals)
        if not clean_vals:
            return
        return self.call(model, "write", {"ids": ids, "vals": clean_vals}, context=context)


# =============================================================================
# HELPERS
# =============================================================================
def m2o_id(val: Any) -> Optional[int]:
    if isinstance(val, (list, tuple)) and val:
        return int(val[0])
    if isinstance(val, int) and val:
        return val
    return None


def m2o_name(val: Any) -> str:
    if isinstance(val, (list, tuple)) and len(val) > 1:
        return str(val[1])
    return ""


def chunked(seq: List[Any], size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# =============================================================================
# ACCOUNTING MIGRATOR
# =============================================================================
class AccountingMigrator:
    PROTECTED_USER_IDS = {1, 2}
    IGNORED_TEMPLATE_LOGINS = {"__system__", "public", "portaltemplate"}

    def __init__(
        self,
        src: OdooJSON2Client,
        dst: OdooJSON2Client,
        state: StateStore,
        dry_run: bool = False,
        only_company_id: Optional[int] = None,
        skip_attachments: bool = False,
        skip_chatter: bool = False,
        allow_legacy_import: bool = True,
        src_pg_dsn: str = "",
        dst_pg_dsn: str = "",
        passwords_file: Optional[Path] = DEFAULT_PASSWORDS_PATH,
        default_user_password: str = "",
        skip_users_hr: bool = False,
        only_users_hr: bool = False,
        skip_employee_documents: bool = False,
        only_employee_documents: bool = False,
    ):
        self.src = src
        self.dst = dst
        self.state = state
        self.dry_run = dry_run
        self.only_company_id = only_company_id
        self.skip_attachments = skip_attachments
        self.skip_chatter = skip_chatter
        self.allow_legacy_import = allow_legacy_import
        self.src_pg_dsn = src_pg_dsn or ""
        self.dst_pg_dsn = dst_pg_dsn or ""
        self.passwords_file = Path(passwords_file) if passwords_file else None
        self.default_user_password = default_user_password or ""
        self.skip_users_hr = skip_users_hr
        self.only_users_hr = only_users_hr
        self.skip_employee_documents = skip_employee_documents
        self.only_employee_documents = only_employee_documents

        self.src_ctx_all = {"active_test": False}
        self.dst_ctx_quiet = {
            "active_test": False,
            "tracking_disable": True,
            "mail_notrack": True,
            "mail_create_nosubscribe": True,
            "mail_create_nolog": True,
            "mail_notify_force_send": False,
            "no_reset_password": True,
        }
        self._src_banks_by_id: Optional[Dict[int, Dict[str, Any]]] = None
        self._dst_company_country_cache: Dict[int, Optional[int]] = {}
        self._hr_struct_default_cache: Dict[int, Optional[int]] = {}
        self._hr_emp_type_cache: Dict[str, int] = {}
        self._src_contract_types_cache: Dict[int, str] = {}
        self._wet_map: Dict[int, int] = {}
        self._dst_state_country_cache: Optional[Dict[int, int]] = None

    @staticmethod
    def _extract_b64(val: Any) -> Optional[str]:
        if not val:
            return None
        if isinstance(val, dict):
            val = val.get("content") or val.get("datas") or val.get("raw")
        if isinstance(val, bytes):
            decoded = val.decode("ascii", errors="ignore")
            return decoded if decoded else None
        if isinstance(val, str) and val.strip():
            return val
        return None

    def _get_src_banks_by_id(self) -> Dict[int, Dict[str, Any]]:
        """Lazily load source res.bank records for Odoo 20+ inline partner bank mapping."""
        if self._src_banks_by_id is None:
            if self.src.fields_get("res.bank"):
                banks = self.src.search_read(
                    "res.bank",
                    [],
                    ["id", "name", "bic", "street", "street2", "zip", "city", "state", "country", "active"],
                    context=self.src_ctx_all,
                )
                self._src_banks_by_id = {b["id"]: b for b in banks}
            else:
                self._src_banks_by_id = {}
        return self._src_banks_by_id

    def _get_dst_company_country_id(self, dst_cid: int, dst_ctx: dict) -> Optional[int]:
        """Return cached destination company country_id for fallback on required tax fields."""
        if dst_cid not in self._dst_company_country_cache:
            rows = self.dst.read("res.company", [dst_cid], ["country_id"], context=dst_ctx)
            self._dst_company_country_cache[dst_cid] = m2o_id(rows[0].get("country_id")) if rows else None
        return self._dst_company_country_cache[dst_cid]

    def _ensure_bank_accounts_trusted(self, bank_ids: Optional[List[int]] = None):
        """Mark destination res.partner.bank records as trusted (allow_out_payment=True)
        so Odoo 20 allows posting migrated customer invoices and outbound payments linked to them.
        """
        if self.dry_run or not self.dst.has_field("res.partner.bank", "allow_out_payment"):
            return
        if bank_ids is None:
            untrusted_ids = self.dst.search(
                "res.partner.bank",
                [["allow_out_payment", "=", False]],
                context=self.dst_ctx_quiet,
            )
        else:
            target_ids = sorted({int(bid) for bid in bank_ids if bid})
            if not target_ids:
                return
            rows = self.dst.read(
                "res.partner.bank",
                target_ids,
                ["id", "allow_out_payment"],
                context=self.dst_ctx_quiet,
            )
            untrusted_ids = [r["id"] for r in rows if not r.get("allow_out_payment")]
        if not untrusted_ids:
            return
        trusted_count = 0
        for bid in untrusted_ids:
            try:
                self.dst.write(
                    "res.partner.bank",
                    [bid],
                    {"allow_out_payment": True},
                    context=self.dst_ctx_quiet,
                )
                trusted_count += 1
            except Exception as exc:
                log.warning("Could not trust destination bank account %d: %s", bid, exc)
        if trusted_count:
            log.info("Marked %d destination bank account(s) as trusted (allow_out_payment=True).", trusted_count)

    def dst_company_ctx(self, src_company_id: Optional[int], extra: Optional[dict] = None) -> dict:
        ctx = dict(self.dst_ctx_quiet)
        if src_company_id:
            dst_cid = self.state.get_dst("res.company", src_company_id)
            if dst_cid:
                ctx["allowed_company_ids"] = [dst_cid]
                ctx["company_id"] = dst_cid
        if extra:
            ctx.update(extra)
        return ctx

    def src_company_ctx(self, src_company_id: Optional[int]) -> dict:
        ctx = dict(self.src_ctx_all)
        if src_company_id:
            ctx["allowed_company_ids"] = [src_company_id]
            ctx["company_id"] = src_company_id
        return ctx

    def get_source_company_ids(self) -> List[int]:
        if self.only_company_id:
            return [self.only_company_id]
        return self.src.search("res.company", [], order="parent_id nulls first, id asc")

    # -------------------------------------------------------------------------
    # STEP 1: COMPANIES & CURRENCIES
    # -------------------------------------------------------------------------
    def migrate_companies(self):
        log.info("=== Phase 1: Companies & Currencies ===")
        src_cids = self.get_source_company_ids()
        src_companies = self.src.read(
            "res.company", src_cids, ["id", "name", "vat", "currency_id", "parent_id"]
        )
        dst_companies = self.dst.search_read(
            "res.company", [], ["id", "name", "vat"], context=self.dst_ctx_quiet
        )

        # Map Currencies first
        self.migrate_currencies()

        for sc in src_companies:
            if self.state.get_dst("res.company", sc["id"]):
                continue
            match = None
            for dc in dst_companies:
                if (sc.get("vat") and dc.get("vat") == sc["vat"]) or (
                    dc["name"].strip().lower() == sc["name"].strip().lower()
                ):
                    match = dc
                    break
            if not match and len(src_companies) == 1 and len(dst_companies) == 1:
                match = dst_companies[0]
                log.warning(
                    "Single-company fallback: mapping source '%s' -> dest '%s'",
                    sc["name"], match["name"],
                )

            if match:
                self.state.set_map("res.company", sc["id"], match["id"])
                log.info("Mapped company: %s (%d -> %d)", sc["name"], sc["id"], match["id"])
            else:
                if self.dry_run:
                    log.info("[DRY-RUN] Would create company: %s", sc["name"])
                    continue
                vals = {
                    "name": sc["name"],
                    "vat": sc.get("vat") or False,
                    "currency_id": self.state.get_dst("res.currency", m2o_id(sc.get("currency_id"))),
                }
                if m2o_id(sc.get("parent_id")):
                    vals["parent_id"] = self.state.get_dst("res.company", m2o_id(sc["parent_id"]))
                dst_id = self.dst.create("res.company", vals, context=self.dst_ctx_quiet)
                self.state.set_map("res.company", sc["id"], dst_id)
                log.info("Created company: %s (%d -> %d)", sc["name"], sc["id"], dst_id)

        self.migrate_currency_rates()

    def migrate_currencies(self):
        src_curr = self.src.search_read(
            "res.currency", [], ["id", "name", "active"], context=self.src_ctx_all
        )
        dst_curr = self.dst.search_read(
            "res.currency", [], ["id", "name", "active"], context=self.dst_ctx_quiet
        )
        dst_by_code = {c["name"]: c for c in dst_curr}

        for sc in src_curr:
            if self.state.get_dst("res.currency", sc["id"]):
                continue
            dc = dst_by_code.get(sc["name"])
            if dc:
                if sc.get("active") and not dc.get("active") and not self.dry_run:
                    self.dst.write("res.currency", [dc["id"]], {"active": True})
                self.state.set_map("res.currency", sc["id"], dc["id"])

    def migrate_currency_rates(self):
        for src_cid in self.get_source_company_ids():
            dst_cid = self.state.get_dst("res.company", src_cid)
            if not dst_cid:
                continue
            rates = self.src.search_read(
                "res.currency.rate",
                [["company_id", "=", src_cid]],
                ["id", "name", "rate", "currency_id", "company_id"],
                order="name asc, id asc",
                context=self.src_company_ctx(src_cid),
            )
            for r in rates:
                if self.state.get_dst("res.currency.rate", r["id"]):
                    continue
                dst_curr_id = self.state.get_dst("res.currency", m2o_id(r["currency_id"]))
                if not dst_curr_id:
                    continue
                existing = self.dst.search(
                    "res.currency.rate",
                    [
                        ["company_id", "=", dst_cid],
                        ["currency_id", "=", dst_curr_id],
                        ["name", "=", r["name"]],
                    ],
                    limit=1,
                    context=self.dst_company_ctx(src_cid),
                )
                if existing:
                    self.state.set_map("res.currency.rate", r["id"], existing[0])
                elif not self.dry_run:
                    dst_id = self.dst.create(
                        "res.currency.rate",
                        {
                            "name": r["name"],
                            "rate": r["rate"],
                            "currency_id": dst_curr_id,
                            "company_id": dst_cid,
                        },
                        context=self.dst_company_ctx(src_cid),
                    )
                    self.state.set_map("res.currency.rate", r["id"], dst_id)

    # -------------------------------------------------------------------------
    # STEP 2: COUNTRIES, STATES, BANKS, ACCOUNT TAGS
    # -------------------------------------------------------------------------
    def migrate_geo_and_tags(self):
        log.info("=== Phase 2: Countries, States, Banks & Account Tags ===")
        # Countries
        dst_countries = {
            c["code"]: c["id"]
            for c in self.dst.search_read("res.country", [], ["id", "code"])
            if c.get("code")
        }
        for c in self.src.search_read("res.country", [], ["id", "code"]):
            if not self.state.get_dst("res.country", c["id"]) and c.get("code") in dst_countries:
                self.state.set_map("res.country", c["id"], dst_countries[c["code"]])

        # States
        dst_states = {}
        for s in self.dst.search_read("res.country.state", [], ["id", "code", "country_id"]):
            dst_states[(m2o_id(s["country_id"]), s.get("code"))] = s["id"]
        for s in self.src.search_read("res.country.state", [], ["id", "code", "name", "country_id"]):
            if self.state.get_dst("res.country.state", s["id"]):
                continue
            dst_country = self.state.get_dst("res.country", m2o_id(s["country_id"]))
            if not dst_country:
                continue
            match = dst_states.get((dst_country, s.get("code")))
            if match:
                self.state.set_map("res.country.state", s["id"], match)
            elif not self.dry_run:
                dst_id = self.dst.create(
                    "res.country.state",
                    {"name": s["name"], "code": s["code"], "country_id": dst_country},
                )
                self.state.set_map("res.country.state", s["id"], dst_id)

        # Banks (Odoo 20 removed res.bank and merged bank attributes onto res.partner.bank)
        src_has_res_bank = bool(self.src.fields_get("res.bank"))
        dst_has_res_bank = bool(self.dst.fields_get("res.bank"))
        if src_has_res_bank and dst_has_res_bank:
            for b in self.src.search_read(
                "res.bank", [], ["id", "name", "bic", "country", "active"], context=self.src_ctx_all
            ):
                if self.state.get_dst("res.bank", b["id"]):
                    continue
                domain = [["bic", "=", b["bic"]]] if b.get("bic") else [["name", "=", b["name"]]]
                existing = self.dst.search("res.bank", domain, limit=1, context=self.dst_ctx_quiet)
                if existing:
                    self.state.set_map("res.bank", b["id"], existing[0])
                elif not self.dry_run:
                    vals = {
                        "name": b["name"],
                        "bic": b.get("bic") or False,
                        "country": self.state.get_dst("res.country", m2o_id(b.get("country"))) or False,
                        "active": b.get("active", True),
                    }
                    dst_id = self.dst.create("res.bank", vals, context=self.dst_ctx_quiet)
                    self.state.set_map("res.bank", b["id"], dst_id)
        elif src_has_res_bank and not dst_has_res_bank:
            src_banks = self._get_src_banks_by_id()
            log.info(
                "Destination has no 'res.bank' model (Odoo 20+); cached %d source banks to inline into res.partner.bank in Phase 6.",
                len(src_banks),
            )

        # Account Tags
        for t in self.src.search_read(
            "account.account.tag",
            [],
            ["id", "name", "applicability", "country_id", "tax_negate", "active"],
            context=self.src_ctx_all,
        ):
            if self.state.get_dst("account.account.tag", t["id"]):
                continue
            dst_country = self.state.get_dst("res.country", m2o_id(t.get("country_id")))
            domain = [
                ["name", "=", t["name"]],
                ["applicability", "=", t["applicability"]],
            ]
            if dst_country:
                domain.append(["country_id", "=", dst_country])
            existing = self.dst.search(
                "account.account.tag", domain, limit=1, context=self.dst_ctx_quiet
            )
            if existing:
                self.state.set_map("account.account.tag", t["id"], existing[0])
            elif not self.dry_run:
                vals = {
                    "name": t["name"],
                    "applicability": t["applicability"],
                    "country_id": dst_country or False,
                    "tax_negate": t.get("tax_negate", False),
                    "active": t.get("active", True),
                }
                dst_id = self.dst.create("account.account.tag", vals, context=self.dst_ctx_quiet)
                self.state.set_map("account.account.tag", t["id"], dst_id)

    # -------------------------------------------------------------------------
    # STEP 3: CHART OF ACCOUNTS (Handles Odoo 19 -> 20 account.group removal)
    # -------------------------------------------------------------------------
    def migrate_accounts(self):
        log.info("=== Phase 3: Chart of Accounts (account.account) ===")
        dst_uses_company_ids = self.dst.has_field("account.account", "company_ids")
        dst_has_active = self.dst.has_field("account.account", "active")
        dst_has_deprecated = self.dst.has_field("account.account", "deprecated")

        for src_cid in self.get_source_company_ids():
            dst_cid = self.state.get_dst("res.company", src_cid)
            if not dst_cid:
                continue
            src_ctx = self.src_company_ctx(src_cid)
            dst_ctx = self.dst_company_ctx(src_cid)

            src_accounts = self.src.search_read(
                "account.account",
                [],
                [
                    "id", "code", "name", "account_type", "reconcile",
                    "currency_id", "tag_ids", "note", "active", "deprecated",
                    "non_trade", "company_ids", "company_id",
                ],
                order="code asc, id asc",
                context=src_ctx,
            )

            # Filter accounts belonging to src_cid
            filtered = []
            for acc in src_accounts:
                cids = acc.get("company_ids") or []
                cid = m2o_id(acc.get("company_id"))
                if (cids and src_cid in cids) or (cid == src_cid) or (not cids and not cid):
                    filtered.append(acc)

            # Build existing dest lookup by code
            dst_existing = self.dst.search_read(
                "account.account",
                [],
                ["id", "code", "name", "reconcile", "company_ids", "company_id"],
                context=dst_ctx,
            )
            dst_by_code = {}
            dst_reconcile_by_id = {}
            for da in dst_existing:
                dcids = da.get("company_ids") or []
                dcid = m2o_id(da.get("company_id"))
                if (dcids and dst_cid in dcids) or (dcid == dst_cid) or (not dcids and not dcid):
                    if da.get("code"):
                        dst_by_code[str(da["code"]).strip()] = da["id"]
                        dst_reconcile_by_id[da["id"]] = bool(da.get("reconcile"))

            created_cnt = 0
            mapped_cnt = 0
            for acc in filtered:
                existing_mapped = self.state.get_dst("account.account", acc["id"])
                if existing_mapped:
                    # Ensure company_ids includes dst_cid in multi-company setups
                    if dst_uses_company_ids and not self.dry_run:
                        self.dst.write(
                            "account.account",
                            [existing_mapped],
                            {"company_ids": [[4, dst_cid]]},
                            context=dst_ctx,
                        )
                    if acc.get("reconcile") and not dst_reconcile_by_id.get(existing_mapped, True) and not self.dry_run:
                        try:
                            self.dst.write("account.account", [existing_mapped], {"reconcile": True}, context=dst_ctx)
                            dst_reconcile_by_id[existing_mapped] = True
                        except Exception as exc:
                            log.warning("Could not enable reconcile on account (%d): %s", existing_mapped, exc)
                    mapped_cnt += 1
                    continue

                code = str(acc.get("code") or "").strip()
                if code and code in dst_by_code:
                    dst_aid = dst_by_code[code]
                    if acc.get("reconcile") and not dst_reconcile_by_id.get(dst_aid) and not self.dry_run:
                        try:
                            self.dst.write("account.account", [dst_aid], {"reconcile": True}, context=dst_ctx)
                            dst_reconcile_by_id[dst_aid] = True
                        except Exception as exc:
                            log.warning("Could not enable reconcile on account [%s] (%d): %s", code, dst_aid, exc)
                    self.state.set_map("account.account", acc["id"], dst_aid)
                    mapped_cnt += 1
                    continue

                if self.dry_run:
                    log.info("[DRY-RUN] Missing account -> would create: %s %s", code, acc["name"])
                    created_cnt += 1
                    continue

                tag_ids = [
                    self.state.get_dst("account.account.tag", tid)
                    for tid in (acc.get("tag_ids") or [])
                ]
                tag_ids = [t for t in tag_ids if t]

                vals: Dict[str, Any] = {
                    "code": code or False,
                    "name": acc["name"],
                    "account_type": acc["account_type"],
                    "reconcile": acc.get("reconcile", False),
                    "currency_id": self.state.get_dst("res.currency", m2o_id(acc.get("currency_id"))) or False,
                    "note": acc.get("note") or False,
                    "non_trade": acc.get("non_trade", False),
                }
                if tag_ids:
                    vals["tag_ids"] = [[6, 0, tag_ids]]
                if dst_uses_company_ids:
                    vals["company_ids"] = [[6, 0, [dst_cid]]]
                else:
                    vals["company_id"] = dst_cid

                if dst_has_active and "active" in acc:
                    vals["active"] = acc["active"]
                if dst_has_deprecated and "deprecated" in acc:
                    vals["deprecated"] = acc["deprecated"]

                try:
                    create_ctx = (
                        {**dst_ctx, "chart_template_load": True}
                        if acc.get("account_type") in ("asset_cash", "liability_credit_card")
                        else dst_ctx
                    )
                    dst_id = self.dst.create("account.account", vals, context=create_ctx)
                    self.state.set_map("account.account", acc["id"], dst_id)
                    if code:
                        dst_by_code[code] = dst_id
                    created_cnt += 1
                    log.info("Created missing account: [%s] %s (%d -> %d)", code, acc["name"], acc["id"], dst_id)
                except Exception as exc:
                    self.state.log_error("accounts", "account.account", acc["id"], str(exc))
                    log.error("Failed creating account [%s] %s: %s", code, acc["name"], exc)

            log.info(
                "Company %d accounts: %d mapped, %d created", src_cid, mapped_cnt, created_cnt
            )

    # -------------------------------------------------------------------------
    # STEP 4: TAXES, TAX GROUPS, FISCAL POSITIONS, PAYMENT TERMS
    # -------------------------------------------------------------------------
    def migrate_taxes_and_terms(self):
        log.info("=== Phase 4: Taxes, Fiscal Positions & Payment Terms ===")
        for src_cid in self.get_source_company_ids():
            dst_cid = self.state.get_dst("res.company", src_cid)
            if not dst_cid:
                continue
            src_ctx = self.src_company_ctx(src_cid)
            dst_ctx = self.dst_company_ctx(src_cid)

            # 4.1 Tax Groups
            src_tgs = self.src.search_read(
                "account.tax.group",
                [["company_id", "=", src_cid]],
                [
                    "id", "name", "sequence", "country_id",
                    "tax_payable_account_id", "tax_receivable_account_id",
                    "advance_tax_payment_account_id",
                ],
                context=src_ctx,
            )
            for tg in src_tgs:
                if self.state.get_dst("account.tax.group", tg["id"]):
                    continue
                existing = self.dst.search(
                    "account.tax.group",
                    [["name", "=", tg["name"]], ["company_id", "=", dst_cid]],
                    limit=1,
                    context=dst_ctx,
                )
                if existing:
                    self.state.set_map("account.tax.group", tg["id"], existing[0])
                elif not self.dry_run:
                    vals = {
                        "name": tg["name"],
                        "sequence": tg.get("sequence", 10),
                        "company_id": dst_cid,
                        "country_id": self.state.get_dst("res.country", m2o_id(tg.get("country_id"))) or False,
                        "tax_payable_account_id": self.state.get_dst("account.account", m2o_id(tg.get("tax_payable_account_id"))) or False,
                        "tax_receivable_account_id": self.state.get_dst("account.account", m2o_id(tg.get("tax_receivable_account_id"))) or False,
                        "advance_tax_payment_account_id": self.state.get_dst("account.account", m2o_id(tg.get("advance_tax_payment_account_id"))) or False,
                    }
                    dst_id = self.dst.create("account.tax.group", vals, context=dst_ctx)
                    self.state.set_map("account.tax.group", tg["id"], dst_id)

            # 4.2 Taxes (non-group taxes first, then group taxes)
            src_taxes = self.src.search_read(
                "account.tax",
                [["company_id", "=", src_cid]],
                [
                    "id", "name", "type_tax_use", "tax_scope", "amount_type", "amount",
                    "active", "price_include", "price_include_override", "include_base_amount",
                    "is_base_affected", "sequence", "description", "invoice_label",
                    "tax_group_id", "country_id", "tax_exigibility",
                    "cash_basis_transition_account_id", "children_tax_ids",
                    "invoice_repartition_line_ids", "refund_repartition_line_ids",
                ],
                order="amount_type desc, sequence asc, id asc",
                context=src_ctx,
            )
            # Ensure amount_type == 'group' goes last
            src_taxes.sort(key=lambda x: 1 if x.get("amount_type") == "group" else 0)

            for tx in src_taxes:
                dst_tx_id = self.state.get_dst("account.tax", tx["id"])
                if not dst_tx_id:
                    existing = self.dst.search(
                        "account.tax",
                        [
                            ["name", "=", tx["name"]],
                            ["type_tax_use", "=", tx["type_tax_use"]],
                            ["amount", "=", tx["amount"]],
                            ["company_id", "=", dst_cid],
                        ],
                        limit=1,
                        context=dst_ctx,
                    )
                    if existing:
                        dst_tx_id = existing[0]
                        self.state.set_map("account.tax", tx["id"], dst_tx_id)
                    elif not self.dry_run:
                        dst_country_id = (
                            self.state.get_dst("res.country", m2o_id(tx.get("country_id")))
                            or self._get_dst_company_country_id(dst_cid, dst_ctx)
                            or False
                        )
                        dst_tax_group_id = self.state.get_dst("account.tax.group", m2o_id(tx.get("tax_group_id")))
                        if not dst_tax_group_id:
                            fallback_tg = self.dst.search(
                                "account.tax.group",
                                [["company_id", "=", dst_cid]],
                                limit=1,
                                context=dst_ctx,
                            )
                            if fallback_tg:
                                dst_tax_group_id = fallback_tg[0]
                            else:
                                dst_tax_group_id = self.dst.create(
                                    "account.tax.group",
                                    {
                                        "name": "Taxes",
                                        "sequence": 10,
                                        "company_id": dst_cid,
                                        "country_id": dst_country_id,
                                    },
                                    context=dst_ctx,
                                )
                        vals = {
                            "name": tx["name"],
                            "type_tax_use": tx["type_tax_use"],
                            "tax_scope": tx.get("tax_scope") or False,
                            "amount_type": tx["amount_type"],
                            "amount": tx["amount"],
                            "active": tx.get("active", True),
                            "price_include": tx.get("price_include", False),
                            "price_include_override": tx.get("price_include_override") or False,
                            "include_base_amount": tx.get("include_base_amount", False),
                            "is_base_affected": tx.get("is_base_affected", True),
                            "sequence": tx.get("sequence", 1),
                            "description": tx.get("description") or False,
                            "invoice_label": tx.get("invoice_label") or False,
                            "company_id": dst_cid,
                            "tax_group_id": dst_tax_group_id,
                            "country_id": dst_country_id,
                            "tax_exigibility": tx.get("tax_exigibility") or "on_invoice",
                            "cash_basis_transition_account_id": self.state.get_dst(
                                "account.account", m2o_id(tx.get("cash_basis_transition_account_id"))
                            ) or False,
                        }
                        if tx.get("amount_type") == "group":
                            children = [
                                self.state.get_dst("account.tax", cid)
                                for cid in (tx.get("children_tax_ids") or [])
                            ]
                            vals["children_tax_ids"] = [[6, 0, [c for c in children if c]]]
                        else:
                            for rep_field in ("invoice_repartition_line_ids", "refund_repartition_line_ids"):
                                rep_ids = tx.get(rep_field) or []
                                if rep_ids:
                                    rep_lines = self.src.read(
                                        "account.tax.repartition.line",
                                        rep_ids,
                                        ["factor_percent", "repartition_type", "account_id", "tag_ids", "use_in_tax_closing"],
                                        context=src_ctx,
                                    )
                                    cmds = [[5, 0, 0]]
                                    for rl in rep_lines:
                                        rtags = [
                                            self.state.get_dst("account.account.tag", tid)
                                            for tid in (rl.get("tag_ids") or [])
                                        ]
                                        cmds.append(
                                            [
                                                0,
                                                0,
                                                {
                                                    "factor_percent": rl.get("factor_percent", 100.0),
                                                    "repartition_type": rl["repartition_type"],
                                                    "account_id": self.state.get_dst(
                                                        "account.account", m2o_id(rl.get("account_id"))
                                                    ) or False,
                                                    "tag_ids": [[6, 0, [t for t in rtags if t]]],
                                                    "use_in_tax_closing": rl.get("use_in_tax_closing", True),
                                                },
                                            ]
                                        )
                                    vals[rep_field] = cmds
                        try:
                            dst_tx_id = self.dst.create("account.tax", vals, context=dst_ctx)
                            self.state.set_map("account.tax", tx["id"], dst_tx_id)
                            log.info("Created missing tax: %s (%d -> %d)", tx["name"], tx["id"], dst_tx_id)
                        except Exception as exc:
                            self.state.log_error("taxes", "account.tax", tx["id"], str(exc))
                            log.error("Failed creating tax %s: %s", tx["name"], exc)
                            continue

                # Map repartition line IDs for accurate tax line references on account.move.line
                if dst_tx_id and not self.dry_run:
                    self._map_tax_repartition_lines(tx, dst_tx_id, src_ctx, dst_ctx)

            # 4.3 Fiscal Positions
            src_fps = self.src.search_read(
                "account.fiscal.position",
                [["company_id", "=", src_cid]],
                ["id", "name", "active", "auto_apply", "vat_required", "country_id", "note"],
                context=src_ctx,
            )
            for fp in src_fps:
                if self.state.get_dst("account.fiscal.position", fp["id"]):
                    continue
                existing = self.dst.search(
                    "account.fiscal.position",
                    [["name", "=", fp["name"]], ["company_id", "=", dst_cid]],
                    limit=1,
                    context=dst_ctx,
                )
                if existing:
                    self.state.set_map("account.fiscal.position", fp["id"], existing[0])
                elif not self.dry_run:
                    vals = {
                        "name": fp["name"],
                        "active": fp.get("active", True),
                        "auto_apply": fp.get("auto_apply", False),
                        "vat_required": fp.get("vat_required", False),
                        "country_id": self.state.get_dst("res.country", m2o_id(fp.get("country_id"))) or False,
                        "note": fp.get("note") or False,
                        "company_id": dst_cid,
                    }
                    dst_id = self.dst.create("account.fiscal.position", vals, context=dst_ctx)
                    self.state.set_map("account.fiscal.position", fp["id"], dst_id)

            # 4.4 Payment Terms
            src_pts = self.src.search_read(
                "account.payment.term",
                ["|", ["company_id", "=", src_cid], ["company_id", "=", False]],
                ["id", "name", "note", "active", "company_id", "line_ids"],
                context=src_ctx,
            )
            for pt in src_pts:
                if self.state.get_dst("account.payment.term", pt["id"]):
                    continue
                pt_dst_cid = self.state.get_dst("res.company", m2o_id(pt.get("company_id")))
                domain = [["name", "=", pt["name"]]]
                if pt_dst_cid:
                    domain.append(["company_id", "=", pt_dst_cid])
                existing = self.dst.search(
                    "account.payment.term", domain, limit=1, context=dst_ctx
                )
                if existing:
                    self.state.set_map("account.payment.term", pt["id"], existing[0])
                elif not self.dry_run:
                    line_cmds = [[5, 0, 0]]
                    if pt.get("line_ids"):
                        pt_lines = self.src.read(
                            "account.payment.term.line",
                            pt["line_ids"],
                            ["value", "value_amount", "nb_days", "delay_type", "days_next_month"],
                            context=src_ctx,
                        )
                        for pl in pt_lines:
                            line_cmds.append(
                                [
                                    0,
                                    0,
                                    {
                                        "value": pl.get("value", "percent"),
                                        "value_amount": pl.get("value_amount", 100.0),
                                        "nb_days": pl.get("nb_days", 0),
                                        "delay_type": pl.get("delay_type", "days_after"),
                                        "days_next_month": pl.get("days_next_month", "10"),
                                    },
                                ]
                            )
                    vals = {
                        "name": pt["name"],
                        "note": pt.get("note") or False,
                        "active": pt.get("active", True),
                        "company_id": pt_dst_cid or False,
                        "line_ids": line_cmds,
                    }
                    dst_id = self.dst.create("account.payment.term", vals, context=dst_ctx)
                    self.state.set_map("account.payment.term", pt["id"], dst_id)

    def _map_tax_repartition_lines(self, src_tx: dict, dst_tx_id: int, src_ctx: dict, dst_ctx: dict):
        dst_tx = self.dst.read(
            "account.tax",
            [dst_tx_id],
            ["invoice_repartition_line_ids", "refund_repartition_line_ids"],
            context=dst_ctx,
        )
        if not dst_tx:
            return
        dst_tx = dst_tx[0]
        for field in ("invoice_repartition_line_ids", "refund_repartition_line_ids"):
            src_rids = src_tx.get(field) or []
            dst_rids = dst_tx.get(field) or []
            if not src_rids or not dst_rids:
                continue
            src_lines = self.src.read(
                "account.tax.repartition.line",
                src_rids,
                ["id", "repartition_type", "factor_percent", "account_id"],
                context=src_ctx,
            )
            dst_lines = self.dst.read(
                "account.tax.repartition.line",
                dst_rids,
                ["id", "repartition_type", "factor_percent", "account_id"],
                context=dst_ctx,
            )
            used = set()
            for sl in src_lines:
                if self.state.get_dst("account.tax.repartition.line", sl["id"]):
                    continue
                target_acc = self.state.get_dst("account.account", m2o_id(sl.get("account_id")))
                for dl in dst_lines:
                    if dl["id"] in used:
                        continue
                    if (
                        dl["repartition_type"] == sl["repartition_type"]
                        and abs((dl.get("factor_percent") or 0) - (sl.get("factor_percent") or 0)) < 0.01
                        and (not target_acc or m2o_id(dl.get("account_id")) == target_acc)
                    ):
                        self.state.set_map("account.tax.repartition.line", sl["id"], dl["id"])
                        used.add(dl["id"])
                        break

    # -------------------------------------------------------------------------
    # STEP 5: ANALYTIC PLANS & ACCOUNTS
    # -------------------------------------------------------------------------
    def migrate_analytics(self):
        log.info("=== Phase 5: Analytic Plans & Analytic Accounts ===")
        if not self.src.fields_get("account.analytic.plan"):
            return

        src_plans = self.src.search_read(
            "account.analytic.plan",
            [],
            ["id", "name", "description", "parent_id", "default_applicability"],
            order="parent_id nulls first, id asc",
            context=self.src_ctx_all,
        )
        for p in src_plans:
            if self.state.get_dst("account.analytic.plan", p["id"]):
                continue
            existing = self.dst.search(
                "account.analytic.plan", [["name", "=", p["name"]]], limit=1, context=self.dst_ctx_quiet
            )
            if existing:
                self.state.set_map("account.analytic.plan", p["id"], existing[0])
            elif not self.dry_run:
                vals = {
                    "name": p["name"],
                    "description": p.get("description") or False,
                    "parent_id": self.state.get_dst("account.analytic.plan", m2o_id(p.get("parent_id"))) or False,
                    "default_applicability": p.get("default_applicability") or "optional",
                }
                dst_id = self.dst.create("account.analytic.plan", vals, context=self.dst_ctx_quiet)
                self.state.set_map("account.analytic.plan", p["id"], dst_id)

        src_aas = self.src.search_read(
            "account.analytic.account",
            [],
            ["id", "name", "code", "plan_id", "company_id", "active"],
            context=self.src_ctx_all,
        )
        for aa in src_aas:
            if self.state.get_dst("account.analytic.account", aa["id"]):
                continue
            src_cid = m2o_id(aa.get("company_id"))
            if self.only_company_id and src_cid and src_cid != self.only_company_id:
                continue
            dst_cid = self.state.get_dst("res.company", src_cid) if src_cid else False
            domain = [["name", "=", aa["name"]]]
            if aa.get("code"):
                domain = [["code", "=", aa["code"]]]
            if dst_cid:
                domain.append(["company_id", "=", dst_cid])
            existing = self.dst.search(
                "account.analytic.account", domain, limit=1, context=self.dst_ctx_quiet
            )
            if existing:
                self.state.set_map("account.analytic.account", aa["id"], existing[0])
            elif not self.dry_run:
                vals = {
                    "name": aa["name"],
                    "code": aa.get("code") or False,
                    "plan_id": self.state.get_dst("account.analytic.plan", m2o_id(aa.get("plan_id"))),
                    "company_id": dst_cid or False,
                    "active": aa.get("active", True),
                }
                dst_id = self.dst.create("account.analytic.account", vals, context=self.dst_ctx_quiet)
                self.state.set_map("account.analytic.account", aa["id"], dst_id)

    def remap_analytic_distribution(self, dist: Any) -> Any:
        if not isinstance(dist, dict) or not dist:
            return False
        new_dist = {}
        for key, pct in dist.items():
            mapped_keys = []
            for part in str(key).split(","):
                part = part.strip()
                if part.isdigit():
                    dst_aa = self.state.get_dst("account.analytic.account", int(part))
                    if dst_aa:
                        mapped_keys.append(str(dst_aa))
            if mapped_keys:
                new_dist[",".join(mapped_keys)] = pct
        return new_dist or False

    # -------------------------------------------------------------------------
    # STEP 5B: USERS (PASSWORDS & PERMISSIONS), HR MASTER DATA, EMPLOYEES & CONTRACTS
    # -------------------------------------------------------------------------
    @staticmethod
    def _query_pg_user_passwords(pg_dsn: str, db_name: str) -> List[Tuple[int, str, str]]:
        sql = (
            "SELECT id, login, password FROM res_users "
            "WHERE id NOT IN (1, 2) "
            "AND login NOT IN ('__system__', 'public', 'portaltemplate', 'admin') "
            "AND password IS NOT NULL AND password != '' "
            "ORDER BY id ASC;"
        )
        rows: List[Tuple[int, str, str]] = []
        try:
            import psycopg2  # type: ignore
            conn = psycopg2.connect(pg_dsn) if pg_dsn else psycopg2.connect(dbname=db_name)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    for uid, login, pw in cur.fetchall():
                        if uid and login and pw and str(pw).startswith("$"):
                            rows.append((int(uid), str(login), str(pw)))
                return rows
            finally:
                conn.close()
        except ImportError:
            pass
        except Exception as exc:
            log.debug("psycopg2 source password query skipped (%s): %s", db_name or pg_dsn, exc)
            return []

        cmd = ["psql"]
        if pg_dsn:
            cmd.append(pg_dsn)
        else:
            cmd.extend(["-d", db_name])
        cmd.extend(["-t", "-A", "-F", "\t", "-c", sql])
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
            if proc.returncode != 0:
                log.debug("psql source password query failed (%s): %s", db_name or pg_dsn, proc.stderr.strip())
                return []
            for line in proc.stdout.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t", 2)
                if len(parts) == 3 and parts[0].isdigit() and parts[2].startswith("$"):
                    rows.append((int(parts[0]), parts[1], parts[2]))
        except Exception as exc:
            log.debug("psql subprocess skipped (%s): %s", db_name or pg_dsn, exc)
        return rows

    @staticmethod
    def _write_pg_user_password(pg_dsn: str, db_name: str, dst_uid: int, login: str, pw_hash: str) -> bool:
        if dst_uid in (1, 2) or login in ("__system__", "public", "portaltemplate", "admin"):
            return False
        if not pw_hash.startswith("$"):
            return False
        sql = (
            "UPDATE res_users SET password = %s "
            "WHERE id = %s AND login = %s AND id NOT IN (1, 2) RETURNING id;"
        )
        try:
            import psycopg2  # type: ignore
            conn = psycopg2.connect(pg_dsn) if pg_dsn else psycopg2.connect(dbname=db_name)
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(sql, (pw_hash, int(dst_uid), str(login)))
                        updated = cur.fetchone()
                        return bool(updated and int(updated[0]) == int(dst_uid))
            finally:
                conn.close()
        except ImportError:
            pass
        except Exception as exc:
            log.debug("psycopg2 dst password write skipped (%s): %s", db_name or pg_dsn, exc)
            return False

        cmd = ["psql"]
        if pg_dsn:
            cmd.append(pg_dsn)
        else:
            cmd.extend(["-d", db_name])
        cmd.extend([
            "-t", "-A",
            "-v", "ON_ERROR_STOP=1",
            "-v", f"pw={pw_hash}",
            "-v", f"uid={int(dst_uid)}",
            "-v", f"ulogin={login}",
        ])
        sql_stdin = (
            "UPDATE res_users SET password = :'pw' "
            "WHERE id = :uid AND login = :'ulogin' AND id NOT IN (1, 2) "
            "RETURNING id;\n"
        )
        try:
            proc = subprocess.run(
                cmd, input=sql_stdin, capture_output=True, text=True, timeout=10, check=False
            )
            if proc.returncode != 0:
                return False
            return proc.stdout.strip() == str(dst_uid)
        except Exception:
            return False

    @staticmethod
    def save_passwords_json_file(
        passwords_path: Path,
        rows: List[Tuple[int, str, str]],
        source_db: str = "",
    ):
        by_id: Dict[str, Dict[str, str]] = {}
        by_login: Dict[str, str] = {}
        for uid, login, pw_hash in rows:
            if uid in (1, 2) or login in ("__system__", "public", "portaltemplate", "admin"):
                continue
            if not pw_hash.startswith("$"):
                continue
            by_id[str(uid)] = {"login": login, "password_hash": pw_hash}
            by_login[login] = pw_hash
        payload = {
            "source_db": source_db,
            "by_id": by_id,
            "by_login": by_login,
        }
        passwords_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            os.chmod(passwords_path, 0o600)
        except OSError:
            pass

    def _load_source_password_hashes(self, expected_logins: Dict[int, str]) -> Dict[int, str]:
        """Load non-admin password hashes from .user_passwords.json, StateStore, and source PG."""
        result: Dict[int, str] = {}
        login_to_src_id = {login: uid for uid, login in expected_logins.items()}

        # 1. File cache (.user_passwords.json)
        if self.passwords_file and self.passwords_file.exists():
            try:
                data = json.loads(self.passwords_file.read_text(encoding="utf-8"))
                for sid_str, entry in (data.get("by_id") or {}).items():
                    if not sid_str.isdigit():
                        continue
                    sid = int(sid_str)
                    login = entry.get("login") or expected_logins.get(sid) or ""
                    pw_hash = entry.get("password_hash") or ""
                    if sid in self.PROTECTED_USER_IDS or login in self.IGNORED_TEMPLATE_LOGINS or login == "admin":
                        continue
                    if pw_hash.startswith("$") and (sid not in expected_logins or expected_logins[sid] == login):
                        self.state.save_user_password(sid, login, pw_hash)
                        result[sid] = pw_hash
                for login, pw_hash in (data.get("by_login") or {}).items():
                    if login in self.IGNORED_TEMPLATE_LOGINS or login == "admin":
                        continue
                    sid = login_to_src_id.get(login)
                    if sid and sid not in result and isinstance(pw_hash, str) and pw_hash.startswith("$"):
                        self.state.save_user_password(sid, login, pw_hash)
                        result[sid] = pw_hash
            except Exception as exc:
                log.warning("Could not parse passwords file %s: %s", self.passwords_file, exc)

        # 2. Source PostgreSQL (ODOO19_PG_DSN or local psql matching src.db)
        pg_rows = self._query_pg_user_passwords(self.src_pg_dsn, self.src.db)
        verified_pg_rows: List[Tuple[int, str, str]] = []
        for sid, login, pw_hash in pg_rows:
            if sid in expected_logins and expected_logins[sid] != login:
                log.warning(
                    "Skipping local PG hash for user id=%d (%s) because remote source login is '%s'.",
                    sid, login, expected_logins[sid],
                )
                continue
            self.state.save_user_password(sid, login, pw_hash)
            result[sid] = pw_hash
            verified_pg_rows.append((sid, login, pw_hash))

        if verified_pg_rows and self.passwords_file and not self.passwords_file.exists():
            try:
                self.save_passwords_json_file(self.passwords_file, verified_pg_rows, source_db=self.src.db)
                log.info("Cached %d source password hash(es) into %s", len(verified_pg_rows), self.passwords_file)
            except Exception as exc:
                log.warning("Could not write passwords cache %s: %s", self.passwords_file, exc)

        # 3. Any previously cached rows in db.sqlite3
        for sid, pw_hash in self.state.get_all_user_passwords().items():
            if sid not in self.PROTECTED_USER_IDS and sid not in result and pw_hash.startswith("$"):
                result[sid] = pw_hash

        return result

    def _apply_user_password_hash(
        self,
        src_uid: int,
        dst_uid: int,
        login: str,
        pwd_hash: Optional[str],
        is_active: bool,
    ) -> str:
        if (
            src_uid in self.PROTECTED_USER_IDS
            or dst_uid in self.PROTECTED_USER_IDS
            or login in self.IGNORED_TEMPLATE_LOGINS
            or login == "admin"
        ):
            return "protected"

        cached_hash, applied_dst_id = self.state.get_user_password(src_uid, login)
        target_hash = pwd_hash or cached_hash
        if target_hash and applied_dst_id == dst_uid:
            return "already_applied"

        if target_hash and target_hash.startswith("$"):
            # Try contractor_core RPC endpoint first
            try:
                self.dst.call(
                    "res.users",
                    "action_apply_migrated_password_hash",
                    {"ids": [dst_uid], "password_hash": target_hash},
                    context=self.dst_ctx_quiet,
                )
                self.state.mark_password_applied(src_uid, dst_uid)
                return "applied_rpc"
            except Exception as rpc_exc:
                log.debug("action_apply_migrated_password_hash not used for uid=%d: %s", dst_uid, rpc_exc)

            # Fallback to destination PostgreSQL (ODOO20_PG_DSN or local psql)
            if self._write_pg_user_password(self.dst_pg_dsn, self.dst.db, dst_uid, login, target_hash):
                self.state.mark_password_applied(src_uid, dst_uid)
                return "applied_pg"

        if self.default_user_password and is_active:
            try:
                self.dst.write(
                    "res.users",
                    [dst_uid],
                    {"password": self.default_user_password},
                    context=self.dst_ctx_quiet,
                )
                return "applied_default"
            except Exception as exc:
                log.warning("Failed setting default password on user %s (dst id=%d): %s", login, dst_uid, exc)

        return "pending_hash" if target_hash else "no_source_password"

    def _build_group_map(self, src_gids: Set[int]) -> Tuple[Dict[int, int], List[str]]:
        if not src_gids:
            return {}, []
        imd_src = self.src.search_read(
            "ir.model.data",
            [["model", "=", "res.groups"], ["res_id", "in", sorted(src_gids)]],
            ["module", "name", "res_id"],
            context=self.src_ctx_all,
        )
        src_by_gid = {r["res_id"]: f"{r['module']}.{r['name']}" for r in imd_src if r.get("res_id")}
        modules = sorted({r["module"] for r in imd_src if r.get("module")})

        dst_by_xmlid: Dict[str, int] = {}
        if modules:
            imd_dst = self.dst.search_read(
                "ir.model.data",
                [["model", "=", "res.groups"], ["module", "in", modules]],
                ["module", "name", "res_id"],
                context=self.dst_ctx_quiet,
            )
            dst_by_xmlid = {f"{r['module']}.{r['name']}": r["res_id"] for r in imd_dst if r.get("res_id")}

        group_map: Dict[int, int] = {}
        missing_xmlids: List[str] = []
        unmapped_gids: List[int] = []
        for sgid in sorted(src_gids):
            xmlid = src_by_gid.get(sgid)
            if xmlid and xmlid in dst_by_xmlid:
                group_map[sgid] = dst_by_xmlid[xmlid]
            else:
                if xmlid:
                    missing_xmlids.append(xmlid)
                unmapped_gids.append(sgid)

        # Fallback match by full_name for groups without matching XMLIDs
        if unmapped_gids and self.dst.has_field("res.groups", "full_name"):
            dst_by_full_name = {
                (g.get("full_name") or "").strip().lower(): g["id"]
                for g in self.dst.search_read("res.groups", [], ["id", "full_name"], context=self.dst_ctx_quiet)
                if g.get("full_name")
            }
            for sg in self.src.read("res.groups", unmapped_gids, ["id", "full_name", "name"], context=self.src_ctx_all):
                fn = (sg.get("full_name") or sg.get("name") or "").strip().lower()
                if fn and fn in dst_by_full_name:
                    group_map[sg["id"]] = dst_by_full_name[fn]
                    xmlid = src_by_gid.get(sg["id"])
                    if xmlid in missing_xmlids:
                        missing_xmlids.remove(xmlid)

        return group_map, missing_xmlids

    def _build_menu_xmlid_map(self, src_menu_ids: Set[int]) -> Dict[int, int]:
        if not src_menu_ids:
            return {}
        imd_src = self.src.search_read(
            "ir.model.data",
            [["model", "=", "ir.ui.menu"], ["res_id", "in", sorted(src_menu_ids)]],
            ["module", "name", "res_id"],
            context=self.src_ctx_all,
        )
        modules = sorted({r["module"] for r in imd_src if r.get("module")})
        if not modules:
            return {}
        imd_dst = self.dst.search_read(
            "ir.model.data",
            [["model", "=", "ir.ui.menu"], ["module", "in", modules]],
            ["module", "name", "res_id"],
            context=self.dst_ctx_quiet,
        )
        dst_by_xmlid = {f"{r['module']}.{r['name']}": r["res_id"] for r in imd_dst if r.get("res_id")}
        menu_map: Dict[int, int] = {}
        for r in imd_src:
            xmlid = f"{r['module']}.{r['name']}"
            if xmlid in dst_by_xmlid:
                menu_map[r["res_id"]] = dst_by_xmlid[xmlid]
        return menu_map

    def migrate_users(self):
        log.info("=== Phase 5B: Users (res.users — Permissions & Passwords, Ignoring Admin id=2) ===")
        group_field = "group_ids" if self.src.has_field("res.users", "group_ids") else "groups_id"
        dst_group_field = "group_ids" if self.dst.has_field("res.users", "group_ids") else "groups_id"

        user_fields = [
            "id", "login", "name", "active", "share", "partner_id",
            "company_id", "company_ids", group_field,
            "lang", "tz", "signature", "notification_type", "odoobot_state",
            "contractor_menu_mode", "contractor_hidden_menu_ids",
        ]
        src_users = self.src.search_read(
            "res.users",
            [],
            user_fields,
            order="id asc",
            context=self.src_ctx_all,
        )
        dst_users = self.dst.search_read(
            "res.users",
            [],
            ["id", "login", "partner_id", "active"],
            context=self.dst_ctx_quiet,
        )
        dst_by_login = {str(u.get("login") or "").strip().lower(): u for u in dst_users if u.get("login")}
        used_partner_to_uid: Dict[int, int] = {
            m2o_id(u.get("partner_id")): u["id"]
            for u in dst_users
            if m2o_id(u.get("partner_id"))
        }

        # Map system/admin IDs strictly for read-only FK resolution ONLY (never modified)
        if not self.dry_run:
            if any(du["id"] == 1 for du in dst_users) and not self.state.get_dst("res.users", 1):
                self.state.set_map("res.users", 1, 1)
            if any(du["id"] == 2 for du in dst_users) and not self.state.get_dst("res.users", 2):
                self.state.set_map("res.users", 2, 2)

        migratable_users: List[Dict[str, Any]] = []
        for su in src_users:
            suid = su["id"]
            login = str(su.get("login") or "").strip()
            login_lc = login.lower()

            if suid in self.PROTECTED_USER_IDS:
                log.info("Ignoring protected source user id=%d (%s) to preserve API key/admin user.", suid, login)
                continue
            if login_lc in self.IGNORED_TEMPLATE_LOGINS or (su.get("share") and suid in (4, 5)):
                continue

            existing_dst = dst_by_login.get(login_lc)
            if existing_dst and existing_dst["id"] in self.PROTECTED_USER_IDS:
                if not self.dry_run and not self.state.get_dst("res.users", suid):
                    self.state.set_map("res.users", suid, existing_dst["id"])
                log.info(
                    "Skipping user '%s' (src id=%d) modification because it maps to protected destination user id=%d.",
                    login, suid, existing_dst["id"],
                )
                continue

            migratable_users.append(su)

        if not migratable_users:
            log.info("No non-admin migratable users found.")
            return

        # Pre-migrate partners belonging to migratable users (excluding system partners 1..5)
        user_partner_ids: Set[int] = {
            m2o_id(u.get("partner_id"))
            for u in migratable_users
            if m2o_id(u.get("partner_id"))
        }
        if user_partner_ids:
            self.migrate_partners(user_partner_ids)

        # Fetch user avatars directly with bin_size=False
        user_avatars: Dict[int, str] = {}
        if self.src.has_field("res.users", "image_1920"):
            for batch in chunked([u["id"] for u in migratable_users], ATTACHMENT_BATCH_SIZE):
                rows = self.src.read(
                    "res.users",
                    batch,
                    ["id", "image_1920"],
                    context={**self.src_ctx_all, "bin_size": False},
                )
                for row in rows:
                    img = self._extract_b64(row.get("image_1920"))
                    if img:
                        user_avatars[row["id"]] = img

        # Load passwords & resolve groups + hidden menus
        expected_logins = {u["id"]: str(u.get("login") or "").strip() for u in migratable_users}
        src_hashes = self._load_source_password_hashes(expected_logins)

        all_src_gids = {gid for u in migratable_users for gid in (u.get(group_field) or [])}
        group_map, missing_xmlids = self._build_group_map(all_src_gids)
        if missing_xmlids:
            log.warning(
                "Skipped %d res.groups XMLIDs not installed on Destination Odoo 20: %s",
                len(missing_xmlids), ", ".join(missing_xmlids),
            )

        hidden_menu_ids = {
            mid for u in migratable_users for mid in (u.get("contractor_hidden_menu_ids") or [])
        }
        menu_map = self._build_menu_xmlid_map(hidden_menu_ids)

        # Resolve disjoint user-type groups on Destination (Odoo 20 _check_one_user_type)
        base_user_imd = self.dst.search_read(
            "ir.model.data",
            [
                ["model", "=", "res.groups"],
                ["module", "=", "base"],
                ["name", "in", ["group_user", "group_portal", "group_public"]],
            ],
            ["name", "res_id"],
            context=self.dst_ctx_quiet,
        )
        utype_gids = {r["name"]: r["res_id"] for r in base_user_imd}
        dst_group_user = utype_gids.get("group_user")
        dst_group_portal = utype_gids.get("group_portal")
        dst_group_public = utype_gids.get("group_public")

        disjoint_by_gid: Dict[int, Set[int]] = {}
        if group_map and self.dst.has_field("res.groups", "disjoint_ids"):
            for dg in self.dst.read(
                "res.groups",
                sorted(set(group_map.values())),
                ["id", "disjoint_ids"],
                context=self.dst_ctx_quiet,
            ):
                if dg.get("disjoint_ids"):
                    disjoint_by_gid[dg["id"]] = set(dg["disjoint_ids"])

        installed_langs = {
            l["code"]
            for l in self.dst.search_read("res.lang", [["active", "=", True]], ["code"], context=self.dst_ctx_quiet)
        }
        dst_has_menu_mode = self.dst.has_field("res.users", "contractor_menu_mode")
        dst_has_hidden_menus = self.dst.has_field("res.users", "contractor_hidden_menu_ids")
        dst_has_notif = self.dst.has_field("res.users", "notification_type")

        pw_stats: Dict[str, int] = {}
        for su in migratable_users:
            suid = su["id"]
            login = str(su.get("login") or "").strip()
            login_lc = login.lower()

            mapped_gids: List[int] = []
            seen_gids: Set[int] = set()
            for sgid in (su.get(group_field) or []):
                dgid = group_map.get(sgid)
                if dgid and dgid not in seen_gids:
                    seen_gids.add(dgid)
                    mapped_gids.append(dgid)

            if su.get("share"):
                if dst_group_user:
                    mapped_gids = [g for g in mapped_gids if g != dst_group_user]
                if dst_group_portal and dst_group_portal not in mapped_gids and dst_group_public not in mapped_gids:
                    mapped_gids.append(dst_group_portal)
            else:
                mapped_gids = [g for g in mapped_gids if g not in (dst_group_portal, dst_group_public)]
                if dst_group_user and dst_group_user not in mapped_gids:
                    mapped_gids.insert(0, dst_group_user)

            # Enforce disjoint groups cleanly (keep first/highest-priority group)
            cleaned_gids: List[int] = []
            blocked_gids: Set[int] = set()
            for dgid in mapped_gids:
                if dgid in blocked_gids:
                    continue
                cleaned_gids.append(dgid)
                blocked_gids |= disjoint_by_gid.get(dgid, set())

            src_cid = m2o_id(su.get("company_id"))
            dst_cid = self.state.get_dst("res.company", src_cid) or 1
            dst_cids = [
                self.state.get_dst("res.company", cid)
                for cid in (su.get("company_ids") or [])
            ]
            dst_cids = [cid for cid in dst_cids if cid]
            if dst_cid not in dst_cids:
                dst_cids.insert(0, dst_cid)

            vals: Dict[str, Any] = {
                "name": su.get("name") or login,
                "login": login,
                "active": bool(su.get("active", True)),
                "company_id": dst_cid,
                "company_ids": [[6, 0, dst_cids]],
                dst_group_field: [[6, 0, cleaned_gids]],
                "tz": su.get("tz") or "Asia/Qatar",
                "signature": su.get("signature") or False,
            }
            if su.get("lang") and su["lang"] in installed_langs:
                vals["lang"] = su["lang"]
            if dst_has_notif and su.get("notification_type"):
                vals["notification_type"] = su["notification_type"]
            if suid in user_avatars:
                vals["image_1920"] = user_avatars[suid]
            if dst_has_menu_mode and su.get("contractor_menu_mode"):
                vals["contractor_menu_mode"] = su["contractor_menu_mode"]
            if dst_has_hidden_menus and su.get("contractor_hidden_menu_ids") is not None:
                mapped_menus = [menu_map[mid] for mid in su.get("contractor_hidden_menu_ids", []) if mid in menu_map]
                vals["contractor_hidden_menu_ids"] = [[6, 0, mapped_menus]]

            dst_uid = self.state.get_dst("res.users", suid)
            if not dst_uid and login_lc in dst_by_login:
                dst_uid = dst_by_login[login_lc]["id"]

            if dst_uid in self.PROTECTED_USER_IDS:
                log.warning("Guard prevented modification on protected destination user id=%d (%s)", dst_uid, login)
                continue

            if self.dry_run:
                has_hash = bool(src_hashes.get(suid) or self.state.get_user_password(suid, login)[0])
                log.info(
                    "[DRY-RUN] User '%s' (src=%d -> dst=%s): active=%s, groups=%d, has_password_hash=%s, avatar=%s",
                    login, suid, dst_uid or "NEW", su.get("active", True), len(cleaned_gids), has_hash, suid in user_avatars,
                )
                continue

            try:
                if dst_uid:
                    write_vals = dict(vals)
                    # Do not replace login/partner needlessly if already set
                    write_vals.pop("login", None)
                    try:
                        self.dst.write("res.users", [dst_uid], write_vals, context=self.dst_ctx_quiet)
                    except Exception:
                        base_vals = dict(write_vals)
                        base_vals.pop(dst_group_field, None)
                        self.dst.write("res.users", [dst_uid], base_vals, context=self.dst_ctx_quiet)
                        self._apply_user_groups_incrementally(dst_uid, dst_group_field, cleaned_gids, dst_group_user)
                else:
                    src_pid = m2o_id(su.get("partner_id"))
                    dst_pid = self.state.get_dst("res.partner", src_pid) if src_pid else None
                    if (
                        dst_pid
                        and dst_pid not in (1, 2, 3, 4, 5)
                        and used_partner_to_uid.get(dst_pid) in (None, dst_uid)
                    ):
                        vals["partner_id"] = dst_pid
                    try:
                        dst_uid = self.dst.create("res.users", vals, context=self.dst_ctx_quiet)
                    except Exception:
                        init_vals = dict(vals)
                        init_vals[dst_group_field] = [[6, 0, [dst_group_user]]] if (dst_group_user and not su.get("share")) else [[6, 0, []]]
                        dst_uid = self.dst.create("res.users", init_vals, context=self.dst_ctx_quiet)
                        self._apply_user_groups_incrementally(dst_uid, dst_group_field, cleaned_gids, dst_group_user)

                if dst_uid in self.PROTECTED_USER_IDS:
                    continue

                self.state.set_map("res.users", suid, dst_uid, meta={"login": login})
                self.state.resolve_error("users", "res.users", suid)
                used_partner_to_uid[m2o_id(su.get("partner_id")) or 0] = dst_uid

                # Link resulting user partner into state map if created automatically
                dst_user_row = self.dst.read("res.users", [dst_uid], ["partner_id"], context=self.dst_ctx_quiet)
                if dst_user_row:
                    dpid = m2o_id(dst_user_row[0].get("partner_id"))
                    spid = m2o_id(su.get("partner_id"))
                    if spid and dpid and not self.state.get_dst("res.partner", spid):
                        self.state.set_map("res.partner", spid, dpid)
                    if dpid:
                        used_partner_to_uid[dpid] = dst_uid

                pw_res = self._apply_user_password_hash(
                    suid, dst_uid, login, src_hashes.get(suid), bool(su.get("active", True))
                )
                pw_stats[pw_res] = pw_stats.get(pw_res, 0) + 1
                log.info(
                    "Migrated user '%s' (%d -> %d) [active=%s, groups=%d, pw=%s]",
                    login, suid, dst_uid, su.get("active", True), len(cleaned_gids), pw_res,
                )
            except Exception as exc:
                self.state.log_error("users", "res.users", suid, str(exc))
                log.error("Failed migrating user '%s' (%d): %s", login, suid, exc)

        if pw_stats.get("pending_hash"):
            log.warning(
                "%d user password hash(es) cached in SQLite/passwords file could not be written via RPC or local PG yet. "
                "Upgrade 'contractor_core' on Odoo 20 and re-run --only-users-hr (or run with ODOO20_PG_DSN set) to apply them.",
                pw_stats["pending_hash"],
            )

    def _apply_user_groups_incrementally(
        self,
        dst_uid: int,
        group_field: str,
        cleaned_gids: List[int],
        dst_group_user: Optional[int],
    ):
        if dst_uid in self.PROTECTED_USER_IDS:
            return
        if dst_group_user and dst_group_user in cleaned_gids:
            try:
                self.dst.write("res.users", [dst_uid], {group_field: [[6, 0, [dst_group_user]]]}, context=self.dst_ctx_quiet)
            except Exception:
                pass
        for gid in cleaned_gids:
            if gid == dst_group_user:
                continue
            try:
                self.dst.write("res.users", [dst_uid], {group_field: [[4, gid]]}, context=self.dst_ctx_quiet)
            except Exception as exc:
                log.warning("Could not add group %d to user dst id=%d: %s", gid, dst_uid, exc)

    def _resolve_dst_employee_type(self, src_ver: dict) -> Tuple[Optional[int], bool]:
        """Map Odoo 19 contract_type_id / employee_type to Odoo 20 (hr.employee.type, fixed_term)."""
        if not self._hr_emp_type_cache and self.dst.fields_get("hr.employee.type"):
            for et in self.dst.search_read("hr.employee.type", [], ["id", "name"], context=self.dst_ctx_quiet):
                self._hr_emp_type_cache[et["name"].strip().lower()] = et["id"]
        if not self._src_contract_types_cache and self.src.fields_get("hr.contract.type"):
            for ct in self.src.search_read("hr.contract.type", [], ["id", "name", "code"], context=self.src_ctx_all):
                self._src_contract_types_cache[ct["id"]] = (ct.get("name") or ct.get("code") or "").strip()

        ct_id = m2o_id(src_ver.get("contract_type_id"))
        ct_name = self._src_contract_types_cache.get(ct_id, "") if ct_id else (m2o_name(src_ver.get("contract_type_id")) or "")
        ct_lc = ct_name.strip().lower()
        emp_type_sel = (src_ver.get("employee_type") or "employee").strip().lower()
        has_end = bool(src_ver.get("contract_date_end"))

        fixed_term = has_end
        target_name = "Employee"
        if ct_lc in ("permanent", "full-time", "full time", "part-time", "part time", "employee", "statutory", ""):
            target_name = "Employee"
        elif ct_lc == "temporary":
            target_name = "Employee"
            fixed_term = True
        elif ct_lc in ("seasonal", "occasional", "occasional / seasonal"):
            target_name = "Occasional / Seasonal"
        elif ct_lc in ("intern", "trainee"):
            target_name = "Intern"
        elif ct_lc == "interim":
            target_name = "Interim"
        elif ct_lc == "student":
            target_name = "Student"
        elif ct_lc == "apprenticeship":
            target_name = "Apprenticeship"
        elif ct_lc == "thesis":
            target_name = "Thesis"
        else:
            target_name = ct_name

        if not ct_lc and emp_type_sel != "employee":
            fallback_map = {
                "student": "Student",
                "trainee": "Intern",
                "freelance": "Interim",
                "contractor": "Interim",
            }
            target_name = fallback_map.get(emp_type_sel, "Employee")

        dst_et_id = self._hr_emp_type_cache.get(target_name.strip().lower())
        if not dst_et_id and not self.dry_run and self.dst.fields_get("hr.employee.type"):
            try:
                dst_et_id = self.dst.create("hr.employee.type", {"name": target_name}, context=self.dst_ctx_quiet)
                self._hr_emp_type_cache[target_name.strip().lower()] = dst_et_id
            except Exception as exc:
                log.warning("Could not create hr.employee.type '%s': %s", target_name, exc)
                dst_et_id = self._hr_emp_type_cache.get("employee")
        if not dst_et_id:
            dst_et_id = self._hr_emp_type_cache.get("employee")
        return dst_et_id, fixed_term

    def _migrate_work_entry_types(self) -> Tuple[Optional[int], Set[int]]:
        """Map source hr.work.entry.type records to Odoo 20 equivalents without country conflicts."""
        if not self.src.fields_get("hr.work.entry.type") or not self.dst.fields_get("hr.work.entry.type"):
            return None, set()

        src_wets = self.src.search_read(
            "hr.work.entry.type",
            [],
            ["id", "name", "code", "is_leave", "country_id"],
            context=self.src_ctx_all,
        )
        dst_wets = self.dst.search_read(
            "hr.work.entry.type",
            [],
            ["id", "name", "code", "count_as", "country_id"],
            context=self.dst_ctx_quiet,
        )
        dst_generic = [w for w in dst_wets if not m2o_id(w.get("country_id"))]
        dst_pool = dst_generic or dst_wets

        dst_by_code = {str(w["code"]).strip().upper(): w["id"] for w in dst_pool if w.get("code")}
        dst_by_name = {str(w["name"]).strip().lower(): w["id"] for w in dst_pool if w.get("name")}
        default_work = (
            dst_by_code.get("002.00")
            or next((w["id"] for w in dst_pool if w.get("count_as") == "working_time"), None)
            or (dst_wets[0]["id"] if dst_wets else None)
        )
        default_absence = (
            dst_by_code.get("LEAVE100")
            or next((w["id"] for w in dst_pool if w.get("count_as") == "absence"), None)
        )

        # Match XMLIDs first
        imd_src = self.src.search_read(
            "ir.model.data",
            [["model", "=", "hr.work.entry.type"]],
            ["module", "name", "res_id"],
            context=self.src_ctx_all,
        )
        src_xmlid_by_id = {r["res_id"]: f"{r['module']}.{r['name']}" for r in imd_src if r.get("res_id")}
        imd_dst = self.dst.search_read(
            "ir.model.data",
            [["model", "=", "hr.work.entry.type"]],
            ["module", "name", "res_id"],
            context=self.dst_ctx_quiet,
        )
        dst_id_by_xmlid = {f"{r['module']}.{r['name']}": r["res_id"] for r in imd_dst if r.get("res_id")}
        dst_country_by_id = {w["id"]: m2o_id(w.get("country_id")) for w in dst_wets}

        src_break_ids: Set[int] = set()
        for sw in src_wets:
            sid = sw["id"]
            scode = str(sw.get("code") or "").strip().upper()
            sname = str(sw.get("name") or "").strip().lower()
            if scode.startswith("BRK") or sname == "break":
                src_break_ids.add(sid)
                continue

            existing_dst = self.state.get_dst("hr.work.entry.type", sid)
            if existing_dst:
                self._wet_map[sid] = existing_dst
                continue

            xmlid = src_xmlid_by_id.get(sid)
            cand: Optional[int] = None
            if xmlid and xmlid in dst_id_by_xmlid and not dst_country_by_id.get(dst_id_by_xmlid[xmlid]):
                cand = dst_id_by_xmlid[xmlid]
            if not cand and scode in dst_by_code:
                cand = dst_by_code[scode]
            if not cand and sname in dst_by_name:
                cand = dst_by_name[sname]
            if not cand:
                if scode == "WORK100" or sname == "attendance":
                    cand = default_work
                elif scode == "WORK110" or "home" in sname:
                    cand = dst_by_code.get("002.08") or default_work
                elif scode == "OVERTIME" or "overtime" in sname:
                    cand = dst_by_code.get("040.00") or default_work
                elif scode == "OUT" or "out of contract" in sname:
                    cand = dst_by_code.get("000.00") or default_work
                elif scode == "LEAVE90" or "unpaid" in sname:
                    cand = dst_by_code.get("158.00") or default_absence
                elif scode == "LEAVE110" or "sick" in sname:
                    cand = dst_by_code.get("013.00") or default_absence
                elif sw.get("is_leave") or "leave" in scode or "time off" in sname or "annual" in sname:
                    cand = default_absence
                else:
                    cand = default_work

            if cand:
                self._wet_map[sid] = cand
                if not self.dry_run:
                    self.state.set_map("hr.work.entry.type", sid, cand)

        return default_work, src_break_ids

    def migrate_hr_master_data(self):
        log.info("=== Phase 5C: HR Master Data (Calendars, Locations, Departments, Jobs, Structures) ===")
        first_src_cid = (self.get_source_company_ids() or [1])[0]
        default_dst_cid = self.state.get_dst("res.company", first_src_cid) or 1

        # 0. Work Entry Types (required by structure types & attendance rest periods)
        default_work_wet_id, src_break_wet_ids = self._migrate_work_entry_types()

        # 1. Working Hours (resource.calendar + resource.calendar.attendance)
        if self.src.fields_get("resource.calendar") and self.dst.fields_get("resource.calendar"):
            src_cals = self.src.search_read(
                "resource.calendar",
                [],
                [
                    "id", "name", "hours_per_day", "full_time_required_hours",
                    "company_id", "active", "flexible_hours", "two_weeks_calendar", "attendance_ids",
                ],
                context=self.src_ctx_all,
            )
            dst_cals = self.dst.search_read(
                "resource.calendar", [], ["id", "name", "company_id"], context=self.dst_ctx_quiet
            )
            dst_cal_lookup: Dict[Tuple[str, Optional[int]], int] = {}
            for dc in dst_cals:
                key = ((dc.get("name") or "").strip().lower(), m2o_id(dc.get("company_id")))
                dst_cal_lookup.setdefault(key, dc["id"])

            dst_wet_countries: Dict[int, Optional[int]] = {}
            if self.dst.fields_get("hr.work.entry.type"):
                for w in self.dst.search_read("hr.work.entry.type", [], ["id", "country_id"], context=self.dst_ctx_quiet):
                    dst_wet_countries[w["id"]] = m2o_id(w.get("country_id"))
            dst_comp_countries: Dict[int, Optional[int]] = {
                dc["id"]: m2o_id(dc.get("country_id"))
                for dc in self.dst.search_read("res.company", [], ["id", "country_id"], context=self.dst_ctx_quiet)
            }

            for sc in src_cals:
                if self.state.get_dst("resource.calendar", sc["id"]):
                    continue
                src_cid = m2o_id(sc.get("company_id"))
                dst_cid = self.state.get_dst("res.company", src_cid) if src_cid else None
                cname = (sc.get("name") or "").strip()
                match = dst_cal_lookup.get((cname.lower(), dst_cid)) or dst_cal_lookup.get((cname.lower(), None))
                if match:
                    self.state.set_map("resource.calendar", sc["id"], match)
                    continue
                if self.dry_run:
                    log.info("[DRY-RUN] Would create resource.calendar: %s", cname)
                    continue

                try:
                    cal_country_id = dst_comp_countries.get(dst_cid) if dst_cid else None
                    cal_type = "undefined" if sc.get("flexible_hours") else "fixed"
                    att_cmds: List[Any] = [[5, 0, 0]]
                    if cal_type == "fixed" and sc.get("attendance_ids"):
                        att_rows = self.src.read(
                            "resource.calendar.attendance",
                            sc["attendance_ids"],
                            ["dayofweek", "day_period", "hour_from", "hour_to", "week_type", "display_type", "work_entry_type_id", "sequence"],
                            context=self.src_ctx_all,
                        )
                        intervals_by_day: Dict[str, List[Tuple[float, float]]] = {}
                        for ar in sorted(att_rows, key=lambda r: (str(r.get("dayofweek") or "0"), float(r.get("hour_from") or 0.0), r["id"])):
                            if ar.get("day_period") == "lunch" or ar.get("display_type"):
                                continue
                            src_wet_id = m2o_id(ar.get("work_entry_type_id"))
                            if src_wet_id in src_break_wet_ids:
                                continue
                            if sc.get("two_weeks_calendar") and ar.get("week_type") not in (False, None, "0"):
                                continue
                            h_from = float(ar.get("hour_from") or 0.0)
                            h_to = float(ar.get("hour_to") or 0.0)
                            if h_to <= h_from or (h_to - h_from) > 24.0:
                                continue
                            dow = str(ar.get("dayofweek") if ar.get("dayofweek") is not None else "0")
                            existing_ivs = intervals_by_day.get(dow, [])
                            if any(h_from < et and ef < h_to for ef, et in existing_ivs):
                                continue
                            intervals_by_day.setdefault(dow, []).append((h_from, h_to))

                            avals: Dict[str, Any] = {
                                "dayofweek": dow,
                                "hour_from": h_from,
                                "hour_to": h_to,
                                "sequence": int(ar.get("sequence") or 10),
                            }
                            if src_wet_id:
                                dst_wet_id = self._wet_map.get(src_wet_id) or self.state.get_dst("hr.work.entry.type", src_wet_id)
                                if dst_wet_id:
                                    wet_c = dst_wet_countries.get(dst_wet_id)
                                    if wet_c is None or (dst_cid and wet_c == cal_country_id):
                                        avals["work_entry_type_id"] = dst_wet_id
                            att_cmds.append([0, 0, avals])

                    cvals: Dict[str, Any] = {
                        "name": cname,
                        "calendar_type": cal_type,
                        "hours_per_day": float(sc.get("hours_per_day") or 8.0),
                        "full_time_required_hours": float(sc.get("full_time_required_hours") or 40.0),
                        "company_id": dst_cid or False,
                        "active": bool(sc.get("active", True)),
                    }
                    if cal_type == "fixed":
                        cvals["attendance_ids"] = att_cmds
                    dst_id = self.dst.create("resource.calendar", cvals, context=self.dst_ctx_quiet)
                    self.state.set_map("resource.calendar", sc["id"], dst_id)
                    self.state.resolve_error("hr_master", "resource.calendar", sc["id"])
                    dst_cal_lookup[(cname.lower(), dst_cid)] = dst_id
                except Exception as exc:
                    self.state.log_error("hr_master", "resource.calendar", sc["id"], str(exc))
                    log.error("Failed migrating resource.calendar '%s' (%d): %s", cname, sc["id"], exc)

        # 2. Work Locations
        if self.src.fields_get("hr.work.location") and self.dst.fields_get("hr.work.location"):
            src_locs = self.src.search_read(
                "hr.work.location",
                [],
                ["id", "name", "address_id", "location_type", "location_number", "company_id", "active"],
                context=self.src_ctx_all,
            )
            loc_partners = {m2o_id(l.get("address_id")) for l in src_locs if m2o_id(l.get("address_id"))}
            if loc_partners:
                self.migrate_partners(loc_partners)
            for wl in src_locs:
                if self.state.get_dst("hr.work.location", wl["id"]):
                    continue
                src_cid = m2o_id(wl.get("company_id"))
                dst_cid = self.state.get_dst("res.company", src_cid) or default_dst_cid
                dom = [["name", "=", wl["name"]], ["company_id", "=", dst_cid]]
                existing = self.dst.search("hr.work.location", dom, limit=1, context=self.dst_ctx_quiet)
                if existing:
                    self.state.set_map("hr.work.location", wl["id"], existing[0])
                elif not self.dry_run:
                    try:
                        vals = {
                            "name": wl["name"],
                            "address_id": self.state.get_dst("res.partner", m2o_id(wl.get("address_id"))) or False,
                            "location_type": wl.get("location_type") or "office",
                            "location_number": wl.get("location_number") or False,
                            "company_id": dst_cid,
                            "active": wl.get("active", True),
                        }
                        dst_id = self.dst.create("hr.work.location", vals, context=self.dst_ctx_quiet)
                        self.state.set_map("hr.work.location", wl["id"], dst_id)
                        self.state.resolve_error("hr_master", "hr.work.location", wl["id"])
                    except Exception as exc:
                        self.state.log_error("hr_master", "hr.work.location", wl["id"], str(exc))
                        log.error("Failed migrating hr.work.location '%s' (%d): %s", wl.get("name"), wl["id"], exc)

        # 3. Departments (Pass 1: parent hierarchy; Pass 2 after employees sets manager_id)
        if self.src.fields_get("hr.department") and self.dst.fields_get("hr.department"):
            src_depts = self.src.search_read(
                "hr.department",
                [],
                ["id", "name", "parent_id", "manager_id", "company_id", "color", "note", "active"],
                order="parent_id nulls first, id asc",
                context=self.src_ctx_all,
            )
            for d in src_depts:
                if self.state.get_dst("hr.department", d["id"]):
                    continue
                src_cid = m2o_id(d.get("company_id"))
                dst_cid = self.state.get_dst("res.company", src_cid) if src_cid else False
                dom = [["name", "=", d["name"]]]
                if dst_cid:
                    dom.append(["company_id", "=", dst_cid])
                existing = self.dst.search("hr.department", dom, limit=1, context=self.dst_ctx_quiet)
                if existing:
                    self.state.set_map("hr.department", d["id"], existing[0])
                elif not self.dry_run:
                    try:
                        vals = {
                            "name": d["name"],
                            "parent_id": self.state.get_dst("hr.department", m2o_id(d.get("parent_id"))) or False,
                            "company_id": dst_cid or False,
                            "color": d.get("color", 0),
                            "note": d.get("note") or False,
                            "active": d.get("active", True),
                        }
                        dst_id = self.dst.create("hr.department", vals, context=self.dst_ctx_quiet)
                        self.state.set_map("hr.department", d["id"], dst_id)
                        self.state.resolve_error("hr_master", "hr.department", d["id"])
                    except Exception as exc:
                        self.state.log_error("hr_master", "hr.department", d["id"], str(exc))
                        log.error("Failed migrating hr.department '%s' (%d): %s", d.get("name"), d["id"], exc)

        # 4. Job Positions
        if self.src.fields_get("hr.job") and self.dst.fields_get("hr.job"):
            src_jobs = self.src.search_read(
                "hr.job",
                [],
                ["id", "name", "department_id", "company_id", "no_of_recruitment", "description", "requirements", "sequence", "active"],
                order="id asc",
                context=self.src_ctx_all,
            )
            for j in src_jobs:
                if self.state.get_dst("hr.job", j["id"]):
                    continue
                src_cid = m2o_id(j.get("company_id"))
                dst_cid = (self.state.get_dst("res.company", src_cid) if src_cid else None) or default_dst_cid
                dst_dept = self.state.get_dst("hr.department", m2o_id(j.get("department_id"))) or False
                dom: List[Any] = [["name", "=", j["name"]], ["company_id", "=", dst_cid]]
                if dst_dept:
                    dom.append(["department_id", "=", dst_dept])
                existing_list = self.dst.search("hr.job", dom, context=self.dst_ctx_quiet)
                unmapped = [cid for cid in existing_list if not self.state.get_src("hr.job", cid)]
                if unmapped:
                    self.state.set_map("hr.job", j["id"], unmapped[0])
                elif not self.dry_run:
                    try:
                        vals = {
                            "name": j["name"],
                            "department_id": dst_dept,
                            "company_id": dst_cid,
                            "no_of_recruitment": j.get("no_of_recruitment") or 1,
                            "description": j.get("description") or False,
                            "requirements": j.get("requirements") or False,
                            "sequence": j.get("sequence", 10),
                            "active": j.get("active", True),
                        }
                        dst_id = self.dst.create("hr.job", vals, context=self.dst_ctx_quiet)
                        self.state.set_map("hr.job", j["id"], dst_id)
                        self.state.resolve_error("hr_master", "hr.job", j["id"])
                    except Exception as exc:
                        self.state.log_error("hr_master", "hr.job", j["id"], str(exc))
                        log.error("Failed migrating hr.job '%s' (%d): %s", j.get("name"), j["id"], exc)

        # 5. Employee Tags (hr.employee.category)
        if self.src.fields_get("hr.employee.category") and self.dst.fields_get("hr.employee.category"):
            for cat in self.src.search_read("hr.employee.category", [], ["id", "name", "color"], context=self.src_ctx_all):
                if self.state.get_dst("hr.employee.category", cat["id"]):
                    continue
                existing = self.dst.search("hr.employee.category", [["name", "=", cat["name"]]], limit=1, context=self.dst_ctx_quiet)
                if existing:
                    self.state.set_map("hr.employee.category", cat["id"], existing[0])
                elif not self.dry_run:
                    try:
                        dst_id = self.dst.create(
                            "hr.employee.category",
                            {"name": cat["name"], "color": cat.get("color", 0)},
                            context=self.dst_ctx_quiet,
                        )
                        self.state.set_map("hr.employee.category", cat["id"], dst_id)
                        self.state.resolve_error("hr_master", "hr.employee.category", cat["id"])
                    except Exception as exc:
                        self.state.log_error("hr_master", "hr.employee.category", cat["id"], str(exc))
                        log.error("Failed migrating hr.employee.category '%s' (%d): %s", cat.get("name"), cat["id"], exc)

        # 6. Departure Reasons
        if self.src.fields_get("hr.departure.reason") and self.dst.fields_get("hr.departure.reason"):
            dst_reasons = {
                r["name"].strip().lower(): r["id"]
                for r in self.dst.search_read("hr.departure.reason", [], ["id", "name"], context=self.dst_ctx_quiet)
                if r.get("name")
            }
            dst_has_reason_code = self.dst.has_field("hr.departure.reason", "reason_code")
            for dr in self.src.search_read("hr.departure.reason", [], ["id", "name", "sequence", "reason_code"], context=self.src_ctx_all):
                if self.state.get_dst("hr.departure.reason", dr["id"]):
                    continue
                rname = (dr.get("name") or "").strip()
                match = dst_reasons.get(rname.lower())
                if match:
                    self.state.set_map("hr.departure.reason", dr["id"], match)
                elif not self.dry_run and rname:
                    try:
                        rvals: Dict[str, Any] = {"name": rname, "sequence": dr.get("sequence", 10)}
                        if dst_has_reason_code and dr.get("reason_code"):
                            rvals["reason_code"] = dr["reason_code"]
                        dst_id = self.dst.create("hr.departure.reason", rvals, context=self.dst_ctx_quiet)
                        self.state.set_map("hr.departure.reason", dr["id"], dst_id)
                        self.state.resolve_error("hr_master", "hr.departure.reason", dr["id"])
                        dst_reasons[rname.lower()] = dst_id
                    except Exception as exc:
                        self.state.log_error("hr_master", "hr.departure.reason", dr["id"], str(exc))
                        log.error("Failed migrating hr.departure.reason '%s' (%d): %s", rname, dr["id"], exc)

        # 7. WPS Sponsors (partner_id, bank_account_id, employer_eid, payer_eid, payer_qid, additional_header)
        if self.src.fields_get("hr.wps.sponsor") and self.dst.fields_get("hr.wps.sponsor"):
            src_sponsors = self.src.search_read(
                "hr.wps.sponsor",
                [],
                ["id", "partner_id", "bank_account_id", "employer_eid", "payer_eid", "payer_qid", "additional_header"],
                context=self.src_ctx_all,
            )
            sp_partners = {m2o_id(sp.get("partner_id")) for sp in src_sponsors if m2o_id(sp.get("partner_id"))}
            if sp_partners:
                self.migrate_partners(sp_partners)
            for sp in src_sponsors:
                if self.state.get_dst("hr.wps.sponsor", sp["id"]):
                    continue
                dst_pid = self.state.get_dst("res.partner", m2o_id(sp.get("partner_id")))
                if not dst_pid:
                    continue
                dom = [["employer_eid", "=", sp["employer_eid"]]] if sp.get("employer_eid") else [["partner_id", "=", dst_pid]]
                existing = self.dst.search("hr.wps.sponsor", dom, limit=1, context=self.dst_ctx_quiet)
                if existing:
                    self.state.set_map("hr.wps.sponsor", sp["id"], existing[0])
                elif not self.dry_run:
                    try:
                        vals = {
                            "partner_id": dst_pid,
                            "bank_account_id": self.state.get_dst("res.partner.bank", m2o_id(sp.get("bank_account_id"))) or False,
                            "employer_eid": sp.get("employer_eid") or False,
                            "payer_eid": sp.get("payer_eid") or False,
                            "payer_qid": sp.get("payer_qid") or False,
                            "additional_header": sp.get("additional_header") or False,
                        }
                        dst_id = self.dst.create("hr.wps.sponsor", vals, context=self.dst_ctx_quiet)
                        self.state.set_map("hr.wps.sponsor", sp["id"], dst_id)
                        self.state.resolve_error("hr_master", "hr.wps.sponsor", sp["id"])
                    except Exception as exc:
                        self.state.log_error("hr_master", "hr.wps.sponsor", sp["id"], str(exc))
                        log.error("Failed migrating hr.wps.sponsor (%d): %s", sp["id"], exc)

        # 8. Payroll Structure Types & Structures
        if self.src.fields_get("hr.payroll.structure.type") and self.dst.fields_get("hr.payroll.structure.type"):
            src_stypes = self.src.search_read(
                "hr.payroll.structure.type",
                [],
                [
                    "id", "name", "default_schedule_pay", "wage_type", "country_id",
                    "default_resource_calendar_id", "default_work_entry_type_id", "default_struct_id", "active",
                ],
                order="id asc",
                context=self.src_ctx_all,
            )
            for st in src_stypes:
                if self.state.get_dst("hr.payroll.structure.type", st["id"]):
                    continue
                existing = self.dst.search(
                    "hr.payroll.structure.type",
                    [["name", "=", st["name"]]],
                    limit=1,
                    context=self.dst_ctx_quiet,
                )
                if existing:
                    self.state.set_map("hr.payroll.structure.type", st["id"], existing[0])
                elif not self.dry_run:
                    try:
                        src_wet = m2o_id(st.get("default_work_entry_type_id"))
                        dst_wet = (self._wet_map.get(src_wet) or self.state.get_dst("hr.work.entry.type", src_wet)) if src_wet else None
                        vals: Dict[str, Any] = {
                            "name": st["name"],
                            "default_schedule_pay": st.get("default_schedule_pay") or "monthly",
                            "wage_type": st.get("wage_type") or "monthly",
                            "country_id": self.state.get_dst("res.country", m2o_id(st.get("country_id"))) or False,
                            "default_resource_calendar_id": self.state.get_dst("resource.calendar", m2o_id(st.get("default_resource_calendar_id"))) or False,
                            "active": st.get("active", True),
                        }
                        if dst_wet or default_work_wet_id:
                            vals["default_work_entry_type_id"] = dst_wet or default_work_wet_id
                        dst_id = self.dst.create("hr.payroll.structure.type", vals, context=self.dst_ctx_quiet)
                        self.state.set_map("hr.payroll.structure.type", st["id"], dst_id)
                        self.state.resolve_error("hr_master", "hr.payroll.structure.type", st["id"])
                    except Exception as exc:
                        self.state.log_error("hr_master", "hr.payroll.structure.type", st["id"], str(exc))
                        log.error("Failed migrating hr.payroll.structure.type '%s' (%d): %s", st.get("name"), st["id"], exc)

            if self.src.fields_get("hr.payroll.structure") and self.dst.fields_get("hr.payroll.structure"):
                dst_has_odoo20_flag = self.dst.has_field("hr.payroll.structure", "use_odoo20_salary_rules")
                src_structs = self.src.search_read(
                    "hr.payroll.structure",
                    [],
                    ["id", "name", "code", "type_id", "country_id", "active"],
                    order="id asc",
                    context=self.src_ctx_all,
                )
                for ps in src_structs:
                    dst_type_id = self.state.get_dst("hr.payroll.structure.type", m2o_id(ps.get("type_id")))
                    if self.state.get_dst("hr.payroll.structure", ps["id"]):
                        continue
                    dom: List[Any] = [["name", "=", ps["name"]]]
                    if dst_type_id:
                        dom.append(["type_id", "=", dst_type_id])
                    existing = self.dst.search("hr.payroll.structure", dom, limit=1, context=self.dst_ctx_quiet)
                    if existing:
                        self.state.set_map("hr.payroll.structure", ps["id"], existing[0])
                    elif not self.dry_run and dst_type_id:
                        try:
                            vals = {
                                "name": ps["name"],
                                "code": ps.get("code") or False,
                                "type_id": dst_type_id,
                                "country_id": self.state.get_dst("res.country", m2o_id(ps.get("country_id"))) or False,
                                "active": ps.get("active", True),
                            }
                            if dst_has_odoo20_flag:
                                vals["use_odoo20_salary_rules"] = bool(ps.get("active", True))
                            dst_id = self.dst.create("hr.payroll.structure", vals, context=self.dst_ctx_quiet)
                            self.state.set_map("hr.payroll.structure", ps["id"], dst_id)
                            self.state.resolve_error("hr_master", "hr.payroll.structure", ps["id"])
                        except Exception as exc:
                            self.state.log_error("hr_master", "hr.payroll.structure", ps["id"], str(exc))
                            log.error("Failed migrating hr.payroll.structure '%s' (%d): %s", ps.get("name"), ps["id"], exc)

            # Link default_struct_id on hr.payroll.structure.type
            if not self.dry_run:
                for st in src_stypes:
                    src_ds = m2o_id(st.get("default_struct_id"))
                    dst_stype_id = self.state.get_dst("hr.payroll.structure.type", st["id"])
                    dst_ds = self.state.get_dst("hr.payroll.structure", src_ds) if src_ds else None
                    if dst_stype_id and dst_ds:
                        self._hr_struct_default_cache[dst_stype_id] = dst_ds
                        try:
                            self.dst.write(
                                "hr.payroll.structure.type",
                                [dst_stype_id],
                                {"default_struct_id": dst_ds},
                                context=self.dst_ctx_quiet,
                            )
                        except Exception as exc:
                            log.warning("Could not set default_struct_id on structure type %d: %s", dst_stype_id, exc)

    def _build_version_vals(
        self,
        v: Dict[str, Any],
        dst_emp_id: Optional[int],
        dst_cid: int,
        dst_default_cal: Optional[int],
        dst_default_addr: Optional[int],
        emp_fallback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ef = emp_fallback or {}
        date_ver = v.get("date_version") or v.get("contract_date_start") or "2020-01-01"
        c_start = v.get("contract_date_start") or False
        c_end = v.get("contract_date_end") or False
        if c_end and not c_start:
            c_start = date_ver
        if c_start and c_end and str(c_end) < str(c_start):
            c_end = c_start

        dst_et_id, fixed_term = self._resolve_dst_employee_type(v)
        src_stype = m2o_id(v.get("structure_type_id")) or m2o_id(ef.get("structure_type_id"))
        dst_stype = self.state.get_dst("hr.payroll.structure.type", src_stype) if src_stype else False

        src_dept = m2o_id(v.get("department_id")) or m2o_id(ef.get("department_id"))
        dst_dept = self.state.get_dst("hr.department", src_dept) if src_dept else False

        src_job = m2o_id(v.get("job_id")) or m2o_id(ef.get("job_id"))
        dst_job = self.state.get_dst("hr.job", src_job) if src_job else False

        src_cal = m2o_id(v.get("resource_calendar_id")) or m2o_id(ef.get("resource_calendar_id"))
        dst_cal = (self.state.get_dst("resource.calendar", src_cal) if src_cal else None) or dst_default_cal

        src_addr = m2o_id(v.get("address_id")) or m2o_id(ef.get("address_id"))
        dst_addr = (self.state.get_dst("res.partner", src_addr) if src_addr else None) or dst_default_addr

        src_wl = m2o_id(v.get("work_location_id")) or m2o_id(ef.get("work_location_id"))
        dst_wl = self.state.get_dst("hr.work.location", src_wl) if src_wl else False

        src_resp = m2o_id(v.get("hr_responsible_id")) or m2o_id(ef.get("hr_responsible_id"))
        dst_resp = (self.state.get_dst("res.users", src_resp) if src_resp else None) or 2

        raw_sex = v.get("sex") or ef.get("sex")
        sex_val = raw_sex if raw_sex in ("male", "female") else False

        priv_country = self.state.get_dst(
            "res.country", m2o_id(v.get("private_country_id")) or m2o_id(ef.get("private_country_id"))
        ) or False
        priv_state = self.state.get_dst(
            "res.country.state", m2o_id(v.get("private_state_id")) or m2o_id(ef.get("private_state_id"))
        ) or False
        if priv_state:
            if self._dst_state_country_cache is None:
                self._dst_state_country_cache = {
                    s["id"]: m2o_id(s.get("country_id"))
                    for s in self.dst.search_read("res.country.state", [], ["id", "country_id"], context=self.dst_ctx_quiet)
                    if m2o_id(s.get("country_id"))
                }
            state_cid = self._dst_state_country_cache.get(priv_state)
            if priv_country and state_cid and state_cid != priv_country:
                priv_state = False

        vals: Dict[str, Any] = {
            "name": v.get("name") or False,
            "date_version": date_ver,
            "contract_date_start": c_start,
            "contract_date_end": c_end,
            "trial_date_end": v.get("trial_date_end") or False,
            "fixed_term": fixed_term,
            "employee_type_id": dst_et_id or False,
            "structure_type_id": dst_stype or False,
            "wage": max(0.0, float(v.get("wage") or 0.0)),
            "wage_type": v.get("wage_type") or "monthly",
            "hourly_wage": max(0.0, float(v.get("hourly_wage") or 0.0)),
            "schedule_pay": v.get("schedule_pay") or "monthly",
            "accommodation_allowance": float(v.get("accommodation_allowance") or 0.0),
            "food_allowance": float(v.get("food_allowance") or 0.0),
            "transportation_allowance": float(v.get("transportation_allowance") or 0.0),
            "other_allowance": float(v.get("other_allowance") or 0.0),
            "rp_charge": float(v.get("rp_charge") or 0.0),
            "air_ticket": float(v.get("air_ticket") or 0.0),
            "leave_pay_days": int(v.get("leave_pay_days") or 0),
            "eos_days": int(v.get("eos_days") or 0),
            "leave_period": int(v.get("leave_period") or 0),
            "analytic_distribution": self.remap_analytic_distribution(v.get("analytic_distribution")),
            "department_id": dst_dept,
            "job_id": dst_job,
            "job_title": v.get("job_title") or ef.get("job_title") or False,
            "tz": v.get("tz") or ef.get("tz") or "Asia/Qatar",
            "address_id": dst_addr or False,
            "work_location_id": dst_wl or False,
            "hr_responsible_id": dst_resp,
            "country_id": self.state.get_dst("res.country", m2o_id(v.get("country_id")) or m2o_id(ef.get("country_id"))) or False,
            "identification_id": v.get("identification_id") or ef.get("identification_id") or False,
            "passport_id": v.get("passport_id") or ef.get("passport_id") or False,
            "passport_expiration_date": (
                v.get("passport_expiration_date")
                or ef.get("passport_expiration_date")
                or ef.get("x_studio_passport_expiry")
                or False
            ),
            "sex": sex_val,
            "marital": v.get("marital") or ef.get("marital") or "single",
            "spouse_complete_name": v.get("spouse_complete_name") or ef.get("spouse_complete_name") or False,
            "spouse_birthdate": v.get("spouse_birthdate") or ef.get("spouse_birthdate") or False,
            "children": int(v.get("children") or ef.get("children") or 0),
            "private_street": v.get("private_street") or ef.get("private_street") or False,
            "private_street2": v.get("private_street2") or ef.get("private_street2") or False,
            "private_city": v.get("private_city") or ef.get("private_city") or False,
            "private_zip": v.get("private_zip") or ef.get("private_zip") or False,
            "private_country_id": priv_country,
            "private_state_id": priv_state,
            "distance_home_work": int(v.get("distance_home_work") or 0),
            "distance_home_work_unit": v.get("distance_home_work_unit") or "kilometers",
            "km_home_work": int(v.get("km_home_work") or 0),
            "is_non_resident": bool(v.get("is_non_resident", False)),
            "disabled": bool(v.get("disabled", False)),
        }
        if dst_cal:
            vals["resource_calendar_id"] = dst_cal
        if dst_emp_id:
            vals["employee_id"] = dst_emp_id
        else:
            vals["company_id"] = dst_cid
        if m2o_id(v.get("contract_template_id")):
            tpl_id = self.state.get_dst("hr.version", m2o_id(v.get("contract_template_id")))
            if tpl_id:
                vals["contract_template_id"] = tpl_id
        return vals

    @staticmethod
    def _intervals_overlap(s1: Optional[str], e1: Optional[str], s2: Optional[str], e2: Optional[str]) -> bool:
        if not s1 or not s2:
            return False
        eff_e1 = e1 or "9999-12-31"
        eff_e2 = e2 or "9999-12-31"
        return str(s1) <= str(eff_e2) and str(s2) <= str(eff_e1)

    def _sync_employee_departure(
        self,
        dst_emp_id: int,
        dst_primary_vid: int,
        emp: Dict[str, Any],
        primary_ver: Dict[str, Any],
        primary_vals: Dict[str, Any],
        has_linked_user: bool,
        dst_ctx: dict,
    ):
        if not self.dst.fields_get("hr.employee.departure"):
            return
        dep_reason_src = m2o_id(emp.get("departure_reason_id")) or m2o_id(primary_ver.get("departure_reason_id"))
        dep_date = emp.get("departure_date") or primary_ver.get("departure_date")
        dep_desc = emp.get("departure_description") or primary_ver.get("departure_description") or False
        if not (dep_reason_src or dep_date):
            return

        dst_ver_row = self.dst.read("hr.version", [dst_primary_vid], ["departure_id"], context=dst_ctx)
        if dst_ver_row and m2o_id(dst_ver_row[0].get("departure_id")):
            return

        dst_reason_id = self.state.get_dst("hr.departure.reason", dep_reason_src) if dep_reason_src else None
        if not dst_reason_id:
            reasons = self.dst.search("hr.departure.reason", [], limit=1, context=dst_ctx)
            dst_reason_id = reasons[0] if reasons else None
        if not dst_reason_id:
            return

        eff_dep_date = str(dep_date or primary_vals.get("contract_date_end") or "2025-01-01")
        orig_date_ver = str(primary_vals.get("date_version") or eff_dep_date)
        orig_c_start = primary_vals.get("contract_date_start")
        orig_c_end = primary_vals.get("contract_date_end")

        # Temporarily satisfy hr.employee.departure._check_departure_validity
        need_temp_dates = (
            orig_date_ver >= eff_dep_date
            or (orig_c_start and str(orig_c_start) > eff_dep_date)
        )
        if need_temp_dates:
            prev_day = (date.fromisoformat(eff_dep_date) - timedelta(days=1)).isoformat()
            self.dst.write(
                "hr.version",
                [dst_primary_vid],
                {"date_version": prev_day, "contract_date_start": False, "contract_date_end": False},
                context={**dst_ctx, "sync_contract_dates": True},
            )

        dep_vals = {
            "employee_id": dst_emp_id,
            "departure_reason_id": dst_reason_id,
            "dismissal_date": eff_dep_date,
            "departure_date": eff_dep_date,
            "departure_description": dep_desc,
        }
        dst_dep_id = self.dst.create("hr.employee.departure", dep_vals, context=dst_ctx)

        if not emp.get("active", True) and eff_dep_date <= date.today().isoformat() and not has_linked_user:
            try:
                self.dst.call(
                    "hr.employee.departure",
                    "action_register",
                    {"ids": [dst_dep_id]},
                    context={**dst_ctx, "skip_tool_clearance_check": True},
                )
            except Exception as exc:
                log.warning("Departure action_register skipped for emp dst=%d: %s", dst_emp_id, exc)

        # Restore exact contract dates & link departure_id cleanly
        self.dst.write(
            "hr.version",
            [dst_primary_vid],
            {
                "date_version": orig_date_ver,
                "contract_date_start": orig_c_start or False,
                "contract_date_end": orig_c_end or False,
                "departure_id": dst_dep_id,
            },
            context={**dst_ctx, "sync_contract_dates": True},
        )

    def migrate_employees_and_contracts(self):
        log.info("=== Phase 5D: Employees (hr.employee + Pictures) & Contracts (hr.version) ===")
        if not self.src.fields_get("hr.employee") or not self.dst.fields_get("hr.employee"):
            log.warning("hr.employee not available on source or destination; skipping.")
            return

        emp_fields = [
            "id", "name", "legal_name", "active", "company_id", "user_id",
            "work_contact_id", "work_email", "work_phone", "mobile_phone",
            "private_email", "private_phone", "lang",
            "birthday", "birthday_public_display", "place_of_birth", "country_of_birth",
            "emergency_contact", "emergency_phone", "certificate", "study_field",
            "permit_no", "visa_no", "visa_expire", "work_permit_expiration_date",
            "barcode", "pin", "color", "additional_note",
            "category_ids", "bank_account_ids", "salary_distribution",
            "wps_sponsor_id", "hourly_cost", "contract_template_id",
            "parent_id", "coach_id", "leave_manager_id", "attendance_manager_id",
            "department_id", "job_id", "job_title", "address_id", "work_location_id",
            "resource_calendar_id", "tz", "hr_responsible_id", "structure_type_id",
            "country_id", "identification_id", "passport_id", "passport_expiration_date",
            "x_studio_passport_expiry", "sex", "marital", "spouse_complete_name",
            "spouse_birthdate", "children", "private_street", "private_street2",
            "private_city", "private_zip", "private_country_id", "private_state_id",
            "departure_reason_id", "departure_date", "departure_description",
            "workforce_category", "monthly_basic_salary", "monthly_total_salary",
            "version_id",
        ]
        src_emps = self.src.search_read(
            "hr.employee", [], emp_fields, order="id asc", context=self.src_ctx_all
        )
        if not src_emps:
            log.info("No source employees found.")
            return

        ver_fields = [
            "id", "name", "active", "employee_id", "company_id", "date_version",
            "contract_date_start", "contract_date_end", "trial_date_end",
            "contract_type_id", "employee_type", "structure_type_id", "contract_template_id",
            "wage", "wage_type", "hourly_wage", "schedule_pay", "total_salary",
            "accommodation_allowance", "food_allowance", "transportation_allowance",
            "other_allowance", "rp_charge", "air_ticket",
            "leave_pay_days", "eos_days", "leave_period", "analytic_distribution",
            "department_id", "job_id", "job_title", "resource_calendar_id", "tz",
            "address_id", "work_location_id", "hr_responsible_id",
            "country_id", "identification_id", "passport_id", "passport_expiration_date",
            "sex", "marital", "spouse_complete_name", "spouse_birthdate", "children",
            "private_street", "private_street2", "private_city", "private_zip",
            "private_country_id", "private_state_id",
            "distance_home_work", "distance_home_work_unit", "km_home_work",
            "is_non_resident", "disabled", "additional_note",
            "departure_reason_id", "departure_date", "departure_description",
        ]
        src_versions = self.src.search_read(
            "hr.version", [], ver_fields, order="employee_id asc, date_version asc, id asc", context=self.src_ctx_all
        )

        # Pre-migrate work contacts & work addresses (+ employee bank accounts)
        hr_partner_ids: Set[int] = set()
        for e in src_emps:
            for pf in ("work_contact_id", "address_id"):
                pid = m2o_id(e.get(pf))
                if pid:
                    hr_partner_ids.add(pid)
        for v in src_versions:
            pid = m2o_id(v.get("address_id"))
            if pid:
                hr_partner_ids.add(pid)
        if hr_partner_ids:
            self.migrate_partners(hr_partner_ids)

        # Separate contract templates (employee_id=False) vs employee versions
        src_templates: List[Dict[str, Any]] = []
        versions_by_emp: Dict[int, List[Dict[str, Any]]] = {}
        for v in src_versions:
            eid = m2o_id(v.get("employee_id"))
            if not eid:
                src_templates.append(v)
            else:
                versions_by_emp.setdefault(eid, []).append(v)

        # Resolve per-company default calendar & partner on destination
        company_defaults: Dict[int, Tuple[Optional[int], Optional[int]]] = {}
        for dc in self.dst.search_read(
            "res.company", [], ["id", "resource_calendar_id", "partner_id"], context=self.dst_ctx_quiet
        ):
            company_defaults[dc["id"]] = (m2o_id(dc.get("resource_calendar_id")), m2o_id(dc.get("partner_id")))
        fallback_cal_ids = self.dst.search("resource.calendar", [["active", "=", True]], limit=1, context=self.dst_ctx_quiet)

        # 1. Migrate Contract Templates first
        for tpl in src_templates:
            if self.state.get_dst("hr.version", tpl["id"]):
                continue
            src_cid = m2o_id(tpl.get("company_id")) or (self.only_company_id or 1)
            dst_cid = self.state.get_dst("res.company", src_cid) or 1
            def_cal, def_addr = company_defaults.get(dst_cid, (fallback_cal_ids[0] if fallback_cal_ids else None, None))
            if self.dry_run:
                log.info("[DRY-RUN] Would create contract template: %s", tpl.get("name") or tpl["id"])
                continue
            try:
                tvals = self._build_version_vals(tpl, None, dst_cid, def_cal, def_addr)
                tvals["active"] = bool(tpl.get("active", True))
                dst_tid = self.dst.create("hr.version", tvals, context={**self.dst_company_ctx(src_cid), "sync_contract_dates": True})
                self.state.set_map("hr.version", tpl["id"], dst_tid)
            except Exception as exc:
                self.state.log_error("contracts", "hr.version", tpl["id"], str(exc))
                log.error("Failed contract template %d: %s", tpl["id"], exc)

        # 2. Fetch employee pictures & binary documents with bin_size=False (bypassing ir.attachment search restrictions)
        bin_fields_to_read = [
            "id", "image_1920", "id_card", "id_card_name",
            "driving_license", "driving_license_name", "has_work_permit",
            "sim_card", "sim_card_name", "internet_invoice", "internet_invoice_name",
        ]
        emp_binaries: Dict[int, Dict[str, Any]] = {}
        if not self.dry_run:
            for batch in chunked([e["id"] for e in src_emps], ATTACHMENT_BATCH_SIZE):
                rows = self.src.read(
                    "hr.employee",
                    batch,
                    bin_fields_to_read,
                    context={**self.src_ctx_all, "bin_size": False},
                )
                for row in rows:
                    emp_binaries[row["id"]] = row

            # Fallback for employees whose photo is stored on work_contact_id instead of hr.employee
            wc_need_photo = sorted({
                m2o_id(e.get("work_contact_id"))
                for e in src_emps
                if m2o_id(e.get("work_contact_id")) and not self._extract_b64((emp_binaries.get(e["id"]) or {}).get("image_1920"))
            })
            wc_photos: Dict[int, str] = {}
            for batch in chunked(wc_need_photo, ATTACHMENT_BATCH_SIZE):
                for prow in self.src.read("res.partner", batch, ["id", "image_1920"], context={**self.src_ctx_all, "bin_size": False}):
                    img = self._extract_b64(prow.get("image_1920"))
                    if img:
                        wc_photos[prow["id"]] = img
            for e in src_emps:
                eid = e["id"]
                wc_id = m2o_id(e.get("work_contact_id"))
                if wc_id and wc_id in wc_photos and not self._extract_b64((emp_binaries.get(eid) or {}).get("image_1920")):
                    emp_binaries.setdefault(eid, {})["image_1920"] = wc_photos[wc_id]

        # Inspect existing destination employees to keep barcode and (user_id, company_id) unique
        dst_existing_emps = self.dst.search_read(
            "hr.employee", [], ["id", "name", "active", "barcode", "user_id", "company_id", "version_id", "work_contact_id"], context=self.dst_ctx_quiet
        )
        used_barcodes: Set[str] = {str(de["barcode"]).strip() for de in dst_existing_emps if de.get("barcode")}
        used_user_company: Set[Tuple[int, int]] = {
            (m2o_id(de.get("user_id")), m2o_id(de.get("company_id")))
            for de in dst_existing_emps
            if m2o_id(de.get("user_id")) and m2o_id(de.get("company_id"))
        }
        installed_langs = {
            l["code"]
            for l in self.dst.search_read("res.lang", [["active", "=", True]], ["code"], context=self.dst_ctx_quiet)
        }
        dst_has_contract_hrms = self.dst.has_field("hr.employee", "monthly_basic_salary")
        dst_has_ticket_amt = self.dst.has_field("hr.employee", "ticket_allowance_amount")
        dst_has_worker_cat = self.dst.has_field("hr.employee", "worker_category")

        # 3. Create/update Employees and their Versions
        for emp in src_emps:
            src_eid = emp["id"]
            src_cid = m2o_id(emp.get("company_id")) or 1
            if self.only_company_id and src_cid != self.only_company_id:
                continue
            dst_cid = self.state.get_dst("res.company", src_cid) or 1
            dst_ctx = self.dst_company_ctx(src_cid)
            def_cal, def_addr = company_defaults.get(dst_cid, (fallback_cal_ids[0] if fallback_cal_ids else None, None))
            if not def_cal:
                def_cal = self.state.get_dst("resource.calendar", m2o_id(emp.get("resource_calendar_id")))

            emp_vers = versions_by_emp.get(src_eid, [])
            active_vers = [v for v in emp_vers if v.get("active", True)]
            cur_vid = m2o_id(emp.get("version_id"))
            primary_ver = (
                next((v for v in active_vers if v["id"] == cur_vid), None)
                or (active_vers[-1] if active_vers else None)
                or (emp_vers[-1] if emp_vers else None)
                or {"id": 0, "date_version": "2020-01-01", "active": True}
            )
            salary_ver = (
                primary_ver
                if (primary_ver.get("wage") or 0.0) > 0
                else next((v for v in reversed(emp_vers) if (v.get("wage") or 0.0) > 0), primary_ver)
            )

            # Resolve linked user (STRICTLY never link protected users id 1 or 2)
            src_uid = m2o_id(emp.get("user_id"))
            dst_uid = self.state.get_dst("res.users", src_uid) if src_uid else None
            if dst_uid in self.PROTECTED_USER_IDS or src_uid in self.PROTECTED_USER_IDS:
                dst_uid = None

            dst_eid = self.state.get_dst("hr.employee", src_eid)
            if dst_uid and (dst_uid, dst_cid) in used_user_company and not dst_eid:
                dst_uid = None

            if not dst_eid and emp.get("barcode"):
                match = next(
                    (de["id"] for de in dst_existing_emps if de.get("barcode") == emp["barcode"] and not self.state.get_src("hr.employee", de["id"])),
                    None,
                )
                if match:
                    dst_eid = match
            if not dst_eid:
                match = next(
                    (
                        de["id"]
                        for de in dst_existing_emps
                        if de.get("name") == emp.get("name")
                        and m2o_id(de.get("company_id")) == dst_cid
                        and bool(de.get("active", True)) == bool(emp.get("active", True))
                        and not self.state.get_src("hr.employee", de["id"])
                    ),
                    None,
                )
                if match:
                    dst_eid = match

            if self.dry_run:
                log.info(
                    "[DRY-RUN] Employee '%s' (src=%d -> dst=%s): active=%s, versions=%d, wage=%.2f",
                    emp.get("name"), src_eid, dst_eid or "NEW", emp.get("active", True),
                    len(emp_vers), float(primary_ver.get("wage") or 0.0),
                )
                continue

            try:
                ebin = emp_binaries.get(src_eid) or {}
                emp_photo = self._extract_b64(ebin.get("image_1920"))

                emp_vals: Dict[str, Any] = {
                    "name": emp.get("name") or f"Employee {src_eid}",
                    "legal_name": emp.get("legal_name") or False,
                    "company_id": dst_cid,
                    "work_email": emp.get("work_email") or False,
                    "work_phone": emp.get("work_phone") or False,
                    "mobile_phone": emp.get("mobile_phone") or False,
                    "private_email": emp.get("private_email") or False,
                    "private_phone": emp.get("private_phone") or False,
                    "birthday": emp.get("birthday") or False,
                    "birthday_public_display": bool(emp.get("birthday_public_display", False)),
                    "place_of_birth": emp.get("place_of_birth") or False,
                    "country_of_birth": self.state.get_dst("res.country", m2o_id(emp.get("country_of_birth"))) or False,
                    "emergency_contact": emp.get("emergency_contact") or False,
                    "emergency_phone": emp.get("emergency_phone") or False,
                    "certificate": emp.get("certificate") or False,
                    "study_field": emp.get("study_field") or False,
                    "permit_no": emp.get("permit_no") or False,
                    "visa_no": emp.get("visa_no") or False,
                    "visa_expire": emp.get("visa_expire") or False,
                    "work_permit_expiration_date": emp.get("work_permit_expiration_date") or False,
                    "pin": emp.get("pin") or False,
                    "color": emp.get("color", 0),
                    "additional_note": emp.get("additional_note") or primary_ver.get("additional_note") or False,
                    "hourly_cost": float(emp.get("hourly_cost") or 0.0),
                    "wps_sponsor_id": self.state.get_dst("hr.wps.sponsor", m2o_id(emp.get("wps_sponsor_id"))) or False,
                    "contract_template_id": self.state.get_dst("hr.version", m2o_id(emp.get("contract_template_id"))) or False,
                }
                if emp.get("lang") and emp["lang"] in installed_langs:
                    emp_vals["lang"] = emp["lang"]
                if emp.get("barcode") and (dst_eid or str(emp["barcode"]).strip() not in used_barcodes):
                    emp_vals["barcode"] = str(emp["barcode"]).strip()
                    used_barcodes.add(emp_vals["barcode"])
                if dst_uid:
                    emp_vals["user_id"] = dst_uid
                    used_user_company.add((dst_uid, dst_cid))
                elif m2o_id(emp.get("work_contact_id")):
                    dst_wc = self.state.get_dst("res.partner", m2o_id(emp.get("work_contact_id")))
                    if dst_wc:
                        emp_vals["work_contact_id"] = dst_wc

                if emp.get("category_ids"):
                    cat_ids = [self.state.get_dst("hr.employee.category", cid) for cid in emp["category_ids"]]
                    cat_ids = [c for c in cat_ids if c]
                    if cat_ids:
                        emp_vals["category_ids"] = [[6, 0, cat_ids]]

                if emp.get("bank_account_ids"):
                    b_ids = [self.state.get_dst("res.partner.bank", bid) for bid in emp["bank_account_ids"]]
                    b_ids = [b for b in b_ids if b]
                    if b_ids:
                        emp_vals["bank_account_ids"] = [[6, 0, b_ids]]
                        if isinstance(emp.get("salary_distribution"), dict):
                            remapped_dist = {}
                            for old_bid, dval in emp["salary_distribution"].items():
                                new_bid = self.state.get_dst("res.partner.bank", int(old_bid)) if str(old_bid).isdigit() else None
                                if new_bid:
                                    remapped_dist[str(new_bid)] = dval
                            if remapped_dist:
                                emp_vals["salary_distribution"] = remapped_dist

                if emp_photo:
                    emp_vals["image_1920"] = emp_photo
                for bf in (
                    "id_card", "id_card_name",
                    "driving_license", "driving_license_name",
                    "has_work_permit",
                    "sim_card", "sim_card_name",
                    "internet_invoice", "internet_invoice_name",
                ):
                    bval = self._extract_b64(ebin.get(bf)) if bf in ("id_card", "driving_license", "has_work_permit", "sim_card", "internet_invoice") else ebin.get(bf)
                    if bval:
                        emp_vals[bf] = bval

                if dst_has_contract_hrms:
                    wage_s = float(salary_ver.get("wage") or 0.0)
                    tot_s = float(
                        salary_ver.get("total_salary")
                        or (
                            wage_s
                            + float(salary_ver.get("accommodation_allowance") or 0.0)
                            + float(salary_ver.get("food_allowance") or 0.0)
                            + float(salary_ver.get("transportation_allowance") or 0.0)
                            + float(salary_ver.get("other_allowance") or 0.0)
                        )
                    )
                    emp_vals["workforce_category"] = emp.get("workforce_category") or "permanent"
                    emp_vals["monthly_basic_salary"] = float(emp.get("monthly_basic_salary") or wage_s)
                    emp_vals["monthly_total_salary"] = float(emp.get("monthly_total_salary") or tot_s)
                if dst_has_worker_cat:
                    emp_vals["worker_category"] = emp.get("workforce_category") or "permanent"
                if dst_has_ticket_amt and float(salary_ver.get("air_ticket") or 0.0) > 0:
                    emp_vals["ticket_allowance_amount"] = float(salary_ver["air_ticket"])

                # Primary contract version values
                primary_vals = self._build_version_vals(
                    primary_ver, None, dst_cid, def_cal, def_addr, emp_fallback=emp
                )
                emp_create_ctx = {
                    **dst_ctx,
                    "sync_contract_dates": True,
                    "allow_reclassification": True,
                    "skip_tool_clearance_check": True,
                }

                if dst_eid:
                    # Update employee base fields + picture (keep active=True during version/departure sync)
                    self.dst.write("hr.employee", [dst_eid], {**emp_vals, "active": True}, context=emp_create_ctx)
                else:
                    # Provide resource_calendar_id/hr_responsible_id from primary_vals at create time so required defaults hold
                    create_vals = dict(emp_vals)
                    create_vals["active"] = True
                    for req_k in ("resource_calendar_id", "hr_responsible_id", "marital", "tz"):
                        if req_k in primary_vals:
                            create_vals[req_k] = primary_vals[req_k]
                    dst_eid = self.dst.create("hr.employee", create_vals, context=emp_create_ctx)

                self.state.set_map("hr.employee", src_eid, dst_eid)

                # Read auto-created primary version_id & work_contact_id
                dst_emp_row = self.dst.read(
                    "hr.employee", [dst_eid], ["version_id", "work_contact_id"], context=dst_ctx
                )[0]
                dst_primary_vid = m2o_id(dst_emp_row.get("version_id"))
                dst_wc_id = m2o_id(dst_emp_row.get("work_contact_id"))

                # Sync employee picture onto linked work_contact_id too
                if emp_photo and dst_wc_id and self.dst.has_field("res.partner", "image_1920"):
                    try:
                        self.dst.write("res.partner", [dst_wc_id], {"image_1920": emp_photo}, context=dst_ctx)
                    except Exception:
                        pass

                # Update primary hr.version in place with all contract details
                if dst_primary_vid:
                    pv_write = {k: val for k, val in primary_vals.items() if k != "company_id"}
                    pv_write["active"] = True
                    self.dst.write(
                        "hr.version",
                        [dst_primary_vid],
                        pv_write,
                        context={**dst_ctx, "sync_contract_dates": True},
                    )
                    if primary_vals.get("job_title") and primary_vals.get("job_id"):
                        self.dst.write(
                            "hr.version",
                            [dst_primary_vid],
                            {"job_title": primary_vals["job_title"]},
                            context={**dst_ctx, "sync_contract_dates": True},
                        )
                    if primary_ver.get("id"):
                        self.state.set_map("hr.version", primary_ver["id"], dst_primary_vid)

                # Create additional historical/secondary versions
                used_active_date_versions: Set[str] = {str(primary_vals["date_version"])}
                active_intervals: List[Tuple[Optional[str], Optional[str]]] = [
                    (primary_vals.get("contract_date_start") or None, primary_vals.get("contract_date_end") or None)
                ]
                for v in emp_vers:
                    if v["id"] == primary_ver.get("id"):
                        continue
                    if self.state.get_dst("hr.version", v["id"]):
                        continue
                    ver_vals = self._build_version_vals(
                        v, dst_eid, dst_cid, def_cal, def_addr, emp_fallback=emp
                    )
                    want_active = bool(v.get("active", True))
                    if want_active:
                        dv_str = str(ver_vals["date_version"])
                        c_s = ver_vals.get("contract_date_start") or None
                        c_e = ver_vals.get("contract_date_end") or None
                        if dv_str in used_active_date_versions or any(
                            self._intervals_overlap(c_s, c_e, es, ee) for es, ee in active_intervals
                        ):
                            want_active = False
                    ver_vals["active"] = want_active
                    if want_active:
                        used_active_date_versions.add(str(ver_vals["date_version"]))
                        active_intervals.append((ver_vals.get("contract_date_start") or None, ver_vals.get("contract_date_end") or None))

                    dst_extra_vid = self.dst.create(
                        "hr.version",
                        ver_vals,
                        context={**dst_ctx, "sync_contract_dates": True},
                    )
                    if ver_vals.get("job_title") and ver_vals.get("job_id"):
                        self.dst.write(
                            "hr.version",
                            [dst_extra_vid],
                            {"job_title": ver_vals["job_title"]},
                            context={**dst_ctx, "sync_contract_dates": True},
                        )
                    self.state.set_map("hr.version", v["id"], dst_extra_vid)

                # Sync departure details and archived status
                if dst_primary_vid:
                    self._sync_employee_departure(
                        dst_eid, dst_primary_vid, emp, primary_ver, primary_vals, bool(dst_uid), dst_ctx
                    )
                if not emp.get("active", True):
                    self.dst.write(
                        "hr.employee",
                        [dst_eid],
                        {"active": False},
                        context={**dst_ctx, "skip_tool_clearance_check": True},
                    )

                self.state.resolve_error("employees", "hr.employee", src_eid)
            except Exception as exc:
                self.state.log_error("employees", "hr.employee", src_eid, str(exc))
                log.error("Failed migrating employee '%s' (%d): %s", emp.get("name"), src_eid, exc)

        # 4. Second Pass: Employee managers, coaches, time-off/attendance managers & department managers
        if not self.dry_run:
            for emp in src_emps:
                dst_eid = self.state.get_dst("hr.employee", emp["id"])
                if not dst_eid:
                    continue
                src_cid = m2o_id(emp.get("company_id")) or 1
                dst_ctx = self.dst_company_ctx(src_cid)
                hier_vals: Dict[str, Any] = {}
                dst_parent = self.state.get_dst("hr.employee", m2o_id(emp.get("parent_id")))
                dst_coach = self.state.get_dst("hr.employee", m2o_id(emp.get("coach_id")))
                if dst_parent and dst_parent != dst_eid:
                    hier_vals["parent_id"] = dst_parent
                if dst_coach and dst_coach != dst_eid:
                    hier_vals["coach_id"] = dst_coach
                if hier_vals:
                    try:
                        self.dst.write("hr.employee", [dst_eid], hier_vals, context=dst_ctx)
                    except Exception as exc:
                        log.warning("Could not set parent/coach for employee dst=%d: %s", dst_eid, exc)

                for mgr_field in ("leave_manager_id", "attendance_manager_id"):
                    src_m_uid = m2o_id(emp.get(mgr_field))
                    dst_m_uid = self.state.get_dst("res.users", src_m_uid) if src_m_uid else None
                    if dst_m_uid and self.dst.has_field("hr.employee", mgr_field):
                        try:
                            self.dst.write("hr.employee", [dst_eid], {mgr_field: dst_m_uid}, context=dst_ctx)
                        except Exception as exc:
                            log.warning("Could not set %s=%d on employee dst=%d: %s", mgr_field, dst_m_uid, dst_eid, exc)

            if self.src.fields_get("hr.department") and self.dst.fields_get("hr.department"):
                for d in self.src.search_read("hr.department", [["manager_id", "!=", False]], ["id", "manager_id"], context=self.src_ctx_all):
                    dst_did = self.state.get_dst("hr.department", d["id"])
                    dst_mgr = self.state.get_dst("hr.employee", m2o_id(d.get("manager_id")))
                    if dst_did and dst_mgr:
                        try:
                            self.dst.write("hr.department", [dst_did], {"manager_id": dst_mgr}, context=self.dst_ctx_quiet)
                        except Exception as exc:
                            log.warning("Could not set manager_id on department dst=%d: %s", dst_did, exc)

        log.info("Employees & contracts phase finished for %d source employees.", len(src_emps))

    # -------------------------------------------------------------------------
    # STEP 5E: EMPLOYEE ATTACHMENTS, DOCUMENTS APP & RESUME CERTIFICATES
    # -------------------------------------------------------------------------
    @staticmethod
    def _doc_name_tokens(text: Any) -> Set[str]:
        if not text:
            return set()
        return {tok for tok in re.findall(r"[a-z0-9]+", str(text).lower()) if len(tok) >= 2}

    def _resolve_employee_doc_target(
        self,
        doc: Dict[str, Any],
        emp_map: Dict[int, int],
        ver_to_emp: Dict[int, int],
        folder_to_emp: Dict[int, int],
        wc_to_emp: Dict[int, int],
        hr_folder_ids: Set[int],
        emp_multi_tokens: List[Tuple[int, Set[str]]],
        emp_unique_tokens: Dict[str, int],
    ) -> Optional[int]:
        res_model = doc.get("res_model")
        res_id = doc.get("res_id")
        if res_model == "hr.employee" and res_id and res_id in emp_map:
            return int(res_id)
        if res_model == "hr.version" and res_id and res_id in ver_to_emp:
            return ver_to_emp[int(res_id)]
        fid = m2o_id(doc.get("folder_id"))
        if fid and fid in folder_to_emp:
            return folder_to_emp[fid]
        pid = m2o_id(doc.get("partner_id"))
        if pid and pid in wc_to_emp:
            return wc_to_emp[pid]
        if fid in hr_folder_ids and (not pid or pid <= 5):
            dtoks = self._doc_name_tokens(doc.get("name"))
            if dtoks:
                multi_hits = [eid for eid, toks in emp_multi_tokens if toks.issubset(dtoks)]
                if len(multi_hits) == 1:
                    return multi_hits[0]
                dtoks_norm = set(dtoks)
                for t in dtoks:
                    if len(t) >= 6 and t.endswith("e"):
                        dtoks_norm.add(t[:-1])
                uniq_hits = {emp_unique_tokens[t] for t in dtoks_norm if t in emp_unique_tokens}
                if len(uniq_hits) == 1:
                    return next(iter(uniq_hits))
        return None

    def _build_employee_doc_context(self) -> Dict[str, Any]:
        """Resolve source<->dest employee maps & lookup indexes for employee documents."""
        src_emps = self.src.search_read(
            "hr.employee",
            [],
            [
                "id", "name", "active", "barcode", "company_id", "work_contact_id",
                "hr_employee_folder_id", "hr_employee_contract_folder_id",
                "message_main_attachment_id",
            ],
            order="id asc",
            context=self.src_ctx_all,
        )
        dst_emps = self.dst.search_read(
            "hr.employee",
            [],
            ["id", "name", "active", "barcode", "company_id", "work_contact_id"],
            order="id asc",
            context=self.dst_ctx_quiet,
        )
        emp_map: Dict[int, int] = {}
        used_dst_eids: Set[int] = set()
        for se in src_emps:
            mapped = self.state.get_dst("hr.employee", se["id"])
            if mapped:
                emp_map[se["id"]] = mapped
                used_dst_eids.add(mapped)

        for se in src_emps:
            if se["id"] in emp_map:
                continue
            src_cid = m2o_id(se.get("company_id")) or 1
            if self.only_company_id and src_cid != self.only_company_id:
                continue
            dst_cid = self.state.get_dst("res.company", src_cid) or 1
            cand: Optional[int] = None
            if se.get("barcode"):
                cand = next(
                    (
                        de["id"]
                        for de in dst_emps
                        if de.get("barcode") == se["barcode"] and de["id"] not in used_dst_eids
                    ),
                    None,
                )
            if not cand:
                cand = next(
                    (
                        de["id"]
                        for de in dst_emps
                        if de.get("name") == se.get("name")
                        and m2o_id(de.get("company_id")) == dst_cid
                        and bool(de.get("active", True)) == bool(se.get("active", True))
                        and de["id"] not in used_dst_eids
                    ),
                    None,
                )
            if cand:
                emp_map[se["id"]] = cand
                used_dst_eids.add(cand)
                if not self.dry_run:
                    self.state.set_map("hr.employee", se["id"], cand)

        wc_to_emp: Dict[int, int] = {}
        folder_to_emp: Dict[int, int] = {}
        dst_emp_wc: Dict[int, Optional[int]] = {de["id"]: m2o_id(de.get("work_contact_id")) for de in dst_emps}
        dst_emp_cid: Dict[int, int] = {de["id"]: (m2o_id(de.get("company_id")) or 1) for de in dst_emps}
        src_emp_by_id: Dict[int, Dict[str, Any]] = {se["id"]: se for se in src_emps}

        # Prefer active employees first for work_contact_id lookup
        for se in sorted(src_emps, key=lambda r: (0 if r.get("active", True) else 1, r["id"])):
            eid = se["id"]
            if eid not in emp_map:
                continue
            wcid = m2o_id(se.get("work_contact_id"))
            if wcid and wcid > 5 and wcid not in wc_to_emp:
                wc_to_emp[wcid] = eid
            for ff in ("hr_employee_folder_id", "hr_employee_contract_folder_id"):
                fid = m2o_id(se.get(ff))
                if fid and fid not in folder_to_emp:
                    folder_to_emp[fid] = eid

        ver_to_emp: Dict[int, int] = {}
        if self.src.fields_get("hr.version"):
            for vr in self.src.search_read(
                "hr.version", [["employee_id", "!=", False]], ["id", "employee_id"], context=self.src_ctx_all
            ):
                eid = m2o_id(vr.get("employee_id"))
                if eid and eid in emp_map:
                    ver_to_emp[vr["id"]] = eid

        return {
            "src_emps": src_emps,
            "src_emp_by_id": src_emp_by_id,
            "emp_map": emp_map,
            "wc_to_emp": wc_to_emp,
            "folder_to_emp": folder_to_emp,
            "ver_to_emp": ver_to_emp,
            "dst_emp_wc": dst_emp_wc,
            "dst_emp_cid": dst_emp_cid,
        }

    def migrate_employee_documents_and_attachments(self):
        if self.skip_employee_documents:
            return
        log.info("=== Phase 5E: Employee Attachments, Documents & Resume Certificates ===")
        if not self.src.fields_get("hr.employee") or not self.dst.fields_get("hr.employee"):
            return

        ctx_data = self._build_employee_doc_context()
        emp_map: Dict[int, int] = ctx_data["emp_map"]
        src_emp_by_id: Dict[int, Dict[str, Any]] = ctx_data["src_emp_by_id"]
        wc_to_emp: Dict[int, int] = ctx_data["wc_to_emp"]
        folder_to_emp: Dict[int, int] = ctx_data["folder_to_emp"]
        ver_to_emp: Dict[int, int] = ctx_data["ver_to_emp"]
        dst_emp_wc: Dict[int, Optional[int]] = ctx_data["dst_emp_wc"]
        dst_emp_cid: Dict[int, int] = ctx_data["dst_emp_cid"]

        if not emp_map:
            log.info("No mapped employees found on destination for attachment/document sync.")
            return

        # Pre-index existing destination hr.employee attachments to deduplicate reliably
        existing_sigs: Dict[int, Set[Tuple[str, str]]] = {}
        dst_att_rows = self.dst.search_read(
            "ir.attachment",
            [["res_model", "=", "hr.employee"], ["res_field", "=", False]],
            ["id", "res_id", "name", "checksum", "file_size", "url"],
            context=self.dst_ctx_quiet,
        )
        for da in dst_att_rows:
            deid = da.get("res_id")
            if not deid:
                continue
            dname = str(da.get("name") or "").strip().lower()
            sigs = existing_sigs.setdefault(deid, set())
            if da.get("checksum"):
                sigs.add((dname, str(da["checksum"])))
            if da.get("file_size") is not None:
                sigs.add((dname, f"size:{da['file_size']}"))
            if da.get("url"):
                sigs.add((dname, f"url:{da['url']}"))

        def is_dup_att(dst_eid: int, name: str, checksum: Optional[str], file_size: Optional[int], url: Optional[str]) -> bool:
            sigs = existing_sigs.get(dst_eid)
            if not sigs:
                return False
            kname = str(name or "").strip().lower()
            if checksum and (kname, str(checksum)) in sigs:
                return True
            if file_size is not None and (kname, f"size:{file_size}") in sigs:
                return True
            if url and (kname, f"url:{url}") in sigs:
                return True
            return False

        def record_att_sig(dst_eid: int, name: str, checksum: Optional[str], file_size: Optional[int], url: Optional[str]):
            sigs = existing_sigs.setdefault(dst_eid, set())
            kname = str(name or "").strip().lower()
            if checksum:
                sigs.add((kname, str(checksum)))
            if file_size is not None:
                sigs.add((kname, f"size:{file_size}"))
            if url:
                sigs.add((kname, f"url:{url}"))

        src_bin_field = "datas" if self.src.has_field("ir.attachment", "datas") else "raw"
        dst_bin_field = "raw" if self.dst.has_field("ir.attachment", "raw") else "datas"
        att_create_ctx = {**self.dst_ctx_quiet, "image_no_postprocess": True}

        # ---------------------------------------------------------------------
        # 1. Direct ir.attachment on hr.employee & hr.version
        # ---------------------------------------------------------------------
        pending_emp_atts: List[Tuple[Dict[str, Any], int]] = []
        for src_model, src_res_ids in (("hr.employee", sorted(emp_map.keys())), ("hr.version", sorted(ver_to_emp.keys()))):
            if not src_res_ids:
                continue
            for res_batch in chunked(src_res_ids, 200):
                att_ids = self.src.search(
                    "ir.attachment",
                    [
                        ["res_model", "=", src_model],
                        ["res_id", "in", res_batch],
                        ["res_field", "=", False],
                    ],
                    order="id asc",
                    context=self.src_ctx_all,
                )
                for meta_batch in chunked(att_ids, BATCH_SIZE):
                    metas = self.src.read(
                        "ir.attachment",
                        meta_batch,
                        ["id", "name", "type", "url", "description", "res_model", "res_id", "company_id", "checksum", "file_size"],
                        context=self.src_ctx_all,
                    )
                    for a in metas:
                        src_aid = a["id"]
                        if self.state.get_dst("ir.attachment", src_aid):
                            continue
                        src_eid = a["res_id"] if src_model == "hr.employee" else ver_to_emp.get(a["res_id"])
                        dst_eid = emp_map.get(src_eid) if src_eid else None
                        if not dst_eid:
                            continue
                        aname = a.get("name") or f"attachment_{src_aid}"
                        if is_dup_att(dst_eid, aname, a.get("checksum"), a.get("file_size"), a.get("url")):
                            continue
                        pending_emp_atts.append((a, dst_eid))

        att_copied = 0
        if self.dry_run:
            log.info("[DRY-RUN] Direct employee/version ir.attachment to copy: %d", len(pending_emp_atts))
        else:
            for batch in chunked(pending_emp_atts, ATTACHMENT_BATCH_SIZE):
                bin_by_id: Dict[int, Any] = {}
                bin_ids = [a["id"] for a, _ in batch if a.get("type", "binary") != "url"]
                if bin_ids:
                    for brow in self.src.read(
                        "ir.attachment",
                        bin_ids,
                        ["id", src_bin_field],
                        context={**self.src_ctx_all, "bin_size": False},
                    ):
                        bin_by_id[brow["id"]] = brow.get(src_bin_field)
                for a, dst_eid in batch:
                    src_aid = a["id"]
                    try:
                        src_cid = m2o_id(a.get("company_id"))
                        aname = a.get("name") or f"attachment_{src_aid}"
                        vals: Dict[str, Any] = {
                            "name": aname,
                            "type": a.get("type") or "binary",
                            "res_model": "hr.employee",
                            "res_id": dst_eid,
                            "description": a.get("description") or False,
                            "company_id": self.state.get_dst("res.company", src_cid) if src_cid else dst_emp_cid.get(dst_eid, False),
                        }
                        if vals["type"] == "url":
                            vals["url"] = a.get("url") or False
                        else:
                            bval = self._extract_b64(bin_by_id.get(src_aid))
                            if not bval:
                                continue
                            vals[dst_bin_field] = bval
                        dst_aid = self.dst.create("ir.attachment", vals, context=att_create_ctx)
                        self.state.set_map("ir.attachment", src_aid, dst_aid)
                        self.state.resolve_error("employee_documents", "ir.attachment", src_aid)
                        record_att_sig(dst_eid, aname, a.get("checksum"), a.get("file_size"), a.get("url"))
                        att_copied += 1
                    except Exception as exc:
                        self.state.log_error("employee_documents", "ir.attachment", src_aid, str(exc))
                        log.error("Failed employee attachment '%s' (%d): %s", a.get("name"), src_aid, exc)
            log.info("Direct employee/version ir.attachment migrated: %d", att_copied)

            # Sync message_main_attachment_id onto destination hr.employee
            for se in ctx_data["src_emps"]:
                dst_eid = emp_map.get(se["id"])
                src_main = m2o_id(se.get("message_main_attachment_id"))
                dst_main = self.state.get_dst("ir.attachment", src_main) if src_main else None
                if dst_eid and dst_main:
                    try:
                        self.dst.write("hr.employee", [dst_eid], {"message_main_attachment_id": dst_main}, context=self.dst_ctx_quiet)
                    except Exception:
                        pass

        # ---------------------------------------------------------------------
        # 2. Employee Documents from Odoo 19 Documents App (documents.document)
        # ---------------------------------------------------------------------
        if self.src.fields_get("documents.document"):
            src_folders = self.src.search_read(
                "documents.document",
                [["type", "=", "folder"]],
                ["id", "name", "folder_id"],
                context=self.src_ctx_all,
            )
            folder_by_id = {f["id"]: f for f in src_folders}

            def get_folder_chain(fid: Optional[int]) -> List[str]:
                names: List[str] = []
                seen: Set[int] = set()
                cur = fid
                while cur and cur not in seen and cur in folder_by_id:
                    seen.add(cur)
                    row = folder_by_id[cur]
                    if row.get("name"):
                        names.append(str(row["name"]))
                    cur = m2o_id(row.get("folder_id"))
                return list(reversed(names))

            hr_folder_ids: Set[int] = set()
            folder_paths: Dict[int, str] = {}
            for fid in folder_by_id:
                chain = get_folder_chain(fid)
                folder_paths[fid] = " / ".join(chain)
                if any(
                    word in {"hr", "human resources", "employee", "employees", "contracts", "recruitment", "payroll"}
                    or any(w in seg.lower() for w in ("hr", "employee", "contract"))
                    for seg in chain
                    for word in [seg.strip().lower()]
                ):
                    hr_folder_ids.add(fid)

            # Build conservative employee name matchers for HR-folder files missing partner_id
            emp_multi_tokens: List[Tuple[int, Set[str]]] = []
            token_to_emp_ids: Dict[str, Set[int]] = {}
            for eid, se in src_emp_by_id.items():
                if eid not in emp_map:
                    continue
                etoks = {t for t in self._doc_name_tokens(se.get("name")) if len(t) >= 3}
                if len(etoks) >= 2:
                    emp_multi_tokens.append((eid, etoks))
                for t in etoks:
                    if len(t) >= 5:
                        token_to_emp_ids.setdefault(t, set()).add(eid)
            emp_unique_tokens = {t: next(iter(eids)) for t, eids in token_to_emp_ids.items() if len(eids) == 1}

            src_docs = self.src.search_read(
                "documents.document",
                [["type", "!=", "folder"]],
                [
                    "id", "name", "type", "url", "description", "partner_id",
                    "folder_id", "res_model", "res_id", "attachment_id",
                    "company_id", "checksum", "file_size", "active",
                ],
                order="id asc",
                context=self.src_ctx_all,
            )

            dst_has_documents_app = bool(self.dst.fields_get("documents.document"))
            pending_emp_docs: List[Tuple[Dict[str, Any], int, str]] = []
            for d in src_docs:
                src_eid = self._resolve_employee_doc_target(
                    d,
                    emp_map=emp_map,
                    ver_to_emp=ver_to_emp,
                    folder_to_emp=folder_to_emp,
                    wc_to_emp=wc_to_emp,
                    hr_folder_ids=hr_folder_ids,
                    emp_multi_tokens=emp_multi_tokens,
                    emp_unique_tokens=emp_unique_tokens,
                )
                if not src_eid or src_eid not in emp_map:
                    continue
                dst_eid = emp_map[src_eid]
                dname = d.get("name") or f"document_{d['id']}"
                if self.state.get_dst("documents.document", d["id"]) and not dst_has_documents_app:
                    continue
                if is_dup_att(dst_eid, dname, d.get("checksum"), d.get("file_size"), d.get("url")) and not dst_has_documents_app:
                    continue
                fpath = folder_paths.get(m2o_id(d.get("folder_id")) or 0, "")
                pending_emp_docs.append((d, dst_eid, fpath))

            if self.dry_run:
                log.info("[DRY-RUN] Employee documents.document to copy: %d", len(pending_emp_docs))
            else:
                dst_hr_folder_id: Optional[int] = None
                if dst_has_documents_app and pending_emp_docs:
                    existing_hr_f = self.dst.search(
                        "documents.document",
                        [["type", "=", "folder"], ["name", "=", "HR"]],
                        limit=1,
                        context=self.dst_ctx_quiet,
                    )
                    if existing_hr_f:
                        dst_hr_folder_id = existing_hr_f[0]
                    else:
                        dst_hr_folder_id = self.dst.create(
                            "documents.document",
                            {"name": "HR", "type": "folder"},
                            context=self.dst_ctx_quiet,
                        )

                docs_copied = 0
                for batch in chunked(pending_emp_docs, ATTACHMENT_BATCH_SIZE):
                    bin_by_doc_id: Dict[int, Any] = {}
                    doc_bin_ids = [d["id"] for d, _, _ in batch if d.get("type", "binary") != "url"]
                    if doc_bin_ids:
                        for drow in self.src.read(
                            "documents.document",
                            doc_bin_ids,
                            ["id", "datas"],
                            context={**self.src_ctx_all, "bin_size": False},
                        ):
                            bin_by_doc_id[drow["id"]] = drow.get("datas")

                    # Fallback to underlying ir.attachment if documents.document.datas is empty
                    fallback_att_map = {
                        d["id"]: m2o_id(d.get("attachment_id"))
                        for d, _, _ in batch
                        if not self._extract_b64(bin_by_doc_id.get(d["id"])) and m2o_id(d.get("attachment_id"))
                    }
                    if fallback_att_map:
                        for arow in self.src.read(
                            "ir.attachment",
                            sorted(set(fallback_att_map.values())),
                            ["id", src_bin_field],
                            context={**self.src_ctx_all, "bin_size": False},
                        ):
                            for did, aid in fallback_att_map.items():
                                if aid == arow["id"]:
                                    bin_by_doc_id[did] = arow.get(src_bin_field)

                    for d, dst_eid, fpath in batch:
                        src_did = d["id"]
                        try:
                            dname = d.get("name") or f"document_{src_did}"
                            src_cid = m2o_id(d.get("company_id"))
                            dst_cid = self.state.get_dst("res.company", src_cid) if src_cid else dst_emp_cid.get(dst_eid, False)
                            bval = self._extract_b64(bin_by_doc_id.get(src_did))
                            is_url = d.get("type") == "url"
                            if not is_url and not bval:
                                continue

                            dst_created_id: Optional[int] = None
                            if not is_dup_att(dst_eid, dname, d.get("checksum"), d.get("file_size"), d.get("url")):
                                desc_parts = [p for p in (f"Documents Folder: {fpath}" if fpath else "", d.get("description") or "") if p]
                                att_vals: Dict[str, Any] = {
                                    "name": dname,
                                    "type": "url" if is_url else "binary",
                                    "res_model": "hr.employee",
                                    "res_id": dst_eid,
                                    "description": "\n".join(desc_parts) or False,
                                    "company_id": dst_cid or False,
                                }
                                if is_url:
                                    att_vals["url"] = d.get("url") or False
                                else:
                                    att_vals[dst_bin_field] = bval
                                dst_created_id = self.dst.create("ir.attachment", att_vals, context=att_create_ctx)
                                record_att_sig(dst_eid, dname, d.get("checksum"), d.get("file_size"), d.get("url"))

                            if dst_has_documents_app and not self.state.get_dst("documents.document", src_did):
                                doc_vals: Dict[str, Any] = {
                                    "name": dname,
                                    "type": "url" if is_url else "binary",
                                    "folder_id": dst_hr_folder_id or False,
                                    "partner_id": dst_emp_wc.get(dst_eid) or False,
                                    "res_model": "hr.employee",
                                    "res_id": dst_eid,
                                    "company_id": dst_cid or False,
                                    "active": bool(d.get("active", True)),
                                }
                                if is_url:
                                    doc_vals["url"] = d.get("url") or False
                                else:
                                    doc_vals["datas"] = bval
                                dst_doc_id = self.dst.create("documents.document", doc_vals, context=self.dst_ctx_quiet)
                                self.state.set_map("documents.document", src_did, dst_doc_id)
                            elif dst_created_id:
                                self.state.set_map("documents.document", src_did, dst_created_id)

                            self.state.resolve_error("employee_documents", "documents.document", src_did)
                            docs_copied += 1
                        except Exception as exc:
                            self.state.log_error("employee_documents", "documents.document", src_did, str(exc))
                            log.error("Failed employee document '%s' (%d): %s", d.get("name"), src_did, exc)
                log.info("Employee documents.document migrated onto hr.employee: %d", docs_copied)

        # ---------------------------------------------------------------------
        # 3. Employee Resume Lines & Certificate Files (hr.resume.line)
        # ---------------------------------------------------------------------
        if self.src.fields_get("hr.resume.line") and self.dst.fields_get("hr.resume.line"):
            if self.src.fields_get("hr.resume.line.type") and self.dst.fields_get("hr.resume.line.type"):
                dst_rtypes = {
                    rt["name"].strip().lower(): rt["id"]
                    for rt in self.dst.search_read("hr.resume.line.type", [], ["id", "name"], context=self.dst_ctx_quiet)
                    if rt.get("name")
                }
                for srt in self.src.search_read(
                    "hr.resume.line.type", [], ["id", "name", "sequence", "is_course"], context=self.src_ctx_all
                ):
                    if self.state.get_dst("hr.resume.line.type", srt["id"]):
                        continue
                    rname = (srt.get("name") or "").strip()
                    match = dst_rtypes.get(rname.lower())
                    if match:
                        self.state.set_map("hr.resume.line.type", srt["id"], match)
                    elif not self.dry_run and rname:
                        try:
                            dst_rtid = self.dst.create(
                                "hr.resume.line.type",
                                {
                                    "name": rname,
                                    "sequence": srt.get("sequence", 10),
                                    "is_course": bool(srt.get("is_course", False)),
                                },
                                context=self.dst_ctx_quiet,
                            )
                            self.state.set_map("hr.resume.line.type", srt["id"], dst_rtid)
                            dst_rtypes[rname.lower()] = dst_rtid
                        except Exception as exc:
                            log.warning("Could not create hr.resume.line.type '%s': %s", rname, exc)

            existing_resumes: Set[Tuple[int, str, str]] = set()
            for drl in self.dst.search_read(
                "hr.resume.line", [], ["id", "employee_id", "name", "date_start"], context=self.dst_ctx_quiet
            ):
                deid = m2o_id(drl.get("employee_id"))
                if deid:
                    existing_resumes.add((deid, str(drl.get("name") or "").strip().lower(), str(drl.get("date_start") or "")))

            src_rlines = self.src.search_read(
                "hr.resume.line",
                [],
                [
                    "id", "employee_id", "name", "date_start", "date_end", "description",
                    "line_type_id", "course_type", "external_url", "department_id", "duration", "color",
                    "certificate_filename",
                ],
                order="id asc",
                context=self.src_ctx_all,
            )
            pending_rlines: List[Tuple[Dict[str, Any], int]] = []
            for rl in src_rlines:
                if self.state.get_dst("hr.resume.line", rl["id"]):
                    continue
                src_eid = m2o_id(rl.get("employee_id"))
                dst_eid = emp_map.get(src_eid) if src_eid else None
                if not dst_eid:
                    continue
                dstart = str(rl.get("date_start") or "2020-01-01")
                rkey = (dst_eid, str(rl.get("name") or "").strip().lower(), dstart)
                if rkey in existing_resumes:
                    continue
                pending_rlines.append((rl, dst_eid))

            if self.dry_run:
                log.info("[DRY-RUN] Employee hr.resume.line records to copy: %d", len(pending_rlines))
            else:
                resumes_copied = 0
                for batch in chunked(pending_rlines, ATTACHMENT_BATCH_SIZE):
                    cert_by_id: Dict[int, Any] = {}
                    for rrow in self.src.read(
                        "hr.resume.line",
                        [rl["id"] for rl, _ in batch],
                        ["id", "certificate_file"],
                        context={**self.src_ctx_all, "bin_size": False},
                    ):
                        cert = self._extract_b64(rrow.get("certificate_file"))
                        if cert:
                            cert_by_id[rrow["id"]] = cert

                    for rl, dst_eid in batch:
                        src_rlid = rl["id"]
                        try:
                            dstart = rl.get("date_start") or "2020-01-01"
                            rvals: Dict[str, Any] = {
                                "employee_id": dst_eid,
                                "name": rl.get("name") or "Resume Entry",
                                "date_start": dstart,
                                "date_end": rl.get("date_end") or False,
                                "description": rl.get("description") or False,
                                "line_type_id": self.state.get_dst("hr.resume.line.type", m2o_id(rl.get("line_type_id"))) or False,
                                "course_type": rl.get("course_type") or "external",
                                "external_url": rl.get("external_url") or False,
                                "department_id": self.state.get_dst("hr.department", m2o_id(rl.get("department_id"))) or False,
                                "duration": int(rl.get("duration") or 0),
                                "color": int(rl.get("color") or 0),
                            }
                            cert_b64 = cert_by_id.get(src_rlid)
                            if cert_b64:
                                rvals["certificate_file"] = cert_b64
                            dst_rlid = self.dst.create("hr.resume.line", rvals, context=self.dst_ctx_quiet)
                            self.state.set_map("hr.resume.line", src_rlid, dst_rlid)
                            existing_resumes.add((dst_eid, str(rvals["name"]).strip().lower(), str(dstart)))

                            if cert_b64:
                                cname = rl.get("certificate_filename") or f"{rvals['name']}.pdf"
                                if not is_dup_att(dst_eid, cname, None, None, None):
                                    self.dst.create(
                                        "ir.attachment",
                                        {
                                            "name": cname,
                                            "type": "binary",
                                            "res_model": "hr.employee",
                                            "res_id": dst_eid,
                                            "company_id": dst_emp_cid.get(dst_eid, False),
                                            dst_bin_field: cert_b64,
                                        },
                                        context=att_create_ctx,
                                    )
                                    record_att_sig(dst_eid, cname, None, None, None)

                            self.state.resolve_error("employee_documents", "hr.resume.line", src_rlid)
                            resumes_copied += 1
                        except Exception as exc:
                            self.state.log_error("employee_documents", "hr.resume.line", src_rlid, str(exc))
                            log.error("Failed hr.resume.line '%s' (%d): %s", rl.get("name"), src_rlid, exc)
                log.info("Employee hr.resume.line migrated: %d", resumes_copied)

    # -------------------------------------------------------------------------
    # STEP 6: PARTNERS & PARTNER BANKS (Referenced in Accounting)
    # -------------------------------------------------------------------------
    def collect_used_partner_and_product_ids(self) -> Tuple[Set[int], Set[int]]:
        partner_ids: Set[int] = set()
        product_ids: Set[int] = set()
        company_ids = self.get_source_company_ids()

        # From moves
        for model, domain, field in [
            ("account.move", [["company_id", "in", company_ids]], "partner_id"),
            ("account.move", [["company_id", "in", company_ids]], "commercial_partner_id"),
            ("account.move.line", [["company_id", "in", company_ids]], "partner_id"),
            ("account.payment", [["company_id", "in", company_ids]], "partner_id"),
            ("account.bank.statement.line", [["company_id", "in", company_ids]], "partner_id"),
        ]:
            offset = 0
            while True:
                rows = self.src.search_read(
                    model, domain, [field], offset=offset, limit=1000, context=self.src_ctx_all
                )
                if not rows:
                    break
                for r in rows:
                    pid = m2o_id(r.get(field))
                    if pid:
                        partner_ids.add(pid)
                offset += len(rows)

        # From move lines -> products
        offset = 0
        while True:
            rows = self.src.search_read(
                "account.move.line",
                [["company_id", "in", company_ids], ["product_id", "!=", False]],
                ["product_id"],
                offset=offset,
                limit=1000,
                context=self.src_ctx_all,
            )
            if not rows:
                break
            for r in rows:
                prod_id = m2o_id(r.get("product_id"))
                if prod_id:
                    product_ids.add(prod_id)
            offset += len(rows)

        # Include company partner_ids and parent partners
        comp_rows = self.src.read("res.company", company_ids, ["partner_id"])
        for cr in comp_rows:
            if m2o_id(cr.get("partner_id")):
                partner_ids.add(m2o_id(cr["partner_id"]))

        if partner_ids:
            parent_rows = self.src.read(
                "res.partner", list(partner_ids), ["parent_id"], context=self.src_ctx_all
            )
            for pr in parent_rows:
                if m2o_id(pr.get("parent_id")):
                    partner_ids.add(m2o_id(pr["parent_id"]))

        return partner_ids, product_ids

    def migrate_partners(self, used_partner_ids: Set[int]):
        log.info("=== Phase 6: Partners (%d used) & Bank Accounts ===", len(used_partner_ids))
        if not used_partner_ids:
            return

        # Pre-map company partners
        for src_cid in self.get_source_company_ids():
            dst_cid = self.state.get_dst("res.company", src_cid)
            if not dst_cid:
                continue
            sp = m2o_id(self.src.read("res.company", [src_cid], ["partner_id"])[0].get("partner_id"))
            dp = m2o_id(self.dst.read("res.company", [dst_cid], ["partner_id"])[0].get("partner_id"))
            if sp and dp and not self.state.get_dst("res.partner", sp):
                self.state.set_map("res.partner", sp, dp)

        fields = [
            "id", "name", "is_company", "parent_id", "type", "ref", "vat", "email", "phone",
            "street", "street2", "city", "zip", "country_id", "state_id", "website",
            "company_id", "active", "customer_rank", "supplier_rank",
            "property_account_receivable_id", "property_account_payable_id",
            "property_payment_term_id", "property_supplier_payment_term_id",
        ]

        # Read all used partners and sort parents before children
        all_partners = []
        for batch in chunked(sorted(used_partner_ids), BATCH_SIZE):
            all_partners.extend(self.src.read("res.partner", batch, fields, context=self.src_ctx_all))
        all_partners.sort(key=lambda p: (1 if m2o_id(p.get("parent_id")) else 0, p["id"]))

        for p in all_partners:
            if self.state.get_dst("res.partner", p["id"]):
                continue
            src_cid = m2o_id(p.get("company_id"))
            dst_cid = self.state.get_dst("res.company", src_cid) if src_cid else False
            dst_ctx = self.dst_company_ctx(src_cid)

            # Match existing partner cleanly by VAT, Ref, or (Name + Email)
            existing = []
            if p.get("vat"):
                existing = self.dst.search(
                    "res.partner", [["vat", "=", p["vat"]], ["is_company", "=", p.get("is_company", False)]],
                    limit=1, context=dst_ctx,
                )
            if not existing and p.get("ref"):
                existing = self.dst.search(
                    "res.partner", [["ref", "=", p["ref"]]], limit=1, context=dst_ctx
                )
            if not existing and p.get("name"):
                dom = [["name", "=", p["name"]]]
                if p.get("email"):
                    dom.append(["email", "=", p["email"]])
                existing = self.dst.search("res.partner", dom, limit=1, context=dst_ctx)

            if existing:
                self.state.set_map("res.partner", p["id"], existing[0])
                continue

            if self.dry_run:
                log.info("[DRY-RUN] Missing partner -> would create: %s", p.get("name"))
                continue

            vals = {
                "name": p.get("name") or f"Partner {p['id']}",
                "is_company": p.get("is_company", False),
                "parent_id": self.state.get_dst("res.partner", m2o_id(p.get("parent_id"))) or False,
                "type": p.get("type") or "contact",
                "ref": p.get("ref") or False,
                "vat": p.get("vat") or False,
                "email": p.get("email") or False,
                "phone": p.get("phone") or False,
                "street": p.get("street") or False,
                "street2": p.get("street2") or False,
                "city": p.get("city") or False,
                "zip": p.get("zip") or False,
                "country_id": self.state.get_dst("res.country", m2o_id(p.get("country_id"))) or False,
                "state_id": self.state.get_dst("res.country.state", m2o_id(p.get("state_id"))) or False,
                "website": p.get("website") or False,
                "company_id": dst_cid or False,
                "active": p.get("active", True),
                "customer_rank": p.get("customer_rank", 0),
                "supplier_rank": p.get("supplier_rank", 0),
            }
            for acc_f in ("property_account_receivable_id", "property_account_payable_id"):
                aid = self.state.get_dst("account.account", m2o_id(p.get(acc_f)))
                if aid:
                    vals[acc_f] = aid
            for pt_f in ("property_payment_term_id", "property_supplier_payment_term_id"):
                ptid = self.state.get_dst("account.payment.term", m2o_id(p.get(pt_f)))
                if ptid:
                    vals[pt_f] = ptid

            try:
                dst_id = self.dst.create("res.partner", vals, context=dst_ctx)
                self.state.set_map("res.partner", p["id"], dst_id)
            except Exception as exc:
                self.state.log_error("partners", "res.partner", p["id"], str(exc))
                log.error("Failed creating partner %s (%d): %s", p.get("name"), p["id"], exc)

        # Partner Bank Accounts (needed for journals & payments)
        src_acc_field = "account_number" if self.src.has_field("res.partner.bank", "account_number") else "acc_number"
        dst_acc_field = "account_number" if self.dst.has_field("res.partner.bank", "account_number") else "acc_number"
        src_holder_field = "holder_name" if self.src.has_field("res.partner.bank", "holder_name") else "acc_holder_name"
        dst_holder_field = "holder_name" if self.dst.has_field("res.partner.bank", "holder_name") else "acc_holder_name"
        dst_has_bank_id = self.dst.has_field("res.partner.bank", "bank_id")
        src_banks_by_id = {} if dst_has_bank_id else self._get_src_banks_by_id()

        src_banks = self.src.search_read(
            "res.partner.bank",
            [],
            [
                "id", src_acc_field, "partner_id", "bank_id", "currency_id", "company_id",
                "active", src_holder_field, "allow_out_payment",
                "bank_name", "bank_bic", "street", "street2", "zip", "city", "state_id", "country_id",
            ],
            context=self.src_ctx_all,
        )
        for pb in src_banks:
            if self.state.get_dst("res.partner.bank", pb["id"]):
                continue
            acc_num = pb.get(src_acc_field)
            if not acc_num:
                continue
            dst_partner = self.state.get_dst("res.partner", m2o_id(pb.get("partner_id")))
            if not dst_partner:
                continue
            src_cid = m2o_id(pb.get("company_id"))
            dst_cid = self.state.get_dst("res.company", src_cid) if src_cid else False
            existing = self.dst.search(
                "res.partner.bank",
                [[dst_acc_field, "=", acc_num], ["partner_id", "=", dst_partner]],
                limit=1,
                context=self.dst_ctx_quiet,
            )
            if existing:
                self.state.set_map("res.partner.bank", pb["id"], existing[0])
            elif not self.dry_run:
                vals: Dict[str, Any] = {
                    dst_acc_field: acc_num,
                    "partner_id": dst_partner,
                    "company_id": dst_cid or False,
                    "active": pb.get("active", True),
                    dst_holder_field: pb.get(src_holder_field) or False,
                    "allow_out_payment": True,
                }
                if dst_has_bank_id:
                    vals["bank_id"] = self.state.get_dst("res.bank", m2o_id(pb.get("bank_id"))) or False
                    vals["currency_id"] = self.state.get_dst("res.currency", m2o_id(pb.get("currency_id"))) or False
                else:
                    src_bank = src_banks_by_id.get(m2o_id(pb.get("bank_id")) or 0) or {}
                    src_state_id = m2o_id(src_bank.get("state")) or m2o_id(pb.get("state_id"))
                    src_country_id = m2o_id(src_bank.get("country")) or m2o_id(pb.get("country_id"))
                    vals.update(
                        {
                            "bank_name": src_bank.get("name") or pb.get("bank_name") or False,
                            "bank_bic": src_bank.get("bic") or pb.get("bank_bic") or False,
                            "street": src_bank.get("street") or pb.get("street") or False,
                            "street2": src_bank.get("street2") or pb.get("street2") or False,
                            "zip": src_bank.get("zip") or pb.get("zip") or False,
                            "city": src_bank.get("city") or pb.get("city") or False,
                            "state_id": self.state.get_dst("res.country.state", src_state_id) or False,
                            "country_id": self.state.get_dst("res.country", src_country_id) or False,
                        }
                    )
                try:
                    dst_id = self.dst.create("res.partner.bank", vals, context=self.dst_ctx_quiet)
                    self.state.set_map("res.partner.bank", pb["id"], dst_id)
                except Exception as exc:
                    self.state.log_error("partner_banks", "res.partner.bank", pb["id"], str(exc))
                    log.error("Failed creating partner bank %s (%d): %s", acc_num, pb["id"], exc)

        self._ensure_bank_accounts_trusted()

    # -------------------------------------------------------------------------
    # STEP 7: PRODUCTS & UOMS (Referenced in Invoice/Bill Lines)
    # -------------------------------------------------------------------------
    def migrate_products(self, used_product_ids: Set[int]):
        log.info("=== Phase 7: Products (%d used) & UoMs ===", len(used_product_ids))
        if not used_product_ids:
            return

        # Map UoMs by name
        dst_uoms = {
            u["name"].strip().lower(): u["id"]
            for u in self.dst.search_read("uom.uom", [], ["id", "name"], context=self.dst_ctx_quiet)
        }
        for u in self.src.search_read("uom.uom", [], ["id", "name"], context=self.src_ctx_all):
            if not self.state.get_dst("uom.uom", u["id"]):
                match = dst_uoms.get(u["name"].strip().lower())
                if match:
                    self.state.set_map("uom.uom", u["id"], match)

        # Product Categories
        src_cats = self.src.search_read(
            "product.category",
            [],
            ["id", "name", "parent_id", "property_account_income_categ_id", "property_account_expense_categ_id"],
            order="parent_id nulls first, id asc",
            context=self.src_ctx_all,
        )
        for cat in src_cats:
            if self.state.get_dst("product.category", cat["id"]):
                continue
            existing = self.dst.search("product.category", [["name", "=", cat["name"]]], limit=1)
            if existing:
                self.state.set_map("product.category", cat["id"], existing[0])
            elif not self.dry_run:
                vals = {
                    "name": cat["name"],
                    "parent_id": self.state.get_dst("product.category", m2o_id(cat.get("parent_id"))) or False,
                }
                inc = self.state.get_dst("account.account", m2o_id(cat.get("property_account_income_categ_id")))
                exp = self.state.get_dst("account.account", m2o_id(cat.get("property_account_expense_categ_id")))
                if inc:
                    vals["property_account_income_categ_id"] = inc
                if exp:
                    vals["property_account_expense_categ_id"] = exp
                dst_id = self.dst.create("product.category", vals, context=self.dst_ctx_quiet)
                self.state.set_map("product.category", cat["id"], dst_id)

        fields = [
            "id", "name", "default_code", "barcode", "type", "categ_id", "uom_id",
            "list_price", "standard_price", "company_id", "active",
            "taxes_id", "supplier_taxes_id",
            "property_account_income_id", "property_account_expense_id",
        ]
        for batch in chunked(sorted(used_product_ids), BATCH_SIZE):
            prods = self.src.read("product.product", batch, fields, context=self.src_ctx_all)
            for prod in prods:
                if self.state.get_dst("product.product", prod["id"]):
                    continue
                existing = []
                if prod.get("default_code"):
                    existing = self.dst.search(
                        "product.product",
                        [["default_code", "=", prod["default_code"]]],
                        limit=1,
                        context=self.dst_ctx_quiet,
                    )
                if not existing and prod.get("barcode"):
                    existing = self.dst.search(
                        "product.product",
                        [["barcode", "=", prod["barcode"]]],
                        limit=1,
                        context=self.dst_ctx_quiet,
                    )
                if not existing:
                    existing = self.dst.search(
                        "product.product",
                        [["name", "=", prod["name"]]],
                        limit=1,
                        context=self.dst_ctx_quiet,
                    )

                if existing:
                    self.state.set_map("product.product", prod["id"], existing[0])
                    continue

                if self.dry_run:
                    log.info("[DRY-RUN] Missing product -> would create: %s", prod["name"])
                    continue

                src_cid = m2o_id(prod.get("company_id"))
                vals = {
                    "name": prod["name"],
                    "default_code": prod.get("default_code") or False,
                    "barcode": prod.get("barcode") or False,
                    "type": prod.get("type") or "consu",
                    "categ_id": self.state.get_dst("product.category", m2o_id(prod.get("categ_id"))) or 1,
                    "list_price": prod.get("list_price", 0.0),
                    "standard_price": prod.get("standard_price", 0.0),
                    "company_id": self.state.get_dst("res.company", src_cid) if src_cid else False,
                    "active": prod.get("active", True),
                }
                if self.state.get_dst("uom.uom", m2o_id(prod.get("uom_id"))):
                    vals["uom_id"] = self.state.get_dst("uom.uom", m2o_id(prod["uom_id"]))
                inc = self.state.get_dst("account.account", m2o_id(prod.get("property_account_income_id")))
                exp = self.state.get_dst("account.account", m2o_id(prod.get("property_account_expense_id")))
                if inc:
                    vals["property_account_income_id"] = inc
                if exp:
                    vals["property_account_expense_id"] = exp
                t_in = [self.state.get_dst("account.tax", t) for t in (prod.get("taxes_id") or [])]
                t_out = [self.state.get_dst("account.tax", t) for t in (prod.get("supplier_taxes_id") or [])]
                if any(t_in):
                    vals["taxes_id"] = [[6, 0, [t for t in t_in if t]]]
                if any(t_out):
                    vals["supplier_taxes_id"] = [[6, 0, [t for t in t_out if t]]]

                try:
                    dst_id = self.dst.create("product.product", vals, context=self.dst_ctx_quiet)
                    self.state.set_map("product.product", prod["id"], dst_id)
                except Exception as exc:
                    self.state.log_error("products", "product.product", prod["id"], str(exc))
                    log.error("Failed creating product %s: %s", prod["name"], exc)

    # -------------------------------------------------------------------------
    # STEP 8: JOURNALS & PAYMENT METHOD LINES
    # -------------------------------------------------------------------------
    def migrate_journals(self):
        log.info("=== Phase 8: Accounting Journals & Payment Method Lines ===")
        for src_cid in self.get_source_company_ids():
            dst_cid = self.state.get_dst("res.company", src_cid)
            if not dst_cid:
                continue
            src_ctx = self.src_company_ctx(src_cid)
            dst_ctx = self.dst_company_ctx(src_cid)

            if not self.dry_run and self.dst.has_field("res.company", "account_journal_suspense_account_id"):
                src_comp = self.src.read("res.company", [src_cid], ["account_journal_suspense_account_id"], context=src_ctx)
                dst_comp = self.dst.read("res.company", [dst_cid], ["account_journal_suspense_account_id"], context=dst_ctx)
                if src_comp and dst_comp:
                    dst_susp = self.state.get_dst("account.account", m2o_id(src_comp[0].get("account_journal_suspense_account_id")))
                    if dst_susp and m2o_id(dst_comp[0].get("account_journal_suspense_account_id")) != dst_susp:
                        self.dst.write("res.company", [dst_cid], {"account_journal_suspense_account_id": dst_susp}, context=dst_ctx)
                        log.info("Synced company %d account_journal_suspense_account_id -> %d", dst_cid, dst_susp)

            src_journals = self.src.search_read(
                "account.journal",
                [["company_id", "=", src_cid]],
                [
                    "id", "name", "code", "type", "active", "currency_id",
                    "default_account_id", "suspense_account_id",
                    "profit_account_id", "loss_account_id",
                    "bank_account_id", "refund_sequence", "payment_sequence",
                    "sequence_override_regex", "restrict_mode_hash_table",
                    "inbound_payment_method_line_ids", "outbound_payment_method_line_ids",
                ],
                order="id asc",
                context=src_ctx,
            )

            for j in src_journals:
                dst_jid = self.state.get_dst("account.journal", j["id"])
                if not dst_jid:
                    existing = self.dst.search(
                        "account.journal",
                        [["code", "=", j["code"]], ["company_id", "=", dst_cid]],
                        limit=1,
                        context=dst_ctx,
                    )
                    if existing:
                        dst_jid = existing[0]
                        self.state.set_map("account.journal", j["id"], dst_jid)
                    elif self.dry_run:
                        log.info("[DRY-RUN] Missing journal -> would create: [%s] %s (%s)", j["code"], j["name"], j["type"])
                        continue
                    else:
                        vals = {
                            "name": j["name"],
                            "code": j["code"],
                            "type": j["type"],
                            "company_id": dst_cid,
                            "active": j.get("active", True),
                            "currency_id": self.state.get_dst("res.currency", m2o_id(j.get("currency_id"))) or False,
                            "default_account_id": self.state.get_dst("account.account", m2o_id(j.get("default_account_id"))) or False,
                            "suspense_account_id": self.state.get_dst("account.account", m2o_id(j.get("suspense_account_id"))) or False,
                            "profit_account_id": self.state.get_dst("account.account", m2o_id(j.get("profit_account_id"))) or False,
                            "loss_account_id": self.state.get_dst("account.account", m2o_id(j.get("loss_account_id"))) or False,
                            "bank_account_id": self.state.get_dst("res.partner.bank", m2o_id(j.get("bank_account_id"))) or False,
                            "refund_sequence": j.get("refund_sequence", False),
                            "payment_sequence": j.get("payment_sequence", False),
                            "sequence_override_regex": j.get("sequence_override_regex") or False,
                        }
                        try:
                            dst_jid = self.dst.create("account.journal", vals, context=dst_ctx)
                            self.state.set_map("account.journal", j["id"], dst_jid)
                            log.info("Created missing journal: [%s] %s (%d -> %d)", j["code"], j["name"], j["id"], dst_jid)
                        except Exception as exc:
                            self.state.log_error("journals", "account.journal", j["id"], str(exc))
                            log.error("Failed creating journal [%s] %s: %s", j["code"], j["name"], exc)
                            continue

                # Sync journal accounts & map payment method lines on the journal
                if dst_jid and not self.dry_run:
                    self._sync_journal_accounts(j, dst_jid, dst_ctx)
                    self._map_payment_method_lines(j, dst_jid, src_ctx, dst_ctx)

    def _sync_journal_accounts(self, src_j: dict, dst_jid: int, dst_ctx: dict):
        """Align destination journal accounts (default, suspense, profit, loss, bank_account) with source."""
        fields = [
            "default_account_id",
            "suspense_account_id",
            "profit_account_id",
            "loss_account_id",
            "bank_account_id",
        ]
        dst_rows = self.dst.read("account.journal", [dst_jid], fields, context=dst_ctx)
        if not dst_rows:
            return
        dst_j = dst_rows[0]

        updates: Dict[str, Any] = {}
        for acc_f in ("default_account_id", "suspense_account_id", "profit_account_id", "loss_account_id"):
            dst_acc = self.state.get_dst("account.account", m2o_id(src_j.get(acc_f)))
            if dst_acc and m2o_id(dst_j.get(acc_f)) != dst_acc:
                updates[acc_f] = dst_acc

        if updates:
            self.dst.write("account.journal", [dst_jid], updates, context=dst_ctx)
            log.info(
                "Synced journal [%s] (%d) accounts: %s",
                src_j.get("code"),
                dst_jid,
                ", ".join(f"{k}={v}" for k, v in updates.items()),
            )

        dst_bank_acc = self.state.get_dst("res.partner.bank", m2o_id(src_j.get("bank_account_id")))
        if dst_bank_acc and m2o_id(dst_j.get("bank_account_id")) != dst_bank_acc:
            try:
                self.dst.write("account.journal", [dst_jid], {"bank_account_id": dst_bank_acc}, context=dst_ctx)
                log.info("Synced journal [%s] (%d) bank_account_id=%d", src_j.get("code"), dst_jid, dst_bank_acc)
            except Exception as exc:
                log.warning("Could not sync bank_account_id on journal [%s] (%d): %s", src_j.get("code"), dst_jid, exc)

    def _map_payment_method_lines(self, src_j: dict, dst_jid: int, src_ctx: dict, dst_ctx: dict):
        src_pml_ids = (src_j.get("inbound_payment_method_line_ids") or []) + (
            src_j.get("outbound_payment_method_line_ids") or []
        )
        if not src_pml_ids:
            return
        src_pmls = self.src.read(
            "account.payment.method.line",
            src_pml_ids,
            ["id", "name", "payment_type", "code", "payment_account_id"],
            context=src_ctx,
        )
        dst_pmls = self.dst.search_read(
            "account.payment.method.line",
            [["journal_id", "=", dst_jid]],
            ["id", "name", "payment_type", "code", "payment_account_id"],
            context=dst_ctx,
        )
        for sp in src_pmls:
            if self.state.get_dst("account.payment.method.line", sp["id"]):
                continue
            match = None
            for dp in dst_pmls:
                if dp.get("payment_type") == sp.get("payment_type") and (
                    dp.get("code") == sp.get("code") or dp.get("name") == sp.get("name")
                ):
                    match = dp
                    break
            if match:
                self.state.set_map("account.payment.method.line", sp["id"], match["id"])
                # Align outstanding payment account if set on source
                dst_pay_acc = self.state.get_dst("account.account", m2o_id(sp.get("payment_account_id")))
                if dst_pay_acc and m2o_id(match.get("payment_account_id")) != dst_pay_acc:
                    try:
                        self.dst.write(
                            "account.payment.method.line",
                            [match["id"]],
                            {"payment_account_id": dst_pay_acc},
                            context=dst_ctx,
                        )
                    except Exception:
                        pass

    # -------------------------------------------------------------------------
    # STEP 9: STANDALONE JOURNAL ENTRIES / INVOICES / BILLS (account.move)
    # -------------------------------------------------------------------------
    def migrate_moves(self, only_ids: Optional[List[int]] = None):
        log.info("=== Phase 9: Journal Entries, Invoices & Bills (account.move) ===")
        src_has_origin_payment = self.src.has_field("account.move", "origin_payment_id")
        src_has_payment_id = self.src.has_field("account.move", "payment_id")
        src_has_stmt_line = self.src.has_field("account.move", "statement_line_id")

        for src_cid in self.get_source_company_ids():
            dst_cid = self.state.get_dst("res.company", src_cid)
            if not dst_cid:
                continue
            src_ctx = self.src_company_ctx(src_cid)
            extra_ctx = {"check_move_validity": False, "skip_invoice_sync": True}
            if self.allow_legacy_import:
                extra_ctx["contractor_legacy_import"] = True
            dst_ctx = self.dst_company_ctx(src_cid, extra_ctx)

            domain: list = [["company_id", "=", src_cid]]
            if only_ids is not None:
                domain.append(["id", "in", only_ids])
            else:
                # Exclude moves owned by payments or bank statement lines (handled in Phases 10 & 11)
                if src_has_origin_payment:
                    domain.append(["origin_payment_id", "=", False])
                if src_has_payment_id:
                    domain.append(["payment_id", "=", False])
                if src_has_stmt_line:
                    domain.append(["statement_line_id", "=", False])

            move_ids = self.src.search(
                "account.move", domain, order="date asc, id asc", context=src_ctx
            )
            log.info("Company %d: %d standalone account.move records to process", src_cid, len(move_ids))
            if self.dry_run:
                continue

            move_fields = [
                "id", "name", "move_type", "state", "date", "ref", "narration",
                "journal_id", "company_id", "currency_id", "partner_id",
                "commercial_partner_id", "partner_bank_id", "partner_shipping_id",
                "invoice_date", "invoice_date_due", "invoice_origin", "invoice_payment_term_id",
                "fiscal_position_id", "payment_reference", "reversed_entry_id",
                "auto_post", "to_check", "checked", "line_ids",
            ]
            line_fields = [
                "id", "name", "sequence", "display_type", "account_id", "partner_id",
                "currency_id", "amount_currency", "debit", "credit", "date_maturity",
                "product_id", "product_uom_id", "quantity", "price_unit", "discount",
                "tax_ids", "tax_tag_ids", "tax_line_id", "tax_repartition_line_id",
                "analytic_distribution", "reconciled",
            ]

            for batch_ids in chunked(move_ids, BATCH_SIZE):
                pending_ids = [mid for mid in batch_ids if not self.state.get_dst("account.move", mid)]
                if not pending_ids:
                    continue
                moves = self.src.read("account.move", pending_ids, move_fields, context=src_ctx)
                for mv in moves:
                    self._copy_single_move(mv, line_fields, src_ctx, dst_ctx, dst_cid)

    def _build_move_line_vals(self, sl: dict) -> Dict[str, Any]:
        """Translate a source account.move.line dict into destination create/command values."""
        dt = sl.get("display_type") or "product"
        lvals: Dict[str, Any] = {
            "name": sl.get("name") or "/",
            "sequence": sl.get("sequence", 10),
            "display_type": dt,
        }
        if dt not in ("line_section", "line_subsection", "line_note"):
            dst_acc = self.state.get_dst("account.account", m2o_id(sl.get("account_id")))
            if not dst_acc:
                raise ValueError(f"Unmapped account {sl.get('account_id')} on line {sl['id']}")
            lvals.update(
                {
                    "account_id": dst_acc,
                    "partner_id": self.state.get_dst("res.partner", m2o_id(sl.get("partner_id"))) or False,
                    "currency_id": self.state.get_dst("res.currency", m2o_id(sl.get("currency_id"))) or False,
                    "amount_currency": sl.get("amount_currency", 0.0),
                    "debit": sl.get("debit", 0.0),
                    "credit": sl.get("credit", 0.0),
                    "date_maturity": sl.get("date_maturity") or False,
                    "product_id": self.state.get_dst("product.product", m2o_id(sl.get("product_id"))) or False,
                    "product_uom_id": self.state.get_dst("uom.uom", m2o_id(sl.get("product_uom_id"))) or False,
                    "quantity": sl.get("quantity", 1.0),
                    "price_unit": sl.get("price_unit", 0.0),
                    "discount": sl.get("discount", 0.0),
                    "tax_line_id": self.state.get_dst("account.tax", m2o_id(sl.get("tax_line_id"))) or False,
                    "tax_repartition_line_id": self.state.get_dst(
                        "account.tax.repartition.line", m2o_id(sl.get("tax_repartition_line_id"))
                    ) or False,
                    "analytic_distribution": self.remap_analytic_distribution(
                        sl.get("analytic_distribution")
                    ),
                }
            )
            tids = [self.state.get_dst("account.tax", t) for t in (sl.get("tax_ids") or [])]
            tids = [t for t in tids if t]
            if tids:
                lvals["tax_ids"] = [[6, 0, tids]]
            ttags = [
                self.state.get_dst("account.account.tag", t) for t in (sl.get("tax_tag_ids") or [])
            ]
            ttags = [t for t in ttags if t]
            if ttags:
                lvals["tax_tag_ids"] = [[6, 0, ttags]]
        return lvals

    def _copy_single_move(
        self, mv: dict, line_fields: List[str], src_ctx: dict, dst_ctx: dict, dst_cid: int
    ):
        src_mid = mv["id"]
        try:
            dst_jid = self.state.get_dst("account.journal", m2o_id(mv.get("journal_id")))
            if not dst_jid:
                raise ValueError(f"Unmapped journal {mv.get('journal_id')}")

            src_line_ids = mv.get("line_ids") or []
            src_lines = self.src.read("account.move.line", src_line_ids, line_fields, context=src_ctx)
            # Preserve exact creation order
            line_by_id = {l["id"]: l for l in src_lines}
            ordered_src_lines = [line_by_id[lid] for lid in src_line_ids if lid in line_by_id]

            line_cmds = [[0, 0, self._build_move_line_vals(sl)] for sl in ordered_src_lines]

            move_vals: Dict[str, Any] = {
                "move_type": mv.get("move_type", "entry"),
                "date": mv["date"],
                "ref": mv.get("ref") or False,
                "narration": mv.get("narration") or False,
                "journal_id": dst_jid,
                "company_id": dst_cid,
                "currency_id": self.state.get_dst("res.currency", m2o_id(mv.get("currency_id"))) or False,
                "partner_id": self.state.get_dst("res.partner", m2o_id(mv.get("partner_id"))) or False,
                "partner_bank_id": self.state.get_dst("res.partner.bank", m2o_id(mv.get("partner_bank_id"))) or False,
                "partner_shipping_id": self.state.get_dst("res.partner", m2o_id(mv.get("partner_shipping_id"))) or False,
                "invoice_date": mv.get("invoice_date") or False,
                "invoice_date_due": mv.get("invoice_date_due") or False,
                "invoice_origin": mv.get("invoice_origin") or False,
                "invoice_payment_term_id": self.state.get_dst(
                    "account.payment.term", m2o_id(mv.get("invoice_payment_term_id"))
                ) or False,
                "fiscal_position_id": self.state.get_dst(
                    "account.fiscal.position", m2o_id(mv.get("fiscal_position_id"))
                ) or False,
                "payment_reference": mv.get("payment_reference") or False,
                "reversed_entry_id": self.state.get_dst(
                    "account.move", m2o_id(mv.get("reversed_entry_id"))
                ) or False,
                "line_ids": line_cmds,
            }
            # Preserve exact Odoo 19 sequence name
            existing_mv = []
            if mv.get("name") and mv["name"] != "/":
                move_vals["name"] = mv["name"]
                candidates = self.dst.search_read(
                    "account.move",
                    [
                        ["name", "=", mv["name"]],
                        ["journal_id", "=", dst_jid],
                        ["company_id", "=", dst_cid],
                    ],
                    ["id", "state"],
                    context=dst_ctx,
                )
                existing_mv = [
                    c for c in candidates
                    if self.state.get_src("account.move", c["id"]) in (None, src_mid)
                ]
            if self.allow_legacy_import:
                move_vals["is_legacy_import"] = True

            if existing_mv:
                dst_mid = existing_mv[0]["id"]
                dst_state = existing_mv[0].get("state")
            else:
                dst_mid = self.dst.create("account.move", move_vals, context=dst_ctx)
                dst_state = "draft"

            # Transition to exact state (posted / cancel / draft)
            src_state = mv.get("state")
            if src_state == "posted" and dst_state == "draft":
                self.dst.call("account.move", "action_post", {"ids": [dst_mid]}, context=dst_ctx)
            elif src_state == "cancel" and dst_state == "draft":
                self.dst.call("account.move", "button_cancel", {"ids": [dst_mid]}, context=dst_ctx)

            # Guarantee exact invoice_date_due & line date_maturity (Odoo 20 recomputes due date from needed_terms on post)
            if mv.get("move_type") in (
                "out_invoice", "out_refund", "in_invoice", "in_refund", "out_receipt", "in_receipt"
            ) and mv.get("invoice_date_due"):
                src_due = mv["invoice_date_due"]
                cur_info = self.dst.read("account.move", [dst_mid], ["invoice_date_due", "state"], context=dst_ctx)
                if cur_info:
                    dst_due = cur_info[0].get("invoice_date_due")
                    cur_state = cur_info[0].get("state")
                    if str(dst_due or "") != str(src_due):
                        if cur_state == "posted":
                            self.dst.call("account.move", "button_draft", {"ids": [dst_mid]}, context=dst_ctx)
                        self.dst.write(
                            "account.move",
                            [dst_mid],
                            {"invoice_payment_term_id": False, "invoice_date_due": src_due},
                            context=dst_ctx,
                        )
                        dst_pt_lines = self.dst.search_read(
                            "account.move.line",
                            [["move_id", "=", dst_mid], ["display_type", "=", "payment_term"]],
                            ["id", "date_maturity"],
                            order="id asc",
                            context=dst_ctx,
                        )
                        src_pt_lines = [
                            sl for sl in ordered_src_lines
                            if (sl.get("display_type") or "product") == "payment_term"
                        ]
                        if len(src_pt_lines) == len(dst_pt_lines):
                            for s_pt, d_pt in zip(src_pt_lines, dst_pt_lines):
                                s_mat = s_pt.get("date_maturity") or src_due
                                if s_mat and str(d_pt.get("date_maturity") or "") != str(s_mat):
                                    self.dst.write(
                                        "account.move.line", [d_pt["id"]], {"date_maturity": s_mat}, context=dst_ctx
                                    )
                        else:
                            for d_pt in dst_pt_lines:
                                if str(d_pt.get("date_maturity") or "") != str(src_due):
                                    self.dst.write(
                                        "account.move.line", [d_pt["id"]], {"date_maturity": src_due}, context=dst_ctx
                                    )
                        if cur_state == "posted":
                            self.dst.call("account.move", "action_post", {"ids": [dst_mid]}, context=dst_ctx)

            # Map move and its lines 1-to-1
            self.state.set_map("account.move", src_mid, dst_mid)
            self._map_move_lines(src_mid, dst_mid, ordered_src_lines, dst_ctx)
            self.state.resolve_error("moves", "account.move", src_mid)
            log.info("Migrated move %s [%s] (%d -> %d)", mv.get("name"), src_state, src_mid, dst_mid)

        except Exception as exc:
            self.state.log_error("moves", "account.move", src_mid, str(exc))
            log.error("Failed migrating move %s (%d): %s", mv.get("name"), src_mid, exc)

    def _move_line_matches(self, sl: dict, dl: dict, require_partner: bool = False) -> bool:
        """Return True if a source move line matches a destination move line on account and amounts."""
        dt_s = sl.get("display_type") or "product"
        dt_d = dl.get("display_type") or "product"
        if dt_s in ("line_section", "line_subsection", "line_note") or dt_d in ("line_section", "line_subsection", "line_note"):
            return dt_s == dt_d and (sl.get("name") or "") == (dl.get("name") or "")
        t_acc = self.state.get_dst("account.account", m2o_id(sl.get("account_id")))
        if m2o_id(dl.get("account_id")) != t_acc:
            return False
        if abs((dl.get("debit") or 0.0) - (sl.get("debit") or 0.0)) >= 0.005:
            return False
        if abs((dl.get("credit") or 0.0) - (sl.get("credit") or 0.0)) >= 0.005:
            return False
        if require_partner:
            t_par = self.state.get_dst("res.partner", m2o_id(sl.get("partner_id")))
            if m2o_id(dl.get("partner_id")) != t_par:
                return False
        return True

    def _map_move_lines(
        self, src_mid: int, dst_mid: int, src_lines: List[dict], dst_ctx: dict
    ):
        dst_lines = self.dst.search_read(
            "account.move.line",
            [["move_id", "=", dst_mid]],
            ["id", "account_id", "partner_id", "debit", "credit", "display_type", "name"],
            order="id asc",
            context=dst_ctx,
        )
        # Fast path only when every positional pair matches account and amounts
        if len(src_lines) == len(dst_lines) and all(
            self._move_line_matches(sl, dl, require_partner=False)
            for sl, dl in zip(src_lines, dst_lines)
        ):
            for sl, dl in zip(src_lines, dst_lines):
                self.state.set_map("account.move.line", sl["id"], dl["id"])
            return

        # Deterministic multi-pass matching for reordered/auto-generated move lines
        used: Set[int] = set()
        for sl in src_lines:
            matched_dl = None
            for pass_idx in (1, 2, 3):
                for dl in dst_lines:
                    if dl["id"] in used:
                        continue
                    if pass_idx == 1:
                        if self._move_line_matches(sl, dl, require_partner=True) and (sl.get("name") or "") == (dl.get("name") or ""):
                            matched_dl = dl
                            break
                    elif pass_idx == 2:
                        if self._move_line_matches(sl, dl, require_partner=True):
                            matched_dl = dl
                            break
                    else:
                        if self._move_line_matches(sl, dl, require_partner=False):
                            matched_dl = dl
                            break
                if matched_dl:
                    self.state.set_map("account.move.line", sl["id"], matched_dl["id"])
                    used.add(matched_dl["id"])
                    break

    # -------------------------------------------------------------------------
    # STEP 10: PAYMENTS (account.payment)
    # -------------------------------------------------------------------------
    def migrate_payments(self, only_ids: Optional[List[int]] = None):
        log.info("=== Phase 10: Payments (account.payment) ===")
        dst_memo_field = "memo" if self.dst.has_field("account.payment", "memo") else "ref"
        src_memo_field = "memo" if self.src.has_field("account.payment", "memo") else "ref"
        dst_has_pay_workflow = self.dst.has_field("account.payment", "payment_workflow_state")

        for src_cid in self.get_source_company_ids():
            dst_cid = self.state.get_dst("res.company", src_cid)
            if not dst_cid:
                continue
            src_ctx = self.src_company_ctx(src_cid)
            dst_ctx = self.dst_company_ctx(src_cid)

            domain: list = [["company_id", "=", src_cid]]
            if only_ids is not None:
                domain.append(["id", "in", only_ids])

            pay_ids = self.src.search(
                "account.payment", domain, order="date asc, id asc", context=src_ctx
            )
            log.info("Company %d: %d account.payment records to process", src_cid, len(pay_ids))
            if self.dry_run:
                continue

            fields = [
                "id", "name", "state", "date", "amount", "currency_id",
                "payment_type", "partner_type", "partner_id", "journal_id",
                "payment_method_line_id", "destination_account_id",
                "partner_bank_id", "move_id", "is_internal_transfer",
                src_memo_field,
            ]

            for batch in chunked(pay_ids, BATCH_SIZE):
                pending = [pid for pid in batch if not self.state.get_dst("account.payment", pid)]
                if not pending:
                    continue
                payments = self.src.read("account.payment", pending, fields, context=src_ctx)
                for p in payments:
                    src_pid = p["id"]
                    try:
                        vals: Dict[str, Any] = {
                            "date": p["date"],
                            "amount": p["amount"],
                            "currency_id": self.state.get_dst("res.currency", m2o_id(p.get("currency_id"))) or False,
                            "payment_type": p["payment_type"],
                            "partner_type": p.get("partner_type") or "customer",
                            "partner_id": self.state.get_dst("res.partner", m2o_id(p.get("partner_id"))) or False,
                            "journal_id": self.state.get_dst("account.journal", m2o_id(p.get("journal_id"))),
                            "payment_method_line_id": self.state.get_dst(
                                "account.payment.method.line", m2o_id(p.get("payment_method_line_id"))
                            ) or False,
                            "destination_account_id": self.state.get_dst(
                                "account.account", m2o_id(p.get("destination_account_id"))
                            ) or False,
                            "partner_bank_id": self.state.get_dst(
                                "res.partner.bank", m2o_id(p.get("partner_bank_id"))
                            ) or False,
                            "company_id": dst_cid,
                            dst_memo_field: p.get(src_memo_field) or False,
                        }
                        if p.get("name") and p["name"] != "/":
                            vals["name"] = p["name"]
                        if "is_internal_transfer" in p:
                            vals["is_internal_transfer"] = p["is_internal_transfer"]

                        src_state = p.get("state")
                        if dst_has_pay_workflow and src_state in ("posted", "in_process", "paid", "reconciled"):
                            vals["payment_workflow_state"] = "released"

                        dst_pid = self.dst.create("account.payment", vals, context=dst_ctx)

                        if src_state in ("posted", "in_process", "paid", "reconciled"):
                            self.dst.call("account.payment", "action_post", {"ids": [dst_pid]}, context=dst_ctx)
                            if src_state in ("paid", "reconciled"):
                                try:
                                    self.dst.call(
                                        "account.payment", "action_validate", {"ids": [dst_pid]}, context=dst_ctx
                                    )
                                except Exception:
                                    pass
                        elif src_state == "rejected":
                            try:
                                self.dst.call("account.payment", "action_reject", {"ids": [dst_pid]}, context=dst_ctx)
                            except Exception:
                                self.dst.call("account.payment", "action_cancel", {"ids": [dst_pid]}, context=dst_ctx)
                        elif src_state in ("cancel", "canceled"):
                            self.dst.call("account.payment", "action_cancel", {"ids": [dst_pid]}, context=dst_ctx)

                        self.state.set_map("account.payment", src_pid, dst_pid)

                        # Map the linked account.move and its lines for reconciliation & attachments
                        src_mid = m2o_id(p.get("move_id"))
                        if src_mid:
                            dst_pay = self.dst.read("account.payment", [dst_pid], ["move_id"], context=dst_ctx)[0]
                            dst_mid = m2o_id(dst_pay.get("move_id"))
                            if dst_mid:
                                self.state.set_map("account.move", src_mid, dst_mid)
                                src_lines = self.src.search_read(
                                    "account.move.line",
                                    [["move_id", "=", src_mid]],
                                    ["id", "account_id", "partner_id", "debit", "credit", "display_type", "name"],
                                    order="id asc",
                                    context=src_ctx,
                                )
                                self._map_move_lines(src_mid, dst_mid, src_lines, dst_ctx)

                        self.state.resolve_error("payments", "account.payment", src_pid)
                        log.info("Migrated payment %s [%s] (%d -> %d)", p.get("name"), src_state, src_pid, dst_pid)
                    except Exception as exc:
                        self.state.log_error("payments", "account.payment", src_pid, str(exc))
                        log.error("Failed migrating payment %s (%d): %s", p.get("name"), src_pid, exc)

    # -------------------------------------------------------------------------
    # STEP 11: BANK STATEMENTS & STATEMENT LINES
    # -------------------------------------------------------------------------
    def migrate_bank_statements(self):
        log.info("=== Phase 11: Bank Statements & Statement Lines ===")
        src_ml_fields = [
            "id", "move_id", "name", "sequence", "display_type", "account_id", "partner_id",
            "currency_id", "amount_currency", "debit", "credit", "date_maturity",
            "product_id", "product_uom_id", "quantity", "price_unit", "discount",
            "tax_ids", "tax_tag_ids", "tax_line_id", "tax_repartition_line_id",
            "analytic_distribution", "reconciled",
        ]
        for src_cid in self.get_source_company_ids():
            dst_cid = self.state.get_dst("res.company", src_cid)
            if not dst_cid:
                continue
            src_ctx = self.src_company_ctx(src_cid)
            dst_ctx = self.dst_company_ctx(src_cid)

            # 11.1 Bank Statements
            stmts = self.src.search_read(
                "account.bank.statement",
                [["company_id", "=", src_cid]],
                ["id", "name", "reference", "date", "balance_start", "balance_end_real", "journal_id"],
                order="date asc, id asc",
                context=src_ctx,
            )
            for st in stmts:
                if self.state.get_dst("account.bank.statement", st["id"]) or self.dry_run:
                    continue
                try:
                    vals = {
                        "name": st.get("name") or False,
                        "reference": st.get("reference") or False,
                        "date": st.get("date") or False,
                        "balance_start": st.get("balance_start", 0.0),
                        "balance_end_real": st.get("balance_end_real", 0.0),
                        "journal_id": self.state.get_dst("account.journal", m2o_id(st.get("journal_id"))),
                        "company_id": dst_cid,
                    }
                    dst_stid = self.dst.create("account.bank.statement", vals, context=dst_ctx)
                    self.state.set_map("account.bank.statement", st["id"], dst_stid)
                except Exception as exc:
                    self.state.log_error("statements", "account.bank.statement", st["id"], str(exc))

            # 11.2 Bank Statement Lines
            st_line_ids = self.src.search(
                "account.bank.statement.line",
                [["company_id", "=", src_cid]],
                order="date asc, id asc",
                context=src_ctx,
            )
            log.info("Company %d: %d bank statement lines", src_cid, len(st_line_ids))
            if self.dry_run:
                continue

            fields = [
                "id", "date", "payment_ref", "ref", "amount", "amount_currency",
                "foreign_currency_id", "partner_id", "partner_name", "account_number",
                "journal_id", "statement_id", "move_id", "transaction_type", "is_reconciled",
            ]
            for batch in chunked(st_line_ids, BATCH_SIZE):
                pending = [i for i in batch if not self.state.get_dst("account.bank.statement.line", i)]
                if not pending:
                    continue
                lines = self.src.read("account.bank.statement.line", pending, fields, context=src_ctx)
                batch_src_mids = sorted({m2o_id(sl.get("move_id")) for sl in lines if m2o_id(sl.get("move_id"))})
                src_moves_by_id: Dict[int, Dict[str, Any]] = {}
                src_lines_by_move: Dict[int, List[Dict[str, Any]]] = {}
                if batch_src_mids:
                    for mv in self.src.read(
                        "account.move",
                        batch_src_mids,
                        ["id", "name", "state", "narration"],
                        context=src_ctx,
                    ):
                        src_moves_by_id[mv["id"]] = mv
                    for ml in self.src.search_read(
                        "account.move.line",
                        [["move_id", "in", batch_src_mids]],
                        src_ml_fields,
                        order="move_id asc, id asc",
                        context=src_ctx,
                    ):
                        mid = m2o_id(ml.get("move_id"))
                        if mid:
                            src_lines_by_move.setdefault(mid, []).append(ml)

                for sl in lines:
                    src_slid = sl["id"]
                    dst_slid: Optional[int] = None
                    try:
                        dst_jid = self.state.get_dst("account.journal", m2o_id(sl.get("journal_id")))
                        src_mid = m2o_id(sl.get("move_id"))
                        src_move = src_moves_by_id.get(src_mid) or {} if src_mid else {}
                        src_ml = src_lines_by_move.get(src_mid, []) if src_mid else []
                        vals: Dict[str, Any] = {
                            "date": sl["date"],
                            "payment_ref": sl.get("payment_ref") or "/",
                            "ref": sl.get("ref") or False,
                            "amount": sl["amount"],
                            "amount_currency": sl.get("amount_currency", 0.0),
                            "foreign_currency_id": self.state.get_dst(
                                "res.currency", m2o_id(sl.get("foreign_currency_id"))
                            ) or False,
                            "partner_id": self.state.get_dst("res.partner", m2o_id(sl.get("partner_id"))) or False,
                            "partner_name": sl.get("partner_name") or False,
                            "account_number": sl.get("account_number") or False,
                            "journal_id": dst_jid,
                            "statement_id": self.state.get_dst(
                                "account.bank.statement", m2o_id(sl.get("statement_id"))
                            ) or False,
                            "transaction_type": sl.get("transaction_type") or False,
                        }
                        if src_ml:
                            vals["line_ids"] = [[0, 0, self._build_move_line_vals(ml)] for ml in src_ml]
                        if self.allow_legacy_import:
                            vals["is_legacy_import"] = True

                        st_extra: Dict[str, Any] = {"from_statement_view": True, "skip_invoice_sync": True}
                        if self.allow_legacy_import:
                            st_extra["contractor_legacy_import"] = True
                        st_create_ctx = self.dst_company_ctx(src_cid, st_extra)

                        dst_slid = self.dst.create("account.bank.statement.line", vals, context=st_create_ctx)

                        if src_mid:
                            dst_sl = self.dst.read(
                                "account.bank.statement.line", [dst_slid], ["move_id"], context=dst_ctx
                            )[0]
                            dst_mid = m2o_id(dst_sl.get("move_id"))
                            if dst_mid:
                                move_upd: Dict[str, Any] = {}
                                if src_move.get("name") and src_move["name"] != "/":
                                    move_upd["name"] = src_move["name"]
                                if src_move.get("narration"):
                                    move_upd["narration"] = src_move["narration"]
                                if move_upd:
                                    self.dst.write("account.move", [dst_mid], move_upd, context=st_create_ctx)

                                src_state = src_move.get("state", "posted")
                                if src_state == "posted":
                                    self.dst.call("account.move", "action_post", {"ids": [dst_mid]}, context=st_create_ctx)
                                elif src_state == "cancel":
                                    self.dst.call("account.move", "button_cancel", {"ids": [dst_mid]}, context=st_create_ctx)

                                self.state.set_map("account.move", src_mid, dst_mid)
                                self._map_move_lines(src_mid, dst_mid, src_ml, dst_ctx)

                        self.state.set_map("account.bank.statement.line", src_slid, dst_slid)
                        self.state.resolve_error("statements", "account.bank.statement.line", src_slid)
                    except Exception as exc:
                        if dst_slid:
                            try:
                                self.dst.call("account.bank.statement.line", "unlink", {"ids": [dst_slid]}, context=dst_ctx)
                            except Exception as cleanup_exc:
                                log.warning("Could not clean up failed statement line %d: %s", dst_slid, cleanup_exc)
                        self.state.log_error("statements", "account.bank.statement.line", src_slid, str(exc))
                        log.error("Failed statement line %d: %s", src_slid, exc)

    # -------------------------------------------------------------------------
    # STEP 12: RECONCILIATIONS (account.partial.reconcile & account.full.reconcile)
    # -------------------------------------------------------------------------
    def migrate_reconciliations(self):
        log.info("=== Phase 12: Reconciliations (account.partial.reconcile) ===")
        for src_cid in self.get_source_company_ids():
            dst_cid = self.state.get_dst("res.company", src_cid)
            if not dst_cid:
                continue
            src_ctx = self.src_company_ctx(src_cid)
            rec_ctx = self.dst_company_ctx(
                src_cid, {"no_exchange_difference": True, "no_cash_basis": True}
            )

            rec_ids = self.src.search(
                "account.partial.reconcile",
                [["company_id", "=", src_cid]],
                order="id asc",
                context=src_ctx,
            )
            log.info("Company %d: %d partial reconciliations", src_cid, len(rec_ids))
            if self.dry_run:
                continue

            pr_fields = [
                "id", "debit_move_id", "credit_move_id", "amount",
                "debit_amount_currency", "credit_amount_currency",
                "debit_currency_id", "credit_currency_id", "full_reconcile_id",
            ]
            src_full_rec_ids: Set[int] = set()
            for batch in chunked(rec_ids, BATCH_SIZE):
                recs = self.src.read(
                    "account.partial.reconcile",
                    batch,
                    pr_fields,
                    context=src_ctx,
                )
                for r in recs:
                    src_rid = r["id"]
                    fr_id = m2o_id(r.get("full_reconcile_id"))
                    if fr_id:
                        src_full_rec_ids.add(fr_id)
                    if self.state.get_dst("account.partial.reconcile", src_rid):
                        continue
                    dst_debit = self.state.get_dst("account.move.line", m2o_id(r.get("debit_move_id")))
                    dst_credit = self.state.get_dst("account.move.line", m2o_id(r.get("credit_move_id")))
                    if not dst_debit or not dst_credit:
                        continue
                    try:
                        # Check if exact partial reconciliation already exists (idempotent re-run)
                        existing_prs = self.dst.search_read(
                            "account.partial.reconcile",
                            [["debit_move_id", "=", dst_debit], ["credit_move_id", "=", dst_credit]],
                            ["id", "amount"],
                            context=rec_ctx,
                        )
                        target_amt = r.get("amount") or 0.0
                        matched_pr = next(
                            (ex for ex in existing_prs if abs((ex.get("amount") or 0.0) - target_amt) < 0.005),
                            None,
                        )
                        if matched_pr:
                            self.state.set_map("account.partial.reconcile", src_rid, matched_pr["id"])
                            self.state.resolve_error("reconcile", "account.partial.reconcile", src_rid)
                            continue

                        pr_vals: Dict[str, Any] = {
                            "debit_move_id": dst_debit,
                            "credit_move_id": dst_credit,
                            "amount": target_amt,
                            "debit_amount_currency": r.get("debit_amount_currency", 0.0),
                            "credit_amount_currency": r.get("credit_amount_currency", 0.0),
                        }
                        dst_d_curr = self.state.get_dst("res.currency", m2o_id(r.get("debit_currency_id")))
                        dst_c_curr = self.state.get_dst("res.currency", m2o_id(r.get("credit_currency_id")))
                        if dst_d_curr:
                            pr_vals["debit_currency_id"] = dst_d_curr
                        if dst_c_curr:
                            pr_vals["credit_currency_id"] = dst_c_curr

                        dst_pr_id = self.dst.create("account.partial.reconcile", pr_vals, context=rec_ctx)
                        self.state.set_map("account.partial.reconcile", src_rid, dst_pr_id)
                        self.state.resolve_error("reconcile", "account.partial.reconcile", src_rid)
                    except Exception as exc:
                        self.state.log_error("reconcile", "account.partial.reconcile", src_rid, str(exc))
                        log.warning("Reconcile %d skipped: %s", src_rid, exc)

            # Create matching account.full.reconcile groups for fully reconciled chains
            pending_frs = [
                fid for fid in sorted(src_full_rec_ids)
                if not self.state.get_dst("account.full.reconcile", fid)
            ]
            for fbatch in chunked(pending_frs, BATCH_SIZE):
                full_recs = self.src.read(
                    "account.full.reconcile",
                    fbatch,
                    ["id", "partial_reconcile_ids", "reconciled_line_ids"],
                    context=src_ctx,
                )
                for fr in full_recs:
                    src_frid = fr["id"]
                    dst_prs = [
                        self.state.get_dst("account.partial.reconcile", pid)
                        for pid in (fr.get("partial_reconcile_ids") or [])
                    ]
                    dst_lines = [
                        self.state.get_dst("account.move.line", lid)
                        for lid in (fr.get("reconciled_line_ids") or [])
                    ]
                    if not dst_prs or not dst_lines or any(p is None for p in dst_prs) or any(l is None for l in dst_lines):
                        continue
                    try:
                        pr_rows = self.dst.read(
                            "account.partial.reconcile",
                            dst_prs,
                            ["id", "full_reconcile_id"],
                            context=rec_ctx,
                        )
                        existing_fr = next(
                            (m2o_id(row.get("full_reconcile_id")) for row in pr_rows if m2o_id(row.get("full_reconcile_id"))),
                            None,
                        )
                        if existing_fr:
                            self.state.set_map("account.full.reconcile", src_frid, existing_fr)
                            continue
                        dst_frid = self.dst.create(
                            "account.full.reconcile",
                            {
                                "partial_reconcile_ids": [[6, 0, dst_prs]],
                                "reconciled_line_ids": [[6, 0, dst_lines]],
                            },
                            context=rec_ctx,
                        )
                        self.state.set_map("account.full.reconcile", src_frid, dst_frid)
                    except Exception as exc:
                        log.warning("Full reconcile %d skipped: %s", src_frid, exc)

    # -------------------------------------------------------------------------
    # STEP 13: ATTACHMENTS (ir.attachment) & CHATTER NOTES (mail.message)
    # -------------------------------------------------------------------------
    def migrate_attachments(self, only_models: Optional[List[str]] = None):
        if self.skip_attachments:
            return
        log.info("=== Phase 13: Attachments (ir.attachment) ===")
        src_bin_field = "datas" if self.src.has_field("ir.attachment", "datas") else "raw"
        dst_bin_field = "raw" if self.dst.has_field("ir.attachment", "raw") else "datas"
        target_models = only_models or [
            "account.move",
            "account.payment",
            "account.bank.statement",
            "account.bank.statement.line",
            "account.journal",
            "res.partner",
        ]

        for model in target_models:
            id_map = self.state.get_all_map(model)
            if not id_map:
                continue
            src_res_ids = list(id_map.keys())
            total_copied = 0

            for res_batch in chunked(src_res_ids, 200):
                att_ids = self.src.search(
                    "ir.attachment",
                    [
                        ["res_model", "=", model],
                        ["res_id", "in", res_batch],
                        ["res_field", "=", False],
                    ],
                    order="id asc",
                    context=self.src_ctx_all,
                )
                pending_atts = [aid for aid in att_ids if not self.state.get_dst("ir.attachment", aid)]
                if not pending_atts:
                    continue
                if self.dry_run:
                    total_copied += len(pending_atts)
                    continue

                # Fetch small batches because binary content contains base64 file payloads
                for att_batch in chunked(pending_atts, ATTACHMENT_BATCH_SIZE):
                    atts = self.src.read(
                        "ir.attachment",
                        att_batch,
                        ["id", "name", "type", src_bin_field, "url", "mimetype", "description", "res_model", "res_id", "company_id"],
                        context=self.src_ctx_all,
                    )
                    for a in atts:
                        src_aid = a["id"]
                        dst_res_id = id_map.get(a["res_id"])
                        if not dst_res_id:
                            continue
                        try:
                            src_cid = m2o_id(a.get("company_id"))
                            vals = {
                                "name": a["name"],
                                "type": a.get("type", "binary"),
                                "res_model": model,
                                "res_id": dst_res_id,
                                "mimetype": a.get("mimetype") or False,
                                "description": a.get("description") or False,
                                "company_id": self.state.get_dst("res.company", src_cid) if src_cid else False,
                            }
                            if a.get("type") == "url":
                                vals["url"] = a.get("url") or False
                            else:
                                bin_val = a.get(src_bin_field)
                                if isinstance(bin_val, dict):
                                    bin_val = bin_val.get("content") or False
                                vals[dst_bin_field] = bin_val or False

                            dst_aid = self.dst.create("ir.attachment", vals, context=self.dst_ctx_quiet)
                            self.state.set_map("ir.attachment", src_aid, dst_aid)
                            self.state.resolve_error("attachments", "ir.attachment", src_aid)
                            total_copied += 1
                        except Exception as exc:
                            self.state.log_error("attachments", "ir.attachment", src_aid, str(exc))
                            log.error("Failed attachment %s (%d): %s", a.get("name"), src_aid, exc)

            log.info("Model %s: %d attachments migrated", model, total_copied)

    def migrate_chatter(self):
        if self.skip_chatter:
            return
        log.info("=== Phase 14: Chatter Messages (mail.message) ===")
        for model in ("account.move", "account.payment", "account.bank.statement"):
            id_map = self.state.get_all_map(model)
            if not id_map:
                continue
            for res_batch in chunked(list(id_map.keys()), 200):
                msg_ids = self.src.search(
                    "mail.message",
                    [
                        ["model", "=", model],
                        ["res_id", "in", res_batch],
                        ["message_type", "in", ["comment", "email"]],
                    ],
                    order="date asc, id asc",
                    context=self.src_ctx_all,
                )
                pending = [mid for mid in msg_ids if not self.state.get_dst("mail.message", mid)]
                if not pending or self.dry_run:
                    continue
                for mb in chunked(pending, BATCH_SIZE):
                    msgs = self.src.read(
                        "mail.message",
                        mb,
                        ["id", "subject", "body", "date", "message_type", "model", "res_id", "author_id", "attachment_ids"],
                        context=self.src_ctx_all,
                    )
                    for m in msgs:
                        dst_res_id = id_map.get(m["res_id"])
                        if not dst_res_id:
                            continue
                        try:
                            att_ids = [
                                self.state.get_dst("ir.attachment", aid)
                                for aid in (m.get("attachment_ids") or [])
                            ]
                            vals = {
                                "model": model,
                                "res_id": dst_res_id,
                                "subject": m.get("subject") or False,
                                "body": m.get("body") or "",
                                "date": m.get("date") or False,
                                "message_type": m.get("message_type") or "comment",
                                "author_id": self.state.get_dst("res.partner", m2o_id(m.get("author_id"))) or False,
                            }
                            if any(att_ids):
                                vals["attachment_ids"] = [[6, 0, [a for a in att_ids if a]]]
                            dst_mid = self.dst.create("mail.message", vals, context=self.dst_ctx_quiet)
                            self.state.set_map("mail.message", m["id"], dst_mid)
                        except Exception as exc:
                            self.state.log_error("chatter", "mail.message", m["id"], str(exc))

    # -------------------------------------------------------------------------
    # RUNNER
    # -------------------------------------------------------------------------
    def run(self, retry_errors: bool = False):
        v19 = self.src.check_connection()
        v20 = self.dst.check_connection()
        log.info(
            "Connected -> Source: %s | Destination: %s",
            v19.get("version") or v19.get("server_version"),
            v20.get("version") or v20.get("server_version"),
        )

        if retry_errors:
            self._ensure_bank_accounts_trusted()
            errs = self.state.get_unresolved_errors()
            log.info("Retrying %d previously failed records...", len(errs))
            failed_moves = [e[2] for e in errs if e[1] == "account.move"]
            failed_pays = [e[2] for e in errs if e[1] == "account.payment"]
            failed_stmts = [e[2] for e in errs if e[1] in ("account.bank.statement", "account.bank.statement.line")]
            failed_users_hr = [e for e in errs if e[0] in ("users", "hr_master", "employees", "contracts")]
            failed_emp_docs = [e for e in errs if e[0] == "employee_documents"]
            if self.only_employee_documents:
                self.migrate_companies()
                self.migrate_employee_documents_and_attachments()
            else:
                if not self.skip_users_hr and (failed_users_hr or self.only_users_hr):
                    self.migrate_users()
                    self.migrate_hr_master_data()
                    self.migrate_employees_and_contracts()
                if not self.skip_employee_documents and (failed_emp_docs or self.only_users_hr):
                    self.migrate_employee_documents_and_attachments()
                if not self.only_users_hr:
                    if failed_pays or failed_stmts:
                        self.migrate_journals()
                    if failed_moves:
                        self.migrate_moves(only_ids=failed_moves)
                    if failed_pays:
                        self.migrate_payments(only_ids=failed_pays)
                    if failed_stmts:
                        self.migrate_bank_statements()
                    self.migrate_reconciliations()
                    self.migrate_attachments()
                    self.migrate_chatter()
            unresolved = self.state.get_unresolved_errors()
            if unresolved:
                log.warning(
                    "Retry finished with %d unresolved errors. Inspect '%s' or re-run with --retry-errors.",
                    len(unresolved),
                    self.state.db_path,
                )
            else:
                log.info("Retry completed cleanly with 0 errors!")
            return

        # Master data phases
        self.migrate_companies()

        if self.only_employee_documents:
            self.migrate_employee_documents_and_attachments()
            unresolved = self.state.get_unresolved_errors()
            doc_errs = [e for e in unresolved if e[0] == "employee_documents"]
            if doc_errs:
                log.warning(
                    "Employee documents migration finished with %d unresolved error(s). Inspect '%s'.",
                    len(doc_errs),
                    self.state.db_path,
                )
            else:
                log.info("Employee documents migration completed cleanly with 0 errors!")
            return

        self.migrate_geo_and_tags()
        self.migrate_analytics()

        if self.only_users_hr:
            self.migrate_users()
            self.migrate_hr_master_data()
            self.migrate_employees_and_contracts()
            self.migrate_employee_documents_and_attachments()
            unresolved = self.state.get_unresolved_errors()
            hr_errs = [e for e in unresolved if e[0] in ("users", "hr_master", "employees", "contracts", "partners", "employee_documents", "attachments")]
            if hr_errs:
                log.warning(
                    "Users & HR migration finished with %d unresolved error(s). Inspect '%s'.",
                    len(hr_errs),
                    self.state.db_path,
                )
            else:
                log.info("Users & HR migration completed cleanly with 0 errors!")
            return

        self.migrate_accounts()
        self.migrate_taxes_and_terms()

        if not self.skip_users_hr:
            self.migrate_users()
            self.migrate_hr_master_data()
            self.migrate_employees_and_contracts()
        self.migrate_employee_documents_and_attachments()

        used_partners, used_products = self.collect_used_partner_and_product_ids()
        self.migrate_partners(used_partners)
        self.migrate_products(used_products)
        self.migrate_journals()
        self._ensure_bank_accounts_trusted()

        # Transaction & attachment phases
        self.migrate_moves()
        self.migrate_payments()
        self.migrate_bank_statements()
        self.migrate_reconciliations()
        self.migrate_attachments()
        self.migrate_chatter()

        unresolved = self.state.get_unresolved_errors()
        if unresolved:
            log.warning(
                "Migration finished with %d unresolved errors. Inspect '%s' or re-run with --retry-errors.",
                len(unresolved),
                self.state.db_path,
            )
        else:
            log.info("Migration completed cleanly with 0 errors!")


# =============================================================================
# CLI ENTRYPOINT
# =============================================================================
def main():
    parser = argparse.ArgumentParser(description="Copy accounting, users, and HR/contracts data from Odoo 19 to Odoo 20.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_PATH), help="Path to git-ignored .env credential file.")
    parser.add_argument("--dry-run", action="store_true", help="Check connections and missing master data without writing.")
    parser.add_argument("--reset", action="store_true", help="Delete local SQLite state and start fresh.")
    parser.add_argument("--retry-errors", action="store_true", help="Retry only failed records from previous run.")
    parser.add_argument("--company-id", type=int, default=None, help="Source res.company ID to migrate (default: all).")
    parser.add_argument("--skip-attachments", action="store_true", help="Skip ir.attachment migration.")
    parser.add_argument("--skip-chatter", action="store_true", help="Skip mail.message chatter migration.")
    parser.add_argument(
        "--only-users-hr",
        action="store_true",
        help="Only migrate Companies, Geo, Analytics, Users (permissions & passwords, ignoring admin id=2), HR Master Data, Employees (with pictures) and Contracts (hr.version).",
    )
    parser.add_argument(
        "--skip-users-hr",
        action="store_true",
        help="Skip Users and HR/Employees/Contracts migration during full run.",
    )
    parser.add_argument(
        "--only-employee-documents",
        action="store_true",
        help="Copy only employee attachments (ir.attachment), Documents app files (documents.document), and resume/certification lines (hr.resume.line) onto migrated Odoo 20 employees.",
    )
    parser.add_argument(
        "--skip-employee-documents",
        action="store_true",
        help="Skip employee attachments, Documents app files, and resume/certification lines migration.",
    )
    parser.add_argument(
        "--passwords-file",
        default=str(DEFAULT_PASSWORDS_PATH),
        help="Path to git-ignored JSON cache of non-admin source password hashes (portable across servers).",
    )
    parser.add_argument(
        "--export-passwords",
        action="store_true",
        help="Read non-admin ($pbkdf2) user password hashes from local/DSN Odoo 19 PostgreSQL into --passwords-file and SQLite state, then exit.",
    )
    parser.add_argument(
        "--default-password",
        default="",
        help="Optional fallback plaintext password for active migrated users when direct hash write is not available.",
    )
    parser.add_argument(
        "--strict-project",
        action="store_true",
        help="Enforce Contractor ERP mandatory Project Code constraint even on migrated historical moves (default: mark migrated moves as legacy imports so old data imports without Project Code).",
    )
    parser.add_argument("--state-db", default=STATE_DB_PATH, help="Path to SQLite resume DB.")
    args = parser.parse_args()

    state = StateStore(args.state_db)
    if args.reset:
        log.warning("Resetting state database: %s", args.state_db)
        state.reset()

    if args.export_passwords:
        src_pg_dsn = os.environ.get("ODOO19_PG_DSN", "")
        src_db = os.environ.get("ODOO19_DB", "maxwell19")
        if Path(args.env_file).exists():
            src_cfg_tmp, _ = load_credentials(Path(args.env_file))
            src_pg_dsn = src_pg_dsn or src_cfg_tmp.get("pg_dsn", "")
            src_db = src_cfg_tmp.get("db", src_db)
        rows = AccountingMigrator._query_pg_user_passwords(src_pg_dsn, src_db)
        for uid, login, pw_hash in rows:
            state.save_user_password(uid, login, pw_hash)
        out_path = Path(args.passwords_file)
        AccountingMigrator.save_passwords_json_file(out_path, rows, source_db=src_db)
        log.info("Exported %d non-admin user password hash(es) from DB '%s' -> %s (and SQLite state).", len(rows), src_db, out_path)
        return

    src_cfg, dst_cfg = load_credentials(Path(args.env_file))

    src = OdooJSON2Client(src_cfg["url"], src_cfg["db"], src_cfg["api_key"], label="Odoo19")
    dst = OdooJSON2Client(dst_cfg["url"], dst_cfg["db"], dst_cfg["api_key"], label="Odoo20")

    migrator = AccountingMigrator(
        src=src,
        dst=dst,
        state=state,
        dry_run=args.dry_run,
        only_company_id=args.company_id,
        skip_attachments=args.skip_attachments,
        skip_chatter=args.skip_chatter,
        allow_legacy_import=not args.strict_project,
        src_pg_dsn=src_cfg.get("pg_dsn", ""),
        dst_pg_dsn=dst_cfg.get("pg_dsn", ""),
        passwords_file=Path(args.passwords_file) if args.passwords_file else None,
        default_user_password=args.default_password or dst_cfg.get("default_user_password", ""),
        skip_users_hr=args.skip_users_hr,
        only_users_hr=args.only_users_hr,
        skip_employee_documents=args.skip_employee_documents,
        only_employee_documents=args.only_employee_documents,
    )
    migrator.run(retry_errors=args.retry_errors)

if __name__ == "__main__":
    main()
