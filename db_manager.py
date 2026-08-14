import hashlib
import os
import re
import threading
import time
from pathlib import Path
from config import DB_PATH, get_logger
import duckdb

logger = get_logger("db_manager")

# =============================================================================
# Optional display-language for the handful of user-facing portfolio messages
# this module returns (add_owned_stock / record_sale). Deliberately isolated
# here rather than importing anything from app_gui.py: this module is also
# used by the unattended CLI pipeline (export_json.py / publish.py), which
# never calls set_language(), so it always defaults to "EN" there and this
# change cannot affect the website/CLI pipeline in any way. app_gui.py calls
# set_language() to keep these messages in sync with the desktop UI's own
# language toggle.
# =============================================================================
_LANG = "EN"


def set_language(lang):
    global _LANG
    _LANG = lang if lang == "AR" else "EN"

def clean_sector_name(sec: str) -> str:
    if not sec or str(sec).strip() in ('', 'nan', 'None', 'N/A', 'NULL'):
        return "General / Diversified"
        
    s = str(sec).strip()
    s = re.sub(r'\s+,\s+', ', ', s)
    s = s.replace(" and ", " & ").replace(" And ", " & ")
    s = s.replace("Textile &", "Textiles &")
    s = s.replace("Non-bank", "Non-Bank").replace("financial services", "Financial Services")
    s = s.replace("Contracting & Construction", "Construction")
    
    s_lower = s.lower()
    if s_lower in ["non-bank financial services", "non bank financial services", "financial services"]:
        return "Non-Bank Financial Services"
    if "food" in s_lower and "beverag" in s_lower:
        return "Food, Beverages & Tobacco"
    if "textile" in s_lower or "durables" in s_lower:
        return "Textiles & Durables"
    if "it," in s_lower or "communication" in s_lower or "media" in s_lower or "technology" in s_lower:
        return "IT, Media & Communication Services"
    if "industrial goods" in s_lower or "automobiles" in s_lower:
        return "Industrial Goods, Services & Automobiles"
    if "construction" in s_lower or "contracting" in s_lower or "engineering" in s_lower:
        return "Construction & Engineering"
    if "health" in s_lower or "pharma" in s_lower or "medical" in s_lower:
        return "Health Care & Pharmaceuticals"
    if "basic resources" in s_lower or "mining" in s_lower or "steel" in s_lower:
        return "Basic Resources"
    if "building materials" in s_lower or "cement" in s_lower or "porcelain" in s_lower:
        return "Building Materials"
    if "travel" in s_lower or "leisure" in s_lower or "hotels" in s_lower or "tourism" in s_lower:
        return "Travel & Leisure"
    if "shipping" in s_lower or "transportation" in s_lower or "cargo" in s_lower:
        return "Shipping & Transportation Services"
    if "trade" in s_lower or "distributor" in s_lower:
        return "Trade & Distributors"
    if "energy" in s_lower or "support services" in s_lower or "oil" in s_lower:
        return "Energy & Support Services"
    if "education" in s_lower:
        return "Education Services"
    if "paper" in s_lower or "packaging" in s_lower:
        return "Paper & Packaging"
    if "banks" in s_lower or "banking" in s_lower:
        return "Banks"
    if "real estate" in s_lower or "housing" in s_lower or "development" in s_lower:
        return "Real Estate"
    if "chemical" in s_lower or "fertilizer" in s_lower:
        return "Chemicals"
    if "utilities" in s_lower or "gas" in s_lower:
        return "Utilities"
        
    return s


class DatabaseLockedError(RuntimeError):
    """Raised when the DuckDB file is already open in another process
    (most commonly: the MB-EGX desktop app is still running)."""


