import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

from config import WATCH_DIR, TRANSACTION_FEE_PCT, get_logger
from db_manager import DatabaseManager
from decision_matrix import DecisionMatrix
from analytics import QuantitativeEngine
from chart_widget import StockSectorChartWidget
from ingestion import IngestionPipeline
from PyQt6.QtCore import QDate, Qt, QThread, QTimer, pyqtSignal, QAbstractTableModel, QModelIndex
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QDateEdit, QDialog, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QTableView, QTabWidget, QVBoxLayout, QWidget,
    QCheckBox
)

logger = get_logger("app_gui")

# =============================================================================
# FIREBASE AUTH + FIRESTORE (REST) — same project as the web dashboard, so
# desktop sign-ins and website sign-ins share one user base and one
# `sessions` collection for usage analytics.
# =============================================================================
FIREBASE_API_KEY = "AIzaSyBCC4D61IHTEFNsgO6i8H_BdixwArE-VRo"
FIREBASE_PROJECT_ID = "mb-egx-12d11"
FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents"

# Emails allowed to see the in-app "Usage Analytics" button/dialog.
# Keep this in sync with the ADMIN_EMAILS list and Firestore rules on the website.
ADMIN_EMAILS = ["drmo071990@gmail.com"]


def _from_firestore_value(v):
    """Decode one Firestore REST 'Value' object into a plain Python value."""
    if "stringValue" in v:
        return v["stringValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "booleanValue" in v:
        return v["booleanValue"]
    if "timestampValue" in v:
        return v["timestampValue"]  # RFC3339 string; parsed by callers as needed
    if "nullValue" in v:
        return None
    if "mapValue" in v:
        return {k: _from_firestore_value(val) for k, val in v["mapValue"].get("fields", {}).items()}
    if "arrayValue" in v:
        return [_from_firestore_value(x) for x in v["arrayValue"].get("values", [])]
    return None


def _doc_to_dict(doc):
    return {k: _from_firestore_value(v) for k, v in doc.get("fields", {}).items()}


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def list_recent_sessions(id_token, limit=1000):
    """Reads the most recent session docs (paginated, 300/request)."""
    headers = {"Authorization": f"Bearer {id_token}"}
    docs = []
    page_token = None
    while len(docs) < limit:
        params = {"pageSize": min(300, limit - len(docs)), "orderBy": "start desc"}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(f"{FIRESTORE_BASE}/sessions", headers=headers, params=params, timeout=15)
        data = resp.json()
        if resp.status_code != 200:
            raise RuntimeError(data.get("error", {}).get("message", "Failed to list sessions."))
        batch = data.get("documents", [])
        docs.extend(batch)
        page_token = data.get("nextPageToken")
        if not page_token or not batch:
            break
    return [_doc_to_dict(d) for d in docs[:limit]]


def get_user_doc(id_token, uid):
    headers = {"Authorization": f"Bearer {id_token}"}
    resp = requests.get(f"{FIRESTORE_BASE}/users/{uid}", headers=headers, timeout=10)
    if resp.status_code == 404:
        return {}
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(data.get("error", {}).get("message", "Failed to load user doc."))
    return _doc_to_dict(data)


def compute_usage_analytics(id_token):
    """Combines sessions (web + desktop) with the trade/portfolio stats each
    app pushes to users/{uid}, into one per-user summary. Mirrors the
    website's Usage Analytics panel."""
    sessions = list_recent_sessions(id_token, limit=1000)
    per_user = {}
    total_duration = 0.0
    session_count = 0

    for s in sessions:
        start_dt = _parse_ts(s.get("start"))
        last_dt = _parse_ts(s.get("lastSeen"))
        if not start_dt or not last_dt:
            continue
        dur = max(0.0, (last_dt - start_dt).total_seconds())
        source = s.get("source") or "web"
        session_count += 1
        total_duration += dur

        key = s.get("uid") or s.get("email") or "unknown"
        entry = per_user.setdefault(key, {
            "uid": s.get("uid"), "name": s.get("name") or s.get("email") or "Unknown",
            "email": s.get("email") or "", "web_sessions": 0, "desktop_sessions": 0,
            "total_sec": 0.0, "last_seen": last_dt,
            "trade_count": 0, "trade_value_egp": 0.0, "portfolio_value_egp": 0.0,
        })
        entry["desktop_sessions" if source == "desktop" else "web_sessions"] += 1
        entry["total_sec"] += dur
        if last_dt > entry["last_seen"]:
            entry["last_seen"] = last_dt

    for entry in per_user.values():
        uid = entry.get("uid")
        if not uid:
            continue
        try:
            udoc = get_user_doc(id_token, uid)
        except Exception as e:
            logger.warning(f"Could not load stats for {uid}: {e}")
            continue
        for field in ("web_stats", "desktop_stats"):
            stats = udoc.get(field) or {}
            entry["trade_count"] += int(stats.get("trade_count", 0) or 0)
            entry["trade_value_egp"] += float(stats.get("total_trade_value_egp", 0) or 0)
            entry["portfolio_value_egp"] += float(stats.get("portfolio_value_egp", 0) or 0)

    return {
        "unique_users": len(per_user),
        "session_count": session_count,
        "avg_duration_sec": (total_duration / session_count) if session_count else 0.0,
        "per_user": sorted(per_user.values(), key=lambda u: u["total_sec"], reverse=True),
    }


def _format_duration(total_seconds):
    if total_seconds is None or total_seconds < 0:
        return "—"
    mins = round(total_seconds / 60)
    if mins < 60:
        return f"{mins}m"
    return f"{mins // 60}h {mins % 60}m"


def _now_iso():
    """RFC3339 UTC timestamp in the form Firestore's REST API expects."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def firebase_sign_in(email, password):
    if requests is None:
        raise RuntimeError("The 'requests' package is required for sign-in. Install it with: pip install requests")
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    resp = requests.post(url, json={"email": email, "password": password, "returnSecureToken": True}, timeout=10)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(data.get("error", {}).get("message", "Sign-in failed."))
    return data


def firebase_sign_up(email, password):
    if requests is None:
        raise RuntimeError("The 'requests' package is required for sign-up. Install it with: pip install requests")
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}"
    resp = requests.post(url, json={"email": email, "password": password, "returnSecureToken": True}, timeout=10)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(data.get("error", {}).get("message", "Sign-up failed."))
    return data


def firebase_send_password_reset(email):
    """Sends a 'set/reset your password' email. Works even for accounts that
    signed up via Google only (no password yet) - following the link lets
    them set one, after which email/password sign-in (e.g. on desktop) works."""
    if requests is None:
        raise RuntimeError("The 'requests' package is required. Install it with: pip install requests")
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={FIREBASE_API_KEY}"
    resp = requests.post(url, json={"requestType": "PASSWORD_RESET", "email": email}, timeout=10)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(data.get("error", {}).get("message", "Could not send reset email."))
    return data


def create_session_doc(id_token, uid, email, name):
    """Logs a new login as a 'sessions' doc, tagged source=desktop, so the
    website's Usage Analytics panel can report on desktop usage too."""
    session_id = f"{uid}_{int(time.time() * 1000)}"
    now = _now_iso()
    url = f"{FIRESTORE_BASE}/sessions?documentId={session_id}"
    body = {"fields": {
        "uid": {"stringValue": uid},
        "email": {"stringValue": email or ""},
        "name": {"stringValue": name},
        "source": {"stringValue": "desktop"},
        "start": {"timestampValue": now},
        "lastSeen": {"timestampValue": now},
    }}
    headers = {"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"}
    requests.post(url, headers=headers, json=body, timeout=10)
    return session_id


def touch_session_doc(id_token, session_id):
    """Heartbeat: bumps lastSeen so session length can be measured later."""
    url = f"{FIRESTORE_BASE}/sessions/{session_id}?updateMask.fieldPaths=lastSeen"
    body = {"fields": {"lastSeen": {"timestampValue": _now_iso()}}}
    headers = {"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"}
    requests.patch(url, headers=headers, json=body, timeout=10)


def push_dealing_stats(id_token, uid, stats):
    """Merges trade_count / total_trade_value_egp / portfolio_value_egp
    onto users/{uid}.desktop_stats without touching any other field on
    that document (e.g. the website's own cash/portfolio/history)."""
    url = f"{FIRESTORE_BASE}/users/{uid}?updateMask.fieldPaths=desktop_stats"
    body = {"fields": {
        "desktop_stats": {"mapValue": {"fields": {
            "trade_count": {"integerValue": str(int(stats.get("trade_count", 0)))},
            "total_trade_value_egp": {"doubleValue": float(stats.get("total_trade_value_egp", 0.0))},
            "portfolio_value_egp": {"doubleValue": float(stats.get("portfolio_value_egp", 0.0))},
            "updated": {"timestampValue": _now_iso()},
        }}}
    }}
    headers = {"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"}
    requests.patch(url, headers=headers, json=body, timeout=10)


class _CloudWorker(QThread):
    """Runs one Firebase/Firestore network call off the UI thread so a slow
    or failed connection never freezes the dashboard. Fire-and-forget."""
    finished_result = pyqtSignal(object)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            logger.warning(f"Cloud sync call failed: {e}")
            result = None
        self.finished_result.emit(result)

TRANSLATIONS = {
    "EN": {
        "title": "MB-EGX — Out-of-Core Trading Matrix & Sector Dashboard",
        "scan_folder": "📂 Scan Folder:",
        "browse": "Browse Folder...",
        "ingest": "⚡ Run Ingestion",
        "analyze": "🧠 Execute Matrix",
        "portfolio": "💼 + Manage Portfolio",
        "risk_calc": "⚖️ Risk Calculator",
        "set_cash": "💵 Set Cash",
        "themes": "⚙️ Themes",
        "top10_btn": "🏆 Top 10 Overview",
        "filters": "🔍 Live Filters:",
        "search_ph": "Search Ticker or keyword...",
        "hide_illiquid": "🚫 Hide Illiquid / Unconfirmed",
        "reset_filters": "Reset Filters",
        "btn_columns": "👁️ Columns",
        "col_dialog_title": "Select Columns to Display",
        "col_select_all": "Select All",
        "col_deselect_all": "Deselect All",
        "tab_matrix": "📈 Full Market Action Matrix",
        "tab_sectors": "🏢 Sector Heatmap & Rotation",
        "tab_exits": "🛡️ Owned Portfolio Exit Strategy",
        "tab_history": "📜 Trade History & Realized P&L",
        "tab_fin": "📊 Financial Statement & Account Summary",
        "tab_top10": "🏆 Top 10 Overview",
        "tab_charts": "📊 Charts & Trend Lines",
        "last_date": "📅 Last Data Date:",
        "cash_lbl": "💵 Cash Balance:",
        "port_val": "📈 Stock Portfolio Value:",
        "equity_lbl": "🏛️ Total Account Equity:",
    },
    "AR": {
        "title": "MB-EGX — مصفوفة التداول والقطاعات",
        "scan_folder": "📂 مجلد البيانات:",
        "browse": "استعراض...",
        "ingest": "⚡ معالجة البيانات",
        "analyze": "🧠 تنفيذ التحليل",
        "portfolio": "💼 + إدارة المحفظة",
        "risk_calc": "⚖️ حاسبة المخاطر",
        "set_cash": "💵 تحديد النقدية",
        "themes": "⚙️ المظهر",
        "top10_btn": "🏆 أفضل 10 أسهم",
        "filters": "🔍 تصفية مباشرة:",
        "search_ph": "بحث عن سهم أو كلمة مفتاحية...",
        "hide_illiquid": "🚫 إخفاء الأسهم ضعيفة السيولة",
        "reset_filters": "إعادة ضبط",
        "btn_columns": "👁️ الأعمدة",
        "col_dialog_title": "تحديد الأعمدة المعروضة",
        "col_select_all": "تحديد الكل",
        "col_deselect_all": "إلغاء تحديد الكل",
        "tab_matrix": "📈 مصفوفة السوق الكاملة",
        "tab_sectors": "🏢 خريطة القطاعات والسيولة",
        "tab_exits": "🛡️ إستراتيجية الخروج للمحفظة",
        "tab_history": "📜 سجل الصفقات والربح المحقق",
        "tab_fin": "📊 القوائم المالية وملخص الحساب",
        "tab_top10": "🏆 أفضل 10 أسهم",
        "tab_charts": "📊 الرسوم البيانية والاتجاهات",
        "last_date": "📅 تاريخ أحدث بيانات:",
        "cash_lbl": "💵 الرصيد النقدي:",
        "port_val": "📈 قيمة محفظة الأسهم:",
        "equity_lbl": "🏛️ إجمالي حقوق الحساب:",
    }
}

THEME_DARK = """
    QMainWindow, QDialog, QWidget { background-color: #1a1d24; color: #e2e8f0; font-family: 'Segoe UI', Arial, sans-serif; }
    QTabWidget::pane { border: 1px solid #2d3748; background-color: #1a1d24; }
    QTabBar::tab { background-color: #2d3748; color: #a0aec0; padding: 8px 16px; border-top-left-radius: 4px; border-top-right-radius: 4px; font-weight: bold; }
    QTabBar::tab:selected { background-color: #3182ce; color: #ffffff; }
    QTableWidget, QTableView { background-color: #1a1d24; alternate-background-color: #222730; color: #e2e8f0; gridline-color: #2d3748; border: none; selection-background-color: #2b6cb0; }
    QHeaderView::section { background-color: #2d3748; color: #ffffff; padding: 6px; font-weight: bold; border: 1px solid #1a1d24; }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background-color: #2d3748; color: #ffffff; border: 1px solid #4a5568; padding: 6px; border-radius: 4px; }
    QPushButton { border-radius: 4px; padding: 8px; font-weight: bold; }
    QProgressBar { border: 1px solid #4a5568; border-radius: 4px; text-align: center; background-color: #2d3748; color: white; }
    QProgressBar::chunk { background-color: #3182ce; }
"""

THEME_LIGHT = """
    QMainWindow, QDialog, QWidget { background-color: #f8fafc; color: #1a202c; font-family: 'Segoe UI', Arial, sans-serif; }
    QTabWidget::pane { border: 1px solid #cbd5e0; background-color: #ffffff; }
    QTabBar::tab { background-color: #e2e8f0; color: #4a5568; padding: 8px 16px; border-top-left-radius: 4px; border-top-right-radius: 4px; font-weight: bold; }
    QTabBar::tab:selected { background-color: #2b6cb0; color: #ffffff; }
    QTableWidget, QTableView { background-color: #ffffff; alternate-background-color: #f1f5f9; color: #1a202c; gridline-color: #e2e8f0; border: none; selection-background-color: #bee3f8; selection-color: #1a202c; }
    QHeaderView::section { background-color: #2d3748; color: #ffffff; padding: 6px; font-weight: bold; border: 1px solid #cbd5e0; }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background-color: #ffffff; color: #1a202c; border: 1px solid #a0aec0; padding: 6px; border-radius: 4px; }
    QPushButton { border-radius: 4px; padding: 8px; font-weight: bold; }
    QProgressBar { border: 1px solid #cbd5e0; border-radius: 4px; text-align: center; background-color: #e2e8f0; color: #1a202c; }
    QProgressBar::chunk { background-color: #3182ce; }
"""

THEME_BLUE = """
    QMainWindow, QDialog, QWidget { background-color: #0f172a; color: #e2e8f0; font-family: 'Segoe UI', Arial, sans-serif; }
    QTabWidget::pane { border: 1px solid #1e293b; background-color: #0f172a; }
    QTabBar::tab { background-color: #1e293b; color: #94a3b8; padding: 8px 16px; border-top-left-radius: 4px; border-top-right-radius: 4px; font-weight: bold; }
    QTabBar::tab:selected { background-color: #0284c7; color: #ffffff; }
    QTableWidget, QTableView { background-color: #0f172a; alternate-background-color: #1e293b; color: #f8fafc; gridline-color: #334155; border: none; selection-background-color: #0369a1; }
    QHeaderView::section { background-color: #1e293b; color: #38bdf8; padding: 6px; font-weight: bold; border: 1px solid #0f172a; }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background-color: #1e293b; color: #f8fafc; border: 1px solid #475569; padding: 6px; border-radius: 4px; }
    QPushButton { border-radius: 4px; padding: 8px; font-weight: bold; }
    QProgressBar { border: 1px solid #334155; border-radius: 4px; text-align: center; background-color: #1e293b; color: white; }
    QProgressBar::chunk { background-color: #0284c7; }
"""

THEME_BLUSH_ROSE = """
    QMainWindow, QDialog, QWidget { background-color: #fdf2f8; color: #500724; font-family: 'Segoe UI', Arial, sans-serif; }
    QTabWidget::pane { border: 1px solid #fbcfe8; background-color: #ffffff; }
    QTabBar::tab { background-color: #fce7f3; color: #831843; padding: 8px 16px; border-top-left-radius: 4px; border-top-right-radius: 4px; font-weight: bold; }
    QTabBar::tab:selected { background-color: #ec4899; color: #ffffff; }
    QTableWidget, QTableView { background-color: #ffffff; alternate-background-color: #fef6fb; color: #500724; gridline-color: #fbcfe8; border: none; selection-background-color: #f472b6; selection-color: #ffffff; }
    QHeaderView::section { background-color: #be185d; color: #ffffff; padding: 6px; font-weight: bold; border: 1px solid #fbcfe8; }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background-color: #ffffff; color: #500724; border: 1px solid #f472b6; padding: 6px; border-radius: 4px; }
    QPushButton { border-radius: 4px; padding: 8px; font-weight: bold; }
    QProgressBar { border: 1px solid #fbcfe8; border-radius: 4px; text-align: center; background-color: #fce7f3; color: #500724; }
    QProgressBar::chunk { background-color: #ec4899; }
"""

THEME_VELVET_ROSE = """
    QMainWindow, QDialog, QWidget { background-color: #20131a; color: #ffe4e6; font-family: 'Segoe UI', Arial, sans-serif; }
    QTabWidget::pane { border: 1px solid #3f2231; background-color: #20131a; }
    QTabBar::tab { background-color: #311825; color: #f472b6; padding: 8px 16px; border-top-left-radius: 4px; border-top-right-radius: 4px; font-weight: bold; }
    QTabBar::tab:selected { background-color: #e11d48; color: #ffffff; }
    QTableWidget, QTableView { background-color: #20131a; alternate-background-color: #2a1822; color: #fff1f2; gridline-color: #3f2231; border: none; selection-background-color: #be185d; }
    QHeaderView::section { background-color: #3f2231; color: #fb7185; padding: 6px; font-weight: bold; border: 1px solid #20131a; }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background-color: #311825; color: #fff1f2; border: 1px solid #9f1239; padding: 6px; border-radius: 4px; }
    QPushButton { border-radius: 4px; padding: 8px; font-weight: bold; }
    QProgressBar { border: 1px solid #9f1239; border-radius: 4px; text-align: center; background-color: #311825; color: white; }
    QProgressBar::chunk { background-color: #e11d48; }
"""

THEMES_MAP = {
    "🌙 Institutional Dark": THEME_DARK,
    "☀️ Professional Light": THEME_LIGHT,
    "🌊 Midnight Blue": THEME_BLUE,
    "🌸 Soft Blush Rose (Pastel & Cream)": THEME_BLUSH_ROSE,
    "✨ Velvet Rose Gold (Warm Elegance)": THEME_VELVET_ROSE,
}


class ColumnChooserDialog(QDialog):
    """Interactive Dialog to show/hide table columns."""
    def __init__(self, table_view, lang="EN", parent=None):
        super().__init__(parent)
        self.table_view = table_view
        self.lang = lang
        t = TRANSLATIONS[lang]
        self.setWindowTitle(t["col_dialog_title"])
        self.resize(350, 400)
        self._init_ui()

    def _init_ui(self):
        t = TRANSLATIONS[self.lang]
        layout = QVBoxLayout(self)

        btn_layout = QHBoxLayout()
        btn_all = QPushButton(t["col_select_all"])
        btn_all.setStyleSheet("background-color: #2b6cb0; color: white;")
        btn_all.clicked.connect(lambda: self.set_all(True))
        
        btn_none = QPushButton(t["col_deselect_all"])
        btn_none.setStyleSheet("background-color: #4a5568; color: white;")
        btn_none.clicked.connect(lambda: self.set_all(False))
        
        btn_layout.addWidget(btn_all)
        btn_layout.addWidget(btn_none)
        layout.addLayout(btn_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        self.vbox = QVBoxLayout(container)
        self.vbox.setSpacing(6)

        model = self.table_view.model()
        header_view = self.table_view.horizontalHeader()
        col_count = model.columnCount() if model else self.table_view.columnCount()

        self.checkboxes = []
        for col in range(col_count):
            if isinstance(self.table_view, QTableView) and model:
                col_name = str(model.headerData(col, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or f"Col {col}")
            else:
                item = self.table_view.horizontalHeaderItem(col)
                col_name = item.text() if item else f"Col {col}"

            chk = QCheckBox(col_name)
            chk.setChecked(not self.table_view.isColumnHidden(col))
            chk.toggled.connect(lambda state, c=col: self.table_view.setColumnHidden(c, not state))
            self.vbox.addWidget(chk)
            self.checkboxes.append(chk)

        self.vbox.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll)

        btn_close = QPushButton("✅ OK" if self.lang == "EN" else "✅ حسناً")
        btn_close.setStyleSheet("background-color: #38a169; color: white; padding: 8px; font-weight: bold;")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

    def set_all(self, state: bool):
        for idx, chk in enumerate(self.checkboxes):
            chk.blockSignals(True)
            chk.setChecked(state)
            self.table_view.setColumnHidden(idx, not state)
            chk.blockSignals(False)


class MatrixTableModel(QAbstractTableModel):
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        self._data = data or []
        self._columns = [
            ("Ticker", "Stock ticker symbol"),
            ("Action", "Recommended action based on multi-factor confirmation"),
            ("Score", "Composite rank score (higher = stronger signal)"),
            ("Price", "Current close price"),
            ("Entry (VWAP)", "Suggested entry price benchmarked to 20-day Volume Weighted Average Price"),
            ("Stop-Loss", "Suggested stop-loss price (2x ATR below current price)"),
            ("Shares (1% Risk)", "Suggested share count so a stop-out costs ~1% of cash balance"),
            ("Proj. Gain %", "Historical-analog projected return"),
            ("Pattern Conf %", "Confidence in the historical pattern match (Sortino downside-penalized)"),
            ("Trend", "Trend classification"),
            ("RSI-14", "14-period Relative Strength Index"),
            ("ADX-14", "14-period trend-strength index"),
            ("Vol Z-Score", "Rolling 20-day Volume Standard Deviation anomaly (Z >= 1.5 indicates institutional influx)"),
            ("Avg Vol (20D)", "20-day average traded volume (shares)"),
            ("Data Conf.", "How much real history backs these numbers"),
        ]
        self._col_keys = [
            "Ticker", "Action", "Rank Score", "Current Price", "Target Entry (VWAP)",
            "Suggested Stop-Loss", "Suggested Shares (1% Risk)", "Projected Gain (%)",
            "Pattern Conf (%)", "Trend Class", "RSI-14", "ADX-14", "Vol Z-Score",
            "Avg Volume (20D)", "Data Confidence",
        ]

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self._columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return self._columns[section][0]
            elif role == Qt.ItemDataRole.ToolTipRole:
                return self._columns[section][1]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        key = self._col_keys[col]
        val = self._data[row].get(key, "")
        val_str = str(val)

        if role == Qt.ItemDataRole.DisplayRole:
            return val_str
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if key in ["Action", "Data Confidence"]:
                return int(Qt.AlignmentFlag.AlignCenter)
            elif col >= 2 and col <= 8:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.BackgroundRole:
            if key == "Action":
                if "ILLIQUID" in val_str or "Unconfirmed" in val_str:
                    return QColor("#4a5568")
                elif "STRONG BUY" in val_str or "BREAKOUT BUY" in val_str:
                    return QColor("#276749")
                elif "ACCUMULATE" in val_str or "BUY ON DIP" in val_str:
                    return QColor("#2b6cb0")
                elif "SELL / AVOID" in val_str:
                    return QColor("#9b2c2c")
                elif "HOLD" in val_str:
                    return QColor("#975a16")
        elif role == Qt.ItemDataRole.ForegroundRole:
            if key == "Action":
                if any(w in val_str for w in ["ILLIQUID", "Unconfirmed", "STRONG BUY", "BREAKOUT BUY", "ACCUMULATE", "BUY ON DIP", "SELL / AVOID", "HOLD"]):
                    return QColor("#ffffff")
            elif key == "Data Confidence":
                if "Very Low" in val_str:
                    return QColor("#e53e3e")
                elif "Low" in val_str:
                    return QColor("#dd6b20")
        elif role == Qt.ItemDataRole.FontRole:
            if key == "Action" and ("STRONG BUY" in val_str or "BREAKOUT BUY" in val_str):
                return QFont("Segoe UI", 9, QFont.Weight.Bold)
        return None

    def update_data(self, new_data):
        self.beginResetModel()
        self._data = new_data
        self.endResetModel()


class ThemeSettingsDialog(QDialog):
    def __init__(self, current_theme_name, apply_callback, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ Appearance & Theme Settings")
        self.resize(380, 160)
        self.apply_callback = apply_callback
        self._init_ui(current_theme_name)

    def _init_ui(self, current_theme_name):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        lbl_info = QLabel("Choose your preferred visual dashboard palette:")
        lbl_info.setWordWrap(True)
        layout.addWidget(lbl_info)

        self.cmb_themes = QComboBox()
        self.cmb_themes.addItems(list(THEMES_MAP.keys()))
        if current_theme_name in THEMES_MAP:
            self.cmb_themes.setCurrentText(current_theme_name)
        
        self.cmb_themes.currentTextChanged.connect(self.apply_callback)
        form.addRow("Color Theme:", self.cmb_themes)
        layout.addLayout(form)

        btn_layout = QHBoxLayout()
        btn_close = QPushButton("✅ Close & Save")
        btn_close.setStyleSheet("background-color: #3182ce; color: white; margin-top: 10px;")
        btn_close.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)


class PositionSizingDialog(QDialog):
    def __init__(self, dbm, cash_balance, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚖️ Interactive Risk & Position-Sizing Calculator")
        self.resize(450, 350)
        self.dbm = dbm
        self.qe = QuantitativeEngine()
        self.cash = cash_balance
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        lbl_cash = QLabel(f"<b>Available Cash:</b> {self.cash:,.2f} EGP")
        layout.addWidget(lbl_cash)

        available_tickers = self.dbm.get_unique_tickers()
        self.cmb_ticker = QComboBox()
        self.cmb_ticker.setEditable(True)
        self.cmb_ticker.addItems([""] + available_tickers)
        self.cmb_ticker.setPlaceholderText("Select symbol to auto-load price...")
        self.cmb_ticker.currentIndexChanged.connect(self.on_mode_changed)
        self.cmb_ticker.lineEdit().editingFinished.connect(self.on_mode_changed)
        form.addRow("Ticker Symbol:", self.cmb_ticker)

        self.cmb_stop_mode = QComboBox()
        self.cmb_stop_mode.addItems(["Manual Stop", "1.5x ATR Stop", "2.0x ATR Stop", "3.0x ATR Stop"])
        self.cmb_stop_mode.currentTextChanged.connect(self.on_mode_changed)
        form.addRow("Stop-Loss Mode:", self.cmb_stop_mode)

        self.spn_risk_pct = QDoubleSpinBox()
        self.spn_risk_pct.setRange(0.1, 10.0)
        self.spn_risk_pct.setValue(1.0)
        self.spn_risk_pct.setSuffix(" %")
        self.spn_risk_pct.valueChanged.connect(self.calculate)
        form.addRow("Max Account Risk:", self.spn_risk_pct)

        self.spn_entry = QDoubleSpinBox()
        self.spn_entry.setRange(0.01, 100000.0)
        self.spn_entry.setValue(10.00)
        self.spn_entry.setDecimals(4)
        self.spn_entry.valueChanged.connect(self.calculate)
        form.addRow("Target Entry Price (EGP):", self.spn_entry)

        self.spn_stop = QDoubleSpinBox()
        self.spn_stop.setRange(0.01, 100000.0)
        self.spn_stop.setValue(9.20)
        self.spn_stop.setDecimals(4)
        self.spn_stop.valueChanged.connect(self.calculate)
        form.addRow("Stop-Loss Price (EGP):", self.spn_stop)

        layout.addLayout(form)
        self.lbl_result = QLabel()
        self.lbl_result.setStyleSheet("padding: 12px; border-radius: 6px; font-size: 13px; border: 1px solid #4a5568;")
        layout.addWidget(self.lbl_result)
        self.calculate()

    def on_mode_changed(self):
        mode = self.cmb_stop_mode.currentText()
        if mode == "Manual Stop":
            self.spn_stop.setReadOnly(False)
            self.calculate()
            return

        ticker = self.cmb_ticker.currentText().strip()
        if not ticker:
            return

        price, atr = self.qe.get_latest_price_and_atr(ticker)
        if price > 0:
            self.spn_entry.setValue(price)
            mult = float(mode.split("x")[0])
            stop_val = max(0.0001, price - (mult * atr))
            self.spn_stop.setValue(stop_val)
            self.spn_stop.setReadOnly(True)
        self.calculate()

    def calculate(self):
        entry = self.spn_entry.value()
        stop = self.spn_stop.value()
        risk_pct = self.spn_risk_pct.value() / 100.0

        if stop >= entry:
            self.lbl_result.setText("⚠️ <b>Invalid Parameters:</b> Stop-Loss must be below Target Entry.")
            return

        risk_budget = self.cash * risk_pct
        stop_distance = entry - stop
        
        effective_entry_cost = entry * (1.0 + TRANSACTION_FEE_PCT)
        max_affordable_shares = int(self.cash / effective_entry_cost) if effective_entry_cost > 0 else 0
        raw_shares = int(risk_budget / stop_distance) if stop_distance > 0 else 0
        shares = min(raw_shares, max_affordable_shares)
        
        total_outlay = shares * entry
        total_with_fees = total_outlay * (1.0 + TRANSACTION_FEE_PCT)
        pct_of_portfolio = (total_with_fees / self.cash) * 100 if self.cash > 0 else 0.0

        self.lbl_result.setText(
            f"🎯 <b>Recommended Shares:</b> {shares:,} shares<br>"
            f"💵 <b>Total Outlay (incl. 0.35% fee):</b> {total_with_fees:,.2f} EGP ({pct_of_portfolio:.1f}% of cash)<br>"
            f"🛡️ <b>Max Capital at Risk:</b> {min(risk_budget, total_outlay):,.2f} EGP"
        )


class PortfolioDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Institutional Portfolio & Trade Manager")
        self.resize(500, 450)
        self.dbm = DatabaseManager()
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        available_tickers = self.dbm.get_unique_tickers()

        tab_buy = QWidget()
        form_buy = QFormLayout(tab_buy)

        self.cmb_buy_ticker = QComboBox()
        self.cmb_buy_ticker.setEditable(True)
        self.cmb_buy_ticker.addItems(available_tickers)
        self.cmb_buy_ticker.setPlaceholderText("Type or select ticker (e.g. PHGC.CA)...")

        completer_buy = self.cmb_buy_ticker.completer()
        if completer_buy:
            completer_buy.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer_buy.setFilterMode(Qt.MatchFlag.MatchContains)
        form_buy.addRow("Ticker Symbol:", self.cmb_buy_ticker)

        self.spn_buy_price = QDoubleSpinBox()
        self.spn_buy_price.setRange(0.0001, 100000.0)
        self.spn_buy_price.setDecimals(4)
        self.spn_buy_price.setValue(0.1351)
        form_buy.addRow("Buy Price (EGP):", self.spn_buy_price)

        self.spn_buy_shares = QDoubleSpinBox()
        self.spn_buy_shares.setRange(0.0001, 10000000.0)
        self.spn_buy_shares.setDecimals(4)
        self.spn_buy_shares.setValue(10000.0000)
        form_buy.addRow("Number of Shares:", self.spn_buy_shares)

        self.dt_buy_date = QDateEdit()
        self.dt_buy_date.setCalendarPopup(True)
        self.dt_buy_date.setDate(QDate.currentDate())
        form_buy.addRow("Purchase Date:", self.dt_buy_date)

        btn_scale = QPushButton("📈 Add Shares / Scale In (Auto-Calculate Average)")
        btn_scale.setStyleSheet("background-color: #3182ce; color: white; margin-top: 5px;")
        btn_scale.clicked.connect(lambda: self.save_buy_position(mode="ADD_SCALE"))
        form_buy.addRow(btn_scale)

        btn_layout = QHBoxLayout()
        btn_overwrite = QPushButton("✏️ Correct Mistake / Overwrite")
        btn_overwrite.setStyleSheet("background-color: #d69e2e; color: white;")
        btn_overwrite.clicked.connect(lambda: self.save_buy_position(mode="OVERWRITE"))

        btn_delete = QPushButton("🗑️ Delete Position")
        btn_delete.setStyleSheet("background-color: #e53e3e; color: white;")
        btn_delete.clicked.connect(self.delete_buy_position)

        btn_layout.addWidget(btn_overwrite)
        btn_layout.addWidget(btn_delete)
        form_buy.addRow(btn_layout)
        self.tabs.addTab(tab_buy, "🛒 Open / Add / Delete Position")

        tab_sell = QWidget()
        form_sell = QFormLayout(tab_sell)

        self.cmb_sell_ticker = QComboBox()
        self.cmb_sell_ticker.setEditable(True)
        self.cmb_sell_ticker.addItems(available_tickers)
        self.cmb_sell_ticker.setPlaceholderText("Type or select sold ticker (e.g. PHGC.CA)...")

        completer_sell = self.cmb_sell_ticker.completer()
        if completer_sell:
            completer_sell.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer_sell.setFilterMode(Qt.MatchFlag.MatchContains)
        form_sell.addRow("Ticker Symbol:", self.cmb_sell_ticker)

        self.spn_sell_price = QDoubleSpinBox()
        self.spn_sell_price.setRange(0.0001, 100000.0)
        self.spn_sell_price.setDecimals(4)
        self.spn_sell_price.setValue(0.1500)
        form_sell.addRow("Selling Price (EGP):", self.spn_sell_price)

        self.spn_sell_shares = QDoubleSpinBox()
        self.spn_sell_shares.setRange(0.0001, 10000000.0)
        self.spn_sell_shares.setDecimals(4)
        self.spn_sell_shares.setValue(10000.0000)
        form_sell.addRow("Shares Sold:", self.spn_sell_shares)

        self.dt_sell_date = QDateEdit()
        self.dt_sell_date.setCalendarPopup(True)
        self.dt_sell_date.setDate(QDate.currentDate())
        form_sell.addRow("Sell Date:", self.dt_sell_date)

        btn_record_sale = QPushButton("🤝 Record Sale & Calculate P&L")
        btn_record_sale.setStyleSheet("background-color: #38a169; color: white; margin-top: 10px;")
        btn_record_sale.clicked.connect(self.record_stock_sale)
        form_sell.addRow(btn_record_sale)
        self.tabs.addTab(tab_sell, "🤝 Record Sale / Close Trade")
        layout.addWidget(self.tabs)

        btn_clean = QPushButton("🧹 Clear Sample Demo Data")
        btn_clean.setStyleSheet("background-color: #4a5568; color: white; margin-top: 5px;")
        btn_clean.clicked.connect(self.clean_samples)
        layout.addWidget(btn_clean)

    def save_buy_position(self, mode="ADD_SCALE"):
        ticker = self.cmb_buy_ticker.currentText().strip().upper()
        if not ticker:
            QMessageBox.warning(self, "Input Error", "Please enter or select a valid Ticker Symbol.")
            return
        available_tickers = self.dbm.get_unique_tickers()
        if ticker not in available_tickers and (ticker + ".CA") in available_tickers:
            ticker = ticker + ".CA"
        price = self.spn_buy_price.value()
        shares = self.spn_buy_shares.value()
        p_date = self.dt_buy_date.date().toString("yyyy-MM-dd")
        success, msg = self.dbm.add_owned_stock(ticker, price, shares, p_date, mode=mode, is_demo=False)
        if success:
            QMessageBox.information(self, "Position Updated", msg)
            self.accept()
        else:
            QMessageBox.warning(self, "Position Error", msg)

    def delete_buy_position(self):
        ticker = self.cmb_buy_ticker.currentText().strip().upper()
        if not ticker:
            QMessageBox.warning(self, "Input Error", "Please select the Ticker Symbol to delete.")
            return
        available_tickers = self.dbm.get_unique_tickers()
        if ticker not in available_tickers and (ticker + ".CA") in available_tickers:
            ticker = ticker + ".CA"
        self.dbm.remove_owned_stock(ticker)
        QMessageBox.information(self, "Deleted", f"Permanently removed {ticker} from your active portfolio.")
        self.accept()

    def record_stock_sale(self):
        ticker = self.cmb_sell_ticker.currentText().strip().upper()
        if not ticker:
            QMessageBox.warning(self, "Input Error", "Please enter or select the Ticker Symbol.")
            return
        available_tickers = self.dbm.get_unique_tickers()
        if ticker not in available_tickers and (ticker + ".CA") in available_tickers:
            ticker = ticker + ".CA"
        sell_price = self.spn_sell_price.value()
        shares_sold = self.spn_sell_shares.value()
        s_date = self.dt_sell_date.date().toString("yyyy-MM-dd")
        success, msg = self.dbm.record_sale(ticker, sell_price, shares_sold, s_date)
        if success:
            QMessageBox.information(self, "Sale Recorded!", msg)
            self.accept()
        else:
            QMessageBox.warning(self, "Sale Error", msg)

    def clean_samples(self):
        self.dbm.clear_sample_data()
        QMessageBox.information(self, "Samples Deleted", "Successfully removed demo samples without deleting real positions!")
        self.accept()


class IngestionWorker(QThread):
    progress_signal = pyqtSignal(int, str)
    finished_signal = pyqtSignal()

    def __init__(self, target_dir):
        super().__init__()
        self.target_dir = target_dir

    def run(self):
        pipeline = IngestionPipeline()
        pipeline.run_incremental_ingestion(
            target_dir=self.target_dir,
            progress_callback=lambda pct, msg: self.progress_signal.emit(pct, msg),
        )
        self.finished_signal.emit()


class AnalysisWorker(QThread):
    progress_signal = pyqtSignal(int, str)
    results_signal = pyqtSignal(list, list, dict, list, dict, list, list)

    def run(self):
        matrix = DecisionMatrix()
        buys, exits, top10, closed, fin_stmt, sectors, breakout_watchlist = matrix.analyze_market(
            progress_callback=lambda pct, msg: self.progress_signal.emit(pct, msg)
        )
        self.results_signal.emit(buys, exits, top10, closed, fin_stmt, sectors, breakout_watchlist)


class LoginDialog(QDialog):
    """Blocks app startup until the user signs in with the same Firebase
    account system as the website. Sets self.user_info on success:
    {"uid", "email", "idToken", "name"}."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MB-EGX — Sign In")
        self.resize(380, 260)
        self.setStyleSheet(THEME_DARK)
        self.user_info = None
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        lbl_title = QLabel("MB-EGX")
        lbl_title.setStyleSheet("font-size: 20px; font-weight: bold; color: #38bdf8;")
        lbl_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_title)

        lbl_sub = QLabel("Sign in to your private dashboard")
        lbl_sub.setStyleSheet("color: #a0aec0;")
        lbl_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(lbl_sub)

        self.txt_email = QLineEdit()
        self.txt_email.setPlaceholderText("Email")
        layout.addWidget(self.txt_email)

        self.txt_password = QLineEdit()
        self.txt_password.setPlaceholderText("Password")
        self.txt_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_password.returnPressed.connect(self.do_sign_in)
        layout.addWidget(self.txt_password)

        self.btn_forgot = QPushButton("Forgot password? / Signed in with Google on the website?")
        self.btn_forgot.setFlat(True)
        self.btn_forgot.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_forgot.setStyleSheet(
            "text-align: left; border: none; color: #63b3ed; font-size: 11px; padding: 0;"
        )
        self.btn_forgot.clicked.connect(self.do_forgot_password)
        layout.addWidget(self.btn_forgot)

        self.lbl_error = QLabel("")
        self.lbl_error.setStyleSheet("color: #e53e3e; font-size: 11px;")
        self.lbl_error.setWordWrap(True)
        layout.addWidget(self.lbl_error)

        btn_row = QHBoxLayout()
        self.btn_signin = QPushButton("Sign In")
        self.btn_signin.setStyleSheet("background-color: #3182ce; color: white; padding: 8px; font-weight: bold;")
        self.btn_signin.clicked.connect(self.do_sign_in)
        self.btn_signup = QPushButton("Create Account")
        self.btn_signup.setStyleSheet("background-color: #4a5568; color: white; padding: 8px;")
        self.btn_signup.clicked.connect(self.do_sign_up)
        btn_row.addWidget(self.btn_signin)
        btn_row.addWidget(self.btn_signup)
        layout.addLayout(btn_row)

    def _friendly_name(self, data, email):
        display_name = (data.get("displayName") or "").strip()
        if display_name:
            return display_name
        local = email.split("@")[0]
        return local[:1].upper() + local[1:] if local else "there"

    def _attempt(self, fn, min_password_len=0):
        email = self.txt_email.text().strip()
        password = self.txt_password.text()
        if not email or not password:
            self.lbl_error.setText("Enter both email and password.")
            return
        if min_password_len and len(password) < min_password_len:
            self.lbl_error.setText(f"Password must be at least {min_password_len} characters.")
            return

        self.lbl_error.setText("")
        self.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            data = fn(email, password)
            self.user_info = {
                "uid": data["localId"],
                "email": email,
                "idToken": data["idToken"],
                "name": self._friendly_name(data, email),
            }
            self.accept()
        except Exception as e:
            self.lbl_error.setText(str(e))
        finally:
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)

    def do_sign_in(self):
        self._attempt(firebase_sign_in)

    def do_sign_up(self):
        self._attempt(firebase_sign_up, min_password_len=6)

    def do_forgot_password(self):
        email = self.txt_email.text().strip()
        if not email:
            self.lbl_error.setText("Type your email above first, then click this link.")
            return

        self.lbl_error.setText("")
        self.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            firebase_send_password_reset(email)
            QMessageBox.information(
                self, "Check Your Email",
                f"If an account exists for {email}, a password-set/reset link has just been sent.\n\n"
                "This also works if you originally signed up with 'Sign in with Google' on the "
                "website — that account has no password yet, and this link lets you set one so "
                "you can sign in here on desktop too."
            )
        except Exception as e:
            self.lbl_error.setText(str(e))
        finally:
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)


class AnalyticsDialog(QDialog):
    """Admin-only viewer: sessions + trading activity combined across the
    website and desktop app, read straight from Firestore."""

    def __init__(self, id_token, parent=None):
        super().__init__(parent)
        self.setWindowTitle("📊 Usage Analytics")
        self.resize(920, 520)
        self.setStyleSheet(THEME_DARK)
        self.id_token = id_token
        self._worker = None
        self._init_ui()
        self._load()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        lbl_info = QLabel(
            "Sessions and trading activity combined across the website (🌐) and the desktop app (🖥️). "
            "Time is approximate (30s heartbeat). Trade Value = total EGP bought + sold; "
            "Portfolio Value = cash + open positions at cost."
        )
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet("color: #a0aec0; font-size: 11px;")
        layout.addWidget(lbl_info)

        self.lbl_status = QLabel("Loading session data…")
        self.lbl_status.setStyleSheet("font-weight: bold; padding: 4px 0;")
        layout.addWidget(self.lbl_status)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["User", "Sessions (🌐/🖥️)", "Total Time", "Trades", "Trade Value", "Portfolio Value", "Last Seen"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_refresh = QPushButton("🔄 Refresh")
        self.btn_refresh.clicked.connect(self._load)
        btn_row.addWidget(self.btn_refresh)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _load(self):
        self.lbl_status.setText("Loading session data…")
        self.btn_refresh.setEnabled(False)
        self._worker = _CloudWorker(compute_usage_analytics, self.id_token)
        self._worker.finished_result.connect(self._on_loaded)
        self._worker.start()

    def _on_loaded(self, result):
        self.btn_refresh.setEnabled(True)
        if not result:
            self.lbl_status.setText("⚠️ Could not load analytics data (check your connection or Firestore rules).")
            self.table.setRowCount(0)
            return

        self.lbl_status.setText(
            f"👥 Unique Users: {result['unique_users']}    |    "
            f"📅 Total Sessions: {result['session_count']}    |    "
            f"⏱️ Avg Time / Session: {_format_duration(result['avg_duration_sec'])}"
        )

        rows = result["per_user"]
        self.table.setRowCount(len(rows))
        for i, u in enumerate(rows):
            last_seen_str = u["last_seen"].astimezone().strftime("%Y-%m-%d %H:%M") if u.get("last_seen") else "—"
            values = [
                u["name"] + (f"  ({u['email']})" if u["email"] else ""),
                f"{u['web_sessions'] + u['desktop_sessions']}  (🌐{u['web_sessions']} 🖥️{u['desktop_sessions']})",
                _format_duration(u["total_sec"]),
                str(u["trade_count"]),
                f"{u['trade_value_egp']:,.0f} EGP",
                f"{u['portfolio_value_egp']:,.0f} EGP",
                last_seen_str,
            ]
            for j, val in enumerate(values):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i, j, item)
        self.table.resizeColumnsToContents()


class QuantDashboard(QMainWindow):
    def __init__(self, user_info=None):
        super().__init__()
        self.setWindowTitle("MB-EGX — Out-of-Core Trading Matrix & Sector Dashboard")
        self.resize(1520, 920)
        self.dbm = DatabaseManager()
        self.qe = QuantitativeEngine()
        self.current_theme = "🌙 Institutional Dark"
        self.current_lang = "EN"
        self.theme_highlight = QColor("#2b6cb0")
        self._raw_buys_data = []
        self.user_info = user_info
        self._session_id = None
        self._cloud_threads = set()
        self._init_ui()
        self.apply_theme(self.current_theme)
        self._start_cloud_session()

    def _run_cloud(self, fn, *args, on_result=None, **kwargs):
        """Fire a Firebase/Firestore call on a background thread; keeps a
        reference until it finishes so Qt doesn't garbage-collect it mid-flight."""
        if requests is None or not self.user_info:
            return
        worker = _CloudWorker(fn, *args, **kwargs)
        if on_result:
            worker.finished_result.connect(on_result)
        worker.finished_result.connect(lambda _: self._cloud_threads.discard(worker))
        self._cloud_threads.add(worker)
        worker.start()

    def _start_cloud_session(self):
        if not self.user_info:
            return
        self._run_cloud(
            create_session_doc,
            self.user_info["idToken"], self.user_info["uid"],
            self.user_info["email"], self.user_info["name"],
            on_result=self._on_session_created,
        )
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._heartbeat_tick)
        self._heartbeat_timer.start(30000)

    def _on_session_created(self, session_id):
        self._session_id = session_id

    def _heartbeat_tick(self):
        if self._session_id:
            self._run_cloud(touch_session_doc, self.user_info["idToken"], self._session_id)

    def _compute_dealing_stats(self, exits, closed_trades, fin_stmt):
        """Total EGP value of trades (buys+sells), trade count, and current
        portfolio value — pushed to Firestore for the Usage Analytics panel."""
        trade_count = len(exits) + len(closed_trades)
        total_value = 0.0
        for pos in exits:
            try:
                total_value += float(pos.get("Shares", 0)) * float(pos.get("Buy Price", 0))
            except (TypeError, ValueError):
                pass
        for t in closed_trades:
            try:
                shares = float(t.get("Shares Sold", 0))
                total_value += shares * float(t.get("Buy Price", 0))
                total_value += shares * float(t.get("Sell Price", 0))
            except (TypeError, ValueError):
                pass
        portfolio_value = 0.0
        if fin_stmt:
            try:
                portfolio_value = float(fin_stmt.get("Total Account Equity / Net Worth (EGP)", 0.0))
            except (TypeError, ValueError):
                pass
        return {
            "trade_count": trade_count,
            "total_trade_value_egp": round(total_value, 2),
            "portfolio_value_egp": round(portfolio_value, 2),
        }

    def open_analytics_dialog(self):
        if not self.user_info:
            return
        dlg = AnalyticsDialog(self.user_info["idToken"], self)
        dlg.exec()

    def closeEvent(self, event):
        if self._session_id and self.user_info and requests is not None:
            try:
                touch_session_doc(self.user_info["idToken"], self._session_id)
            except Exception as e:
                logger.warning(f"Final session heartbeat failed: {e}")
        DatabaseManager.close_connection()
        event.accept()

    def apply_theme(self, theme_name):
        if theme_name in THEMES_MAP:
            self.current_theme = theme_name
            stylesheet = THEMES_MAP[theme_name]
            self.setStyleSheet(stylesheet)
            if QApplication.instance():
                QApplication.instance().setStyleSheet(stylesheet)

            if "Blush Rose" in theme_name:
                self.theme_highlight = QColor("#be185d")
                self.btn_ingest.setStyleSheet("background-color: #db2777; color: white;")
                self.btn_analyze.setStyleSheet("background-color: #be185d; color: white;")
                self.btn_manage_portfolio.setStyleSheet("background-color: #9d174d; color: white;")
                self.btn_calc.setStyleSheet("background-color: #e11d48; color: white;")
                self.btn_set_cash.setStyleSheet("background-color: #831843; color: white;")
                self.btn_settings.setStyleSheet("background-color: #f472b6; color: #500724; font-weight: bold;")
                self.lbl_account_header.setStyleSheet("font-size: 14px; font-weight: bold; background-color: #fce7f3; color: #831843; padding: 10px; border-radius: 4px; border: 1px solid #fbcfe8;")
                self.lbl_disclosure.setStyleSheet("font-size: 11px; color: #9d174d; background-color: #fef6fb; padding: 6px; border: 1px solid #f472b6; border-radius: 4px;")
            elif "Velvet Rose" in theme_name:
                self.theme_highlight = QColor("#e11d48")
                self.btn_ingest.setStyleSheet("background-color: #e11d48; color: white;")
                self.btn_analyze.setStyleSheet("background-color: #be185d; color: white;")
                self.btn_manage_portfolio.setStyleSheet("background-color: #9f1239; color: white;")
                self.btn_calc.setStyleSheet("background-color: #fb7185; color: #20131a; font-weight: bold;")
                self.btn_set_cash.setStyleSheet("background-color: #881337; color: white;")
                self.btn_settings.setStyleSheet("background-color: #4c1d32; color: #ffe4e6;")
                self.lbl_account_header.setStyleSheet("font-size: 14px; font-weight: bold; background-color: #311825; color: #fb7185; padding: 10px; border-radius: 4px; border: 1px solid #9f1239;")
                self.lbl_disclosure.setStyleSheet("font-size: 11px; color: #fecdd3; background-color: #2a1822; padding: 6px; border: 1px solid #e11d48; border-radius: 4px;")
            elif "Light" in theme_name:
                self.theme_highlight = QColor("#3182ce")
                self.btn_ingest.setStyleSheet("background-color: #3182ce; color: white;")
                self.btn_analyze.setStyleSheet("background-color: #38a169; color: white;")
                self.btn_manage_portfolio.setStyleSheet("background-color: #805ad5; color: white;")
                self.btn_calc.setStyleSheet("background-color: #dd6b20; color: white;")
                self.btn_set_cash.setStyleSheet("background-color: #d69e2e; color: white;")
                self.btn_settings.setStyleSheet("background-color: #4a5568; color: white;")
                self.lbl_account_header.setStyleSheet("font-size: 14px; font-weight: bold; background-color: #edf2f7; color: #2b6cb0; padding: 10px; border-radius: 4px; border: 1px solid #cbd5e0;")
                self.lbl_disclosure.setStyleSheet("font-size: 11px; color: #744210; background-color: #fffaf0; padding: 6px; border: 1px solid #ecc94b; border-radius: 4px;")
            elif "Midnight" in theme_name:
                self.theme_highlight = QColor("#0284c7")
                self.btn_ingest.setStyleSheet("background-color: #0284c7; color: white;")
                self.btn_analyze.setStyleSheet("background-color: #059669; color: white;")
                self.btn_manage_portfolio.setStyleSheet("background-color: #7c3aed; color: white;")
                self.btn_calc.setStyleSheet("background-color: #ea580c; color: white;")
                self.btn_set_cash.setStyleSheet("background-color: #d97706; color: white;")
                self.btn_settings.setStyleSheet("background-color: #475569; color: white;")
                self.lbl_account_header.setStyleSheet("font-size: 14px; font-weight: bold; background-color: #1e293b; color: #38bdf8; padding: 10px; border-radius: 4px; border: 1px solid #334155;")
                self.lbl_disclosure.setStyleSheet("font-size: 11px; color: #fde68a; background-color: #451a03; padding: 6px; border: 1px solid #d97706; border-radius: 4px;")
            else:
                self.theme_highlight = QColor("#2b6cb0")
                self.btn_ingest.setStyleSheet("background-color: #2b6cb0; color: white;")
                self.btn_analyze.setStyleSheet("background-color: #2f855a; color: white;")
                self.btn_manage_portfolio.setStyleSheet("background-color: #6b46c1; color: white;")
                self.btn_calc.setStyleSheet("background-color: #c05621; color: white;")
                self.btn_set_cash.setStyleSheet("background-color: #b7791f; color: white;")
                self.btn_settings.setStyleSheet("background-color: #4a5568; color: white;")
                self.lbl_account_header.setStyleSheet("font-size: 14px; font-weight: bold; background-color: #222730; color: #63b3ed; padding: 10px; border-radius: 4px; border: 1px solid #4a5568;")
                self.lbl_disclosure.setStyleSheet("font-size: 11px; color: #fbd38d; background-color: #744210; padding: 6px; border: 1px solid #b7791f; border-radius: 4px;")

    def _init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        top_bar = QHBoxLayout()
        self.lbl_last_date = QLabel("📅 Last Data Date: Loading...")
        self.lbl_last_date.setStyleSheet("font-weight: bold; background-color: #2d3748; color: #38bdf8; padding: 4px 8px; border-radius: 4px;")
        
        self.cmb_lang = QComboBox()
        self.cmb_lang.addItems(["🇬🇧 English", "🇪🇬 العربية"])
        self.cmb_lang.currentIndexChanged.connect(self.switch_language)

        top_bar.addWidget(self.lbl_last_date)
        top_bar.addStretch()

        self.lbl_welcome_user = QLabel("")
        if self.user_info:
            self.lbl_welcome_user.setText(f"👋 {self.user_info['name']}")
            self.lbl_welcome_user.setStyleSheet("font-weight: bold; color: #38bdf8; padding: 4px 8px;")
        top_bar.addWidget(self.lbl_welcome_user)

        self.btn_analytics = QPushButton("📊 Usage Analytics")
        self.btn_analytics.setStyleSheet("background-color: #0d9488; color: white; font-weight: bold;")
        self.btn_analytics.clicked.connect(self.open_analytics_dialog)
        self.btn_analytics.setVisible(bool(self.user_info and self.user_info.get("email") in ADMIN_EMAILS))
        top_bar.addWidget(self.btn_analytics)

        top_bar.addWidget(QLabel("🌐 Language / اللغة:"))
        top_bar.addWidget(self.cmb_lang)
        layout.addLayout(top_bar)

        dir_layout = QHBoxLayout()
        self.lbl_dir = QLabel("📂 Scan Folder:")
        self.lbl_dir.setStyleSheet("font-weight: bold; font-size: 13px;")
        dir_layout.addWidget(self.lbl_dir)

        self.txt_scan_dir = QLineEdit(str(WATCH_DIR))
        dir_layout.addWidget(self.txt_scan_dir, stretch=1)

        self.btn_browse = QPushButton("Browse Folder...")
        self.btn_browse.setStyleSheet("background-color: #4a5568; color: white;")
        self.btn_browse.clicked.connect(self.browse_folder)
        dir_layout.addWidget(self.btn_browse)
        layout.addLayout(dir_layout)

        control_panel = QHBoxLayout()
        self.btn_ingest = QPushButton("⚡ Run Ingestion")
        self.btn_ingest.clicked.connect(self.start_ingestion)

        self.btn_analyze = QPushButton("🧠 Execute Matrix")
        self.btn_analyze.clicked.connect(self.start_analysis)

        self.btn_manage_portfolio = QPushButton("💼 + Manage Portfolio")
        self.btn_manage_portfolio.clicked.connect(self.open_portfolio_dialog)

        self.btn_calc = QPushButton("⚖️ Risk Calculator")
        self.btn_calc.clicked.connect(self.open_calculator_dialog)

        self.btn_set_cash = QPushButton("💵 Set Cash")
        self.btn_set_cash.clicked.connect(self.prompt_set_cash)

        self.btn_settings = QPushButton("⚙️ Themes")
        self.btn_settings.clicked.connect(self.open_settings_dialog)

        self.btn_top10 = QPushButton("🏆 Top 10 Overview")
        self.btn_top10.clicked.connect(self.show_top10_overview)

        control_panel.addWidget(self.btn_ingest)
        control_panel.addWidget(self.btn_analyze)
        control_panel.addWidget(self.btn_manage_portfolio)
        control_panel.addWidget(self.btn_calc)
        control_panel.addWidget(self.btn_set_cash)
        control_panel.addWidget(self.btn_settings)
        control_panel.addWidget(self.btn_top10)
        layout.addLayout(control_panel)

        self.lbl_account_header = QLabel()
        self.lbl_account_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_account_header)

        self.lbl_disclosure = QLabel("⚠️ Educational tool, not investment advice. Sector Breadth, VWAP entries, and Sortino risk penalties applied.")
        self.lbl_disclosure.setWordWrap(True)
        layout.addWidget(self.lbl_disclosure)

        self.lbl_status = QLabel("System Idle. Ready for processing.")
        layout.addWidget(self.lbl_status)
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        filter_layout = QHBoxLayout()
        self.lbl_filter = QLabel("🔍 Live Filters:")
        self.lbl_filter.setStyleSheet("font-weight: bold; font-size: 13px;")
        filter_layout.addWidget(self.lbl_filter)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Search Ticker or keyword...")
        self.txt_search.textChanged.connect(self.apply_filters)
        filter_layout.addWidget(self.txt_search, stretch=2)

        self.cmb_action = QComboBox()
        self.cmb_action.addItems([
            "All Actions",
            "🔥 STRONG BUY",
            "⚡ BREAKOUT BUY",
            "📈 ACCUMULATE",
            "⏳ BUY ON DIP",
            "🟡 HOLD / NEUTRAL",
            "🛑 SELL / AVOID",
        ])
        self.cmb_action.currentTextChanged.connect(self.apply_filters)
        filter_layout.addWidget(self.cmb_action, stretch=1)

        self.cmb_trend = QComboBox()
        self.cmb_trend.addItems([
            "All Trends",
            "Strong Bullish",
            "Weak Bullish",
            "Consolidation / Neutral",
            "Weak Bearish",
            "Strong Bearish",
        ])
        self.cmb_trend.currentTextChanged.connect(self.apply_filters)
        filter_layout.addWidget(self.cmb_trend, stretch=1)

        self.cmb_confidence = QComboBox()
        self.cmb_confidence.addItems([
            "All Data Confidence",
            "High (1Y+)",
            "Medium (<1 Year)",
            "Low (<3 Months)",
            "Very Low (New/Short History)",
        ])
        self.cmb_confidence.currentTextChanged.connect(self.apply_filters)
        filter_layout.addWidget(self.cmb_confidence, stretch=1)

        self.chk_hide_illiquid = QPushButton("🚫 Hide Illiquid / Unconfirmed")
        self.chk_hide_illiquid.setCheckable(True)
        self.chk_hide_illiquid.setChecked(True)
        self.chk_hide_illiquid.clicked.connect(self.apply_filters)
        filter_layout.addWidget(self.chk_hide_illiquid)

        self.btn_columns = QPushButton("👁️ Columns")
        self.btn_columns.setStyleSheet("background-color: #4a5568; color: white;")
        self.btn_columns.clicked.connect(self.open_column_chooser)
        filter_layout.addWidget(self.btn_columns)

        self.btn_reset_filters = QPushButton("Reset Filters")
        self.btn_reset_filters.clicked.connect(self.reset_filters)
        filter_layout.addWidget(self.btn_reset_filters)
        layout.addLayout(filter_layout)

        self.tabs = QTabWidget()
        self.tbl_buys = self._create_matrix_table()
        self.tabs.addTab(self.tbl_buys, "📈 Full Market Action Matrix")

        self.tbl_sectors = QTableWidget()
        sector_cols = ["Sector", "Stocks", "1D Return (%)", "5D Return (%)", "Money Flow (CMF)", "Bullish Breadth (%)", "Traded Value (EGP)", "Sector Leader", "Sector Status"]
        self.tbl_sectors.setColumnCount(len(sector_cols))
        self.tbl_sectors.setHorizontalHeaderLabels(sector_cols)
        self.tbl_sectors.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.tbl_sectors, "🏢 Sector Heatmap & Rotation")

        self.tbl_exits = QTableWidget()
        exit_columns = [
            ("Ticker", "Stock ticker symbol"),
            ("Shares", "Shares currently held"),
            ("Buy Price", "Your average cost basis"),
            ("Price", "Current close price"),
            ("P&L (EGP)", "Unrealized profit/loss in EGP"),
            ("P&L (%)", "Unrealized profit/loss percentage"),
            ("Action", "Suggested action: hold/trail, take-profit zone, or cut-loss review"),
            ("Take-Profit", "Take-profit target"),
            ("Trail Stop", "Trailing stop-loss (2x ATR below current price)"),
            ("Trend", "Trend classification"),
            ("RSI-14", "14-period Relative Strength Index"),
            ("ADX-14", "14-period trend-strength index"),
            ("Data Conf.", "How much real history backs these numbers"),
            ("Purchase Date", "Date this position was opened"),
        ]
        self.tbl_exits.setColumnCount(len(exit_columns))
        for idx, (header, tooltip) in enumerate(exit_columns):
            item = QTableWidgetItem(header)
            item.setToolTip(tooltip)
            self.tbl_exits.setHorizontalHeaderItem(idx, item)
        self.tbl_exits.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_exits.horizontalHeader().setMinimumSectionSize(70)
        self.tabs.addTab(self.tbl_exits, "🛡️ Owned Portfolio Exit Strategy")

        self.tbl_breakout_watch = QTableWidget()
        breakout_watch_columns = [
            ("Ticker", "Stock ticker symbol"),
            ("Breakout Score", "0-100 composite pre-breakout score (higher = more setup elements aligned)"),
            ("Price", "Current close price"),
            ("Dist. to Resistance %", "% move still needed to reach the recent high"),
            ("RSI-14", "14-period Relative Strength Index"),
            ("ADX-14", "14-period trend-strength index"),
            ("Squeeze", "Bollinger Bands inside Keltner Channels — volatility compression, often precedes a move"),
            ("Volume Trend", "Is 5-day average volume rising vs. the prior 5 days"),
            ("Trend", "Trend classification"),
            ("Signals", "Which setup elements fired for this ticker"),
        ]
        self.tbl_breakout_watch.setColumnCount(len(breakout_watch_columns))
        for idx, (header, tooltip) in enumerate(breakout_watch_columns):
            item = QTableWidgetItem(header)
            item.setToolTip(tooltip)
            self.tbl_breakout_watch.setHorizontalHeaderItem(idx, item)
        self.tbl_breakout_watch.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_breakout_watch.horizontalHeader().setMinimumSectionSize(70)
        self.tabs.addTab(self.tbl_breakout_watch, "🎯 Breakout Watchlist")

        tab_history_widget = QWidget()
        history_layout = QVBoxLayout(tab_history_widget)
        
        btn_export = QPushButton("📥 Export Tax & Audit Ledger (Excel/CSV)")
        btn_export.setStyleSheet("background-color: #38a169; color: white; font-weight: bold; padding: 8px;")
        btn_export.clicked.connect(self.export_trade_ledger)
        history_layout.addWidget(btn_export)

        self.tbl_closed = QTableWidget()
        self.tbl_closed.setColumnCount(8)
        self.tbl_closed.setHorizontalHeaderLabels([
            "Ticker", "Shares Sold", "Buy Price", "Sell Price",
            "Realized P&L (EGP)", "Realized P&L (%)", "Purchase Date", "Sell Date",
        ])
        self.tbl_closed.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        history_layout.addWidget(self.tbl_closed)
        self.tabs.addTab(tab_history_widget, "📜 Trade History & Realized P&L")

        self.tbl_fin_stmt = QTableWidget()
        self.tbl_fin_stmt.setColumnCount(2)
        self.tbl_fin_stmt.setHorizontalHeaderLabels(["Accounting Metric / Line Item", "Value (EGP / %)"])
        self.tbl_fin_stmt.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.tbl_fin_stmt, "📊 Financial Statement & Account Summary")

        self.tbl_top_strong = self._create_matrix_table()
        self.tbl_top_breakout = self._create_matrix_table()
        self.tbl_top_accum = self._create_matrix_table()
        self.tbl_top_dip = self._create_matrix_table()
        self.top10_overview_widget = self._build_top10_overview_tab()
        self.tabs.addTab(self.top10_overview_widget, "🏆 Top 10 Overview")

        self.chart_widget = StockSectorChartWidget(self.qe, self.dbm, self)
        self.tabs.addTab(self.chart_widget, "📊 Charts & Trend Lines")

        layout.addWidget(self.tabs)
        self.update_last_data_date_display()
        self.refresh_account_header()

    def open_column_chooser(self):
        current_idx = self.tabs.currentIndex()
        view = None
        if current_idx == 0:
            view = self.tbl_buys
        elif current_idx == 1:
            view = self.tbl_sectors
        elif current_idx == 2:
            view = self.tbl_exits
        elif current_idx == 3:
            view = self.tbl_closed
        elif current_idx == 4:
            view = self.tbl_fin_stmt
        elif current_idx == 5:
            view = self.tbl_top_strong
            
        if view:
            dlg = ColumnChooserDialog(view, self.current_lang, self)
            dlg.exec()
        else:
            msg = "Column selection is not applicable for this tab." if self.current_lang == "EN" else "تحديد الأعمدة غير متاح في هذه التبويبة."
            QMessageBox.information(self, "Columns", msg)

    def update_last_data_date_display(self):
        last_date = self.dbm.get_latest_market_date()
        t = TRANSLATIONS[self.current_lang]
        self.lbl_last_date.setText(f"{t['last_date']} {last_date}")

    def switch_language(self, index):
        self.current_lang = "AR" if index == 1 else "EN"
        t = TRANSLATIONS[self.current_lang]

        if self.current_lang == "AR":
            QApplication.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        else:
            QApplication.setLayoutDirection(Qt.LayoutDirection.LeftToRight)

        self.setWindowTitle(t["title"])
        self.lbl_dir.setText(t["scan_folder"])
        self.btn_browse.setText(t["browse"])
        self.btn_ingest.setText(t["ingest"])
        self.btn_analyze.setText(t["analyze"])
        self.btn_manage_portfolio.setText(t["portfolio"])
        self.btn_calc.setText(t["risk_calc"])
        self.btn_set_cash.setText(t["set_cash"])
        self.btn_settings.setText(t["themes"])
        self.btn_top10.setText(t["top10_btn"])
        self.lbl_filter.setText(t["filters"])
        self.txt_search.setPlaceholderText(t["search_ph"])
        self.chk_hide_illiquid.setText(t["hide_illiquid"])
        self.btn_columns.setText(t["btn_columns"])
        self.btn_reset_filters.setText(t["reset_filters"])

        self.tabs.setTabText(0, t["tab_matrix"])
        self.tabs.setTabText(1, t["tab_sectors"])
        self.tabs.setTabText(2, t["tab_exits"])
        self.tabs.setTabText(3, t["tab_history"])
        self.tabs.setTabText(4, t["tab_fin"])
        self.tabs.setTabText(5, t["tab_top10"])
        self.tabs.setTabText(6, t["tab_charts"])

        if hasattr(self, "chart_widget"):
            self.chart_widget.set_language(self.current_lang)
        self.update_last_data_date_display()
        self.refresh_account_header()

    def _create_matrix_table(self):
        tbl = QTableView()
        model = MatrixTableModel()
        tbl.setModel(model)
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        tbl.horizontalHeader().setMinimumSectionSize(70)
        tbl.verticalHeader().setVisible(False)
        tbl.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        tbl.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        return tbl

    def _build_top10_overview_tab(self):
        container = QWidget()
        v_layout = QVBoxLayout(container)
        v_layout.setSpacing(4)

        sections = [
            ("🔥 Top 10 Strong Buy", self.tbl_top_strong),
            ("⚡ Top 10 Breakout", self.tbl_top_breakout),
            ("📈 Top 10 Accumulate", self.tbl_top_accum),
            ("⏳ Top 10 Buy on Dip", self.tbl_top_dip),
        ]
        for title, tbl in sections:
            section_label = QLabel(title)
            section_label.setStyleSheet("font-weight: bold; font-size: 14px; padding: 6px 2px 2px 2px;")
            v_layout.addWidget(section_label)
            tbl.setMinimumHeight(260)
            tbl.setMaximumHeight(260)
            v_layout.addWidget(tbl)

        v_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        return scroll

    def show_top10_overview(self):
        self.tabs.setCurrentWidget(self.top10_overview_widget)

    def browse_folder(self):
        selected_dir = QFileDialog.getExistingDirectory(self, "Select Folder", self.txt_scan_dir.text())
        if selected_dir:
            self.txt_scan_dir.setText(selected_dir)

    def open_portfolio_dialog(self):
        dlg = PortfolioDialog(self)
        dlg.exec()
        self.start_analysis()

    def open_calculator_dialog(self):
        current_cash = self.dbm.get_cash_balance()
        dlg = PositionSizingDialog(self.dbm, current_cash, self)
        dlg.exec()

    def open_settings_dialog(self):
        dlg = ThemeSettingsDialog(self.current_theme, self.apply_theme, self)
        dlg.exec()

    def prompt_set_cash(self):
        current_cash = self.dbm.get_cash_balance()
        val, ok = QInputDialog.getDouble(self, "Set Account Cash Balance", "Enter available cash balance in EGP:", current_cash, 0.0, 1000000000.0, 2)
        if ok:
            self.dbm.set_cash_balance(val)
            QMessageBox.information(self, "Cash Updated", f"Account cash balance successfully updated to: {val:,.2f} EGP.")
            self.start_analysis()

    def export_trade_ledger(self):
        all_trades = self.dbm.get_all_closed_trades()
        trades = [t for t in all_trades if not t.get("is_demo")]
        n_demo_excluded = len(all_trades) - len(trades)
        if not trades:
            msg = "No real (non-demo) closed trades available to export."
            if n_demo_excluded:
                msg += f"\n({n_demo_excluded} demo trade(s) were excluded.)"
            QMessageBox.warning(self, "Export Error", msg)
            return
        
        file_path, _ = QFileDialog.getSaveFileName(self, "Export Audit Ledger", "Trade_Audit_Ledger.csv", "CSV Files (*.csv);;Excel Files (*.xlsx)")
        if not file_path:
            return
        
        import pandas as pd
        df = pd.DataFrame(trades)
        if "is_demo" in df.columns:
            df = df.drop(columns=["is_demo"])
        
        total_trades = len(df)
        winning_trades = len(df[df["Realized P&L (EGP)"] > 0])
        win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0.0
        
        gross_gains = df[df["Realized P&L (EGP)"] > 0]["Realized P&L (EGP)"].sum()
        gross_losses = abs(df[df["Realized P&L (EGP)"] < 0]["Realized P&L (EGP)"].sum())
        profit_factor = (gross_gains / gross_losses) if gross_losses > 0 else (gross_gains if gross_gains > 0 else 0.0)
        net_pnl = df["Realized P&L (EGP)"].sum()
        
        summary_rows = pd.DataFrame([
            {},
            {"Ticker": "=== AUDIT SUMMARY ==="},
            {"Ticker": "Total Trades Executed:", "Shares Sold": total_trades},
            {"Ticker": "Winning Trades:", "Shares Sold": winning_trades, "Buy Price": f"Win Rate: {win_rate:.2f}%"},
            {"Ticker": "Gross Realized Gains:", "Realized P&L (EGP)": round(gross_gains, 2)},
            {"Ticker": "Gross Realized Losses:", "Realized P&L (EGP)": round(-gross_losses, 2)},
            {"Ticker": "Profit Factor (Gains/Losses):", "Realized P&L (EGP)": round(profit_factor, 2)},
            {"Ticker": "NET REALIZED P&L (EGP):", "Realized P&L (EGP)": round(net_pnl, 2)}
        ])
        
        df_export = pd.concat([df, summary_rows], ignore_index=True)
        
        try:
            if file_path.endswith(".xlsx"):
                df_export.to_excel(file_path, index=False)
            else:
                df_export.to_csv(file_path, index=False)
            QMessageBox.information(self, "Export Successful", f"Audit ledger successfully saved to:\n{file_path}\n\nWin Rate: {win_rate:.1f}% | Profit Factor: {profit_factor:.2f}")
        except Exception as e:
            QMessageBox.critical(self, "Export Failed", f"Could not save file:\n{str(e)}")

    def refresh_account_header(self, fin_stmt=None):
        t = TRANSLATIONS[self.current_lang]
        if not fin_stmt:
            cash = self.dbm.get_cash_balance()
            self.lbl_account_header.setText(f"{t['cash_lbl']} {cash:,.2f} EGP | {t['port_val']} 0.00 EGP | {t['equity_lbl']} {cash:,.2f} EGP")
        else:
            cash = fin_stmt.get("Cash Balance (EGP)", 0.0)
            stock_val = fin_stmt.get("Stock Portfolio Market Value (EGP)", 0.0)
            total = fin_stmt.get("Total Account Equity / Net Worth (EGP)", cash + stock_val)
            self.lbl_account_header.setText(f"{t['cash_lbl']} {cash:,.2f} EGP | {t['port_val']} {stock_val:,.2f} EGP | {t['equity_lbl']} {total:,.2f} EGP")

    def reset_filters(self):
        self.txt_search.clear()
        self.cmb_action.setCurrentIndex(0)
        self.cmb_trend.setCurrentIndex(0)
        self.cmb_confidence.setCurrentIndex(0)
        self.chk_hide_illiquid.setChecked(True)
        self.apply_filters()

    def _set_ui_controls_enabled(self, enabled: bool):
        self.btn_ingest.setEnabled(enabled)
        self.btn_analyze.setEnabled(enabled)
        self.btn_manage_portfolio.setEnabled(enabled)
        self.btn_calc.setEnabled(enabled)
        self.btn_set_cash.setEnabled(enabled)
        self.btn_settings.setEnabled(enabled)

    def start_ingestion(self):
        target_directory = self.txt_scan_dir.text().strip()
        if not os.path.exists(target_directory):
            QMessageBox.warning(self, "Invalid Folder", f"The directory does not exist:\n{target_directory}")
            return
        self._set_ui_controls_enabled(False)
        self.ingest_worker = IngestionWorker(target_dir=target_directory)
        self.ingest_worker.progress_signal.connect(self.update_progress)
        self.ingest_worker.finished_signal.connect(self.ingestion_done)
        self.ingest_worker.start()

    def ingestion_done(self):
        self._set_ui_controls_enabled(True)
        self.lbl_status.setText("⚡ Ingestion successfully flushed to DuckDB.")
        self.update_last_data_date_display()

    def start_analysis(self):
        self._set_ui_controls_enabled(False)
        self.analysis_worker = AnalysisWorker()
        self.analysis_worker.progress_signal.connect(self.update_progress)
        self.analysis_worker.results_signal.connect(self.populate_tables)
        self.analysis_worker.start()

    def update_progress(self, pct, msg):
        self.progress_bar.setValue(pct)
        self.lbl_status.setText(msg)

    def populate_tables(self, buys, exits, top10, closed_trades, fin_stmt, sector_summary, breakout_watchlist=None):
        breakout_watchlist = breakout_watchlist or []
        self._set_ui_controls_enabled(True)
        self.lbl_status.setText("✅ Quantitative signal matrix & sector heatmaps successfully updated.")
        self.refresh_account_header(fin_stmt)
        self.update_last_data_date_display()
        self._raw_buys_data = buys

        if self.user_info:
            stats = self._compute_dealing_stats(exits, closed_trades, fin_stmt)
            self._run_cloud(push_dealing_stats, self.user_info["idToken"], self.user_info["uid"], stats)

        for tbl in [self.tbl_sectors, self.tbl_exits, self.tbl_closed, self.tbl_fin_stmt, self.tbl_breakout_watch]:
            tbl.setUpdatesEnabled(False)

        try:
            self._fill_matrix_table(self.tbl_buys, buys)
            self._fill_matrix_table(self.tbl_top_strong, top10.get("🔥 STRONG BUY", []))
            self._fill_matrix_table(self.tbl_top_breakout, top10.get("⚡ BREAKOUT BUY", []))
            self._fill_matrix_table(self.tbl_top_accum, top10.get("📈 ACCUMULATE", []))
            self._fill_matrix_table(self.tbl_top_dip, top10.get("⏳ BUY ON DIP", []))

            self.tbl_sectors.setRowCount(len(sector_summary))
            for row_idx, row_data in enumerate(sector_summary):
                for col_idx, key in enumerate(["Sector", "Stocks", "1D Return (%)", "5D Return (%)", "Money Flow (CMF)", "Bullish Breadth (%)", "Traded Value (EGP)", "Sector Leader", "Sector Status"]):
                    val = row_data.get(key, "")
                    val_str = f"{val:,.2f}" if isinstance(val, float) and "Return" not in key and "Flow" not in key else str(val)
                    item = QTableWidgetItem(val_str)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                    if "Return" in key or key == "Money Flow (CMF)":
                        try:
                            v_num = float(val)
                            if v_num > 0:
                                item.setForeground(QColor("#38a169"))
                                item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                            elif v_num < 0:
                                item.setForeground(QColor("#e53e3e"))
                                item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                        except ValueError:
                            pass

                    if key == "Sector Status":
                        if "STRONG INFLOW" in val_str or "BREAKOUT" in val_str:
                            item.setBackground(QColor("#276749"))
                            item.setForeground(Qt.GlobalColor.white)
                        elif "HEAVY DISTRIBUTION" in val_str:
                            item.setBackground(QColor("#9b2c2c"))
                            item.setForeground(Qt.GlobalColor.white)
                        elif "ACCUMULATE" in val_str:
                            item.setBackground(QColor("#2b6cb0"))
                            item.setForeground(Qt.GlobalColor.white)

                    self.tbl_sectors.setItem(row_idx, col_idx, item)

            self.tbl_exits.setRowCount(len(exits))
            for row_idx, row_data in enumerate(exits):
                for col_idx, key in enumerate([
                    "Ticker", "Shares", "Buy Price", "Current Price", "P&L (EGP)", "P&L (%)",
                    "Action Command", "Take-Profit Target", "Trailing Stop-Loss", "Trend Class",
                    "RSI-14", "ADX-14", "Data Confidence", "Purchase Date",
                ]):
                    val_str = str(row_data.get(key, ""))
                    item = QTableWidgetItem(val_str)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                    if key in ["P&L (EGP)", "P&L (%)"]:
                        try:
                            val_num = float(row_data.get(key, 0))
                            if val_num > 0:
                                item.setForeground(QColor("#38a169"))
                                item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                            elif val_num < 0:
                                item.setForeground(QColor("#e53e3e"))
                                item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                        except ValueError:
                            pass

                    if key == "Action Command":
                        if "URGENT SELL" in val_str or "CUT LOSS" in val_str:
                            item.setBackground(QColor("#9b2c2c"))
                            item.setForeground(Qt.GlobalColor.white)
                        elif "TAKE PROFIT" in val_str:
                            item.setBackground(QColor("#22543d"))
                            item.setForeground(Qt.GlobalColor.white)
                        elif "HOLD" in val_str:
                            item.setBackground(QColor("#975a16"))
                            item.setForeground(Qt.GlobalColor.white)

                    self.tbl_exits.setItem(row_idx, col_idx, item)

            self.tbl_closed.setRowCount(len(closed_trades))
            for row_idx, row_data in enumerate(closed_trades):
                for col_idx, key in enumerate([
                    "Ticker", "Shares Sold", "Buy Price", "Sell Price",
                    "Realized P&L (EGP)", "Realized P&L (%)", "Purchase Date", "Sell Date",
                ]):
                    val_str = str(row_data.get(key, ""))
                    item = QTableWidgetItem(val_str)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                    if key in ["Realized P&L (EGP)", "Realized P&L (%)"]:
                        try:
                            val_num = float(row_data.get(key, 0))
                            if val_num > 0:
                                item.setForeground(QColor("#38a169"))
                                item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                            elif val_num < 0:
                                item.setForeground(QColor("#e53e3e"))
                                item.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                        except ValueError:
                            pass
                    self.tbl_closed.setItem(row_idx, col_idx, item)

            self.tbl_fin_stmt.setRowCount(len(fin_stmt))
            for row_idx, (metric_name, val_num) in enumerate(fin_stmt.items()):
                item_name = QTableWidgetItem(metric_name)
                item_name.setFlags(item_name.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item_name.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))

                val_str = f"{val_num:,.2f}" if "%" not in metric_name else f"{val_num:,.2f}%"
                item_val = QTableWidgetItem(val_str)
                item_val.setFlags(item_val.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item_val.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item_val.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))

                if "P&L" in metric_name:
                    if val_num > 0:
                        item_val.setForeground(QColor("#38a169"))
                    elif val_num < 0:
                        item_val.setForeground(QColor("#e53e3e"))
                elif "Total Account Equity" in metric_name:
                    item_name.setBackground(self.theme_highlight)
                    item_val.setBackground(self.theme_highlight)
                    item_val.setForeground(Qt.GlobalColor.white)

                self.tbl_fin_stmt.setItem(row_idx, 0, item_name)
                self.tbl_fin_stmt.setItem(row_idx, 1, item_val)

            self.tbl_breakout_watch.setRowCount(len(breakout_watchlist))
            for row_idx, row_data in enumerate(breakout_watchlist):
                for col_idx, key in enumerate([
                    "Ticker", "Breakout Score", "Current Price", "Dist. to Resistance (%)",
                    "RSI-14", "ADX-14", "Squeeze Active", "Volume Trend", "Trend Class", "Signals",
                ]):
                    val = row_data.get(key, "")
                    if key == "Squeeze Active":
                        val_str = "✅ Yes" if val else "—"
                    else:
                        val_str = str(val)
                    item = QTableWidgetItem(val_str)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if key != "Signals":
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                    if key == "Breakout Score":
                        try:
                            score_num = float(val)
                            if score_num >= 70:
                                item.setBackground(QColor("#276749"))
                                item.setForeground(Qt.GlobalColor.white)
                            elif score_num >= 55:
                                item.setBackground(QColor("#2b6cb0"))
                                item.setForeground(Qt.GlobalColor.white)
                        except (TypeError, ValueError):
                            pass

                    self.tbl_breakout_watch.setItem(row_idx, col_idx, item)
        finally:
            for tbl in [self.tbl_sectors, self.tbl_exits, self.tbl_closed, self.tbl_fin_stmt, self.tbl_breakout_watch]:
                tbl.setUpdatesEnabled(True)

        if hasattr(self, "chart_widget"):
            self.chart_widget.populate_selector()
        self.apply_filters()

    def _fill_matrix_table(self, table_view, data_list):
        model = table_view.model()
        if hasattr(model, "update_data"):
            model.update_data(data_list)

    def apply_filters(self):
        search_text = self.txt_search.text().strip().upper()
        action_filter = self.cmb_action.currentText()
        trend_filter = self.cmb_trend.currentText()
        confidence_filter = self.cmb_confidence.currentText()
        hide_illiquid = self.chk_hide_illiquid.isChecked()

        if hasattr(self, "_raw_buys_data") and self._raw_buys_data:
            filtered_list = []
            for row in self._raw_buys_data:
                ticker_text = str(row.get("Ticker", "")).upper()
                action_text = str(row.get("Action", ""))
                trend_text = str(row.get("Trend Class", ""))
                confidence_text = str(row.get("Data Confidence", ""))

                match_search = (search_text in ticker_text) if search_text else True
                match_action = ((action_filter in action_text) if action_filter != "All Actions" else True)
                match_trend = ((trend_text == trend_filter) if trend_filter != "All Trends" else True)
                match_confidence = ((confidence_text == confidence_filter) if confidence_filter != "All Data Confidence" else True)
                match_liquidity = (("ILLIQUID" not in action_text and "Unconfirmed" not in action_text) if hide_illiquid else True)

                if match_search and match_action and match_trend and match_confidence and match_liquidity:
                    filtered_list.append(row)
            
            self._fill_matrix_table(self.tbl_buys, filtered_list)


