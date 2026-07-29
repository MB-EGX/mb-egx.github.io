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
from PyQt6.QtGui import QFont, QColor, QPixmap, QIcon
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QDateEdit, QDialog, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QTableView, QTabWidget, QVBoxLayout, QWidget,
    QCheckBox, QTextEdit, QSizePolicy, QRadioButton, QFrame
)

logger = get_logger("app_gui")

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "mb-egx-logo.png"
# Fallback to jpg if png doesn't exist based on your structure
if not LOGO_PATH.exists():
    LOGO_PATH = Path(__file__).resolve().parent / "mb-egx-logo.jpg"
if not LOGO_PATH.exists():
    LOGO_PATH = Path("mb-egx-logo.jpg")

# =============================================================================
# FIREBASE AUTH + FIRESTORE (REST)
# =============================================================================
FIREBASE_API_KEY = "AIzaSyBCC4D61IHTEFNsgO6i8H_BdixwArE-VRo"
FIREBASE_PROJECT_ID = "mb-egx-12d11"
FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents"
ADMIN_EMAILS = ["drmo071990@gmail.com"]

TERMS_VERSION = "1.0"

DISCLAIMER_TEXT = (
    "End-User Consent and Legal Disclaimer:\n\n"
    "By accessing, subscribing to, or using this application, you explicitly "
    "acknowledge, understand, and agree to the following terms:\n\n"
    "• Informational and Educational Use Only: This app is for informational "
    "and educational purposes only. It does not constitute financial or "
    "investment advice.\n\n"
    "• Nature of the Tools: The service operates by providing educational and "
    "analytical tools, effectively giving you a mirror to look at the market. "
    "The outputs are generated via raw data, charts, historical trends, "
    "mathematical calculations, quantitative indicators, and automated "
    "technical screening tools.\n\n"
    "• No Unlicensed Financial Advisory: The application and its creators do "
    "not direct a user's specific actions, nor do they act as a portfolio "
    "manager. Giving direct investment advice or portfolio management "
    "requires rigorous licensing from the Egyptian Financial Regulatory "
    "Authority (FRA), which this software does not provide.\n\n"
    "• Analytics, Not Commands: All signals and feature outputs provided by "
    "the software are strictly classified as analytics, such as a "
    "'Quantitative Indicator Output' or 'Technical Pattern Matcher'. They are "
    "never to be interpreted as direct 'Buy/Sell Recommendations' or market "
    "commands. The application provides the data, and you must independently "
    "decide what to do with it.\n\n"
    "• Assumption of Risk: Users are solely responsible for their own trading "
    "decisions.\n\n"
    "• No Handling of Client Funds: This application functions strictly as a "
    "Software as a Service (SaaS) analytics tool. We will never request, "
    "hold, or allow users to deposit trading capital or funds into our bank "
    "accounts or app wallets.\n\n"
    "• Third-Party Trade Execution: You cannot execute trades independently "
    "through this app; all users must execute their actual trades through "
    "approved and licensed EGX brokers (such as Thndr, EFG Hermes, etc.)."
)

def fetch_client_ip():
    if requests is None:
        return None
    try:
        resp = requests.get("https://api.ipify.org?format=json", timeout=5)
        if resp.status_code == 200:
            return resp.json().get("ip")
    except Exception:
        pass
    return None

def write_consent_doc(id_token, uid, ip_address):
    if requests is None:
        return
    now = _now_iso()
    url = f"{FIRESTORE_BASE}/users/{uid}?updateMask.fieldPaths=cash&updateMask.fieldPaths=portfolio&updateMask.fieldPaths=history&updateMask.fieldPaths=agreed_to_terms&updateMask.fieldPaths=terms_version&updateMask.fieldPaths=agreed_at&updateMask.fieldPaths=ip_address"
    body = {"fields": {
        "cash": {"doubleValue": 0.0},
        "portfolio": {"arrayValue": {}},
        "history": {"arrayValue": {}},
        "agreed_to_terms": {"booleanValue": True},
        "terms_version": {"stringValue": TERMS_VERSION},
        "agreed_at": {"timestampValue": now},
        "ip_address": {"stringValue": ip_address} if ip_address else {"nullValue": None},
    }}
    headers = {"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"}
    try:
        requests.patch(url, headers=headers, json=body, timeout=10)
    except Exception as e:
        logger.warning(f"Could not write consent doc for {uid}: {e}")

def _from_firestore_value(v):
    if "stringValue" in v: return v["stringValue"]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue" in v: return float(v["doubleValue"])
    if "booleanValue" in v: return v["booleanValue"]
    if "timestampValue" in v: return v["timestampValue"] 
    if "nullValue" in v: return None
    if "mapValue" in v: return {k: _from_firestore_value(val) for k, val in v["mapValue"].get("fields", {}).items()}
    if "arrayValue" in v: return [_from_firestore_value(x) for x in v["arrayValue"].get("values", [])]
    return None

def _doc_to_dict(doc):
    return {k: _from_firestore_value(v) for k, v in doc.get("fields", {}).items()}

def _parse_ts(ts):
    if not ts: return None
    try: return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError: return None

def list_recent_sessions(id_token, limit=1000):
    headers = {"Authorization": f"Bearer {id_token}"}
    docs = []
    page_token = None
    while len(docs) < limit:
        params = {"pageSize": min(300, limit - len(docs)), "orderBy": "start desc"}
        if page_token: params["pageToken"] = page_token
        resp = requests.get(f"{FIRESTORE_BASE}/sessions", headers=headers, params=params, timeout=15)
        data = resp.json()
        if resp.status_code != 200: raise RuntimeError(data.get("error", {}).get("message", "Failed to list sessions."))
        batch = data.get("documents", [])
        docs.extend(batch)
        page_token = data.get("nextPageToken")
        if not page_token or not batch: break
    return [_doc_to_dict(d) for d in docs[:limit]]

