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
    QMainWindow, QDialog, QWidget#main_widget { 
        background-color: #0f1115; 
        color: #e2e2e8; 
        font-family: 'Inter', 'Segoe UI', Arial, sans-serif; 
    }
    QWidget#webPanel, QWidget#filterPanel {
        background-color: #1a1d24;
        border: 1px solid #2d3748;
        border-radius: 8px;
    }
    QTabWidget::pane { 
        border: 1px solid #2d3748; 
        background-color: #1a1d24; 
        border-radius: 8px;
        margin-top: -1px;
    }
    QTabBar::tab { 
        background: transparent; 
        color: #a0aec0; 
        padding: 6px 10px;
        font-size: 12px;
        border-top-left-radius: 6px; 
        border-top-right-radius: 6px; 
        margin-right: 2px;
        font-weight: bold; 
        border: 1px solid transparent;
    }
    QTabBar::tab:selected { 
        background-color: #3198dc; 
        color: #ffffff; 
        border: 1px solid #2d3748;
        border-bottom: none;
    }
    QTabBar::tab:hover:!selected {
        color: #ffffff;
        background-color: rgba(255, 255, 255, 0.05);
    }
    QTableWidget, QTableView { 
        background-color: #1a1d24; 
        alternate-background-color: #15181e; 
        color: #e2e2e8; 
        gridline-color: #2d3748; 
        border: none;
        border-bottom-left-radius: 8px;
        border-bottom-right-radius: 8px;
        selection-background-color: rgba(49, 152, 220, 0.2); 
        selection-color: #ffffff;
        outline: none;
    }
    QHeaderView { background-color: #2d3748; border: none; }
    QTableCornerButton::section { background-color: #2d3748; border: none; }
    QHeaderView::section { 
        background-color: #2d3748; 
        color: #93ccff; 
        padding: 6px; 
        font-weight: bold; 
        font-size: 11px;
        letter-spacing: 1px;
        border: none;
        border-bottom: 2px solid #0f1115;
    }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { 
        background-color: #0f1115; 
        color: #ffffff; 
        border: 1px solid #2d3748; 
        padding: 4px 8px; 
        border-radius: 4px; 
    }
    QLineEdit:focus, QComboBox:focus { border: 1px solid #3198dc; }
    QPushButton { 
        background-color: #2d3748; 
        color: #ffffff; 
        border: none; 
        border-radius: 4px; 
        padding: 4px; 
        font-weight: bold; 
        font-size: 11px;
    }
    QPushButton:hover { background-color: #3a4557; }
    QPushButton:pressed { background-color: #232b38; }
    QRadioButton {
        color: #e2e2e8;
        font-weight: bold;
        font-size: 12px;
        spacing: 6px;
    }
    QRadioButton::indicator {
        width: 14px;
        height: 14px;
        border-radius: 7px;
        border: 1px solid #2d3748;
        background-color: #0f1115;
    }
    QRadioButton::indicator:checked {
        background-color: #3198dc;
        border: 1px solid #93ccff;
    }
    QLabel {
        color: #e2e2e8;
    }
    QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget {
        background-color: #0f1115;
        border: none;
    }
    QProgressBar { 
        border: none; 
        background-color: rgba(255,255,255,0.1); 
        border-radius: 2px; 
        height: 4px; 
        max-height: 4px; 
    }
    QProgressBar::chunk { background-color: #3198dc; border-radius: 2px; }
    QScrollBar:vertical { background: #0f1115; width: 14px; margin: 0px; }
    QScrollBar::handle:vertical { background: #2d3748; border-radius: 7px; min-height: 20px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar:horizontal { background: #0f1115; height: 14px; margin: 0px; }
    QScrollBar::handle:horizontal { background: #2d3748; border-radius: 7px; min-width: 20px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QAbstractScrollArea::corner { background-color: #0f1115; }
"""

THEME_LIGHT = """
    QMainWindow, QDialog, QWidget#main_widget { background-color: #f8fafc; color: #1a202c; font-family: 'Inter', 'Segoe UI', Arial, sans-serif; }
    QWidget#webPanel, QWidget#filterPanel { background-color: #ffffff; border: 1px solid #cbd5e0; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
    QTabWidget::pane { border: 1px solid #cbd5e0; background-color: #ffffff; border-radius: 8px; margin-top: -1px; }
    QTabBar::tab { background: transparent; color: #4a5568; padding: 6px 10px; font-size: 12px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; font-weight: bold; border: 1px solid transparent; }
    QTabBar::tab:selected { background-color: #2b6cb0; color: #ffffff; border: 1px solid #cbd5e0; border-bottom: none; }
    QTabBar::tab:hover:!selected { background-color: rgba(0, 0, 0, 0.03); }
    QTableWidget, QTableView { background-color: #ffffff; alternate-background-color: #f1f5f9; color: #1a202c; gridline-color: #e2e8f0; border: none; border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; selection-background-color: #bee3f8; selection-color: #1a202c; }
    QHeaderView { background-color: #2d3748; border: none; }
    QTableCornerButton::section { background-color: #2d3748; border: none; }
    QHeaderView::section { background-color: #2d3748; color: #ffffff; padding: 6px; font-weight: bold; font-size: 11px; letter-spacing: 1px; border: none; border-bottom: 2px solid #f8fafc; }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background-color: #ffffff; color: #1a202c; border: 1px solid #a0aec0; padding: 4px 8px; border-radius: 4px; }
    QPushButton { background-color: #e2e8f0; color: #1a202c; border: none; border-radius: 4px; padding: 4px; font-size: 11px; font-weight: bold; }
    QPushButton:hover { background-color: #cbd5e0; }
    QPushButton:pressed { background-color: #b8c4d4; }
    QRadioButton { color: #1a202c; font-weight: bold; font-size: 12px; spacing: 6px; }
    QRadioButton::indicator { width: 14px; height: 14px; border-radius: 7px; border: 1px solid #a0aec0; background-color: #ffffff; }
    QRadioButton::indicator:checked { background-color: #2b6cb0; border: 1px solid #2b6cb0; }
    QLabel { color: #1a202c; }
    QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget { background-color: #f8fafc; border: none; }
    QProgressBar { border: none; background-color: rgba(0,0,0,0.1); border-radius: 2px; height: 4px; max-height: 4px; }
    QProgressBar::chunk { background-color: #3182ce; border-radius: 2px; }
    QScrollBar:vertical { background: #f8fafc; width: 14px; margin: 0px; }
    QScrollBar::handle:vertical { background: #cbd5e0; border-radius: 7px; min-height: 20px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar:horizontal { background: #f8fafc; height: 14px; margin: 0px; }
    QScrollBar::handle:horizontal { background: #cbd5e0; border-radius: 7px; min-width: 20px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QAbstractScrollArea::corner { background-color: #f8fafc; }
"""

THEME_BLUE = """
    QMainWindow, QDialog, QWidget#main_widget { background-color: #0f172a; color: #e2e8f0; font-family: 'Inter', 'Segoe UI', Arial, sans-serif; }
    QWidget#webPanel, QWidget#filterPanel { background-color: #1e293b; border: 1px solid #334155; border-radius: 8px; }
    QTabWidget::pane { border: 1px solid #1e293b; background-color: #0f172a; border-radius: 8px; margin-top: -1px; }
    QTabBar::tab { background: transparent; color: #94a3b8; padding: 6px 10px; font-size: 12px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; font-weight: bold; border: 1px solid transparent; }
    QTabBar::tab:selected { background-color: #0284c7; color: #ffffff; border: 1px solid #1e293b; border-bottom: none; }
    QTabBar::tab:hover:!selected { background-color: rgba(255, 255, 255, 0.05); color: #ffffff; }
    QTableWidget, QTableView { background-color: #0f172a; alternate-background-color: #162032; color: #f8fafc; gridline-color: #334155; border: none; border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; selection-background-color: #0369a1; }
    QHeaderView { background-color: #1e293b; border: none; }
    QTableCornerButton::section { background-color: #1e293b; border: none; }
    QHeaderView::section { background-color: #1e293b; color: #38bdf8; padding: 6px; font-weight: bold; font-size: 11px; letter-spacing: 1px; border: none; border-bottom: 2px solid #0f172a; }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background-color: #1e293b; color: #f8fafc; border: 1px solid #475569; padding: 4px 8px; border-radius: 4px; }
    QPushButton { background-color: #1e293b; color: #f8fafc; border: none; border-radius: 4px; padding: 4px; font-size: 11px; font-weight: bold; }
    QPushButton:hover { background-color: #334155; }
    QPushButton:pressed { background-color: #16202f; }
    QRadioButton { color: #f8fafc; font-weight: bold; font-size: 12px; spacing: 6px; }
    QRadioButton::indicator { width: 14px; height: 14px; border-radius: 7px; border: 1px solid #475569; background-color: #0f172a; }
    QRadioButton::indicator:checked { background-color: #0284c7; border: 1px solid #38bdf8; }
    QLabel { color: #e2e8f0; }
    QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget { background-color: #0f172a; border: none; }
    QProgressBar { border: none; background-color: rgba(255,255,255,0.1); border-radius: 2px; height: 4px; max-height: 4px; }
    QProgressBar::chunk { background-color: #0284c7; border-radius: 2px; }
    QScrollBar:vertical { background: #0f172a; width: 14px; margin: 0px; }
    QScrollBar::handle:vertical { background: #334155; border-radius: 7px; min-height: 20px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar:horizontal { background: #0f172a; height: 14px; margin: 0px; }
    QScrollBar::handle:horizontal { background: #334155; border-radius: 7px; min-width: 20px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QAbstractScrollArea::corner { background-color: #0f172a; }
"""

THEME_BLUSH_ROSE = """
    QMainWindow, QDialog, QWidget#main_widget { background-color: #fdf2f8; color: #500724; font-family: 'Inter', 'Segoe UI', Arial, sans-serif; }
    QWidget#webPanel, QWidget#filterPanel { background-color: #ffffff; border: 1px solid #fbcfe8; border-radius: 8px; }
    QTabWidget::pane { border: 1px solid #fbcfe8; background-color: #ffffff; border-radius: 8px; margin-top: -1px; }
    QTabBar::tab { background: transparent; color: #831843; padding: 6px 10px; font-size: 12px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; font-weight: bold; border: 1px solid transparent; }
    QTabBar::tab:selected { background-color: #ec4899; color: #ffffff; border: 1px solid #fbcfe8; border-bottom: none; }
    QTabBar::tab:hover:!selected { background-color: rgba(0, 0, 0, 0.03); }
    QTableWidget, QTableView { background-color: #ffffff; alternate-background-color: #fef6fb; color: #500724; gridline-color: #fbcfe8; border: none; border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; selection-background-color: #f472b6; selection-color: #ffffff; }
    QHeaderView { background-color: #be185d; border: none; }
    QTableCornerButton::section { background-color: #be185d; border: none; }
    QHeaderView::section { background-color: #be185d; color: #ffffff; padding: 6px; font-weight: bold; font-size: 11px; letter-spacing: 1px; border: none; border-bottom: 2px solid #fdf2f8; }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background-color: #ffffff; color: #500724; border: 1px solid #f472b6; padding: 4px 8px; border-radius: 4px; }
    QPushButton { background-color: #fce7f3; color: #831843; border: none; border-radius: 4px; padding: 4px; font-size: 11px; font-weight: bold; }
    QPushButton:hover { background-color: #fbcfe8; }
    QPushButton:pressed { background-color: #f9a8d4; }
    QRadioButton { color: #500724; font-weight: bold; font-size: 12px; spacing: 6px; }
    QRadioButton::indicator { width: 14px; height: 14px; border-radius: 7px; border: 1px solid #f472b6; background-color: #ffffff; }
    QRadioButton::indicator:checked { background-color: #ec4899; border: 1px solid #ec4899; }
    QLabel { color: #500724; }
    QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget { background-color: #fdf2f8; border: none; }
    QProgressBar { border: none; background-color: rgba(0,0,0,0.05); border-radius: 2px; height: 4px; max-height: 4px; }
    QProgressBar::chunk { background-color: #ec4899; border-radius: 2px; }
    QScrollBar:vertical { background: #fdf2f8; width: 14px; margin: 0px; }
    QScrollBar::handle:vertical { background: #fbcfe8; border-radius: 7px; min-height: 20px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar:horizontal { background: #fdf2f8; height: 14px; margin: 0px; }
    QScrollBar::handle:horizontal { background: #fbcfe8; border-radius: 7px; min-width: 20px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QAbstractScrollArea::corner { background-color: #ffffff; }
"""

THEME_VELVET_ROSE = """
    QMainWindow, QDialog, QWidget#main_widget { background-color: #20131a; color: #ffe4e6; font-family: 'Inter', 'Segoe UI', Arial, sans-serif; }
    QWidget#webPanel, QWidget#filterPanel { background-color: #311825; border: 1px solid #3f2231; border-radius: 8px; }
    QTabWidget::pane { border: 1px solid #3f2231; background-color: #20131a; border-radius: 8px; margin-top: -1px; }
    QTabBar::tab { background: transparent; color: #f472b6; padding: 6px 10px; font-size: 12px; border-top-left-radius: 6px; border-top-right-radius: 6px; margin-right: 2px; font-weight: bold; border: 1px solid transparent; }
    QTabBar::tab:selected { background-color: #e11d48; color: #ffffff; border: 1px solid #3f2231; border-bottom: none; }
    QTabBar::tab:hover:!selected { background-color: rgba(255, 255, 255, 0.05); color: #ffffff; }
    QTableWidget, QTableView { background-color: #20131a; alternate-background-color: #26171f; color: #fff1f2; gridline-color: #3f2231; border: none; border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; selection-background-color: #be185d; }
    QHeaderView { background-color: #3f2231; border: none; }
    QTableCornerButton::section { background-color: #3f2231; border: none; }
    QHeaderView::section { background-color: #3f2231; color: #fb7185; padding: 6px; font-weight: bold; font-size: 11px; letter-spacing: 1px; border: none; border-bottom: 2px solid #20131a; }
    QLineEdit, QComboBox, QDateEdit, QDoubleSpinBox { background-color: #311825; color: #fff1f2; border: 1px solid #9f1239; padding: 4px 8px; border-radius: 4px; }
    QPushButton { background-color: #311825; color: #fecdd3; border: none; border-radius: 4px; padding: 4px; font-size: 11px; font-weight: bold; }
    QPushButton:hover { background-color: #3f2231; }
    QPushButton:pressed { background-color: #26121b; }
    QRadioButton { color: #fff1f2; font-weight: bold; font-size: 12px; spacing: 6px; }
    QRadioButton::indicator { width: 14px; height: 14px; border-radius: 7px; border: 1px solid #9f1239; background-color: #20131a; }
    QRadioButton::indicator:checked { background-color: #e11d48; border: 1px solid #fb7185; }
    QLabel { color: #ffe4e6; }
    QScrollArea, QScrollArea > QWidget, QScrollArea > QWidget > QWidget { background-color: #20131a; border: none; }
    QProgressBar { border: none; background-color: rgba(255,255,255,0.1); border-radius: 2px; height: 4px; max-height: 4px; }
    QProgressBar::chunk { background-color: #e11d48; border-radius: 2px; }
    QScrollBar:vertical { background: #20131a; width: 14px; margin: 0px; }
    QScrollBar::handle:vertical { background: #3f2231; border-radius: 7px; min-height: 20px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QScrollBar:horizontal { background: #20131a; height: 14px; margin: 0px; }
    QScrollBar::handle:horizontal { background: #3f2231; border-radius: 7px; min-width: 20px; }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
    QAbstractScrollArea::corner { background-color: #20131a; }
"""

THEMES_MAP = {
    "🌙 Institutional Dark": THEME_DARK,
    "☀️ Professional Light": THEME_LIGHT,
    "🌊 Midnight Blue": THEME_BLUE,
    "🌸 Soft Blush Rose (Pastel & Cream)": THEME_BLUSH_ROSE,
    "✨ Velvet Rose Gold (Warm Elegance)": THEME_VELVET_ROSE,
}

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
                return QFont("Inter", 9, QFont.Weight.Bold)
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
        t = TRANSLATIONS[lang]
        self.setWindowTitle(t["col_dialog_title"])
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
        self.apply_callback = apply_callback
        self._init_ui(current_theme_name)

    def _init_ui(self, current_theme_name):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        lbl_info = QLabel("Choose your preferred visual dashboard palette:")
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet("font-size: 13px;")
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
        btn_close.setStyleSheet("background-color: #3198dc; color: white; margin-top: 10px; padding: 10px 20px; font-size: 13px; border-radius: 6px;")
        btn_close.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)


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
        lbl_cash.setStyleSheet("font-size: 14px; margin-bottom: 8px;")
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
        self.lbl_result.setStyleSheet("padding: 16px; border-radius: 8px; font-size: 14px; border: 1px solid #4a5568; background-color: #1a1d24;")
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
            f"🎯 <b>Recommended Shares:</b> {shares:,} shares<br><br>"
            f"💵 <b>Total Outlay (incl. 0.35% fee):</b> {total_with_fees:,.2f} EGP ({pct_of_portfolio:.1f}% of cash)<br><br>"
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
        form_buy.setSpacing(12)

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
        btn_scale.setStyleSheet("background-color: #3198dc; color: white; margin-top: 10px; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_scale.clicked.connect(lambda: self.save_buy_position(mode="ADD_SCALE"))
        form_buy.addRow(btn_scale)

        btn_layout = QHBoxLayout()
        btn_overwrite = QPushButton("✏️ Correct Mistake")
        btn_overwrite.setStyleSheet("background-color: #d69e2e; color: white; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_overwrite.clicked.connect(lambda: self.save_buy_position(mode="OVERWRITE"))

        btn_delete = QPushButton("🗑️ Delete Position")
        btn_delete.setStyleSheet("background-color: #e53e3e; color: white; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_delete.clicked.connect(self.delete_buy_position)

        btn_layout.addWidget(btn_overwrite)
        btn_layout.addWidget(btn_delete)
        form_buy.addRow(btn_layout)
        self.tabs.addTab(tab_buy, "🛒 Open / Add / Delete Position")

        tab_sell = QWidget()
        form_sell = QFormLayout(tab_sell)
        form_sell.setSpacing(12)

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
        btn_record_sale.setStyleSheet("background-color: #38a169; color: white; margin-top: 10px; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_record_sale.clicked.connect(self.record_stock_sale)
        form_sell.addRow(btn_record_sale)
        self.tabs.addTab(tab_sell, "🤝 Record Sale / Close Trade")
        layout.addWidget(self.tabs)

        btn_clean = QPushButton("🧹 Clear Sample Demo Data")
        btn_clean.setStyleSheet("background-color: #4a5568; color: white; margin-top: 5px; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
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

# =============================================================================
# REDESIGNED FULL-SCREEN LOGIN DIALOG
# =============================================================================
class LoginDialog(QDialog):
    _BG = "#0f1115"
    _CARD = "#1a1d24"
    _CARD_LOWEST = "#0c0e12"
    _OUTLINE = "#4a5568" # Lighter outline for input visibility
    _PRIMARY = "#93ccff"
    _ON_PRIMARY = "#003351"
    _TEXT_MUTED = "#bfc7d2"

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Set as full standalone window
        self.setWindowFlags(Qt.WindowType.Window)
        self.setWindowTitle("MB-EGX Alpha — Terminal Access")
        
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("loginDialog")
        
        # Fixed styling for inputs, scrollbars, and explicitly targeted QFrames
        self.setStyleSheet(f"""
            QDialog#loginDialog {{ background-color: {self._BG}; }}
            
            /* Target the consent box specifically as a QFrame to ensure rendering */
            QFrame#consentBox {{ 
                background-color: #1a1c20; 
                border: 1px solid {self._OUTLINE}; 
                border-radius: 6px; 
            }}
            
            QLabel {{ color: #ffffff; font-family: 'Inter', 'Segoe UI', sans-serif; }}
            
            /* Fixed Input Boxes */
            QLineEdit {{
                background-color: {self._CARD_LOWEST}; 
                color: #ffffff;
                border: 1px solid {self._OUTLINE}; 
                border-radius: 6px; 
                padding: 0px 14px; 
                font-size: 16px; /* Larger, clearer text */
            }}
            QLineEdit:focus {{ border: 1px solid {self._PRIMARY}; }}
            
            /* Disclaimer Text */
            QTextEdit {{
                background-color: transparent; 
                color: #a0aec0;
                border: none; 
                font-size: 11px; 
                padding: 0px;
            }}
            
            /* Dark Scrollbar for Disclaimer */
            QScrollBar:vertical {{ background: #1a1c20; width: 6px; margin: 0px; }}
            QScrollBar::handle:vertical {{ background: #3f4850; border-radius: 3px; min-height: 20px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            
            /* Explicit Checkbox Styling */
            QCheckBox {{ color: #e2e2e8; font-size: 13px; }}
            QCheckBox::indicator {{ 
                width: 16px; height: 16px; 
                border-radius: 4px; 
                border: 1px solid {self._OUTLINE}; 
                background-color: {self._CARD_LOWEST}; 
            }}
            QCheckBox::indicator:checked {{ 
                background-color: {self._PRIMARY}; 
                border: 1px solid {self._PRIMARY}; 
            }}
        """)
        
        if LOGO_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.user_info = None
        self._init_ui()
        self.showMaximized()

    def _init_ui(self):
        # Master Layout (No margins so background touches edges)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # MAIN 2-COLUMN CONTENT
        content_area = QWidget()
        content_layout = QHBoxLayout(content_area)
        # Squeeze margins slightly to allow plenty of vertical room for right-side card
        content_layout.setContentsMargins(40, 20, 40, 20)
        content_layout.setSpacing(40)

        # --- LEFT COLUMN (Branding) ---
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
        lbl_desc.setStyleSheet("color: #bfc7d2; font-size: 15px; line-height: 1.6;")
        lbl_desc.setWordWrap(True)
        lbl_desc.setMaximumWidth(500)
        left_layout.addWidget(lbl_desc)

        content_layout.addWidget(left_col, stretch=1)

        # --- RIGHT COLUMN (Form) ---
        right_col = QWidget()
        right_layout = QVBoxLayout(right_col)
        right_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        form_card = QFrame()
        form_card.setObjectName("formCard")
        # Ensure card doesn't stretch infinitely but stays contained
        form_card.setMinimumWidth(400)
        form_card.setMaximumWidth(460)
        form_card.setStyleSheet(f"""
            QFrame#formCard {{
                background-color: {self._CARD};
                border: 1px solid #2d3748;
                border-radius: 12px;
            }}
        """)
        f_layout = QVBoxLayout(form_card)
        # Reduced vertical spacing and margins to ensure checkbox fits securely inside
        f_layout.setContentsMargins(30, 25, 30, 25)
        f_layout.setSpacing(14)

        # Terminal Access Heading
        lbl_form_title = QLabel("Terminal Access")
        lbl_form_title.setStyleSheet("font-size: 26px; font-weight: bold; color: #ffffff; border: none;")
        f_layout.addWidget(lbl_form_title)

        lbl_form_sub = QLabel("Sign in to view your private dashboard")
        lbl_form_sub.setStyleSheet("color: #bfc7d2; font-size: 13px; margin-bottom: 5px; border: none;")
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
        self.txt_email.setFixedHeight(45) # Force robust vertical height
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
        self.txt_password.setFixedHeight(45) # Force robust vertical height
        self.txt_password.returnPressed.connect(self.do_sign_in)
        pw_layout.addWidget(self.txt_password)
        f_layout.addWidget(pw_container)

        # Consent Box (Using QFrame for guaranteed background render)
        consent_box = QFrame()
        consent_box.setObjectName("consentBox")
        consent_layout = QVBoxLayout(consent_box)
        consent_layout.setContentsMargins(12, 12, 12, 12)
        consent_layout.setSpacing(8)

        self.txt_disclaimer = QTextEdit()
        self.txt_disclaimer.setReadOnly(True)
        self.txt_disclaimer.setPlainText(DISCLAIMER_TEXT)
        self.txt_disclaimer.setFixedHeight(60) # Shorter height leaves plenty of room for checkbox
        consent_layout.addWidget(self.txt_disclaimer)

        self.chk_consent = QCheckBox("I acknowledge and agree to the Terms.")
        self.chk_consent.stateChanged.connect(self._on_consent_toggled)
        consent_layout.addWidget(self.chk_consent)
        f_layout.addWidget(consent_box)

        # Error Label - Hidden by default to save layout space
        self.lbl_error = QLabel("")
        self.lbl_error.setStyleSheet("color: #ffb4ab; font-size: 13px; border: none; background: transparent; font-weight: bold;")
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setVisible(False)
        f_layout.addWidget(self.lbl_error)

        # Sign In Button
        self.btn_signin = QPushButton("Sign In  →")
        self.btn_signin.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_signin.setStyleSheet(
            f"background-color: {self._PRIMARY}; color: {self._ON_PRIMARY}; "
            "padding: 12px; font-weight: bold; font-size: 15px; border: none; border-radius: 8px;"
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
            "background-color: #282a2e; color: #e2e2e8; padding: 12px; font-size: 14px; font-weight: bold; border-radius: 8px; border: 1px solid #4a5568;"
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

        # FOOTER
        footer = QWidget()
        footer.setFixedHeight(40)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0,0,0,15)
        lbl_footer = QLabel("🔒 AES-256 BANK GRADE ENCRYPTION MATRIX ENABLED")
        lbl_footer.setStyleSheet("color: #89929b; font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        lbl_footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_layout.addWidget(lbl_footer)
        outer.addWidget(footer)

    def _show_error(self, msg):
        self.lbl_error.setText(msg)
        self.lbl_error.setVisible(bool(msg))

    def _on_consent_toggled(self, _state):
        if self.chk_consent.isChecked():
            self._show_error("")

    def _friendly_name(self, data, email):
        display_name = (data.get("displayName") or "").strip()
        if display_name: return display_name
        local = email.split("@")[0]
        return local[:1].upper() + local[1:] if local else "there"

    def _attempt(self, fn, min_password_len=0, require_consent=False):
        email = self.txt_email.text().strip()
        password = self.txt_password.text()
        if not email or not password:
            self._show_error("Enter both email and password.")
            return
        if min_password_len and len(password) < min_password_len:
            self._show_error(f"Password must be at least {min_password_len} characters.")
            return
        if require_consent and not self.chk_consent.isChecked():
            self._show_error("You must explicitly agree to the legal terms to create an account.")
            return

        self._show_error("")
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
            self._show_error(str(e))
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
            self._show_error("Type your email above first, then click this link.")
            return

        self._show_error("")
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
            self._show_error(str(e))
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

        lbl_info = QLabel(
            "Sessions and trading activity combined across the website (🌐) and the desktop app (🖥️). "
            "Time is approximate (30s heartbeat). Trade Value = total EGP bought + sold; "
            "Portfolio Value = cash + open positions at cost."
        )
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet("color: #a0aec0; font-size: 13px;")
        layout.addWidget(lbl_info)

        self.lbl_status = QLabel("Loading session data…")
        self.lbl_status.setStyleSheet("font-weight: bold; font-size: 14px; padding: 6px 0;")
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
        if LOGO_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.dbm = DatabaseManager()
        self.qe = QuantitativeEngine()
        self.current_theme = "🌙 Institutional Dark"
        self.current_lang = "EN"
        self.theme_highlight = QColor("#3198dc")
        self._raw_buys_data = []
        self.user_info = user_info
        self._session_id = None
        self._cloud_threads = set()
        self._init_ui()
        self.apply_theme(self.current_theme)
        self._start_cloud_session()

    def _run_cloud(self, fn, *args, on_result=None, **kwargs):
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

            # Preserved larger padding for main action buttons while global QSS handles compact filter buttons
            btn_base_style = "color: white; border-radius: 6px; font-weight: bold; padding: 6px 14px; font-size: 12px;"

            if "Blush Rose" in theme_name:
                self.theme_highlight = QColor("#be185d")
                self.btn_ingest.setStyleSheet(f"background-color: #db2777; {btn_base_style}")
                self.btn_analyze.setStyleSheet(f"background-color: #be185d; {btn_base_style}")
                self.btn_manage_portfolio.setStyleSheet(f"background-color: #9d174d; {btn_base_style}")
                self.btn_calc.setStyleSheet(f"background-color: #e11d48; {btn_base_style}")
                self.btn_set_cash.setStyleSheet(f"background-color: #831843; {btn_base_style}")
                self.btn_settings.setStyleSheet(f"background-color: #f472b6; color: #500724; font-weight: bold; border-radius: 6px; padding: 6px 14px; font-size: 12px;")
                self.btn_top10.setStyleSheet(f"background-color: #be185d; {btn_base_style}")
                self.lbl_account_header.setStyleSheet("font-size: 13px; font-weight: bold; background-color: #fce7f3; color: #831843; padding: 8px; border-radius: 8px; border: 1px solid #fbcfe8;")
                
                self.lbl_disclosure.setStyleSheet("font-size: 10px; font-weight: bold; color: #be185d; background: transparent;")
                self.lbl_status.setStyleSheet("font-size: 11px; font-weight: bold; color: #db2777; background: transparent;")
                
            elif "Velvet Rose" in theme_name:
                self.theme_highlight = QColor("#e11d48")
                self.btn_ingest.setStyleSheet(f"background-color: #e11d48; {btn_base_style}")
                self.btn_analyze.setStyleSheet(f"background-color: #be185d; {btn_base_style}")
                self.btn_manage_portfolio.setStyleSheet(f"background-color: #9f1239; {btn_base_style}")
                self.btn_calc.setStyleSheet(f"background-color: #fb7185; color: #20131a; font-weight: bold; border-radius: 6px; padding: 6px 14px; font-size: 12px;")
                self.btn_set_cash.setStyleSheet(f"background-color: #881337; {btn_base_style}")
                self.btn_settings.setStyleSheet(f"background-color: #4c1d32; color: #ffe4e6; border-radius: 6px; padding: 6px 14px; font-size: 12px;")
                self.btn_top10.setStyleSheet(f"background-color: #e11d48; {btn_base_style}")
                self.lbl_account_header.setStyleSheet("font-size: 13px; font-weight: bold; background-color: #311825; color: #fb7185; padding: 8px; border-radius: 8px; border: 1px solid #9f1239;")
                
                self.lbl_disclosure.setStyleSheet("font-size: 10px; font-weight: bold; color: #fb7185; background: transparent;")
                self.lbl_status.setStyleSheet("font-size: 11px; font-weight: bold; color: #fecdd3; background: transparent;")
                
            elif "Light" in theme_name:
                self.theme_highlight = QColor("#3182ce")
                self.btn_ingest.setStyleSheet(f"background-color: #3182ce; {btn_base_style}")
                self.btn_analyze.setStyleSheet(f"background-color: #38a169; {btn_base_style}")
                self.btn_manage_portfolio.setStyleSheet(f"background-color: #805ad5; {btn_base_style}")
                self.btn_calc.setStyleSheet(f"background-color: #dd6b20; {btn_base_style}")
                self.btn_set_cash.setStyleSheet(f"background-color: #d69e2e; {btn_base_style}")
                self.btn_settings.setStyleSheet(f"background-color: #4a5568; {btn_base_style}")
                self.btn_top10.setStyleSheet(f"background-color: #3182ce; {btn_base_style}")
                self.lbl_account_header.setStyleSheet("font-size: 13px; font-weight: bold; background-color: #ffffff; color: #2b6cb0; padding: 8px; border-radius: 8px; border: 1px solid #cbd5e0; box-shadow: 0 4px 6px rgba(0,0,0,0.1);")
                
                self.lbl_disclosure.setStyleSheet("font-size: 10px; font-weight: bold; color: #d69e2e; background: transparent;")
                self.lbl_status.setStyleSheet("font-size: 11px; font-weight: bold; color: #38a169; background: transparent;")
                
            elif "Midnight" in theme_name:
                self.theme_highlight = QColor("#0284c7")
                self.btn_ingest.setStyleSheet(f"background-color: #0284c7; {btn_base_style}")
                self.btn_analyze.setStyleSheet(f"background-color: #059669; {btn_base_style}")
                self.btn_manage_portfolio.setStyleSheet(f"background-color: #7c3aed; {btn_base_style}")
                self.btn_calc.setStyleSheet(f"background-color: #ea580c; {btn_base_style}")
                self.btn_set_cash.setStyleSheet(f"background-color: #d97706; {btn_base_style}")
                self.btn_settings.setStyleSheet(f"background-color: #475569; {btn_base_style}")
                self.btn_top10.setStyleSheet(f"background-color: #0284c7; {btn_base_style}")
                self.lbl_account_header.setStyleSheet("font-size: 13px; font-weight: bold; background-color: #1e293b; color: #38bdf8; padding: 8px; border-radius: 8px; border: 1px solid #334155;")
                
                self.lbl_disclosure.setStyleSheet("font-size: 10px; font-weight: bold; color: #d97706; background: transparent;")
                self.lbl_status.setStyleSheet("font-size: 11px; font-weight: bold; color: #10b981; background: transparent;")
                
            else:
                self.theme_highlight = QColor("#3198dc")
                self.btn_ingest.setStyleSheet(f"background-color: #059669; {btn_base_style}") 
                self.btn_analyze.setStyleSheet(f"background-color: #0d9488; {btn_base_style}") 
                self.btn_manage_portfolio.setStyleSheet(f"background-color: #9333ea; {btn_base_style}") 
                self.btn_calc.setStyleSheet(f"background-color: #d97706; {btn_base_style}") 
                self.btn_set_cash.setStyleSheet(f"background-color: #2563eb; {btn_base_style}") 
                self.btn_settings.setStyleSheet(f"background-color: #334155; {btn_base_style}") 
                self.btn_top10.setStyleSheet(f"background-color: #ca8a04; {btn_base_style}") 
                
                self.lbl_account_header.setStyleSheet("font-size: 13px; font-weight: bold; background-color: #1a1d24; color: #e2e2e8; padding: 8px; border-radius: 8px; border: 1px solid #2d3748;")
                
                self.lbl_disclosure.setStyleSheet("font-size: 10px; font-weight: bold; color: #d69e2e; background: transparent;")
                self.lbl_status.setStyleSheet("font-size: 11px; font-weight: bold; color: #38a169; background: transparent;")

    def _init_ui(self):
        main_widget = QWidget()
        main_widget.setObjectName("main_widget")
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # Main header wrap
        header_wrap = QWidget()
        header_wrap.setObjectName("webPanel")
        header_layout = QVBoxLayout(header_wrap)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_layout.setSpacing(6)

        # 1. Top Row: Brand & Status Messages & Date/User
        top_row = QHBoxLayout()
        
        # Left: Brand
        brand_layout = QHBoxLayout()
        if LOGO_PATH.exists():
            self.lbl_brand_logo = QLabel()
            logo_pixmap = QPixmap(str(LOGO_PATH)).scaledToHeight(32, Qt.TransformationMode.SmoothTransformation)
            self.lbl_brand_logo.setPixmap(logo_pixmap)
            brand_layout.addWidget(self.lbl_brand_logo)
        
        brand_text_col = QVBoxLayout()
        brand_text_col.setSpacing(0)
        self.lbl_brand_title = QLabel("MB-EGX")
        self.lbl_brand_title.setStyleSheet("font-size: 18px; font-weight: 900; color: #93ccff; letter-spacing: 1px;")
        self.lbl_brand_tagline = QLabel("Out-of-Core Trading Matrix & Sector Dashboard")
        self.lbl_brand_tagline.setStyleSheet("font-size: 10px; color: #cbd5e0;")
        brand_text_col.addWidget(self.lbl_brand_title)
        brand_text_col.addWidget(self.lbl_brand_tagline)
        brand_layout.addLayout(brand_text_col)
        top_row.addLayout(brand_layout)
        top_row.addStretch(1)

        # Center: Progress & Status Messages Reallocated Here
        msg_layout = QVBoxLayout()
        msg_layout.setSpacing(2)
        msg_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.lbl_disclosure = QLabel("⚠️ Educational tool, not investment advice. Sector Breadth, VWAP entries, and Sortino risk penalties applied.")
        self.lbl_disclosure.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.lbl_status = QLabel("System Idle. Ready for processing.")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(4)
        
        msg_layout.addWidget(self.lbl_disclosure)
        msg_layout.addWidget(self.lbl_status)
        msg_layout.addWidget(self.progress_bar)
        
        top_row.addLayout(msg_layout, stretch=2)
        top_row.addStretch(1)

        # Right: Status (Date + User)
        status_layout = QVBoxLayout()
        status_layout.setSpacing(2)
        self.lbl_last_date = QLabel("📅 Last Data Date: Loading...")
        self.lbl_last_date.setStyleSheet("font-weight: bold; color: #93ccff; font-size: 11px;")
        self.lbl_welcome_user = QLabel("")
        if self.user_info:
            self.lbl_welcome_user.setText(f"👋 {self.user_info['name']}")
            self.lbl_welcome_user.setStyleSheet("font-weight: bold; color: #cbd5e0; font-size: 11px;")
        status_layout.addWidget(self.lbl_last_date, alignment=Qt.AlignmentFlag.AlignRight)
        status_layout.addWidget(self.lbl_welcome_user, alignment=Qt.AlignmentFlag.AlignRight)
        top_row.addLayout(status_layout)
        
        header_layout.addLayout(top_row)

        # 2. Controls Row
        controls_row = QHBoxLayout()
        controls_row.setSpacing(8)

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

        self.btn_top10 = QPushButton("🏆 Top 10")
        self.btn_top10.clicked.connect(self.show_top10_overview)

        controls_row.addWidget(self.btn_ingest)
        controls_row.addWidget(self.btn_analyze)
        controls_row.addWidget(self.btn_manage_portfolio)
        controls_row.addWidget(self.btn_calc)
        controls_row.addWidget(self.btn_set_cash)
        controls_row.addWidget(self.btn_settings)
        controls_row.addWidget(self.btn_top10)

        self.cmb_lang = QComboBox()
        self.cmb_lang.addItems(["🇬🇧 EN", "🇪🇬 AR"])
        self.cmb_lang.currentIndexChanged.connect(self.switch_language)
        self.cmb_lang.setFixedWidth(80)
        controls_row.addWidget(self.cmb_lang)
        
        self.btn_analytics = QPushButton("📊 Analytics")
        self.btn_analytics.setStyleSheet("background-color: #0d9488; color: white; font-weight: bold; padding: 6px 12px; font-size: 12px; border-radius: 6px;")
        self.btn_analytics.clicked.connect(self.open_analytics_dialog)
        self.btn_analytics.setVisible(bool(self.user_info and self.user_info.get("email") in ADMIN_EMAILS))
        controls_row.addWidget(self.btn_analytics)

        controls_row.addStretch()
        header_layout.addLayout(controls_row)

        # 3. Scan Folder Row
        dir_layout = QHBoxLayout()
        dir_layout.setSpacing(8)
        self.lbl_dir = QLabel("📂 Scan Folder:")
        self.lbl_dir.setStyleSheet("font-weight: bold; font-size: 12px; color: #cbd5e0;")
        dir_layout.addWidget(self.lbl_dir)
        self.txt_scan_dir = QLineEdit(str(WATCH_DIR))
        dir_layout.addWidget(self.txt_scan_dir, stretch=1)
        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.setStyleSheet("background-color: #4a5568; color: white; padding: 6px 12px; border-radius: 6px; font-size: 11px;")
        self.btn_browse.clicked.connect(self.browse_folder)
        dir_layout.addWidget(self.btn_browse)
        
        header_layout.addLayout(dir_layout)
        layout.addWidget(header_wrap)

        # Account Equity Bar
        self.lbl_account_header = QLabel()
        self.lbl_account_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_account_header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.lbl_account_header)

        # Filters Wrap
        filter_wrap = QWidget()
        filter_wrap.setObjectName("filterPanel")
        filter_layout = QHBoxLayout(filter_wrap)
        filter_layout.setContentsMargins(10, 6, 10, 6)
        filter_layout.setSpacing(6)
        
        self.lbl_filter = QLabel("🔍 Live Filters:")
        self.lbl_filter.setStyleSheet("font-weight: bold; font-size: 13px; color: #93ccff;")
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
        self.chk_hide_illiquid.setStyleSheet("background-color: #334155; color: white; padding: 6px 12px; font-size: 11px; border-radius: 6px;")
        self.chk_hide_illiquid.clicked.connect(self.apply_filters)
        filter_layout.addWidget(self.chk_hide_illiquid)

        self.btn_columns = QPushButton("👁️ Columns")
        self.btn_columns.setStyleSheet("background-color: #4a5568; color: white; padding: 6px 12px; font-size: 11px; border-radius: 6px;")
        self.btn_columns.clicked.connect(self.open_column_chooser)
        filter_layout.addWidget(self.btn_columns)

        self.btn_reset_filters = QPushButton("Reset Filters")
        self.btn_reset_filters.setStyleSheet("background-color: #4a5568; color: white; padding: 6px 12px; font-size: 11px; border-radius: 6px;")
        self.btn_reset_filters.clicked.connect(self.reset_filters)
        filter_layout.addWidget(self.btn_reset_filters)
        
        layout.addWidget(filter_wrap)

        self.tabs = QTabWidget()
        self.tbl_buys = self._create_matrix_table()
        
        self.tbl_sectors = QTableWidget()
        sector_cols = ["Sector", "Stocks", "1D Return (%)", "5D Return (%)", "Money Flow (CMF)", "Bullish Breadth (%)", "Traded Value (EGP)", "Sector Leader", "Sector Status"]
        self.tbl_sectors.setColumnCount(len(sector_cols))
        self.tbl_sectors.setHorizontalHeaderLabels(sector_cols)
        self.tbl_sectors.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

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

        tab_history_widget = QWidget()
        history_layout = QVBoxLayout(tab_history_widget)
        btn_export = QPushButton("📥 Export Tax & Audit Ledger (Excel/CSV)")
        btn_export.setStyleSheet("background-color: #38a169; color: white; font-weight: bold; padding: 8px 14px; font-size: 12px; border-radius: 6px;")
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

        self.tbl_fin_stmt = QTableWidget()
        self.tbl_fin_stmt.setColumnCount(2)
        self.tbl_fin_stmt.setHorizontalHeaderLabels(["Accounting Metric / Line Item", "Value (EGP / %)"])
        self.tbl_fin_stmt.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        self.tbl_top_strong = self._create_matrix_table()
        self.tbl_top_breakout = self._create_matrix_table()
        self.tbl_top_accum = self._create_matrix_table()
        self.tbl_top_dip = self._create_matrix_table()
        self.top10_overview_widget = self._build_top10_overview_tab()

        self.chart_widget = StockSectorChartWidget(self.qe, self.dbm, self)

        # Precise 8 Tab mappings matching TRANSLATIONS
        self.tabs.addTab(self.tbl_buys, "📈 Action Matrix")
        self.tabs.addTab(self.tbl_sectors, "🏢 Sectors")
        self.tabs.addTab(self.tbl_exits, "🛡️ Exits")
        self.tabs.addTab(self.tbl_breakout_watch, "🎯 Breakouts")
        self.tabs.addTab(tab_history_widget, "📜 History")
        self.tabs.addTab(self.tbl_fin_stmt, "📊 Financials")
        self.tabs.addTab(self.top10_overview_widget, "🏆 Top 10")
        self.tabs.addTab(self.chart_widget, "📊 Charts")

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
            view = self.tbl_breakout_watch
        elif current_idx == 4:
            view = self.tbl_closed
        elif current_idx == 5:
            view = self.tbl_fin_stmt
        elif current_idx == 6:
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

        self.tabs.setTabText(0, t.get("tab_matrix", "📈 Action Matrix"))
        self.tabs.setTabText(1, t.get("tab_sectors", "🏢 Sectors"))
        self.tabs.setTabText(2, t.get("tab_exits", "🛡️ Exits"))
        self.tabs.setTabText(3, t.get("tab_breakout", "🎯 Breakouts"))
        self.tabs.setTabText(4, t.get("tab_history", "📜 History"))
        self.tabs.setTabText(5, t.get("tab_fin", "📊 Financials"))
        self.tabs.setTabText(6, t.get("tab_top10", "🏆 Top 10"))
        self.tabs.setTabText(7, t.get("tab_charts", "📊 Charts"))

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
        container.setObjectName("top10Container")
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
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                            elif v_num < 0:
                                item.setForeground(QColor("#e53e3e"))
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
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
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                            elif val_num < 0:
                                item.setForeground(QColor("#e53e3e"))
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
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
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                            elif val_num < 0:
                                item.setForeground(QColor("#e53e3e"))
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                        except ValueError:
                            pass
                    self.tbl_closed.setItem(row_idx, col_idx, item)

            self.tbl_fin_stmt.setRowCount(len(fin_stmt))
            for row_idx, (metric_name, val_num) in enumerate(fin_stmt.items()):
                item_name = QTableWidgetItem(metric_name)
                item_name.setFlags(item_name.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item_name.setFont(QFont("Inter", 11, QFont.Weight.Bold))

                val_str = f"{val_num:,.2f}" if "%" not in metric_name else f"{val_num:,.2f}%"
                item_val = QTableWidgetItem(val_str)
                item_val.setFlags(item_val.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item_val.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                item_val.setFont(QFont("Inter", 11, QFont.Weight.Bold))

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
        tb_text = traceback.format_exc()
        logger.error(f"Fatal startup error:\n{tb_text}")
        _show_fatal_error(
            "MB-EGX — Failed to Start",
            f"{tb_text[-1200:]}\n\nFull details were written to quant_app.log."
        )
        sys.exit(1)
