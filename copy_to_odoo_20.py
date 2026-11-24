#!/usr/bin/env python3
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

# =============================================================================
# PATHS & CREDENTIAL LOADER (.env is ignored by .gitignore)
# =============================================================================
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = SCRIPT_DIR / ".env"
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
    }
    dst_cfg = {
        "url": values["ODOO20_URL"],
        "db": values["ODOO20_DB"],
        "api_key": values["ODOO20_API_KEY"],
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

    def fields_get(self, model: str) -> Dict[str, Any]:
        if model not in self._fields_cache:
            try:
                self._fields_cache[model] = self.call(
                    model,
                    "fields_get",
                    {"attributes": ["type", "relation", "readonly", "store", "required"]},
                )
            except Exception:
                self._fields_cache[model] = {}
        return self._fields_cache[model]

    def has_field(self, model: str, field_name: str) -> bool:
        return field_name in self.fields_get(model)

    def filter_existing_fields(self, model: str, fields: List[str]) -> List[str]:
        meta = self.fields_get(model)
        return [f for f in fields if f in meta]

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
        # Strip fields that do not exist on target Odoo 20 model
        meta = self.fields_get(model)
        clean_vals = {k: v for k, v in vals.items() if k in meta}
        res = self.call(model, "create", {"vals_list": [clean_vals]}, context=context)
        return res[0] if isinstance(res, list) else int(res)

    def write(self, model: str, ids: List[int], vals: Dict[str, Any], context: Optional[dict] = None):
        meta = self.fields_get(model)
        clean_vals = {k: v for k, v in vals.items() if k in meta}
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
    ):
        self.src = src
        self.dst = dst
        self.state = state
        self.dry_run = dry_run
        self.only_company_id = only_company_id
        self.skip_attachments = skip_attachments
        self.skip_chatter = skip_chatter
        self.allow_legacy_import = allow_legacy_import

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
    def migrate_attachments(self):
        if self.skip_attachments:
            return
        log.info("=== Phase 13: Attachments (ir.attachment) ===")
        src_bin_field = "datas" if self.src.has_field("ir.attachment", "datas") else "raw"
        dst_bin_field = "raw" if self.dst.has_field("ir.attachment", "raw") else "datas"
        target_models = [
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
        self.migrate_geo_and_tags()
        self.migrate_accounts()
        self.migrate_taxes_and_terms()
        self.migrate_analytics()

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
    parser = argparse.ArgumentParser(description="Copy all accounting data from Odoo 19 to Odoo 20.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_PATH), help="Path to git-ignored .env credential file.")
    parser.add_argument("--dry-run", action="store_true", help="Check connections and missing master data without writing.")
    parser.add_argument("--reset", action="store_true", help="Delete local SQLite state and start fresh.")
    parser.add_argument("--retry-errors", action="store_true", help="Retry only failed records from previous run.")
    parser.add_argument("--company-id", type=int, default=None, help="Source res.company ID to migrate (default: all).")
    parser.add_argument("--skip-attachments", action="store_true", help="Skip ir.attachment migration.")
    parser.add_argument("--skip-chatter", action="store_true", help="Skip mail.message chatter migration.")
    parser.add_argument(
        "--strict-project",
        action="store_true",
        help="Enforce Contractor ERP mandatory Project Code constraint even on migrated historical moves (default: mark migrated moves as legacy imports so old data imports without Project Code).",
    )
    parser.add_argument("--state-db", default=STATE_DB_PATH, help="Path to SQLite resume DB.")
    args = parser.parse_args()

    src_cfg, dst_cfg = load_credentials(Path(args.env_file))

    state = StateStore(args.state_db)
    if args.reset:
        log.warning("Resetting state database: %s", args.state_db)
        state.reset()

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
    )
    migrator.run(retry_errors=args.retry_errors)

if __name__ == "__main__":
    main()