class _ConnectionWrapper:
    def __init__(self, db_path: str, retries: int = 3, retry_delay_seconds: float = 1.5):
        self._lock = threading.RLock()
        last_err = None
        for attempt in range(retries):
            try:
                self._conn = duckdb.connect(db_path)
                return
            except duckdb.IOException as e:
                last_err = e
                # DuckDB's lock-conflict message mentions "being used by
                # another process" (or similar) - anything else (corrupt
                # file, missing directory, etc.) shouldn't be silently
                # retried and re-labeled as a lock issue.
                if "another process" not in str(e) and "lock" not in str(e).lower():
                    raise
                if attempt < retries - 1:
                    time.sleep(retry_delay_seconds)
        raise DatabaseLockedError(
            f"Can't open the database at '{db_path}' — it's already open in "
            f"another program (usually the MB-EGX desktop app, if it's "
            f"running). Close that app and run this again.\n\n"
            f"Original error: {last_err}"
        ) from last_err

    def cursor(self):
        return self._conn.cursor()

    def execute(self, query: str, parameters=None):
        with self._lock:
            if parameters:
                return self._conn.execute(query, parameters)
            return self._conn.execute(query)

    def executemany(self, query: str, parameters):
        with self._lock:
            return self._conn.executemany(query, parameters)

    def register(self, view_name: str, df):
        with self._lock:
            return self._conn.register(view_name, df)

    def unregister(self, view_name: str):
        with self._lock:
            return self._conn.unregister(view_name)

    def close(self):
        with self._lock:
            self._conn.close()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._lock.release()


