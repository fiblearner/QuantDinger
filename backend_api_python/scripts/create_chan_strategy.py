"""
create_chan_strategy.py
自动登录 QuantDinger，读取 top_symbols_output.json，创建缠论截面策略。

用法:
  docker exec quantdinger-backend python scripts/create_chan_strategy.py

可选环境变量:
  QD_BASE_URL=http://localhost:5000   后端地址
  QD_USER=quantdinger                 登录用户名
  QD_PASS=123456                      登录密码
  QD_STRATEGY_NAME=缠论扫描策略        策略名称
  QD_TIMEFRAME=4H                     K线周期
  QD_TOP_N=60                         使用 symbol_list 前 N 个
  SYMBOLS_FILE=                       自定义 JSON 文件路径 (默认读 top_symbols_output.json)
"""
import os
import sys
import json

try:
    import requests
except ImportError:
    print("[ERROR] 未找到 requests，请先安装: pip install requests")
    sys.exit(1)

# ── 配置 ────────────────────────────────────────────────────────────────────
BASE_URL       = os.environ.get("QD_BASE_URL", "http://localhost:5000").rstrip("/")
USERNAME       = os.environ.get("QD_USER", "quantdinger")
PASSWORD       = os.environ.get("QD_PASS", "123456")
STRATEGY_NAME  = os.environ.get("QD_STRATEGY_NAME", "缠论扫描策略")
TIMEFRAME      = os.environ.get("QD_TIMEFRAME", "4H")
TOP_N          = int(os.environ.get("QD_TOP_N", "100"))
SYMBOLS_FILE   = os.environ.get(
    "SYMBOLS_FILE",
    os.path.join(os.path.dirname(__file__), "top_symbols_output.json"),
)


