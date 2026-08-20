import numpy as np
import pandas as pd
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLabel, 
    QRadioButton, QButtonGroup, QPushButton, QScrollArea, QFrame, QFileDialog,
    QDialog
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates

# Arabic display names for the canonical sector strings db_manager.py's
# clean_sector_name() produces. Kept local (rather than importing from
# app_gui.py) to avoid a circular import — app_gui.py imports this module.
# Any sector not listed here (e.g. a name clean_sector_name() didn't
# recognize and passed through unchanged) just falls back to itself.
_AR_SECTOR_NAMES = {
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
}

# Overlay color per detected-pattern direction (matches the existing
# up/down palette used for candles and trend lines elsewhere in this file).
_PATTERN_COLORS = {"bullish": "#22c55e", "bearish": "#ef4444", "neutral": "#a0aec0"}


class _ChartFullscreenDialog(QDialog):
    """Fullscreen host window for the chart canvas, opened on demand by
    StockSectorChartWidget._toggle_maximize_chart(). Qt widgets only ever
    have one parent, so reparenting ``canvas`` (and ``controls``) into
    this dialog's layout automatically removes them from the small
    embedded view — and reparenting them back (done by the ``on_close``
    callback) is all that's needed to restore the normal default-size
    layout, no separate "shrink" codepath required.

    ``controls`` are the same Line/Candle/Support-Resistance/Patterns/
    Volume/Save-Chart widgets the embedded toolbar normally shows —
    moved up here into the dialog's own top bar so every one of them
    stays usable while maximized, instead of only the bare chart.

    Closing works three ways, all routed through closeEvent() below so
    ``on_close`` always fires exactly once: the ✕ button, the Esc key, or
    the OS window-close control (still available since this is a normal
    QDialog, not a frameless one).
    """
    def __init__(self, canvas, controls, on_close, lang="EN", parent=None):
        super().__init__(parent)
        self.setWindowTitle("MB-EGX — Chart")
        self.setStyleSheet("background-color: #1a1d24;")
        self._on_close = on_close
        self._closed_cleanly = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        top_bar = QHBoxLayout()
        hint = QLabel("Esc")
        hint.setStyleSheet("color: #64748b; font-size: 11px;")
        top_bar.addWidget(hint)
        for w in controls:
            top_bar.addWidget(w)
        top_bar.addStretch()
        self.btn_close = QPushButton("✕ Close" if lang != "AR" else "✕ إغلاق")
        self.btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_close.setStyleSheet(
            "QPushButton { background-color: #2d3748; color: #e2e8f0; border-radius: 4px; "
            "border: none; padding: 6px 16px; font-size: 12px; font-weight: bold; }"
            "QPushButton:hover { background-color: #dc2626; color: white; }"
        )
        self.btn_close.clicked.connect(self.close)
        top_bar.addWidget(self.btn_close)
        outer.addLayout(top_bar)

        outer.addWidget(canvas, stretch=1)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        # Guards against firing on_close twice — e.g. the ✕ button calls
        # close(), which then also triggers this same closeEvent.
        if not self._closed_cleanly:
            self._closed_cleanly = True
            self._on_close()
        super().closeEvent(event)


