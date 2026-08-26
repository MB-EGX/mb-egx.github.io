import os

# MUST be imported before pandas (below) - sets OPENBLAS/MKL/OMP/NUMEXPR
# thread caps as a module-level side effect; those only take effect if
# set before numpy/pandas load anywhere in this process. See config.py's
# module docstring.
from config import WATCH_DIR, MAX_WORKERS, CHUNK_SIZE, get_logger
from freshness import today_cairo

import re
import glob
import pandas as pd
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

try:
    from python_calamine import CalamineWorkbook
except ImportError:
    from calamine import CalamineWorkbook

from db_manager import clean_sector_name

logger = get_logger("ingestion")

# =============================================================================
# Optional display-language for the progress_callback messages below.
# Self-contained (no import from app_gui.py) so there's no circular-import
# risk. The CLI pipeline (publish.py) never calls set_language(), so it
# always sees English here — this can't affect that pipeline in any way.
# app_gui.py calls set_language() to keep these in sync with its own toggle.
# =============================================================================
_LANG = "EN"


def set_language(lang):
    global _LANG
    _LANG = lang if lang == "AR" else "EN"


def _t(en, ar):
    return ar if _LANG == "AR" else en


FIELD_EXACT = {
    'date': ['date', 'dt', 'timestamp', 'datetime', 'trading date', 'trade date'],
    'ticker': ['ticker', 'symbol', 'stock symbol', 'sym'],
    'name': ['name', 'company', 'company name', 'stock name', 'description'],
    'sector': ['sector', 'market sector', 'industry', 'group'],
    'open': ['open', 'open price', 'o'],
    'high': ['high', 'high price', 'h'],
    'low': ['low', 'low price', 'l'],
    'close': ['close', 'close price', 'last', 'price', 'c', 'adj close'],
    'volume': ['volume', 'vol', 'v', 'shares'],
}

FIELD_WORDS = {
    'date': ['date', 'time', 'day', 'dt'],
    'name': ['name', 'company', 'description'],
    'sector': ['sector', 'industry'],
    'open': ['open'],
    'high': ['high'],
    'low': ['low'],
    'close': ['close', 'last', 'price'],
    'volume': ['vol', 'volume', 'shares'],
}

FIELD_EXCLUDE_WORDS = {
    'date': ['earnings', 'dividend', 'expiry', 'expiration', 'ipo'],
    'close': ['prev', 'previous', 'target', 'avg', 'average'],
    'volume': ['average', 'avg', 'float', 'outstanding'],
    'open': [], 'high': [], 'low': [], 'name': [], 'sector': [],
}

TICKER_EXACT_HEADERS = {'ticker', 'symbol', 'stock symbol', 'sym', 'stock_symbol'}
TICKER_FALLBACK_WORDS = ['symbol', 'sym']

# =============================================================================
# Watchlist enrichment columns (fundamentals, technical-rating consensus,
# and period returns) - a broker/data-provider "watchlist export" like
# Investing.com's often carries 25-30 columns beyond plain OHLCV: P/E,
# EPS, Beta, Dividend, Yield, Market Cap, Revenue, an 8-timeframe
# Buy/Sell/Strong Buy technical-rating consensus, and Daily/1W/1M/YTD/1Y/3Y
# % return columns. None of these feed the required date/ticker/close
# schema above, so resolve_column_map() never looks for them and they were
# previously read into df_raw and then silently dropped - parse_excel_worker
# only ever builds the OHLCV data_records list.
#
# This is matched by EXACT (lowercased) header text against the RAW
# (pre-dedup) column strings, not by the same word-token approach as
# FIELD_WORDS above, for one specific reason: a real watchlist export can
# legitimately use the same column name "Daily" once for a technical
# RATING (Buy/Sell/...) and again for a Daily % CHANGE - two different
# fields, same text. pandas.read_csv already disambiguates this for us on
# read (auto-renaming the second occurrence "Daily" -> "Daily.1"), so
# matching the exact mangled string is the only reliable way to route each
# occurrence to the right field - a token/word match would just find
# whichever "daily" column appears first in the header row and stop there,
# silently losing the second one.
ENRICHMENT_FIELD_EXACT = {
    # Fundamentals
    'market cap': 'market_cap',
    'revenue': 'revenue',
    'average vol. (3m)': 'avg_vol_3m',
    'eps': 'eps',
    'p/e ratio': 'pe_ratio',
    'beta': 'beta',
    'dividend': 'dividend',
    'yield': 'yield_pct',
    # Multi-timeframe technical-rating consensus (text: Strong Buy / Buy /
    # Neutral / Sell / Strong Sell)
    '5 minutes': 'rating_5min',
    '15 minutes': 'rating_15min',
    '30 minutes': 'rating_30min',
    'hourly': 'rating_hourly',
    '5 hours': 'rating_5hour',
    'daily': 'rating_daily',       # first "Daily" occurrence = rating
    'weekly': 'rating_weekly',
    'monthly': 'rating_monthly',
    # Period % returns (numeric provider-computed, spans far deeper history
    # than this app's own ingested bars while that history is still short)
    'daily.1': 'return_daily_pct',  # second "Daily" occurrence (pandas-mangled) = % return
    '1 week': 'return_1w_pct',
    '1 month': 'return_1m_pct',
    'ytd': 'return_ytd_pct',
    '1 year': 'return_1y_pct',
    '3 years': 'return_3y_pct',
}