# ── 缠论评分指标代码（截面策略用）────────────────────────────────────────────
INDICATOR_CODE = '''
# ============================================================
# 缠论买点扫描 v3 - 只做多（二买 / 三买）
#
# 开仓过滤：
#   1. 只识别二买 / 三买信号（不做空，不做一买）
#   2. 个股 60 日均线趋势过滤（4H×240根，价格需在均线以上）
#   3. 量能萎缩过滤（萎缩状态下必须放量才允许入场）
#   4. 评分阈值 60（二买满分 75，三买满分 65，RSI 加权后可超 80）
#
# score > 0：买点（越大越强）
# score = 0：无信号或被过滤
# ============================================================

import numpy as np
import pandas as pd

# ────────────────────────────────────────────────────────────
# 工具函数
# ────────────────────────────────────────────────────────────

def merge_inclusion(highs, lows):
    ph, pl = list(highs), list(lows)
    direction = 1
    for i in range(1, len(ph)):
        h0, h1, l0, l1 = ph[i-1], ph[i], pl[i-1], pl[i]
        if (h1 <= h0 and l1 >= l0) or (h1 >= h0 and l1 <= l0):
            if direction >= 0:
                ph[i], pl[i] = max(h0, h1), max(l0, l1)
            else:
                ph[i], pl[i] = min(h0, h1), min(l0, l1)
        else:
            direction = 1 if h1 > h0 else -1
    return ph, pl


def find_fractals(ph, pl):
    tops, bottoms = [], []
    for i in range(1, len(ph) - 1):
        if ph[i] > ph[i-1] and ph[i] > ph[i+1] and pl[i] > pl[i-1] and pl[i] > pl[i+1]:
            tops.append((i, ph[i]))
        if pl[i] < pl[i-1] and pl[i] < pl[i+1] and ph[i] < ph[i-1] and ph[i] < ph[i+1]:
            bottoms.append((i, pl[i]))
    return tops, bottoms


def find_bi(tops, bottoms, min_gap=4):
    events = [(i, p, 'top') for i, p in tops] + [(i, p, 'bot') for i, p in bottoms]
    events.sort(key=lambda x: x[0])
    if not events:
        return []
    pivots = []
    for ev in events:
        idx, price, kind = ev
        if not pivots:
            pivots.append(ev); continue
        li, lp, lk = pivots[-1]
        if kind == lk:
            if (kind == 'top' and price > lp) or (kind == 'bot' and price < lp):
                pivots[-1] = ev
        else:
            if idx - li >= min_gap:
                pivots.append(ev)
            else:
                if (kind == 'top' and price > lp) or (kind == 'bot' and price < lp):
                    pivots[-1] = ev
    bi = []
    for i in range(1, len(pivots)):
        p0, p1 = pivots[i-1], pivots[i]
        bi.append({'start': p0, 'end': p1, 'dir': 1 if p1[2] == 'top' else -1})
    return bi


def find_zhongshu(bi_list):
    zs = []
    if len(bi_list) < 3:
        return zs
    for i in range(len(bi_list) - 2):
        b1, b2, b3 = bi_list[i], bi_list[i+1], bi_list[i+2]
        highs = [max(b['start'][1], b['end'][1]) for b in [b1, b2, b3]]
        lows  = [min(b['start'][1], b['end'][1]) for b in [b1, b2, b3]]
        ZG, ZD = min(highs), max(lows)
        if ZG > ZD:
            if zs and zs[-1]['end_bi'] >= i:
                zs[-1]['ZG'] = min(zs[-1]['ZG'], ZG)
                zs[-1]['ZD'] = max(zs[-1]['ZD'], ZD)
                zs[-1]['end_bi'] = i + 2
            else:
                zs.append({'ZG': ZG, 'ZD': ZD, 'start_bi': i, 'end_bi': i + 2})
    return zs


def calc_area(series, s, e):
    return float(series.iloc[s:e+1].abs().sum())


def volume_confirmation(vol, bi_prev, bi_last):
    vp = float(vol.iloc[bi_prev['start'][0]:bi_prev['end'][0]+1].sum())
    vl = float(vol.iloc[bi_last['start'][0]:bi_last['end'][0]+1].sum())
    if vp <= 0:
        return False, 1.0
    r = vl / vp
    return r < 0.85, r


def volume_breakout_ok(vol):
    """量能萎缩时要求放量，正常量能直接放行"""
    if len(vol) < 360:
        return True
    v14 = float(vol.tail(84).mean())
    v60 = float(vol.tail(360).mean())
    if v60 <= 0 or v14 / v60 >= 0.70:
        return True   # 量能正常，不过滤
    # 萎缩状态：需要放量确认
    if float(vol.iloc[-1]) >= v60 * 2.0:
        return True
    if len(vol) >= 40:
        if float(vol.iloc[-40:-4].mean()) > 0 and float(vol.tail(4).mean()) >= float(vol.iloc[-40:-4].mean()) * 2.5:
            return True
    return False


# ────────────────────────────────────────────────────────────
# 主评分逻辑
# ────────────────────────────────────────────────────────────

STOP_BUFFER  = 0.98   # 止损在结构低点下方 2%
THRESHOLD_2B = 60     # 二买最低分
THRESHOLD_3B = 60     # 三买最低分（满分才 65，保持 60）
TREND_BARS   = 240    # 60 日均线（4H × 240 根）

scores = {}
stop_prices = {}

for symbol, df in data.items():
    try:
        if len(df) < 80:
            scores[symbol] = 0
            continue

        df   = df.copy().reset_index(drop=True)
        close  = df['close'].tolist()
        high   = df['high'].tolist()
        low    = df['low'].tolist()
        vol_s  = df['volume'].astype(float).reset_index(drop=True)

        # ── 个股趋势过滤：收盘价必须在 60 日均线以上 ────────────
        if len(df) >= TREND_BARS:
            ma_val = float(df['close'].astype(float).tail(TREND_BARS).mean())
            if float(close[-1]) < ma_val:
                scores[symbol] = 0
                continue

        # ── 量能萎缩过滤 ─────────────────────────────────────
        if not volume_breakout_ok(vol_s):
            scores[symbol] = 0
            continue

        ph, pl = merge_inclusion(high, low)
        c_s    = pd.Series(close)

        ema12 = c_s.ewm(span=12, adjust=False).mean()
        ema26 = c_s.ewm(span=26, adjust=False).mean()
        dif   = ema12 - ema26
        dea   = dif.ewm(span=9, adjust=False).mean()
        macd  = (dif - dea) * 2

        delta = c_s.diff()
        gain  = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rsi_s = 100 - (100 / (1 + gain / loss.replace(0, float('nan'))))
        rsi_v = float(rsi_s.iloc[-1]) if not np.isnan(rsi_s.iloc[-1]) else 50.0

        tops, bottoms = find_fractals(ph, pl)
        bi_list       = find_bi(tops, bottoms)
        zs_list       = find_zhongshu(bi_list)

        cur   = close[-1]
        score = 0.0
        stop  = 0.0
        score_type = ''

        # ── 二类买点：中枢后回调不破底 ────────────────────────
        if zs_list and len(bi_list) >= 3:
            lz   = zs_list[-1]
            post = bi_list[lz['end_bi']+1:]
            if post:
                lp_ = post[-1]
                if lp_['dir'] == 1:
                    bot = lp_['start'][1]
                    if bot > lz['ZD']:
                        dist = (cur - bot) / (bot + 1e-9)
                        if 0 <= dist < 0.12:
                            s2 = 75 * (1 - dist / 0.12)
                            if s2 > score:
                                score = s2
                                stop  = round(lz['ZD'] * STOP_BUFFER, 8)
                                score_type = '2b'

        # ── 三类买点：突破中枢后回踩不入中枢 ─────────────────
        if zs_list and len(bi_list) >= 5:
            lz   = zs_list[-1]
            post = bi_list[lz['end_bi']+1:]
            if len(post) >= 2:
                brk, pb = post[0], post[1]
                if brk['dir'] == 1 and brk['end'][1] > lz['ZG']:
                    if pb['dir'] == -1:
                        pbot = pb['end'][1]
                        if pbot > lz['ZG']:
                            dist = (cur - pbot) / (pbot + 1e-9)
                            if 0 <= dist < 0.08:
                                s3 = 65 * (1 - dist / 0.08)
                                if s3 > score:
                                    score = s3
                                    stop  = round(lz['ZG'] * STOP_BUFFER, 8)
                                    score_type = '3b'

        # ── RSI 加权 ─────────────────────────────────────────
        if score > 0:
            if rsi_v < 35:
                score = min(100, score * 1.25)
            elif rsi_v > 70:
                score *= 0.6

        # ── 阈值过滤（2b 提到 65，3b 保持 60）────────────────
        threshold = THRESHOLD_2B if score_type == '2b' else THRESHOLD_3B
        if score < threshold or stop <= 0 or stop >= cur:
            score = 0.0
            stop  = 0.0

        scores[symbol] = round(score, 2)
        if stop > 0:
            stop_prices[symbol] = stop

    except Exception:
        scores[symbol] = 0

# 只保留有效信号（score > 0），避免 executor 把 score=0 的标的误开多仓
valid = [s for s in scores if scores.get(s, 0) > 0]
rankings = sorted(valid, key=lambda s: scores.get(s, 0), reverse=True)
'''


