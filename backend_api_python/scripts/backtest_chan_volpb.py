"""
backtest_chan_volpb.py
2025-01-01 ~ 2025-12-31  缠论截面策略回测（二买 / 三买 / vol-pullback 对比）

用法:
  本地: PROXY_URL=http://127.0.0.1:7890 python scripts/backtest_chan_volpb.py
  离线: NO_FETCH=1 python scripts/backtest_chan_volpb.py

说明:
  基于 backtest_chan.py，在 score_symbol() 中加入 vol-pullback 逻辑（与
  indicator_code_v4.1.py 生产代码保持一致），并在汇总中按信号类型拆分统计，
  用于评估是否值得在实盘保留 vol-pullback 信号。
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

# ── 回测时间范围 ──────────────────────────────────────────────────────────────
PROXY_URL       = os.environ.get("PROXY_URL", "")
SYMBOLS_FILE    = os.environ.get(
    "SYMBOLS_FILE",
    os.path.join(os.path.dirname(__file__), "top_symbols_output.json"),
)
CACHE_DIR       = Path(os.path.dirname(__file__)) / "kline_cache"
NO_FETCH        = os.environ.get("NO_FETCH", "0") == "1"
SIM_START       = datetime(2025, 1, 1, tzinfo=timezone.utc)
SIM_END         = datetime(2025, 12, 31, tzinfo=timezone.utc)
WARMUP_DAYS     = 60
TIMEFRAME       = "4h"
REBALANCE_HOURS_UTC = [16, 4]
PORTFOLIO_SIZE  = 5
INITIAL_CAPITAL = 10_000.0
LEVERAGE        = 2
ENTRY_PCT       = 0.20        # 二买/三买仓位
VP_ENTRY_PCT    = 0.10        # vol-pullback 仓位（与生产一致：高风险半仓）
MIN_HOLD_HOURS  = 72
BREAKEVEN_PCT   = 0.10
STOP_BUFFER     = 0.98
MAX_SYMBOLS     = 100
COOLDOWN_DAYS   = 5

# 信号阈值
THRESHOLD_2B    = 60
THRESHOLD_3B    = 60

# 趋势过滤
TREND_MA_BARS   = 240

# 量缩横盘出场
VOL_CONSOL_DAYS   = 7
VOL_CONSOL_PCT    = 0.08
VOL_SHRINK_RATIO  = 0.60
VOL_MIN_HOLD_DAYS = 3

# vol-pullback 信号参数（与 indicator_code_v4.1.py 完全一致）
VP_SPIKE_RATIO   = 3.0    # 放量柱：量 > 60日均量 × 3
VP_SPIKE_GAIN    = 0.10   # 放量柱：涨幅 > 10%
VP_COOLDOWN_BARS = 12     # 放量后观察窗口（12根4H ≈ 2天）
VP_SHRINK_RATIO  = 0.40   # 缩量：近3根量 < 放量柱 × 40%

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
    cached  = load_cache(symbol)
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

        pieces = []
        if need_prepend:
            pre = fetch_ohlcv_full(exchange, symbol, since_dt,
                                   datetime.fromtimestamp(cache_start / 1000, tz=timezone.utc))
            if not pre.empty:
                pieces.append(pre)
        pieces.append(cached)
        if need_append:
            app = fetch_ohlcv_full(exchange, symbol,
                                   datetime.fromtimestamp(cache_end / 1000, tz=timezone.utc),
                                   end_dt)
            if not app.empty:
                pieces.append(app)
        merged = pd.concat(pieces).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        merged["dt"] = pd.to_datetime(merged["dt"] if "dt" in merged else merged["ts"], unit="ms" if "dt" not in merged.columns else None, utc=True)
        mask = (merged["ts"] >= since_ms) & (merged["ts"] < end_ms)
        return merged[mask].reset_index(drop=True)

    if NO_FETCH:
        return pd.DataFrame()
    df = fetch_ohlcv_full(exchange, symbol, since_dt, end_dt)
    return df


# ── 缠论计算（与 backtest_chan.py 完全一致） ───────────────────────────────────

def merge_inclusion(high: list, low: list):
    n = len(high)
    ph, pl = list(high), list(low)
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(ph) - 1:
            h1, l1 = ph[i], pl[i]
            h2, l2 = ph[i+1], pl[i+1]
            if (h1 >= h2 and l1 <= l2) or (h2 >= h1 and l2 <= l1):
                if h1 >= h2:
                    ph[i] = max(h1, h2); pl[i] = max(l1, l2)
                else:
                    ph[i] = max(h1, h2); pl[i] = min(l1, l2)
                ph.pop(i+1); pl.pop(i+1)
                changed = True
            else:
                i += 1
    return ph, pl


def find_fractals(ph: list, pl: list):
    tops, bots = [], []
    for i in range(1, len(ph)-1):
        if ph[i] > ph[i-1] and ph[i] > ph[i+1]:
            tops.append((i, ph[i]))
        if pl[i] < pl[i-1] and pl[i] < pl[i+1]:
            bots.append((i, pl[i]))
    return tops, bots


def find_bi(tops: list, bots: list):
    events = [(i, 'top', v) for i, v in tops] + [(i, 'bot', v) for i, v in bots]
    events.sort(key=lambda x: x[0])
    bi_list = []
    last = None
    for idx, kind, val in events:
        if last is None:
            last = (idx, kind, val)
            continue
        if kind == last[1]:
            if (kind == 'top' and val >= last[2]) or (kind == 'bot' and val <= last[2]):
                last = (idx, kind, val)
        else:
            bi_list.append({
                'dir': 1 if last[1] == 'bot' else -1,
                'start': (last[0], last[2]),
                'end': (idx, val),
            })
            last = (idx, kind, val)
    return bi_list


def find_zhongshu(bi_list: list):
    zs_list = []
    i = 0
    while i + 2 < len(bi_list):
        b0, b1, b2 = bi_list[i], bi_list[i+1], bi_list[i+2]
        h0 = max(b0['start'][1], b0['end'][1])
        l0 = min(b0['start'][1], b0['end'][1])
        h1 = max(b1['start'][1], b1['end'][1])
        l1 = min(b1['start'][1], b1['end'][1])
        h2 = max(b2['start'][1], b2['end'][1])
        l2 = min(b2['start'][1], b2['end'][1])
        ZG = min(h0, h1, h2)
        ZD = max(l0, l1, l2)
        if ZG > ZD:
            zs = {'ZG': ZG, 'ZD': ZD, 'start_bi': i, 'end_bi': i+2}
            j = i + 3
            while j < len(bi_list):
                bj = bi_list[j]
                bj_h = max(bj['start'][1], bj['end'][1])
                bj_l = min(bj['start'][1], bj['end'][1])
                if bj_h >= ZD and bj_l <= ZG:
                    ZG = min(ZG, bj_h)
                    ZD = max(ZD, bj_l)
                    zs['end_bi'] = j
                    zs['ZG'] = ZG
                    zs['ZD'] = ZD
                    j += 1
                else:
                    break
            zs_list.append(zs)
            i = zs['end_bi'] + 1
        else:
            i += 1
    return zs_list


def _volume_breakout_ok(vol: pd.Series) -> bool:
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


def score_symbol(df: pd.DataFrame) -> Tuple[float, float, str]:
    """返回 (score, stop_price, signal_type)
    signal_type: '2b' | '3b' | 'vol-pullback' | ''
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

    # ── 二类买点 ──────────────────────────────────────────────────────────────
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
                            score, stop, signal_type = s2, round(lz['ZD'] * STOP_BUFFER, 8), '2b'

    # ── 三类买点 ──────────────────────────────────────────────────────────────
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
                                score, stop, signal_type = s3, round(lz['ZG'] * STOP_BUFFER, 8), '3b'

    # ── RSI 加权（二买/三买） ─────────────────────────────────────────────────
    if score > 0:
        if rsi_v < 35:
            score = min(100, score * 1.25)
        elif rsi_v > 70:
            score *= 0.6

    # ── vol-pullback（方案三：放量大阳后缩量回踩） ────────────────────────────
    if len(df) >= 180:
        _v60 = float(vol.tail(360).mean()) if len(vol) >= 360 else float(vol.mean())
        if _v60 > 0:
            _window = df.tail(VP_COOLDOWN_BARS + 1).iloc[:-1]
            _spike_vol = 0.0; _spike_low = 0.0; _spike_high = 0.0; _spike_idx = -1
            for _ri in range(len(_window) - 1, -1, -1):
                _row  = _window.iloc[_ri]
                _vi   = float(_row['volume'])
                _o    = float(_row['open'])
                _ci   = float(_row['close'])
                _gain = (_ci - _o) / max(_o, 1e-9)
                if _vi >= _v60 * VP_SPIKE_RATIO and _gain >= VP_SPIKE_GAIN:
                    _spike_vol  = _vi
                    _spike_low  = float(_row['low'])
                    _spike_high = float(_row['high'])
                    _spike_idx  = _window.index[_ri]
                    break
            if _spike_vol > 0:
                _recent3 = vol.tail(3)
                _shrink  = float(_recent3.mean()) < _spike_vol * VP_SHRINK_RATIO
                _intact  = cur >= _spike_low
                _bars_after = df[df.index > _spike_idx]
                _post_low   = float(_bars_after['low'].min()) if len(_bars_after) > 0 else cur
                _support_broken = _post_low < _spike_low
                _drawdown_from_spike = (_spike_high - cur) / _spike_high if _spike_high > 0 else 0
                _excessive_drop = _drawdown_from_spike > 0.20
                if _shrink and _intact and not _support_broken and not _excessive_drop:
                    _vs = 65.0
                    if rsi_v < 35:
                        _vs = min(100, _vs * 1.25)
                    elif rsi_v > 70:
                        _vs *= 0.6
                    _vstop = round(_spike_low * STOP_BUFFER, 8)
                    if _vstop > 0 and _vstop < cur and _vs >= 60 and _vs > score:
                        score      = _vs
                        stop       = _vstop
                        signal_type = 'vol-pullback'

    # ── 阈值过滤 ──────────────────────────────────────────────────────────────
    threshold = THRESHOLD_2B if signal_type in ('2b', '') else THRESHOLD_3B
    if score < threshold:
        return 0.0, 0.0, ''

    return round(score, 2), stop, signal_type