# Text rating -> numeric scale, for a single averaged "consensus score"
# (-2..+2) across whichever of the 8 rating columns are present/non-empty,
# so decision_matrix.py can use one number instead of 8 separate strings.
RATING_TEXT_TO_SCORE = {
    'strong sell': -2, 'sell': -1, 'neutral': 0, 'buy': 1, 'strong buy': 2,
}
RATING_FIELDS = (
    'rating_5min', 'rating_15min', 'rating_30min', 'rating_hourly',
    'rating_5hour', 'rating_daily', 'rating_weekly', 'rating_monthly',
)
RETURN_FIELDS = (
    'return_daily_pct', 'return_1w_pct', 'return_1m_pct',
    'return_ytd_pct', 'return_1y_pct', 'return_3y_pct',
)
FUNDAMENTAL_FIELDS = (
    'market_cap', 'revenue', 'avg_vol_3m', 'eps', 'pe_ratio', 'beta',
    'dividend', 'yield_pct',
)


def resolve_enrichment_column_map(raw_columns):
    """Maps this watchlist CSV's enrichment columns (see
    ENRICHMENT_FIELD_EXACT) by exact lowercased text against the RAW
    (pandas-already-deduped) column names - e.g. pandas.read_csv turns a
    header row containing "Daily" twice into columns named "Daily" and
    "Daily.1" automatically; this relies on that exact behavior. Returns
    {field_name: column_index}; a watchlist with none of these columns
    (a plain OHLCV-only feed) returns {} and the caller skips enrichment
    entirely for that file - this never affects the required OHLCV
    ingestion path."""
    col_map = {}
    for idx, raw_col in enumerate(raw_columns):
        key = str(raw_col).strip().lower()
        field = ENRICHMENT_FIELD_EXACT.get(key)
        if field and field not in col_map:
            col_map[field] = idx
    return col_map


def _clean_percent(val):
    """Like _clean_price but for provider "% change"-style cells
    ('43.70%', '-1.39%', '-', '--') - keeps the sign (unlike _clean_price,
    which discards non-positive values; a negative return is a normal,
    meaningful value here, not a bad parse)."""
    if pd.isna(val):
        return None
    if isinstance(val, str):
        v = val.replace(',', '').replace('%', '').strip()
        if v in ('', '--', '-', 'nan', 'None', 'N/A'):
            return None
        val = v
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _clean_number(val):
    """Like _clean_percent but for plain numeric fundamentals (EPS, P/E,
    Beta, Market Cap, ...) that can also legitimately be negative (a
    loss-making company's EPS/P-E) - same '-'/'--' blank handling."""
    return _clean_percent(val)


def _clean_rating_text(val):
    """Normalizes a technical-rating cell ('Strong Buy', 'Sell', ...) to
    lowercase for RATING_TEXT_TO_SCORE lookup; returns None for blanks so
    a missing timeframe's rating doesn't get invented as 'Neutral'."""
    if pd.isna(val):
        return None
    s = str(val).strip().lower()
    return s if s in RATING_TEXT_TO_SCORE else None


