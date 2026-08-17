import os
import sys
import time
import traceback
import json
from datetime import datetime, timezone, date
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

from config import WATCH_DIR, TRANSACTION_FEE_PCT, PORTFOLIO_RISK_THRESHOLDS, get_logger
from db_manager import DatabaseManager, DatabaseLockedError, set_language as _set_db_language
from decision_matrix import DecisionMatrix, set_language as _set_dm_language
from analytics import QuantitativeEngine
from chart_widget import StockSectorChartWidget
from ingestion import IngestionPipeline, set_language as _set_ing_language
from PyQt6.QtCore import QDate, Qt, QThread, QTimer, pyqtSignal, QAbstractTableModel, QModelIndex, QSettings
from PyQt6.QtGui import QFont, QColor, QPixmap, QIcon
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QCompleter, QDateEdit, QDialog, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QTableView, QTabWidget, QVBoxLayout, QWidget,
    QCheckBox, QTextEdit, QSizePolicy, QRadioButton, QFrame, QTextBrowser
)
from glossary_content import TERMS as GLOSSARY_TERMS, ACTION_LABELS as GLOSSARY_ACTIONS, CHART_PATTERNS as GLOSSARY_PATTERNS

logger = get_logger("app_gui")

LOGO_PATH = Path(__file__).resolve().parent / "assets" / "mb-egx-logo.png"
if not LOGO_PATH.exists():
    LOGO_PATH = Path(__file__).resolve().parent / "mb-egx-logo.jpg"
if not LOGO_PATH.exists():
    LOGO_PATH = Path("mb-egx-logo.jpg")

# =============================================================================
# FIREBASE AUTH + FIRESTORE (REST)
# =============================================================================
FIREBASE_API_KEY = "AIzaSyDlLv9qJFZ87mIztvbJZ0tBbwczYbnutwk"
FIREBASE_PROJECT_ID = "mb-egx-12d11"
FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/(default)/documents"
# Configurable via MBEGX_ADMIN_EMAILS (comma-separated) so the admin
# account isn't only ever a hardcoded literal shipped inside the compiled
# desktop app - falls back to the existing default if unset, so this is
# a no-op change for anyone who hasn't set the env var. Note this list is
# a UI-only gate (whether the "Usage Analytics" button is shown) - the
# actual permission boundary is enforced server-side by firestore.rules'
# own isAdmin() check, which independently gates every real data access.
ADMIN_EMAILS = [
    e.strip() for e in os.environ.get("MBEGX_ADMIN_EMAILS", "drmo071990@gmail.com").split(",") if e.strip()
]


def _safe_float(value, default: float = 0.0) -> float:
    """Coerce a matrix-row value (may be None/NaN/missing) to float for
    the Screener Preset predicates below, mirroring the `Number(x || 0)`
    / `Number(x ?? 100)` coercions the web dashboard's own predicates use."""
    try:
        f = float(value)
        return default if f != f else f  # f != f is the NaN check
    except (TypeError, ValueError):
        return default

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

DISCLAIMER_TEXT_AR = (
    "إقرار المستخدم والإخلاء القانوني للمسؤولية:\n\n"
    "من خلال الوصول إلى هذا التطبيق أو الاشتراك فيه أو استخدامه، فإنك تقر "
    "وتوافق صراحةً على الشروط التالية:\n\n"
    "• لأغراض المعلومات والتثقيف فقط: هذا التطبيق مخصص لأغراض إعلامية "
    "وتثقيفية فقط، ولا يُشكّل استشارة مالية أو استثمارية.\n\n"
    "• طبيعة الأدوات: تعمل الخدمة من خلال توفير أدوات تعليمية وتحليلية، فهي "
    "بمثابة مرآة لرؤية السوق. تُولَّد المخرجات من بيانات خام، ورسوم بيانية، "
    "واتجاهات تاريخية، وحسابات رياضية، ومؤشرات كمية، وأدوات فرز فني آلية.\n\n"
    "• لا توجد استشارة مالية مرخصة: لا يقوم التطبيق أو القائمون عليه بتوجيه "
    "إجراءات المستخدم المحددة، ولا يعملون كمدير محفظة. تقديم استشارة "
    "استثمارية مباشرة أو إدارة محفظة يتطلب ترخيصًا صارمًا من الهيئة العامة "
    "للرقابة المالية المصرية (FRA)، وهو ما لا يوفره هذا البرنامج.\n\n"
    "• تحليلات، وليست أوامر: جميع الإشارات والمخرجات التي يقدمها البرنامج "
    "تُصنَّف بدقة كتحليلات، مثل \"مخرجات مؤشر كمي\" أو \"أداة مطابقة الأنماط "
    "الفنية\"، ولا يجب تفسيرها أبدًا كتوصيات شراء أو بيع مباشرة أو أوامر "
    "سوقية. يوفر التطبيق البيانات، وعليك أن تقرر بنفسك ماذا تفعل بها.\n\n"
    "• تحمل المخاطر: يتحمل المستخدمون وحدهم المسؤولية الكاملة عن قراراتهم "
    "التداولية.\n\n"
    "• عدم التعامل مع أموال العملاء: يعمل هذا التطبيق حصريًا كأداة تحليلية "
    "ضمن نموذج البرمجيات كخدمة (SaaS). لن نطلب أو نحتفظ أو نسمح للمستخدمين "
    "بإيداع رأس مال التداول أو الأموال في حساباتنا البنكية أو محافظ "
    "التطبيق.\n\n"
    "• تنفيذ الصفقات عبر طرف ثالث: لا يمكنك تنفيذ الصفقات بشكل مستقل من خلال "
    "هذا التطبيق؛ يجب على جميع المستخدمين تنفيذ صفقاتهم الفعلية عبر شركات "
    "وساطة معتمدة ومرخصة في البورصة المصرية (مثل Thndr وEFG هيرميس، وغيرها)."
)

# =============================================================================
# i18n (English / Arabic)
# =============================================================================
# Language choice persists across launches via QSettings (the standard Qt
# mechanism for small per-user app preferences — no extra file to manage).
# New/first-time users (no stored preference yet) default to English.
_SETTINGS = QSettings("MB-EGX", "QuantDashboard")
CURRENT_LANG = _SETTINGS.value("lang", "EN")

# Keyed by the exact English string used at the call site — this lets us
# retrofit i18n onto an existing codebase without inventing a parallel set
# of semantic keys everywhere; tr() just looks up the literal you already
# wrote and returns its Arabic counterpart if one exists and Arabic is active.
AR_TRANSLATIONS = {
    "MB-EGX Alpha — Terminal Access": "إم بي-إي جي إكس ألفا — بوابة الدخول",
    "Ancient Legacy meets": "إرث عريق يلتقي",
    "Digital Future": "بمستقبل رقمي",
    "Vision:": "الرؤية:",
    "A transparent stock market where every investor has the insights to succeed.":
        "سوق أسهم شفاف يمتلك فيه كل مستثمر الرؤى اللازمة للنجاح.",
    "Mission:": "المهمة:",
    "We build seamless analytical platforms that decode EGX data, cut through the noise, and empower you to trade smarter.":
        "نبني منصات تحليلية متكاملة تفكّ رموز بيانات البورصة المصرية، وتزيل التشويش، وتمكّنك من التداول بذكاء أكبر.",
    "Terminal Access": "بوابة الدخول",
    "Sign in to view your private dashboard": "سجّل الدخول لعرض لوحتك الخاصة",
    "EMAIL ADDRESS": "البريد الإلكتروني",
    "PASSWORD": "كلمة المرور",
    "Forgot Access?": "نسيت بيانات الدخول؟",
    "I acknowledge and agree to the Terms.": "أقر وأوافق على الشروط.",
    "Sign In  →": "تسجيل الدخول  ←",
    "OR": "أو",
    "Sign in with Google": "تسجيل الدخول عبر Google",
    "Google Sign In": "تسجيل الدخول عبر Google",
    "In the desktop client, please use your Email/Password. If you created your account with Google on the web, click 'Forgot Access?' to set a password for desktop use.":
        "في تطبيق سطح المكتب، يرجى استخدام البريد الإلكتروني وكلمة المرور. إذا أنشأت حسابك عبر Google على الموقع، اضغط 'نسيت بيانات الدخول؟' لتعيين كلمة مرور لاستخدام سطح المكتب.",
    "Don't have an account?": "ليس لديك حساب؟",
    "Create Account": "إنشاء حساب",
    "🔒 AES-256 BANK GRADE ENCRYPTION MATRIX ENABLED": "🔒 مصفوفة تشفير AES-256 بمستوى المصارف مُفعّلة",
    "Enter both email and password.": "أدخل البريد الإلكتروني وكلمة المرور.",
    "Password must be at least {n} characters.": "يجب أن تتكون كلمة المرور من {n} أحرف على الأقل.",
    "You must explicitly agree to the legal terms to create an account.": "يجب الموافقة صراحةً على الشروط القانونية لإنشاء حساب.",
    "Sign-in failed. Please check your credentials and try again.": "فشل تسجيل الدخول. يرجى التحقق من بيانات الاعتماد والمحاولة مرة أخرى.",
    "Type your email above first, then click this link.": "اكتب بريدك الإلكتروني أعلاه أولاً، ثم اضغط هذا الرابط.",
    "Check Your Email": "تحقق من بريدك الإلكتروني",
    "If an account exists for {email}, a password-set/reset link has just been sent.\n\n"
    "This also works if you originally signed up with 'Sign in with Google' on the "
    "website — that account has no password yet, and this link lets you set one so "
    "you can sign in here on desktop too.":
        "إذا كان هناك حساب مرتبط بـ {email}، فقد تم إرسال رابط لتعيين/إعادة تعيين كلمة المرور.\n\n"
        "يعمل هذا أيضًا إذا سجّلت في الأصل عبر 'تسجيل الدخول عبر Google' على "
        "الموقع — ذلك الحساب ليس لديه كلمة مرور بعد، وهذا الرابط يتيح لك تعيين واحدة "
        "لتتمكن من تسجيل الدخول هنا على سطح المكتب أيضًا.",
    "Couldn't send the reset email. Double-check the address and try again.": "تعذّر إرسال رابط إعادة التعيين. تحقق من العنوان وحاول مجددًا.",
    "⚙️ Appearance & Theme Settings": "⚙️ إعدادات المظهر والألوان",
    "Choose your preferred visual dashboard palette:": "اختر لوحة الألوان المفضلة لديك:",
    "Color Theme:": "لوحة الألوان:",
    "✅ Close & Save": "✅ إغلاق وحفظ",
    "⚖️ Interactive Risk & Position-Sizing Calculator": "⚖️ حاسبة المخاطر وحجم المركز التفاعلية",
    "<b>Available Cash:</b> {v} EGP": "<b>الرصيد النقدي المتاح:</b> {v} جنيه",
    "Select symbol to auto-load price...": "اختر رمز السهم لتحميل السعر تلقائيًا...",
    "Ticker Symbol:": "رمز السهم:",
    "Manual Stop": "وقف يدوي", "1.5x ATR Stop": "وقف 1.5x ATR",
    "2.0x ATR Stop": "وقف 2.0x ATR", "3.0x ATR Stop": "وقف 3.0x ATR",
    "Stop-Loss Mode:": "وضع وقف الخسارة:",
    "Max Account Risk:": "أقصى مخاطرة للحساب:",
    "Target Entry Price (EGP):": "سعر الدخول المستهدف (جنيه):",
    "Stop-Loss Price (EGP):": "سعر وقف الخسارة (جنيه):",
    "⚠️ <b>Invalid Parameters:</b> Stop-Loss must be below Target Entry.": "⚠️ <b>معايير غير صحيحة:</b> يجب أن يكون وقف الخسارة أقل من سعر الدخول المستهدف.",
    "🎯 <b>Recommended Shares:</b> {shares} shares<br><br>"
    "💵 <b>Total Outlay (incl. 0.35% fee):</b> {outlay} EGP ({pct}% of cash)<br><br>"
    "🛡️ <b>Max Capital at Risk:</b> {risk} EGP":
        "🎯 <b>عدد الأسهم الموصى به:</b> {shares} سهم<br><br>"
        "💵 <b>إجمالي التكلفة (شاملة رسوم 0.35%):</b> {outlay} جنيه ({pct}% من الرصيد النقدي)<br><br>"
        "🛡️ <b>أقصى رأس مال معرّض للمخاطرة:</b> {risk} جنيه",
    "Institutional Portfolio & Trade Manager": "إدارة المحفظة والصفقات المؤسسية",
    "Type or select ticker (e.g. PHGC.CA)...": "اكتب أو اختر رمز السهم (مثل PHGC.CA)...",
    "Buy Price (EGP):": "سعر الشراء (جنيه):",
    "Number of Shares:": "عدد الأسهم:",
    "Purchase Date:": "تاريخ الشراء:",
    "📈 Add Shares / Scale In (Auto-Calculate Average)": "📈 إضافة أسهم / زيادة المركز (حساب المتوسط تلقائيًا)",
    "✏️ Correct Mistake": "✏️ تصحيح خطأ",
    "🗑️ Delete Position": "🗑️ حذف المركز",
    "🛒 Open / Add / Delete Position": "🛒 فتح / إضافة / حذف مركز",
    "Type or select sold ticker (e.g. PHGC.CA)...": "اكتب أو اختر رمز السهم المباع (مثل PHGC.CA)...",
    "Selling Price (EGP):": "سعر البيع (جنيه):",
    "Shares Sold:": "عدد الأسهم المباعة:",
    "Sell Date:": "تاريخ البيع:",
    "🤝 Record Sale & Calculate P&L": "🤝 تسجيل البيع وحساب الربح/الخسارة",
    "🤝 Record Sale / Close Trade": "🤝 تسجيل بيع / إغلاق صفقة",
    "🧹 Clear Sample Demo Data": "🧹 مسح بيانات تجريبية",
    "Input Error": "خطأ في الإدخال",
    "Please enter or select a valid Ticker Symbol.": "يرجى إدخال أو اختيار رمز سهم صحيح.",
    "Position Updated": "تم تحديث المركز",
    "Position Error": "خطأ في المركز",
    "Please select the Ticker Symbol to delete.": "يرجى اختيار رمز السهم المراد حذفه.",
    "Deleted": "تم الحذف",
    "Permanently removed {ticker} from your active portfolio.": "تمت إزالة {ticker} نهائيًا من محفظتك النشطة.",
    "Please enter or select the Ticker Symbol.": "يرجى إدخال أو اختيار رمز السهم.",
    "Sale Recorded!": "تم تسجيل البيع!",
    "Sale Error": "خطأ في البيع",
    "Samples Deleted": "تم حذف العينات",
    "Successfully removed demo samples without deleting real positions!": "تمت إزالة العينات التجريبية بنجاح دون حذف المراكز الحقيقية!",
    "📊 Usage Analytics": "📊 إحصائيات الاستخدام",
    "Sessions and trading activity combined across the website (🌐) and the desktop app (🖥️). "
    "Time is approximate (30s heartbeat). Trade Value = total EGP bought + sold; "
    "Portfolio Value = cash + open positions at cost.":
        "الجلسات ونشاط التداول مجمعة عبر الموقع (🌐) وتطبيق سطح المكتب (🖥️). "
        "الوقت تقريبي (نبضة كل 30 ثانية). قيمة التداول = إجمالي الشراء والبيع بالجنيه؛ "
        "قيمة المحفظة = النقد + المراكز المفتوحة بسعر التكلفة.",
    "Loading session data…": "جاري تحميل بيانات الجلسات…",
    "User": "المستخدم", "Sessions (🌐/🖥️)": "الجلسات (🌐/🖥️)", "Total Time": "إجمالي الوقت",
    "Trades": "الصفقات", "Trade Value": "قيمة التداول", "Portfolio Value": "قيمة المحفظة", "Last Seen": "آخر ظهور",
    "🔄 Refresh": "🔄 تحديث", "Close": "إغلاق",
    "⚠️ Could not load analytics data (check your connection or Firestore rules).": "⚠️ تعذّر تحميل بيانات الإحصائيات (تحقق من اتصالك أو قواعد Firestore).",
    "👥 Unique Users: {users}    |    "
    "📅 Total Sessions: {sessions}    |    "
    "⏱️ Avg Time / Session: {avg}":
        "👥 المستخدمون الفريدون: {users}    |    "
        "📅 إجمالي الجلسات: {sessions}    |    "
        "⏱️ متوسط الوقت / الجلسة: {avg}",
    "Stock ticker symbol": "رمز السهم",
    "Ticker": "الرمز", "Action": "الإجراء", "Trend": "الاتجاه",
    "Recommended action based on multi-factor confirmation": "الإجراء الموصى به بناءً على تأكيد متعدد العوامل",
    "Score": "الدرجة", "Composite rank score (higher = stronger signal)": "درجة التصنيف المركبة (كلما زادت، زادت قوة الإشارة)",
    "Price": "السعر", "Current close price": "سعر الإغلاق الحالي",
    "Entry (VWAP)": "سعر الدخول (VWAP)", "Suggested entry price benchmarked to 20-day Volume Weighted Average Price": "سعر الدخول المقترح مقاسًا بمتوسط السعر المرجح بالحجم لـ 20 يومًا",
    "Stop-Loss": "وقف الخسارة", "Suggested stop-loss price (2x ATR below current price)": "سعر وقف الخسارة المقترح (2x ATR أقل من السعر الحالي)",
    "Shares (1% Risk)": "الأسهم (مخاطرة 1%)", "Suggested share count so a stop-out costs ~1% of cash balance": "عدد الأسهم المقترح بحيث تكلف الخسارة القصوى ~1% من الرصيد النقدي",
    "Proj. Gain %": "الربح المتوقع %", "Historical-analog projected return": "العائد المتوقع بناءً على أنماط تاريخية مشابهة",
    "Pattern Conf %": "ثقة النموذج %", "Confidence in the historical pattern match (Sortino downside-penalized)": "الثقة في تطابق النموذج التاريخي (معدّلة بعقوبة الهبوط Sortino)",
    "Trend classification": "تصنيف الاتجاه",
    "Vol Z-Score": "انحراف الحجم", "Rolling 20-day Volume Standard Deviation anomaly (Z >= 1.5 indicates institutional influx)": "شذوذ الانحراف المعياري للحجم خلال 20 يومًا (Z >= 1.5 يشير إلى تدفق مؤسسي)",
    "MACD Signal": "إشارة MACD", "Momentum direction/crossover state from the 12/26/9 MACD": "اتجاه الزخم/حالة التقاطع من MACD (12/26/9)",
    "MACD Hist.": "مدرج MACD", "MACD histogram value (MACD line minus signal line)": "قيمة مدرج MACD (خط MACD ناقص خط الإشارة)",
    "Bollinger %B": "بولينجر %B", "Where price sits within the 20-period Bollinger Bands (0 = lower band, 1 = upper band)": "موقع السعر ضمن نطاقات بولينجر لـ 20 فترة (0 = النطاق السفلي، 1 = النطاق العلوي)",
    "Avg Vol (20D)": "متوسط الحجم (20 يوم)", "20-day average traded volume (shares)": "متوسط حجم التداول خلال 20 يومًا (بالأسهم)",
    "Data Conf.": "دقة البيانات", "How much real history backs these numbers": "مقدار البيانات التاريخية الفعلية الداعمة لهذه الأرقام",
    "Take-Profit": "جني الأرباح", "Suggested take-profit target (pattern-match or ATR-floor based)": "هدف جني الأرباح المقترح (بناءً على مطابقة النموذج أو حد ATR)",
    "Nearest pivot resistance (from last completed week's H/L/C)": "أقرب مستوى مقاومة محوري (من أعلى/أدنى/إغلاق آخر أسبوع مكتمل)",
    "Second pivot resistance level": "مستوى المقاومة المحوري الثاني",
    "Third (furthest) pivot resistance level": "مستوى المقاومة المحوري الثالث (الأبعد)",
    "Nearest pivot support (from last completed week's H/L/C)": "أقرب مستوى دعم محوري (من أعلى/أدنى/إغلاق آخر أسبوع مكتمل)",
    "Second pivot support level": "مستوى الدعم المحوري الثاني",
    "Third (furthest) pivot support level": "مستوى الدعم المحوري الثالث (الأبعد)",
    # Action badges (exact strings from decision_matrix.py)
    "🛡️ HOLD / TRAIL STOP": "🛡️ احتفاظ / تتبع وقف الخسارة",
    "⚠️ CUT LOSS / REVIEW (Below VWAP)": "⚠️ قطع الخسارة / مراجعة (تحت VWAP)",
    "💰 TAKE PROFIT ZONE": "💰 منطقة جني الأرباح",
    "🛡️ HOLD / TRAIL STOP (Low Data)": "🛡️ احتفاظ / تتبع وقف الخسارة (بيانات محدودة)",
    "🛑 SELL / AVOID": "🛑 بيع / تجنب",
    "🔥 STRONG BUY": "🔥 شراء قوي",
    "⚡ BREAKOUT BUY (X-OVER + MOMENTUM)": "⚡ شراء اختراق (تقاطع + زخم)",
    "⚡ BREAKOUT BUY (X-OVER)": "⚡ شراء اختراق (تقاطع)",
    "⚡ BREAKOUT BUY (MOMENTUM)": "⚡ شراء اختراق (زخم)",
    "⏳ BUY ON DIP": "⏳ شراء عند الهبوط",
    "📈 ACCUMULATE": "📈 تجميع",
    # Live-filter dropdown items (generic/aggregate labels, distinct from
    # the specific badge variants above)
    "All Actions": "كل الإجراءات",
    "⚡ BREAKOUT BUY": "⚡ شراء اختراق",
    "🟡 HOLD / NEUTRAL": "🟡 احتفاظ / محايد",
    "All Trends": "كل الاتجاهات",
    "All Data Confidence": "كل مستويات الثقة",
    # Trend Class (exact strings from analytics.py)
    "Strong Bullish": "صعودي قوي",
    "Weak Bullish (Low Trend Strength)": "صعودي ضعيف (قوة اتجاه منخفضة)",
    "Weak Bullish": "صعودي ضعيف",
    "Consolidation / Neutral": "تماسك / محايد",
    "Strong Bearish": "هبوطي قوي",
    "Weak Bearish (Low Trend Strength)": "هبوطي ضعيف (قوة اتجاه منخفضة)",
    "Weak Bearish": "هبوطي ضعيف",
    "Insufficient Data": "بيانات غير كافية",
    # Data Confidence tiers (exact strings from analytics.py's data_confidence_tier)
    "Very Low (New/Short History)": "منخفضة جدًا (سجل جديد/قصير)",
    "Low (<3 Months)": "منخفضة (أقل من 3 أشهر)",
    "Medium (<1 Year)": "متوسطة (أقل من سنة)",
    "High (1Y+)": "عالية (سنة+)",
    "Set Account Cash Balance": "تحديد الرصيد النقدي للحساب",
    "Enter available cash balance in EGP:": "أدخل الرصيد النقدي المتاح بالجنيه:",
    "Cash Updated": "تم تحديث الرصيد النقدي",
    "Account cash balance successfully updated to: {v} EGP.": "تم تحديث الرصيد النقدي للحساب بنجاح إلى: {v} جنيه.",
    "How would you like to update your cash balance?\n\n• Set Exact Amount — directly overwrite the balance (use this if you already know the correct number).\n• Recalculate From Trades — enter what you started with BEFORE your first trade, and the app rebuilds the balance from your full buy/sell history (fixes drift from before cash tracking was wired up).": "كيف تريد تحديث رصيدك النقدي؟\n\n• تحديد مبلغ محدد — استبدال الرصيد مباشرة (استخدم هذا إذا كنت تعرف الرقم الصحيح بالفعل).\n• إعادة الحساب من الصفقات — أدخل رأس المال الذي بدأت به قبل أول صفقة، وسيقوم التطبيق بإعادة بناء الرصيد من سجل الشراء والبيع الكامل (يصلح أي انحراف حدث قبل ربط تتبع النقد بشكل صحيح).",
    "Set Exact Amount": "تحديد مبلغ محدد",
    "Recalculate From Trades": "إعادة الحساب من الصفقات",
    "Recalculate Cash From Trade History": "إعادة حساب النقد من سجل الصفقات",
    "Enter the cash you started with BEFORE your very first trade (EGP):": "أدخل رأس المال الذي بدأت به قبل أول صفقة (بالجنيه):",
    "Cash Recalculated": "تمت إعادة حساب الرصيد النقدي",
    "Rebuilt from your full trade history. New cash balance: {v} EGP.": "تمت إعادة البناء من سجل صفقاتك الكامل. الرصيد النقدي الجديد: {v} جنيه.",
    "No real (non-demo) closed trades available to export.": "لا توجد صفقات مغلقة حقيقية (غير تجريبية) متاحة للتصدير.",
    "({n} demo trade(s) were excluded.)": "(تم استبعاد {n} صفقة/صفقات تجريبية.)",
    "Export Error": "خطأ في التصدير",
    "Export Audit Ledger": "تصدير سجل المراجعة",
    "CSV Files (*.csv);;Excel Files (*.xlsx)": "ملفات CSV (*.csv);;ملفات Excel (*.xlsx)",
    "Export Successful": "تم التصدير بنجاح",
    "Audit ledger successfully saved to:\n{path}\n\nWin Rate: {wr}% | Profit Factor: {pf}": "تم حفظ سجل المراجعة بنجاح في:\n{path}\n\nمعدل الفوز: {wr}% | معامل الربحية: {pf}",
    "Export Failed": "فشل التصدير",
    "Could not save file:\n{err}": "تعذّر حفظ الملف:\n{err}",
    "Invalid Folder": "مجلد غير صحيح",
    "The directory does not exist:\n{dir}": "المجلد غير موجود:\n{dir}",
    "MB-EGX — Out-of-Core Trading Matrix & Sector Dashboard": "إم بي-إي جي إكس — لوحة مصفوفة التداول والقطاعات",

    # --- Sector tab: column headers ---
    "Stocks": "عدد الشركات",
    "1D Return (%)": "(%) عائد يوم",
    "5D Return (%)": "(%) عائد 5 أيام",
    "Money Flow (CMF)": "التدفق النقدي (CMF)",
    "Bullish Breadth (%)": "(%) الاتساع الصعودي",
    "Traded Value (EGP)": "قيمة التداول (جنيه)",
    "Sector Leader": "قائد القطاع",
    "Sector Status": "حالة القطاع",
    # Sector Status badges (exact strings from analytics.py's compute_sector_analytics)
    "🟢 STRONG INFLOW": "🟢 تدفق قوي للداخل",
    "⚡ BREAKOUT": "⚡ اختراق",
    "🔴 HEAVY DISTRIBUTION": "🔴 توزيع كثيف",
    "⚪ CONSOLIDATION": "⚪ تماسك",
    # Canonical sector names (exact strings from db_manager.py's clean_sector_name)
    "Non-Bank Financial Services": "خدمات مالية غير مصرفية",
    "Food, Beverages & Tobacco": "الأغذية والمشروبات والتبغ",
    "Textiles & Durables": "المنسوجات والسلع المعمرة",
    "IT, Media & Communication Services": "تكنولوجيا المعلومات والإعلام والاتصالات",
    "Industrial Goods, Services & Automobiles": "السلع والخدمات الصناعية والسيارات",
    "Construction & Engineering": "التشييد والهندسة",
    "Health Care & Pharmaceuticals": "الرعاية الصحية والأدوية",
    "Basic Resources": "الموارد الأساسية",
    "Building Materials": "مواد البناء",
    "Travel & Leisure": "السياحة والترفيه",
    "Shipping & Transportation Services": "الشحن وخدمات النقل",
    "Trade & Distributors": "التجارة والموزعون",
    "Energy & Support Services": "الطاقة والخدمات المساندة",
    "Education Services": "الخدمات التعليمية",
    "Paper & Packaging": "الورق والتغليف",
    "Banks": "البنوك",
    "Real Estate": "العقارات",
    "Chemicals": "الكيماويات",
    "Utilities": "المرافق العامة",
    "General / Diversified": "عام / متنوع",

    # --- Exits tab: column headers ---
    "Shares": "الأسهم",
    "Buy Price": "سعر الشراء",
    "P&L (EGP)": "الربح/الخسارة (جنيه)",
    "P&L (%)": "(%) الربح/الخسارة",
    "Trail Stop": "وقف متحرك",
    "Purchase Date": "تاريخ الشراء",
    # --- Exits tab: new analytics columns ---
    "Net P&L (EGP)": "صافي الربح/الخسارة (جنيه)",
    "Net P&L (%)": "(%) صافي الربح/الخسارة",
    "Dist. to Stop %": "(%) المسافة لوقف الخسارة",
    "Annualized %": "(%) العائد السنوي",
    "Drawdown from Peak %": "(%) التراجع من القمة",
    "Risk vs Plan": "الحجم مقابل خطة المخاطرة",
    "This position is {p}% of your account equity — over the single-position concentration threshold.": "تمثل هذه الصفقة {p}% من رأس مالك — أعلى من حد التركز المسموح به لصفقة واحدة.",
    "Cash Drag (%)": "(%) النقد غير المستثمر",
    "💵 {pct}% cash — fully invested, no dry powder for new signals.": "💵 {pct}% نقدًا فقط — رأس المال مستثمر بالكامل، لا توجد سيولة لصفقات جديدة.",
    "🔄 You're holding {held} ({hs}) but the matrix now prefers {cand} ({cs}) in the same {cat} pool.": "🔄 أنت تمتلك {held} ({hs}) لكن المصفوفة تفضّل الآن {cand} ({cs}) ضمن نفس فئة {cat}.",
    "🔄 {held} outranked by {cand} in the same {cat} pool.": "🔄 المصفوفة تفضّل {cand} على {held} ضمن نفس فئة {cat}.",
    "{first}  •  +{n} more risk note(s) — hover for details": "{first}  •  +{n} ملاحظة أخرى — مرر الفأرة لعرض التفاصيل",

    # --- History (closed trades) tab: column headers ---
    "Shares Sold": "الأسهم المباعة",
    "Sell Price": "سعر البيع",
    "Realized P&L (EGP)": "الربح/الخسارة المحققة (جنيه)",
    "Realized P&L (%)": "(%) الربح/الخسارة المحققة",
    "Sell Date": "تاريخ البيع",

    # --- Breakout watch tab: column headers + values ---
    "Breakout Score": "درجة الاختراق",
    "Dist. to Resistance %": "المسافة للمقاومة %",
    "Squeeze": "الانضغاط",
    "Volume Trend": "اتجاه الحجم",
    "Signals": "الإشارات",
    "Rising": "صاعد",
    "Flat/Falling": "مستقر/هابط",
    "✅ Yes": "✅ نعم",
    # Signal-sentence fragments (joined with ", " in decision_matrix.py's
    # breakout-watch screening — matched as substrings, not exact strings)
    "Volatility squeeze": "انضغاط تذبذب",
    "ADX trend just building": "اتجاه ADX في طور التكوّن",
    "RSI bullish with room to run": "RSI صعودي مع مجال للاستمرار",
    "Volume trending up": "الحجم في اتجاه صاعد",
    "Near recent high (resistance test)": "قرب القمة الأخيرة (اختبار مقاومة)",
    "Positive money flow": "تدفق نقدي إيجابي",
    "Weekly trend aligned": "توافق الاتجاه الأسبوعي",

    # --- Financials tab: column headers + row labels ---
    "Accounting Metric / Line Item": "البيان المحاسبي",
    "Value (EGP / %)": "القيمة (جنيه / %)",
    "Cash Balance (EGP)": "الرصيد النقدي (جنيه)",
    "Stock Portfolio Cost Basis (EGP)": "تكلفة محفظة الأسهم (جنيه)",
    "Stock Portfolio Market Value (EGP)": "القيمة السوقية لمحفظة الأسهم (جنيه)",
    "Unrealized Stock P&L (EGP)": "الربح/الخسارة غير المحققة للأسهم (جنيه)",
    "Unrealized Stock P&L (%)": "(%) الربح/الخسارة غير المحققة للأسهم",
    "Realized P&L from Closed Trades (EGP)": "الربح/الخسارة المحققة من الصفقات المغلقة (جنيه)",
    "Total Account Equity / Net Worth (EGP)": "إجمالي حقوق الحساب / صافي الثروة (جنيه)",

    # --- Top 10 tab: section titles ---
    "🔥 Top 10 Strong Buy": "🔥 أفضل 10 شراء قوي",
    "⚡ Top 10 Breakout": "⚡ أفضل 10 اختراق",
    "📈 Top 10 Accumulate": "📈 أفضل 10 تجميع",
    "⏳ Top 10 Buy on Dip": "⏳ أفضل 10 شراء عند الهبوط",

    # --- Action badge fragments (dynamically appended in decision_matrix.py,
    # so the full assembled string rarely matches a dict entry exactly —
    # tr() falls back to substituting these as substrings instead). ---
    " [💥 SQUEEZE]": " [💥 انضغاط]",
    " [👑 WEEKLY ALIGNED]": " [👑 توافق أسبوعي]",
    "🚫 ILLIQUID - ": "🚫 سيولة ضعيفة - ",
}