def _show_fatal_error(title, message):
    """Surfaces a fatal error even when launched via pythonw.exe, which has
    no console to print a traceback to. Tries a Qt dialog first, then falls
    back to a raw Windows message box as a last resort."""
    try:
        QMessageBox.critical(None, title, message)
        return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    except Exception:
        pass


def _install_excepthook():
    def _hook(exc_type, exc_value, exc_tb):
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error(f"Unhandled exception:\n{tb_text}")
        _show_fatal_error(
            "MB-EGX — Unexpected Error",
            f"{exc_value}\n\nFull details were written to quant_app.log."
        )
    sys.excepthook = _hook


if __name__ == "__main__":
    app = QApplication(sys.argv)
    _install_excepthook()  # catches crashes during the Qt event loop

    try:
        login = LoginDialog()
        if login.exec() != QDialog.DialogCode.Accepted or not login.user_info:
            sys.exit(0)

        window = QuantDashboard(user_info=login.user_info)
        window.show()
        sys.exit(app.exec())
    except SystemExit:
        raise
    except Exception:
        # Catches crashes during startup itself (before the event loop runs).
        tb_text = traceback.format_exc()
        logger.error(f"Fatal startup error:\n{tb_text}")
        _show_fatal_error(
            "MB-EGX — Failed to Start",
            f"{tb_text[-1200:]}\n\nFull details were written to quant_app.log."
        )
        sys.exit(1)