def _extract_enrichment_row(row, enrichment_map, date_str, ticker_val):
    """Builds one ticker_enrichment record from a watchlist row, or None
    if none of the mapped enrichment columns actually had a usable value
    (an index row like .EGX30 has no P/E/EPS/ratings at all - skip it
    rather than inserting an all-NULL row)."""
    rec = {'date': date_str, 'ticker': ticker_val}
    any_value = False

    for field in FUNDAMENTAL_FIELDS:
        if field in enrichment_map:
            v = _clean_number(row[enrichment_map[field]])
            rec[field] = v
            any_value = any_value or v is not None
        else:
            rec[field] = None

    for field in RETURN_FIELDS:
        if field in enrichment_map:
            v = _clean_percent(row[enrichment_map[field]])
            rec[field] = v
            any_value = any_value or v is not None
        else:
            rec[field] = None

    scores = []
    for field in RATING_FIELDS:
        if field in enrichment_map:
            text = _clean_rating_text(row[enrichment_map[field]])
            rec[field] = text
            if text is not None:
                any_value = True
                scores.append(RATING_TEXT_TO_SCORE[text])
        else:
            rec[field] = None
    rec['rating_consensus_score'] = round(sum(scores) / len(scores), 3) if scores else None

    return rec if any_value else None


def _tokenize(header):
    return set(re.findall(r'[a-z0-9]+', header))


def resolve_column_map(headers):
    col_map = {}
    for idx, h in enumerate(headers):
        if h in TICKER_EXACT_HEADERS:
            col_map['ticker'] = idx
            break

    for field, exact_names in FIELD_EXACT.items():
        if field in col_map:
            continue
        for idx, h in enumerate(headers):
            if h in exact_names:
                col_map[field] = idx
                break

    for field, words in FIELD_WORDS.items():
        if field in col_map:
            continue
        exclude = set(FIELD_EXCLUDE_WORDS.get(field, []))
        for idx, h in enumerate(headers):
            tokens = _tokenize(h)
            if tokens & exclude:
                continue
            if tokens & set(words):
                col_map[field] = idx
                break

    if 'ticker' not in col_map:
        for idx, h in enumerate(headers):
            if _tokenize(h) & set(TICKER_FALLBACK_WORDS):
                col_map['ticker'] = idx
                break

    return col_map


def _clean_price(val):
    if pd.isna(val):
        return None
    if isinstance(val, str):
        v = val.replace(',', '').replace('%', '').strip()
        if v in ('', '--', '-', 'nan', 'None', 'N/A'):
            return None
        val = v
    try:
        f = float(val)
    except (ValueError, TypeError):
        return None
    return f if f > 0 else None


def _clean_volume(val):
    if pd.isna(val):
        return 0.0
    if isinstance(val, str):
        v = val.replace(',', '').replace('%', '').strip()
        if v in ('', '--', '-', 'nan', 'None', 'N/A'):
            return 0.0
        val = v
    try:
        f = float(val)
    except (ValueError, TypeError):
        return 0.0
    return f if f >= 0 else 0.0


_HTML_METACHARS = str.maketrans({
    '<': '', '>': '', '"': "'", "'": '\u2019', '&': 'and', '`': ''
})
_BIDI_DANGEROUS = ''.join(chr(c) for c in (0x202A, 0x202B, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069))

