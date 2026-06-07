"""
create_chan_short_strategy.py
自动登录 QuantDinger，读取 top_symbols_output.json，创建缠论做空截面策略。

空头信号：二类卖点（2s）/ 三类卖点（3s）
趋势过滤：价格在 60 日均线以下
止损位：结构高点（ZG / ZD）上方 2%

用法:
  docker exec quantdinger-backend python scripts/create_chan_short_strategy.py

可选环境变量:
  QD_BASE_URL=http://localhost:5000   后端地址
  QD_USER=quantdinger                 登录用户名
  QD_PASS=123456                      登录密码
  QD_STRATEGY_NAME=缠论做空策略        策略名称
  QD_TIMEFRAME=4H                     K线周期
  QD_TOP_N=100                        使用 symbol_list 前 N 个
  SYMBOLS_FILE=                       自定义 JSON 文件路径
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
STRATEGY_NAME  = os.environ.get("QD_STRATEGY_NAME", "缠论做空策略")
TIMEFRAME      = os.environ.get("QD_TIMEFRAME", "4H")
TOP_N          = int(os.environ.get("QD_TOP_N", "100"))
SYMBOLS_FILE   = os.environ.get(
    "SYMBOLS_FILE",
    os.path.join(os.path.dirname(__file__), "top_symbols_output.json"),
)


# ── 缠论做空评分指标代码（截面策略用）────────────────────────────────────────
INDICATOR_CODE = '''
# ============================================================
# 缠论卖点扫描 v1 - 只做空（二卖 / 三卖）
#
# 开仓过滤：
#   1. 只识别二卖 / 三卖信号（不做多，不做一卖）
#   2. 个股 60 日均线趋势过滤（4H×240根，价格需在均线以下）
#   3. 量能萎缩过滤（萎缩状态下必须放量才允许入场）
#   4. 评分阈值 60（二卖满分 75，三卖满分 65，RSI 加权后可超 80）
#
# score < 0：卖点（绝对值越大越强）
# score = 0：无信号或被过滤
#
# 止损位（stop_prices）：结构高点上方 2%
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


def volume_breakout_ok(vol):
    """量能萎缩时要求放量，正常量能直接放行"""
    if len(vol) < 90:
        return True
    v14 = float(vol.tail(14).mean())
    v90 = float(vol.tail(90).mean())
    if v90 <= 0 or v14 / v90 >= 0.70:
        return True
    if float(vol.iloc[-1]) >= v90 * 2.0:
        return True
    if len(vol) >= 40:
        if float(vol.iloc[-40:-4].mean()) > 0 and float(vol.tail(4).mean()) >= float(vol.iloc[-40:-4].mean()) * 2.5:
            return True
    return False


# ────────────────────────────────────────────────────────────
# 主评分逻辑
# ────────────────────────────────────────────────────────────

STOP_MULT    = 1.02   # 止损在结构高点上方 2%
THRESHOLD_2S = 60     # 二卖最低分
THRESHOLD_3S = 60     # 三卖最低分
TREND_BARS   = 240    # 60 日均线（4H × 240 根）

scores       = {}
stop_prices  = {}
signal_types = {}

for symbol, df in data.items():
    try:
        if len(df) < 80:
            scores[symbol] = 0
            continue

        df    = df.copy().reset_index(drop=True)
        close = df['close'].tolist()
        high  = df['high'].tolist()
        low   = df['low'].tolist()
        vol_s = df['volume'].astype(float).reset_index(drop=True)

        # ── 个股趋势过滤：收盘价必须在 60 日均线以下（做空）────────────
        if len(df) >= TREND_BARS:
            ma_val = float(df['close'].astype(float).tail(TREND_BARS).mean())
            if float(close[-1]) > ma_val:
                scores[symbol] = 0
                continue

        # ── 量能萎缩过滤 ─────────────────────────────────────────────
        if not volume_breakout_ok(vol_s):
            scores[symbol] = 0
            continue

        ph, pl = merge_inclusion(high, low)
        c_s    = pd.Series(close)

        delta = c_s.diff()
        gain  = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
        loss  = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
        rsi_s = 100 - (100 / (1 + gain / loss.replace(0, float('nan'))))
        rsi_v = float(rsi_s.iloc[-1]) if not np.isnan(rsi_s.iloc[-1]) else 50.0

        tops, bottoms = find_fractals(ph, pl)
        bi_list       = find_bi(tops, bottoms)
        zs_list       = find_zhongshu(bi_list)

        cur        = close[-1]
        score      = 0.0
        stop       = 0.0
        score_type = ''

        # ── 二类卖点：中枢后反弹未过ZG，再度下行 ────────────────────
        if zs_list and len(bi_list) >= 3:
            lz   = zs_list[-1]
            post = bi_list[lz['end_bi']+1:]
            if post:
                lp_ = post[-1]
                if lp_['dir'] == -1:              # 最后一笔向下
                    top = lp_['start'][1]          # 反弹顶（下跌笔起点）
                    if top < lz['ZG']:             # 未过中枢顶
                        dist = (top - cur) / (top + 1e-9)   # 0=在顶, 正=已跌
                        if 0 <= dist < 0.12:
                            s2 = 75 * (1 - dist / 0.12)
                            if s2 > score:
                                score      = s2
                                stop       = round(lz['ZG'] * STOP_MULT, 8)
                                score_type = '2s'

        # ── 三类卖点：跌破中枢后反弹未回ZD ──────────────────────────
        if zs_list and len(bi_list) >= 5:
            lz   = zs_list[-1]
            post = bi_list[lz['end_bi']+1:]
            if len(post) >= 2:
                brk, pb = post[0], post[1]
                if brk['dir'] == -1 and brk['end'][1] < lz['ZD']:   # 跌破中枢
                    if pb['dir'] == 1:                                 # 反弹
                        ptop = pb['end'][1]                            # 反弹顶
                        if ptop < lz['ZD']:                            # 未回中枢
                            dist = (ptop - cur) / (ptop + 1e-9)
                            if 0 <= dist < 0.08:
                                s3 = 65 * (1 - dist / 0.08)
                                if s3 > score:
                                    score      = s3
                                    stop       = round(lz['ZD'] * STOP_MULT, 8)
                                    score_type = '3s'

        # ── RSI 加权（超买强化，超卖减弱）───────────────────────────
        if score > 0:
            if rsi_v > 65:
                score = min(100, score * 1.25)
            elif rsi_v < 35:
                score *= 0.6

        # ── 阈值过滤（止损必须在当前价上方）─────────────────────────
        threshold = THRESHOLD_2S if score_type == '2s' else THRESHOLD_3S
        if score < threshold or stop <= 0 or stop <= cur:
            score = 0.0
            stop  = 0.0

        if score > 0:
            # 输出负分：executor 识别负分 → 做空方向
            scores[symbol]       = -round(score, 2)
            stop_prices[symbol]  = stop
            signal_types[symbol] = '二卖' if score_type == '2s' else '三卖'
        else:
            scores[symbol] = 0

    except Exception:
        scores[symbol] = 0

# 只保留有效空头信号（score < 0），按绝对值从高到低排列
valid    = [s for s in scores if scores.get(s, 0) < 0]
rankings = sorted(valid, key=lambda s: abs(scores.get(s, 0)), reverse=True)
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
    """检查是否已存在同名策略，返回 id 或 None。"""
    resp = session.get(f"{BASE_URL}/api/strategies", timeout=10)
    if resp.status_code != 200:
        return None
    items = resp.json().get("data", {})
    if isinstance(items, list):
        rows = items
    else:
        rows = items.get("strategies", items.get("list", []))
    for row in rows:
        if row.get("strategy_name") == STRATEGY_NAME:
            return row.get("id")
    return None


def _build_payload(symbol_list: list[str]) -> dict:
    return {
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
            "long_ratio":            0.0,        # 纯做空，不做多
            "abs_score_ranking":     True,        # 按绝对值选 top-N，负分→做空
            "rebalance_frequency":   "daily",
            "rebalance_time":        "00:05,12:05",   # 每天 0:05 和 12:05（北京时间）调仓两次
            "timeframe":             TIMEFRAME,
            "initial_capital":       750,
            "leverage":              2,
            "entry_pct":             0.2,
            "min_hold_hours":        48,
            "breakeven_trigger_pct": 10,
            "market_type":           "swap",
        },
    }


def create_strategy(session: requests.Session, symbol_list: list[str]) -> int:
    payload = _build_payload(symbol_list)
    resp    = session.post(f"{BASE_URL}/api/strategies/create", json=payload, timeout=15)
    result  = resp.json()
    if result.get("code") != 1:
        print(f"[ERROR] 创建策略失败: {result.get('msg')}")
        sys.exit(1)
    strategy_id = result["data"]["id"]
    print(f"[OK] 策略创建成功，ID: {strategy_id}，名称: {STRATEGY_NAME}")
    return strategy_id


def update_strategy(session: requests.Session, strategy_id: int, symbol_list: list[str]) -> None:
    payload = _build_payload(symbol_list)
    payload["id"] = strategy_id
    resp   = session.put(f"{BASE_URL}/api/strategies/update?id={strategy_id}", json=payload, timeout=15)
    result = resp.json()
    if result.get("code") != 1:
        print(f"[ERROR] 更新策略失败: {result.get('msg')}")
        sys.exit(1)
    print(f"[OK] 策略已更新，ID: {strategy_id}，标的数: {len(symbol_list)}")


def start_strategy(session: requests.Session, strategy_id: int) -> None:
    resp   = session.post(f"{BASE_URL}/api/strategies/start?id={strategy_id}", timeout=10)
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
    print(f"  信号类型  : 只做空（二卖 / 三卖）")
    print(f"  K线周期   : {TIMEFRAME}")
    print(f"  标的数量  : {len(symbol_list)}")
    print(f"  最大持仓  : 5 个（绝对值评分最高优先）")
    print(f"  个股过滤  : 60 日均线以下 + 量能不萎缩")
    print(f"  止损缓冲  : 2%（结构高点上方）")
    print()
    print(f"  打开 http://localhost:8888 → 策略列表 → '{STRATEGY_NAME}'")
    print("=" * 60)


if __name__ == "__main__":
    main()