class DatabaseManager:
    _schema_initialized = False
    _shared_connection = None
    _init_lock = threading.Lock()

    def __init__(self, db_path=DB_PATH):
        self.db_path = str(db_path)
        with DatabaseManager._init_lock:
            if DatabaseManager._shared_connection is None:
                DatabaseManager._shared_connection = _ConnectionWrapper(self.db_path)
            if not DatabaseManager._schema_initialized:
                self._init_db()
                DatabaseManager._schema_initialized = True

    def get_connection(self):
        return DatabaseManager._shared_connection

    @classmethod
    def close_connection(cls):
        with cls._init_lock:
            if cls._shared_connection is not None:
                try:
                    cls._shared_connection.execute("CHECKPOINT;")
                except Exception as e:
                    logger.warning(f"CHECKPOINT on shutdown failed: {e}")
                try:
                    cls._shared_connection.close()
                except Exception as e:
                    logger.warning(f"Closing DuckDB connection on shutdown failed: {e}")
                cls._shared_connection = None
                cls._schema_initialized = False

    def optimize_database(self):
        with self.get_connection() as conn:
            conn.execute("CHECKPOINT;")
            conn.execute("VACUUM;")

    def _init_db(self):
        with self.get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_data (
                    date DATE,
                    ticker VARCHAR,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE,
                    volume DOUBLE,
                    PRIMARY KEY (ticker, date)
                );
            """)
            conn.execute("DROP INDEX IF EXISTS idx_ticker_date;")
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_tracker (
                    file_path VARCHAR PRIMARY KEY,
                    last_modified DOUBLE,
                    file_hash VARCHAR
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ingestion_errors (
                    file_path VARCHAR,
                    error_message VARCHAR,
                    error_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_owned (
                    ticker VARCHAR PRIMARY KEY,
                    buy_price DOUBLE,
                    shares DOUBLE,
                    purchase_date DATE,
                    is_demo BOOLEAN DEFAULT FALSE
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_closed (
                    ticker VARCHAR,
                    buy_price DOUBLE,
                    sell_price DOUBLE,
                    shares DOUBLE,
                    purchase_date DATE,
                    sell_date DATE,
                    realized_pnl DOUBLE,
                    pnl_pct DOUBLE,
                    is_demo BOOLEAN DEFAULT FALSE
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS account_cash (
                    id INTEGER PRIMARY KEY,
                    balance DOUBLE
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sector_map (
                    ticker VARCHAR PRIMARY KEY,
                    sector VARCHAR
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticker_names (
                    ticker VARCHAR PRIMARY KEY,
                    name VARCHAR
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS position_targets (
                    ticker VARCHAR PRIMARY KEY,
                    target_mode VARCHAR,   -- 'PCT' (percent gain from buy price) or 'AMOUNT' (EGP profit)
                    target_value DOUBLE    -- meaning depends on target_mode
                );
            """)
            # Session Picks — see session_picks.py. A "pick" is a ticker
            # stamped with the price/date it was chosen at for a given
            # horizon; it stays 'active' until either it hits the
            # horizon's SESSION_PICKS_EXPECTED_PCT gain (-> 'achieved', slot freed for
            # a new pick) or is manually cleared from the desktop app.
            # id uses a sequence rather than the ticker as PK because the
            # same ticker can cycle through this table many times over
            # (achieved, then picked again later) and we want each cycle
            # to keep its own achieved_date/achieved_price history.
            conn.execute("CREATE SEQUENCE IF NOT EXISTS seq_session_picks_id START 1;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_picks (
                    id INTEGER PRIMARY KEY DEFAULT nextval('seq_session_picks_id'),
                    ticker VARCHAR,
                    horizon VARCHAR,        -- 'short' | 'medium' | 'long'
                    pick_date DATE,
                    ref_price DOUBLE,
                    status VARCHAR DEFAULT 'active',  -- 'active' | 'achieved'
                    achieved_date DATE,
                    achieved_price DOUBLE,
                    achieved_pct DOUBLE
                );
            """)
            # Manual-removal memory (see remove_pick / get_excluded_tickers
            # below). Without this, clicking "Remove" just deletes the row
            # and re-runs the matrix - but refresh_session_picks() refills
            # any freed slot from the SAME ranked candidate pool, so on the
            # SAME session date the ticker you just removed is very often
            # still the #1 candidate and gets immediately re-picked into
            # its own freed slot. That's the "I click yes, it runs, but the
            # ticker isn't removed" bug. Recording the removal here lets the
            # refill step skip that ticker for the rest of that session
            # date only - a new/different session date (real new data) is
            # free to pick it again if it's still a strong candidate then.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_picks_excluded (
                    ticker VARCHAR,
                    horizon VARCHAR,
                    excluded_date DATE,
                    PRIMARY KEY (ticker, horizon, excluded_date)
                );
            """)

            count_cash = conn.execute("SELECT COUNT(*) FROM account_cash;").fetchone()[0]
            if count_cash == 0:
                conn.execute("INSERT INTO account_cash VALUES (1, 0.0);")

    @staticmethod
    def compute_file_metadata(file_path: Path):
        stat = file_path.stat()
        last_mod = stat.st_mtime
        hasher = hashlib.md5()
        hasher.update(str(file_path).encode("utf-8"))
        hasher.update(str(last_mod).encode("utf-8"))
        hasher.update(str(stat.st_size).encode("utf-8"))
        return str(file_path), last_mod, hasher.hexdigest()

    @staticmethod
    def normalize_symbol(ticker: str) -> str:
        t = str(ticker).strip().upper()
        if not t.endswith(".CA") and len(t) <= 6 and "." not in t:
            return t + ".CA"
        return t

    def get_latest_market_date(self) -> str:
        with self.get_connection() as conn:
            try:
                row = conn.cursor().execute("SELECT MAX(date) FROM market_data;").fetchone()
                if row and row[0]:
                    return str(row[0])
                return "N/A"
            except Exception as e:
                logger.error(f"get_latest_market_date() failed: {e}")
                return "N/A"

    # =========================================================================
    # SESSION PICKS — see session_picks.py for the selection/achievement logic
    # that drives these. This class only does storage; it never decides
    # which tickers get picked or what counts as "achieved".
    # =========================================================================
    def get_active_picks(self, horizon: str = None) -> list:
        """Active (not-yet-achieved) picks, newest first. Pass a horizon
        ('short'/'medium'/'long') to filter to just that bucket, or omit
        it to get every active pick across all three."""
        query = "SELECT id, ticker, horizon, pick_date, ref_price FROM session_picks WHERE status = 'active'"
        params = ()
        if horizon:
            query += " AND horizon = ?"
            params = (horizon,)
        query += " ORDER BY pick_date DESC, id DESC;"
        with self.get_connection() as conn:
            rows = conn.cursor().execute(query, params).fetchall()
        return [
            {"id": r[0], "ticker": r[1], "horizon": r[2], "pick_date": str(r[3]), "ref_price": float(r[4])}
            for r in rows
        ]

    def add_pick(self, ticker: str, horizon: str, pick_date: str, ref_price: float):
        ticker = self.normalize_symbol(ticker)
        with self.get_connection() as conn:
            conn.execute(
                "INSERT INTO session_picks (ticker, horizon, pick_date, ref_price, status) "
                "VALUES (?, ?, ?, ?, 'active');",
                (ticker, horizon, str(pick_date), float(ref_price)),
            )

    def mark_pick_achieved(self, pick_id: int, achieved_date: str, achieved_price: float, achieved_pct: float):
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE session_picks SET status = 'achieved', achieved_date = ?, "
                "achieved_price = ?, achieved_pct = ? WHERE id = ?;",
                (str(achieved_date), float(achieved_price), float(achieved_pct), int(pick_id)),
            )

    def remove_pick(self, pick_id: int):
        """Manual removal from the desktop app (e.g. you've changed your mind
        on a pick) — frees its slot for the next auto-refill same as an
        achievement would, just without the achieved_* fields being set.

        Also records the (ticker, horizon) into session_picks_excluded for
        TODAY's session date, so the very next matrix run (triggered right
        after this, from the same session data) doesn't just hand the same
        ticker its freed slot back — see get_excluded_tickers() and
        session_picks.refresh_session_picks()."""
        session_date = self.get_latest_market_date()
        with self.get_connection() as conn:
            row = conn.cursor().execute(
                "SELECT ticker, horizon FROM session_picks WHERE id = ?;", (int(pick_id),)
            ).fetchone()
            conn.execute("DELETE FROM session_picks WHERE id = ?;", (int(pick_id),))
            if row and session_date and session_date != "N/A":
                ticker, horizon = row
                conn.execute(
                    "INSERT INTO session_picks_excluded (ticker, horizon, excluded_date) "
                    "VALUES (?, ?, ?) ON CONFLICT DO NOTHING;",
                    (ticker, horizon, str(session_date)),
                )

    def get_excluded_tickers(self, horizon: str, session_date: str) -> set:
        """Tickers manually removed from this horizon on this exact session
        date (see remove_pick) — skip these when refilling today's slots."""
        if not session_date or session_date == "N/A":
            return set()
        with self.get_connection() as conn:
            rows = conn.cursor().execute(
                "SELECT ticker FROM session_picks_excluded WHERE horizon = ? AND excluded_date = ?;",
                (horizon, str(session_date)),
            ).fetchall()
        return {r[0] for r in rows}

    def get_achievements_for_date(self, achieved_date: str) -> list:
        """Picks that were marked achieved on a specific session date —
        used to build/re-build the social 'achievement' post for that day
        without depending on it still being in the same process run."""
        with self.get_connection() as conn:
            rows = conn.cursor().execute(
                "SELECT id, ticker, horizon, pick_date, ref_price, achieved_date, achieved_price, achieved_pct "
                "FROM session_picks WHERE status = 'achieved' AND achieved_date = ? "
                "ORDER BY achieved_pct DESC;",
                (str(achieved_date),),
            ).fetchall()
        return [
            {
                "id": r[0], "ticker": r[1], "horizon": r[2], "pick_date": str(r[3]),
                "ref_price": float(r[4]), "achieved_date": str(r[5]),
                "achieved_price": float(r[6]), "achieved_pct": float(r[7]),
            }
            for r in rows
        ]

    def get_recent_achieved_picks(self, limit: int = 20) -> list:
        """Most recently achieved picks across all horizons/dates, newest
        first — powers the desktop app's 'Recent Achievements' list."""
        with self.get_connection() as conn:
            rows = conn.cursor().execute(
                "SELECT id, ticker, horizon, pick_date, ref_price, achieved_date, achieved_price, achieved_pct "
                "FROM session_picks WHERE status = 'achieved' "
                "ORDER BY achieved_date DESC, achieved_pct DESC LIMIT ?;",
                (int(limit),),
            ).fetchall()
        return [
            {
                "id": r[0], "ticker": r[1], "horizon": r[2], "pick_date": str(r[3]),
                "ref_price": float(r[4]), "achieved_date": str(r[5]),
                "achieved_price": float(r[6]), "achieved_pct": float(r[7]),
            }
            for r in rows
        ]

    def get_sector_map(self) -> dict:
        with self.get_connection() as conn:
            try:
                rows = conn.cursor().execute("SELECT ticker, sector FROM sector_map;").fetchall()
                s_map, excel_names = {}, {}
                ALIASES = {
                    "QNBE": "QNBA", "IRON": "IRAX", "GEMM": "ECAP", "MEFM": "CEFM",
                    "LECI": "LCSW", "GTEX": "GTXC", "AIND": "AIH",
                    "DREG": "DDEV", "EGX3": "EGX30ETF",
                    "MNHD": "MASR", "ESRS": "ATQA", "AUTO": "GBCO", "EIPC": "PHAR",
                    "OBFH": "OFH", "CFGI": "CFGH", "OBOR": "OBRI",
                    "ELAH": "AFDI", "EACB": "EACE", "EGGR": "AREH", "GCLR": "AALR",
                    "ACAP": "ACAMD", "ANEP": "ACFR", "IRSM": "ISMQ",
                    "MEPH": "MPCI", "ICON": "ENGC", "EXOL": "ZEOT", "VLMR": "VLMRA"
                }

                for r in rows:
                    key = str(r[0]).strip().upper()
                    sec = clean_sector_name(str(r[1]))
                    norm = self.normalize_symbol(key)
                    raw = key.replace(".CA", "")
                    s_map[key] = s_map[norm] = s_map[raw] = sec
                    
                    if raw in ALIASES:
                        alias = ALIASES[raw]
                        s_map[alias] = s_map[self.normalize_symbol(alias)] = sec
                    for old_k, new_k in ALIASES.items():
                        if raw == new_k:
                            s_map[old_k] = s_map[self.normalize_symbol(old_k)] = sec

                    s_clean = key.lower().replace("go green", "gogreen")
                    s_clean = re.sub(r'\b(s\.?a\.?e\.?|co\.?|company|egypt|egyptian|for|and|of|the|in|-|–|&|holding|group)\b', ' ', s_clean)
                    s_clean = re.sub(r'[^\w\s]', ' ', s_clean)
                    tokens = set(s_clean.split())
                    if len(tokens) >= 1:
                        excel_names[key] = (tokens, sec)

                t_names = conn.cursor().execute("SELECT ticker, name FROM ticker_names;").fetchall()
                for t_sym, t_name in t_names:
                    norm_sym = self.normalize_symbol(t_sym)
                    if norm_sym not in s_map and t_name:
                        n_clean = str(t_name).lower().replace("go green", "gogreen")
                        n_clean = re.sub(r'\b(s\.?a\.?e\.?|co\.?|company|egypt|egyptian|for|and|of|the|in|-|–|&|holding|group)\b', ' ', n_clean)
                        n_clean = re.sub(r'[^\w\s]', ' ', n_clean)
                        n_tokens = set(n_clean.split())
                        if n_tokens:
                            best_sec, best_ratio = None, 0
                            for _, (ex_tokens, sec) in excel_names.items():
                                overlap = len(n_tokens & ex_tokens)
                                if overlap > 0:
                                    ratio = overlap / min(len(n_tokens), len(ex_tokens))
                                    if ratio > best_ratio and ratio >= 0.5:
                                        best_ratio, best_sec = ratio, sec
                            if best_sec:
                                s_map[norm_sym] = s_map[t_sym] = s_map[t_sym.replace(".CA", "")] = best_sec

                all_db_tickers = conn.cursor().execute("SELECT DISTINCT ticker FROM market_data;").fetchall()
                for (db_sym,) in all_db_tickers:
                    norm_sym = self.normalize_symbol(db_sym)
                    if norm_sym not in s_map:
                        s_map[db_sym] = s_map[norm_sym] = s_map[db_sym.replace(".CA", "")] = "General / Diversified"

                return s_map
            except Exception as e:
                logger.error(f"get_sector_map() failed: {e}")
                return {}

    def get_cash_balance(self):
        with self.get_connection() as conn:
            try:
                row = conn.cursor().execute("SELECT balance FROM account_cash WHERE id = 1;").fetchone()
                return float(row[0]) if row else 0.0
            except Exception as e:
                logger.error(f"get_cash_balance() failed: {e}")
                return 0.0

    def set_cash_balance(self, amount: float):
        with self.get_connection() as conn:
            conn.execute("INSERT OR REPLACE INTO account_cash (id, balance) VALUES (1, ?);", (float(amount),))

    def clear_sample_data(self):
        with self.get_connection() as conn:
            conn.execute("DELETE FROM portfolio_owned WHERE is_demo = TRUE;")
            conn.execute("DELETE FROM portfolio_closed WHERE is_demo = TRUE;")

    def get_unique_tickers(self):
        with self.get_connection() as conn:
            try:
                rows = conn.cursor().execute("SELECT DISTINCT ticker FROM market_data ORDER BY ticker ASC;").fetchall()
                return [self.normalize_symbol(r[0]) for r in rows if r[0]]
            except Exception as e:
                logger.error(f"get_unique_tickers() failed: {e}")
                return []

    def add_owned_stock(self, ticker: str, buy_price: float, shares: float, purchase_date: str, mode="ADD_SCALE", is_demo=False):
        ticker = self.normalize_symbol(ticker)
        if mode != "OVERWRITE" and float(shares) <= 0:
            if _LANG == "AR":
                return (False, "⚠️ خطأ: يجب أن يكون عدد الأسهم المضافة أكبر من صفر.")
            return (False, "⚠️ Error: Number of shares to add must be greater than zero.")
            
        with self.get_connection() as conn:
            if mode == "OVERWRITE":
                conn.execute(
                    "INSERT OR REPLACE INTO portfolio_owned (ticker, buy_price, shares, purchase_date, is_demo) VALUES (?, ?, ?, ?, ?);",
                    (ticker, float(buy_price), float(shares), str(purchase_date), bool(is_demo)),
                )
                if _LANG == "AR":
                    return (True, f"✏️ تم تصحيح / استبدال المركز لـ {ticker}:\nتم تعيينه إلى {shares:,.4f} سهم بالضبط بسعر {buy_price:.4f} جنيه.")
                return (True, f"✏️ Corrected / Overwritten position for {ticker}:\nSet to exactly {shares:,.4f} shares @ {buy_price:.4f} EGP.")
            else:
                row = conn.cursor().execute("SELECT buy_price, shares, is_demo FROM portfolio_owned WHERE ticker = ?;", (ticker,)).fetchone()
                if not row:
                    conn.execute(
                        "INSERT INTO portfolio_owned (ticker, buy_price, shares, purchase_date, is_demo) VALUES (?, ?, ?, ?, ?);",
                        (ticker, float(buy_price), float(shares), str(purchase_date), bool(is_demo)),
                    )
                    if _LANG == "AR":
                        return (True, f"🛒 تم فتح مركز جديد لـ {ticker}:\n{shares:,.4f} سهم بسعر {buy_price:.4f} جنيه.")
                    return (True, f"🛒 Opened fresh position for {ticker}:\n{shares:,.4f} shares @ {buy_price:.4f} EGP.")
                else:
                    old_p, old_s = float(row[0]), float(row[1])
                    new_s = old_s + float(shares)
                    if new_s <= 0:
                        if _LANG == "AR":
                            return (False, "⚠️ خطأ: لا يمكن أن يكون إجمالي عدد الأسهم صفرًا أو أقل.")
                        return (False, "⚠️ Error: Combined share quantity cannot be zero or less.")
                    new_p = ((old_s * old_p) + (float(shares) * float(buy_price))) / new_s
                    conn.execute("UPDATE portfolio_owned SET buy_price = ?, shares = ?, purchase_date = ?, is_demo = ? WHERE ticker = ?;", (new_p, new_s, str(purchase_date), bool(is_demo), ticker))
                    if _LANG == "AR":
                        return (True, f"📈 تمت الزيادة في {ticker}! دمج {old_s:,.4f} + {shares:,.4f} سهم.\nالإجمالي الجديد: {new_s:,.4f} سهم | متوسط التكلفة المرجح الجديد: {new_p:.4f} جنيه (كان {old_p:.4f}).")
                    return (True, f"📈 Scaled into {ticker}! Combined {old_s:,.4f} + {shares:,.4f} shares.\nNew Total: {new_s:,.4f} shares | New Weighted Average Cost: {new_p:.4f} EGP (was {old_p:.4f}).")

    def set_position_target(self, ticker: str, target_mode: str, target_value: float):
        """target_mode: 'PCT' (target_value = % gain from buy price) or
        'AMOUNT' (target_value = EGP profit desired on the whole position)."""
        ticker = self.normalize_symbol(ticker)
        target_mode = "AMOUNT" if str(target_mode).upper().startswith("A") else "PCT"
        with self.get_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO position_targets (ticker, target_mode, target_value) VALUES (?, ?, ?);",
                (ticker, target_mode, float(target_value)),
            )

    def get_position_target(self, ticker: str):
        ticker = self.normalize_symbol(ticker)
        with self.get_connection() as conn:
            row = conn.cursor().execute(
                "SELECT target_mode, target_value FROM position_targets WHERE ticker = ?;", (ticker,)
            ).fetchone()
        return {"target_mode": row[0], "target_value": float(row[1])} if row else None

    def get_all_position_targets(self) -> dict:
        with self.get_connection() as conn:
            rows = conn.cursor().execute(
                "SELECT ticker, target_mode, target_value FROM position_targets;"
            ).fetchall()
        return {
            self.normalize_symbol(r[0]): {"target_mode": r[1], "target_value": float(r[2])}
            for r in rows
        }

    def remove_position_target(self, ticker: str):
        ticker = self.normalize_symbol(ticker)
        with self.get_connection() as conn:
            conn.execute(
                "DELETE FROM position_targets WHERE ticker = ? OR ticker = ?;",
                (ticker, ticker.replace(".CA", "")),
            )

    def remove_owned_stock(self, ticker: str):
        ticker = self.normalize_symbol(ticker)
        with self.get_connection() as conn:
            conn.execute("DELETE FROM portfolio_owned WHERE ticker = ? OR ticker = ?;", (ticker, ticker.replace(".CA", "")))
        # A deleted position's profit target is meaningless once the
        # position itself is gone - don't leave it behind to silently
        # reattach if the same ticker gets bought again later.
        self.remove_position_target(ticker)

    def get_all_owned_stocks(self):
        with self.get_connection() as conn:
            rows = conn.cursor().execute("SELECT ticker, buy_price, shares, purchase_date, is_demo FROM portfolio_owned ORDER BY ticker ASC;").fetchall()
        return {self.normalize_symbol(r[0]): {"buy_price": r[1], "shares": r[2], "purchase_date": str(r[3]), "is_demo": bool(r[4]) if len(r) > 4 else False} for r in rows}

    def record_sale(self, ticker: str, sell_price: float, shares_to_sell: float, sell_date: str):
        ticker = self.normalize_symbol(ticker)
        with self.get_connection() as conn:
            row = conn.cursor().execute(
                "SELECT ticker, buy_price, shares, purchase_date, is_demo FROM portfolio_owned WHERE ticker = ? OR ticker = ?;",
                (ticker, ticker.replace(".CA", "")),
            ).fetchone()
            if not row:
                if _LANG == "AR":
                    return (False, f"ليس لديك مركز مفتوح لـ {ticker} في محفظتك النشطة.")
                return (False, f"You do not have an open position for {ticker} in your active portfolio.")
            actual_ticker, buy_price, current_shares, purchase_date = row[0], row[1], row[2], str(row[3])
            is_demo = bool(row[4]) if len(row) > 4 else False
            
            if shares_to_sell > (current_shares + 0.0001):
                if _LANG == "AR":
                    return (False, f"لا يمكن بيع {shares_to_sell} سهم. أنت تمتلك فقط {current_shares} سهم من {actual_ticker}.")
                return (False, f"Cannot sell {shares_to_sell} shares. You only own {current_shares} shares of {actual_ticker}.")
            
            realized_pnl = (sell_price - buy_price) * shares_to_sell
            pnl_pct = (((sell_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0.0)

            conn.execute(
                """
                INSERT INTO portfolio_closed (ticker, buy_price, sell_price, shares, purchase_date, sell_date, realized_pnl, pnl_pct, is_demo)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (actual_ticker, buy_price, sell_price, shares_to_sell, purchase_date, str(sell_date), realized_pnl, pnl_pct, is_demo),
            )

            remaining_shares = current_shares - shares_to_sell
            if remaining_shares <= 0.0001:
                conn.execute("DELETE FROM portfolio_owned WHERE ticker = ?;", (actual_ticker,))
                conn.execute(
                    "DELETE FROM position_targets WHERE ticker = ? OR ticker = ?;",
                    (actual_ticker, actual_ticker.replace(".CA", "")),
                )
            else:
                conn.execute("UPDATE portfolio_owned SET shares = ? WHERE ticker = ?;", (remaining_shares, actual_ticker))
        if _LANG == "AR":
            return (True, f"تم تسجيل بيع {shares_to_sell} سهم من {actual_ticker} بسعر {sell_price} جنيه بنجاح.\nالربح/الخسارة المحققة: {realized_pnl:.2f} جنيه ({pnl_pct:.2f}%).")
        return (True, f"Successfully recorded sale of {shares_to_sell} shares of {actual_ticker} @ {sell_price} EGP.\nRealized P&L: {realized_pnl:.2f} EGP ({pnl_pct:.2f}%).")

    def get_all_closed_trades(self):
        with self.get_connection() as conn:
            rows = conn.cursor().execute("SELECT ticker, shares, buy_price, sell_price, realized_pnl, pnl_pct, purchase_date, sell_date, is_demo FROM portfolio_closed ORDER BY sell_date DESC;").fetchall()
        return [
            {
                "Ticker": self.normalize_symbol(r[0]), "Shares Sold": round(r[1], 4),
                "Buy Price": round(r[2], 4), "Sell Price": round(r[3], 4),
                "Realized P&L (EGP)": round(r[4], 2), "Realized P&L (%)": round(r[5], 2),
                "Purchase Date": str(r[6]), "Sell Date": str(r[7]), "is_demo": bool(r[8]) if len(r) > 8 else False
            }
            for r in rows
        ]