# CSV/Excel "formula injection" (CWE-1236): a cell value that STARTS with
# one of these characters is interpreted as a formula by Excel/Sheets/
# LibreOffice the moment this data is ever re-opened as a spreadsheet or
# CSV (e.g. someone exports ingested data for review, or a future feature
# adds a "download as CSV" button) - letting a malicious market-data feed
# smuggle a formula like =HYPERLINK(...) or =cmd|'/c calc'!A1 into a
# ticker/name/sector cell that then executes in whoever opens that file.
# This is a distinct injection class from the HTML/script handling above
# (a plain-text sink, not an HTML one) and wasn't covered by it at all.
_FORMULA_INJECTION_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def _sanitize_text_field(val: str, max_len: int = 200) -> str:
    """Strip HTML/script-ish and invisible-directional characters from
    ingested text before it can reach desktop/web surfaces, and defuse
    CSV/Excel formula injection (see _FORMULA_INJECTION_PREFIXES) before
    it can reach any spreadsheet surface.

    BUGFIX (W4): the HTML-metachar translation used to map " -> ' but
    left pre-existing ' characters completely untouched - so on top of
    not neutralizing an attacker's own single quotes, every double quote
    it "sanitized" was converted INTO a fresh, still-unescaped single
    quote, net *increasing* the number of raw quote characters in the
    output. ' now maps to the typographic right-single-quote (’) instead,
    matching the existing " -> ' spirit (still reads naturally in real
    names like "O'Reilly") while actually removing the raw delimiter.
    """
    if val is None:
        return ""
    s = str(val)
    # Strip control chars FIRST: doing this after the regex checks below
    # (as a previous version of this function did) let an attacker split
    # a blocked keyword with an embedded control character - e.g.
    # "java\tscript:" - and slip through, since \s* in the regex only
    # matches BETWEEN "javascript" and ":", not a tab hidden inside the
    # word itself. Stripping control chars up front closes that bypass
    # class entirely rather than trying to special-case it in the regex.
    s = re.sub(r'[\x00-\x1f\x7f]', '', s)
    s = ''.join(ch for ch in s if ch not in _BIDI_DANGEROUS)
    s = s.translate(_HTML_METACHARS)
    s = re.sub(r'(?i)javascript\s*:', '', s)
    s = re.sub(r'(?i)<\s*/?\s*script[^>]*>', '', s)
    s = s.strip()

    # Defuse formula injection: a leading apostrophe forces Excel/Sheets/
    # LibreOffice to treat the cell as literal text instead of a formula,
    # without altering how the value displays or sorts anywhere else
    # (web/desktop just see one extra leading char, which is harmless
    # there - only spreadsheet programs treat a leading ' specially).
    if s and s[0] in _FORMULA_INJECTION_PREFIXES:
        s = "'" + s

    return s[:max_len]


# How far past "today" (Cairo) a parsed date is still allowed to be
# before it's treated as corrupt input and rejected. Small and positive
# (not 0) purely to absorb the gap between "today" as measured on
# whatever machine/CI runner does the parsing and "today" in Cairo -
# never intended to let a genuinely-future date (a bad MM/DD<->DD/MM
# read, a fabricated/synthetic row, a typo'd year) through silently.
_MAX_FUTURE_DATE_SLACK_DAYS = 2

# Explicit date formats tried IN ORDER before the generic pandas fallback.
# First match wins. Rationale: this app's feeds are US-ordered MM/DD/YYYY
# plus the long "Thursday, 25 June 2026" style (investing.com watchlist
# exports). Pinning the known shapes up front makes a misparse impossible
# instead of relying on pandas guessing (see _parse_excel_date's docstring
# for the May-12 <-> Dec-5 corruption this guards against). US order is
# checked BEFORE European order, so "05/12/2026" stays May 12 (the feed's
# real meaning).
_EXPLICIT_DATE_FORMATS = (
    ("%Y-%m-%d", "ISO 8601"),
    ("%Y/%m/%d", "ISO with slash"),
    ("%A, %d %B %Y", "Investing.com long English date"),
    ("%a, %d %b %Y", "Abbreviated English long date"),
    ("%b %d, %Y", "US 'Sep 02, 2026' style"),
    ("%d %b %Y", "DD Mon YYYY"),
    ("%m/%d/%Y", "US-ordered MM/DD/YYYY"),
    ("%d/%m/%Y", "European-ordered DD/MM/YYYY"),
)

# Explicit date formats tried IN ORDER before the generic pandas fallback.
# First match wins. Rationale: this app's feeds are US-ordered MM/DD/YYYY
# plus the long "Thursday, 25 June 2026" style (investing.com watchlist
# exports). Pinning the known shapes up front makes a misparse impossible
# instead of relying on pandas guessing (see _parse_excel_date's docstring
# for the May-12 <-> Dec-5 corruption this guards against). US order is
# checked BEFORE European order, so "05/12/2026" stays May 12 (the feed's
# real meaning).
_EXPLICIT_DATE_FORMATS = (
    ("%Y-%m-%d", "ISO 8601"),
    ("%Y/%m/%d", "ISO with slash"),
    ("%A, %d %B %Y", "Investing.com long English date"),
    ("%a, %d %b %Y", "Abbreviated English long date"),
    ("%b %d, %Y", "US 'Sep 02, 2026' style"),
    ("%d %b %Y", "DD Mon YYYY"),
    ("%m/%d/%Y", "US-ordered MM/DD/YYYY"),
    ("%d/%m/%Y", "European-ordered DD/MM/YYYY"),
)


