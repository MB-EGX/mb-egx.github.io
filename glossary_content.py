"""
glossary_content.py
====================
Single source of truth for the in-app Glossary: every indicator/term the
engine computes, every action label & marker it displays, and every
chart pattern chart_patterns.PatternDetector can detect — each with a
plain-language explanation in English and Arabic.

Kept as a standalone data module (no Qt/web imports) so both app_gui.py
(desktop) and the web dashboard's build step can read from it without
either one depending on the other's UI framework.

Each entry is a dict: {"term": {"en": ..., "ar": ...}, "definition": {...},
"why_it_matters": {...}, "bias": "bullish"|"bearish"|"neutral"|None}
bias is only set for action labels and chart patterns.
"""

TERMS = [
    {
        "term": {"en": "SMA-50 / SMA-200 (Moving Average)", "ar": "المتوسط المتحرك البسيط (50 / 200 يوم)"},
        "definition": {
            "en": "The average closing price over the last 50 (or 200) trading days. A slow, smoothed line that shows the underlying trend direction, ignoring day-to-day noise.",
            "ar": "متوسط سعر الإغلاق خلال آخر 50 (أو 200) يوم تداول. خط بطيء ومُنعّم يوضح اتجاه الترند الأساسي متجاهلاً تقلبات اليوم الواحد.",
        },
        "why_it_matters": {
            "en": "The SMA-50 golden cross (price/short average crossing above it) is one of the two triggers behind every Breakout Buy signal.",
            "ar": "التقاطع الذهبي للمتوسط 50 يوم (تجاوز السعر له) هو أحد المحفزين الأساسيين خلف كل إشارة شراء اختراق (Breakout Buy).",
        },
    },
    {
        "term": {"en": "EMA-20 (Exponential Moving Average)", "ar": "المتوسط المتحرك الأسي (20 يوم)"},
        "definition": {
            "en": "Like the SMA but weights recent days more heavily, so it reacts faster to new price action.",
            "ar": "مثل المتوسط البسيط لكنه يمنح وزناً أكبر للأيام الأخيرة، لذلك يتفاعل بشكل أسرع مع حركة السعر الجديدة.",
        },
        "why_it_matters": {
            "en": "Price closing above EMA-20 with RSI ≥ 52 is the 'momentum' half of the Breakout Buy signal.",
            "ar": "إغلاق السعر فوق EMA-20 مع مؤشر RSI أكبر من أو يساوي 52 يمثل النصف 'الزخمي' من إشارة شراء الاختراق.",
        },
    },
    {
        "term": {"en": "RSI-14 (Relative Strength Index)", "ar": "مؤشر القوة النسبية (14 يوم)"},
        "definition": {
            "en": "A 0-100 momentum gauge built from the size of recent up-moves vs. down-moves. Above 70 is traditionally 'overbought'; below 30 is 'oversold'.",
            "ar": "مقياس زخم من 0 إلى 100 مبني على حجم الحركات الصاعدة مقابل الهابطة مؤخراً. فوق 70 تُعتبر تقليدياً 'ذروة شراء'، وتحت 30 'ذروة بيع'.",
        },
        "why_it_matters": {
            "en": "Strong Buy requires RSI 55-75 (bullish but not euphoric); Buy on Dip triggers under 38; Sell/Avoid requires under 28.",
            "ar": "إشارة الشراء القوي تتطلب RSI بين 55-75 (صاعد دون نشوة)؛ الشراء عند الانخفاض يُفعّل تحت 38؛ البيع/التجنب يتطلب أقل من 28.",
        },
    },
    {
        "term": {"en": "ADX-14 (Average Directional Index)", "ar": "مؤشر الاتجاه المتوسط (14 يوم)"},
        "definition": {
            "en": "Measures trend strength (not direction) using proper Wilder smoothing. High ADX = a real, strong trend; low ADX = flat, directionless chop.",
            "ar": "يقيس قوة الاتجاه (وليس اتجاهه) باستخدام تنعيم وايلدر الصحيح. ADX مرتفع = ترند حقيقي وقوي؛ ADX منخفض = حركة عشوائية بلا اتجاه.",
        },
        "why_it_matters": {
            "en": "A confirmation gate: Strong Buy / Breakout signals with weak ADX get their score cut to ~35% and are flagged 'Unconfirmed'.",
            "ar": "بوابة تأكيد: إشارات الشراء القوي/الاختراق ذات ADX الضعيف تُخفَّض نقاطها لنحو 35% وتُوسَم بـ 'غير مؤكدة'.",
        },
    },
    {
        "term": {"en": "Volume Ratio & Volume Z-Score", "ar": "نسبة حجم التداول والانحراف المعياري للحجم"},
        "definition": {
            "en": "Today's trading volume compared to the 20-day average, expressed both as a simple ratio and as a statistical z-score (how many standard deviations above normal).",
            "ar": "حجم تداول اليوم مقارنة بمتوسط 20 يوماً، معبراً عنه كنسبة بسيطة وكانحراف معياري إحصائي (عدد الانحرافات المعيارية فوق المعدل الطبيعي).",
        },
        "why_it_matters": {
            "en": "The second half of the confirmation gate alongside ADX — a breakout without above-average volume behind it is exactly the kind of setup that traps traders.",
            "ar": "النصف الثاني من بوابة التأكيد بجانب ADX — الاختراق بدون حجم تداول أعلى من المتوسط هو بالضبط النوع الذي يوقع المتداولين في الفخ.",
        },
    },
    {
        "term": {"en": "CMF (Chaikin Money Flow)", "ar": "مؤشر تشايكن للتدفق النقدي"},
        "definition": {
            "en": "Weights each day's volume by where the close landed within that day's high-low range — positive when buying pressure dominates, negative when selling pressure does.",
            "ar": "يُرجّح حجم كل يوم حسب موقع الإغلاق ضمن نطاق أعلى/أدنى سعر لذلك اليوم — موجب عندما يهيمن ضغط الشراء، وسالب عندما يهيمن ضغط البيع.",
        },
        "why_it_matters": {
            "en": "A +15 score bonus when positive and above threshold — confirms buying is happening on strength, not just price drifting up on light volume.",
            "ar": "مكافأة +15 نقطة عندما يكون موجباً وفوق العتبة — يؤكد أن الشراء يحدث بقوة حقيقية، وليس مجرد ارتفاع سعر على حجم تداول ضعيف.",
        },
    },
    {
        "term": {"en": "ATR-14 (Average True Range)", "ar": "متوسط المدى الحقيقي (14 يوم)"},
        "definition": {
            "en": "The average size of the stock's daily price swings over 14 days, in price terms — a direct measure of volatility.",
            "ar": "متوسط حجم تذبذبات السعر اليومية للسهم خلال 14 يوماً، بوحدة السعر — مقياس مباشر للتقلب.",
        },
        "why_it_matters": {
            "en": "Drives the trailing stop-loss distance and the take-profit floor, so risk levels scale with how volatile the stock actually is instead of a flat percentage for every stock.",
            "ar": "يحدد مسافة وقف الخسارة المتحرك وأرضية هدف الربح، بحيث تتناسب مستويات المخاطرة مع التقلب الفعلي للسهم بدلاً من نسبة ثابتة لكل الأسهم.",
        },
    },
    {
        "term": {"en": "Volatility Squeeze (Bollinger inside Keltner)", "ar": "الانضغاط التقلبي (بولينجر داخل كيلتنر)"},
        "definition": {
            "en": "Bollinger Bands (price-volatility bands) compressed inside Keltner Channels (ATR-based bands) — a classic 'calm before the storm' setup.",
            "ar": "انضغاط نطاقات بولينجر (نطاقات التقلب السعري) داخل قنوات كيلتنر (نطاقات مبنية على ATR) — إعداد كلاسيكي لـ 'الهدوء الذي يسبق العاصفة'.",
        },
        "why_it_matters": {
            "en": "Volatility clusters — a quiet period often precedes a bigger move. Shown as a [💥 SQUEEZE] marker and a modest +10 score bonus.",
            "ar": "التقلب يميل للتجمع — فترة الهدوء غالباً ما تسبق حركة أكبر. يظهر كعلامة [💥 SQUEEZE] ومكافأة متواضعة +10 نقاط.",
        },
    },
    {
        "term": {"en": "VWAP-20 (Volume-Weighted Average Price)", "ar": "متوسط السعر المرجح بالحجم (20 يوم)"},
        "definition": {
            "en": "The average price over 20 days, weighted by how much volume traded at each price level — a benchmark for whether the current price is 'cheap' or 'expensive' relative to recent real trading activity.",
            "ar": "متوسط السعر خلال 20 يوماً، مرجحاً بحجم التداول عند كل مستوى سعري — معيار لمعرفة ما إذا كان السعر الحالي 'رخيصاً' أو 'مرتفعاً' نسبة للنشاط التجاري الفعلي الأخير.",
        },
        "why_it_matters": {
            "en": "An owned position trading below VWAP with a loss triggers the ⚠️ CUT LOSS / REVIEW action command.",
            "ar": "المركز المملوك المتداول تحت VWAP مع خسارة يُفعّل أمر الإجراء ⚠️ وقف الخسارة / مراجعة.",
        },
    },
    {
        "term": {"en": "MACD (Moving Average Convergence Divergence)", "ar": "مؤشر تقارب وتباعد المتوسطات المتحركة"},
        "definition": {
            "en": "The gap between the 12-day and 26-day EMA, plus a 9-day signal line. A bullish cross (MACD above its signal line) or bearish cross tells you momentum is shifting.",
            "ar": "الفارق بين المتوسط الأسي 12 يوماً و26 يوماً، بالإضافة إلى خط إشارة 9 أيام. التقاطع الصاعد (MACD فوق خط الإشارة) أو الهابط يوضح تحول الزخم.",
        },
        "why_it_matters": {
            "en": "Shown as a 🟢 Bullish Cross / 🔴 Bearish Cross state alongside the other momentum readings for extra context on a chart.",
            "ar": "يظهر كحالة 🟢 تقاطع صاعد / 🔴 تقاطع هابط بجانب قراءات الزخم الأخرى لسياق إضافي على الرسم البياني.",
        },
    },
    {
        "term": {"en": "Pivot Points (Support / Resistance)", "ar": "نقاط الارتكاز (الدعم / المقاومة)"},
        "definition": {
            "en": "Classic formula-based levels derived from the prior period's high, low, and close, marking likely reaction zones on the chart.",
            "ar": "مستويات كلاسيكية مبنية على معادلة مشتقة من أعلى وأدنى وإغلاق الفترة السابقة، تحدد مناطق رد الفعل المحتملة على الرسم البياني.",
        },
        "why_it_matters": {
            "en": "Plotted directly on the chart as reference lines alongside the 52-week Resistance/Support levels.",
            "ar": "تُرسم مباشرة على الرسم البياني كخطوط مرجعية بجانب مستويات المقاومة/الدعم لـ52 أسبوعاً.",
        },
    },
    {
        "term": {"en": "52-Week Range Position", "ar": "الموقع ضمن نطاق 52 أسبوعاً"},
        "definition": {
            "en": "Where today's price sits between the highest and lowest close of the past 250 trading days, as a percentage (0% = at the low, 100% = at the high).",
            "ar": "موقع سعر اليوم بين أعلى وأدنى إغلاق خلال آخر 250 يوم تداول، كنسبة مئوية (0% = عند القاع، 100% = عند القمة).",
        },
        "why_it_matters": {
            "en": "Strong Buy requires being in the top 15% of the range; Buy on Dip requires being in the bottom 25%.",
            "ar": "الشراء القوي يتطلب التواجد ضمن أعلى 15% من النطاق؛ الشراء عند الانخفاض يتطلب التواجد ضمن أدنى 25%.",
        },
    },
    {
        "term": {"en": "Weekly Alignment", "ar": "التوافق الأسبوعي"},
        "definition": {
            "en": "The stock's 50-week moving average and weekly RSI, calculated from the last fully completed week only — never the current, in-progress week.",
            "ar": "المتوسط المتحرك 50 أسبوعاً ومؤشر RSI الأسبوعي، محسوبان من آخر أسبوع مكتمل فقط — وليس من الأسبوع الجاري.",
        },
        "why_it_matters": {
            "en": "The single largest score bonus (+20), reserved for when the daily signal and the higher weekly timeframe genuinely agree — the strongest filter against false starts.",
            "ar": "أكبر مكافأة نقاط منفردة (+20)، مخصصة لحالة توافق الإشارة اليومية الحقيقي مع الإطار الزمني الأسبوعي الأعلى — أقوى فلتر ضد الانطلاقات الكاذبة.",
        },
    },
    {
        "term": {"en": "Trend Class", "ar": "تصنيف الترند"},
        "definition": {
            "en": "A plain-language read of trend + strength: Strong Bullish, Weak Bullish, Weak Bullish (Low Trend Strength), Consolidation / Neutral, Weak Bearish, or Strong Bearish.",
            "ar": "قراءة بلغة مبسطة للترند وقوته: صاعد قوي، صاعد ضعيف، صاعد ضعيف (قوة ترند منخفضة)، تماسك/محايد، هابط ضعيف، أو هابط قوي.",
        },
        "why_it_matters": {
            "en": "A quick-glance summary column so you don't have to mentally combine SMA position and ADX yourself.",
            "ar": "عمود ملخص سريع حتى لا تضطر لدمج موقع SMA وADX ذهنياً بنفسك.",
        },
    },
    {
        "term": {"en": "Rank Score", "ar": "درجة الترتيب"},
        "definition": {
            "en": "The composite number every stock is sorted by — built from the base signal plus every bonus and penalty described in this glossary, scaled by data confidence.",
            "ar": "الرقم المركب الذي تُرتَّب الأسهم بموجبه — مبني من الإشارة الأساسية بالإضافة إلى كل مكافأة وعقوبة موضحة في هذا القاموس، مُقاسة بثقة البيانات.",
        },
        "why_it_matters": {
            "en": "Lets you compare conviction across every stock in the market in one glance, not just a yes/no flag.",
            "ar": "يتيح لك مقارنة درجة الثقة عبر كل الأسهم في السوق بنظرة واحدة، وليس مجرد علامة نعم/لا.",
        },
    },
    {
        "term": {"en": "Data Confidence", "ar": "ثقة البيانات"},
        "definition": {
            "en": "A tier (Very Low, Low, Medium, High) reflecting how many clean trading days of history a stock has — floored at 50% trust under ~25 bars, full trust past ~250 bars (about a year).",
            "ar": "مستوى (منخفض جداً، منخفض، متوسط، مرتفع) يعكس عدد أيام التداول النظيفة المتوفرة للسهم — بحد أدنى ثقة 50% تحت ~25 شمعة، وثقة كاملة بعد ~250 شمعة (نحو عام).",
        },
        "why_it_matters": {
            "en": "Stops a newly listed or thinly-tracked stock from ever generating a maximum-confidence score, no matter how clean the chart looks.",
            "ar": "يمنع سهماً حديث الإدراج أو ضعيف التتبع من الحصول على درجة ثقة قصوى، مهما بدا الرسم البياني نظيفاً.",
        },
    },
    {
        "term": {"en": "Historical Pattern Match & Confidence", "ar": "التطابق التاريخي للنمط والثقة"},
        "definition": {
            "en": "The engine takes the most recent 15 trading days as a fingerprint, searches the stock's entire prior history for similar windows, and looks at what happened in the 5 days after each match.",
            "ar": "يأخذ المحرك آخر 15 يوم تداول كبصمة، ويبحث في كامل التاريخ السابق للسهم عن نوافذ مشابهة، وينظر لما حدث خلال 5 أيام بعد كل تطابق.",
        },
        "why_it_matters": {
            "en": "Confidence is reduced (via a Sortino-style penalty) when past outcomes disagreed with each other or had a bad downside tail — an honest, not just optimistic, statistic.",
            "ar": "تُخفَّض الثقة (عبر عقوبة على طراز سورتينو) عندما تتباين النتائج التاريخية مع بعضها أو تحمل ذيلاً سلبياً سيئاً — إحصائية صادقة وليست متفائلة فقط.",
        },
    },
    {
        "term": {"en": "Suggested Shares (1% Risk)", "ar": "الأسهم المقترحة (مخاطرة 1%)"},
        "definition": {
            "en": "The share count sized so that if the stock hits its suggested stop-loss, the loss equals exactly 1% of your total account equity.",
            "ar": "عدد الأسهم المحسوب بحيث إذا وصل السهم إلى وقف الخسارة المقترح، تساوي الخسارة 1% بالضبط من إجمالي حقوق حسابك.",
        },
        "why_it_matters": {
            "en": "Turns a ranked list of stocks into an actual, executable position size instead of leaving sizing to guesswork.",
            "ar": "يحوّل قائمة أسهم مرتبة إلى حجم مركز فعلي وقابل للتنفيذ بدلاً من ترك التحجيم للتخمين.",
        },
    },
    {
        "term": {"en": "Stop-Loss / Trailing Stop / Take-Profit", "ar": "وقف الخسارة / الوقف المتحرك / هدف الربح"},
        "definition": {
            "en": "Stop-loss: an ATR-based exit price to cap downside. Trailing stop: the same idea but rises as an owned position gains, locking in profit. Take-profit: a target blended from either the pattern-match projection or an ATR-based floor.",
            "ar": "وقف الخسارة: سعر خروج مبني على ATR للحد من الخسارة. الوقف المتحرك: نفس الفكرة لكنه يرتفع مع ارتفاع المركز المملوك، مما يثبّت الربح. هدف الربح: هدف مُركّب من توقع تطابق النمط أو أرضية مبنية على ATR.",
        },
        "why_it_matters": {
            "en": "A good signal with no risk plan is half an analysis — this is what turns a score into an actual trading plan.",
            "ar": "الإشارة الجيدة بدون خطة مخاطرة هي نصف تحليل فقط — هذا ما يحوّل الدرجة إلى خطة تداول فعلية.",
        },
    },
    {
        "term": {"en": "Breakeven Shares Needed", "ar": "الأسهم المطلوبة لتعادل النقطة"},
        "definition": {
            "en": "For a losing position: exactly how many more shares you'd need to buy at today's price to bring your weighted-average cost down to today's price (accounting for round-trip fees).",
            "ar": "لمركز خاسر: العدد الدقيق للأسهم الإضافية التي يجب شراؤها بسعر اليوم لخفض متوسط التكلفة المرجح إلى سعر اليوم (مع احتساب رسوم التداول ذهاباً وإياباً).",
        },
        "why_it_matters": {
            "en": "Turns 'should I average down?' from a gut feeling into an exact, fee-aware number.",
            "ar": "يحوّل سؤال 'هل يجب أن أخفض متوسط السعر؟' من شعور غريزي إلى رقم دقيق يراعي الرسوم.",
        },
    },
    {
        "term": {"en": "Portfolio Concentration Risk", "ar": "مخاطر تركز المحفظة"},
        "definition": {
            "en": "A check on whether too much of your account sits in one sector or one ticker — a blind spot that per-stock risk metrics can't catch on their own.",
            "ar": "فحص لمعرفة ما إذا كان جزء كبير من حسابك مركزاً في قطاع واحد أو سهم واحد — نقطة عمياء لا تستطيع مقاييس المخاطرة الفردية للسهم اكتشافها بمفردها.",
        },
        "why_it_matters": {
            "en": "Each stock can look individually fine while the account as a whole is one bad sector-day away from a large loss — this warning catches that.",
            "ar": "قد يبدو كل سهم جيداً بمفرده بينما الحساب ككل على بعد يوم قطاعي سيء واحد من خسارة كبيرة — هذا التحذير يكشف ذلك.",
        },
    },
]