def _trail_stop_mult(level: int) -> float:
    if level == 1: return 1.0
    if level == 2: return 1.2
    if level == 3: return 1.5
    return float(level - 2)


def run_backtest(symbol_map: Dict[str, pd.DataFrame]) -> None:
    rebalance_slots = []
    d = SIM_START.replace(hour=0, minute=0, second=0, microsecond=0)
    while d <= SIM_END:
        for h in sorted(REBALANCE_HOURS_UTC):
            slot = d.replace(hour=h, minute=5)
            if SIM_START <= slot <= SIM_END:
                rebalance_slots.append(slot)
        d += timedelta(days=1)
    rebalance_slots.sort()

    positions: Dict[str, Dict] = {}
    cooldown_until: Dict[str, object] = {}
    equity    = INITIAL_CAPITAL
    trade_log: List[Dict] = []

    print(f"\n{'='*80}")
    print(f"  缠论策略回测（二买 / 三买 / vol-pullback 对比）  {SIM_START.date()} → {SIM_END.date()}")
    print(f"  标的数: {len(symbol_map)}  组合上限: {PORTFOLIO_SIZE}  杠杆: {LEVERAGE}×")
    print(f"  二买/三买仓位: {int(ENTRY_PCT*100)}%  vol-pullback仓位: {int(VP_ENTRY_PCT*100)}%")
    print(f"  止损冷却: {COOLDOWN_DAYS}天  止损缓冲: {int((1-STOP_BUFFER)*100)}%")
    cst_desc = ', '.join(f"{(h+8)%24:02d}:05" for h in sorted(REBALANCE_HOURS_UTC))
    print(f"  调仓时间: {cst_desc} (北京时间)  共 {len(rebalance_slots)} 个调仓点")
    print(f"{'='*80}\n")

    def get_entry_pct(stype: str) -> float:
        return VP_ENTRY_PCT if stype == 'vol-pullback' else ENTRY_PCT

    def pos_pnl(pos: Dict, cur_px: float) -> Tuple[float, float]:
        entry_px  = pos['entry_price']
        ep        = pos.get('entry_pct', ENTRY_PCT)
        pnl_pct   = (cur_px - entry_px) / entry_px * 100
        pnl_usd   = pnl_pct / 100 * (INITIAL_CAPITAL * ep * LEVERAGE)
        return round(pnl_pct, 2), round(pnl_usd, 2)

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

        day_scores: Dict[str, float] = {}
        day_stops:  Dict[str, float] = {}
        day_types:  Dict[str, str]   = {}

        for sym, df in symbol_map.items():
            slice_df = df[df['dt'] < slot].copy().reset_index(drop=True)
            if len(slice_df) < 80:
                continue
            try:
                sc, st, stype = score_symbol(slice_df)
                if sc > 0:
                    day_scores[sym] = sc
                    day_stops[sym]  = st
                    day_types[sym]  = stype
            except Exception:
                pass

        ranking = sorted(day_scores.keys(), key=lambda s: day_scores[s], reverse=True)

        # ── 1. 止损检查 ────────────────────────────────────────────────────────
        for sym in list(positions.keys()):
            pos       = positions[sym]
            entry_px  = pos['entry_price']
            struct_sl = pos['stop']
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
                bar_low   = float(bar['low'])
                float_pct = (bar_close / entry_px - 1) * 100

                if float_pct >= 300:   new_level = 5
                elif float_pct >= 200: new_level = 4
                elif float_pct >= 100: new_level = 3
                elif float_pct >= 50:  new_level = 2
                elif float_pct >= BREAKEVEN_PCT * 100: new_level = 1
                else:                  new_level = 0
                if new_level > pos['trail_level']:
                    pos['trail_level'] = new_level

                tl = pos['trail_level']
                if tl > 0:
                    eff_stop = max(struct_sl, entry_px * _trail_stop_mult(tl))
                else:
                    eff_stop = struct_sl
                struct_hit = bar_low   < struct_sl
                trail_hit  = tl > 0 and bar_close < entry_px * _trail_stop_mult(tl)
                if struct_hit or trail_hit:
                    hit_bar = bar
                    break

            if hit_bar is None:
                continue

            pnl_pct, pnl_usd = pos_pnl(pos, eff_stop)
            equity += pnl_usd
            tl = pos.get('trail_level', 0)
            trail_tag = f" [锁利{(tl-1)*100}%]" if tl >= 2 else (" [保本]" if tl == 1 else "")
            trade_log.append({
                'date': day_str, 'action': '止损平仓',
                'symbol': sym, 'price': eff_stop, 'score': pos['score'],
                'signal_type': pos.get('signal_type', ''),
                'stop': eff_stop, 'pnl%': pnl_pct, 'pnl$': pnl_usd,
                'hold_h': round((slot - pos['entry_time']).total_seconds() / 3600, 1),
                'reason': f"破止损 {eff_stop:.6g}{trail_tag}",
            })
            cooldown_until[sym] = slot + timedelta(days=COOLDOWN_DAYS)
            del positions[sym]

        # ── 2. 量缩横盘出场 ────────────────────────────────────────────────────
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
            recent      = bars_since.tail(consol_bars)
            recent_vol  = float(recent['volume'].astype(float).mean())
            recent_high = float(recent['close'].astype(float).max())
            entry_px    = pos['entry_price']
            if not (recent_high <= entry_px * (1 + VOL_CONSOL_PCT) and
                    recent_vol  <  breakout_vol * VOL_SHRINK_RATIO):
                continue
            cur_px = get_cur_px(sym)
            if cur_px is None:
                continue
            pnl_pct, pnl_usd = pos_pnl(pos, cur_px)
            equity += pnl_usd
            trade_log.append({
                'date': day_str, 'action': '量缩横盘平仓',
                'symbol': sym, 'price': cur_px, 'score': pos['score'],
                'signal_type': pos.get('signal_type', ''),
                'stop': pos['stop'], 'pnl%': pnl_pct, 'pnl$': pnl_usd,
                'hold_h': round((slot - pos['entry_time']).total_seconds() / 3600, 1),
                'reason': f"量缩横盘",
            })
            cooldown_until[sym] = slot + timedelta(days=COOLDOWN_DAYS)
            del positions[sym]

        # ── 3. 开仓 ────────────────────────────────────────────────────────────
        for sym in ranking:
            if len(positions) >= PORTFOLIO_SIZE:
                break
            if sym in positions or sym in cooldown_until:
                continue
            stype = day_types.get(sym, '')
            df_ = symbol_map.get(sym)
            if df_ is not None:
                snap_close = df_[df_['dt'] < slot]['close'].astype(float)
                if len(snap_close) >= TREND_MA_BARS:
                    if float(snap_close.iloc[-1]) < float(snap_close.tail(TREND_MA_BARS).mean()):
                        continue
            if df_ is not None:
                snap_vol = df_[df_['dt'] < slot]['volume'].astype(float).reset_index(drop=True)
                if not _volume_breakout_ok(snap_vol):
                    continue
            cur_px = get_cur_px(sym)
            if cur_px is None:
                continue
            stop = day_stops.get(sym, 0)
            if stop > 0 and stop >= cur_px:
                continue
            ep   = get_entry_pct(stype)
            bvol = float(df_[df_['dt'] < slot]['volume'].astype(float).tail(40).max()) \
                   if df_ is not None else 0.0
            positions[sym] = {
                'entry_price': cur_px, 'entry_time': slot,
                'score': day_scores[sym], 'stop': stop,
                'signal_type': stype, 'trail_level': 0,
                'breakout_vol': bvol, 'entry_pct': ep,
            }
            trade_log.append({
                'date': day_str, 'action': '开仓',
                'symbol': sym, 'price': cur_px, 'score': day_scores[sym],
                'signal_type': stype,
                'stop': stop, 'pnl%': 0, 'pnl$': 0, 'hold_h': 0,
                'reason': f"{stype} score={day_scores[sym]} pct={int(ep*100)}%",
            })

        # ── 打印有操作的时间点 ────────────────────────────────────────────────
        day_trades = [t for t in trade_log if t['date'] == day_str]
        if day_trades:
            print(f"\n{'─'*80}")
            print(f"  {day_str}  持仓: {len(positions)}/{PORTFOLIO_SIZE}  净值: ${equity:,.2f}")
            for t in day_trades:
                stype_tag = f"[{t.get('signal_type','?')}]"
                if t['action'] == '开仓':
                    print(f"    ▶ 开多 {t['symbol']:<20} @{t['price']:.6g}"
                          f"  {stype_tag}  score={t['score']:>+8.2f}  止损={t['stop']:.6g}")
                elif t['action'] == '止损平仓':
                    print(f"    ✕ 止损 {t['symbol']:<20} @{t['price']:.6g}"
                          f"  {stype_tag}  持仓={t['hold_h']}h  {t['pnl%']}%  (${t['pnl$']:.2f})")
                elif t['action'] == '量缩横盘平仓':
                    sign = "+" if t['pnl%'] >= 0 else ""
                    print(f"    ◈ 量缩 {t['symbol']:<20} @{t['price']:.6g}"
                          f"  {stype_tag}  持仓={t['hold_h']}h  {sign}{t['pnl%']}%  ({sign}${t['pnl$']:.2f})")

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
        pnl_pct, pnl_usd = pos_pnl(pos, cur_px)
        equity += pnl_usd
        sign = "+" if pnl_pct >= 0 else ""
        print(f"    多 {sym:<20} @{cur_px:.6g}  [{pos.get('signal_type','?')}]"
              f"  持仓={held_h:.0f}h  {sign}{pnl_pct:.2f}%  ({sign}${pnl_usd:.2f})")
        trade_log.append({
            'date': SIM_END.strftime("%Y-%m-%d"), 'action': '模拟结束平仓',
            'symbol': sym, 'price': cur_px, 'score': pos['score'],
            'signal_type': pos.get('signal_type', ''),
            'stop': pos['stop'], 'pnl%': pnl_pct, 'pnl$': pnl_usd,
            'hold_h': round(held_h, 1), 'reason': '模拟结束',
        })

    # ── 汇总 ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  回测汇总  {SIM_START.date()} → {SIM_END.date()}")
    print(f"{'='*80}")

    all_closed = [t for t in trade_log if t['action'] != '开仓']
    opens      = [t for t in trade_log if t['action'] == '开仓']
    stops_l    = [t for t in trade_log if t['action'] == '止损平仓']
    consol_l   = [t for t in trade_log if t['action'] == '量缩横盘平仓']
    wins       = [t for t in all_closed if t['pnl%'] > 0]
    loses      = [t for t in all_closed if t['pnl%'] <= 0]
    total_pnl  = sum(t['pnl$'] for t in all_closed)
    win_rate   = len(wins) / len(all_closed) * 100 if all_closed else 0

    print(f"  开仓次数     : {len(opens)}")
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

    # ── 按信号类型拆分统计 ────────────────────────────────────────────────────
    print(f"\n{'─'*80}")
    print(f"  按信号类型拆分统计:")
    print(f"  {'类型':<16} {'开仓':>6} {'已平':>6} {'盈利':>6} {'止损':>6} {'胜率':>8} {'总盈亏':>12} {'平均盈亏':>12}")
    print(f"  {'-'*78}")
    for stype in ['2b', '3b', 'vol-pullback']:
        o_n  = [t for t in opens      if t.get('signal_type') == stype]
        c_n  = [t for t in all_closed if t.get('signal_type') == stype]
        sl_n = [t for t in stops_l    if t.get('signal_type') == stype]
        w_n  = [t for t in c_n        if t['pnl%'] > 0]
        wr_n = len(w_n) / len(c_n) * 100 if c_n else 0
        tp_n = sum(t['pnl$'] for t in c_n)
        ap_n = tp_n / len(c_n) if c_n else 0
        print(f"  {stype:<16} {len(o_n):>6} {len(c_n):>6} {len(w_n):>6} {len(sl_n):>6}"
              f"  {wr_n:>6.1f}%  ${tp_n:>+10.2f}  ${ap_n:>+10.2f}")

    # ── 完整交易记录 ──────────────────────────────────────────────────────────
    print(f"\n  完整交易记录（{len(trade_log)} 条）：")
    print(f"  {'日期':<12} {'操作':<8} {'类型':<14} {'标的':<18} {'价格':>10} "
          f"{'评分':>8} {'持仓h':>7} {'收益%':>8} {'收益$':>9}")
    print(f"  {'-'*100}")
    for t in trade_log:
        sign = "+" if t['pnl%'] > 0 else ""
        print(f"  {t['date']:<12} {t['action']:<8} {t.get('signal_type',''):<14}"
              f" {t['symbol']:<18} {t['price']:>10.6g} {t['score']:>+8.2f}"
              f" {t['hold_h']:>7.1f} {sign+str(t['pnl%'])+'%':>8}"
              f" {('+' if t['pnl$']>=0 else '')+str(t['pnl$']):>9}")

    out = os.path.join(os.path.dirname(__file__), "backtest_volpb_result.json")
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
                "by_signal": {
                    stype: {
                        "opens": len([t for t in opens if t.get('signal_type') == stype]),
                        "closed": len([t for t in all_closed if t.get('signal_type') == stype]),
                        "stops": len([t for t in stops_l if t.get('signal_type') == stype]),
                        "wins": len([t for t in all_closed if t.get('signal_type') == stype and t['pnl%'] > 0]),
                        "total_pnl": round(sum(t['pnl$'] for t in all_closed if t.get('signal_type') == stype), 2),
                    }
                    for stype in ['2b', '3b', 'vol-pullback']
                }
            },
            "trades": trade_log,
        }, f, indent=2, ensure_ascii=False)
    print(f"\n  详细结果已保存: {out}")
    print(f"{'='*80}\n")


def main():
    if not os.path.exists(SYMBOLS_FILE):
        print(f"[ERROR] 找不到 {SYMBOLS_FILE}")
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
    run_backtest(symbol_map)


if __name__ == "__main__":
    main()
