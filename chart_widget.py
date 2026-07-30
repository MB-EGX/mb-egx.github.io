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
        style_layout.addWidget(self.rad_line)
        style_layout.addWidget(self.rad_candle)
        style_layout.addWidget(self.chk_sr)
        style_layout.addStretch()
        layout.addLayout(style_layout)

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
        self.cmb_selector.currentIndexChanged.connect(self.plot_chart)

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

    @staticmethod
    def _compute_support_resistance(df, window=10, max_levels=2, tolerance_pct=0.015):
        """Find recent swing highs/lows as candidate resistance/support levels.

        A bar's high is a "swing high" if it is the max within a +/-window
        neighborhood (same idea for swing lows, using the min). This is a
        standard fractal pivot-point technique, so it reflects levels price
        has actually reacted to recently rather than just the absolute
        52-week high/low. Nearby duplicate levels (within tolerance_pct) are
        merged; only the max_levels most recent distinct levels per side are
        kept so the chart doesn't get cluttered with stale lines.
        """
        if len(df) < window * 2 + 1:
            return [], []

        highs, lows = df["high"].values, df["low"].values
        n = len(df)
        swing_highs, swing_lows = [], []
        for i in range(window, n - window):
            wnd_h = highs[i - window: i + window + 1]
            wnd_l = lows[i - window: i + window + 1]
            if highs[i] == wnd_h.max():
                swing_highs.append((i, highs[i]))
            if lows[i] == wnd_l.min():
                swing_lows.append((i, lows[i]))

        def _dedup_recent(levels):
            out = []
            for idx, price in sorted(levels, key=lambda t: -t[0]):
                if not any(abs(price - p) / p < tolerance_pct for p in out):
                    out.append(price)
                if len(out) >= max_levels:
                    break
            return out

        return _dedup_recent(swing_highs), _dedup_recent(swing_lows)

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
        else:
            self.rad_stock.setText("📈 Per Stock")
            self.rad_sector.setText("🏢 Per Sector")
            self.lbl_select.setText("Select Item:")
            self.lbl_filter_text.setText("🔤 Quick Filter:")
            self.alpha_buttons["ALL"].setText("ALL")
            self.rad_line.setText("📉 Line")
            self.rad_candle.setText("🕯️ Candles")
            self.chk_sr.setText("📐 Support / Resistance")
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

            if self.rad_candle.isChecked() and {'open', 'high', 'low'}.issubset(df.columns):
                self._plot_candles(ax, df)
            else:
                ax.plot(dates, prices, label="Close Price" if self.lang == "EN" else "سعر الإغلاق", color="#38bdf8", linewidth=1.8)
            if 'vwap_20' in df.columns:
                ax.plot(dates, df['vwap_20'], label="VWAP (20D)", color="#f59e0b", linestyle=":", alpha=0.8)

            if self.chk_sr.isChecked() and {'high', 'low'}.issubset(df.columns):
                res_levels, sup_levels = self._compute_support_resistance(df)
                for lvl in res_levels:
                    ax.axhline(y=lvl, color="#ef4444", linestyle=(0, (4, 3)), linewidth=1.1, alpha=0.75, zorder=1)
                    ax.text(dates[-1], lvl, f" R {lvl:.4g}", color="#ef4444", fontsize=8, va="bottom", ha="right", zorder=4)
                for lvl in sup_levels:
                    ax.axhline(y=lvl, color="#22c55e", linestyle=(0, (4, 3)), linewidth=1.1, alpha=0.75, zorder=1)
                    ax.text(dates[-1], lvl, f" S {lvl:.4g}", color="#22c55e", fontsize=8, va="top", ha="right", zorder=4)

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