ACTION_LABELS = [
    {
        "term": {"en": "🔥 STRONG BUY", "ar": "🔥 شراء قوي"},
        "bias": "bullish",
        "definition": {
            "en": "Near the top of its 52-week range, RSI in the bullish-but-not-euphoric 55-75 zone, no down-gap.",
            "ar": "قرب قمة نطاق 52 أسبوعاً، RSI ضمن نطاق 55-75 الصاعد دون نشوة، بدون فجوة هبوطية.",
        },
        "why_it_matters": {
            "en": "The highest-conviction bullish label the system produces.",
            "ar": "أعلى تصنيف صاعد ثقة يُنتجه النظام.",
        },
    },
    {
        "term": {"en": "⚡ BREAKOUT BUY (X-OVER + MOMENTUM)", "ar": "⚡ شراء اختراق (تقاطع + زخم)"},
        "bias": "bullish",
        "definition": {
            "en": "Both the SMA-50 golden-cross trigger AND the EMA20/RSI momentum trigger fired together — the strongest breakout label.",
            "ar": "تفعّل كلٌ من محفز التقاطع الذهبي SMA-50 ومحفز الزخم EMA20/RSI معاً — أقوى تصنيف اختراق.",
        },
        "why_it_matters": {
            "en": "Two independent breakout confirmations agreeing is rarer, and historically more reliable, than either alone.",
            "ar": "تفعيل محفزين مستقلين للاختراق معاً أندر، وأكثر موثوقية تاريخياً، من تفعيل أي منهما بمفرده.",
        },
    },
    {
        "term": {"en": "⚡ BREAKOUT BUY (X-OVER)", "ar": "⚡ شراء اختراق (تقاطع)"},
        "bias": "bullish",
        "definition": {
            "en": "Only the SMA-50 golden-cross component fired.",
            "ar": "تفعّل مكوّن التقاطع الذهبي SMA-50 فقط.",
        },
        "why_it_matters": {
            "en": "A real structural trend shift, without confirmation from short-term momentum yet.",
            "ar": "تحول هيكلي حقيقي في الترند، دون تأكيد من الزخم قصير المدى بعد.",
        },
    },
    {
        "term": {"en": "⚡ BREAKOUT BUY (MOMENTUM)", "ar": "⚡ شراء اختراق (زخم)"},
        "bias": "bullish",
        "definition": {
            "en": "Only the EMA20 + RSI≥52 momentum component fired.",
            "ar": "تفعّل مكوّن الزخم EMA20 + RSI≥52 فقط.",
        },
        "why_it_matters": {
            "en": "Fresh short-term momentum, without a confirmed longer-term structural cross yet.",
            "ar": "زخم قصير المدى جديد، دون تأكيد تقاطع هيكلي طويل المدى بعد.",
        },
    },
    {
        "term": {"en": "⏳ BUY ON DIP", "ar": "⏳ شراء عند الانخفاض"},
        "bias": "bullish",
        "definition": {
            "en": "In the bottom 25% of the 52-week range with RSI under 38 — oversold but not in a Sell/Avoid breakdown.",
            "ar": "ضمن أدنى 25% من نطاق 52 أسبوعاً مع RSI أقل من 38 — ذروة بيع لكن ليس ضمن انهيار البيع/التجنب.",
        },
        "why_it_matters": {
            "en": "Flags potential value entries in a pullback, distinct from a stock in genuine breakdown.",
            "ar": "يحدد نقاط دخول قيمة محتملة أثناء التصحيح، بشكل مختلف عن سهم في انهيار حقيقي.",
        },
    },
    {
        "term": {"en": "📈 ACCUMULATE", "ar": "📈 تجميع"},
        "bias": "bullish",
        "definition": {
            "en": "A moderate, steady bullish setup that doesn't meet the stricter bar for Strong Buy or Breakout.",
            "ar": "إعداد صاعد معتدل وثابت لا يستوفي المعيار الأكثر صرامة للشراء القوي أو الاختراق.",
        },
        "why_it_matters": {
            "en": "Worth watching or scaling into gradually, rather than a high-urgency signal.",
            "ar": "يستحق المراقبة أو الدخول التدريجي فيه، وليس إشارة عالية الإلحاح.",
        },
    },
    {
        "term": {"en": "🛑 SELL / AVOID", "ar": "🛑 بيع / تجنب"},
        "bias": "bearish",
        "definition": {
            "en": "Close is under 75% of the SMA-50 with RSI under 28 — a real breakdown, not just a dip.",
            "ar": "الإغلاق أقل من 75% من SMA-50 مع RSI أقل من 28 — انهيار حقيقي وليس مجرد تصحيح.",
        },
        "why_it_matters": {
            "en": "Catastrophic drawdown plus deep oversold together is a different situation from Buy on Dip and is flagged accordingly.",
            "ar": "التراجع الكارثي مع ذروة البيع العميقة معاً وضع مختلف عن الشراء عند الانخفاض ويُوسم على هذا الأساس.",
        },
    },
    {
        "term": {"en": "(Unconfirmed: low ADX/volume)", "ar": "(غير مؤكد: ADX/حجم منخفض)"},
        "bias": None,
        "definition": {
            "en": "Appended when a base signal fired but ADX and volume didn't confirm it. Score is cut to ~35% of normal.",
            "ar": "يُضاف عندما تُفعَّل الإشارة الأساسية لكن ADX والحجم لم يؤكداها. تُخفَّض الدرجة إلى نحو 35% من قيمتها الطبيعية.",
        },
        "why_it_matters": {
            "en": "Tells you the setup looks right but the market hasn't committed to it with real trend strength and volume yet.",
            "ar": "يوضح أن الإعداد يبدو صحيحاً لكن السوق لم يلتزم به بعد بقوة ترند وحجم حقيقيين.",
        },
    },
    {
        "term": {"en": "🚫 ILLIQUID (prefix)", "ar": "🚫 غير سائل (بادئة)"},
        "bias": "bearish",
        "definition": {
            "en": "Average trading volume is under the minimum liquidity floor. A 22-40 point penalty is applied on top of the base score.",
            "ar": "متوسط حجم التداول أقل من الحد الأدنى للسيولة. تُطبَّق عقوبة من 22-40 نقطة فوق الدرجة الأساسية.",
        },
        "why_it_matters": {
            "en": "A perfect-looking setup on a stock nobody trades is a trap — you may not be able to enter or exit at a fair price.",
            "ar": "الإعداد المثالي المظهر لسهم لا يتداوله أحد هو فخ — قد لا تستطيع الدخول أو الخروج بسعر عادل.",
        },
    },
    {
        "term": {"en": "[💥 SQUEEZE]", "ar": "[💥 انضغاط]"},
        "bias": None,
        "definition": {
            "en": "Bollinger Bands compressed inside Keltner Channels — volatility has contracted and may be about to expand.",
            "ar": "انضغاط نطاقات بولينجر داخل قنوات كيلتنر — انكمش التقلب وقد يكون على وشك التوسع.",
        },
        "why_it_matters": {
            "en": "A 'get ready' marker layered on top of whatever the base signal already says — doesn't predict direction by itself.",
            "ar": "علامة 'استعد' تُضاف فوق ما تقوله الإشارة الأساسية أصلاً — لا تتنبأ بالاتجاه بمفردها.",
        },
    },
    {
        "term": {"en": "[👑 WEEKLY ALIGNED]", "ar": "[👑 توافق أسبوعي]"},
        "bias": None,
        "definition": {
            "en": "The weekly SMA-50 and weekly RSI (from the last completed week) agree with the daily signal.",
            "ar": "المتوسط الأسبوعي SMA-50 وRSI الأسبوعي (من آخر أسبوع مكتمل) يتوافقان مع الإشارة اليومية.",
        },
        "why_it_matters": {
            "en": "Only appears on Strong Buy and Breakout Buy — the single largest score bonus (+20), and the strongest filter against false starts.",
            "ar": "يظهر فقط على الشراء القوي وشراء الاختراق — أكبر مكافأة نقاط منفردة (+20)، وأقوى فلتر ضد الانطلاقات الكاذبة.",
        },
    },
    {
        "term": {"en": "🎯 Breakout Watchlist", "ar": "🎯 قائمة مراقبة الاختراق"},
        "bias": "bullish",
        "definition": {
            "en": "A separate, pre-breakout screen: ADX 15-25 (some trend forming, not flat, not overextended), RSI 50-65, near resistance, and 5-day volume building versus the prior 5 days.",
            "ar": "فحص منفصل قبل الاختراق: ADX بين 15-25 (ترند يتشكل، ليس مسطحاً ولا مبالغاً فيه)، RSI بين 50-65، قرب المقاومة، وحجم تداول متزايد خلال 5 أيام مقارنة بالـ5 أيام السابقة.",
        },
        "why_it_matters": {
            "en": "Answers a different question than the reactive Breakout Buy labels: 'what might break out next session/week', not 'what broke out today'.",
            "ar": "يجيب على سؤال مختلف عن تصنيفات شراء الاختراق التفاعلية: 'ما الذي قد يخترق الجلسة/الأسبوع القادم'، وليس 'ما الذي اخترق اليوم'.",
        },
    },
    {
        "term": {"en": "🛡️ HOLD / TRAIL STOP", "ar": "🛡️ احتفظ / وقف متحرك"},
        "bias": None,
        "definition": {
            "en": "Shown on an owned position that's healthy — keep holding and let the trailing stop protect gains.",
            "ar": "يظهر على مركز مملوك في حالة جيدة — استمر بالاحتفاظ ودع الوقف المتحرك يحمي الأرباح.",
        },
        "why_it_matters": {
            "en": "The default, no-action-needed state for a position that isn't near either exit trigger.",
            "ar": "الحالة الافتراضية التي لا تتطلب إجراءً لمركز ليس قريباً من أي محفز خروج.",
        },
    },
    {
        "term": {"en": "⚠️ CUT LOSS / REVIEW (Below VWAP)", "ar": "⚠️ وقف الخسارة / مراجعة (تحت VWAP)"},
        "bias": "bearish",
        "definition": {
            "en": "An owned position that's underwater and trading below its VWAP-20 — a signal the position needs a hard look.",
            "ar": "مركز مملوك في حالة خسارة ويتداول تحت VWAP-20 — إشارة على أن المركز يحتاج مراجعة جادة.",
        },
        "why_it_matters": {
            "en": "Combines an actual loss with a real weakness signal, rather than reacting to price alone.",
            "ar": "يجمع بين خسارة فعلية وإشارة ضعف حقيقية، بدلاً من التفاعل مع السعر وحده.",
        },
    },
    {
        "term": {"en": "💰 TAKE PROFIT ZONE", "ar": "💰 منطقة جني الأرباح"},
        "bias": "bullish",
        "definition": {
            "en": "An owned position that has reached its take-profit target.",
            "ar": "مركز مملوك وصل إلى هدف جني الأرباح المحدد له.",
        },
        "why_it_matters": {
            "en": "A clear, pre-committed exit signal instead of having to decide in the moment whether a gain is 'enough'.",
            "ar": "إشارة خروج واضحة ومحددة مسبقاً بدلاً من الاضطرار لقرار لحظي حول ما إذا كان الربح 'كافياً'.",
        },
    },
]