def _parse_excel_date(val):
    """Parses one raw date cell from an ingested CSV/XLSX row.

    ROOT-CAUSE FIX (recurring future-date corruption incident): every
    feed this app ingests - the daily investing.com-style watchlist
    export AND the historical backfill CSVs (see config.py's own
    "investing.com-style watchlist exports" note and normalize_
    historical_csvs.py's docstring) - is US-ordered, MM/DD/YYYY. This
    function used to call pd.to_datetime(val, dayfirst=True), which is
    backwards for that format: any cell where both components are <=12
    (e.g. "5/12/2026", meant as 12 May 2026) got silently misread as
    the other date (5 Dec 2026). normalize_historical_csvs.py and
    data_repair_tools.py were both built to CLEAN UP data already
    corrupted this way, but neither one patched THIS function - the one
    every daily/backfill row actually flows through - so every new
    batch kept re-corrupting the database the same way. dayfirst is now
    explicitly False, matching the feed's real, confirmed format, with
    no ambiguity left for this app to get backwards again.

    SECOND, INDEPENDENT SAFETY NET: even with the correct date order, a
    single bad cell (fat-fingered year, a stray future-dated row in a
    hand-edited CSV, or a still-unknown parsing edge case) could
    previously slide straight into the database as long as it happened
    to be *some* valid calendar date - nothing ever checked whether that
    date made sense. It didn't: this is exactly how 262 tickers' worth
    of fabricated Aug-Dec 2026 bars made it into market_data.json.  Any
    date more than _MAX_FUTURE_DATE_SLACK_DAYS past today (Cairo) is now
    rejected outright (logged, row skipped by the caller) instead of
    silently accepted - a market can't print a bar for a session that
    hasn't happened yet, so a "future" date is never valid data, only
    ever a parsing bug or bad input, either way it must not sail through
    silently.
    """
    if pd.isna(val):
        return None
    try:
        if isinstance(val, (int, float)) or (isinstance(val, str) and val.replace('.', '', 1).isdigit()):
            num = float(val)
            if 30000 < num < 70000:
                parsed = pd.to_datetime(num, unit='D', origin='1899-12-30')
            else:
                return None
        else:
            # Pin the known shapes FIRST (see _EXPLICIT_DATE_FORMATS) so
            # ambiguous strings are resolved by an explicit format, never
            # by pandas guessing. Then fall back to dayfirst=False: this
            # app's feeds are confirmed US-ordered (MM/DD/YYYY), not
            # DD/MM/YYYY - see the docstring above for the recurring
            # May-12 <-> Dec-5 corruption this guards against.
            parsed = None
            for fmt, _label in _EXPLICIT_DATE_FORMATS:
                try:
                    parsed = pd.to_datetime(val, format=fmt)
                except (ValueError, TypeError):
                    continue
                break
            if parsed is None:
                parsed = pd.to_datetime(val, dayfirst=False)
                parsed = None
                for fmt, _label in _EXPLICIT_DATE_FORMATS:
                    try:
                        parsed = pd.to_datetime(val, format=fmt)
                    except (ValueError, TypeError):
                        continue
                    break
                if parsed is None:
                    parsed = pd.to_datetime(val, dayfirst=False)
    except Exception:
        return None

    today_limit = pd.Timestamp(today_cairo()) + pd.Timedelta(days=_MAX_FUTURE_DATE_SLACK_DAYS)
    if parsed > today_limit:
        logger.warning(
            f"Rejected impossible future-dated row: raw value {val!r} parsed to "
            f"{parsed.strftime('%Y-%m-%d')}, which is beyond today (Cairo) + "
            f"{_MAX_FUTURE_DATE_SLACK_DAYS} day(s) slack. This is either a bad "
            f"source cell or a date-order misparse - row skipped, not ingested."
        )
        return None

    return parsed.strftime('%Y-%m-%d')