# Sorted longest-first so multi-word fragments (e.g. an entire action badge)
# are substituted before any shorter fragment they might contain.
_AR_FRAGMENTS_BY_LEN = sorted(AR_TRANSLATIONS.keys(), key=len, reverse=True)


def tr(text):
    """Return the Arabic translation of `text` if Arabic is the active
    language and a translation exists; otherwise return `text` unchanged.

    Falls back to fragment-level substitution for strings assembled at
    runtime (action badges with SQUEEZE/WEEKLY ALIGNED suffixes or an
    ILLIQUID prefix, breakout-watch "Signals" sentences, etc.) that won't
    have an exact-match entry of their own.
    """
    if CURRENT_LANG != "AR":
        return text
    direct = AR_TRANSLATIONS.get(text)
    if direct is not None:
        return direct
    result = text
    for frag in _AR_FRAGMENTS_BY_LEN:
        if frag in result:
            result = result.replace(frag, AR_TRANSLATIONS[frag])
    return result


def set_language(lang):
    """Switch the active language and persist the choice for next launch."""
    global CURRENT_LANG
    CURRENT_LANG = lang
    _SETTINGS.setValue("lang", lang)
    _set_db_language(lang)
    _set_dm_language(lang)
    _set_ing_language(lang)


# Keep the backend modules' small sets of user-facing message translations
# in sync with whatever language was persisted from a previous launch, right
# from import time — not just after the next toggle.
_set_db_language(CURRENT_LANG)
_set_dm_language(CURRENT_LANG)
_set_ing_language(CURRENT_LANG)


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
    if "stringValue" in v:
        return v["stringValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "booleanValue" in v:
        return v["booleanValue"]
    if "timestampValue" in v:
        return v["timestampValue"] 
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
    if requests is None:
        raise RuntimeError("The 'requests' package is required. Install it with: pip install requests")
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={FIREBASE_API_KEY}"
    resp = requests.post(url, json={"requestType": "PASSWORD_RESET", "email": email}, timeout=10)
    data = resp.json()
    if resp.status_code != 200:
        raise RuntimeError(data.get("error", {}).get("message", "Could not send reset email."))
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
    QCheckBox { color: #e2e2e8; font-weight: bold; font-size: 12px; spacing: 6px; }
    QCheckBox::indicator { width: 14px; height: 14px; border-radius: 4px; border: 1px solid #2d3748; background-color: #0f1115; }
    QCheckBox::indicator:checked { background-color: #3198dc; border: 1px solid #93ccff; }
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
    QCheckBox { color: #1a202c; font-weight: bold; font-size: 12px; spacing: 6px; }
    QCheckBox::indicator { width: 14px; height: 14px; border-radius: 4px; border: 1px solid #a0aec0; background-color: #ffffff; }
    QCheckBox::indicator:checked { background-color: #2b6cb0; border: 1px solid #2b6cb0; }
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
    QCheckBox { color: #f8fafc; font-weight: bold; font-size: 12px; spacing: 6px; }
    QCheckBox::indicator { width: 14px; height: 14px; border-radius: 4px; border: 1px solid #475569; background-color: #0f172a; }
    QCheckBox::indicator:checked { background-color: #0284c7; border: 1px solid #38bdf8; }
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
    QCheckBox { color: #500724; font-weight: bold; font-size: 12px; spacing: 6px; }
    QCheckBox::indicator { width: 14px; height: 14px; border-radius: 4px; border: 1px solid #f472b6; background-color: #ffffff; }
    QCheckBox::indicator:checked { background-color: #ec4899; border: 1px solid #ec4899; }
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
    QCheckBox { color: #ffe4e6; font-weight: bold; font-size: 12px; spacing: 6px; }
    QCheckBox::indicator { width: 14px; height: 14px; border-radius: 4px; border: 1px solid #9f1239; background-color: #20131a; }
    QCheckBox::indicator:checked { background-color: #e11d48; border: 1px solid #fb7185; }
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
            ("Position", "Whether this is a fresh candidate or a position you already own being re-evaluated for scaling in"),
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
            ("MACD Signal", "Momentum direction/crossover state from the 12/26/9 MACD"),
            ("MACD Hist.", "MACD histogram value (MACD line minus signal line)"),
            ("Bollinger %B", "Where price sits within the 20-period Bollinger Bands (0 = lower band, 1 = upper band)"),
            ("Avg Vol (20D)", "20-day average traded volume (shares)"),
            ("Data Conf.", "How much real history backs these numbers"),
            ("Take-Profit", "Suggested take-profit target (pattern-match or ATR-floor based)"),
            ("R1", "Nearest pivot resistance (from last completed week's H/L/C)"),
            ("R2", "Second pivot resistance level"),
            ("R3", "Third (furthest) pivot resistance level"),
            ("S1", "Nearest pivot support (from last completed week's H/L/C)"),
            ("S2", "Second pivot support level"),
            ("S3", "Third (furthest) pivot support level"),
        ]
        self._col_keys = [
            "Ticker", "Position", "Action", "Rank Score", "Current Price", "Target Entry (VWAP)",
            "Suggested Stop-Loss", "Suggested Shares (1% Risk)", "Projected Gain (%)",
            "Pattern Conf (%)", "Trend Class", "RSI-14", "ADX-14", "Vol Z-Score",
            "MACD Signal", "MACD Histogram", "Bollinger %B",
            "Avg Volume (20D)", "Data Confidence",
            "Take-Profit Target", "R1", "R2", "R3", "S1", "S2", "S3",
            "Kelly %", "Signal Reason",
        ]
        self._columns = self._columns + [
            ("Kelly %", "Position-size fraction suggested by the Kelly criterion (half-Kelly capped)."),
            ("Signal Reason", "Plain-language evidence behind the action, straight from the decision matrix."),
        ]


    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return len(self._columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return tr(self._columns[section][0])
            elif role == Qt.ItemDataRole.ToolTipRole:
                return tr(self._columns[section][1])
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        key = self._col_keys[col]
        val = self._data[row].get(key, "")
        if val is None:
            val = "-"
        val_str = str(val)

        if role == Qt.ItemDataRole.DisplayRole:
            if key in ("Action", "Trend Class", "Data Confidence", "Position"):
                return tr(val_str)
            return val_str
        elif role == Qt.ItemDataRole.TextAlignmentRole:
            if key in ["Action", "Data Confidence", "Position"]:
                return int(Qt.AlignmentFlag.AlignCenter)
            elif (3 <= col <= 9) or (15 <= col <= 16) or (19 <= col <= 25):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        elif role == Qt.ItemDataRole.ToolTipRole:
            if key == "Action" and self._data[row].get("Signal Reason"):
                return str(self._data[row]["Signal Reason"])
        elif role == Qt.ItemDataRole.BackgroundRole:
            if key == "Position" and "OWNED" in val_str:
                return QColor("#553c9a")
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
            if key == "Position" and "OWNED" in val_str:
                return QColor("#ffffff")
            elif key == "Action":
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
        self.setWindowTitle(tr("⚙️ Appearance & Theme Settings"))
        self.resize(400, 180)
        self.apply_callback = apply_callback
        self._init_ui(current_theme_name)

    def _init_ui(self, current_theme_name):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        lbl_info = QLabel(tr("Choose your preferred visual dashboard palette:"))
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet("font-size: 13px;")
        layout.addWidget(lbl_info)

        self.cmb_themes = QComboBox()
        self.cmb_themes.addItems(list(THEMES_MAP.keys()))
        if current_theme_name in THEMES_MAP:
            self.cmb_themes.setCurrentText(current_theme_name)
        
        self.cmb_themes.currentTextChanged.connect(self.apply_callback)
        form.addRow(tr("Color Theme:"), self.cmb_themes)
        layout.addLayout(form)

        btn_layout = QHBoxLayout()
        btn_close = QPushButton(tr("✅ Close & Save"))
        btn_close.setStyleSheet("background-color: #3198dc; color: white; margin-top: 10px; padding: 10px 20px; font-size: 13px; border-radius: 6px;")
        btn_close.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_close)
        layout.addLayout(btn_layout)


class PositionSizingDialog(QDialog):
    def __init__(self, dbm, cash_balance, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("⚖️ Interactive Risk & Position-Sizing Calculator"))
        self.resize(480, 400)
        self.dbm = dbm
        self.qe = QuantitativeEngine()
        self.cash = cash_balance
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        form = QFormLayout()
        lbl_cash = QLabel(tr("<b>Available Cash:</b> {v} EGP").format(v=f"{self.cash:,.2f}"))
        lbl_cash.setStyleSheet("font-size: 14px; margin-bottom: 8px;")
        layout.addWidget(lbl_cash)

        available_tickers = self.dbm.get_unique_tickers()
        self.cmb_ticker = QComboBox()
        self.cmb_ticker.setEditable(True)
        self.cmb_ticker.addItems([""] + available_tickers)
        self.cmb_ticker.setPlaceholderText(tr("Select symbol to auto-load price..."))
        self.cmb_ticker.currentIndexChanged.connect(self.on_mode_changed)
        self.cmb_ticker.lineEdit().editingFinished.connect(self.on_mode_changed)
        form.addRow(tr("Ticker Symbol:"), self.cmb_ticker)

        self.cmb_stop_mode = QComboBox()
        self.cmb_stop_mode.addItems([tr("Manual Stop"), tr("1.5x ATR Stop"), tr("2.0x ATR Stop"), tr("3.0x ATR Stop")])
        self.cmb_stop_mode.currentTextChanged.connect(self.on_mode_changed)
        form.addRow(tr("Stop-Loss Mode:"), self.cmb_stop_mode)

        self.spn_risk_pct = QDoubleSpinBox()
        self.spn_risk_pct.setRange(0.1, 10.0)
        self.spn_risk_pct.setValue(1.0)
        self.spn_risk_pct.setSuffix(" %")
        self.spn_risk_pct.valueChanged.connect(self.calculate)
        form.addRow(tr("Max Account Risk:"), self.spn_risk_pct)

        self.spn_entry = QDoubleSpinBox()
        self.spn_entry.setRange(0.01, 100000.0)
        self.spn_entry.setValue(10.00)
        self.spn_entry.setDecimals(4)
        self.spn_entry.valueChanged.connect(self.calculate)
        form.addRow(tr("Target Entry Price (EGP):"), self.spn_entry)

        self.spn_stop = QDoubleSpinBox()
        self.spn_stop.setRange(0.01, 100000.0)
        self.spn_stop.setValue(9.20)
        self.spn_stop.setDecimals(4)
        self.spn_stop.valueChanged.connect(self.calculate)
        form.addRow(tr("Stop-Loss Price (EGP):"), self.spn_stop)

        layout.addLayout(form)
        self.lbl_result = QLabel()
        self.lbl_result.setStyleSheet("padding: 16px; border-radius: 8px; font-size: 14px; border: 1px solid #4a5568; background-color: #1a1d24;")
        layout.addWidget(self.lbl_result)
        self.calculate()

    def on_mode_changed(self):
        # Keyed off index, not the displayed text, so this keeps working
        # correctly regardless of which language's combo item text is shown.
        mode_index = self.cmb_stop_mode.currentIndex()
        atr_multipliers = {1: 1.5, 2: 2.0, 3: 3.0}
        if mode_index == 0:
            self.spn_stop.setReadOnly(False)
            self.calculate()
            return

        ticker = self.cmb_ticker.currentText().strip()
        if not ticker:
            return

        price, atr = self.qe.get_latest_price_and_atr(ticker)
        if price > 0:
            self.spn_entry.setValue(price)
            mult = atr_multipliers.get(mode_index, 1.5)
            stop_val = max(0.0001, price - (mult * atr))
            self.spn_stop.setValue(stop_val)
            self.spn_stop.setReadOnly(True)
        self.calculate()

    def calculate(self):
        entry = self.spn_entry.value()
        stop = self.spn_stop.value()
        risk_pct = self.spn_risk_pct.value() / 100.0

        if stop >= entry:
            self.lbl_result.setText(tr("⚠️ <b>Invalid Parameters:</b> Stop-Loss must be below Target Entry."))
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
            tr("🎯 <b>Recommended Shares:</b> {shares} shares<br><br>"
               "💵 <b>Total Outlay (incl. 0.35% fee):</b> {outlay} EGP ({pct}% of cash)<br><br>"
               "🛡️ <b>Max Capital at Risk:</b> {risk} EGP").format(
                shares=f"{shares:,}",
                outlay=f"{total_with_fees:,.2f}",
                pct=f"{pct_of_portfolio:.1f}",
                risk=f"{min(risk_budget, total_outlay):,.2f}",
            )
        )


class PortfolioDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Institutional Portfolio & Trade Manager"))
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
        self.cmb_buy_ticker.setPlaceholderText(tr("Type or select ticker (e.g. PHGC.CA)..."))

        completer_buy = self.cmb_buy_ticker.completer()
        if completer_buy:
            completer_buy.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer_buy.setFilterMode(Qt.MatchFlag.MatchContains)
        form_buy.addRow(tr("Ticker Symbol:"), self.cmb_buy_ticker)

        self.spn_buy_price = QDoubleSpinBox()
        self.spn_buy_price.setRange(0.0001, 100000.0)
        self.spn_buy_price.setDecimals(4)
        self.spn_buy_price.setValue(0.1351)
        form_buy.addRow(tr("Buy Price (EGP):"), self.spn_buy_price)

        self.spn_buy_shares = QDoubleSpinBox()
        self.spn_buy_shares.setRange(0.0001, 10000000.0)
        self.spn_buy_shares.setDecimals(4)
        self.spn_buy_shares.setValue(10000.0000)
        form_buy.addRow(tr("Number of Shares:"), self.spn_buy_shares)

        self.dt_buy_date = QDateEdit()
        self.dt_buy_date.setCalendarPopup(True)
        self.dt_buy_date.setDate(QDate.currentDate())
        form_buy.addRow(tr("Purchase Date:"), self.dt_buy_date)

        btn_scale = QPushButton(tr("📈 Add Shares / Scale In (Auto-Calculate Average)"))
        btn_scale.setStyleSheet("background-color: #3198dc; color: white; margin-top: 10px; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_scale.clicked.connect(lambda: self.save_buy_position(mode="ADD_SCALE"))
        form_buy.addRow(btn_scale)

        btn_layout = QHBoxLayout()
        btn_overwrite = QPushButton(tr("✏️ Correct Mistake"))
        btn_overwrite.setStyleSheet("background-color: #d69e2e; color: white; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_overwrite.clicked.connect(lambda: self.save_buy_position(mode="OVERWRITE"))

        btn_delete = QPushButton(tr("🗑️ Delete Position"))
        btn_delete.setStyleSheet("background-color: #e53e3e; color: white; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_delete.clicked.connect(self.delete_buy_position)

        btn_layout.addWidget(btn_overwrite)
        btn_layout.addWidget(btn_delete)
        form_buy.addRow(btn_layout)
        self.tabs.addTab(tab_buy, tr("🛒 Open / Add / Delete Position"))

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
        form_sell.addRow(tr("Ticker Symbol:"), self.cmb_sell_ticker)

        self.spn_sell_price = QDoubleSpinBox()
        self.spn_sell_price.setRange(0.0001, 100000.0)
        self.spn_sell_price.setDecimals(4)
        self.spn_sell_price.setValue(0.1500)
        form_sell.addRow(tr("Selling Price (EGP):"), self.spn_sell_price)

        self.spn_sell_shares = QDoubleSpinBox()
        self.spn_sell_shares.setRange(0.0001, 10000000.0)
        self.spn_sell_shares.setDecimals(4)
        self.spn_sell_shares.setValue(10000.0000)
        form_sell.addRow(tr("Shares Sold:"), self.spn_sell_shares)

        self.dt_sell_date = QDateEdit()
        self.dt_sell_date.setCalendarPopup(True)
        self.dt_sell_date.setDate(QDate.currentDate())
        form_sell.addRow(tr("Sell Date:"), self.dt_sell_date)

        btn_record_sale = QPushButton(tr("🤝 Record Sale & Calculate P&L"))
        btn_record_sale.setStyleSheet("background-color: #38a169; color: white; margin-top: 10px; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_record_sale.clicked.connect(self.record_stock_sale)
        form_sell.addRow(btn_record_sale)
        self.tabs.addTab(tab_sell, tr("🤝 Record Sale / Close Trade"))

        tab_target = QWidget()
        form_target = QFormLayout(tab_target)
        form_target.setSpacing(12)

        owned_tickers = list(self.dbm.get_all_owned_stocks().keys())
        self.cmb_target_ticker = QComboBox()
        self.cmb_target_ticker.setEditable(True)
        self.cmb_target_ticker.addItems([""] + owned_tickers)
        self.cmb_target_ticker.setPlaceholderText(tr("Select an owned ticker..."))
        completer_target = self.cmb_target_ticker.completer()
        if completer_target:
            completer_target.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            completer_target.setFilterMode(Qt.MatchFlag.MatchContains)
        self.cmb_target_ticker.currentIndexChanged.connect(self.on_target_ticker_changed)
        self.cmb_target_ticker.lineEdit().editingFinished.connect(self.on_target_ticker_changed)
        form_target.addRow(tr("Owned Ticker:"), self.cmb_target_ticker)

        self.lbl_target_current = QLabel("")
        self.lbl_target_current.setWordWrap(True)
        self.lbl_target_current.setStyleSheet("color: #a0aec0; font-size: 11px;")
        form_target.addRow(self.lbl_target_current)

        self.cmb_target_mode = QComboBox()
        self.cmb_target_mode.addItems([tr("% Gain from Buy Price"), tr("Money Amount (EGP)")])
        self.cmb_target_mode.currentIndexChanged.connect(self.on_target_mode_changed)
        form_target.addRow(tr("Target Mode:"), self.cmb_target_mode)

        self.spn_target_value = QDoubleSpinBox()
        self.spn_target_value.setDecimals(2)
        self.spn_target_value.setValue(15.0)
        self.spn_target_value.valueChanged.connect(self.update_target_preview)
        form_target.addRow(tr("Target Value:"), self.spn_target_value)

        self.lbl_target_preview = QLabel()
        self.lbl_target_preview.setWordWrap(True)
        self.lbl_target_preview.setStyleSheet("padding: 12px; border-radius: 8px; font-size: 13px; border: 1px solid #4a5568; background-color: #1a1d24;")
        form_target.addRow(self.lbl_target_preview)

        btn_save_target = QPushButton(tr("🎯 Save Profit Target"))
        btn_save_target.setStyleSheet("background-color: #3198dc; color: white; margin-top: 10px; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_save_target.clicked.connect(self.save_profit_target)

        btn_clear_target = QPushButton(tr("🗑️ Clear Target"))
        btn_clear_target.setStyleSheet("background-color: #e53e3e; color: white; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_clear_target.clicked.connect(self.clear_profit_target)

        target_btn_layout = QHBoxLayout()
        target_btn_layout.addWidget(btn_save_target)
        target_btn_layout.addWidget(btn_clear_target)
        form_target.addRow(target_btn_layout)

        self.on_target_mode_changed()
        self.tabs.addTab(tab_target, tr("🎯 Set Profit Target"))

        layout.addWidget(self.tabs)

        btn_clean = QPushButton(tr("🧹 Clear Sample Demo Data"))
        btn_clean.setStyleSheet("background-color: #4a5568; color: white; margin-top: 5px; padding: 10px 14px; font-size: 13px; border-radius: 6px;")
        btn_clean.clicked.connect(self.clean_samples)
        layout.addWidget(btn_clean)

    def save_buy_position(self, mode="ADD_SCALE"):
        ticker = self.cmb_buy_ticker.currentText().strip().upper()
        if not ticker:
            QMessageBox.warning(self, tr("Input Error"), tr("Please enter or select a valid Ticker Symbol."))
            return
        available_tickers = self.dbm.get_unique_tickers()
        if ticker not in available_tickers and (ticker + ".CA") in available_tickers:
            ticker = ticker + ".CA"
        price = self.spn_buy_price.value()
        shares = self.spn_buy_shares.value()
        p_date = self.dt_buy_date.date().toString("yyyy-MM-dd")
        success, msg = self.dbm.add_owned_stock(ticker, price, shares, p_date, mode=mode, is_demo=False)
        if success:
            QMessageBox.information(self, tr("Position Updated"), msg)
            self.accept()
        else:
            QMessageBox.warning(self, tr("Position Error"), msg)

    def delete_buy_position(self):
        ticker = self.cmb_buy_ticker.currentText().strip().upper()
        if not ticker:
            QMessageBox.warning(self, tr("Input Error"), tr("Please select the Ticker Symbol to delete."))
            return
        available_tickers = self.dbm.get_unique_tickers()
        if ticker not in available_tickers and (ticker + ".CA") in available_tickers:
            ticker = ticker + ".CA"
        self.dbm.remove_owned_stock(ticker)
        QMessageBox.information(self, tr("Deleted"), tr("Permanently removed {ticker} from your active portfolio.").format(ticker=ticker))
        self.accept()

    def record_stock_sale(self):
        ticker = self.cmb_sell_ticker.currentText().strip().upper()
        if not ticker:
            QMessageBox.warning(self, tr("Input Error"), tr("Please enter or select the Ticker Symbol."))
            return
        available_tickers = self.dbm.get_unique_tickers()
        if ticker not in available_tickers and (ticker + ".CA") in available_tickers:
            ticker = ticker + ".CA"
        sell_price = self.spn_sell_price.value()
        shares_sold = self.spn_sell_shares.value()
        s_date = self.dt_sell_date.date().toString("yyyy-MM-dd")
        success, msg = self.dbm.record_sale(ticker, sell_price, shares_sold, s_date)
        if success:
            QMessageBox.information(self, tr("Sale Recorded!"), msg)
            self.accept()
        else:
            QMessageBox.warning(self, tr("Sale Error"), msg)

    def on_target_ticker_changed(self):
        ticker = self.cmb_target_ticker.currentText().strip().upper()
        if not ticker:
            self.lbl_target_current.setText("")
            self.update_target_preview()
            return
        norm = self.dbm.normalize_symbol(ticker)
        owned = self.dbm.get_all_owned_stocks()
        pos = owned.get(norm)
        if not pos:
            self.lbl_target_current.setText(tr("⚠️ You don't currently own {t}.").format(t=norm))
            self.update_target_preview()
            return
        existing = self.dbm.get_position_target(norm)
        if existing:
            mode_txt = tr("% Gain") if existing["target_mode"] == "PCT" else tr("Money Amount (EGP)")
            self.lbl_target_current.setText(
                tr("Buy Price: {bp} EGP | {n} shares | Current target: {mv} {mode}").format(
                    bp=f"{pos['buy_price']:.4f}", n=f"{pos['shares']:,.4f}",
                    mv=f"{existing['target_value']:,.2f}", mode=mode_txt,
                )
            )
            self.cmb_target_mode.setCurrentIndex(0 if existing["target_mode"] == "PCT" else 1)
            self.spn_target_value.setValue(existing["target_value"])
        else:
            self.lbl_target_current.setText(
                tr("Buy Price: {bp} EGP | {n} shares | No target set yet.").format(
                    bp=f"{pos['buy_price']:.4f}", n=f"{pos['shares']:,.4f}"
                )
            )
        self.update_target_preview()

    def on_target_mode_changed(self):
        if self.cmb_target_mode.currentIndex() == 1:
            self.spn_target_value.setSuffix(" EGP")
            self.spn_target_value.setRange(0.01, 100000000.0)
        else:
            self.spn_target_value.setSuffix(" %")
            self.spn_target_value.setRange(0.01, 1000.0)
        self.update_target_preview()

    def update_target_preview(self):
        ticker = self.cmb_target_ticker.currentText().strip().upper()
        norm = self.dbm.normalize_symbol(ticker) if ticker else ""
        pos = self.dbm.get_all_owned_stocks().get(norm)
        if not pos:
            self.lbl_target_preview.setText(tr("Select an owned ticker to preview the target price."))
            return
        buy_price = pos["buy_price"]
        shares = pos["shares"]
        value = self.spn_target_value.value()
        if self.cmb_target_mode.currentIndex() == 1:  # Money amount
            target_profit_egp = value
            target_price = buy_price + (value / shares) if shares > 0 else buy_price
            target_pct = ((target_price - buy_price) / buy_price * 100) if buy_price > 0 else 0.0
        else:
            target_pct = value
            target_price = buy_price * (1 + value / 100.0)
            target_profit_egp = (target_price - buy_price) * shares
        self.lbl_target_preview.setText(
            tr("🎯 Sell at <b>{p} EGP</b> to hit this target<br>= {pct}% gain from your buy price<br>= {egp} EGP profit on this position").format(
                p=f"{target_price:,.4f}", pct=f"{target_pct:,.2f}", egp=f"{target_profit_egp:,.2f}",
            )
        )

    def save_profit_target(self):
        ticker = self.cmb_target_ticker.currentText().strip().upper()
        if not ticker:
            QMessageBox.warning(self, tr("Input Error"), tr("Please select an owned ticker."))
            return
        norm = self.dbm.normalize_symbol(ticker)
        if norm not in self.dbm.get_all_owned_stocks():
            QMessageBox.warning(self, tr("Input Error"), tr("You don't currently own {t}.").format(t=norm))
            return
        mode = "AMOUNT" if self.cmb_target_mode.currentIndex() == 1 else "PCT"
        value = self.spn_target_value.value()
        self.dbm.set_position_target(norm, mode, value)
        QMessageBox.information(
            self, tr("Target Saved"),
            tr("Profit target saved for {t}. It'll show up in the Exits tab next time you run the matrix.").format(t=norm),
        )
        self.accept()

    def clear_profit_target(self):
        ticker = self.cmb_target_ticker.currentText().strip().upper()
        if not ticker:
            QMessageBox.warning(self, tr("Input Error"), tr("Please select a ticker."))
            return
        norm = self.dbm.normalize_symbol(ticker)
        self.dbm.remove_position_target(norm)
        QMessageBox.information(self, tr("Target Cleared"), tr("Removed the profit target for {t}.").format(t=norm))
        self.accept()

    def clean_samples(self):
        self.dbm.clear_sample_data()
        QMessageBox.information(self, tr("Samples Deleted"), tr("Successfully removed demo samples without deleting real positions!"))
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
    results_signal = pyqtSignal(list, list, dict, list, dict, list, list, dict, dict)

    def run(self):
        matrix = DecisionMatrix()
        buys, exits, top10, closed, fin_stmt, sectors, breakout_watchlist, portfolio_risk, session_picks = matrix.analyze_market(
            progress_callback=lambda pct, msg: self.progress_signal.emit(pct, msg)
        )
        self.results_signal.emit(buys, exits, top10, closed, fin_stmt, sectors, breakout_watchlist, portfolio_risk, session_picks)


# =============================================================================
# N6 (desktop) — Strategy Calculator
#
# The web dashboard's "🧮 Strategy Calculator" tab (index.html) reads a
# small public JSON shard, web_public/data/strategy_performance.json,
# written by the standalone export_backtest_summary.py script (see that
# file's own docstring for why it's deliberately NOT part of the nightly
# publish.py pipeline — a multi-year walk-forward run is too slow to
# block every publish). The desktop app has direct DuckDB access and its
# own "Execute Matrix" background-thread pattern already (AnalysisWorker
# above), so instead of just reading that JSON file (which may be stale
# or missing on a machine that's never run publish.py), this dialog can
# also trigger backtester.run_walk_forward_backtest() itself, off the UI
# thread, and writes the SAME strategy_performance.json shape via
# export_backtest_summary.build_summary() — so a desktop-triggered run
# updates the exact file the web dashboard reads next time you publish,
# same "one code path, no drift" rule the rest of this app already
# follows for session_picks (see session_picks.py's own docstring).
# =============================================================================
class BacktestWorker(QThread):
    progress_signal = pyqtSignal(int, str)
    finished_result = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def run(self):
        try:
            from backtester import run_walk_forward_backtest
            result = run_walk_forward_backtest(
                progress_callback=lambda pct, msg: self.progress_signal.emit(pct, msg)
            )
            self.finished_result.emit(result)
        except Exception as e:
            self.failed.emit(str(e))


class StrategyCalculatorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("🧮 Strategy Calculator"))
        self.resize(680, 560)
        self._worker = None
        self._last_summary = None

        layout = QVBoxLayout(self)
        self.lbl_info = QLabel(tr(
            "Walk-forward backtest of every qualifying BUY signal this app's Action "
            "Matrix has ever produced — same rules, same thresholds. Describes the "
            "STRATEGY's historical behavior, not any account's real results."
        ))
        self.lbl_info.setWordWrap(True)
        layout.addWidget(self.lbl_info)

        self.lbl_reliability = QLabel("")
        self.lbl_reliability.setWordWrap(True)
        self.lbl_reliability.setStyleSheet("color: #d69e2e;")
        self.lbl_reliability.hide()
        layout.addWidget(self.lbl_reliability)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        layout.addWidget(self.progress)

        stats_row = QHBoxLayout()
        self.lbl_trades = QLabel(tr("Trades: —"))
        self.lbl_winrate = QLabel(tr("Win Rate: —"))
        self.lbl_avgret = QLabel(tr("Avg Return: —"))
        self.lbl_pf = QLabel(tr("Profit Factor: —"))
        for lbl in (self.lbl_trades, self.lbl_winrate, self.lbl_avgret, self.lbl_pf):
            lbl.setStyleSheet("font-weight: bold;")
            stats_row.addWidget(lbl)
        layout.addLayout(stats_row)

        stats_row2 = QHBoxLayout()
        self.lbl_sharpe = QLabel(tr("Sharpe: —"))
        self.lbl_sortino = QLabel(tr("Sortino: —"))
        self.lbl_dd = QLabel(tr("Max Drawdown: —"))
        for lbl in (self.lbl_sharpe, self.lbl_sortino, self.lbl_dd):
            stats_row2.addWidget(lbl)
        layout.addLayout(stats_row2)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels([tr("Signal"), tr("Trades"), tr("Win Rate"), tr("Avg Return")])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, stretch=1)

        calc_row = QHBoxLayout()
        calc_row.addWidget(QLabel(tr("Hypothetical amount (EGP):")))
        self.input_amount = QLineEdit("10000")
        self.input_amount.textChanged.connect(self._update_calc_result)
        calc_row.addWidget(self.input_amount)
        layout.addLayout(calc_row)
        self.lbl_calc_result = QLabel("")
        self.lbl_calc_result.setWordWrap(True)
        self.lbl_calc_result.setStyleSheet("color: #48bb78; font-weight: bold;")
        layout.addWidget(self.lbl_calc_result)
        self.lbl_calc_note = QLabel(tr(
            "Illustrative only — trades from different tickers can overlap in real "
            "time, so a real account could hold more than one position at once. "
            "This treats every trade as if taken one after another in a single "
            "account. Not investment advice."
        ))
        self.lbl_calc_note.setWordWrap(True)
        self.lbl_calc_note.setStyleSheet("color: gray; font-size: 10px;")
        layout.addWidget(self.lbl_calc_note)

        btn_row = QHBoxLayout()
        self.btn_load_cached = QPushButton(tr("📂 Load Last Published Result"))
        self.btn_load_cached.clicked.connect(self._load_cached_summary)
        btn_row.addWidget(self.btn_load_cached)
        self.btn_run = QPushButton(tr("▶ Run Backtest Now"))
        self.btn_run.clicked.connect(self._run_backtest)
        btn_row.addWidget(self.btn_run)
        btn_row.addStretch()
        btn_close = QPushButton(tr("Close"))
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        self._load_cached_summary(silent=True)

    def _summary_file_path(self):
        return str((Path(__file__).parent / "web_public" / "data" / "strategy_performance.json").resolve())

    def _load_cached_summary(self, silent: bool = False):
        path = self._summary_file_path()
        if not os.path.exists(path):
            if not silent:
                QMessageBox.information(
                    self, tr("No Cached Result"),
                    tr("No strategy_performance.json found yet at:\n{path}\n\nClick "
                       "\"Run Backtest Now\" to generate one.").format(path=path),
                )
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                summary = json.load(f)
            self._apply_summary(summary)
        except (OSError, json.JSONDecodeError) as e:
            if not silent:
                QMessageBox.warning(self, tr("Load Failed"), str(e))

    def _run_backtest(self):
        self.btn_run.setEnabled(False)
        self.btn_load_cached.setEnabled(False)
        self.progress.setValue(0)
        self.progress.show()
        self._worker = BacktestWorker()
        self._worker.progress_signal.connect(lambda pct, msg: (self.progress.setValue(pct), self.progress.setFormat(f"{msg} (%p%)")))
        self._worker.finished_result.connect(self._on_backtest_finished)
        self._worker.failed.connect(self._on_backtest_failed)
        self._worker.start()

    def _on_backtest_finished(self, result: dict):
        self.progress.hide()
        self.btn_run.setEnabled(True)
        self.btn_load_cached.setEnabled(True)
        try:
            from export_backtest_summary import build_summary
            summary = build_summary(result, equity_points=200)
            # Persist it so this desktop run also updates the file the
            # web dashboard reads next time publish.py runs — same
            # shape/location export_backtest_summary.py itself writes.
            out_path = Path(self._summary_file_path())
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
            self._apply_summary(summary)
            QMessageBox.information(
                self, tr("Backtest Complete"),
                tr("Saved to {path}. This will be picked up next time you run "
                   "publish.py.").format(path=out_path),
            )
        except Exception as e:
            QMessageBox.warning(self, tr("Backtest Finished, Save Failed"), str(e))

    def _on_backtest_failed(self, error_text: str):
        self.progress.hide()
        self.btn_run.setEnabled(True)
        self.btn_load_cached.setEnabled(True)
        QMessageBox.warning(self, tr("Backtest Failed"), error_text)

    def _apply_summary(self, summary: dict):
        self._last_summary = summary
        if not summary.get("is_reliable", True) and summary.get("reliability_note"):
            self.lbl_reliability.setText("⚠️ " + summary["reliability_note"])
            self.lbl_reliability.show()
        else:
            self.lbl_reliability.hide()

        self.lbl_trades.setText(tr("Trades: {n}").format(n=summary.get("trade_count", 0)))
        self.lbl_winrate.setText(tr("Win Rate: {pct}%").format(pct=summary.get("win_rate_pct", 0)))
        avg_ret = summary.get("avg_return_pct", 0)
        self.lbl_avgret.setText(tr("Avg Return: {sign}{pct}%").format(sign="+" if avg_ret >= 0 else "", pct=avg_ret))
        pf = summary.get("profit_factor")
        self.lbl_pf.setText(tr("Profit Factor: {pf}").format(pf="∞" if pf is None else pf))
        overall = summary.get("overall", {})
        self.lbl_sharpe.setText(tr("Sharpe: {v}").format(v=overall.get("sharpe", "—")))
        self.lbl_sortino.setText(tr("Sortino: {v}").format(v=overall.get("sortino", "—")))
        dd = overall.get("max_drawdown")
        self.lbl_dd.setText(tr("Max Drawdown: {v}").format(v=f"{dd * 100:.2f}%" if dd is not None else "—"))

        by_action = summary.get("by_action", [])
        self.table.setRowCount(len(by_action))
        for i, row in enumerate(by_action):
            vals = [row.get("action", ""), str(row.get("trade_count", 0)), f"{row.get('win_rate_pct', 0)}%", f"{row.get('avg_return_pct', 0):+.2f}%"]
            for j, val in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(str(val)))
        self.table.resizeColumnsToContents()

        self._update_calc_result()

    def _update_calc_result(self):
        if not self._last_summary:
            self.lbl_calc_result.setText("")
            return
        try:
            amount = float(self.input_amount.text())
        except ValueError:
            self.lbl_calc_result.setText("")
            return
        curve = self._last_summary.get("equity_curve", [])
        if not curve or amount <= 0:
            self.lbl_calc_result.setText("")
            return
        start = curve[0].get("equity", 100) or 100
        end = curve[-1].get("equity", start) or start
        final_value = amount * (end / start)
        pct = (end / start - 1) * 100
        self.lbl_calc_result.setStyleSheet(f"color: {'#48bb78' if final_value >= amount else '#f56565'}; font-weight: bold;")
        self.lbl_calc_result.setText(
            tr("{amount} EGP → {final} EGP ({sign}{pct}%) across {n} sequential trades").format(
                amount=f"{amount:,.0f}", final=f"{final_value:,.0f}",
                sign="+" if pct >= 0 else "", pct=f"{pct:.1f}", n=self._last_summary.get("trade_count", 0),
            )
        )


# =============================================================================
# W1 — VISIBLE DB-LOCK RETRY (startup connect dialog)
#
# DatabaseManager() previously either connected instantly or, if
# quant_master.duckdb was already held open by another running instance of
# this same app (a known real scenario - see launch_and_publish.bat's own
# "already running" check), retried completely silently for up to ~64s
# (0.5 * 2**0..6 across 8 attempts) before either succeeding or raising
# DatabaseLockedError. From the user's side that silent window looked
# exactly like a frozen/hung app, with no way to tell "still retrying"
# apart from "crashed" - and a plain unhandled DatabaseLockedError would
# have shown up as a generic fatal-error traceback box instead of the
# actionable "close the other window" message it actually deserves.
#
# DBConnectWorker runs the (retrying) DatabaseManager() connection off the
# UI thread; DBConnectDialog shows live retry progress via the on_retry
# hook threaded through db_manager in this same change, and turns a final
# DatabaseLockedError into a friendly, specific dialog instead of falling
# through to the generic fatal-error handler in __main__ below.
# =============================================================================
class DBConnectWorker(QThread):
    retrying = pyqtSignal(int, int, float, str)   # attempt, retries, delay_seconds, error_text
    connected = pyqtSignal()
    failed = pyqtSignal(str)                      # friendly error message

    def _on_retry(self, attempt, retries, delay, error):
        # Called from db_manager on the worker thread; re-emit as a Qt
        # signal so the dialog only ever touches its widgets on the UI
        # thread via the normal signal/slot queued-connection mechanism.
        self.retrying.emit(attempt, retries, delay, str(error))

    def run(self):
        try:
            DatabaseManager(on_retry=self._on_retry)
            self.connected.emit()
        except DatabaseLockedError as e:
            self.failed.emit(str(e))
        except Exception as e:
            # Anything else (corrupt file, missing directory, ...) isn't a
            # lock issue - surface it plainly rather than pretending it's
            # the same "close the other app" situation.
            logger.error(f"Unexpected error connecting to database:\n{traceback.format_exc()}")
            self.failed.emit(f"Unexpected database error: {e}")


class DBConnectDialog(QDialog):
    """Shown while DBConnectWorker connects. Starts out looking like a
    plain, quick splash ('Connecting to database...') and only grows
    visible retry detail once a lock is actually encountered, so the
    common case (instant connect) never flashes retry UI at all."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("MB-EGX — Starting"))
        self.setModal(True)
        self.setFixedSize(420, 150)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        self.result_ok = False
        self.error_message = None

        layout = QVBoxLayout(self)
        self.status_label = QLabel(tr("Connecting to database..."))
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # indeterminate until a retry tells us otherwise
        layout.addWidget(self.progress)

        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        layout.addWidget(self.detail_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.cancel_btn = QPushButton(tr("Cancel"))
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.cancel_btn)
        layout.addLayout(btn_row)

        self.worker = DBConnectWorker()
        self.worker.retrying.connect(self._on_retrying)
        self.worker.connected.connect(self._on_connected)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_retrying(self, attempt, retries, delay, error_text):
        self.progress.setRange(0, retries)
        self.progress.setValue(attempt)
        self.status_label.setText(
            tr("Database is busy — retrying ({0}/{1})...").format(attempt, retries)
        )
        self.detail_label.setText(
            tr("Another program (usually MB-EGX itself) has the database open. "
               "Waiting {0:.1f}s before the next attempt...").format(delay)
        )

    def _on_connected(self):
        self.result_ok = True
        self.accept()

    def _on_failed(self, message):
        self.result_ok = False
        self.error_message = message
        self.reject()

    def _on_cancel(self):
        # The worker thread is mid-connect (or mid-sleep) and can't be
        # force-killed safely mid-DuckDB-call, so we just stop waiting for
        # it here and let __main__ exit; any in-flight attempt finishes
        # and is discarded harmlessly since nothing reads its signals once
        # this dialog is gone.
        self.result_ok = False
        self.error_message = None
        self.reject()


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

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Set as full standalone window
        self.setWindowFlags(Qt.WindowType.Window)
        self.setWindowTitle(tr("MB-EGX Alpha — Terminal Access"))
        
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setObjectName("loginDialog")
        
        self.setStyleSheet(f"""
            QDialog#loginDialog {{ background-color: {self._BG}; }}
            
            /* Target the consent box specifically as a QFrame to ensure rendering */
            QFrame#consentBox {{ 
                background-color: #1a1c20; 
                border: 1px solid {self._OUTLINE}; 
                border-radius: 6px; 
            }}
            
            /* Fix for QMessageBox popups inheriting white text on default white bg */
            QMessageBox {{
                background-color: {self._CARD};
            }}
            QMessageBox QLabel {{
                color: #ffffff;
            }}
            QMessageBox QPushButton {{
                background-color: {self._PRIMARY};
                color: {self._ON_PRIMARY};
                padding: 6px 16px;
                border-radius: 4px;
                font-weight: bold;
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

        # 0. TOP BAR (language selector) — kept outside the vertically-centered
        # content below so it stays anchored to a fixed, predictable corner of
        # the window instead of drifting into the middle of the screen.
        top_bar = QWidget()
        top_bar_layout = QHBoxLayout(top_bar)
        top_bar_layout.setContentsMargins(40, 16, 40, 0)
        top_bar_layout.addStretch()
        self.cmb_lang = QComboBox()
        self.cmb_lang.addItems(["EN", "AR"])
        self.cmb_lang.setCurrentText(CURRENT_LANG)
        self.cmb_lang.setFixedWidth(70)
        self.cmb_lang.currentTextChanged.connect(self._on_language_changed)
        top_bar_layout.addWidget(self.cmb_lang)
        outer.addWidget(top_bar)

        # 1. MAIN 2-COLUMN CONTENT
        content_area = QWidget()
        content_layout = QHBoxLayout(content_area)
        # Squeeze margins slightly to allow plenty of vertical room for right-side card
        content_layout.setContentsMargins(40, 30, 40, 30)
        content_layout.setSpacing(40)

        # --- LEFT COLUMN (Branding & Vision/Mission) ---
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
        self.lbl_hero = QLabel(self._hero_html())
        self.lbl_hero.setStyleSheet("font-family: 'Hanken Grotesk', sans-serif; font-size: 42px; font-weight: bold; line-height: 1.2;")
        left_layout.addWidget(self.lbl_hero)

        # Vision & Mission text replacing the old description with metallic gold font styling
        self.lbl_vm = QLabel(self._vm_html())
        self.lbl_vm.setStyleSheet("color: #bfc7d2; font-size: 15px; line-height: 1.6;")
        self.lbl_vm.setWordWrap(True)
        self.lbl_vm.setMaximumWidth(520)
        left_layout.addWidget(self.lbl_vm)

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
        f_layout.setContentsMargins(30, 30, 30, 30)
        f_layout.setSpacing(15)

        # Terminal Access Heading
        self.lbl_form_title = QLabel(tr("Terminal Access"))
        self.lbl_form_title.setStyleSheet("font-size: 26px; font-weight: bold; color: #ffffff; border: none;")
        f_layout.addWidget(self.lbl_form_title)

        self.lbl_form_sub = QLabel(tr("Sign in to view your private dashboard"))
        self.lbl_form_sub.setStyleSheet("color: #bfc7d2; font-size: 13px; margin-bottom: 5px; border: none;")
        f_layout.addWidget(self.lbl_form_sub)

        # Email
        email_container = QWidget()
        email_layout = QVBoxLayout(email_container)
        email_layout.setContentsMargins(0,0,0,0)
        email_layout.setSpacing(6)
        lbl_email_hdr = QLabel(tr("EMAIL ADDRESS"))
        lbl_email_hdr.setStyleSheet("color: #89929b; font-size: 11px; font-weight: bold; letter-spacing: 1px; border: none;")
        email_layout.addWidget(lbl_email_hdr)
        self.lbl_email_hdr = lbl_email_hdr
        
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
        lbl_pw_hdr = QLabel(tr("PASSWORD"))
        lbl_pw_hdr.setStyleSheet("color: #89929b; font-size: 11px; font-weight: bold; letter-spacing: 1px; border: none;")
        pw_header_row.addWidget(lbl_pw_hdr)
        self.lbl_pw_hdr = lbl_pw_hdr
        pw_header_row.addStretch()
        
        self.btn_forgot = QPushButton(tr("Forgot Access?"))
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
        self.txt_disclaimer.setPlainText(DISCLAIMER_TEXT_AR if CURRENT_LANG == "AR" else DISCLAIMER_TEXT)
        self.txt_disclaimer.setFixedHeight(65) # Shorter height leaves plenty of room for checkbox
        consent_layout.addWidget(self.txt_disclaimer)

        self.chk_consent = QCheckBox(tr("I acknowledge and agree to the Terms."))
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
        self.btn_signin = QPushButton(tr("Sign In  →"))
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
        self.lbl_or = QLabel(tr("OR"))
        self.lbl_or.setStyleSheet("color: #89929b; font-size: 11px; font-weight: bold; background: transparent; border: none; padding: 0 10px;")
        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setStyleSheet("border-top: 1px solid #3f4850; background: transparent;")
        
        divider_layout.addWidget(line1, stretch=1)
        divider_layout.addWidget(self.lbl_or)
        divider_layout.addWidget(line2, stretch=1)
        f_layout.addLayout(divider_layout)

        # Google Button
        self.btn_google = QPushButton(tr("Sign in with Google"))
        self.btn_google.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_google.setStyleSheet(
            "background-color: #282a2e; color: #e2e2e8; padding: 12px; font-size: 14px; font-weight: bold; border-radius: 8px; border: 1px solid #4a5568;"
        )
        self.btn_google.clicked.connect(lambda: QMessageBox.information(self, tr("Google Sign In"), tr("In the desktop client, please use your Email/Password. If you created your account with Google on the web, click 'Forgot Access?' to set a password for desktop use.")))
        f_layout.addWidget(self.btn_google)

        # Create Account Link
        create_layout = QHBoxLayout()
        self.lbl_no_account = QLabel(tr("Don't have an account?"))
        self.lbl_no_account.setStyleSheet("color: #89929b; font-size: 13px; border: none; background: transparent;")
        self.btn_signup = QPushButton(tr("Create Account"))
        self.btn_signup.setFlat(True)
        self.btn_signup.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_signup.setStyleSheet(f"color: {self._PRIMARY}; font-size: 13px; font-weight: bold; border: none; background: transparent;")
        self.btn_signup.clicked.connect(self.do_sign_up)
        
        create_layout.addStretch()
        create_layout.addWidget(self.lbl_no_account)
        create_layout.addWidget(self.btn_signup)
        create_layout.addStretch()
        f_layout.addLayout(create_layout)

        right_layout.addWidget(form_card)
        content_layout.addWidget(right_col, stretch=1)
        outer.addWidget(content_area, stretch=1)

        # 2. FOOTER
        footer = QWidget()
        footer.setFixedHeight(40)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0,0,0,15)
        self.lbl_footer = QLabel(tr("🔒 AES-256 BANK GRADE ENCRYPTION MATRIX ENABLED"))
        self.lbl_footer.setStyleSheet("color: #89929b; font-size: 10px; font-weight: bold; letter-spacing: 1px;")
        self.lbl_footer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        footer_layout.addWidget(self.lbl_footer)
        outer.addWidget(footer)

    def _hero_html(self):
        if CURRENT_LANG == "AR":
            return f"<span style='color: {self._PRIMARY};'>إرث عريق</span> يلتقي<br><span style='color: #ffffff;'>بمستقبل رقمي</span>"
        return f"<span style='color: {self._PRIMARY};'>Ancient Legacy</span> meets<br><span style='color: #ffffff;'>Digital Future</span>"

    def _vm_html(self):
        if CURRENT_LANG == "AR":
            return (
                "<span style='color: #D4AF37; font-weight: 800; font-family: \"Hanken Grotesk\", sans-serif; font-size: 17px;'>الرؤية:</span> سوق أسهم شفاف يمتلك فيه كل مستثمر الرؤى اللازمة للنجاح.<br><br>"
                "<span style='color: #D4AF37; font-weight: 800; font-family: \"Hanken Grotesk\", sans-serif; font-size: 17px;'>المهمة:</span> نبني منصات تحليلية متكاملة تفكّ رموز بيانات البورصة المصرية، وتزيل التشويش، وتمكّنك من التداول بذكاء أكبر."
            )
        return (
            "<span style='color: #D4AF37; font-weight: 800; font-family: \"Hanken Grotesk\", sans-serif; font-size: 17px;'>Vision:</span> A transparent stock market where every investor has the insights to succeed.<br><br>"
            "<span style='color: #D4AF37; font-weight: 800; font-family: \"Hanken Grotesk\", sans-serif; font-size: 17px;'>Mission:</span> We build seamless analytical platforms that decode EGX data, cut through the noise, and empower you to trade smarter."
        )

    def _on_language_changed(self, lang):
        set_language(lang)
        self.retranslate_ui()

    def retranslate_ui(self):
        """Re-applies all visible text after a language switch — no restart
        needed. Kept as one explicit pass over the widgets stored as self.xxx
        during _init_ui, mirroring the same live-toggle behavior as the web
        dashboard."""
        self.setWindowTitle(tr("MB-EGX Alpha — Terminal Access"))
        self.lbl_hero.setText(self._hero_html())
        self.lbl_vm.setText(self._vm_html())
        self.lbl_form_title.setText(tr("Terminal Access"))
        self.lbl_form_sub.setText(tr("Sign in to view your private dashboard"))
        self.lbl_email_hdr.setText(tr("EMAIL ADDRESS"))
        self.lbl_pw_hdr.setText(tr("PASSWORD"))
        self.btn_forgot.setText(tr("Forgot Access?"))
        self.txt_disclaimer.setPlainText(DISCLAIMER_TEXT_AR if CURRENT_LANG == "AR" else DISCLAIMER_TEXT)
        self.chk_consent.setText(tr("I acknowledge and agree to the Terms."))
        self.btn_signin.setText(tr("Sign In  →"))
        self.lbl_or.setText(tr("OR"))
        self.btn_google.setText(tr("Sign in with Google"))
        self.lbl_no_account.setText(tr("Don't have an account?"))
        self.btn_signup.setText(tr("Create Account"))
        self.lbl_footer.setText(tr("🔒 AES-256 BANK GRADE ENCRYPTION MATRIX ENABLED"))
        # Layout direction follows the language so Arabic reads right-to-left.
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft if CURRENT_LANG == "AR" else Qt.LayoutDirection.LeftToRight)

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
            self._show_error(tr("Enter both email and password."))
            return
        if min_password_len and len(password) < min_password_len:
            self._show_error(tr("Password must be at least {n} characters.").format(n=min_password_len))
            return
        if require_consent and not self.chk_consent.isChecked():
            self._show_error(tr("You must explicitly agree to the legal terms to create an account."))
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
            # Log the real error (Firebase/Google's raw message, e.g. an API
            # key restriction rejection or a specific auth/* error code) for
            # diagnosis, but never surface that raw text to the user - it can
            # expose backend config details or just be confusing/unhelpful.
            logger.error(f"Auth attempt failed ({fn.__name__}): {e}")
            self._show_error(tr("Sign-in failed. Please check your credentials and try again."))
        finally:
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)

    def do_sign_in(self):
        self._attempt(firebase_sign_in)

    def do_sign_up(self):
        self._attempt(firebase_sign_up, min_password_len=8, require_consent=True)

    def do_forgot_password(self):
        email = self.txt_email.text().strip()
        if not email:
            self._show_error(tr("Type your email above first, then click this link."))
            return

        self._show_error("")
        self.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            firebase_send_password_reset(email)
            QMessageBox.information(
                self, tr("Check Your Email"),
                tr(
                    "If an account exists for {email}, a password-set/reset link has just been sent.\n\n"
                    "This also works if you originally signed up with 'Sign in with Google' on the "
                    "website — that account has no password yet, and this link lets you set one so "
                    "you can sign in here on desktop too."
                ).format(email=email)
            )
        except Exception as e:
            logger.error(f"Password reset request failed: {e}")
            self._show_error(tr("Couldn't send the reset email. Double-check the address and try again."))
        finally:
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)


_BIAS_COLOR = {"bullish": "#0d9488", "bearish": "#dc2626", "neutral": "#a16207", None: "#475569"}
_BIAS_LABEL = {
    "bullish": {"en": "BULLISH", "ar": "صاعد"},
    "bearish": {"en": "BEARISH", "ar": "هابط"},
    "neutral": {"en": "NEUTRAL", "ar": "محايد"},
    None: {"en": "MARKER", "ar": "علامة"},
}

# Per-theme text palette for the Glossary's QTextBrowser content. Qt's rich-text
# engine does NOT inherit colors from the dialog's QSS stylesheet for HTML set via
# setHtml() — the document has its own white canvas by default — so every theme
# needs its own explicit (background, text, muted-text, accent, border) tuple here,
# keyed by the exact THEMES_MAP name, to actually match the app's active theme.
GLOSSARY_PALETTES = {
    "🌙 Institutional Dark": {"bg": "#0f1115", "panel": "#1a1d24", "text": "#e2e2e8", "muted": "#a0aec0", "accent": "#93ccff", "border": "#2d3748"},
    "☀️ Professional Light": {"bg": "#ffffff", "panel": "#f8fafc", "text": "#1a202c", "muted": "#4a5568", "accent": "#2b6cb0", "border": "#cbd5e0"},
    "🌊 Midnight Blue": {"bg": "#0f172a", "panel": "#1e293b", "text": "#f8fafc", "muted": "#94a3b8", "accent": "#38bdf8", "border": "#334155"},
    "🌸 Soft Blush Rose (Pastel & Cream)": {"bg": "#ffffff", "panel": "#fef6fb", "text": "#500724", "muted": "#831843", "accent": "#be185d", "border": "#fbcfe8"},
    "✨ Velvet Rose Gold (Warm Elegance)": {"bg": "#20131a", "panel": "#311825", "text": "#ffe4e6", "muted": "#f472b6", "accent": "#fb7185", "border": "#3f2231"},
}
_DEFAULT_PALETTE = GLOSSARY_PALETTES["🌙 Institutional Dark"]


def _glossary_entry_html(entry, lang, palette):
    # Content dict keys are lowercase ("en"/"ar"); CURRENT_LANG/self.lang
    # elsewhere in this file is uppercase ("EN"/"AR") — normalize here.
    key = "ar" if str(lang).upper() == "AR" else "en"
    term = entry["term"].get(key, entry["term"]["en"])
    definition = entry["definition"].get(key, entry["definition"]["en"])
    why = entry["why_it_matters"].get(key, entry["why_it_matters"]["en"])
    bias = entry.get("bias", "__none__")
    why_label = "Why it matters" if key != "ar" else "لماذا يهم"

    # Badges are rendered as their own small table cell, not an inline <span>
    # with padding/border-radius — Qt's rich-text engine (QTextDocument) only
    # reliably supports padding/background-color on table cells, not inline
    # spans, so the span version visually overlapped the term text next to it.
    badge_cell = ""
    if bias != "__none__":
        color = _BIAS_COLOR.get(bias, "#475569")
        label = _BIAS_LABEL.get(bias, _BIAS_LABEL[None]).get(key, "")
        badge_cell = (
            f'<td align="right" valign="middle" width="92">'
            f'<table cellspacing="0" align="right"><tr>'
            f'<td style="background-color:{color}; padding:3px 8px;">'
            f'<font color="#ffffff" size="2"><b>{label}</b></font>'
            f'</td></tr></table></td>'
        )

    return f"""
    <table width="100%" cellspacing="0" cellpadding="0" style="margin-top:14px;">
      <tr>
        <td valign="middle"><font color="{palette['text']}" size="4"><b>{term}</b></font></td>
        {badge_cell}
      </tr>
    </table>
    <p style="margin:4px 0 2px 0;"><font color="{palette['muted']}" size="3">{definition}</font></p>
    <p style="margin:2px 0 8px 0;"><font color="{palette['accent']}" size="2"><b>{why_label}:</b> {why}</font></p>
    <hr style="border:none; border-top:1px solid {palette['border']};">
    """


def _glossary_page_html(items, lang, palette, search=""):
    search = (search or "").strip().lower()
    blocks = []
    for entry in items:
        if search:
            haystack = " ".join([
                entry["term"].get("en", ""), entry["term"].get("ar", ""),
                entry["definition"].get("en", ""), entry["definition"].get("ar", ""),
            ]).lower()
            if search not in haystack:
                continue
        blocks.append(_glossary_entry_html(entry, lang, palette))
    body = "".join(blocks)
    if not blocks:
        no_results = "No matching terms." if str(lang).upper() != "AR" else "لا توجد نتائج مطابقة."
        body = f'<p align="center"><font color="{palette["muted"]}">{no_results}</font></p>'
    direction = 'dir="rtl"' if str(lang).upper() == "AR" else 'dir="ltr"'
    # Explicit <body> background matches the active theme's palette, since
    # QTextBrowser's document canvas otherwise defaults to white regardless
    # of the dialog's own stylesheet.
    return (
        f'<html {direction}><body style="background-color:{palette["bg"]}; '
        f'margin:0; padding:4px 6px;">{body}</body></html>'
    )


class GlossaryDialog(QDialog):
    """Reference guide covering every indicator, action label/marker, and
    geometric chart pattern the engine can display — same content set
    (glossary_content.py) the web dashboard's Glossary modal uses, so a
    user gets an identical explanation regardless of which surface they're
    reading it on. Matches whichever of the 5 app themes is currently active."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("📖 Glossary & Chart Patterns"))
        self.resize(760, 640)
        self.theme_name = getattr(parent, "current_theme", "🌙 Institutional Dark")
        self.setStyleSheet(THEMES_MAP.get(self.theme_name, THEME_DARK))
        self.palette_colors = GLOSSARY_PALETTES.get(self.theme_name, _DEFAULT_PALETTE)
        self.lang = CURRENT_LANG
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        p = self.palette_colors

        lbl_info = QLabel(tr(
            "Every indicator, action label, and chart pattern the app shows you \u2014 explained in plain language."
        ))
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet(f"color: {p['muted']}; font-size: 13px; margin-bottom: 4px;")
        layout.addWidget(lbl_info)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(tr("🔎 Search terms…"))
        self.search_box.textChanged.connect(self._refresh)
        layout.addWidget(self.search_box)

        browser_style = (
            f"QTextBrowser {{ background-color: {p['bg']}; color: {p['text']}; "
            f"border: 1px solid {p['border']}; border-radius: 8px; padding: 4px; }}"
        )

        self.tabs = QTabWidget()
        self.browser_terms = QTextBrowser()
        self.browser_actions = QTextBrowser()
        self.browser_patterns = QTextBrowser()
        for b in (self.browser_terms, self.browser_actions, self.browser_patterns):
            b.setOpenExternalLinks(False)
            b.setStyleSheet(browser_style)
        self.tabs.addTab(self.browser_terms, tr("📊 Indicators & Terms"))
        self.tabs.addTab(self.browser_actions, tr("🏷️ Action Labels"))
        self.tabs.addTab(self.browser_patterns, tr("📐 Chart Patterns"))
        layout.addWidget(self.tabs, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton(tr("Close"))
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        self._refresh()

    def _refresh(self):
        q = self.search_box.text()
        p = self.palette_colors
        self.browser_terms.setHtml(_glossary_page_html(GLOSSARY_TERMS, self.lang, p, q))
        self.browser_actions.setHtml(_glossary_page_html(GLOSSARY_ACTIONS, self.lang, p, q))
        self.browser_patterns.setHtml(_glossary_page_html(GLOSSARY_PATTERNS, self.lang, p, q))


class AnalyticsDialog(QDialog):
    def __init__(self, id_token, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("📊 Usage Analytics"))
        self.resize(920, 520)
        self.setStyleSheet(THEME_DARK)
        self.id_token = id_token
        self._worker = None
        self._init_ui()
        self._load()

    def _init_ui(self):
        layout = QVBoxLayout(self)

        lbl_info = QLabel(tr(
            "Sessions and trading activity combined across the website (🌐) and the desktop app (🖥️). "
            "Time is approximate (30s heartbeat). Trade Value = total EGP bought + sold; "
            "Portfolio Value = cash + open positions at cost."
        ))
        lbl_info.setWordWrap(True)
        lbl_info.setStyleSheet("color: #a0aec0; font-size: 13px;")
        layout.addWidget(lbl_info)

        self.lbl_status = QLabel(tr("Loading session data…"))
        self.lbl_status.setStyleSheet("font-weight: bold; font-size: 14px; padding: 6px 0;")
        layout.addWidget(self.lbl_status)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            [tr("User"), tr("Sessions (🌐/🖥️)"), tr("Total Time"), tr("Trades"), tr("Trade Value"), tr("Portfolio Value"), tr("Last Seen")]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        layout.addWidget(self.table, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_refresh = QPushButton(tr("🔄 Refresh"))
        self.btn_refresh.clicked.connect(self._load)
        btn_row.addWidget(self.btn_refresh)
        btn_close = QPushButton(tr("Close"))
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _load(self):
        self.lbl_status.setText(tr("Loading session data…"))
        self.btn_refresh.setEnabled(False)
        self._worker = _CloudWorker(compute_usage_analytics, self.id_token)
        self._worker.finished_result.connect(self._on_loaded)
        self._worker.start()

    def _on_loaded(self, result):
        self.btn_refresh.setEnabled(True)
        if not result:
            self.lbl_status.setText(tr("⚠️ Could not load analytics data (check your connection or Firestore rules)."))
            self.table.setRowCount(0)
            return

        self.lbl_status.setText(
            tr("👥 Unique Users: {users}    |    "
               "📅 Total Sessions: {sessions}    |    "
               "⏱️ Avg Time / Session: {avg}").format(
                users=result['unique_users'],
                sessions=result['session_count'],
                avg=_format_duration(result['avg_duration_sec']),
            )
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


class PaperTradingDialog(QDialog):
    def __init__(self, dbm, parent=None):
        super().__init__(parent)
        self.dbm = dbm
        self.setWindowTitle(tr("🧪 Paper Trading"))
        self.resize(900, 620)
        self._init_ui()
        self._refresh()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        self.lbl_cash = QLabel()
        self.lbl_cash.setStyleSheet("font-weight: bold; font-size: 14px; color: #38bdf8;")
        layout.addWidget(self.lbl_cash)

        form = QHBoxLayout()
        # Editable QComboBox, not a plain QLineEdit: matches the Risk
        # Calculator dialog's own ticker field (see cmb_ticker above) and
        # the web app's <input list="portfolio-ticker-list"> - a typeable
        # box with pick-from-list autocomplete, not a free-text field the
        # user has to get exactly right from memory.
        self.cmb_ticker = QComboBox()
        self.cmb_ticker.setEditable(True)
        self.cmb_ticker.addItems([""] + self.dbm.get_unique_tickers())
        self.cmb_ticker.setPlaceholderText(tr("Ticker"))
        self.cmb_ticker.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        completer = self.cmb_ticker.completer()
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.spn_price = QDoubleSpinBox()
        self.spn_price.setDecimals(4)
        self.spn_price.setRange(0.0001, 1000000.0)
        self.spn_price.setPrefix(tr("Price: "))
        self.spn_shares = QDoubleSpinBox()
        self.spn_shares.setDecimals(4)
        self.spn_shares.setRange(0.0001, 100000000.0)
        self.spn_shares.setPrefix(tr("Shares: "))
        self.txt_note = QLineEdit()
        self.txt_note.setPlaceholderText(tr("Optional note"))
        self.btn_buy = QPushButton(tr("🟢 Paper Buy"))
        self.btn_sell = QPushButton(tr("🔴 Paper Sell"))
        self.btn_refresh = QPushButton(tr("🔄 Refresh"))
        self.btn_buy.clicked.connect(self._paper_buy)
        self.btn_sell.clicked.connect(self._paper_sell)
        self.btn_refresh.clicked.connect(self._refresh)
        for w in [self.cmb_ticker, self.spn_price, self.spn_shares, self.txt_note, self.btn_buy, self.btn_sell, self.btn_refresh]:
            form.addWidget(w)
        layout.addLayout(form)

        self.tbl_open = QTableWidget()
        self.tbl_open.setColumnCount(3)
        self.tbl_open.setHorizontalHeaderLabels([tr("Ticker"), tr("Shares"), tr("Avg Buy Price")])
        self.tbl_open.horizontalHeader().setStretchLastSection(True)
        self.tbl_open.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(QLabel(tr("Open Paper Positions")))
        layout.addWidget(self.tbl_open, stretch=1)

        self.tbl_history = QTableWidget()
        self.tbl_history.setColumnCount(6)
        self.tbl_history.setHorizontalHeaderLabels([tr("Date"), tr("Ticker"), tr("Side"), tr("Price"), tr("Shares"), tr("Note")])
        self.tbl_history.horizontalHeader().setStretchLastSection(True)
        self.tbl_history.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(QLabel(tr("Recent Paper Trades")))
        layout.addWidget(self.tbl_history, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_close = QPushButton(tr("Close"))
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def _paper_buy(self):
        ok, msg = self.dbm.paper_buy(self.cmb_ticker.currentText().strip().upper(), float(self.spn_price.value()), float(self.spn_shares.value()), self.txt_note.text().strip())
        QMessageBox.information(self, tr("Paper Trading"), msg) if ok else QMessageBox.warning(self, tr("Paper Trading"), msg)
        if ok: self._refresh()

    def _paper_sell(self):
        ok, msg = self.dbm.paper_sell(self.cmb_ticker.currentText().strip().upper(), float(self.spn_price.value()), float(self.spn_shares.value()), self.txt_note.text().strip())
        QMessageBox.information(self, tr("Paper Trading"), msg) if ok else QMessageBox.warning(self, tr("Paper Trading"), msg)
        if ok: self._refresh()

    def _refresh(self):
        self.lbl_cash.setText(tr("Paper Cash Balance: {v} EGP").format(v=f"{self.dbm.get_paper_cash_balance():,.2f}"))
        open_positions = self.dbm.get_paper_open_positions()
        self.tbl_open.setRowCount(len(open_positions))
        for i, row in enumerate(open_positions):
            vals = [row.get('ticker', ''), f"{row.get('shares', 0):,.4f}", f"{row.get('avg_buy_price', 0):,.4f}"]
            for j, val in enumerate(vals):
                self.tbl_open.setItem(i, j, QTableWidgetItem(str(val)))
        self.tbl_open.resizeColumnsToContents()

        trades = self.dbm.get_paper_trades(limit=100)
        self.tbl_history.setRowCount(len(trades))
        for i, row in enumerate(trades):
            vals = [row.get('trade_date', ''), row.get('ticker', ''), row.get('side', ''), f"{row.get('price', 0):,.4f}", f"{row.get('shares', 0):,.4f}", row.get('note', '')]
            for j, val in enumerate(vals):
                self.tbl_history.setItem(i, j, QTableWidgetItem(str(val)))
        self.tbl_history.resizeColumnsToContents()


class LeaderboardDialog(QDialog):
    def __init__(self, dbm, parent=None):
        super().__init__(parent)
        self.dbm = dbm
        self.setWindowTitle(tr("🥇 Leaderboard"))
        self.resize(680, 460)
        layout = QVBoxLayout(self)
        self.lbl_info = QLabel(tr("Most frequently achieved picks based on the local leaderboard table."))
        self.lbl_info.setWordWrap(True)
        layout.addWidget(self.lbl_info)
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels([tr("Ticker"), tr("Hits"), tr("Avg Return %"), tr("Last Achieved")])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, stretch=1)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_refresh = QPushButton(tr("🔄 Refresh"))
        btn_refresh.clicked.connect(self.refresh)
        btn_row.addWidget(btn_refresh)
        btn_close = QPushButton(tr("Close"))
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)
        self.refresh()

    def refresh(self):
        rows = self.dbm.get_leaderboard(limit=50)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            vals = [row.get('ticker', ''), str(row.get('hits', 0)), f"{row.get('avg_return_pct', row.get('avg_pct', 0)):.2f}", row.get('last_achieved_date', row.get('last_hit_date', ''))]
            for j, val in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(str(val)))
        self.table.resizeColumnsToContents()


class QuantDashboard(QMainWindow):
    def __init__(self, user_info=None):
        super().__init__()
        self.setWindowTitle(tr("MB-EGX — Out-of-Core Trading Matrix & Sector Dashboard"))
        self.resize(1520, 920)
        if LOGO_PATH.exists():
            self.setWindowIcon(QIcon(str(LOGO_PATH)))
        self.dbm = DatabaseManager()
        self.qe = QuantitativeEngine()
        self.current_theme = _SETTINGS.value("theme", "🌙 Institutional Dark")
        self.compact_mode = _SETTINGS.value("compact_mode", True, type=bool)
        # Shares the same persisted preference as LoginDialog (QSettings key
        # "lang") so a language choice made on either screen carries over.
        self.current_lang = CURRENT_LANG
        self.theme_highlight = QColor("#3198dc")
        self._raw_buys_data = []
        self.user_info = user_info
        self._session_id = None
        self._cloud_threads = set()
        self._init_ui()
        self.apply_theme(self.current_theme)
        self.apply_compact_mode(self.compact_mode)
        # Widgets are created with hardcoded English text; explicitly apply
        # the persisted language now so a saved Arabic preference actually
        # shows up immediately instead of only updating the dropdown itself.
        self.switch_language(1 if self.current_lang == "AR" else 0)
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

    def open_glossary_dialog(self):
        dlg = GlossaryDialog(self)
        dlg.exec()

    def open_paper_trading_dialog(self):
        dlg = PaperTradingDialog(self.dbm, self)
        dlg.exec()
        self.start_analysis()

    def open_leaderboard_dialog(self):
        dlg = LeaderboardDialog(self.dbm, self)
        dlg.exec()

    def open_strategy_calculator_dialog(self):
        dlg = StrategyCalculatorDialog(self)
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
            _SETTINGS.setValue("theme", theme_name)
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

    def apply_compact_mode(self, enabled: bool):
        self.compact_mode = bool(enabled)
        _SETTINGS.setValue("compact_mode", self.compact_mode)
        row_h = 24 if self.compact_mode else 30
        for table in self.findChildren(QTableView):
            table.verticalHeader().setDefaultSectionSize(row_h)

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

        self.lbl_concentration_warning = QLabel("")
        self.lbl_concentration_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_concentration_warning.setStyleSheet("color: #f6ad55; font-weight: bold; font-size: 11px;")
        # Deliberately NOT word-wrapped: this bar used to grow to however many
        # lines its combined text needed (concentration warnings + cash-drag
        # note + one rotation-flag line per held ticker), which could push
        # the toolbar/scan-folder row down several inches and eat the actual
        # workspace below. It's now a fixed-height, single-line summary with
        # the full detail in the tooltip (hover) instead - see
        # _build_risk_banner_text() for how the summary line is built.
        self.lbl_concentration_warning.setWordWrap(False)
        self.lbl_concentration_warning.setFixedHeight(18)
        self.lbl_concentration_warning.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.lbl_concentration_warning.hide()

        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(4)
        
        msg_layout.addWidget(self.lbl_disclosure)
        msg_layout.addWidget(self.lbl_status)
        msg_layout.addWidget(self.lbl_concentration_warning)
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

        self.btn_paper = QPushButton("🧪 Paper Trading")
        self.btn_paper.clicked.connect(self.open_paper_trading_dialog)

        self.btn_leaderboard = QPushButton("🥇 Leaderboard")
        self.btn_leaderboard.clicked.connect(self.open_leaderboard_dialog)

        self.btn_calc = QPushButton("⚖️ Risk Calculator")
        self.btn_calc.clicked.connect(self.open_calculator_dialog)

        self.btn_set_cash = QPushButton("💵 Set Cash")
        self.btn_set_cash.clicked.connect(self.prompt_set_cash)

        self.btn_settings = QPushButton("⚙️ Themes")
        self.btn_settings.clicked.connect(self.open_settings_dialog)

        self.btn_density = QPushButton("↕ Compact")
        self.btn_density.setCheckable(True)
        self.btn_density.setChecked(self.compact_mode)
        self.btn_density.clicked.connect(lambda checked: self.apply_compact_mode(checked))

        self.btn_top10 = QPushButton("🏆 Top 10")
        self.btn_top10.clicked.connect(self.show_top10_overview)

        self.btn_strategy_calc = QPushButton("🧮 Strategy Calculator")
        self.btn_strategy_calc.clicked.connect(self.open_strategy_calculator_dialog)

        controls_row.addWidget(self.btn_ingest)
        controls_row.addWidget(self.btn_analyze)
        controls_row.addWidget(self.btn_manage_portfolio)
        controls_row.addWidget(self.btn_paper)
        controls_row.addWidget(self.btn_leaderboard)
        controls_row.addWidget(self.btn_calc)
        controls_row.addWidget(self.btn_set_cash)
        controls_row.addWidget(self.btn_settings)
        controls_row.addWidget(self.btn_density)
        controls_row.addWidget(self.btn_top10)
        controls_row.addWidget(self.btn_strategy_calc)

        self.cmb_lang = QComboBox()
        self.cmb_lang.addItems(["🇬🇧 EN", "🇪🇬 AR"])
        self.cmb_lang.setCurrentIndex(1 if self.current_lang == "AR" else 0)
        self.cmb_lang.currentIndexChanged.connect(self.switch_language)
        self.cmb_lang.setFixedWidth(80)
        controls_row.addWidget(self.cmb_lang)
        
        self.btn_analytics = QPushButton("📊 Analytics")
        self.btn_analytics.setStyleSheet("background-color: #0d9488; color: white; font-weight: bold; padding: 6px 12px; font-size: 12px; border-radius: 6px;")
        self.btn_analytics.clicked.connect(self.open_analytics_dialog)
        self.btn_analytics.setVisible(bool(self.user_info and self.user_info.get("email") in ADMIN_EMAILS))
        controls_row.addWidget(self.btn_analytics)

        self.btn_glossary = QPushButton(tr("📖 Glossary"))
        self.btn_glossary.setStyleSheet("background-color: #6d28d9; color: white; font-weight: bold; padding: 6px 12px; font-size: 12px; border-radius: 6px;")
        self.btn_glossary.clicked.connect(self.open_glossary_dialog)
        controls_row.addWidget(self.btn_glossary)

        controls_row.addStretch()

        # BUGFIX: the toolbar previously sat directly in header_layout as a
        # plain QHBoxLayout with no wrap and no scroll area. On any window
        # narrower than the toolbar's natural width (e.g. not maximized, or
        # a smaller laptop screen), the right-most buttons - Analytics and
        # Glossary - were pushed past the visible edge with no way to reach
        # them short of manually widening the window. Wrapping the row in a
        # horizontal-only QScrollArea guarantees every button stays
        # reachable (via scroll/trackpad/shift+wheel) at any window size,
        # without forcing a minimum window width that would fight users on
        # smaller screens.
        controls_container = QWidget()
        controls_container.setLayout(controls_row)
        controls_scroll = QScrollArea()
        controls_scroll.setWidget(controls_container)
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        controls_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setFrameShape(QFrame.Shape.NoFrame)
        controls_scroll.setFixedHeight(controls_container.sizeHint().height() + 10)
        header_layout.addWidget(controls_scroll)

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
        self._action_filter_items = [
            "All Actions",
            "🔥 STRONG BUY",
            "⚡ BREAKOUT BUY",
            "📈 ACCUMULATE",
            "⏳ BUY ON DIP",
            "🟡 HOLD / NEUTRAL",
            "🛑 SELL / AVOID",
        ]
        self.cmb_action.addItems([tr(x) for x in self._action_filter_items])
        self.cmb_action.currentTextChanged.connect(self.apply_filters)
        filter_layout.addWidget(self.cmb_action, stretch=1)

        self.cmb_trend = QComboBox()
        self._trend_filter_items = [
            "All Trends",
            "Strong Bullish",
            "Weak Bullish",
            "Consolidation / Neutral",
            "Weak Bearish",
            "Strong Bearish",
        ]
        self.cmb_trend.addItems([tr(x) for x in self._trend_filter_items])
        self.cmb_trend.currentTextChanged.connect(self.apply_filters)
        filter_layout.addWidget(self.cmb_trend, stretch=1)

        self.cmb_confidence = QComboBox()
        self._confidence_filter_items = [
            "All Data Confidence",
            "High (1Y+)",
            "Medium (<1 Year)",
            "Low (<3 Months)",
            "Very Low (New/Short History)",
        ]
        self.cmb_confidence.addItems([tr(x) for x in self._confidence_filter_items])
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

        # Exits-tab-only toggle: the per-sector subtotal rows (one row per
        # sector represented among open positions) are useful context but
        # were previously always inserted, mixing "here's a position" rows
        # with "here's a sector rollup" rows in one table by default - off
        # by default now, one click to bring back when actually wanted.
        self.chk_sector_subtotals = QPushButton("🏢 Sector Subtotals")
        self.chk_sector_subtotals.setCheckable(True)
        self.chk_sector_subtotals.setChecked(False)
        self.chk_sector_subtotals.setStyleSheet("background-color: #4a5568; color: white; padding: 6px 12px; font-size: 11px; border-radius: 6px;")
        self.chk_sector_subtotals.setToolTip("Show one subtotal row per sector represented among your open positions, in the Exits tab.")
        self.chk_sector_subtotals.clicked.connect(self._on_sector_subtotals_toggled)
        filter_layout.addWidget(self.chk_sector_subtotals)

        self.btn_reset_filters = QPushButton("Reset Filters")
        self.btn_reset_filters.setStyleSheet("background-color: #4a5568; color: white; padding: 6px 12px; font-size: 11px; border-radius: 6px;")
        self.btn_reset_filters.clicked.connect(self.reset_filters)
        filter_layout.addWidget(self.btn_reset_filters)
        
        layout.addWidget(filter_wrap)

        # --- Screener Presets (parity port of the web dashboard's
        # SCREENER_PRESETS/applyScreenerPreset - see index.html). Same five
        # one-click screens, same predicates, evaluated against the exact
        # same row dicts (self._raw_buys_data) apply_filters() already
        # filters over, so the two apps can never disagree about what a
        # preset matches. ---
        self.SCREENER_PRESETS = [
            {
                "id": "strong_momentum", "icon": "🔥", "label": "Strong Momentum",
                "predicate": lambda r: (("STRONG BUY" in str(r.get("Action", "")) or "BREAKOUT BUY" in str(r.get("Action", "")))
                                         and _safe_float(r.get("ADX-14")) >= 20),
            },
            {
                "id": "low_vol_accumulate", "icon": "📈", "label": "Low-Vol Accumulation",
                "predicate": lambda r: ("ACCUMULATE" in str(r.get("Action", ""))
                                         and _safe_float(r.get("ADX-14")) < 20
                                         and -1 < _safe_float(r.get("Vol Z-Score")) < 1),
            },
            {
                "id": "oversold_dip", "icon": "⏳", "label": "Oversold Dip",
                "predicate": lambda r: ("BUY ON DIP" in str(r.get("Action", ""))
                                         and _safe_float(r.get("RSI-14"), default=100) <= 38),
            },
            {
                "id": "high_confidence", "icon": "💎", "label": "High Confidence",
                "predicate": lambda r: ("High" in str(r.get("Data Confidence", ""))
                                         and any(a in str(r.get("Action", "")) for a in ("STRONG BUY", "BREAKOUT BUY", "ACCUMULATE", "BUY ON DIP"))),
            },
            {
                "id": "pattern_confirmed", "icon": "🎯", "label": "Pattern Confirmed",
                "predicate": lambda r: isinstance(r.get("Pattern Conf (%)"), (int, float)) and r.get("Pattern Conf (%)") >= 60,
            },
        ]
        self.active_screener_preset = None
        self._preset_buttons = {}

        preset_wrap = QWidget()
        preset_wrap.setObjectName("webPanel")
        preset_layout = QHBoxLayout(preset_wrap)
        preset_layout.setContentsMargins(10, 6, 10, 6)
        preset_layout.setSpacing(6)
        lbl_presets = QLabel(tr("🧪 Screener Presets:"))
        lbl_presets.setStyleSheet("font-weight: bold; font-size: 12px; color: #93ccff;")
        preset_layout.addWidget(lbl_presets)
        for preset in self.SCREENER_PRESETS:
            btn = QPushButton(f"{preset['icon']} {tr(preset['label'])}")
            btn.setCheckable(True)
            btn.setStyleSheet(
                "QPushButton { background-color: #1e293b; color: #cbd5e0; padding: 5px 12px; "
                "font-size: 11px; border-radius: 12px; border: 1px solid #334155; }"
                "QPushButton:checked { background-color: #0284c7; color: white; border: 1px solid #38bdf8; }"
            )
            btn.clicked.connect(lambda checked, pid=preset["id"]: self.apply_screener_preset(pid))
            preset_layout.addWidget(btn)
            self._preset_buttons[preset["id"]] = btn
        preset_layout.addStretch()
        layout.addWidget(preset_wrap)

        self.tabs = QTabWidget()
        # Force reliable scroll arrows for the tab bar regardless of what the
        # active theme's stylesheet does to QTabBar - with 9 tabs (several
        # using emoji + longer labels), the bar can overflow the window
        # width. Qt normally auto-shows scroll arrows in that case, but a
        # custom QSS theme can style QTabBar::scroller invisibly (0px,
        # transparent) without an error anywhere, silently hiding whichever
        # tabs don't fit - which is what was hiding the Session Picks tab.
        self.tabs.setUsesScrollButtons(True)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.tabBar().setExpanding(False)
        # Explicit, theme-proof styling for the tab bar's overflow scroll
        # buttons - applied directly on the tab bar itself (not the window
        # stylesheet), so no THEMES_MAP entry can zero it out or make it
        # blend into the background. This guarantees a visible way to reach
        # any tab that doesn't fit in the bar, regardless of theme.
        self.tabs.tabBar().setStyleSheet(
            "QTabBar::scroller { width: 28px; } "
            "QTabBar QToolButton { background-color: #2b6cb0; border: 1px solid #cbd5e0; "
            "border-radius: 4px; }"
        )
        self.tbl_buys = self._create_matrix_table()
        
        self.tbl_sectors = QTableWidget()
        # Stored in English and re-applied through tr() both here and from
        # switch_language(), so a language toggle re-translates the headers
        # of this plain QTableWidget too (unlike tbl_buys/top10, which use
        # MatrixTableModel and read tr() live on every repaint).
        self._sector_cols = ["Sector", "Stocks", "1D Return (%)", "5D Return (%)", "Money Flow (CMF)", "Bullish Breadth (%)", "Traded Value (EGP)", "Sector Leader", "Sector Status"]
        self.tbl_sectors.setColumnCount(len(self._sector_cols))
        self.tbl_sectors.setHorizontalHeaderLabels([tr(c) for c in self._sector_cols])
        self.tbl_sectors.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        self.tbl_exits = QTableWidget()
        self._exit_columns = [
            ("Ticker", "Stock ticker symbol"),
            ("Shares", "Shares currently held"),
            ("Buy Price", "Your average cost basis"),
            ("Price", "Current close price"),
            ("Purchased Value (EGP)", "Total amount you paid for this position (Shares × Buy Price)"),
            ("Current Value (EGP)", "What this position is worth right now (Shares × Current Price)"),
            ("P&L (EGP)", "Unrealized profit/loss in EGP"),
            ("P&L (%)", "Unrealized profit/loss percentage"),
            ("Net P&L (EGP)", "P&L after both round-trip trading fees: the buy-side fee already paid plus the sell-side fee you'd pay to close this position today"),
            ("Net P&L (%)", "Fee-adjusted P&L, as a % of your original cost basis"),
            ("Action", "Suggested action: hold/trail, take-profit zone, or cut-loss review"),
            ("Take-Profit", "Take-profit target"),
            ("Trail Stop", "Trailing stop-loss (2x ATR below current price)"),
            ("Dist. to Stop %", "How close the current price already is to crossing the trailing stop-loss line — a leading indicator, unlike the Action column which only flags once it's already crossed"),
            ("Trend", "Trend classification"),
            ("RSI-14", "14-period Relative Strength Index"),
            ("ADX-14", "14-period trend-strength index"),
            ("Data Conf.", "How much real history backs these numbers"),
            ("Purchase Date", "Date this position was opened"),
            ("Days Held", "Calendar days since this position was opened"),
            ("Annualized %", "Unrealized P&L (%) compounded to a yearly rate based on Days Held — separates a slow multi-week grind from a fast one-week pop that show the same raw P&L (%)"),
            ("Drawdown from Peak %", "How far the current price has pulled back from its highest close since this position was opened — a stock that ran +20% and is now at +3% reads very differently from one that climbed steadily to +3%"),
            ("Risk vs Plan", "This position's size as a multiple of your normal 1%-risk position size (config.RISK_PER_TRADE_PCT). Flagged when the position alone exceeds the single-position concentration threshold"),
            ("My Target Price", "The exit price that reaches your chosen profit target for this position"),
            ("My Target %", "Your chosen profit target, as a % gain from your buy price"),
            ("My Target (EGP)", "Your chosen profit target, in EGP profit on this position"),
            ("Est. Time to Target", "Rough estimate of how many trading days it might take to reach your target, based on this stock's own recent pace - not a guarantee"),
            ("Breakeven Shares", "Extra shares to buy right now at the current price to blend your average cost down to breakeven (net of round-trip fees)"),
            ("Breakeven Avg Cost", "Your new average cost per share if you bought the Breakeven Shares amount above"),
            ("Breakeven Cost (EGP)", "Total EGP needed to buy the Breakeven Shares amount at the current price"),
        ]
        self.tbl_exits.setColumnCount(len(self._exit_columns))
        for idx, (header, tooltip) in enumerate(self._exit_columns):
            item = QTableWidgetItem(tr(header))
            item.setToolTip(tooltip)
            self.tbl_exits.setHorizontalHeaderItem(idx, item)
        self.tbl_exits.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tbl_exits.horizontalHeader().setMinimumSectionSize(70)

        # Default to a curated, "what do I actually do about this position"
        # column set instead of dumping all 24 on screen at once - the rest
        # (raw indicator values, target/breakeven planning numbers) are one
        # click away via the existing 👁️ Columns button, not gone. Keeps a
        # first look at the Exits tab scannable instead of a wall of numbers.
        _exit_secondary_columns = {
            "Shares", "Buy Price", "Purchased Value (EGP)", "Current Value (EGP)",
            "P&L (EGP)", "Net P&L (EGP)", "Take-Profit", "Trail Stop",
            "RSI-14", "ADX-14", "Data Conf.", "Purchase Date",
            "Drawdown from Peak %", "My Target Price", "My Target %", "My Target (EGP)",
            "Est. Time to Target", "Breakeven Shares", "Breakeven Avg Cost", "Breakeven Cost (EGP)",
        }
        for idx, (header, _tooltip) in enumerate(self._exit_columns):
            self.tbl_exits.setColumnHidden(idx, header in _exit_secondary_columns)

        self.tbl_breakout_watch = QTableWidget()
        self._breakout_watch_columns = [
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
        self.tbl_breakout_watch.setColumnCount(len(self._breakout_watch_columns))
        for idx, (header, tooltip) in enumerate(self._breakout_watch_columns):
            item = QTableWidgetItem(tr(header))
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
        self._closed_cols = [
            "Ticker", "Shares Sold", "Buy Price", "Sell Price",
            "Realized P&L (EGP)", "Realized P&L (%)", "Purchase Date", "Sell Date",
        ]
        self.tbl_closed.setHorizontalHeaderLabels([tr(c) for c in self._closed_cols])
        self.tbl_closed.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        history_layout.addWidget(self.tbl_closed)

        self.tbl_fin_stmt = QTableWidget()
        self.tbl_fin_stmt.setColumnCount(2)
        self._fin_stmt_cols = ["Accounting Metric / Line Item", "Value (EGP / %)"]
        self.tbl_fin_stmt.setHorizontalHeaderLabels([tr(c) for c in self._fin_stmt_cols])
        self.tbl_fin_stmt.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        self.tbl_top_strong = self._create_matrix_table()
        self.tbl_top_breakout = self._create_matrix_table()
        self.tbl_top_accum = self._create_matrix_table()
        self.tbl_top_dip = self._create_matrix_table()
        self.top10_overview_widget = self._build_top10_overview_tab()

        logger.info("Building Session Picks tab...")
        self.session_picks_widget = self._build_session_picks_tab()
        logger.info("Session Picks tab widget built OK.")

        self.chart_widget = StockSectorChartWidget(self.qe, self.dbm, self)

        # Precise 8 Tab mappings matching TRANSLATIONS
        self.tabs.addTab(self.tbl_buys, "📈 Action Matrix")
        self.tabs.addTab(self.tbl_sectors, "🏢 Sectors")
        self.tabs.addTab(self.tbl_exits, "🛡️ Exits")
        self.tabs.addTab(self.tbl_breakout_watch, "🎯 Breakouts")
        self.tabs.addTab(self.session_picks_widget, "🎯 Session Picks")
        logger.info(f"Session Picks tab added. Total tab count is now: {self.tabs.count()}")
        self.tabs.addTab(tab_history_widget, "📜 History")
        self.tabs.addTab(self.tbl_fin_stmt, "📊 Financials")
        self.tabs.addTab(self.top10_overview_widget, "🏆 Top 10")
        self.tabs.addTab(self.chart_widget, "📊 Charts")

        layout.addWidget(self.tabs)
        self.update_last_data_date_display()
        self.refresh_account_header()

    def _on_sector_subtotals_toggled(self):
        if getattr(self, "_last_populate_args", None):
            self.populate_tables(**self._last_populate_args, _push_cloud_stats=False)

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
        set_language(self.current_lang)
        self.setWindowTitle(tr("MB-EGX — Out-of-Core Trading Matrix & Sector Dashboard"))
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
        self.btn_paper.setText("🧪 Paper Trading" if self.current_lang == "EN" else "🧪 تداول تجريبي")
        self.btn_leaderboard.setText("🥇 Leaderboard" if self.current_lang == "EN" else "🥇 لوحة الصدارة")
        self.btn_calc.setText(t["risk_calc"])
        self.btn_set_cash.setText(t["set_cash"])
        self.btn_settings.setText(t["themes"])
        self.btn_density.setText("↕ Compact" if self.current_lang == "EN" else "↕ وضع مضغوط")
        self.btn_top10.setText(t["top10_btn"])
        self.lbl_filter.setText(t["filters"])
        self.txt_search.setPlaceholderText(t["search_ph"])
        self.chk_hide_illiquid.setText(t["hide_illiquid"])
        self.chk_sector_subtotals.setText("🏢 Sector Subtotals" if self.current_lang == "EN" else "🏢 إجماليات القطاعات")
        self.btn_columns.setText(t["btn_columns"])
        self.btn_reset_filters.setText(t["reset_filters"])

        self.tabs.setTabText(0, t.get("tab_matrix", "📈 Action Matrix"))
        self.tabs.setTabText(1, t.get("tab_sectors", "🏢 Sectors"))
        self.tabs.setTabText(2, t.get("tab_exits", "🛡️ Exits"))
        self.tabs.setTabText(3, t.get("tab_breakout", "🎯 Breakouts"))
        self.tabs.setTabText(4, t.get("tab_session_picks", "🎯 Session Picks"))
        self.tabs.setTabText(5, t.get("tab_history", "📜 History"))
        self.tabs.setTabText(6, t.get("tab_fin", "📊 Financials"))
        self.tabs.setTabText(7, t.get("tab_top10", "🏆 Top 10"))
        self.tabs.setTabText(8, t.get("tab_charts", "📊 Charts"))

        if hasattr(self, "chart_widget"):
            self.chart_widget.set_language(self.current_lang)
        self.update_last_data_date_display()
        self.refresh_account_header()

        # MatrixTableModel's data()/headerData() read the module-level
        # CURRENT_LANG directly, but Qt views only repaint when told the
        # model changed — so explicitly signal each one now.
        for tbl in (getattr(self, "tbl_buys", None), getattr(self, "tbl_top_strong", None),
                    getattr(self, "tbl_top_breakout", None), getattr(self, "tbl_top_accum", None),
                    getattr(self, "tbl_top_dip", None)):
            if tbl is not None and tbl.model() is not None:
                m = tbl.model()
                m.layoutChanged.emit()
                m.headerDataChanged.emit(Qt.Orientation.Horizontal, 0, m.columnCount() - 1)

        # The Sectors / Exits / Breakouts / History / Financials tabs are
        # plain QTableWidgets (not tr()-aware models like the Action Matrix
        # / Top 10 tables above), so their headers need to be re-applied by
        # hand here.
        if hasattr(self, "_sector_cols"):
            self.tbl_sectors.setHorizontalHeaderLabels([tr(c) for c in self._sector_cols])
        if hasattr(self, "_exit_columns"):
            for idx, (header, tooltip) in enumerate(self._exit_columns):
                item = QTableWidgetItem(tr(header))
                item.setToolTip(tooltip)
                self.tbl_exits.setHorizontalHeaderItem(idx, item)
        if hasattr(self, "_breakout_watch_columns"):
            for idx, (header, tooltip) in enumerate(self._breakout_watch_columns):
                item = QTableWidgetItem(tr(header))
                item.setToolTip(tooltip)
                self.tbl_breakout_watch.setHorizontalHeaderItem(idx, item)
        if hasattr(self, "_closed_cols"):
            self.tbl_closed.setHorizontalHeaderLabels([tr(c) for c in self._closed_cols])
        if hasattr(self, "_fin_stmt_cols"):
            self.tbl_fin_stmt.setHorizontalHeaderLabels([tr(c) for c in self._fin_stmt_cols])

        # Top 10 tab section titles ("🔥 Top 10 Strong Buy", etc.)
        for english_title, label in getattr(self, "_top10_section_labels", []):
            label.setText(tr(english_title))

        # Live-filter dropdowns: re-apply translated display text at each
        # fixed index (filtering logic itself always compares against the
        # English values in self._*_filter_items, so this is display-only).
        if hasattr(self, "_action_filter_items"):
            for i, key in enumerate(self._action_filter_items):
                self.cmb_action.setItemText(i, tr(key))
        if hasattr(self, "_trend_filter_items"):
            for i, key in enumerate(self._trend_filter_items):
                self.cmb_trend.setItemText(i, tr(key))
        if hasattr(self, "_confidence_filter_items"):
            for i, key in enumerate(self._confidence_filter_items):
                self.cmb_confidence.setItemText(i, tr(key))

        # Re-render already-loaded row data (Sector/Action/Trend/Signals
        # values, financial statement labels...) in the new language,
        # without re-running analysis or re-pushing cloud stats.
        if getattr(self, "_last_populate_args", None):
            self.populate_tables(**self._last_populate_args, _push_cloud_stats=False)

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
        self._top10_section_labels = []
        for title, tbl in sections:
            section_label = QLabel(tr(title))
            section_label.setStyleSheet("font-weight: bold; font-size: 14px; padding: 6px 2px 2px 2px;")
            self._top10_section_labels.append((title, section_label))
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

    # =========================================================================
    # SESSION PICKS TAB
    # Forward-looking watchlist: up to 5 next-session / 3 medium-term /
    # 3 long-term active picks, auto-refilled and achievement-checked by
    # session_picks.refresh_session_picks() on every "Execute Matrix" run
    # (see decision_matrix.DecisionMatrix.analyze_market). This tab only
    # displays what's already in the DB + lets you manually clear a pick;
    # it never decides which tickers get picked.
    # =========================================================================
    _PICKS_COLS = ["Ticker", "Picked On", "Target Gain", "Expected By", "Pick Price", "Current Price", "Change (%)", "Status", ""]
    _ACHIEVED_COLS = ["Ticker", "Horizon", "Picked On", "Pick Price", "Achieved On", "Achieved Price", "Achieved (%)"]

    def _make_picks_table(self, columns):
        tbl = QTableWidget()
        tbl.setColumnCount(len(columns))
        tbl.setHorizontalHeaderLabels([tr(c) if c else "" for c in columns])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        tbl.verticalHeader().setVisible(False)
        return tbl

    def _build_session_picks_tab(self):
        container = QWidget()
        v_layout = QVBoxLayout(container)
        v_layout.setSpacing(4)

        intro = QLabel(tr(
            "🎯 The app's forward-looking watchlist — auto-picked and auto-refilled every time you run the matrix. "
            "Each horizon has its own target gain (see the Target Gain column) — a pick moves down to Track "
            "Record once it's up that much from the price it was picked at."
        ))
        intro.setWordWrap(True)
        intro.setStyleSheet("font-size: 11px; color: #a0aec0; padding: 2px 2px 6px 2px;")
        v_layout.addWidget(intro)

        from config import SESSION_PICKS_EXPECTED_PCT
        sections = [
            ("short", f"🚀 Next Session (up to 5, target +{SESSION_PICKS_EXPECTED_PCT.get('short', 3):.0f}%)"),
            ("medium", f"📈 Medium-Term (up to 3, target +{SESSION_PICKS_EXPECTED_PCT.get('medium', 8):.0f}%)"),
            ("long", f"🏛️ Long-Term (up to 3, target +{SESSION_PICKS_EXPECTED_PCT.get('long', 15):.0f}%)"),
        ]
        self._picks_tables = {}
        for horizon, title in sections:
            label = QLabel(tr(title))
            label.setStyleSheet("font-weight: bold; font-size: 14px; padding: 6px 2px 2px 2px;")
            v_layout.addWidget(label)
            tbl = self._make_picks_table(self._PICKS_COLS)
            tbl.setMinimumHeight(160)
            tbl.setMaximumHeight(160)
            self._picks_tables[horizon] = tbl
            v_layout.addWidget(tbl)

        achieved_label = QLabel(tr("📜 Track Record — Calls That Hit"))
        achieved_label.setStyleSheet("font-weight: bold; font-size: 14px; padding: 10px 2px 2px 2px;")
        v_layout.addWidget(achieved_label)
        self.tbl_picks_achieved = self._make_picks_table(self._ACHIEVED_COLS)
        self.tbl_picks_achieved.setMinimumHeight(220)
        v_layout.addWidget(self.tbl_picks_achieved)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        return scroll

    def _remove_session_pick(self, pick_id):
        reply = QMessageBox.question(
            self, tr("Remove Pick"),
            tr("Remove this pick from the watchlist? A new candidate will take its slot next time you run the matrix."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.dbm.remove_pick(pick_id)
            self.start_analysis()

    def _fill_session_picks(self, session_picks: dict):
        session_picks = session_picks or {}
        # Current prices come from the same matrix run's buy_recommendations
        # (cached as self._raw_buys_data by populate_tables) - no extra query.
        price_map = {r["Ticker"]: r.get("Current Price") for r in (self._raw_buys_data or [])}
        achieved_today_ids = {p["id"] for p in session_picks.get("achieved_today", [])}

        for horizon, tbl in self._picks_tables.items():
            picks = session_picks.get(horizon, [])
            tbl.setRowCount(len(picks))
            for row_idx, pick in enumerate(picks):
                current_price = price_map.get(pick["ticker"])
                ref_price = pick["ref_price"]
                if current_price is not None and ref_price:
                    pct = (current_price / ref_price - 1.0) * 100.0
                    pct_str = f"{pct:+.2f}%"
                else:
                    pct, pct_str = None, "-"

                expected_from = pick.get("expected_from")
                expected_by = pick.get("expected_by")
                if expected_from and expected_by:
                    expected_str = f"{expected_from} → {expected_by}" if expected_from != expected_by else expected_from
                else:
                    expected_str = "-"

                target_pct = pick.get("expected_pct")
                target_str = f"+{target_pct:.0f}%" if target_pct is not None else "-"

                values = [
                    pick["ticker"],
                    pick["pick_date"],
                    target_str,
                    expected_str,
                    f"{ref_price:.4f}",
                    f"{current_price:.4f}" if current_price is not None else "-",
                    pct_str,
                    tr("🟢 Active"),
                ]
                for col_idx, val in enumerate(values):
                    item = QTableWidgetItem(str(val))
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if col_idx == 6 and pct is not None:
                        item.setForeground(QColor("#38a169" if pct >= 0 else "#e53e3e"))
                        item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                    tbl.setItem(row_idx, col_idx, item)

                btn_remove = QPushButton(tr("✖ Remove"))
                btn_remove.setStyleSheet("background-color: #742a2a; color: white; border-radius: 4px; padding: 2px 8px;")
                btn_remove.clicked.connect(lambda _, pid=pick["id"]: self._remove_session_pick(pid))
                tbl.setCellWidget(row_idx, 8, btn_remove)

        # Track Record — pulled fresh from the DB (full history, not just
        # this run), with today's newly-achieved rows highlighted gold.
        recent = self.dbm.get_recent_achieved_picks(limit=20)
        self.tbl_picks_achieved.setRowCount(len(recent))
        for row_idx, pick in enumerate(recent):
            values = [
                pick["ticker"],
                tr({"short": "Next Session", "medium": "Medium-Term", "long": "Long-Term"}.get(pick["horizon"], pick["horizon"])),
                pick["pick_date"],
                f"{pick['ref_price']:.4f}",
                pick["achieved_date"],
                f"{pick['achieved_price']:.4f}",
                f"+{pick['achieved_pct']:.2f}%",
            ]
            is_fresh = pick["id"] in achieved_today_ids
            for col_idx, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if is_fresh:
                    item.setBackground(QColor("#975a16"))
                    item.setForeground(Qt.GlobalColor.white)
                    item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                elif col_idx == 6:
                    item.setForeground(QColor("#38a169"))
                self.tbl_picks_achieved.setItem(row_idx, col_idx, item)

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
        box = QMessageBox(self)
        box.setWindowTitle(tr("Set Account Cash Balance"))
        box.setText(tr(
            "How would you like to update your cash balance?\n\n"
            "• Set Exact Amount — directly overwrite the balance (use this if you already know the correct number).\n"
            "• Recalculate From Trades — enter what you started with BEFORE your first trade, and the app rebuilds "
            "the balance from your full buy/sell history (fixes drift from before cash tracking was wired up)."
        ))
        btn_exact = box.addButton(tr("Set Exact Amount"), QMessageBox.ButtonRole.AcceptRole)
        btn_recalc = box.addButton(tr("Recalculate From Trades"), QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()

        if clicked == btn_exact:
            val, ok = QInputDialog.getDouble(self, tr("Set Account Cash Balance"), tr("Enter available cash balance in EGP:"), current_cash, 0.0, 1000000000.0, 2)
            if ok:
                self.dbm.set_cash_balance(val)
                QMessageBox.information(self, tr("Cash Updated"), tr("Account cash balance successfully updated to: {v} EGP.").format(v=f"{val:,.2f}"))
                self.start_analysis()
        elif clicked == btn_recalc:
            start_val, ok = QInputDialog.getDouble(self, tr("Recalculate Cash From Trade History"), tr("Enter the cash you started with BEFORE your very first trade (EGP):"), current_cash, 0.0, 1000000000.0, 2)
            if ok:
                new_balance = self.dbm.recalculate_cash_from_history(start_val)
                QMessageBox.information(self, tr("Cash Recalculated"), tr("Rebuilt from your full trade history. New cash balance: {v} EGP.").format(v=f"{new_balance:,.2f}"))
                self.start_analysis()

    def export_trade_ledger(self):
        all_trades = self.dbm.get_all_closed_trades()
        trades = [t for t in all_trades if not t.get("is_demo")]
        n_demo_excluded = len(all_trades) - len(trades)
        if not trades:
            msg = tr("No real (non-demo) closed trades available to export.")
            if n_demo_excluded:
                msg += "\n" + tr("({n} demo trade(s) were excluded.)").format(n=n_demo_excluded)
            QMessageBox.warning(self, tr("Export Error"), msg)
            return
        
        file_path, _ = QFileDialog.getSaveFileName(self, tr("Export Audit Ledger"), "Trade_Audit_Ledger.csv", tr("CSV Files (*.csv);;Excel Files (*.xlsx)"))
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
            QMessageBox.information(self, tr("Export Successful"), tr("Audit ledger successfully saved to:\n{path}\n\nWin Rate: {wr}% | Profit Factor: {pf}").format(path=file_path, wr=f"{win_rate:.1f}", pf=f"{profit_factor:.2f}"))
        except Exception as e:
            QMessageBox.critical(self, tr("Export Failed"), tr("Could not save file:\n{err}").format(err=str(e)))

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
        self.active_screener_preset = None
        for btn in getattr(self, "_preset_buttons", {}).values():
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
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
            QMessageBox.warning(self, tr("Invalid Folder"), tr("The directory does not exist:\n{dir}").format(dir=target_directory))
            return
        self._set_ui_controls_enabled(False)
        self.ingest_worker = IngestionWorker(target_dir=target_directory)
        self.ingest_worker.progress_signal.connect(self.update_progress)
        self.ingest_worker.finished_signal.connect(self.ingestion_done)
        self.ingest_worker.start()

    def ingestion_done(self):
        self._set_ui_controls_enabled(True)
        self.lbl_status.setText(tr("⚡ Ingestion successfully flushed to DuckDB."))
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

    @staticmethod
    def _safe_float(val, default=0.0):
        """Best-effort float coercion for table cells that may be None,
        '-', or already numeric — used when summing/aggregating raw
        row_data straight out of the matrix, which is display-formatted,
        not guaranteed numeric."""
        if val is None:
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    def _set_exit_summary_item(self, row_idx, col_idx, text, bg, fg=Qt.GlobalColor.white, tooltip=None, bold=True):
        """Shared cell builder for every appended Exits-tab summary row
        (sector subtotals, grand total, combined account total) — keeps
        their read-only/non-selectable/centered/font styling identical so
        only the color and text vary row to row."""
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable & ~Qt.ItemFlag.ItemIsSelectable)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setFont(QFont("Inter", 10, QFont.Weight.Bold if bold else QFont.Weight.Normal))
        item.setBackground(bg)
        item.setForeground(fg)
        if tooltip:
            item.setToolTip(tooltip)
        self.tbl_exits.setItem(row_idx, col_idx, item)

    def _fill_exits_sector_rows(self, row_idx_start, keys, by_sector, total_current):
        """One subtotal row per sector represented among open positions —
        lets a concentration warning like 'PHGC.CA is 26% of equity' be
        checked against whether that risk is isolated to one stock or
        spread across a correlated sector. Sorted by current value so the
        biggest sector exposure is always the first row. A sector whose
        share of total portfolio value crosses the same
        sector_concentration_warn_pct the account-level risk warning
        already uses is flagged amber, for one consistent threshold
        instead of a second one invented just for this table."""
        warn_pct = PORTFOLIO_RISK_THRESHOLDS.get("sector_concentration_warn_pct", 35.0)
        ordered = sorted(by_sector.items(), key=lambda kv: kv[1]["current"], reverse=True)
        row_idx = row_idx_start
        for sector, agg in ordered:
            weight_pct = (agg["current"] / total_current * 100.0) if total_current else 0.0
            sector_pl_pct = (agg["pl_egp"] / agg["purchased"] * 100.0) if agg["purchased"] else 0.0
            avg_days = (agg["days_sum"] / agg["days_n"]) if agg["days_n"] else None
            is_concentrated = weight_pct >= warn_pct

            label = f"🏢 {sector} • {weight_pct:.1f}%"
            if is_concentrated:
                label += " ⚠️"
            bg = QColor("#7c4a03") if is_concentrated else QColor("#2d3748")
            tip = (
                f"{sector}: {agg['count']} ticker{'s' if agg['count'] != 1 else ''}, "
                f"{weight_pct:.1f}% of total open-position value"
                + (f" — above the {warn_pct:.0f}% concentration warning threshold" if is_concentrated else "")
            )

            values_by_key = {
                "Ticker": label,
                "Purchased Value (EGP)": f"{agg['purchased']:,.2f}",
                "Current Value (EGP)": f"{agg['current']:,.2f}",
                "P&L (EGP)": f"{agg['pl_egp']:,.2f}",
                "P&L (%)": f"{sector_pl_pct:,.2f}",
                "Days Held": f"{avg_days:.0f}d avg" if avg_days is not None else "-",
            }
            for col_idx, key in enumerate(keys):
                text = values_by_key.get(key, "-")
                fg = Qt.GlobalColor.white
                if key in ("P&L (EGP)", "P&L (%)"):
                    if agg["pl_egp"] > 0:
                        fg = QColor("#68d391")
                    elif agg["pl_egp"] < 0:
                        fg = QColor("#fc8181")
                self._set_exit_summary_item(row_idx, col_idx, text, bg, fg, tip if key == "Ticker" else None, bold=False)
            row_idx += 1
        return row_idx

    def _fill_exits_totals_row(self, row_idx, keys, total_shares, total_purchased,
                                total_current, total_pl_egp, win_count, loss_count,
                                flat_count, best_pct, worst_pct, best_egp, worst_egp,
                                gross_gains_egp, gross_losses_egp, action_counts,
                                avg_days_held):
        """Appends a bold, visually distinct grand-total row under the open
        positions (and any sector subtotal rows above it): total shares
        held, total amount invested at cost, total current market value,
        and the resulting overall P&L in both EGP and % — the weighted
        portfolio-level return, not an average of each row's own %, so a
        large position's move counts proportionally more than a small
        one's (matches how the real account P&L works). The whole row is
        tinted green/red by the sign of that total, the same visual
        language as the per-row Action column.

        The label itself carries the two numbers most likely to change
        what someone does next: the win/loss breadth, and — if one ticker
        is responsible for the majority of total losses or gains — which
        one and how much, so a warning like 'one stock is 26% of your
        equity' upstream becomes 'and yes, it's also 91% of your losses'
        right here instead of requiring a scan down the P&L column."""
        total_pl_pct = (total_pl_egp / total_purchased * 100.0) if total_purchased else 0.0
        n_positions = win_count + loss_count + flat_count

        breadth = f"{win_count} up / {loss_count} down"
        if flat_count:
            breadth += f" / {flat_count} flat"

        concentration_flag = ""
        concentration_tip = ""
        if worst_egp and gross_losses_egp > 0:
            worst_loss_share = abs(worst_egp[1]) / gross_losses_egp * 100.0
            if worst_loss_share >= 50.0:
                concentration_flag = f" ⚠️ {worst_egp[0]} drives {worst_loss_share:.0f}% of losses"
                concentration_tip = (
                    f"{worst_egp[0]} alone accounts for {worst_loss_share:.0f}% of your total "
                    f"unrealized losses ({worst_egp[1]:,.2f} EGP of -{gross_losses_egp:,.2f} EGP)."
                )
        if best_egp and gross_gains_egp > 0 and not concentration_flag:
            best_gain_share = best_egp[1] / gross_gains_egp * 100.0
            if best_gain_share >= 50.0:
                concentration_flag = f" • {best_egp[0]} drives {best_gain_share:.0f}% of gains"
                concentration_tip = (
                    f"{best_egp[0]} alone accounts for {best_gain_share:.0f}% of your total "
                    f"unrealized gains (+{best_egp[1]:,.2f} EGP of +{gross_gains_egp:,.2f} EGP)."
                )

        best_pct_str = f"{best_pct[0]} ({best_pct[1]:+.2f}%)" if best_pct else "-"
        worst_pct_str = f"{worst_pct[0]} ({worst_pct[1]:+.2f}%)" if worst_pct else "-"
        best_egp_str = f"{best_egp[0]} ({best_egp[1]:+,.2f} EGP)" if best_egp else "-"
        worst_egp_str = f"{worst_egp[0]} ({worst_egp[1]:+,.2f} EGP)" if worst_egp else "-"
        record_tip = (
            f"{breadth}\n"
            f"Best by %: {best_pct_str}   •   Worst by %: {worst_pct_str}\n"
            f"Best by EGP impact: {best_egp_str}   •   Worst by EGP impact: {worst_egp_str}"
            + (f"\n\n{concentration_tip}" if concentration_tip else "")
        )
        action_tip = (
            f"Hold/Trail: {action_counts.get('HOLD', 0)}  •  "
            f"Take-Profit zone: {action_counts.get('TAKE PROFIT', 0)}  •  "
            f"Cut-Loss review: {action_counts.get('CUT LOSS', 0)}"
        )

        totals_by_key = {
            "Ticker": f"📊 OPEN POSITIONS TOTAL — {n_positions} position{'s' if n_positions != 1 else ''} ({breadth}){concentration_flag}",
            "Shares": f"{total_shares:,.2f}".rstrip("0").rstrip("."),
            "Purchased Value (EGP)": f"{total_purchased:,.2f}",
            "Current Value (EGP)": f"{total_current:,.2f}",
            "P&L (EGP)": f"{total_pl_egp:,.2f}",
            "P&L (%)": f"{total_pl_pct:,.2f}",
            "Days Held": f"{avg_days_held:.0f}d avg" if avg_days_held is not None else "-",
        }
        tooltips_by_key = {
            "Ticker": record_tip,
            "P&L (EGP)": record_tip,
            "P&L (%)": record_tip,
            "Action Command": action_tip,
        }

        if total_pl_egp > 0:
            totals_bg = QColor("#22543d")
        elif total_pl_egp < 0:
            totals_bg = QColor("#9b2c2c")
        else:
            totals_bg = QColor("#1a2942")

        for col_idx, key in enumerate(keys):
            text = totals_by_key.get(key, "-")
            fg = Qt.GlobalColor.white
            if key in ("P&L (EGP)", "P&L (%)"):
                if total_pl_egp > 0:
                    fg = QColor("#9ae6b4")
                elif total_pl_egp < 0:
                    fg = QColor("#feb2b2")
            self._set_exit_summary_item(row_idx, col_idx, text, totals_bg, fg, tooltips_by_key.get(key))
        return row_idx + 1

    def _fill_exits_combined_row(self, row_idx, keys, unrealized_pl_egp, unrealized_cost, closed_trades):
        """Appends the 'am I up overall' row: unrealized P&L on today's
        open positions blended with realized P&L already banked from
        closed_trades (the same list the History tab totals), so someone
        doesn't have to flip tabs and add two numbers themselves to
        answer that question."""
        realized_pl_egp = sum(self._safe_float(t.get("Realized P&L (EGP)")) for t in closed_trades)
        realized_cost = sum(
            self._safe_float(t.get("Shares Sold")) * self._safe_float(t.get("Buy Price"))
            for t in closed_trades
        )
        combined_pl_egp = unrealized_pl_egp + realized_pl_egp
        combined_cost = unrealized_cost + realized_cost
        combined_pl_pct = (combined_pl_egp / combined_cost * 100.0) if combined_cost else 0.0

        tip = (
            f"Unrealized (open positions): {unrealized_pl_egp:+,.2f} EGP\n"
            f"Realized (closed trades, {len(closed_trades)} total): {realized_pl_egp:+,.2f} EGP\n"
            f"Combined: {combined_pl_egp:+,.2f} EGP on {combined_cost:,.2f} EGP total cost basis"
        )

        values_by_key = {
            "Ticker": "🧮 ACCOUNT TOTAL — Realized + Unrealized",
            "Purchased Value (EGP)": f"{combined_cost:,.2f}",
            "P&L (EGP)": f"{combined_pl_egp:,.2f}",
            "P&L (%)": f"{combined_pl_pct:,.2f}",
        }
        bg = QColor("#2c5282")
        for col_idx, key in enumerate(keys):
            text = values_by_key.get(key, "-")
            fg = Qt.GlobalColor.white
            if key in ("P&L (EGP)", "P&L (%)"):
                if combined_pl_egp > 0:
                    fg = QColor("#9ae6b4")
                elif combined_pl_egp < 0:
                    fg = QColor("#feb2b2")
            self._set_exit_summary_item(row_idx, col_idx, text, bg, fg, tip)

    def _render_risk_banner(self, pr):
        """Fixed-height, single-line risk summary bar. Cash-drag is
        deliberately left out here - it's informational, not urgent, and
        already has its own row on the Financials tab, so repeating it here
        was pure clutter. Rotation flags are consolidated by CANDIDATE
        (one line covering every held ticker that candidate outranks)
        instead of one line per held ticker, since several held positions
        often lose to the same single candidate - three lines all ending
        '...prefers CCRS.CA' said nothing that one line couldn't.
        Everything (warnings + rotation detail) is still fully available
        via the bar's tooltip on hover - nothing is actually lost, it's
        just not force-displayed across five lines eating the workspace.
        """
        warnings = pr.get("warnings", [])
        rotation_flags = pr.get("rotation_flags", [])

        by_candidate: dict = {}
        for rf in rotation_flags:
            by_candidate.setdefault((rf["candidate_ticker"], rf["category"]), []).append(rf["held_ticker"])
        rotation_lines = []
        for (cand, cat), held_list in by_candidate.items():
            held_str = ", ".join(held_list[:4]) + ("…" if len(held_list) > 4 else "")
            rotation_lines.append(
                tr("🔄 {held} outranked by {cand} in the same {cat} pool.").format(
                    held=held_str, cand=cand, cat=tr(cat)
                )
            )

        all_lines = warnings + rotation_lines
        if not all_lines:
            self.lbl_concentration_warning.clear()
            self.lbl_concentration_warning.setToolTip("")
            self.lbl_concentration_warning.hide()
            return

        if len(all_lines) == 1:
            summary = all_lines[0]
        else:
            summary = tr("{first}  •  +{n} more risk note(s) — hover for details").format(
                first=all_lines[0], n=len(all_lines) - 1
            )
        # Belt-and-braces against a single very long warning line still
        # overflowing the fixed-height bar on a narrower window - the full
        # text is always in the tooltip regardless, this is just what's
        # painted in the bar itself.
        if len(summary) > 160:
            summary = summary[:157].rstrip() + "…"
        self.lbl_concentration_warning.setText(summary)
        self.lbl_concentration_warning.setToolTip("\n\n".join(all_lines))
        self.lbl_concentration_warning.show()

    def populate_tables(self, buys, exits, top10, closed_trades, fin_stmt, sector_summary, breakout_watchlist=None, portfolio_risk=None, session_picks=None, _push_cloud_stats=True):
        breakout_watchlist = breakout_watchlist or []
        session_picks = session_picks or {}
        self._set_ui_controls_enabled(True)
        self.lbl_status.setText(tr("✅ Quantitative signal matrix & sector heatmaps successfully updated."))
        self.refresh_account_header(fin_stmt)
        self.update_last_data_date_display()
        self._raw_buys_data = buys

        # Cached so a language switch can re-render every table's already-
        # loaded data in the new language without re-running the matrix or
        # re-pushing cloud analytics (see _push_cloud_stats below).
        self._last_populate_args = dict(
            buys=buys, exits=exits, top10=top10, closed_trades=closed_trades,
            fin_stmt=fin_stmt, sector_summary=sector_summary,
            breakout_watchlist=breakout_watchlist, portfolio_risk=portfolio_risk,
            session_picks=session_picks,
        )

        pr = portfolio_risk or {}
        self._render_risk_banner(pr)

        if self.user_info and _push_cloud_stats:
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
            self._fill_session_picks(session_picks)

            # Columns whose *values* (not just headers) are translatable UI
            # vocabulary rather than raw numbers/tickers/dates.
            _sector_translatable = {"Sector", "Sector Status"}
            _exit_translatable = {"Action Command", "Trend Class", "Data Confidence"}

            self.tbl_sectors.setRowCount(len(sector_summary))
            for row_idx, row_data in enumerate(sector_summary):
                for col_idx, key in enumerate(["Sector", "Stocks", "1D Return (%)", "5D Return (%)", "Money Flow (CMF)", "Bullish Breadth (%)", "Traded Value (EGP)", "Sector Leader", "Sector Status"]):
                    val = row_data.get(key, "")
                    val_str = f"{val:,.2f}" if isinstance(val, float) and "Return" not in key and "Flow" not in key else str(val)
                    display_str = tr(val_str) if key in _sector_translatable else val_str
                    item = QTableWidgetItem(display_str)
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

                    # Styling checks stay against the original English value
                    # so they keep working regardless of display language.
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

            _exit_keys = [
                "Ticker", "Shares", "Buy Price", "Current Price",
                "Purchased Value (EGP)", "Current Value (EGP)", "P&L (EGP)", "P&L (%)",
                "Net P&L (EGP)", "Net P&L (%)",
                "Action Command", "Take-Profit Target", "Trailing Stop-Loss", "Distance to Stop (%)",
                "Trend Class", "RSI-14", "ADX-14", "Data Confidence", "Purchase Date", "Days Held",
                "Annualized Return (%)", "Drawdown from Peak (%)", "Risk Multiple",
                "Target Price", "Target Profit %", "Target Profit (EGP)", "Est. Days to Target",
                "Breakeven Shares Needed", "Breakeven New Avg Cost", "Breakeven Cost (EGP)",
            ]
            _n_sectors_preview = len({row_data.get("Sector") or "General / Diversified" for row_data in exits})
            _show_sector_rows = self.chk_sector_subtotals.isChecked()
            _show_combined_row = bool(exits) and bool(closed_trades)
            _extra_rows = ((_n_sectors_preview if exits else 0) if _show_sector_rows else 0) + (1 if exits else 0) + (1 if _show_combined_row else 0)
            self.tbl_exits.setRowCount(len(exits) + _extra_rows)

            # Portfolio-wide accumulators for the summary rows appended after
            # the last position — computed alongside the normal per-row fill
            # so they're always one pass, never a second one that could
            # drift from what's actually on screen.
            _tot_shares = 0.0
            _tot_purchased = 0.0
            _tot_current = 0.0
            _tot_pl_egp = 0.0
            _win_count = 0
            _loss_count = 0
            _flat_count = 0
            _best_pct = None   # (ticker, pct) — biggest % gainer
            _worst_pct = None  # (ticker, pct) — biggest % loser
            _best_egp = None   # (ticker, egp) — biggest EGP contributor to gains
            _worst_egp = None  # (ticker, egp) — biggest EGP contributor to losses
            _gross_gains_egp = 0.0
            _gross_losses_egp = 0.0
            _action_counts = {"HOLD": 0, "TAKE PROFIT": 0, "CUT LOSS": 0, "OTHER": 0}
            _days_held_sum = 0
            _days_held_n = 0
            _by_sector = {}  # sector -> dict of running totals

            today = date.today()

            for row_idx, row_data in enumerate(exits):
                shares = self._safe_float(row_data.get("Shares"))
                buy_price = self._safe_float(row_data.get("Buy Price"))
                cur_price = self._safe_float(row_data.get("Current Price"))
                purchased_val = shares * buy_price
                current_val = shares * cur_price
                pl_egp = self._safe_float(row_data.get("P&L (EGP)"), default=current_val - purchased_val)
                pl_pct = self._safe_float(row_data.get("P&L (%)"))
                ticker = str(row_data.get("Ticker", "?"))
                sector = row_data.get("Sector") or "General / Diversified"

                days_held = None
                try:
                    purchase_d = date.fromisoformat(str(row_data.get("Purchase Date"))[:10])
                    days_held = (today - purchase_d).days
                except (ValueError, TypeError):
                    pass

                _tot_shares += shares
                _tot_purchased += purchased_val
                _tot_current += current_val
                _tot_pl_egp += pl_egp
                if pl_egp > 0:
                    _win_count += 1
                    _gross_gains_egp += pl_egp
                    if _best_egp is None or pl_egp > _best_egp[1]:
                        _best_egp = (ticker, pl_egp)
                elif pl_egp < 0:
                    _loss_count += 1
                    _gross_losses_egp += abs(pl_egp)
                    if _worst_egp is None or pl_egp < _worst_egp[1]:
                        _worst_egp = (ticker, pl_egp)
                else:
                    _flat_count += 1
                if _best_pct is None or pl_pct > _best_pct[1]:
                    _best_pct = (ticker, pl_pct)
                if _worst_pct is None or pl_pct < _worst_pct[1]:
                    _worst_pct = (ticker, pl_pct)
                if days_held is not None:
                    _days_held_sum += days_held
                    _days_held_n += 1

                sec = _by_sector.setdefault(sector, {
                    "count": 0, "purchased": 0.0, "current": 0.0, "pl_egp": 0.0,
                    "days_sum": 0, "days_n": 0,
                })
                sec["count"] += 1
                sec["purchased"] += purchased_val
                sec["current"] += current_val
                sec["pl_egp"] += pl_egp
                if days_held is not None:
                    sec["days_sum"] += days_held
                    sec["days_n"] += 1

                action_val = str(row_data.get("Action Command", ""))
                if "URGENT SELL" in action_val or "CUT LOSS" in action_val:
                    _action_counts["CUT LOSS"] += 1
                elif "TAKE PROFIT" in action_val:
                    _action_counts["TAKE PROFIT"] += 1
                elif "HOLD" in action_val:
                    _action_counts["HOLD"] += 1
                else:
                    _action_counts["OTHER"] += 1

                for col_idx, key in enumerate(_exit_keys):
                    if key == "Purchased Value (EGP)":
                        val_str = f"{purchased_val:,.2f}"
                    elif key == "Current Value (EGP)":
                        val_str = f"{current_val:,.2f}"
                    elif key == "Days Held":
                        val_str = f"{days_held}d" if days_held is not None else "-"
                    elif key in ("Net P&L (EGP)",):
                        raw_val = row_data.get(key)
                        val_str = "-" if raw_val is None else f"{float(raw_val):,.2f}"
                    elif key in ("Net P&L (%)", "Distance to Stop (%)", "Annualized Return (%)", "Drawdown from Peak (%)"):
                        raw_val = row_data.get(key)
                        val_str = "-" if raw_val is None else f"{float(raw_val):,.2f}%"
                    elif key == "Risk Multiple":
                        raw_val = row_data.get(key)
                        val_str = "-" if raw_val is None else f"{float(raw_val):,.1f}x"
                    else:
                        raw_val = row_data.get(key, "")
                        val_str = "-" if raw_val is None else str(raw_val)
                    display_str = tr(val_str) if key in _exit_translatable else val_str
                    item = QTableWidgetItem(display_str)
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                    if key in ["P&L (EGP)", "P&L (%)", "Net P&L (EGP)", "Net P&L (%)", "Annualized Return (%)"]:
                        try:
                            val_num = float(row_data.get(key))
                            if val_num > 0:
                                item.setForeground(QColor("#38a169"))
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                            elif val_num < 0:
                                item.setForeground(QColor("#e53e3e"))
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                        except (TypeError, ValueError):
                            pass

                    if key == "Drawdown from Peak (%)":
                        try:
                            val_num = float(row_data.get(key))
                            if val_num <= -10.0:
                                item.setForeground(QColor("#e53e3e"))
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                        except (TypeError, ValueError):
                            pass

                    if key == "Distance to Stop (%)":
                        try:
                            val_num = float(row_data.get(key))
                            if val_num <= 3.0:
                                item.setForeground(QColor("#e53e3e"))
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                        except (TypeError, ValueError):
                            pass

                    if key == "Risk Multiple" and row_data.get("Oversized Position"):
                        item.setForeground(QColor("#e53e3e"))
                        item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                        pct = row_data.get("Position % of Equity")
                        item.setToolTip(
                            tr("This position is {p}% of your account equity — over the single-position concentration threshold.").format(p=pct)
                            if pct is not None else ""
                        )

                    if key == "Breakeven Shares Needed":
                        note = row_data.get("Breakeven Note")
                        if note:
                            item.setToolTip(note)
                        try:
                            val_num = float(row_data.get(key, 0))
                            if val_num > 0:
                                item.setForeground(QColor("#d69e2e"))
                                item.setFont(QFont("Inter", 10, QFont.Weight.Bold))
                        except (ValueError, TypeError):
                            pass

                    # Styling checks stay against the original English value.
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

            if exits:
                next_row = len(exits)
                if _show_sector_rows:
                    next_row = self._fill_exits_sector_rows(
                        row_idx_start=len(exits), keys=_exit_keys,
                        by_sector=_by_sector, total_current=_tot_current,
                    )
                next_row = self._fill_exits_totals_row(
                    row_idx=next_row,
                    keys=_exit_keys,
                    total_shares=_tot_shares,
                    total_purchased=_tot_purchased,
                    total_current=_tot_current,
                    total_pl_egp=_tot_pl_egp,
                    win_count=_win_count,
                    loss_count=_loss_count,
                    flat_count=_flat_count,
                    best_pct=_best_pct,
                    worst_pct=_worst_pct,
                    best_egp=_best_egp,
                    worst_egp=_worst_egp,
                    gross_gains_egp=_gross_gains_egp,
                    gross_losses_egp=_gross_losses_egp,
                    action_counts=_action_counts,
                    avg_days_held=(_days_held_sum / _days_held_n) if _days_held_n else None,
                )
                if _show_combined_row:
                    self._fill_exits_combined_row(
                        row_idx=next_row, keys=_exit_keys,
                        unrealized_pl_egp=_tot_pl_egp, unrealized_cost=_tot_purchased,
                        closed_trades=closed_trades,
                    )

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
                item_name = QTableWidgetItem(tr(metric_name))
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

            _breakout_translatable = {"Squeeze Active", "Volume Trend", "Trend Class", "Signals"}

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
                    display_str = tr(val_str) if key in _breakout_translatable else val_str
                    item = QTableWidgetItem(display_str)
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

        # Guarded by _push_cloud_stats (False on a language-switch replay of
        # cached results) so this popup only fires for a genuinely fresh
        # matrix run, never re-shown just from toggling EN/AR.
        achieved_today = session_picks.get("achieved_today") or []
        if achieved_today and _push_cloud_stats:
            lines = "\n".join(
                f"  • {p['ticker']}  (+{p['achieved_pct']:.2f}%, picked {p['pick_date']} @ {p['ref_price']:.4f})"
                for p in achieved_today
            )
            QMessageBox.information(
                self, tr("🎯 Session Pick Achieved!"),
                tr("{n} pick(s) just crossed +3% from their pick price:\n\n{lines}\n\nSee the Session Picks tab for details.").format(
                    n=len(achieved_today), lines=lines,
                ),
            )

    def _fill_matrix_table(self, table_view, data_list):
        model = table_view.model()
        if hasattr(model, "update_data"):
            model.update_data(data_list)

    def apply_screener_preset(self, preset_id: str):
        # Single-select, toggle-off-on-repeat-click - identical behavior to
        # the web dashboard's applyScreenerPreset().
        self.active_screener_preset = None if self.active_screener_preset == preset_id else preset_id
        for pid, btn in self._preset_buttons.items():
            btn.blockSignals(True)
            btn.setChecked(pid == self.active_screener_preset)
            btn.blockSignals(False)
        self.apply_filters()

    def apply_filters(self):
        search_text = self.txt_search.text().strip().upper()
        # Match against the underlying English value (by index), not the
        # displayed/translated text, so filtering still works correctly
        # when the UI is in Arabic — row data (Action/Trend/Confidence) is
        # always stored/compared in English regardless of display language.
        action_filter = self._action_filter_items[self.cmb_action.currentIndex()]
        trend_filter = self._trend_filter_items[self.cmb_trend.currentIndex()]
        confidence_filter = self._confidence_filter_items[self.cmb_confidence.currentIndex()]
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
                match_preset = True
                if self.active_screener_preset:
                    preset = next((p for p in self.SCREENER_PRESETS if p["id"] == self.active_screener_preset), None)
                    if preset:
                        try:
                            match_preset = bool(preset["predicate"](row))
                        except Exception:
                            match_preset = False

                if match_search and match_action and match_trend and match_confidence and match_liquidity and match_preset:
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

        # Establish the shared DuckDB connection (and run its one-time
        # schema init) BEFORE building QuantDashboard, via a small dialog
        # that shows visible retry progress if quant_master.duckdb is
        # already open in another running instance of this app - instead
        # of QuantDashboard.__init__'s own `DatabaseManager()` call
        # silently blocking the main window from ever appearing. See the
        # DBConnectWorker/DBConnectDialog docstring above.
        connect_dialog = DBConnectDialog()
        connect_dialog.exec()
        if not connect_dialog.result_ok:
            if connect_dialog.error_message:
                QMessageBox.critical(
                    None,
                    "MB-EGX — Can't Connect to Database",
                    connect_dialog.error_message,
                )
            sys.exit(1)

        window = QuantDashboard(user_info=login.user_info)
        window.show()
        sys.exit(app.exec())
    except SystemExit:
        raise
    except DatabaseLockedError as e:
        # Belt-and-suspenders: covers the (normally unreachable, since the
        # connect dialog above already handles the startup path) case of
        # a lock surfacing later, e.g. from a dialog that opens its own
        # DatabaseManager() such as PortfolioDialog. Same friendly,
        # specific message as the startup dialog rather than falling
        # through to the generic fatal-error traceback box below.
        logger.error(f"Database locked: {e}")
        _show_fatal_error("MB-EGX — Database Locked", str(e))
        sys.exit(1)
    except Exception:
        tb_text = traceback.format_exc()
        logger.error(f"Fatal startup error:\n{tb_text}")
        _show_fatal_error(
            "MB-EGX — Failed to Start",
            f"{tb_text[-1200:]}\n\nFull details were written to quant_app.log."
        )
        sys.exit(1)