def get_user_doc(id_token, uid):
    headers = {"Authorization": f"Bearer {id_token}"}
    resp = requests.get(f"{FIRESTORE_BASE}/users/{uid}", headers=headers, timeout=10)
    if resp.status_code == 404: return {}
    data = resp.json()
    if resp.status_code != 200: raise RuntimeError(data.get("error", {}).get("message", "Failed to load user doc."))
    return _doc_to_dict(data)

def compute_usage_analytics(id_token):
    sessions = list_recent_sessions(id_token, limit=1000)
    per_user = {}
    total_duration = 0.0
    session_count = 0

    for s in sessions:
        start_dt = _parse_ts(s.get("start"))
        last_dt = _parse_ts(s.get("lastSeen"))
        if not start_dt or not last_dt: continue
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
        if last_dt > entry["last_seen"]: entry["last_seen"] = last_dt

    for entry in per_user.values():
        uid = entry.get("uid")
        if not uid: continue
        try: udoc = get_user_doc(id_token, uid)
        except Exception: continue
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
    if total_seconds is None or total_seconds < 0: return "—"
    mins = round(total_seconds / 60)
    if mins < 60: return f"{mins}m"
    return f"{mins // 60}h {mins % 60}m"

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

def firebase_sign_in(email, password):
    if requests is None: raise RuntimeError("requests package required.")
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    resp = requests.post(url, json={"email": email, "password": password, "returnSecureToken": True}, timeout=10)
    data = resp.json()
    if resp.status_code != 200: raise RuntimeError(data.get("error", {}).get("message", "Sign-in failed."))
    return data

def firebase_sign_up(email, password):
    if requests is None: raise RuntimeError("requests package required.")
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_API_KEY}"
    resp = requests.post(url, json={"email": email, "password": password, "returnSecureToken": True}, timeout=10)
    data = resp.json()
    if resp.status_code != 200: raise RuntimeError(data.get("error", {}).get("message", "Sign-up failed."))
    return data

def firebase_send_password_reset(email):
    if requests is None: raise RuntimeError("requests package required.")
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={FIREBASE_API_KEY}"
    resp = requests.post(url, json={"requestType": "PASSWORD_RESET", "email": email}, timeout=10)
    data = resp.json()
    if resp.status_code != 200: raise RuntimeError(data.get("error", {}).get("message", "Could not send reset email."))
    return data

def create_session_doc(id_token, uid, email, name):
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
    url = f"{FIRESTORE_BASE}/sessions/{session_id}?updateMask.fieldPaths=lastSeen"
    body = {"fields": {"lastSeen": {"timestampValue": _now_iso()}}}
    headers = {"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"}
    requests.patch(url, headers=headers, json=body, timeout=10)

def push_dealing_stats(id_token, uid, stats):
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
    finished_result = pyqtSignal(object)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try: result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            logger.warning(f"Cloud sync call failed: {e}")
            result = None
        self.finished_result.emit(result)

# Translations and Themes truncated for brevity but they remain identical to your version
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
        "tab_matrix": "📈 Action Matrix",
        "tab_sectors": "🏢 Sectors",
        "tab_exits": "🛡️ Exits",
        "tab_breakout": "🎯 Breakouts",
        "tab_history": "📜 History",
        "tab_fin": "📊 Financials",
        "tab_top10": "🏆 Top 10",
        "tab_charts": "📊 Charts",
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
        "tab_matrix": "📈 مصفوفة السوق",
        "tab_sectors": "🏢 القطاعات",
        "tab_exits": "🛡️ التخارج",
        "tab_breakout": "🎯 الاختراقات",
        "tab_history": "📜 سجل الصفقات",
        "tab_fin": "📊 الماليات",
        "tab_top10": "🏆 أفضل 10",
        "tab_charts": "📊 رسوم بيانية",
        "last_date": "📅 تاريخ أحدث بيانات:",
        "cash_lbl": "💵 الرصيد النقدي:",
        "port_val": "📈 قيمة محفظة الأسهم:",
        "equity_lbl": "🏛️ إجمالي حقوق الحساب:",
    }
}

THEME_DARK = """
    QMainWindow, QDialog, QWidget#main_widget { background-color: #0f1115; color: #e2e2e8; font-family: 'Inter', 'Segoe UI', Arial, sans-serif; }
    QWidget#webPanel, QWidget#filterPanel { background-color: #1a1d24; border: 1px solid #2d3748; border-radius: 8px; }
    QTabWidget::pane { border: 1px solid #2d3748; background-color: #1a1d24; border-radius: 8px; margin-top: -1px; }
    QTabBar::tab { background: transparent; color: #a0aec0; padding: 6px 10px; font-size: 12px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; font-weight: bold; border: 1px solid transparent; }
    QTabBar::tab:selected { background-color: #3198dc; color: #ffffff; border: 1px solid #2d3748; border-bottom: none; }
    QTabBar::tab:hover:!selected { color: #ffffff; background-color: rgba(255, 255, 255, 0.05); }
    QTableWidget, QTableView { background-color: #1a1d24; alternate-background-color: #15181e; color: #e2e2e8; gridline-color: #2d3748; border: none; border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; selection-background-color: rgba(49, 152, 220, 0.2); selection-color: #ffffff; outline: none; }
    QHeaderView { background-color: #2d3748; border: none; }
    QTableCornerButton::section { background-color: #2d3748; border: none; }
    QHeaderView::section { background-color: #2d3748; color: #93ccff; padding: 6px; font-weight: bold; font-size: 11px; letter-spacing: 1px; border: none; border-bottom: 2px solid #0f1115; }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background-color: #0f1115; color: #ffffff; border: 1px solid #2d3748; padding: 4px 8px; border-radius: 4px; }
    QLineEdit:focus, QComboBox:focus { border: 1px solid #3198dc; }
    QPushButton { background-color: #2d3748; color: #ffffff; border: none; border-radius: 4px; padding: 4px; font-weight: bold; font-size: 11px; }
    QPushButton:hover { background-color: #3a4557; }
    QPushButton:pressed { background-color: #232b38; }
    QRadioButton { color: #e2e2e8; font-weight: bold; font-size: 12px; spacing: 6px; }
    QRadioButton::indicator { width: 14px; height: 14px; border-radius: 7px; border: 1px solid #2d3748; background-color: #0f1115; }
    QRadioButton::indicator:checked { background-color: #3198dc; border: 1px solid #93ccff; }
    QLabel { color: #e2e2e8; }
    QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget { background-color: #0f1115; border: none; }
    QProgressBar { border: none; background-color: rgba(255,255,255,0.1); border-radius: 2px; height: 4px; max-height: 4px; }
    QProgressBar::chunk { background-color: #3198dc; border-radius: 2px; }
    QScrollBar:vertical { background: #0f1115; width: 14px; margin: 0px; }
    QScrollBar::handle:vertical { background: #2d3748; border-radius: 7px; min-height: 20px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar:horizontal { background: #0f1115; height: 14px; margin: 0px; }
    QScrollBar::handle:horizontal { background: #2d3748; border-radius: 7px; min-width: 20px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QAbstractScrollArea::corner { background-color: #0f1115; }
"""
THEMES_MAP = { "🌙 Institutional Dark": THEME_DARK }

