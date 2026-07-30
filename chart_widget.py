import numpy as np
import pandas as pd
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QLabel, 
    QRadioButton, QButtonGroup, QPushButton, QScrollArea, QFrame
)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates

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
        self.cmb_selector.currentIndexChanged.connect(self.plot_chart)

        self.populate_selector()

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
        else:
            self.rad_stock.setText("📈 Per Stock")
            self.rad_sector.setText("🏢 Per Sector")
            self.lbl_select.setText("Select Item:")
            self.lbl_filter_text.setText("🔤 Quick Filter:")
            self.alpha_buttons["ALL"].setText("ALL")
        self.plot_chart()

    def on_mode_changed(self):
        # Hide alphabet bar if in sector mode
        is_stock_mode = self.rad_stock.isChecked()
        self.alpha_container.setVisible(is_stock_mode)
        self.apply_letter_filter("ALL")

    def apply_letter_filter(self, letter: str):
        self.current_letter_filter = letter
        for l, btn in self.alpha_buttons.items():
            btn.setStyleSheet(self._get_alpha_btn_style(l == letter))
        self.populate_selector()

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
            self.cmb_selector.addItems(unique_sectors)
            
        self.cmb_selector.blockSignals(False)
        self.plot_chart()

    def plot_chart(self):
        selected = self.cmb_selector.currentText().strip()
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
            dates = df.index
            prices = df['close']
            trend_vals, slope_pct = self.qe.compute_trendline(prices)
            
            ax.plot(dates, prices, label="Close Price" if self.lang == "EN" else "سعر الإغلاق", color="#38bdf8", linewidth=1.8)
            if 'vwap_20' in df.columns:
                ax.plot(dates, df['vwap_20'], label="VWAP (20D)", color="#f59e0b", linestyle=":", alpha=0.8)
            
            trend_color = "#22c55e" if slope_pct >= 0 else "#ef4444"
            trend_label = f"Trendline ({'+' if slope_pct >= 0 else ''}{slope_pct}%)"
            if self.lang == "AR":
                trend_label = f"خط الاتجاه ({'+' if slope_pct >= 0 else ''}{slope_pct}%)"
                
            ax.plot(dates, trend_vals, label=trend_label, color=trend_color, linestyle="--", linewidth=2.0)
            title = f"Stock: {selected} — Trend Analysis" if self.lang == "EN" else f"سهم: {selected} — تحليل الاتجاه"
            ax.set_title(title, color="#ffffff", fontsize=12, fontweight="bold")
            
        else:
            sector_map = self._get_sector_map_cached()
            df_sec = self.qe.get_sector_historical_index(
                selected, sector_map, bulk_data=self._get_bulk_data_cached()
            )
            if df_sec.empty:
                self.canvas.draw()
                return
                
            dates = df_sec.index
            idx_vals = df_sec['sector_index']
            trend_vals, slope_pct = self.qe.compute_trendline(idx_vals)
            
            ax.plot(dates, idx_vals, label="Sector Index (Base 100)" if self.lang == "EN" else "مؤشر القطاع", color="#a855f7", linewidth=2.0)
            trend_color = "#22c55e" if slope_pct >= 0 else "#ef4444"
            trend_label = f"Sector Macro Trend ({'+' if slope_pct >= 0 else ''}{slope_pct}%)"
            if self.lang == "AR":
                trend_label = f"اتجاه القطاع العام ({'+' if slope_pct >= 0 else ''}{slope_pct}%)"
                
            ax.plot(dates, trend_vals, label=trend_label, color=trend_color, linestyle="--", linewidth=2.0)
            title = f"Sector: {selected} Index" if self.lang == "EN" else f"قطاع: {selected}"
            ax.set_title(title, color="#ffffff", fontsize=12, fontweight="bold")

        if len(dates) > 0:
            last_date = dates[-1]
            last_date_lbl = f"Last Data: {latest_date_str}" if self.lang == "EN" else f"آخر بيانات: {latest_date_str}"
            ax.axvline(x=last_date, color="#e11d48", linestyle="-.", alpha=0.85, linewidth=1.5, label=last_date_lbl)

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax.legend(facecolor='#1a1d24', edgecolor='#4a5568', labelcolor='#ffffff', loc='upper left')
        self.fig.tight_layout()
        self.canvas.draw()