def parse_excel_worker(file_info):
    file_path, last_mod, file_hash, override_map = file_info
    path_obj = Path(file_path)
    enrichment_records = []

    try:
        if path_obj.suffix.lower() == '.csv':
            df_raw = pd.read_csv(file_path)
            headers = [str(c).strip().lower() for c in df_raw.columns]
            col_map = dict(override_map) if override_map else resolve_column_map(headers)
            required_keys = ['date', 'ticker', 'close']
            if not all(k in col_map for k in required_keys):
                return ("ERROR", file_path, f"Missing required CSV schema (need date/ticker/close). Found headers: {headers}")
            # Independent of col_map above - matched against df_raw's own
            # (pandas-deduped) column names, not the lowercased/collapsed
            # `headers` list, so the "Daily" vs "Daily.1" split survives.
            # See resolve_enrichment_column_map's docstring.
            enrichment_map = resolve_enrichment_column_map(list(df_raw.columns))
            rows_data = df_raw.values.tolist()
        else:
            workbook = CalamineWorkbook.from_path(file_path)
            sheet_names = workbook.sheet_names
            if not sheet_names:
                return ("ERROR", file_path, "Workbook contains no sheets.")
            
            rows = None
            headers = []
            col_map = {}
            for name in sheet_names:
                temp_rows = workbook.get_sheet_by_name(name).to_python()
                if len(temp_rows) >= 2:
                    temp_headers = [str(col).strip().lower() for col in temp_rows[0]]
                    temp_map = dict(override_map) if override_map else resolve_column_map(temp_headers)
                    if all(k in temp_map for k in ['date', 'ticker', 'close']):
                        rows = temp_rows
                        headers = temp_headers
                        col_map = temp_map
                        break

            if not rows:
                return ("ERROR", file_path, "No workbook tab matching required schema (date/ticker/close) found.")
            rows_data = rows[1:]
            # Enrichment extraction is CSV-only for now (see module
            # docstring above) - an .xlsx/.xls watchlist export skips it,
            # same as before this feature existed. Its OHLCV ingestion is
            # completely unaffected either way.
            enrichment_map = {}

        data_records = []
        skipped_rows = 0
        for row in rows_data:
            try:
                close_val = _clean_price(row[col_map['close']])
                if close_val is None:
                    skipped_rows += 1
                    continue

                date_str = _parse_excel_date(row[col_map['date']])
                if not date_str:
                    skipped_rows += 1
                    continue

                open_val = _clean_price(row[col_map['open']]) if 'open' in col_map else None
                high_val = _clean_price(row[col_map['high']]) if 'high' in col_map else None
                low_val = _clean_price(row[col_map['low']]) if 'low' in col_map else None
                volume_val = _clean_volume(row[col_map['volume']]) if 'volume' in col_map else 0.0
                name_val = _sanitize_text_field(row[col_map['name']]) if 'name' in col_map else ""
                sec_val = clean_sector_name(_sanitize_text_field(row[col_map['sector']])) if 'sector' in col_map else ""
                ticker_val = _sanitize_text_field(row[col_map['ticker']], max_len=20).strip().upper()
                if not ticker_val:
                    skipped_rows += 1
                    continue

                data_records.append({
                    'date': date_str,
                    'ticker': ticker_val,
                    'name': name_val,
                    'sector': sec_val,
                    'open': open_val if open_val is not None else close_val,
                    'high': high_val if high_val is not None else close_val,
                    'low': low_val if low_val is not None else close_val,
                    'close': close_val,
                    'volume': volume_val
                })

                if enrichment_map:
                    enr = _extract_enrichment_row(row, enrichment_map, date_str, ticker_val)
                    if enr is not None:
                        enrichment_records.append(enr)
            except (ValueError, TypeError, IndexError):
                skipped_rows += 1
                continue

        if not data_records:
            return ("ERROR", file_path, f"No valid data rows could be parsed ({skipped_rows} row(s) skipped).")

        df = pd.DataFrame(data_records)
        enrichment_df = pd.DataFrame(enrichment_records) if enrichment_records else None
        return ("SUCCESS", file_path, last_mod, file_hash, df, col_map, enrichment_df)

    except Exception as e:
        return ("ERROR", file_path, f"Fatal parse failure: {str(e)}")


