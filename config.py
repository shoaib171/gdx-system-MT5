"""
GDX-CORR — Gold/Dollar Correlation Trading System
Central configuration. Edit this file only — engines read from here.
"""

# ============ MT5 CONNECTION ============
MT5_LOGIN = 10011377563  # account number (0 = use already-logged-in terminal)
MT5_PASSWORD = "_wOuIhC8"
MT5_SERVER = "MetaQuotes-Demo"
MT5_TERMINAL_PATH = None # e.g. r"C:\Program Files\MetaTrader 5\terminal64.exe" (None = auto)

# ============ SYMBOLS ============
GOLD_SYMBOL = "XAUUSD"   # Exness standard/mini suffix — change to "XAUUSD" if needed
# Broker DXY symbol candidates (first one found is used). If none exist,
# a synthetic DXY is computed from the 6 component pairs below.
# NOTE: on MetaQuotes-Demo "USDX" is an ETF (SGI Enhanced Core), NOT the dollar
# index — keep it out of the candidates or the engine picks the wrong symbol.
DXY_CANDIDATES = ["DXY", "USDIDX", "USDOLLAR", "USINDEX"]
DXY_COMPONENTS = {
    # pair: (weight, inverted)  — official ICE DXY weights
    "EURUSD": (0.576, True),
    "USDJPY": (0.136, False),
    "GBPUSD": (0.119, True),
    "USDCAD": (0.091, False),
    "USDSEK": (0.042, False),
    "USDCHF": (0.036, False),
}
DXY_CONSTANT = 50.14348112

# ============ ANALYSIS ============
TIMEFRAME = "M15"          # M5 / M15 / M30 / H1
BARS_LOOKBACK = 500        # history pulled per refresh
CORR_WINDOW = 50           # rolling correlation window (bars)
CORR_REGIME_THRESHOLD = -0.60   # correlation must be <= this for regime to be "active"
CORR_Z_WINDOW = 200        # window for correlation z-score (decoupling detection)
EMA_FAST = 9
EMA_SLOW = 21
ROC_PERIOD = 10
ROC_THRESHOLD = 0.05       # % ROC on DXY considered meaningful momentum
ATR_PERIOD = 14

# ============ SIGNAL SCORING (out of 100) ============
SCORE_WEIGHTS = {
    "regime": 30,        # inverse correlation regime active
    "dxy_momentum": 30,  # DXY EMA + ROC direction
    "gold_momentum": 20, # gold's own EMA alignment agrees
    "decoupling": 10,    # correlation z-score breakdown bonus
    "session": 10,       # inside London/NY overlap
}
SIGNAL_THRESHOLD = 70      # score >= this fires a signal
AUTO_TRADE_THRESHOLD = 75  # score >= this executes (if auto-trade ON)

# ============ SESSION FILTER (PKT — Asia/Karachi) ============
SESSION_TZ = "Asia/Karachi"
SESSION_START = "13:00"    # 1:00 PM PKT
SESSION_END = "21:30"      # 9:30 PM PKT
TRADE_ONLY_IN_SESSION = True

# ============ RISK MANAGEMENT ============
RISK_PERCENT = 0.5         # % of equity risked per trade
MIN_LOT = 0.01
MAX_LOT = 0.50
SL_ATR_MULT = 1.5          # stop loss = 1.5 x ATR
TP_RR = 2.0                # take profit = 2R
MAX_OPEN_POSITIONS = 1
MAX_TRADES_PER_DAY = 4
COOLDOWN_AFTER_LOSS_MIN = 45   # minutes paused after a losing trade
MAX_CONSECUTIVE_LOSSES = 2     # hard stop for the day after this many losses
MAGIC_NUMBER = 77201
TRADE_COMMENT = "GDX-CORR"
DEVIATION_POINTS = 30

# ============ DASHBOARD ============
DASHBOARD_HOST = "0.0.0.0"    # 0.0.0.0 = accessible from your phone on VPS IP
DASHBOARD_PORT = 5077
REFRESH_SECONDS = 5           # engine loop interval

# ============ DISCORD (optional) ============
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1524558030098796625/YEUm-ERKJGAG5WGtmaWeKh1Gpe_R1wOhUTzetLMQXjhAFWzgngerSENTtiCeWp-PJla2"      # paste webhook to get signal/trade alerts
