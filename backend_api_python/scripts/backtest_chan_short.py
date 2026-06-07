"""
backtest_chan_short.py
2026-01-01 ~ 今日  缠论截面策略逐日回测（仅做空）

空头信号：
  二类卖点（2s）- 中枢后反弹未过顶，再度下行
  三类卖点（3s）- 跌破中枢后反弹未回，站稳下方

开仓过滤：
  1. 收盘价须在 60 日均线以下（趋势向下）
  2. 量能过滤：量能萎缩时须放量才允许入场
  3. 止损位必须在当前价上方

止损：结构高点上方 2%，阶梯移动保本/锁利
出场：止损 / 量缩横盘（量缩+价格未有效下行）

用法:
  docker exec quantdinger-backend python scripts/backtest_chan_short.py
  本地: PROXY_URL=http://127.0.0.1:7890 python scripts/backtest_chan_short.py

环境变量:
  NO_FETCH=1   只用本地缓存，缺失标的直接跳过（离线模式）
"""
import os, sys, json, time as _time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt")
    sys.exit(1)

from pathlib import Path

# ── 配置 ─────────────────────────────────────────────────────────────────────
PROXY_URL       = os.environ.get("PROXY_URL", "")
SYMBOLS_FILE    = os.environ.get(
    "SYMBOLS_FILE",
    os.path.join(os.path.dirname(__file__), "top_symbols_output.json"),
)
CACHE_DIR       = Path(os.path.dirname(__file__)) / "kline_cache"
NO_FETCH        = os.environ.get("NO_FETCH", "0") == "1"
SIM_START       = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIM_END         = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
WARMUP_DAYS     = 60          # SIM_START 前 60 天起拉数据作为暖机
TIMEFRAME       = "4h"
REBALANCE_HOURS_UTC = [16, 4]   # 00:05 CST=16:05 UTC, 12:05 CST=04:05 UTC
PORTFOLIO_SIZE  = 5
INITIAL_CAPITAL = 10_000.0
LEVERAGE        = 2
ENTRY_PCT       = 0.20
MIN_HOLD_HOURS  = 72
BREAKEVEN_PCT   = 0.10        # 浮盈 10% 后止损下移至成本
STOP_MULT       = 1.02        # 止损缓冲：结构高点上方 2%
MAX_SYMBOLS     = 100
COOLDOWN_DAYS   = 5           # 止损/量缩平仓后同标的冷却天数

# 信号阈值（仅二卖/三卖）
THRESHOLD_2S    = 60          # 二类卖点入选分
THRESHOLD_3S    = 60          # 三类卖点入选分

# 趋势过滤
TREND_MA_BARS   = 240         # 60 日均线（4H × 240 根）

# 量缩横盘出场（空头：未有效下跌 + 量缩 → 动能丧失）
VOL_CONSOL_DAYS   = 7
VOL_CONSOL_PCT    = 0.08      # 近期低点未跌超入场价 -8% 视为横盘
VOL_SHRINK_RATIO  = 0.60      # 近期均量 < 突破量 × 60% 视为量缩
VOL_MIN_HOLD_DAYS = 3

# ─────────────────────────────────────────────────────────────────────────────


def build_exchange() -> ccxt.Exchange:
    opts = {"enableRateLimit": True}
    if PROXY_URL:
        opts["proxies"] = {"http": PROXY_URL, "https": PROXY_URL}
    return ccxt.binanceusdm(opts)


def cache_path(symbol: str) -> Path:
    return CACHE_DIR / (symbol.replace("/", "_") + ".csv")


