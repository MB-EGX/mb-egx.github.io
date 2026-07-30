import os

# MUST be imported before pandas (below) - sets OPENBLAS/MKL/OMP/NUMEXPR
# thread caps as a module-level side effect; those only take effect if
# set before numpy/pandas load anywhere in this process. See config.py's
# module docstring.
from config import WATCH_DIR, MAX_WORKERS, CHUNK_SIZE, get_logger

import re
import glob
import pandas as pd
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

try:
    from python_calamine import CalamineWorkbook
except ImportError:
    from calamine import CalamineWorkbook

from db_manager import clean_sector_name

logger = get_logger("ingestion")

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
    '<': '', '>': '', '"': "'", '&': 'and',
})


def _sanitize_text_field(val: str, max_len: int = 200) -> str:
    """Strip HTML metacharacters and control chars from ingested text.

    SECURITY: name/sector (and ticker) columns come straight from
    user-supplied Excel/CSV files. They flow DB -> export_json.py ->
    market_data.json -> index.html, where several sites do
    el.innerHTML = TEMPLATE_LITERAL(value) with no escaping. A malicious
    cell value (e.g. "<img src=x onerror=...>") would otherwise become
    stored XSS served to every visitor. Stripping '<' and '>' at the
    ingestion boundary neutralizes this regardless of what any
    downstream renderer does or forgets to do.
    """
    if val is None:
        return ""
    s = str(val).translate(_HTML_METACHARS)
    s = re.sub(r'[\x00-\x1f\x7f]', '', s)  # strip control chars
    return s.strip()[:max_len]


def _parse_excel_date(val):
    if pd.isna(val):
        return None
    try:
        if isinstance(val, (int, float)) or (isinstance(val, str) and val.replace('.', '', 1).isdigit()):
            num = float(val)
            if 30000 < num < 70000:
                return pd.to_datetime(num, unit='D', origin='1899-12-30').strftime('%Y-%m-%d')
        return pd.to_datetime(val, dayfirst=True).strftime('%Y-%m-%d')
    except Exception:
        return None


def parse_excel_worker(file_info):
    file_path, last_mod, file_hash = file_info
    path_obj = Path(file_path)

    try:
        if path_obj.suffix.lower() == '.csv':
            df_raw = pd.read_csv(file_path)
            headers = [str(c).strip().lower() for c in df_raw.columns]
            col_map = resolve_column_map(headers)
            required_keys = ['date', 'ticker', 'close']
            if not all(k in col_map for k in required_keys):
                return ("ERROR", file_path, f"Missing required CSV schema (need date/ticker/close). Found headers: {headers}")
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
                    temp_map = resolve_column_map(temp_headers)
                    if all(k in temp_map for k in ['date', 'ticker', 'close']):
                        rows = temp_rows
                        headers = temp_headers
                        col_map = temp_map
                        break

            if not rows:
                return ("ERROR", file_path, "No workbook tab matching required schema (date/ticker/close) found.")
            rows_data = rows[1:]

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
            except (ValueError, TypeError, IndexError):
                skipped_rows += 1
                continue

        if not data_records:
            return ("ERROR", file_path, f"No valid data rows could be parsed ({skipped_rows} row(s) skipped).")

        df = pd.DataFrame(data_records)
        return ("SUCCESS", file_path, last_mod, file_hash, df)

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
                progress_callback(100, "No spreadsheet or CSV files found.")
            return

        with self.dbm.get_connection() as conn:
            # UPGRADE: Use thread-safe read cursor
            existing_files = dict(
                conn.cursor().execute("SELECT file_path, file_hash FROM file_tracker").fetchall()
            )

        files_to_process = []
        for f in all_files:
            meta = self.DatabaseManager.compute_file_metadata(f)
            if meta[0] not in existing_files or existing_files[meta[0]] != meta[2]:
                files_to_process.append(meta)

        if not files_to_process:
            if progress_callback: 
                progress_callback(100, "All files up to date. Zero ingestion required.")
            return

        processed_count = 0
        batch_data = []
        batch_tracker = []
        batch_errors = []

        def _handle_result(res):
            nonlocal processed_count
            processed_count += 1
            if progress_callback and processed_count % 5 == 0:
                pct = int((processed_count / len(files_to_process)) * 100)
                progress_callback(pct, f"Ingested {processed_count}/{len(files_to_process)} files...")

            if res[0] == "SUCCESS":
                _, f_path, l_mod, f_hash, df = res
                df['_file_mod_time'] = l_mod
                batch_data.append(df)
                batch_tracker.append((f_path, l_mod, f_hash))
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
                self._flush_to_db(batch_data, batch_tracker, batch_errors)
                batch_data.clear()
                batch_tracker.clear()
                batch_errors.clear()
        else:
            with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
                for i in range(0, len(files_to_process), CHUNK_SIZE):
                    chunk = files_to_process[i:i + CHUNK_SIZE]
                    futures = {executor.submit(parse_excel_worker, meta): meta for meta in chunk}

                    for future in as_completed(futures):
                        _handle_result(future.result())

                    self._flush_to_db(batch_data, batch_tracker, batch_errors)
                    batch_data.clear()
                    batch_tracker.clear()
                    batch_errors.clear()

        if progress_callback:
            progress_callback(99, "Compacting DuckDB database file...")
        self.dbm.optimize_database()

        if progress_callback:
            progress_callback(100, f"Ingestion complete. Processed {processed_count} files.")

    def _flush_to_db(self, data_dfs, tracker_records, error_records):
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