CHART_PATTERNS = [
    {
        "term": {"en": "Head & Shoulders", "ar": "الرأس والكتفين"},
        "bias": "bearish",
        "definition": {
            "en": "Three peaks — a higher 'head' between two roughly equal 'shoulders' — with a support 'neckline' connecting the troughs between them.",
            "ar": "ثلاث قمم — 'رأس' أعلى بين 'كتفين' متساويين تقريباً — مع خط 'رقبة' داعم يربط بين القيعان بينهما.",
        },
        "why_it_matters": {
            "en": "A break below the neckline is a classic reversal signal from an uptrend into a downtrend; the projected target is the head-to-neckline distance measured down from the break.",
            "ar": "الكسر تحت خط الرقبة إشارة انعكاس كلاسيكية من ترند صاعد إلى هابط؛ الهدف المتوقع هو مسافة الرأس إلى الرقبة مقاسة للأسفل من نقطة الكسر.",
        },
    },
    {
        "term": {"en": "Inverse Head & Shoulders", "ar": "الرأس والكتفين المعكوس"},
        "bias": "bullish",
        "definition": {
            "en": "The mirror image of Head & Shoulders — three troughs with a lower 'head', and a resistance neckline connecting the peaks between them.",
            "ar": "الصورة المعكوسة للرأس والكتفين — ثلاث قيعان مع 'رأس' أدنى، وخط رقبة مقاوم يربط بين القمم بينهما.",
        },
        "why_it_matters": {
            "en": "A break above the neckline signals a reversal from a downtrend into an uptrend.",
            "ar": "الكسر فوق خط الرقبة يشير إلى انعكاس من ترند هابط إلى صاعد.",
        },
    },
    {
        "term": {"en": "Double Top", "ar": "القمة المزدوجة"},
        "bias": "bearish",
        "definition": {
            "en": "Two roughly equal peaks separated by a pullback — the second failed attempt to break higher.",
            "ar": "قمتان متساويتان تقريباً يفصل بينهما تصحيح — المحاولة الثانية الفاشلة لكسر الأعلى.",
        },
        "why_it_matters": {
            "en": "Signals the stock failed twice at the same resistance — a warning that upward momentum is exhausted.",
            "ar": "يشير إلى فشل السهم مرتين عند نفس المقاومة — تحذير من نفاد الزخم الصاعد.",
        },
    },
    {
        "term": {"en": "Double Bottom", "ar": "القاع المزدوج"},
        "bias": "bullish",
        "definition": {
            "en": "Two roughly equal troughs separated by a bounce — the second failed attempt to break lower.",
            "ar": "قاعان متساويان تقريباً يفصل بينهما ارتداد — المحاولة الثانية الفاشلة لكسر الأدنى.",
        },
        "why_it_matters": {
            "en": "Signals the stock found support twice at the same level — a sign selling pressure may be exhausted.",
            "ar": "يشير إلى إيجاد السهم دعماً مرتين عند نفس المستوى — علامة على احتمال نفاد ضغط البيع.",
        },
    },
    {
        "term": {"en": "Ascending Triangle", "ar": "المثلث الصاعد"},
        "bias": "bullish",
        "definition": {
            "en": "A flat resistance line on top with a rising line of higher lows underneath, squeezing price into a point.",
            "ar": "خط مقاومة أفقي في الأعلى مع خط صاعد من قيعان مرتفعة أسفله، يضغط السعر نحو نقطة.",
        },
        "why_it_matters": {
            "en": "Buyers are stepping in at progressively higher prices while sellers defend one fixed level — usually resolves upward through resistance.",
            "ar": "المشترون يدخلون بأسعار مرتفعة تدريجياً بينما يدافع البائعون عن مستوى ثابت واحد — عادة ما يُحل صعوداً عبر المقاومة.",
        },
    },
    {
        "term": {"en": "Descending Triangle", "ar": "المثلث الهابط"},
        "bias": "bearish",
        "definition": {
            "en": "A flat support line on the bottom with a falling line of lower highs above it.",
            "ar": "خط دعم أفقي في الأسفل مع خط هابط من قمم منخفضة فوقه.",
        },
        "why_it_matters": {
            "en": "Sellers are stepping in at progressively lower prices while buyers defend one fixed level — usually resolves downward through support.",
            "ar": "البائعون يدخلون بأسعار منخفضة تدريجياً بينما يدافع المشترون عن مستوى ثابت واحد — عادة ما يُحل هبوطاً عبر الدعم.",
        },
    },
    {
        "term": {"en": "Symmetrical Triangle", "ar": "المثلث المتماثل"},
        "bias": "neutral",
        "definition": {
            "en": "Both the resistance line (falling) and support line (rising) converge toward a point at roughly the same angle.",
            "ar": "كل من خط المقاومة (الهابط) وخط الدعم (الصاعد) يتقاربان نحو نقطة بزاوية متماثلة تقريباً.",
        },
        "why_it_matters": {
            "en": "A genuine coin-flip pattern by itself — direction depends on which side it breaks, so it's most useful combined with the trend and volume context around it.",
            "ar": "نمط متساوي الاحتمالات بمفرده — الاتجاه يعتمد على أي جانب يُكسر، لذا فهو أكثر فائدة عند دمجه مع سياق الترند والحجم المحيط به.",
        },
    },
    {
        "term": {"en": "Price Channel (Ascending / Descending / Horizontal)", "ar": "القناة السعرية (صاعدة / هابطة / أفقية)"},
        "bias": None,
        "definition": {
            "en": "Two roughly parallel trendlines containing price action — rising together (ascending), falling together (descending), or flat (horizontal / range-bound).",
            "ar": "خطا ترند متوازيان تقريباً يحصران حركة السعر — يرتفعان معاً (صاعدة)، ينخفضان معاً (هابطة)، أو مسطحان (أفقية / محصورة النطاق).",
        },
        "why_it_matters": {
            "en": "Shows the stock is trading in an orderly, structured range — useful for spotting where it sits within its own established rhythm.",
            "ar": "يوضح تداول السهم بنطاق منظم ومنضبط — مفيد لتحديد موقعه ضمن إيقاعه الخاص المستقر.",
        },
    },
    {
        "term": {"en": "Bull Flag", "ar": "علم صاعد"},
        "bias": "bullish",
        "definition": {
            "en": "A sharp upward 'pole' move followed by a brief, controlled, slightly downward-drifting consolidation (the 'flag').",
            "ar": "حركة 'عمود' صاعدة حادة تليها فترة تماسك قصيرة ومنضبطة بميل هابط طفيف (الـ'علم').",
        },
        "why_it_matters": {
            "en": "A pause that digests a strong move without giving it back — historically often continues in the same direction as the pole.",
            "ar": "توقف يهضم حركة قوية دون التخلي عنها — تاريخياً غالباً ما يستمر بنفس اتجاه العمود.",
        },
    },
    {
        "term": {"en": "Bear Flag", "ar": "علم هابط"},
        "bias": "bearish",
        "definition": {
            "en": "The mirror of a Bull Flag — a sharp downward pole followed by a brief, controlled, slightly upward-drifting consolidation.",
            "ar": "الصورة المعكوسة للعلم الصاعد — عمود هابط حاد يليه تماسك قصير ومنضبط بميل صاعد طفيف.",
        },
        "why_it_matters": {
            "en": "Often continues the downward move once the brief consolidation resolves.",
            "ar": "غالباً ما يستمر بالحركة الهابطة بمجرد انتهاء التماسك القصير.",
        },
    },
    {
        "term": {"en": "Pennant", "ar": "الراية"},
        "bias": None,
        "definition": {
            "en": "Like a flag, but the consolidation after the sharp pole move converges into a small symmetrical triangle instead of a parallel channel.",
            "ar": "مثل العلم، لكن التماسك بعد حركة العمود الحادة يتقارب في مثلث متماثل صغير بدلاً من قناة متوازية.",
        },
        "why_it_matters": {
            "en": "Same 'pause that digests a strong move' logic as a flag; direction is inherited from the pole that preceded it.",
            "ar": "نفس منطق 'التوقف الذي يهضم حركة قوية' كالعلم؛ الاتجاه موروث من العمود الذي سبقه.",
        },
    },
    {
        "term": {"en": "Cup & Handle", "ar": "الكوب والمقبض"},
        "bias": "bullish",
        "definition": {
            "en": "A rounded, U-shaped consolidation (the 'cup') between two roughly equal price rims, followed by a small downward-drifting pullback (the 'handle') before a breakout.",
            "ar": "تماسك دائري على شكل حرف U (الـ'كوب') بين حافتين سعريتين متساويتين تقريباً، يليه تصحيح صغير هابط (الـ'مقبض') قبل الاختراق.",
        },
        "why_it_matters": {
            "en": "A longer-duration bullish base-building pattern — the handle pullback is often the last shakeout before a genuine breakout above the rim.",
            "ar": "نمط بناء قاعدة صاعد أطول أمداً — تصحيح المقبض غالباً ما يكون آخر هزة قبل اختراق حقيقي فوق الحافة.",
        },
    },
]