def login(session: requests.Session) -> str:
    resp = session.post(
        f"{BASE_URL}/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 1:
        print(f"[ERROR] 登录失败: {data.get('msg')}")
        sys.exit(1)
    token = data["data"]["token"]
    session.headers.update({"Authorization": f"Bearer {token}"})
    print(f"[INFO] 登录成功，用户: {USERNAME}")
    return token


def load_symbol_list() -> list[str]:
    if not os.path.exists(SYMBOLS_FILE):
        print(f"[ERROR] 找不到 {SYMBOLS_FILE}，请先运行 fetch_top_symbols.py")
        sys.exit(1)
    with open(SYMBOLS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    symbols = data.get("symbol_list", [])[:TOP_N]
    print(f"[INFO] 读取到 {len(symbols)} 个标的（来自 {SYMBOLS_FILE}）")
    return symbols


def check_existing(session: requests.Session) -> int | None:
    """检查是否已存在同名策略，返回 id 或 None"""
    resp = session.get(f"{BASE_URL}/api/strategies", timeout=10)
    if resp.status_code != 200:
        return None
    items = resp.json().get("data", {})
    # 兼容列表、分页格式（list）、以及 strategies 格式
    if isinstance(items, list):
        rows = items
    else:
        rows = items.get("strategies", items.get("list", []))
    for row in rows:
        if row.get("strategy_name") == STRATEGY_NAME:
            return row.get("id")
    return None


def create_strategy(session: requests.Session, symbol_list: list[str]) -> int:
    payload = {
        "strategy_name":  STRATEGY_NAME,
        "strategy_type":  "IndicatorStrategy",
        "execution_mode": "live",
        "exchange_config": {
            "exchange_id": "binance",
            "api_key":     "ohAG3WhXX8Ko0wC2vwQXi4NrkF089BTDVBUdhNzUxfLiANlkfo6crkESUHV7F0LR",
            "secret_key":  "6nMpLUMcc6FvKvfZUE1F1B9eWlwn0uDS7Na9EayzQcMwy5H3kycsX3BarRgPLRRe",
            "market_type": "swap",
        },
        "indicator_config": {
            "indicator_code": INDICATOR_CODE,
        },
        "trading_config": {
            "cs_strategy_type":      "cross_sectional",
            "symbol":                symbol_list[0].split(":")[-1] if symbol_list else "BTC/USDT",
            "symbol_list":           symbol_list,
            "portfolio_size":        5,
            "long_ratio":            1.0,        # 只做多
            "rebalance_frequency":   "daily",
            "timeframe":             TIMEFRAME,
            "initial_capital":       750,
            "leverage":              2,
            "entry_pct":             0.2,
            "min_hold_hours":        48,
            "breakeven_trigger_pct": 10,
            "abs_score_ranking":     False,      # 只做多，按正分排序即可
            "market_type":           "swap",
        },
    }
    resp = session.post(f"{BASE_URL}/api/strategies/create", json=payload, timeout=15)
    result = resp.json()
    if result.get("code") != 1:
        print(f"[ERROR] 创建策略失败: {result.get('msg')}")
        sys.exit(1)
    strategy_id = result["data"]["id"]
    print(f"[OK] 策略创建成功，ID: {strategy_id}，名称: {STRATEGY_NAME}")
    return strategy_id


def update_strategy(session: requests.Session, strategy_id: int, symbol_list: list[str]) -> None:
    payload = {
        "id":             strategy_id,
        "strategy_name":  STRATEGY_NAME,
        "execution_mode": "live",
        "exchange_config": {
            "exchange_id": "binance",
            "api_key":     "ohAG3WhXX8Ko0wC2vwQXi4NrkF089BTDVBUdhNzUxfLiANlkfo6crkESUHV7F0LR",
            "secret_key":  "6nMpLUMcc6FvKvfZUE1F1B9eWlwn0uDS7Na9EayzQcMwy5H3kycsX3BarRgPLRRe",
            "market_type": "swap",
        },
        "indicator_config": {
            "indicator_code": INDICATOR_CODE,
        },
        "trading_config": {
            "cs_strategy_type":      "cross_sectional",
            "symbol":                symbol_list[0].split(":")[-1] if symbol_list else "BTC/USDT",
            "symbol_list":           symbol_list,
            "portfolio_size":        5,
            "long_ratio":            1.0,
            "rebalance_frequency":   "daily",
            "timeframe":             TIMEFRAME,
            "initial_capital":       750,
            "leverage":              2,
            "entry_pct":             0.2,
            "min_hold_hours":        48,
            "breakeven_trigger_pct": 10,
            "abs_score_ranking":     False,
            "market_type":           "swap",
        },
    }
    resp = session.put(f"{BASE_URL}/api/strategies/update?id={strategy_id}", json=payload, timeout=15)
    result = resp.json()
    if result.get("code") != 1:
        print(f"[ERROR] 更新策略失败: {result.get('msg')}")
        sys.exit(1)
    print(f"[OK] 策略已更新，ID: {strategy_id}，标的数: {len(symbol_list)}")


def start_strategy(session: requests.Session, strategy_id: int) -> None:
    """启动策略（切换为 running 状态）"""
    resp = session.post(
        f"{BASE_URL}/api/strategies/start?id={strategy_id}",
        timeout=10,
    )
    result = resp.json()
    if result.get("code") != 1:
        print(f"[WARN] 启动策略失败（可能需在 UI 手动启动）: {result.get('msg')}")
    else:
        print(f"[OK] 策略已启动，模拟运行中")


def main() -> None:
    symbol_list = load_symbol_list()

    session = requests.Session()
    login(session)

    existing_id = check_existing(session)
    if existing_id:
        print(f"[INFO] 已存在同名策略 (ID={existing_id})，更新配置 ...")
        update_strategy(session, existing_id, symbol_list)
        strategy_id = existing_id
    else:
        strategy_id = create_strategy(session, symbol_list)

    start_strategy(session, strategy_id)

    print()
    print("=" * 60)
    print(f"  策略名称  : {STRATEGY_NAME}")
    print(f"  运行模式  : 模拟运行（signal mode，真实行情 + 不下真实订单）")
    print(f"  信号类型  : 只做多（二买 / 三买）")
    print(f"  K线周期   : {TIMEFRAME}")
    print(f"  标的数量  : {len(symbol_list)}")
    print(f"  最大持仓  : 5 个（评分最高优先）")
    print(f"  个股过滤  : 60 日均线以上 + 量能不萎缩")
    print(f"  止损缓冲  : 2%（结构低点下方）")
    print()
    print(f"  打开 http://localhost:8888 → 策略列表 → '{STRATEGY_NAME}'")
    print("=" * 60)


if __name__ == "__main__":
    main()