class StockSectorChartWidget(QWidget):
    def __init__(self, qe, dbm, parent=None):
        super().__init__(parent)
        self.qe = qe
        self.dbm = dbm
        self.lang = "EN"
        self.current_letter_filter = "ALL"
        # PERF: get_sector_historical_index() used to call
        # get_all_market_data_bulk() (a full-table fetch + per-ticker
        # cleaning pass over the ENTIRE market) and dbm.get_sector_map()
        # (an O(n*m) fuzzy token-match) on every single sector dropdown
        # selection. Cache both for the widget's lifetime; refresh_data()
        # lets the host window invalidate them after re-ingestion.
        self._bulk_cache = None
        self._sector_map_cache = None
        self._init_ui()

    def _tr_sector(self, name: str) -> str:
        """Translate a canonical sector name for display when Arabic is
        active; returns it unchanged in English mode or if unrecognized."""
        if self.lang == "AR":
            return _AR_SECTOR_NAMES.get(name, name)
        return name

    def refresh_data(self):
        """Call after new market data has been ingested to drop caches."""
        self._bulk_cache = None
        self._sector_map_cache = None
        self.populate_selector()

    def _get_bulk_data_cached(self):
        if self._bulk_cache is None:
            self._bulk_cache = self.qe.get_all_market_data_bulk()
        return self._bulk_cache

    def _get_sector_map_cached(self):
        if self._sector_map_cache is None:
            self._sector_map_cache = self.dbm.get_sector_map()
        return self._sector_map_cache

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 1. Top Control Bar (Mode & Dropdown)
        ctrl_layout = QHBoxLayout()
        self.grp_mode = QButtonGroup(self)
        self.rad_stock = QRadioButton("📈 Per Stock")
        self.rad_sector = QRadioButton("🏢 Per Sector")
        self.rad_stock.setChecked(True)
        self.grp_mode.addButton(self.rad_stock, 1)
        self.grp_mode.addButton(self.rad_sector, 2)
        
        ctrl_layout.addWidget(self.rad_stock)
        ctrl_layout.addWidget(self.rad_sector)

        self.lbl_select = QLabel("Select Item:")
        self.lbl_select.setStyleSheet("font-weight: bold;")
        self.cmb_selector = QComboBox()
        self.cmb_selector.setMinimumWidth(280)
        self.cmb_selector.setStyleSheet("padding: 4px; font-weight: bold;")
        
        ctrl_layout.addWidget(self.lbl_select)
        ctrl_layout.addWidget(self.cmb_selector, stretch=1)
        layout.addLayout(ctrl_layout)

        # 1b. Chart-style row (Line vs Candles) + Support/Resistance toggle.
        # Candles need real OHLC bars, which only the per-stock series has
        # (the sector index is a synthetic equal-weight average, so it stays
        # line-only); the toggle is disabled automatically in sector mode.
        style_layout = QHBoxLayout()
        self.grp_style = QButtonGroup(self)
        self.rad_line = QRadioButton("📉 Line")
        self.rad_candle = QRadioButton("🕯️ Candles")
        self.rad_line.setChecked(True)
        self.grp_style.addButton(self.rad_line, 1)
        self.grp_style.addButton(self.rad_candle, 2)
        self.chk_sr = QPushButton("📐 Support / Resistance")
        self.chk_sr.setCheckable(True)
        self.chk_sr.setChecked(True)
        self.chk_sr.setStyleSheet(
            "QPushButton { background-color: #2d3748; color: #cbd5e0; border-radius: 4px; border: none; padding: 4px 10px; font-size: 11px; font-weight: bold; }"
            "QPushButton:checked { background-color: #3182ce; color: #ffffff; }"
        )
        # Geometric pattern overlay (chart_patterns.PatternDetector) - off by
        # default so existing chart behavior/screenshots don't change until
        # someone opts in.
        self.chk_patterns = QPushButton("🔺 Patterns")
        self.chk_patterns.setCheckable(True)
        self.chk_patterns.setChecked(False)
        self.chk_patterns.setStyleSheet(
            "QPushButton { background-color: #2d3748; color: #cbd5e0; border-radius: 4px; border: none; padding: 4px 10px; font-size: 11px; font-weight: bold; }"
            "QPushButton:checked { background-color: #7c3aed; color: #ffffff; }"
        )
        self.chk_volume = QPushButton("📊 Volume")
        self.chk_volume.setCheckable(True)
        self.chk_volume.setChecked(True)
        self.chk_volume.setStyleSheet(
            "QPushButton { background-color: #2d3748; color: #cbd5e0; border-radius: 4px; border: none; padding: 4px 10px; font-size: 11px; font-weight: bold; }"
            "QPushButton:checked { background-color: #0891b2; color: #ffffff; }"
        )
        self.btn_save_chart = QPushButton("💾 Save Chart")
        self.btn_save_chart.setStyleSheet("QPushButton { background-color: #0f766e; color: white; border-radius: 4px; border: none; padding: 4px 10px; font-size: 11px; font-weight: bold; }")
        self.btn_maximize_chart = QPushButton("⛶ Maximize")
        self.btn_maximize_chart.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_maximize_chart.setStyleSheet(
            "QPushButton { background-color: #4338ca; color: white; border-radius: 4px; border: none; padding: 4px 10px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background-color: #4f46e5; }"
        )
        style_layout.addWidget(self.rad_line)
        style_layout.addWidget(self.rad_candle)
        style_layout.addWidget(self.chk_sr)
        style_layout.addWidget(self.chk_patterns)
        style_layout.addWidget(self.chk_volume)
        style_layout.addWidget(self.btn_save_chart)
        style_layout.addWidget(self.btn_maximize_chart)
        style_layout.addStretch()
        layout.addLayout(style_layout)
        self._style_layout = style_layout  # kept for _toggle_maximize_chart's reparent-out/back

        # 1c. Time-horizon zoom (parity port of the web dashboard's
        # timeframe-buttons bar — index.html's applyTimeFrame). Slices the
        # plotted window to the last N bars; 'ALL' plots everything.
        zoom_row = QHBoxLayout()
        self.lbl_time_zoom = QLabel("⏱️ Time Horizon:")
        self.lbl_time_zoom.setStyleSheet("font-weight: bold; color: #38bdf8; font-size: 11px;")
        zoom_row.addWidget(self.lbl_time_zoom)
        self._timeframe_buttons = {}
        self._timeframe_limit = 0
        for tf, bars in (("1M", 22), ("3M", 66), ("6M", 132), ("1Y", 252), ("ALL", 0)):
            btn = QPushButton(tf)
            btn.setCheckable(True)
            btn.setChecked(bars == 0)
            btn.setStyleSheet(
                "QPushButton { background-color: #1e293b; color: #cbd5e0; padding: 3px 12px; "
                "font-size: 11px; font-weight: bold; border-radius: 12px; border: 1px solid #334155; }"
                "QPushButton:checked { background-color: #0284c7; color: white; border: 1px solid #38bdf8; }"
            )
            btn.clicked.connect(lambda _, b=bars, btb=btn: self._select_timeframe(b, btb))
            zoom_row.addWidget(btn)
            self._timeframe_buttons[tf] = btn
        zoom_row.addStretch()
        layout.addLayout(zoom_row)

        # 2. Alphabet Quick-Filter Bar
        self.alpha_container = QFrame()
        self.alpha_container.setStyleSheet("QFrame { background-color: #222730; border-radius: 4px; padding: 2px; }")
        alpha_layout = QHBoxLayout(self.alpha_container)
        alpha_layout.setContentsMargins(4, 4, 4, 4)
        alpha_layout.setSpacing(2)
        
        self.lbl_filter_text = QLabel("🔤 Quick Filter:")
        self.lbl_filter_text.setStyleSheet("color: #a0aec0; font-size: 11px; font-weight: bold; padding-right: 4px;")
        alpha_layout.addWidget(self.lbl_filter_text)

        self.alpha_buttons = {}
        letters = ["ALL"] + [chr(i) for i in range(65, 91)] + ["#"]
        for letter in letters:
            btn = QPushButton(letter)
            btn.setFixedSize(28, 24)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(self._get_alpha_btn_style(letter == "ALL"))
            btn.clicked.connect(lambda ch, l=letter: self.apply_letter_filter(l))
            alpha_layout.addWidget(btn)
            self.alpha_buttons[letter] = btn

        alpha_layout.addStretch()
        layout.addWidget(self.alpha_container)

        # 3. Matplotlib Canvas
        self.fig = Figure(figsize=(10, 5), facecolor='#1a1d24')
        self.canvas = FigureCanvas(self.fig)
        layout.addWidget(self.canvas)

        # Signals
        self.rad_stock.toggled.connect(self.on_mode_changed)
        self.rad_sector.toggled.connect(self.on_mode_changed)
        self.rad_line.toggled.connect(self.plot_chart)
        self.rad_candle.toggled.connect(self.plot_chart)
        self.chk_sr.toggled.connect(self.plot_chart)
        self.chk_patterns.toggled.connect(self.plot_chart)
        self.chk_volume.toggled.connect(self.plot_chart)
        self.btn_save_chart.clicked.connect(self._save_chart)
        self.btn_maximize_chart.clicked.connect(self._toggle_maximize_chart)
        self.cmb_selector.currentIndexChanged.connect(self.plot_chart)

        self._fullscreen_dialog = None
        self.populate_selector()

    def _plot_candles(self, ax, df):
        """Manual OHLC candlestick renderer (no mplfinance dependency).

        Draws a high-low wick plus an open-close body per bar, colored by
        direction. Uses numeric date positions (mdates.date2num) so bar
        width scales sensibly regardless of the date range plotted.
        """
        import matplotlib.dates as _mdates
        from matplotlib.patches import Rectangle
        from matplotlib.lines import Line2D

        x = _mdates.date2num(df.index.to_pydatetime())
        if len(x) > 1:
            spacing = np.median(np.diff(x))
        else:
            spacing = 1.0
        width = max(spacing * 0.6, 0.3)

        up_color, down_color = "#22c55e", "#ef4444"
        for xi, (_, row) in zip(x, df.iterrows()):
            o, h, l, c = row["open"], row["high"], row["low"], row["close"]
            if pd.isna(o) or pd.isna(h) or pd.isna(l) or pd.isna(c):
                continue
            color = up_color if c >= o else down_color
            ax.add_line(Line2D([xi, xi], [l, h], color=color, linewidth=0.9, zorder=2))
            body_low, body_high = min(o, c), max(o, c)
            height = max(body_high - body_low, (h - l) * 0.02 + 1e-6)
            ax.add_patch(Rectangle(
                (xi - width / 2, body_low), width, height,
                facecolor=color, edgecolor=color, linewidth=0.5, zorder=3,
            ))
        ax.xaxis_date()

    def _plot_patterns(self, ax, df):
        """Overlay detected geometric chart patterns (chart_patterns.py) on
        the current stock-mode axes: a faint shaded span over the pattern's
        date range, dashed lines for its key price levels (neckline,
        support/resistance, target, ...), and a name+quality label at the
        end of the pattern.
        """
        from chart_patterns import PatternDetector
        from config import PATTERN_DETECTION

        if len(df) < PATTERN_DETECTION["min_bars_required"]:
            return
        try:
            patterns = PatternDetector(
                df,
                epsilon=PATTERN_DETECTION["epsilon"],
                order=PATTERN_DETECTION["order"],
            ).detect_all()
        except Exception:
            # A bad fit on one ticker's history should never take the whole
            # chart down - same "skip and continue" posture used elsewhere
            # in this codebase for per-ticker failures.
            return

        min_quality = PATTERN_DETECTION["min_quality"]
        # Multiple detected patterns often end within a few bars of each
        # other (most commonly right at the most recent bar, since that's
        # where "still forming" patterns naturally converge) — with every
        # label using the same fixed (4, 10) point offset, those labels
        # rendered exactly on top of one another. Bucketing by end_index
        # (grouped into small ranges rather than requiring an exact match,
        # since "close" bars should stack too) and giving each subsequent
        # label in the same bucket a larger vertical offset staggers them
        # into a readable stack instead.
        label_bucket_counts = {}
        for p in patterns:
            if p.get("quality", 1.0) < min_quality:
                continue
            color = _PATTERN_COLORS.get(p["direction"], "#a0aec0")
            start_i = max(0, min(p["start_index"], len(df) - 1))
            end_i = max(0, min(p["end_index"], len(df) - 1))
            start_date, end_date = df.index[start_i], df.index[end_i]

            ax.axvspan(start_date, end_date, color=color, alpha=0.06, zorder=0)

            for key, val in p["levels"].items():
                if "index" in key or not isinstance(val, (int, float)):
                    continue  # e.g. apex_index is an x-position, not a price level
                ax.hlines(
                    y=val, xmin=start_date, xmax=end_date, color=color,
                    linestyle=(0, (3, 2)), linewidth=1.1, alpha=0.65, zorder=2,
                )

            label = p["pattern"]
            if "quality" in p:
                label += f" ({p['quality']:.0%})"
            bucket_key = end_i // 5
            slot = label_bucket_counts.get(bucket_key, 0)
            label_bucket_counts[bucket_key] = slot + 1
            ax.annotate(
                label, xy=(end_date, float(df["close"].iloc[end_i])),
                xytext=(4, 10 + slot * 15), textcoords="offset points",
                color=color, fontsize=7.5, fontweight="bold", zorder=6,
                bbox=dict(boxstyle="round,pad=0.2", facecolor="#1a1d24", edgecolor=color, alpha=0.85),
            )

    def _get_alpha_btn_style(self, is_active=False):
        if is_active:
            return "QPushButton { background-color: #3182ce; color: #ffffff; font-weight: bold; border-radius: 3px; border: none; font-size: 11px; }"
        return "QPushButton { background-color: #2d3748; color: #cbd5e0; border-radius: 3px; border: none; font-size: 11px; } QPushButton:hover { background-color: #4a5568; color: white; }"

    def set_language(self, lang: str):
        self.lang = lang
        if lang == "AR":
            self.rad_stock.setText("📈 تحليل الأسهم")
            self.rad_sector.setText("🏢 تحليل القطاعات")
            self.lbl_select.setText("اختر العنصر:")
            self.lbl_filter_text.setText("🔤 تصفية بالحرف:")
            self.alpha_buttons["ALL"].setText("الكل")
            self.rad_line.setText("📉 خط")
            self.rad_candle.setText("🕯️ شموع")
            self.chk_sr.setText("📐 الدعم / المقاومة")
            self.chk_volume.setText("📊 الحجم")
            self.btn_save_chart.setText("💾 حفظ الرسم")
            self.btn_maximize_chart.setText("⛶ تكبير")
            if hasattr(self, "lbl_time_zoom"):
                self.lbl_time_zoom.setText("⏱️ الأفق الزمني:")
        else:
            self.rad_stock.setText("📈 Per Stock")
            self.rad_sector.setText("🏢 Per Sector")
            self.lbl_select.setText("Select Item:")
            self.lbl_filter_text.setText("🔤 Quick Filter:")
            self.alpha_buttons["ALL"].setText("ALL")
            self.rad_line.setText("📉 Line")
            self.rad_candle.setText("🕯️ Candles")
            self.chk_sr.setText("📐 Support / Resistance")
            self.chk_volume.setText("📊 Volume")
            self.btn_save_chart.setText("💾 Save Chart")
            self.btn_maximize_chart.setText("⛶ Maximize")
            if hasattr(self, "lbl_time_zoom"):
                self.lbl_time_zoom.setText("⏱️ Time Horizon:")

        if not self.rad_stock.isChecked():
            # Sector-mode dropdown items are translated display text with the
            # raw English sector name as itemData — repopulate so the shown
            # labels switch language too, then restore the same selection.
            prev_selected = self.cmb_selector.currentData() or self.cmb_selector.currentText().strip()
            self.populate_selector()
            if prev_selected:
                idx = self.cmb_selector.findData(prev_selected)
                if idx >= 0:
                    self.cmb_selector.setCurrentIndex(idx)
        else:
            self.plot_chart()

    def on_mode_changed(self):
        # Hide alphabet bar if in sector mode
        is_stock_mode = self.rad_stock.isChecked()
        self.alpha_container.setVisible(is_stock_mode)
        self.rad_candle.setEnabled(is_stock_mode)
        if not is_stock_mode and self.rad_candle.isChecked():
            self.rad_line.setChecked(True)
        self.apply_letter_filter("ALL")

    def apply_letter_filter(self, letter: str):
        self.current_letter_filter = letter
        for l, btn in self.alpha_buttons.items():
            btn.setStyleSheet(self._get_alpha_btn_style(l == letter))
        self.populate_selector()

    def _select_timeframe(self, bars: int, btn):
        self._timeframe_limit = bars
        for b in self._timeframe_buttons.values():
            if b is not btn:
                b.setChecked(False)
        self.plot_chart()

    def populate_selector(self):
        self.cmb_selector.blockSignals(True)
        self.cmb_selector.clear()
        
        if self.rad_stock.isChecked():
            tickers = self.dbm.get_unique_tickers()
            if self.current_letter_filter != "ALL":
                if self.current_letter_filter == "#":
                    tickers = [t for t in tickers if not t[0].isalpha()]
                else:
                    tickers = [t for t in tickers if t.upper().startswith(self.current_letter_filter)]
            self.cmb_selector.addItems(tickers)
        else:
            sector_map = self._get_sector_map_cached()
            unique_sectors = sorted(list(set(sector_map.values())))
            for sec in unique_sectors:
                self.cmb_selector.addItem(self._tr_sector(sec), sec)
            
        self.cmb_selector.blockSignals(False)
        self.plot_chart()

    def _save_chart(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Chart" if self.lang == "EN" else "حفظ الرسم", "mb_egx_chart.png", "PNG Files (*.png)")
        if file_path:
            self.fig.savefig(file_path, dpi=180, bbox_inches="tight")

    def _toggle_maximize_chart(self):
        """Pops the chart canvas — plus its Line/Candle/Support-Resistance/
        Patterns/Volume/Save-Chart controls — out into a fullscreen window
        (see _ChartFullscreenDialog above), or is a no-op if it's already
        maximized: the dialog owns those widgets until it closes, so a
        second click while it's open just does nothing rather than
        opening a second dialog around the same widgets."""
        if self._fullscreen_dialog is not None:
            return
        controls = [self.rad_line, self.rad_candle, self.chk_sr, self.chk_patterns,
                    self.chk_volume, self.btn_save_chart]
        dlg = _ChartFullscreenDialog(self.canvas, controls, on_close=self._on_chart_fullscreen_closed,
                                      lang=self.lang, parent=self.window())
        self._fullscreen_dialog = dlg
        dlg.showMaximized()

    def _on_chart_fullscreen_closed(self):
        """Reparents the canvas and the moved-out controls back into this
        widget's own layout — Qt widgets only have one parent at a time,
        so this is also what removes them from the closing dialog. The
        controls are re-inserted at index 0..N in their original left-to-
        right order, ahead of the Maximize button + trailing stretch that
        stayed behind in the embedded row, which restores the exact
        original ordering. The default-size chart's own resize/redraw
        happens automatically once it's back (Qt fires this via the
        canvas's normal resizeEvent once it's laid out here again, no
        manual call needed)."""
        self.layout().addWidget(self.canvas)
        controls = [self.rad_line, self.rad_candle, self.chk_sr, self.chk_patterns,
                    self.chk_volume, self.btn_save_chart]
        for i, w in enumerate(controls):
            self._style_layout.insertWidget(i, w)
        self._fullscreen_dialog = None

    def plot_chart(self):
        is_stock_mode = self.rad_stock.isChecked()
        if is_stock_mode:
            selected = self.cmb_selector.currentText().strip()
            selected_display = selected
        else:
            # itemData holds the raw English sector name (used for data
            # lookups); the combo's displayed text may be translated.
            selected = self.cmb_selector.currentData()
            selected_display = self.cmb_selector.currentText().strip()
            if not selected:
                selected = selected_display
        self.fig.clear()
        
        if not selected:
            self.canvas.draw()
            return

        ax = self.fig.add_subplot(111)
        ax.set_facecolor('#111318')
        ax.tick_params(colors='#e2e8f0', labelsize=9)
        for spine in ax.spines.values():
            spine.set_color('#2d3748')
        ax.grid(True, linestyle='--', alpha=0.3, color='#4a5568')

        latest_date_str = self.dbm.get_latest_market_date()

        if self.rad_stock.isChecked():
            df = self.qe.get_ticker_data(selected)
            if df.empty:
                self.canvas.draw()
                return
                
            df = self.qe.compute_indicators(df)
            if self._timeframe_limit and len(df) > self._timeframe_limit:
                df = df.iloc[-self._timeframe_limit:]
            dates = df.index
            prices = df['close']
            trend_vals, slope_pct = self.qe.compute_trendline(prices)

            if self.rad_candle.isChecked() and {'open', 'high', 'low'}.issubset(df.columns):
                self._plot_candles(ax, df)
            else:
                ax.plot(dates, prices, label="Close Price" if self.lang == "EN" else "سعر الإغلاق", color="#38bdf8", linewidth=1.8)
            if 'vwap_20' in df.columns:
                vwap_label = "VWAP (20D)" if self.lang == "EN" else "متوسط السعر المرجح (20 يوم)"
                ax.plot(dates, df['vwap_20'], label=vwap_label, color="#f59e0b", linestyle=":", alpha=0.8)

            if self.chk_sr.isChecked() and {'high', 'low', 'close'}.issubset(df.columns):
                pivots = self.qe.compute_pivot_points(df)
                if pivots:
                    # When the six SR levels cluster tightly (e.g. a high-priced
                    # ticker whose pivot range collapses into a narrow band near
                    # the current close), the dotted axhlines visually pile on
                    # top of each other and their right-edge text labels print
                    # in the same row, producing the unreadable "block of
                    # overlapping numbers" seen in screenshots. Sort all levels
                    # and stack each label by a small vertical step (in points)
                    # so even tightly clustered SR stays individually legibl
                    # while the lines themselves still sit exactly where the
                    # pivots say they should.
                    sr_tags = (
                        ("r3", "#ef4444", "bottom"),
                        ("r2", "#ef4444", "bottom"),
                        ("r1", "#ef4444", "bottom"),
                        ("s1", "#22c55e", "top"),
                        ("s2", "#22c55e", "top"),
                        ("s3", "#22c55e", "top"),
                    )
                    plotted = [(tag, pivots[tag]) for tag, _, _ in sr_tags if pivots.get(tag) is not None]
                    plotted.sort(key=lambda t: t[1])
                    # Tight-cluster threshold in price units: if any two SR
                    # levels are closer than 1.5% of the visible price range
                    # apart, push labels apart in point space.
                    if prices is not None and len(prices) > 1:
                        span = float(prices.max() - prices.min()) or max(float(prices.max()) * 0.01, 1e-6)
                        cluster_step_pts = 11 if span > 0 and (max(p[1] for p in plotted) - min(p[1] for p in plotted)) < span * 0.06 else 0
                    else:
                        cluster_step_pts = 0
                    label_offsets = {}
                    for i, (tag, _) in enumerate(plotted):
                        label_offsets[tag] = i * cluster_step_pts
                    for tag, color, va in sr_tags:
                        lvl = pivots[tag]
                        weight = 1.4 if tag in ("r1", "s1") else 0.9
                        alpha = 0.85 if tag in ("r1", "s1") else 0.5
                        ax.axhline(y=lvl, color=color, linestyle=(0, (4, 3)), linewidth=weight, alpha=alpha, zorder=1)
                        # Up-shift "top" labels (S*) and down-shift "bottom"
                        # labels (R*) so even if multiple series collide at
                        # the same y, the SRS1..3 / RSR1..3 text strings stack
                        # instead of overprinting.
                        y_text_offset = label_offsets.get(tag, 0)
                        if va == "top":
                            y_text_offset = -y_text_offset
                        ax.annotate(
                            f" {tag.upper()} {lvl:.4g}",
                            xy=(dates[-1], lvl),
                            xytext=(4, y_text_offset),
                            textcoords="offset points",
                            color=color,
                            fontsize=7.5,
                            va=va,
                            ha="left",
                            zorder=4,
                            bbox=dict(boxstyle="round,pad=0.15", facecolor="#111318", edgecolor=color, alpha=0.85, linewidth=0.5),
                        )

            if self.chk_patterns.isChecked():
                self._plot_patterns(ax, df)

            if self.chk_volume.isChecked() and "volume" in df.columns:
                axv = ax.twinx()
                up_mask = df["close"] >= df["open"]
                colors = np.where(up_mask, "#22c55e", "#ef4444")
                axv.bar(dates, df["volume"], width=0.8, color=colors, alpha=0.16, label="Volume")
                axv.set_yticks([])
                for spine in axv.spines.values():
                    spine.set_visible(False)

            trend_color = "#22c55e" if slope_pct >= 0 else "#ef4444"
            trend_label = f"Trendline ({'+' if slope_pct >= 0 else ''}{slope_pct}%)"
            if self.lang == "AR":
                trend_label = f"خط الاتجاه ({'+' if slope_pct >= 0 else ''}{slope_pct}%)"
                
            ax.plot(dates, trend_vals, label=trend_label, color=trend_color, linestyle="--", linewidth=2.0)
            title = f"Stock: {selected} — Trend Analysis" if self.lang == "EN" else f"سهم: {selected} — تحليل الاتجاه"
            # Auto-shrink the title when longer ticker names + the trailing
            # "Trend Analysis" label would otherwise clip against the figure
            # edge. Empirically a ~12pt bold title breaks ~around 36 chars on
            # a 10in-wide figure; scale down from there, never below 9pt so
            # it stays readable.
            title_fontsize = max(9, 12 - max(0, (len(title) - 30) // 4))
            ax.set_title(title, color="#ffffff", fontsize=title_fontsize, fontweight="bold", pad=12, loc="center")

        else:
            sector_map = self._get_sector_map_cached()
            df_sec = self.qe.get_sector_historical_index(
                selected, sector_map, bulk_data=self._get_bulk_data_cached()
            )
            if df_sec.empty:
                self.canvas.draw()
                return
            if self._timeframe_limit and len(df_sec) > self._timeframe_limit:
                df_sec = df_sec.iloc[-self._timeframe_limit:]

            dates = df_sec.index
            idx_vals = df_sec['sector_index']
            trend_vals, slope_pct = self.qe.compute_trendline(idx_vals)
            
            ax.plot(dates, idx_vals, label="Sector Index (Base 100)" if self.lang == "EN" else "مؤشر القطاع", color="#a855f7", linewidth=2.0)
            trend_color = "#22c55e" if slope_pct >= 0 else "#ef4444"
            trend_label = f"Sector Macro Trend ({'+' if slope_pct >= 0 else ''}{slope_pct}%)"
            if self.lang == "AR":
                trend_label = f"اتجاه القطاع العام ({'+' if slope_pct >= 0 else ''}{slope_pct}%)"
                
            ax.plot(dates, trend_vals, label=trend_label, color=trend_color, linestyle="--", linewidth=2.0)
            title = f"Sector: {selected_display} Index" if self.lang == "EN" else f"قطاع: {selected_display}"
            title_fontsize = max(9, 12 - max(0, (len(title) - 30) // 4))
            ax.set_title(title, color="#ffffff", fontsize=title_fontsize, fontweight="bold", pad=12, loc="center")

        if len(dates) > 0:
            last_date = dates[-1]
            last_price = float(prices.iloc[-1]) if self.rad_stock.isChecked() else float(idx_vals.iloc[-1])
            last_date_lbl = f"Last Data: {latest_date_str}" if self.lang == "EN" else f"آخر بيانات: {latest_date_str}"
            ax.axvline(x=last_date, color="#e11d48", linestyle="-.", alpha=0.85, linewidth=1.5, label=last_date_lbl)
            # Explicit last-price callout on the final session, requested
            # separately from the "Last Data" date marker above.
            ax.annotate(
                f"{last_price:.4g}",
                xy=(last_date, last_price), xytext=(8, 0), textcoords="offset points",
                color="#0b0f14", fontsize=9, fontweight="bold", va="center", zorder=5,
                bbox=dict(boxstyle="round,pad=0.25", facecolor="#e11d48", edgecolor="none"),
            )

        # BUGFIX: candlesticks are drawn via ax.add_line()/ax.add_patch() with
        # raw mdates.date2num() coordinates, which never goes through
        # matplotlib's automatic datetime-locator selection the way
        # ax.plot(datetime_index, ...) does. Left to its own devices the
        # default numeric locator then places ticks evenly by index and the
        # '%Y-%m' formatter stamps each with only a month - producing the
        # "2026-07" label repeated 6+ times seen in candle mode. An explicit
        # date-aware locator/formatter fixes this for both line and candle
        # modes and shows the actual date, not just a repeated month.
        locator = mdates.AutoDateLocator(minticks=5, maxticks=9)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        self.fig.autofmt_xdate(rotation=0, ha="center")
        ax.legend(facecolor='#1a1d24', edgecolor='#4a5568', labelcolor='#ffffff', loc='upper left')
        self.fig.tight_layout()
        self.canvas.draw()