class IngestionPipeline:
    def __init__(self):
        from db_manager import DatabaseManager
        self.DatabaseManager = DatabaseManager
        self.dbm = DatabaseManager()

    def run_incremental_ingestion(self, target_dir=WATCH_DIR, progress_callback=None):
        target_path = Path(target_dir)
        all_files = [
            f for f in (list(target_path.rglob("*.xls*")) + list(target_path.rglob("*.csv")))
            if "sector" not in f.stem.lower()
        ]
        total_files = len(all_files)
        
        if total_files == 0:
            if progress_callback: 
                progress_callback(100, _t("No spreadsheet or CSV files found.", "لم يتم العثور على ملفات جداول بيانات أو CSV."))
            return

        with self.dbm.get_connection() as conn:
            # UPGRADE: Use thread-safe read cursor
            existing_files = dict(
                conn.cursor().execute("SELECT file_path, file_hash FROM file_tracker").fetchall()
            )

        override_map_by_file = self.dbm.get_all_ingest_overrides()
        files_to_process = []
        for f in all_files:
            meta = self.DatabaseManager.compute_file_metadata(f)
            if meta[0] not in existing_files or existing_files[meta[0]] != meta[2]:
                files_to_process.append((*meta, override_map_by_file.get(str(f), {})))

        if not files_to_process:
            if progress_callback: 
                progress_callback(100, _t("All files up to date. Zero ingestion required.", "جميع الملفات محدّثة. لا حاجة لأي استيعاب."))
            return

        processed_count = 0
        batch_data = []
        batch_tracker = []
        batch_errors = []
        batch_enrichment = []

        def _handle_result(res):
            nonlocal processed_count
            processed_count += 1
            if progress_callback and processed_count % 5 == 0:
                pct = int((processed_count / len(files_to_process)) * 100)
                progress_callback(pct, _t(
                    f"Ingested {processed_count}/{len(files_to_process)} files...",
                    f"تم استيعاب {processed_count}/{len(files_to_process)} ملف..."
                ))

            if res[0] == "SUCCESS":
                _, f_path, l_mod, f_hash, df, used_col_map, enrichment_df = res
                df['_file_mod_time'] = l_mod
                batch_data.append(df)
                batch_tracker.append((f_path, l_mod, f_hash))
                if enrichment_df is not None and not enrichment_df.empty:
                    batch_enrichment.append(enrichment_df)
                try:
                    self.dbm.save_ingest_override(f_path, used_col_map)
                except Exception:
                    pass
            else:
                _, f_path, err_msg = res
                batch_errors.append((f_path, err_msg))
                logger.warning(f"Ingestion failed for {f_path}: {err_msg}")

        SMALL_BATCH_THRESHOLD = 40

        if len(files_to_process) <= SMALL_BATCH_THRESHOLD:
            for i in range(0, len(files_to_process), CHUNK_SIZE):
                chunk = files_to_process[i:i + CHUNK_SIZE]
                for meta in chunk:
                    _handle_result(parse_excel_worker(meta))
                self._flush_to_db(batch_data, batch_tracker, batch_errors, batch_enrichment)
                batch_data.clear()
                batch_tracker.clear()
                batch_errors.clear()
                batch_enrichment.clear()
        else:
            # W7: BrokenProcessPool (the OpenBLAS/memory issue config.py's
            # own module docstring warns about on Windows) used to
            # propagate straight out of run_incremental_ingestion and
            # abort the whole ingestion run, even though every file NOT
            # yet submitted to the dead pool could still be processed
            # sequentially. Now: if the pool itself dies mid-batch, fall
            # back to processing the REMAINING files in this chunk (and
            # any later chunks) one-by-one in this same process instead
            # of losing the run entirely. Slower, but a slow successful
            # ingestion beats a fast total failure.
            i = 0
            pool_broken = False
            while i < len(files_to_process):
                chunk = files_to_process[i:i + CHUNK_SIZE]
                if not pool_broken:
                    try:
                        with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
                            futures = {executor.submit(parse_excel_worker, meta): meta for meta in chunk}
                            for future in as_completed(futures):
                                _handle_result(future.result())
                    except BrokenProcessPool as e:
                        logger.warning(
                            f"ProcessPoolExecutor died ({e}); falling back to sequential "
                            f"processing for this and all remaining chunks."
                        )
                        pool_broken = True
                        # This chunk's own results are indeterminate (some
                        # futures may have completed, some may not have) -
                        # safest is to reprocess the whole chunk
                        # sequentially rather than guess which succeeded.
                        for meta in chunk:
                            _handle_result(parse_excel_worker(meta))
                else:
                    for meta in chunk:
                        _handle_result(parse_excel_worker(meta))

                self._flush_to_db(batch_data, batch_tracker, batch_errors, batch_enrichment)
                batch_data.clear()
                batch_tracker.clear()
                batch_errors.clear()
                batch_enrichment.clear()
                i += CHUNK_SIZE

        if progress_callback:
            progress_callback(99, _t("Compacting DuckDB database file...", "جاري ضغط ملف قاعدة بيانات DuckDB..."))
        self.dbm.optimize_database()

        if progress_callback:
            progress_callback(100, _t(
                f"Ingestion complete. Processed {processed_count} files.",
                f"اكتمل الاستيعاب. تمت معالجة {processed_count} ملف."
            ))

    def _flush_to_db(self, data_dfs, tracker_records, error_records, enrichment_dfs=None):
        with self.dbm.get_connection() as conn:
            if data_dfs:
                combined_df = pd.concat(data_dfs, ignore_index=True)
                
                if 'name' in combined_df.columns:
                    names_df = combined_df[['ticker', 'name']].drop_duplicates().dropna()
                    names_df = names_df[names_df['name'] != '']
                    if not names_df.empty:
                        conn.register("temp_names_df", names_df)
                        conn.execute("INSERT OR REPLACE INTO ticker_names (ticker, name) SELECT ticker, name FROM temp_names_df;")
                        conn.unregister("temp_names_df")

                if 'sector' in combined_df.columns:
                    sec_df = combined_df[['ticker', 'sector']].drop_duplicates().dropna()
                    sec_df = sec_df[sec_df['sector'] != '']
                    if not sec_df.empty:
                        conn.register("temp_sec_df", sec_df)
                        conn.execute("INSERT OR REPLACE INTO sector_map (ticker, sector) SELECT ticker, sector FROM temp_sec_df;")
                        conn.unregister("temp_sec_df")

                # UPGRADE: Push down deduplication and sorting to DuckDB SQL window functions
                conn.register("temp_df", combined_df[['date', 'ticker', 'open', 'high', 'low', 'close', 'volume', '_file_mod_time']])
                conn.execute("""
                    INSERT OR REPLACE INTO market_data 
                    SELECT date, ticker, open, high, low, close, volume FROM (
                        SELECT *, ROW_NUMBER() OVER (
                            PARTITION BY ticker, date 
                            ORDER BY _file_mod_time DESC
                        ) as rn
                        FROM temp_df
                    ) WHERE rn = 1;
                """)
                conn.unregister("temp_df")

            if enrichment_dfs:
                # Fundamentals / technical-rating consensus / period returns
                # from watchlist CSVs that carry them (see ENRICHMENT_FIELD_
                # EXACT above) - a plain OHLCV-only feed contributes nothing
                # here and this block is simply skipped for it, same as
                # market_data ingestion is unaffected by files that DO carry
                # enrichment columns. Same last-write-wins dedup shape as
                # market_data above, keyed on (ticker, date) instead of
                # needing _file_mod_time since a single watchlist file only
                # ever contributes one row per ticker for a given date.
                enrichment_combined = pd.concat(enrichment_dfs, ignore_index=True)
                enrichment_combined = enrichment_combined.drop_duplicates(subset=['ticker', 'date'], keep='last')
                conn.register("temp_enrichment_df", enrichment_combined)
                conn.execute("""
                    INSERT OR REPLACE INTO ticker_enrichment
                    SELECT date, ticker, market_cap, revenue, avg_vol_3m, eps, pe_ratio, beta,
                           dividend, yield_pct, return_daily_pct, return_1w_pct, return_1m_pct,
                           return_ytd_pct, return_1y_pct, return_3y_pct, rating_5min, rating_15min,
                           rating_30min, rating_hourly, rating_5hour, rating_daily, rating_weekly,
                           rating_monthly, rating_consensus_score
                    FROM temp_enrichment_df;
                """)
                conn.unregister("temp_enrichment_df")

            if tracker_records:
                conn.executemany("""
                    INSERT OR REPLACE INTO file_tracker (file_path, last_modified, file_hash)
                    VALUES (?, ?, ?);
                """, tracker_records)

            if error_records:
                conn.executemany("""
                    INSERT INTO ingestion_errors (file_path, error_message)
                    VALUES (?, ?);
                """, error_records)