def load_cache(symbol: str) -> pd.DataFrame:
    p = cache_path(symbol)
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(p, dtype={"ts": int})
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.sort_values("ts").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def fetch_ohlcv_full(exchange: ccxt.Exchange, symbol: str,
                     since_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    since_ms = int(since_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)
    all_bars = []
    cur = since_ms
    while cur < end_ms:
        try:
            bars = exchange.fetch_ohlcv(symbol, TIMEFRAME, since=cur, limit=500)
        except Exception:
            break
        if not bars:
            break
        all_bars.extend(bars)
        cur = bars[-1][0] + 1
        if len(bars) < 500:
            break
        _time.sleep(0.2)

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df[df["ts"] < end_ms].copy()
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def load_symbol_data(exchange, symbol: str,
                     since_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    cached   = load_cache(symbol)
    since_ms = int(since_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)

    if not cached.empty:
        cache_start = int(cached["ts"].iloc[0])
        cache_end   = int(cached["ts"].iloc[-1])
        need_prepend = cache_start > since_ms
        need_append  = cache_end < end_ms - 4 * 3600 * 1000

        if not need_prepend and not need_append:
            mask = (cached["ts"] >= since_ms) & (cached["ts"] < end_ms)
            return cached[mask].reset_index(drop=True)

        if NO_FETCH:
            mask = (cached["ts"] >= since_ms) & (cached["ts"] < end_ms)
            return cached[mask].reset_index(drop=True)

        parts = [cached]
        if need_prepend:
            pre = fetch_ohlcv_full(exchange, symbol, since_dt,
                                   datetime.fromtimestamp(cache_start / 1000, tz=timezone.utc))
            if not pre.empty:
                parts.insert(0, pre)
        if need_append:
            post = fetch_ohlcv_full(
                exchange, symbol,
                datetime.fromtimestamp((cache_end + 1) / 1000, tz=timezone.utc),
                end_dt,
            )
            if not post.empty:
                parts.append(post)

        merged = pd.concat(parts, ignore_index=True)
        merged = merged.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        merged[["ts", "open", "high", "low", "close", "volume"]].to_csv(
            cache_path(symbol), index=False
        )
        mask = (merged["ts"] >= since_ms) & (merged["ts"] < end_ms)
        return merged[mask].reset_index(drop=True)

    if NO_FETCH:
        return pd.DataFrame()

    df = fetch_ohlcv_full(exchange, symbol, since_dt, end_dt)
    if not df.empty:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df[["ts", "open", "high", "low", "close", "volume"]].to_csv(
            cache_path(symbol), index=False
        )
    return df


# ── 缠论核心函数（与多头版一致）──────────────────────────────────────────────

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
            pivots.append(ev)
            continue
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


def _volume_breakout_ok(vol: pd.Series) -> bool:
    """量能萎缩时要求放量，正常量能直接放行（与多头版一致）。"""
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


# ── 空头专用函数 ──────────────────────────────────────────────────────────────

def score_symbol_short(df: pd.DataFrame) -> Tuple[float, float, str]:
    """
    返回 (score, stop_price, signal_type)，仅输出空头信号。
    signal_type: '2s'=二卖  '3s'=三卖  ''=无信号
    stop_price: 止损位，在入场价上方（空头止损方向）
    """
    if len(df) < 80:
        return 0.0, 0.0, ''

    close = df['close'].tolist()
    high  = df['high'].tolist()
    low   = df['low'].tolist()
    vol   = df['volume'].astype(float).reset_index(drop=True)

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

    cur   = close[-1]
    score = 0.0
    stop  = 0.0
    signal_type = ''

    # ── 二类卖点：中枢后反弹未过ZG，再度下行 ──────────────────────────────
    if zs_list and len(bi_list) >= 3:
        lz   = zs_list[-1]
        post = bi_list[lz['end_bi']+1:]
        if post:
            lp_ = post[-1]
            if lp_['dir'] == -1:               # 最后一笔向下（空头方向）
                top = lp_['start'][1]           # 这笔下跌的起点（反弹顶）
                if top < lz['ZG']:              # 顶部未过中枢顶
                    dist = (top - cur) / (top + 1e-9)   # 当前价距顶部（0=在顶, 正=已跌）
                    if 0 <= dist < 0.12:
                        s2 = 75 * (1 - dist / 0.12)
                        if s2 > score:
                            score = s2
                            stop  = round(lz['ZG'] * STOP_MULT, 8)
                            signal_type = '2s'

    # ── 三类卖点：跌破中枢后反弹未回ZD ────────────────────────────────────
    if zs_list and len(bi_list) >= 5:
        lz   = zs_list[-1]
        post = bi_list[lz['end_bi']+1:]
        if len(post) >= 2:
            brk, pb = post[0], post[1]
            if brk['dir'] == -1 and brk['end'][1] < lz['ZD']:   # 向下突破中枢
                if pb['dir'] == 1:                                 # 反弹笔
                    ptop = pb['end'][1]                            # 反弹顶
                    if ptop < lz['ZD']:                            # 未回中枢
                        dist = (ptop - cur) / (ptop + 1e-9)
                        if 0 <= dist < 0.08:
                            s3 = 65 * (1 - dist / 0.08)
                            if s3 > score:
                                score = s3
                                stop  = round(lz['ZD'] * STOP_MULT, 8)
                                signal_type = '3s'

    # ── RSI 加权（空头：超买强化，超卖减弱）──────────────────────────────
    if score > 0:
        if rsi_v > 65:
            score = min(100, score * 1.25)   # 超买区域 → 空头信号更强
        elif rsi_v < 35:
            score *= 0.6                     # 超卖区域 → 空头信号减弱

    # ── 阈值过滤 ──────────────────────────────────────────────────────────
    thresholds = {'2s': THRESHOLD_2S, '3s': THRESHOLD_3S}
    if score < thresholds.get(signal_type, THRESHOLD_2S):
        return 0.0, 0.0, ''

    return round(score, 2), stop, signal_type


def _short_trail_stop_mult(level: int) -> float:
    """
    空头阶梯止损乘数（止损位 = entry_px × mult，越低 = 锁利越多）。
    level 0: 无阶梯，用结构止损
    level 1: entry × 1.0  保本
    level 2: entry × 0.80 锁利 20%
    level 3: entry × 0.50 锁利 50%
    level 4+: entry × max(0.10, 1 - level * 0.2)
    """
    if level == 1: return 1.0
    if level == 2: return 0.80
    if level == 3: return 0.50
    return max(0.10, 1.0 - level * 0.20)


# ── 回测主逻辑 ────────────────────────────────────────────────────────────────

def _pos_pnl_short(pos: Dict, cur_px: float) -> Tuple[float, float]:
    """返回 (pnl_pct, pnl_usd)，空头方向。"""
    entry_px = pos['entry_price']
    pnl_pct  = (entry_px - cur_px) / entry_px * 100
    pnl_usd  = pnl_pct / 100 * (INITIAL_CAPITAL * ENTRY_PCT * LEVERAGE)
    return round(pnl_pct, 2), round(pnl_usd, 2)


def run_backtest_short(symbol_map: Dict[str, pd.DataFrame]) -> None:
    # 构建调仓时间点列表（每天 00:05 和 12:05 北京时间 → 16:05 UTC 和 04:05 UTC）
    rebalance_slots = []
    d = SIM_START
    while d <= SIM_END:
        for h in REBALANCE_HOURS_UTC:
            slot_candidate = d.replace(hour=h, minute=5, second=0, microsecond=0)
            if SIM_START <= slot_candidate <= SIM_END:
                rebalance_slots.append(slot_candidate)
        d += timedelta(days=1)
    rebalance_slots.sort()

    positions: Dict[str, Dict] = {}
    cooldown_until: Dict[str, object] = {}

    equity    = INITIAL_CAPITAL
    trade_log: List[Dict] = []

    print(f"\n{'='*80}")
    print(f"  缠论截面策略回测（仅做空 二卖/三卖）  {SIM_START.date()} → {SIM_END.date()}")
    print(f"  调仓时间: 00:05, 12:05 (北京时间) 共 {len(rebalance_slots)} 个调仓点")
    print(f"  标的数: {len(symbol_map)}  组合上限: {PORTFOLIO_SIZE}  杠杆: {LEVERAGE}×")
    print(f"  单仓: {int(ENTRY_PCT*100)}%  最短持仓: {MIN_HOLD_HOURS}h  保本触发: {int(BREAKEVEN_PCT*100)}%")
    print(f"  止损冷却: {COOLDOWN_DAYS}天  止损缓冲: {int((STOP_MULT-1)*100)}%（高点上方）")
    print(f"{'='*80}\n")

    def get_cur_px(sym: str) -> Optional[float]:
        df_ = symbol_map.get(sym)
        if df_ is None:
            return None
        recent = df_[df_['dt'] < slot]
        if recent.empty:
            return None
        return float(recent.iloc[-1]['close'])

    prev_slot = SIM_START
    for slot in rebalance_slots:
        day_str = slot.strftime("%Y-%m-%d %H:%M")

        cooldown_until = {s: dt for s, dt in cooldown_until.items() if dt > slot}

        # ── 评分所有标的（空头信号）────────────────────────────────────────
        day_scores: Dict[str, float] = {}
        day_stops:  Dict[str, float] = {}
        day_types:  Dict[str, str]   = {}

        for sym, df in symbol_map.items():
            slice_df = df[df['dt'] < slot].copy().reset_index(drop=True)
            if len(slice_df) < 80:
                continue
            try:
                sc, st, stype = score_symbol_short(slice_df)
                if sc > 0:
                    day_scores[sym] = sc
                    day_stops[sym]  = st
                    day_types[sym]  = stype
            except Exception:
                pass

        ranking = sorted(day_scores.keys(), key=lambda s: day_scores[s], reverse=True)

        # ── 1. 止损检查（空头：cur > effective_stop 触发）────────────────
        for sym in list(positions.keys()):
            pos       = positions[sym]
            entry_px  = pos['entry_price']
            struct_sl = pos['stop']           # 止损位在入场价上方
            df_       = symbol_map.get(sym)
            if df_ is None:
                continue
            bars_today = df_[(df_['dt'] >= prev_slot) & (df_['dt'] < slot)]
            if bars_today.empty:
                continue

            hit_bar  = None
            eff_stop = struct_sl
            for _, bar in bars_today.iterrows():
                bar_close = float(bar['close'])
                bar_high  = float(bar['high'])
                float_pct = (entry_px - bar_close) / entry_px * 100   # 空头浮盈

                # 阶梯锁利（只升不降）
                if float_pct >= 300:
                    new_level = int(float_pct // 100) + 2
                elif float_pct >= 200:
                    new_level = 4
                elif float_pct >= 100:
                    new_level = 3
                elif float_pct >= 50:
                    new_level = 2
                elif float_pct >= BREAKEVEN_PCT * 100:
                    new_level = 1
                else:
                    new_level = 0
                if new_level > pos['trail_level']:
                    pos['trail_level'] = new_level

                tl = pos['trail_level']
                if tl > 0:
                    # 空头：有效止损 = min(结构止损, 入场价×乘数) → 价格越低锁利越多
                    eff = min(struct_sl, entry_px * _short_trail_stop_mult(tl))
                else:
                    eff = struct_sl

                # 空头触发：价格升至止损位以上
                struct_hit = bar_high   > struct_sl
                trail_hit  = tl > 0 and bar_close > entry_px * _short_trail_stop_mult(tl)
                if struct_hit or trail_hit:
                    hit_bar, eff_stop = bar, eff
                    break

            if hit_bar is None:
                continue

            pnl_pct, pnl_usd = _pos_pnl_short(pos, eff_stop)
            equity += pnl_usd
            tl = pos.get('trail_level', 0)
            trail_tag = f" [锁利{(tl-1)*20}%]" if tl >= 2 else (" [保本]" if tl == 1 else "")
            trade_log.append({
                'date': day_str, 'action': '止损平仓',
                'symbol': sym, 'price': eff_stop, 'score': pos['score'],
                'stop': eff_stop, 'pnl%': pnl_pct, 'pnl$': pnl_usd,
                'hold_h': round((slot - pos['entry_time']).total_seconds() / 3600, 1),
                'reason': f"破止损 {eff_stop:.6g}{trail_tag}",
            })
            cooldown_until[sym] = slot + timedelta(days=COOLDOWN_DAYS)
            del positions[sym]

        # ── 2. 量缩横盘出场（空头：未有效下跌 + 量缩 → 动能丧失）────────
        consol_bars = VOL_CONSOL_DAYS * 6
        for sym in list(positions.keys()):
            pos = positions[sym]
            held_days = (slot - pos['entry_time']).total_seconds() / 86400
            if held_days < VOL_MIN_HOLD_DAYS:
                continue
            breakout_vol = pos.get('breakout_vol', 0)
            if breakout_vol <= 0:
                continue
            df_ = symbol_map.get(sym)
            if df_ is None:
                continue
            bars_since = df_[(df_['dt'] >= pos['entry_time']) & (df_['dt'] < slot)]
            if len(bars_since) < consol_bars:
                continue
            recent     = bars_since.tail(consol_bars)
            recent_vol = float(recent['volume'].astype(float).mean())
            # 空头：关注最低价是否有效跌破（未跌表示横盘）
            recent_low  = float(recent['close'].astype(float).min())
            entry_px    = pos['entry_price']
            price_no_breakdown = recent_low >= entry_px * (1 - VOL_CONSOL_PCT)
            vol_shrunk         = recent_vol  < breakout_vol * VOL_SHRINK_RATIO
            if not (price_no_breakdown and vol_shrunk):
                continue
            cur_px = get_cur_px(sym)
            if cur_px is None:
                continue
            pnl_pct, pnl_usd = _pos_pnl_short(pos, cur_px)
            equity += pnl_usd
            trade_log.append({
                'date': day_str, 'action': '量缩横盘平仓',
                'symbol': sym, 'price': cur_px, 'score': pos['score'],
                'stop': pos['stop'], 'pnl%': pnl_pct, 'pnl$': pnl_usd,
                'hold_h': round((slot - pos['entry_time']).total_seconds() / 3600, 1),
                'reason': f"量缩横盘 avg_vol={recent_vol:.0f} < breakout×{VOL_SHRINK_RATIO}={breakout_vol*VOL_SHRINK_RATIO:.0f}",
            })
            cooldown_until[sym] = slot + timedelta(days=COOLDOWN_DAYS)
            del positions[sym]

        # ── 3. 开空仓：评分靠前、未持仓、未冷却、有空位 ────────────────
        for sym in ranking:
            if len(positions) >= PORTFOLIO_SIZE:
                break
            if sym in positions or sym in cooldown_until:
                continue

            # 个股趋势过滤：收盘价须在 60 日均线以下（做空）
            df_ = symbol_map.get(sym)
            if df_ is not None:
                snap_close = df_[df_['dt'] < slot]['close'].astype(float)
                if len(snap_close) >= TREND_MA_BARS:
                    if float(snap_close.iloc[-1]) > float(snap_close.tail(TREND_MA_BARS).mean()):
                        continue

            # 量能过滤
            if df_ is not None:
                snap_vol = df_[df_['dt'] < slot]['volume'].astype(float).reset_index(drop=True)
                if not _volume_breakout_ok(snap_vol):
                    continue

            cur_px = get_cur_px(sym)
            if cur_px is None:
                continue

            stop = day_stops.get(sym, 0)
            # 止损方向校验：空头止损须在当前价上方
            if stop > 0 and stop <= cur_px:
                continue

            bvol = float(df_[df_['dt'] < slot]['volume'].astype(float).tail(40).max()) \
                   if df_ is not None else 0.0
            positions[sym] = {
                'entry_price': cur_px, 'entry_time': slot,
                'score': day_scores[sym], 'stop': stop,
                'signal_type': day_types.get(sym, ''), 'trail_level': 0,
                'breakout_vol': bvol,
            }
            trade_log.append({
                'date': day_str, 'action': '开空',
                'symbol': sym, 'price': cur_px, 'score': day_scores[sym],
                'stop': stop, 'pnl%': 0, 'pnl$': 0, 'hold_h': 0,
                'reason': f"{day_types.get(sym, '')} score={day_scores[sym]}",
            })

        # ── 打印有操作的时间点 ────────────────────────────────────────────
        day_trades = [t for t in trade_log if t['date'] == day_str]
        if day_trades:
            cd_list = list(cooldown_until.keys())
            print(f"\n{'─'*80}")
            print(f"  {day_str}  持仓: {len(positions)}/{PORTFOLIO_SIZE}"
                  f"  净值: ${equity:,.2f}"
                  + (f"  冷却: {cd_list}" if cd_list else ""))
            for t in day_trades:
                if t['action'] == '开空':
                    print(f"    ▼ 开空 {t['symbol']:<20} @{t['price']:.6g}"
                          f"  score={t['score']:>+8.2f}  止损={t['stop']:.6g}")
                elif t['action'] == '止损平仓':
                    print(f"    ✕ 止损 {t['symbol']:<20} @{t['price']:.6g}"
                          f"  持仓={t['hold_h']}h  {t['pnl%']}%  (${t['pnl$']:.2f})"
                          f"  {t['reason']}")
                elif t['action'] == '量缩横盘平仓':
                    sign = "+" if t['pnl%'] >= 0 else ""
                    print(f"    ◈ 量缩 {t['symbol']:<20} @{t['price']:.6g}"
                          f"  持仓={t['hold_h']}h  {sign}{t['pnl%']}%  ({sign}${t['pnl$']:.2f})"
                          f"  {t['reason']}")

        prev_slot = slot

    # ── 强制平仓所有剩余持仓 ──────────────────────────────────────────────────
    print(f"\n{'─'*80}")
    print(f"  模拟结束，强制平仓：")
    for sym, pos in list(positions.items()):
        df_ = symbol_map.get(sym)
        if df_ is None or df_.empty:
            continue
        cur_px = float(df_.iloc[-1]['close'])
        held_h = (SIM_END - pos['entry_time']).total_seconds() / 3600
        pnl_pct, pnl_usd = _pos_pnl_short(pos, cur_px)
        equity += pnl_usd
        sign = "+" if pnl_pct >= 0 else ""
        print(f"    空 {sym:<20} @{cur_px:.6g}  持仓={held_h:.0f}h  "
              f"{sign}{pnl_pct:.2f}%  ({sign}${pnl_usd:.2f})")
        trade_log.append({
            'date': SIM_END.strftime("%Y-%m-%d"), 'action': '模拟结束平仓',
            'symbol': sym, 'price': cur_px, 'score': pos['score'],
            'stop': pos['stop'], 'pnl%': pnl_pct, 'pnl$': pnl_usd,
            'hold_h': round(held_h, 1), 'reason': '模拟结束',
        })

    # ── 汇总 ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  回测汇总  {SIM_START.date()} → {SIM_END.date()}")
    print(f"{'='*80}")

    all_closed = [t for t in trade_log if t['action'] != '开空']
    opens      = [t for t in trade_log if t['action'] == '开空']
    stops_l    = [t for t in trade_log if t['action'] == '止损平仓']
    consol_l   = [t for t in trade_log if t['action'] == '量缩横盘平仓']
    wins       = [t for t in all_closed if t['pnl%'] > 0]
    loses      = [t for t in all_closed if t['pnl%'] <= 0]
    total_pnl  = sum(t['pnl$'] for t in all_closed)
    win_rate   = len(wins) / len(all_closed) * 100 if all_closed else 0

    print(f"  开空次数     : {len(opens)}")
    print(f"  止损平仓     : {len(stops_l)}")
    print(f"  量缩横盘平仓 : {len(consol_l)}")
    print(f"  盈利次数     : {len(wins)}  亏损次数: {len(loses)}")
    print(f"  胜率         : {win_rate:.1f}%")
    print(f"  总盈亏       : ${total_pnl:+,.2f}")
    print(f"  期末净值     : ${equity:,.2f}  ({(equity/INITIAL_CAPITAL-1)*100:+.2f}%)")
    if wins:
        print(f"  平均盈利     : ${sum(t['pnl$'] for t in wins)/len(wins):,.2f}")
    if loses:
        print(f"  平均亏损     : ${sum(t['pnl$'] for t in loses)/len(loses):,.2f}")

    print(f"\n  完整交易记录（{len(trade_log)} 条）：")
    print(f"  {'日期':<12} {'操作':<8} {'标的':<18} {'价格':>12} {'评分':>9} "
          f"{'止损价':>12} {'持仓h':>7} {'收益%':>8} {'收益$':>9}")
    print(f"  {'-'*97}")
    for t in trade_log:
        sign = "+" if t['pnl%'] > 0 else ""
        print(f"  {t['date']:<12} {t['action']:<8} {t['symbol']:<18} "
              f"{t['price']:>12.6g} {t['score']:>+9.2f} "
              f"{t['stop']:>12.6g} {t['hold_h']:>7.1f} "
              f"{sign+str(t['pnl%'])+'%':>8} {('+' if t['pnl$']>=0 else '')+str(t['pnl$']):>9}")

    out = os.path.join(os.path.dirname(__file__), "backtest_result_short.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {
                "start": SIM_START.isoformat(), "end": SIM_END.isoformat(),
                "opens": len(opens), "stops": len(stops_l),
                "vol_exits": len(consol_l),
                "trades": len(all_closed), "win_rate": round(win_rate, 2),
                "total_pnl": round(total_pnl, 2),
                "final_equity": round(equity, 2),
                "return_pct": round((equity / INITIAL_CAPITAL - 1) * 100, 2),
            },
            "trades": trade_log,
        }, f, indent=2, ensure_ascii=False)
    print(f"\n  详细结果已保存: {out}")
    print(f"{'='*80}\n")


def main():
    if not os.path.exists(SYMBOLS_FILE):
        print(f"[ERROR] 找不到 {SYMBOLS_FILE}，请先运行 fetch_top_symbols.py")
        sys.exit(1)
    with open(SYMBOLS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    symbols_raw = [s.split(":")[-1] for s in raw.get("symbol_list", [])[:MAX_SYMBOLS]]
    print(f"[INFO] 标的列表: {len(symbols_raw)} 个")

    exchange = build_exchange() if not NO_FETCH else None

    fetch_since = SIM_START - timedelta(days=WARMUP_DAYS)
    fetch_end   = SIM_END + timedelta(days=1)

    cached_count = sum(1 for s in symbols_raw if cache_path(s).exists())
    print(f"[INFO] 本地缓存: {cached_count}/{len(symbols_raw)} 个标的已缓存")
    if NO_FETCH:
        print("[INFO] 离线模式（NO_FETCH=1），仅读缓存")
    print(f"[INFO] 数据窗口: {fetch_since.date()} → {fetch_end.date()}\n")

    symbol_map: Dict[str, pd.DataFrame] = {}
    for i, sym in enumerate(symbols_raw, 1):
        print(f"  [{i:>3}/{len(symbols_raw)}] {sym:<22}", end=" ", flush=True)
        df = load_symbol_data(exchange, sym, fetch_since, fetch_end)
        if len(df) >= 20:
            symbol_map[sym] = df
            src = "缓存" if cache_path(sym).exists() else "API "
            print(f"[{src}] {len(df)} 根  ({df['dt'].iloc[0].date()} ~ {df['dt'].iloc[-1].date()})")
        else:
            print("数据不足，跳过")

    print(f"\n[INFO] 有效标的: {len(symbol_map)} 个，开始回测...\n")
    run_backtest_short(symbol_map)


if __name__ == "__main__":
    main()