class MatrixTableModel(QAbstractTableModel):
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        self._data = data or []
        self._columns = [
            ("Ticker", "Stock ticker symbol"), ("Action", "Recommended action"), ("Score", "Composite rank score"),
            ("Price", "Current close price"), ("Entry (VWAP)", "Suggested entry price"), ("Stop-Loss", "Suggested stop-loss"),
            ("Shares (1% Risk)", "Suggested shares"), ("Proj. Gain %", "Projected return"), ("Pattern Conf %", "Confidence"),
            ("Trend", "Trend classification"), ("RSI-14", "RSI"), ("ADX-14", "ADX"), ("Vol Z-Score", "Vol Z-Score"),
            ("Avg Vol (20D)", "Avg Volume"), ("Data Conf.", "Data Confidence"),
        ]
        self._col_keys = [
            "Ticker", "Action", "Rank Score", "Current Price", "Target Entry (VWAP)", "Suggested Stop-Loss",
            "Suggested Shares (1% Risk)", "Projected Gain (%)", "Pattern Conf (%)", "Trend Class", "RSI-14",
            "ADX-14", "Vol Z-Score", "Avg Volume (20D)", "Data Confidence",
        ]

    def rowCount(self, parent=QModelIndex()): return len(self._data)
    def columnCount(self, parent=QModelIndex()): return len(self._columns)
    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole: return self._columns[section][0]
            elif role == Qt.ItemDataRole.ToolTipRole: return self._columns[section][1]
        return None
    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid(): return None
        row, col = index.row(), index.column()
        key = self._col_keys[col]
        val = self._data[row].get(key, "")
        val_str = str(val)
        if role == Qt.ItemDataRole.DisplayRole: return val_str
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if key in ["Action", "Data Confidence"]: return int(Qt.AlignmentFlag.AlignCenter)
            elif 2 <= col <= 8: return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.BackgroundRole:
            if key == "Action":
                if "ILLIQUID" in val_str or "Unconfirmed" in val_str: return QColor("#4a5568")
                elif "STRONG BUY" in val_str or "BREAKOUT BUY" in val_str: return QColor("#276749")
                elif "ACCUMULATE" in val_str or "BUY ON DIP" in val_str: return QColor("#2b6cb0")
                elif "SELL / AVOID" in val_str: return QColor("#9b2c2c")
                elif "HOLD" in val_str: return QColor("#975a16")
        elif role == Qt.ItemDataRole.ForegroundRole:
            if key == "Action" and any(w in val_str for w in ["ILLIQUID", "Unconfirmed", "STRONG BUY", "BREAKOUT BUY", "ACCUMULATE", "BUY ON DIP", "SELL / AVOID", "HOLD"]): return QColor("#ffffff")
            elif key == "Data Confidence":
                if "Very Low" in val_str: return QColor("#e53e3e")
                elif "Low" in val_str: return QColor("#dd6b20")
        elif role == Qt.ItemDataRole.FontRole:
            if key == "Action" and ("STRONG BUY" in val_str or "BREAKOUT BUY" in val_str): return QFont("Inter", 9, QFont.Weight.Bold)
        return None
    def update_data(self, new_data):
        self.beginResetModel()
        self._data = new_data
        self.endResetModel()

class ColumnChooserDialog(QDialog):
    def __init__(self, table_view, lang="EN", parent=None):
        super().__init__(parent)
        self.table_view = table_view
        self.lang = lang
        self.setWindowTitle(TRANSLATIONS[lang]["col_dialog_title"])
        self.resize(350, 400)
        self._init_ui()

    def _init_ui(self):
        t = TRANSLATIONS[self.lang]
        layout = QVBoxLayout(self)
        btn_layout = QHBoxLayout()
        btn_all = QPushButton(t["col_select_all"])
        btn_all.setStyleSheet("background-color: #2b6cb0; color: white; padding: 6px 12px; border-radius: 6px; font-size: 12px;")
        btn_all.clicked.connect(lambda: self.set_all(True))
        btn_none = QPushButton(t["col_deselect_all"])
        btn_none.setStyleSheet("background-color: #4a5568; color: white; padding: 6px 12px; border-radius: 6px; font-size: 12px;")
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
        col_count = model.columnCount() if model else self.table_view.columnCount()

        self.checkboxes = []
        for col in range(col_count):
            col_name = str(model.headerData(col, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole) or f"Col {col}") if model else self.table_view.horizontalHeaderItem(col).text()
            chk = QCheckBox(col_name)
            chk.setChecked(not self.table_view.isColumnHidden(col))
            chk.toggled.connect(lambda state, c=col: self.table_view.setColumnHidden(c, not state))
            self.vbox.addWidget(chk)
            self.checkboxes.append(chk)

        self.vbox.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll)

        btn_close = QPushButton("✅ OK")
        btn_close.setStyleSheet("background-color: #38a169; color: white; padding: 10px; font-weight: bold; border-radius: 6px;")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

    def set_all(self, state: bool):
        for idx, chk in enumerate(self.checkboxes):
            chk.blockSignals(True)
            chk.setChecked(state)
            self.table_view.setColumnHidden(idx, not state)
            chk.blockSignals(False)

class ThemeSettingsDialog(QDialog):
    def __init__(self, current_theme_name, apply_callback, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ Appearance & Theme Settings")
        self.resize(400, 180)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        lbl_info = QLabel("Choose your preferred visual dashboard palette:")
        layout.addWidget(lbl_info)
        self.cmb_themes = QComboBox()
        self.cmb_themes.addItems(list(THEMES_MAP.keys()))
        self.cmb_themes.setCurrentText(current_theme_name)
        self.cmb_themes.currentTextChanged.connect(apply_callback)
        form.addRow("Color Theme:", self.cmb_themes)
        layout.addLayout(form)
        btn_close = QPushButton("✅ Close & Save")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

class PositionSizingDialog(QDialog):
    def __init__(self, dbm, cash_balance, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚖️ Interactive Risk & Position-Sizing Calculator")
        self.resize(480, 400)
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
        self.spn_risk_pct.valueChanged.connect(self.calculate)
        form.addRow("Max Account Risk (%):", self.spn_risk_pct)

        self.spn_entry = QDoubleSpinBox()
        self.spn_entry.setRange(0.01, 100000.0)
        self.spn_entry.setValue(10.00)
        self.spn_entry.valueChanged.connect(self.calculate)
        form.addRow("Target Entry Price (EGP):", self.spn_entry)

        self.spn_stop = QDoubleSpinBox()
        self.spn_stop.setRange(0.01, 100000.0)
        self.spn_stop.setValue(9.20)
        self.spn_stop.valueChanged.connect(self.calculate)
        form.addRow("Stop-Loss Price (EGP):", self.spn_stop)

        layout.addLayout(form)
        self.lbl_result = QLabel()
        self.lbl_result.setStyleSheet("padding: 16px; border-radius: 8px; border: 1px solid #4a5568; background-color: #1a1d24;")
        layout.addWidget(self.lbl_result)
        self.calculate()

    def on_mode_changed(self):
        mode = self.cmb_stop_mode.currentText()
        if mode == "Manual Stop":
            self.spn_stop.setReadOnly(False)
            self.calculate()
            return
        ticker = self.cmb_ticker.currentText().strip()
        if not ticker: return
        price, atr = self.qe.get_latest_price_and_atr(ticker)
        if price > 0:
            self.spn_entry.setValue(price)
            mult = float(mode.split("x")[0])
            self.spn_stop.setValue(max(0.0001, price - (mult * atr)))
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
        
        self.lbl_result.setText(
            f"🎯 <b>Recommended Shares:</b> {shares:,} shares<br><br>"
            f"💵 <b>Total Outlay (incl. fee):</b> {total_with_fees:,.2f} EGP<br><br>"
            f"🛡️ <b>Max Capital at Risk:</b> {min(risk_budget, total_outlay):,.2f} EGP"
        )

class PortfolioDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Institutional Portfolio & Trade Manager")
        self.resize(550, 500)
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
        form_buy.addRow("Ticker Symbol:", self.cmb_buy_ticker)

        self.spn_buy_price = QDoubleSpinBox()
        self.spn_buy_price.setRange(0.0001, 100000.0)
        form_buy.addRow("Buy Price (EGP):", self.spn_buy_price)

        self.spn_buy_shares = QDoubleSpinBox()
        self.spn_buy_shares.setRange(0.0001, 10000000.0)
        form_buy.addRow("Number of Shares:", self.spn_buy_shares)

        self.dt_buy_date = QDateEdit()
        self.dt_buy_date.setCalendarPopup(True)
        self.dt_buy_date.setDate(QDate.currentDate())
        form_buy.addRow("Purchase Date:", self.dt_buy_date)

        btn_scale = QPushButton("📈 Add Shares / Scale In")
        btn_scale.clicked.connect(lambda: self.save_buy_position(mode="ADD_SCALE"))
        form_buy.addRow(btn_scale)
        
        btn_delete = QPushButton("🗑️ Delete Position")
        btn_delete.clicked.connect(self.delete_buy_position)
        form_buy.addRow(btn_delete)
        self.tabs.addTab(tab_buy, "🛒 Open / Add / Delete Position")

        tab_sell = QWidget()
        form_sell = QFormLayout(tab_sell)
        self.cmb_sell_ticker = QComboBox()
        self.cmb_sell_ticker.setEditable(True)
        self.cmb_sell_ticker.addItems(available_tickers)
        form_sell.addRow("Ticker Symbol:", self.cmb_sell_ticker)

        self.spn_sell_price = QDoubleSpinBox()
        self.spn_sell_price.setRange(0.0001, 100000.0)
        form_sell.addRow("Selling Price (EGP):", self.spn_sell_price)

        self.spn_sell_shares = QDoubleSpinBox()
        self.spn_sell_shares.setRange(0.0001, 10000000.0)
        form_sell.addRow("Shares Sold:", self.spn_sell_shares)

        self.dt_sell_date = QDateEdit()
        self.dt_sell_date.setDate(QDate.currentDate())
        form_sell.addRow("Sell Date:", self.dt_sell_date)

        btn_record_sale = QPushButton("🤝 Record Sale & Calculate P&L")
        btn_record_sale.clicked.connect(self.record_stock_sale)
        form_sell.addRow(btn_record_sale)
        self.tabs.addTab(tab_sell, "🤝 Record Sale")
        
        layout.addWidget(self.tabs)

    def save_buy_position(self, mode="ADD_SCALE"):
        ticker = self.cmb_buy_ticker.currentText().strip().upper()
        if not ticker: return
        available_tickers = self.dbm.get_unique_tickers()
        if ticker not in available_tickers and (ticker + ".CA") in available_tickers: ticker += ".CA"
        success, msg = self.dbm.add_owned_stock(ticker, self.spn_buy_price.value(), self.spn_buy_shares.value(), self.dt_buy_date.date().toString("yyyy-MM-dd"), mode=mode, is_demo=False)
        QMessageBox.information(self, "Result", msg)
        if success: self.accept()

    def delete_buy_position(self):
        ticker = self.cmb_buy_ticker.currentText().strip().upper()
        if not ticker: return
        self.dbm.remove_owned_stock(ticker)
        QMessageBox.information(self, "Deleted", f"Removed {ticker}")
        self.accept()

    def record_stock_sale(self):
        ticker = self.cmb_sell_ticker.currentText().strip().upper()
        if not ticker: return
        success, msg = self.dbm.record_sale(ticker, self.spn_sell_price.value(), self.spn_sell_shares.value(), self.dt_sell_date.date().toString("yyyy-MM-dd"))
        QMessageBox.information(self, "Result", msg)
        if success: self.accept()

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

# =============================================================================
# REDESIGNED LOGIN DIALOG (2-COLUMN LAYOUT)
# =============================================================================
class LoginDialog(QDialog):
    _BG = "#0f1115"
    _CARD = "#1a1d24"
    _CARD_LOWEST = "#0c0e12"
    _OUTLINE = "#3f4850"
    _PRIMARY = "#93ccff"
    _ON_PRIMARY = "#003351"
    _TEXT_MUTED = "#bfc7d2"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("MB-EGX Alpha — Terminal Access")
        self.showMaximized()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("loginDialog")
        
        self.setStyleSheet(f"""
            QDialog#loginDialog {{ background-color: {self._BG}; }}
            QWidget#header {{ background-color: transparent; border-bottom: 1px solid {self._OUTLINE}; }}
            QWidget#consentBox {{ background-color: #1a1c20; border: 1px solid {self._OUTLINE}; border-radius: 8px; }}
            QLabel {{ color: #ffffff; font-family: 'Inter', 'Segoe UI', sans-serif; }}
            QLineEdit {{
                background-color: {self._CARD_LOWEST}; color: #ffffff;
                border: 1px solid {self._OUTLINE}; border-radius: 8px; padding: 12px;
                font-size: 14px;
            }}
            QLineEdit:focus {{ border: 1px solid {self._PRIMARY}; }}
            QTextEdit {{
                background-color: transparent; color: {self._TEXT_MUTED};
                border: none; font-size: 11px; padding: 0px;
            }}
            QCheckBox {{ color: #e2e2e8; font-size: 13px; margin-top: 4px; }}
            QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {self._OUTLINE}; background-color: {self._CARD_LOWEST}; }}
            QCheckBox::indicator:checked {{ background-color: {self._PRIMARY}; border: 1px solid {self._PRIMARY}; }}
        """)
        
        if LOGO_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.user_info = None
        self._init_ui()

    def _init_ui(self):
        # Master Layout (No margins so header can go edge-to-edge)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 1. HEADER
        header = QWidget()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(40, 15, 40, 15)
        
        lbl_header_title = QLabel("MB-EGX Alpha")
        lbl_header_title.setStyleSheet(f"font-size: 22px; font-weight: bold; color: {self._PRIMARY}; font-family: 'Hanken Grotesk', sans-serif;")
        header_layout.addWidget(lbl_header_title)
        
        header_layout.addStretch()
        
        lbl_header_tag = QLabel("Precision Wealth Alpha    🌐  ❓")
        lbl_header_tag.setStyleSheet("font-size: 13px; color: #bfc7d2;")
        header_layout.addWidget(lbl_header_tag)
        
        outer.addWidget(header)

        # 2. MAIN 2-COLUMN CONTENT
        content_area = QWidget()
        content_layout = QHBoxLayout(content_area)
        # Margin: Left, Top, Right, Bottom
        content_layout.setContentsMargins(60, 40, 60, 40)
        content_layout.setSpacing(40)

        # --- LEFT COLUMN (Branding & Stats) ---
        left_col = QWidget()
        left_layout = QVBoxLayout(left_col)
        left_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        left_layout.setSpacing(25)

        # Logo Image
        if LOGO_PATH.exists():
            lbl_logo = QLabel()
            pixmap = QPixmap(str(LOGO_PATH)).scaledToWidth(300, Qt.TransformationMode.SmoothTransformation)
            lbl_logo.setPixmap(pixmap)
            left_layout.addWidget(lbl_logo)

        # Hero Text
        lbl_hero = QLabel(f"<span style='color: {self._PRIMARY};'>Ancient Legacy</span> meets<br><span style='color: #ffffff;'>Digital Future</span>")
        lbl_hero.setStyleSheet("font-family: 'Hanken Grotesk', sans-serif; font-size: 42px; font-weight: bold; line-height: 1.2;")
        left_layout.addWidget(lbl_hero)

        # Description
        lbl_desc = QLabel("Experience the synergy of millenia-old strategic wisdom and hyper-modern algorithmic execution. MB-EGX provides the precision wealth tools required for the high-net-worth Egyptian investor.")
        lbl_desc.setStyleSheet(f"color: {self._TEXT_MUTED}; font-size: 15px; line-height: 1.6;")
        lbl_desc.setWordWrap(True)
        lbl_desc.setMaximumWidth(500)
        left_layout.addWidget(lbl_desc)

        # Stats Grid (Horizontal)
        stats_layout = QHBoxLayout()
        stats_layout.setSpacing(15)
        
        # Stat Card 1
        stat1 = QFrame()
        stat1.setStyleSheet(f"background-color: {self._CARD}; border: 1px solid rgba(255,255,255,0.05); border-radius: 12px;")
        stat1_layout = QVBoxLayout(stat1)
        stat1_layout.setContentsMargins(20, 15, 20, 15)
        lbl_s1_val = QLabel("20ms")
        lbl_s1_val.setStyleSheet(f"color: {self._PRIMARY}; font-size: 24px; font-weight: bold; font-family: 'JetBrains Mono', monospace; border: none; background: transparent;")
        lbl_s1_lbl = QLabel("EXECUTION SPEED")
        lbl_s1_lbl.setStyleSheet("color: #89929b; font-size: 11px; font-weight: bold; letter-spacing: 1px; border: none; background: transparent;")
        stat1_layout.addWidget(lbl_s1_val)
        stat1_layout.addWidget(lbl_s1_lbl)

        # Stat Card 2
        stat2 = QFrame()
        stat2.setStyleSheet(f"background-color: {self._CARD}; border: 1px solid rgba(255,255,255,0.05); border-radius: 12px;")
        stat2_layout = QVBoxLayout(stat2)
        stat2_layout.setContentsMargins(20, 15, 20, 15)
        lbl_s2_val = QLabel("0.01%")
        lbl_s2_val.setStyleSheet(f"color: {self._PRIMARY}; font-size: 24px; font-weight: bold; font-family: 'JetBrains Mono', monospace; border: none; background: transparent;")
        lbl_s2_lbl = QLabel("ALPHA PRECISION")
        lbl_s2_lbl.setStyleSheet("color: #89929b; font-size: 11px; font-weight: bold; letter-spacing: 1px; border: none; background: transparent;")
        stat2_layout.addWidget(lbl_s2_val)
        stat2_layout.addWidget(lbl_s2_lbl)

        stats_layout.addWidget(stat1)
        stats_layout.addWidget(stat2)
        stats_layout.addStretch()
        left_layout.addLayout(stats_layout)
        
        content_layout.addWidget(left_col, stretch=1)

        # --- RIGHT COLUMN (Form) ---
        right_col = QWidget()
        right_layout = QVBoxLayout(right_col)
        right_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        form_card = QFrame()
        form_card.setObjectName("formCard")
        form_card.setFixedWidth(440)
        form_card.setStyleSheet(f"""
            QFrame#formCard {{
                background-color: {self._CARD};
                border: 1px solid rgba(255,255,255,0.05);
                border-radius: 12px;
            }}
        """)
        f_layout = QVBoxLayout(form_card)
        f_layout.setContentsMargins(35, 40, 35, 40)
        f_layout.setSpacing(20)

        # Terminal Access Heading
        lbl_form_title = QLabel("Terminal Access")
        lbl_form_title.setStyleSheet("font-size: 26px; font-weight: bold; color: #ffffff; border: none;")
        f_layout.addWidget(lbl_form_title)

        lbl_form_sub = QLabel("Sign in to view your private dashboard")
        lbl_form_sub.setStyleSheet(f"color: {self._TEXT_MUTED}; font-size: 14px; margin-bottom: 5px; border: none;")
        f_layout.addWidget(lbl_form_sub)

        # Email
        email_container = QWidget()
        email_layout = QVBoxLayout(email_container)
        email_layout.setContentsMargins(0,0,0,0)
        email_layout.setSpacing(6)
        lbl_email_hdr = QLabel("EMAIL ADDRESS")
        lbl_email_hdr.setStyleSheet("color: #89929b; font-size: 11px; font-weight: bold; letter-spacing: 1px; border: none;")
        email_layout.addWidget(lbl_email_hdr)
        self.txt_email = QLineEdit()
        self.txt_email.setPlaceholderText("investor@mb-egx.ai")
        email_layout.addWidget(self.txt_email)
        f_layout.addWidget(email_container)

        # Password
        pw_container = QWidget()
        pw_layout = QVBoxLayout(pw_container)
        pw_layout.setContentsMargins(0,0,0,0)
        pw_layout.setSpacing(6)
        
        pw_header_row = QHBoxLayout()
        lbl_pw_hdr = QLabel("PASSWORD")
        lbl_pw_hdr.setStyleSheet("color: #89929b; font-size: 11px; font-weight: bold; letter-spacing: 1px; border: none;")
        pw_header_row.addWidget(lbl_pw_hdr)
        pw_header_row.addStretch()
        
        self.btn_forgot = QPushButton("Forgot Access?")
        self.btn_forgot.setFlat(True)
        self.btn_forgot.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_forgot.setStyleSheet(f"background: transparent; border: none; color: {self._PRIMARY}; font-size: 12px; padding: 0;")
        self.btn_forgot.clicked.connect(self.do_forgot_password)
        pw_header_row.addWidget(self.btn_forgot)
        
        pw_layout.addLayout(pw_header_row)
        
        self.txt_password = QLineEdit()
        self.txt_password.setPlaceholderText("••••••••")
        self.txt_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_password.returnPressed.connect(self.do_sign_in)
        pw_layout.addWidget(self.txt_password)
        f_layout.addWidget(pw_container)

        # Consent Box
        consent_box = QWidget()
        consent_box.setObjectName("consentBox")
        consent_layout = QVBoxLayout(consent_box)
        consent_layout.setContentsMargins(15, 15, 15, 15)
        consent_layout.setSpacing(10)

        lbl_consent_hdr = QLabel("END-USER CONSENT AND LEGAL DISCLAIMER")
        lbl_consent_hdr.setStyleSheet("color: #89929b; font-size: 10px; font-weight: bold; letter-spacing: 1px; border-bottom: 1px solid #3f4850; padding-bottom: 6px;")
        consent_layout.addWidget(lbl_consent_hdr)

        self.txt_disclaimer = QTextEdit()
        self.txt_disclaimer.setReadOnly(True)
        self.txt_disclaimer.setPlainText(DISCLAIMER_TEXT)
        self.txt_disclaimer.setFixedHeight(90)
        consent_layout.addWidget(self.txt_disclaimer)

        self.chk_consent = QCheckBox("I acknowledge and agree to the Terms.")
        self.chk_consent.stateChanged.connect(self._on_consent_toggled)
        consent_layout.addWidget(self.chk_consent)
        f_layout.addWidget(consent_box)

        # Error Label
        self.lbl_error = QLabel("")
        self.lbl_error.setStyleSheet("color: #ffb4ab; font-size: 13px; border: none; background: transparent;")
        self.lbl_error.setWordWrap(True)
        f_layout.addWidget(self.lbl_error)

        # Sign In Button
        self.btn_signin = QPushButton("Sign In  →")
        self.btn_signin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_signin.setStyleSheet(
            f"background-color: {self._PRIMARY}; color: {self._ON_PRIMARY}; "
            "padding: 14px; font-weight: bold; font-size: 16px; border: none; border-radius: 8px;"
        )
        self.btn_signin.clicked.connect(self.do_sign_in)
        f_layout.addWidget(self.btn_signin)

        # Divider
        divider_layout = QHBoxLayout()
        line1 = QFrame()
        line1.setFrameShape(QFrame.Shape.HLine)
        line1.setStyleSheet("border-top: 1px solid #3f4850; background: transparent;")
        lbl_or = QLabel("OR")
        lbl_or.setStyleSheet("color: #89929b; font-size: 11px; font-weight: bold; background: transparent; border: none; padding: 0 10px;")
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setStyleSheet("border-top: 1px solid #3f4850; background: transparent;")
        
        divider_layout.addWidget(line1, stretch=1)
        divider_layout.addWidget(lbl_or)
        divider_layout.addWidget(line2, stretch=1)
        f_layout.addLayout(divider_layout)

        # Google Button
        self.btn_google = QPushButton("Sign in with Google")
        self.btn_google.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_google.setStyleSheet(
            "background-color: #282a2e; color: #e2e2e8; padding: 12px; font-size: 14px; font-weight: 500; border-radius: 8px; border: 1px solid #3f4850;"
        )
        self.btn_google.clicked.connect(lambda: QMessageBox.information(self, "Google Sign In", "In the desktop client, please use your Email/Password. If you created your account with Google on the web, click 'Forgot Access?' to set a password for desktop use."))
        f_layout.addWidget(self.btn_google)

        # Create Account Link
        create_layout = QHBoxLayout()
        lbl_no_account = QLabel("Don't have an account?")
        lbl_no_account.setStyleSheet("color: #89929b; font-size: 13px; border: none; background: transparent;")
        self.btn_signup = QPushButton("Create Account")
        self.btn_signup.setFlat(True)
        self.btn_signup.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_signup.setStyleSheet(f"color: {self._PRIMARY}; font-size: 13px; font-weight: bold; border: none; background: transparent;")
        self.btn_signup.clicked.connect(self.do_sign_up)
        
        create_layout.addStretch()
        create_layout.addWidget(lbl_no_account)
        create_layout.addWidget(self.btn_signup)
        create_layout.addStretch()
        f_layout.addLayout(create_layout)

        right_layout.addWidget(form_card)
        content_layout.addWidget(right_col, stretch=1)
        outer.addWidget(content_area, stretch=1)

        # 3. FOOTER
        footer = QWidget()
        footer.setFixedHeight(40)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0,0,0,15)
        lbl_footer = QLabel("🔒 AES-256 BANK GRADE ENCRYPTION MATRIX ENABLED")
        lbl_footer.setStyleSheet("color: #89929b; font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        lbl_footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_layout.addWidget(lbl_footer)
        outer.addWidget(footer)

    def _on_consent_toggled(self, _state):
        checked = self.chk_consent.isChecked()
        if checked:
            self.lbl_error.setText("")

    def _friendly_name(self, data, email):
        display_name = (data.get("displayName") or "").strip()
        if display_name: return display_name
        local = email.split("@")[0]
        return local[:1].upper() + local[1:] if local else "there"

    def _attempt(self, fn, min_password_len=0, require_consent=False):
        email = self.txt_email.text().strip()
        password = self.txt_password.text()
        if not email or not password:
            self.lbl_error.setText("Enter both email and password.")
            return
        if min_password_len and len(password) < min_password_len:
            self.lbl_error.setText(f"Password must be at least {min_password_len} characters.")
            return
        if require_consent and not self.chk_consent.isChecked():
            self.lbl_error.setText("You must agree to the legal terms to create an account.")
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
            if require_consent:
                ip_address = fetch_client_ip()
                write_consent_doc(data["idToken"], data["localId"], ip_address)
            self.accept()
        except Exception as e:
            self.lbl_error.setText(str(e))
        finally:
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)

    def do_sign_in(self):
        self._attempt(firebase_sign_in)

    def do_sign_up(self):
        self._attempt(firebase_sign_up, min_password_len=6, require_consent=True)

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
        self.lbl_status = QLabel("Loading session data…")
        layout.addWidget(self.lbl_status)
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["User", "Sessions (🌐/🖥️)", "Total Time", "Trades", "Trade Value", "Portfolio Value", "Last Seen"])
        layout.addWidget(self.table, stretch=1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        layout.addWidget(btn_close)

    def _load(self):
        self._worker = _CloudWorker(compute_usage_analytics, self.id_token)
        self._worker.finished_result.connect(self._on_loaded)
        self._worker.start()

    def _on_loaded(self, result):
        if not result:
            self.lbl_status.setText("⚠️ Could not load analytics data.")
            return
        self.lbl_status.setText(f"👥 Unique Users: {result['unique_users']} | 📅 Sessions: {result['session_count']}")
        rows = result["per_user"]
        self.table.setRowCount(len(rows))
        for i, u in enumerate(rows):
            values = [
                u["name"], str(u['web_sessions'] + u['desktop_sessions']), _format_duration(u["total_sec"]),
                str(u["trade_count"]), f"{u['trade_value_egp']:,.0f} EGP", f"{u['portfolio_value_egp']:,.0f} EGP",
                u["last_seen"].astimezone().strftime("%Y-%m-%d %H:%M") if u.get("last_seen") else "—"
            ]
            for j, val in enumerate(values):
                item = QTableWidgetItem(val)
                self.table.setItem(i, j, item)
        self.table.resizeColumnsToContents()

class QuantDashboard(QMainWindow):
    def __init__(self, user_info=None):
        super().__init__()
        self.setWindowTitle("MB-EGX — Out-of-Core Trading Matrix & Sector Dashboard")
        self.resize(1520, 920)
        if LOGO_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.dbm = DatabaseManager()
        self.qe = QuantitativeEngine()
        self.current_theme = "🌙 Institutional Dark"
        self.current_lang = "EN"
        self._raw_buys_data = []
        self.user_info = user_info
        self._session_id = None
        self._cloud_threads = set()
        self._init_ui()
        self.apply_theme(self.current_theme)
        self._start_cloud_session()

    def _run_cloud(self, fn, *args, on_result=None, **kwargs):
        if requests is None or not self.user_info: return
        worker = _CloudWorker(fn, *args, **kwargs)
        if on_result: worker.finished_result.connect(on_result)
        worker.finished_result.connect(lambda _: self._cloud_threads.discard(worker))
        self._cloud_threads.add(worker)
        worker.start()

    def _start_cloud_session(self):
        if not self.user_info: return
        self._run_cloud(create_session_doc, self.user_info["idToken"], self.user_info["uid"], self.user_info["email"], self.user_info["name"], on_result=self._on_session_created)
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.timeout.connect(self._heartbeat_tick)
        self._heartbeat_timer.start(30000)

    def _on_session_created(self, session_id): self._session_id = session_id

    def _heartbeat_tick(self):
        if self._session_id: self._run_cloud(touch_session_doc, self.user_info["idToken"], self._session_id)

    def apply_theme(self, theme_name):
        if theme_name in THEMES_MAP:
            self.current_theme = theme_name
            self.setStyleSheet(THEMES_MAP[theme_name])

    def _init_ui(self):
        main_widget = QWidget()
        main_widget.setObjectName("main_widget")
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        header_wrap = QWidget()
        header_wrap.setObjectName("webPanel")
        header_layout = QVBoxLayout(header_wrap)

        top_row = QHBoxLayout()
        if LOGO_PATH.exists():
            self.lbl_brand_logo = QLabel()
            self.lbl_brand_logo.setPixmap(QPixmap(str(LOGO_PATH)).scaledToHeight(32, Qt.TransformationMode.SmoothTransformation))
            top_row.addWidget(self.lbl_brand_logo)
        
        self.lbl_brand_title = QLabel("MB-EGX Dashboard")
        self.lbl_brand_title.setStyleSheet("font-size: 18px; font-weight: 900; color: #93ccff;")
        top_row.addWidget(self.lbl_brand_title)
        top_row.addStretch()
        
        self.lbl_status = QLabel("System Idle.")
        top_row.addWidget(self.lbl_status)
        top_row.addStretch()
        
        header_layout.addLayout(top_row)
        
        controls_row = QHBoxLayout()
        self.btn_ingest = QPushButton("⚡ Run Ingestion")
        self.btn_ingest.clicked.connect(self.start_ingestion)
        self.btn_analyze = QPushButton("🧠 Execute Matrix")
        self.btn_analyze.clicked.connect(self.start_analysis)
        self.btn_port = QPushButton("💼 Manage Portfolio")
        self.btn_port.clicked.connect(lambda: PortfolioDialog(self).exec())
        self.btn_calc = QPushButton("⚖️ Calculator")
        self.btn_calc.clicked.connect(lambda: PositionSizingDialog(self.dbm, self.dbm.get_cash_balance(), self).exec())

        controls_row.addWidget(self.btn_ingest)
        controls_row.addWidget(self.btn_analyze)
        controls_row.addWidget(self.btn_port)
        controls_row.addWidget(self.btn_calc)
        
        if self.user_info and self.user_info.get("email") in ADMIN_EMAILS:
            btn_admin = QPushButton("📊 Analytics")
            btn_admin.clicked.connect(lambda: AnalyticsDialog(self.user_info["idToken"], self).exec())
            controls_row.addWidget(btn_admin)
            
        controls_row.addStretch()
        header_layout.addLayout(controls_row)

        dir_layout = QHBoxLayout()
        self.txt_scan_dir = QLineEdit(str(WATCH_DIR))
        dir_layout.addWidget(QLabel("📂 Scan Folder:"))
        dir_layout.addWidget(self.txt_scan_dir)
        header_layout.addLayout(dir_layout)
        
        layout.addWidget(header_wrap)
        
        self.tabs = QTabWidget()
        self.tbl_buys = self._create_matrix_table()
        self.tabs.addTab(self.tbl_buys, "📈 Action Matrix")
        layout.addWidget(self.tabs)

    def _create_matrix_table(self):
        tbl = QTableView()
        tbl.setModel(MatrixTableModel())
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        return tbl

    def start_ingestion(self):
        self.ingest_worker = IngestionWorker(self.txt_scan_dir.text())
        self.ingest_worker.progress_signal.connect(lambda pct, msg: self.lbl_status.setText(msg))
        self.ingest_worker.start()

    def start_analysis(self):
        self.analysis_worker = AnalysisWorker()
        self.analysis_worker.results_signal.connect(self.populate_tables)
        self.analysis_worker.start()

    def populate_tables(self, buys, exits, top10, closed_trades, fin_stmt, sector_summary, breakout_watchlist=None):
        self.lbl_status.setText("✅ Matrix updated.")
        self.tbl_buys.model().update_data(buys)

def _show_fatal_error(title, message):
    try: QMessageBox.critical(None, title, message)
    except Exception: pass

def _install_excepthook():
    def _hook(exc_type, exc_value, exc_tb):
        logger.error("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    sys.excepthook = _hook

if __name__ == "__main__":
    app = QApplication(sys.argv)
    _install_excepthook() 
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
        sys.exit(1)
