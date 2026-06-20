"""
backtest_chan_oscillate.py
2026-01-01 ~ 今日  缠论中枢震荡策略回测（1H 级别，仅做多）

逻辑：
  - 找到最近有效中枢（ZD / ZG）
  - 当前价落在 ZD 附近（ZD ~ ZD*(1+ENTRY_DIST)）时开多
  - 止盈：价格触达 ZG
  - 止损：价格跌破 ZD × STOP_BUFFER

用法:
  本地: PROXY_URL=http://127.0.0.1:7890 python scripts/backtest_chan_oscillate.py
  离线: NO_FETCH=1 python scripts/backtest_chan_oscillate.py
"""
import os, sys, json, time as _time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt"); sys.exit(1)

from pathlib import Path

# ── 配置 ─────────────────────────────────────────────────────────────────────
PROXY_URL       = os.environ.get("PROXY_URL", "")
SYMBOLS_FILE    = os.environ.get(
    "SYMBOLS_FILE",
    os.path.join(os.path.dirname(__file__), "top_symbols_output.json"),
)
CACHE_DIR       = Path(os.path.dirname(__file__)) / "kline_cache_1h"
NO_FETCH        = os.environ.get("NO_FETCH", "0") == "1"
SIM_START       = datetime(2026, 1, 1, tzinfo=timezone.utc)
SIM_END         = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
WARMUP_DAYS     = 60
TIMEFRAME       = "1h"
REBALANCE_HOURS_UTC = list(range(0, 24, 2))  # 每2h扫一次
PORTFOLIO_SIZE  = 5
INITIAL_CAPITAL = 10_000.0
LEVERAGE        = 2
ENTRY_PCT       = 0.20
MAX_SYMBOLS     = 100
COOLDOWN_DAYS   = 3

# 信号参数
ENTRY_DIST      = 0.08   # 当前价在 ZD 上方 8% 以内触发开仓
STOP_BUFFER     = 0.98   # 止损在 ZD 下方 2%
TREND_MA_BARS   = 240    # 趋势过滤（1H × 240 = 10日均线）

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


def fetch_ohlcv_full(exchange, symbol: str, since_dt: datetime, end_dt: datetime) -> pd.DataFrame:
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


def load_symbol_data(exchange, symbol: str, since_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    cached   = load_cache(symbol)
    since_ms = int(since_dt.timestamp() * 1000)
    end_ms   = int(end_dt.timestamp() * 1000)

    if not cached.empty:
        cache_end = int(cached["ts"].iloc[-1])
        need_append = cache_end < end_ms - 3600 * 1000
        if not need_append:
            mask = (cached["ts"] >= since_ms) & (cached["ts"] < end_ms)
            return cached[mask].reset_index(drop=True)
        if NO_FETCH:
            mask = (cached["ts"] >= since_ms) & (cached["ts"] < end_ms)
            return cached[mask].reset_index(drop=True)
        app = fetch_ohlcv_full(exchange, symbol,
                               datetime.fromtimestamp(cache_end / 1000, tz=timezone.utc), end_dt)
        if not app.empty:
            merged = pd.concat([cached, app]).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
            merged["dt"] = pd.to_datetime(merged["ts"], unit="ms", utc=True)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            merged[["ts", "open", "high", "low", "close", "volume"]].to_csv(cache_path(symbol), index=False)
            mask = (merged["ts"] >= since_ms) & (merged["ts"] < end_ms)
            return merged[mask].reset_index(drop=True)
        mask = (cached["ts"] >= since_ms) & (cached["ts"] < end_ms)
        return cached[mask].reset_index(drop=True)

    if NO_FETCH:
        return pd.DataFrame()
    df = fetch_ohlcv_full(exchange, symbol, since_dt, end_dt)
    if not df.empty:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df[["ts", "open", "high", "low", "close", "volume"]].to_csv(cache_path(symbol), index=False)
    return df


# ── 缠论计算 ─────────────────────────────────────────────────────────────────

def merge_inclusion(high: list, low: list):
    ph, pl = list(high), list(low)
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(ph) - 1:
            h1, l1 = ph[i], pl[i]
            h2, l2 = ph[i+1], pl[i+1]
            if (h1 >= h2 and l1 <= l2) or (h2 >= h1 and l2 <= l1):
                ph[i] = max(h1, h2)
                pl[i] = max(l1, l2) if h1 >= h2 else min(l1, l2)
                ph.pop(i+1); pl.pop(i+1)
                changed = True
            else:
                i += 1
    return ph, pl


def find_fractals(ph, pl):
    tops, bots = [], []
    for i in range(1, len(ph)-1):
        if ph[i] > ph[i-1] and ph[i] > ph[i+1]:
            tops.append((i, ph[i]))
        if pl[i] < pl[i-1] and pl[i] < pl[i+1]:
            bots.append((i, pl[i]))
    return tops, bots


def find_bi(tops, bots):
    events = [(i, 'top', v) for i, v in tops] + [(i, 'bot', v) for i, v in bots]
    events.sort(key=lambda x: x[0])
    bi_list = []
    last = None
    for idx, kind, val in events:
        if last is None:
            last = (idx, kind, val); continue
        if kind == last[1]:
            if (kind == 'top' and val >= last[2]) or (kind == 'bot' and val <= last[2]):
                last = (idx, kind, val)
        else:
            bi_list.append({'dir': 1 if last[1] == 'bot' else -1,
                            'start': (last[0], last[2]), 'end': (idx, val)})
            last = (idx, kind, val)
    return bi_list


def find_zhongshu(bi_list):
    zs_list = []
    i = 0
    while i + 2 < len(bi_list):
        b0, b1, b2 = bi_list[i], bi_list[i+1], bi_list[i+2]
        h0 = max(b0['start'][1], b0['end'][1]); l0 = min(b0['start'][1], b0['end'][1])
        h1 = max(b1['start'][1], b1['end'][1]); l1 = min(b1['start'][1], b1['end'][1])
        h2 = max(b2['start'][1], b2['end'][1]); l2 = min(b2['start'][1], b2['end'][1])
        ZG = min(h0, h1, h2); ZD = max(l0, l1, l2)
        if ZG > ZD:
            zs = {'ZG': ZG, 'ZD': ZD, 'start_bi': i, 'end_bi': i+2}
            j = i + 3
            while j < len(bi_list):
                bj = bi_list[j]
                bj_h = max(bj['start'][1], bj['end'][1])
                bj_l = min(bj['start'][1], bj['end'][1])
                if bj_h >= ZD and bj_l <= ZG:
                    ZG = min(ZG, bj_h); ZD = max(ZD, bj_l)
                    zs['end_bi'] = j; zs['ZG'] = ZG; zs['ZD'] = ZD
                    j += 1
                else:
                    break
            zs_list.append(zs)
            i = zs['end_bi'] + 1
        else:
            i += 1
    return zs_list


def score_oscillate(df: pd.DataFrame) -> Tuple[float, float, float]:
    """返回 (score, stop_price, tp_price)
    score > 0 表示有效信号，stop=ZD*0.98，tp=ZG
    """
    if len(df) < 80:
        return 0.0, 0.0, 0.0

    close = df['close'].tolist()
    high  = df['high'].tolist()
    low   = df['low'].tolist()

    ph, pl   = merge_inclusion(high, low)
    tops, bots = find_fractals(ph, pl)
    bi_list  = find_bi(tops, bots)
    zs_list  = find_zhongshu(bi_list)

    if not zs_list:
        return 0.0, 0.0, 0.0

    cur = close[-1]
    lz  = zs_list[-1]
    ZD  = lz['ZD']
    ZG  = lz['ZG']

    # 中枢宽度至少 5% 才有意义
    if (ZG - ZD) / ZD < 0.05:
        return 0.0, 0.0, 0.0

    # 当前价在 ZD 上方 ENTRY_DIST 以内
    if cur < ZD:
        return 0.0, 0.0, 0.0
    dist = (cur - ZD) / ZD
    if dist > ENTRY_DIST:
        return 0.0, 0.0, 0.0

    # 止损必须在当前价下方
    stop = round(ZD * STOP_BUFFER, 8)
    if stop >= cur:
        return 0.0, 0.0, 0.0

    # 盈亏比：ZG-cur vs cur-stop，至少 1:1
    reward = ZG - cur
    risk   = cur - stop
    if risk <= 0 or reward / risk < 1.0:
        return 0.0, 0.0, 0.0

    # 评分 = 盈亏比 × 60（上限100）
    score = min(100.0, round(reward / risk * 60, 2))

    return score, stop, round(ZG, 8)


# ── 回测主逻辑 ────────────────────────────────────────────────────────────────

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

    positions:     Dict[str, Dict] = {}
    cooldown_until: Dict[str, object] = {}
    equity    = INITIAL_CAPITAL
    trade_log: List[Dict] = []

    print(f"\n{'='*80}")
    print(f"  缠论中枢震荡策略回测（1H 仅做多）  {SIM_START.date()} → {SIM_END.date()}")
    print(f"  标的数: {len(symbol_map)}  组合上限: {PORTFOLIO_SIZE}  杠杆: {LEVERAGE}×")
    print(f"  单仓: {int(ENTRY_PCT*100)}%  入场距ZD: ≤{int(ENTRY_DIST*100)}%  止损缓冲: {int((1-STOP_BUFFER)*100)}%")
    print(f"  调仓间隔: 每2h  共 {len(rebalance_slots)} 个调仓点")
    print(f"{'='*80}\n")

    def pos_pnl(pos: Dict, px: float) -> Tuple[float, float]:
        pnl_pct = (px - pos['entry_price']) / pos['entry_price'] * 100
        pnl_usd = pnl_pct / 100 * (INITIAL_CAPITAL * ENTRY_PCT * LEVERAGE)
        return round(pnl_pct, 2), round(pnl_usd, 2)

    def get_cur_px(sym: str, slot) -> Optional[float]:
        df_ = symbol_map.get(sym)
        if df_ is None: return None
        recent = df_[df_['dt'] < slot]
        return float(recent.iloc[-1]['close']) if not recent.empty else None

    prev_slot = SIM_START
    for slot in rebalance_slots:
        day_str = slot.strftime("%Y-%m-%d %H:%M")
        cooldown_until = {s: dt for s, dt in cooldown_until.items() if dt > slot}

        # ── 1. 止损 / 止盈检查（逐根1H K线） ─────────────────────────────────
        for sym in list(positions.keys()):
            pos      = positions[sym]
            entry_px = pos['entry_price']
            stop     = pos['stop']
            tp       = pos['tp']
            df_      = symbol_map.get(sym)
            if df_ is None: continue
            bars = df_[(df_['dt'] >= prev_slot) & (df_['dt'] < slot)]
            if bars.empty: continue

            hit_stop = None; hit_tp = None
            for _, bar in bars.iterrows():
                bar_high = float(bar['high'])
                bar_low  = float(bar['low'])
                if bar_low < stop:
                    hit_stop = stop; break
                if bar_high >= tp:
                    hit_tp = tp; break

            if hit_tp is not None:
                pnl_pct, pnl_usd = pos_pnl(pos, hit_tp)
                equity += pnl_usd
                trade_log.append({
                    'date': day_str, 'action': '止盈平仓', 'symbol': sym,
                    'price': hit_tp, 'score': pos['score'], 'stop': stop, 'tp': tp,
                    'pnl%': pnl_pct, 'pnl$': pnl_usd,
                    'hold_h': round((slot - pos['entry_time']).total_seconds() / 3600, 1),
                    'reason': f"触达ZG {hit_tp:.6g}",
                })
                cooldown_until[sym] = slot + timedelta(days=COOLDOWN_DAYS)
                del positions[sym]
            elif hit_stop is not None:
                pnl_pct, pnl_usd = pos_pnl(pos, hit_stop)
                equity += pnl_usd
                trade_log.append({
                    'date': day_str, 'action': '止损平仓', 'symbol': sym,
                    'price': hit_stop, 'score': pos['score'], 'stop': stop, 'tp': tp,
                    'pnl%': pnl_pct, 'pnl$': pnl_usd,
                    'hold_h': round((slot - pos['entry_time']).total_seconds() / 3600, 1),
                    'reason': f"破ZD止损 {hit_stop:.6g}",
                })
                cooldown_until[sym] = slot + timedelta(days=COOLDOWN_DAYS)
                del positions[sym]

        # ── 2. 评分所有标的 ────────────────────────────────────────────────────
        day_scores: Dict[str, float] = {}
        day_stops:  Dict[str, float] = {}
        day_tps:    Dict[str, float] = {}

        for sym, df in symbol_map.items():
            slice_df = df[df['dt'] < slot].copy().reset_index(drop=True)
            if len(slice_df) < 80: continue
            try:
                sc, st, tp = score_oscillate(slice_df)
                if sc > 0:
                    day_scores[sym] = sc
                    day_stops[sym]  = st
                    day_tps[sym]    = tp
            except Exception:
                pass

        ranking = sorted(day_scores.keys(), key=lambda s: day_scores[s], reverse=True)

        # ── 3. 开仓 ────────────────────────────────────────────────────────────
        for sym in ranking:
            if len(positions) >= PORTFOLIO_SIZE: break
            if sym in positions or sym in cooldown_until: continue

            df_ = symbol_map.get(sym)
            if df_ is not None:
                snap_close = df_[df_['dt'] < slot]['close'].astype(float)
                if len(snap_close) >= TREND_MA_BARS:
                    if float(snap_close.iloc[-1]) < float(snap_close.tail(TREND_MA_BARS).mean()):
                        continue

            cur_px = get_cur_px(sym, slot)
            if cur_px is None: continue
            stop = day_stops[sym]
            tp   = day_tps[sym]
            if stop >= cur_px: continue

            positions[sym] = {
                'entry_price': cur_px, 'entry_time': slot,
                'score': day_scores[sym], 'stop': stop, 'tp': tp,
            }
            trade_log.append({
                'date': day_str, 'action': '开仓', 'symbol': sym,
                'price': cur_px, 'score': day_scores[sym], 'stop': stop, 'tp': tp,
                'pnl%': 0, 'pnl$': 0, 'hold_h': 0,
                'reason': f"近ZD score={day_scores[sym]:.1f} tp={tp:.6g}",
            })

        # ── 打印有操作的时间点 ────────────────────────────────────────────────
        day_trades = [t for t in trade_log if t['date'] == day_str]
        if day_trades:
            print(f"\n{'─'*80}")
            print(f"  {day_str}  持仓: {len(positions)}/{PORTFOLIO_SIZE}  净值: ${equity:,.2f}")
            for t in day_trades:
                if t['action'] == '开仓':
                    print(f"    ▶ 开多 {t['symbol']:<20} @{t['price']:.6g}"
                          f"  score={t['score']:>+7.1f}  止损={t['stop']:.6g}  止盈={t['tp']:.6g}")
                elif t['action'] == '止盈平仓':
                    print(f"    ✓ 止盈 {t['symbol']:<20} @{t['price']:.6g}"
                          f"  持仓={t['hold_h']}h  +{t['pnl%']}%  (+${t['pnl$']:.2f})")
                elif t['action'] == '止损平仓':
                    print(f"    ✕ 止损 {t['symbol']:<20} @{t['price']:.6g}"
                          f"  持仓={t['hold_h']}h  {t['pnl%']}%  (${t['pnl$']:.2f})")

        prev_slot = slot

    # ── 强制平仓 ──────────────────────────────────────────────────────────────
    print(f"\n{'─'*80}")
    print(f"  模拟结束，强制平仓：")
    for sym, pos in list(positions.items()):
        df_ = symbol_map.get(sym)
        if df_ is None or df_.empty: continue
        cur_px = float(df_.iloc[-1]['close'])
        held_h = (SIM_END - pos['entry_time']).total_seconds() / 3600
        pnl_pct, pnl_usd = pos_pnl(pos, cur_px)
        equity += pnl_usd
        sign = "+" if pnl_pct >= 0 else ""
        print(f"    多 {sym:<20} @{cur_px:.6g}  持仓={held_h:.0f}h  {sign}{pnl_pct:.2f}%  ({sign}${pnl_usd:.2f})")
        trade_log.append({
            'date': SIM_END.strftime("%Y-%m-%d"), 'action': '模拟结束平仓', 'symbol': sym,
            'price': cur_px, 'score': pos['score'], 'stop': pos['stop'], 'tp': pos['tp'],
            'pnl%': pnl_pct, 'pnl$': pnl_usd, 'hold_h': round(held_h, 1), 'reason': '模拟结束',
        })

    # ── 汇总 ──────────────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  回测汇总  {SIM_START.date()} → {SIM_END.date()}")
    print(f"{'='*80}")

    all_closed = [t for t in trade_log if t['action'] != '开仓']
    opens      = [t for t in trade_log if t['action'] == '开仓']
    tps_l      = [t for t in trade_log if t['action'] == '止盈平仓']
    stops_l    = [t for t in trade_log if t['action'] == '止损平仓']
    wins       = [t for t in all_closed if t['pnl%'] > 0]
    total_pnl  = sum(t['pnl$'] for t in all_closed)
    win_rate   = len(wins) / len(all_closed) * 100 if all_closed else 0

    print(f"  开仓次数     : {len(opens)}")
    print(f"  止盈平仓     : {len(tps_l)}")
    print(f"  止损平仓     : {len(stops_l)}")
    print(f"  盈利次数     : {len(wins)}  亏损次数: {len(all_closed)-len(wins)}")
    print(f"  胜率         : {win_rate:.1f}%")
    print(f"  总盈亏       : ${total_pnl:+,.2f}")
    print(f"  期末净值     : ${equity:,.2f}  ({(equity/INITIAL_CAPITAL-1)*100:+.2f}%)")
    if wins:
        print(f"  平均盈利     : ${sum(t['pnl$'] for t in wins)/len(wins):,.2f}")
    loses = [t for t in all_closed if t['pnl%'] <= 0]
    if loses:
        print(f"  平均亏损     : ${sum(t['pnl$'] for t in loses)/len(loses):,.2f}")

    print(f"\n  完整交易记录（{len(trade_log)} 条）：")
    print(f"  {'日期':<14} {'操作':<8} {'标的':<20} {'价格':>10} {'评分':>7} {'止盈':>10} {'持仓h':>7} {'收益%':>8} {'收益$':>9}")
    print(f"  {'-'*100}")
    for t in trade_log:
        sign = "+" if t['pnl%'] > 0 else ""
        print(f"  {t['date']:<14} {t['action']:<8} {t['symbol']:<20} {t['price']:>10.6g}"
              f" {t['score']:>+7.1f} {t['tp']:>10.6g} {t['hold_h']:>7.1f}"
              f" {sign+str(t['pnl%'])+'%':>8} {('+' if t['pnl$']>=0 else '')+str(t['pnl$']):>9}")

    out = os.path.join(os.path.dirname(__file__), "backtest_oscillate_result.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {
                "start": SIM_START.isoformat(), "end": SIM_END.isoformat(),
                "opens": len(opens), "tp_exits": len(tps_l), "stops": len(stops_l),
                "trades": len(all_closed), "win_rate": round(win_rate, 2),
                "total_pnl": round(total_pnl, 2), "final_equity": round(equity, 2),
                "return_pct": round((equity / INITIAL_CAPITAL - 1) * 100, 2),
            },
            "trades": trade_log,
        }, f, indent=2, ensure_ascii=False)
    print(f"\n  详细结果已保存: {out}")
    print(f"{'='*80}\n")


def main():
    if not os.path.exists(SYMBOLS_FILE):
        print(f"[ERROR] 找不到 {SYMBOLS_FILE}"); sys.exit(1)
    with open(SYMBOLS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    symbols_raw = [s.split(":")[-1] for s in raw.get("symbol_list", [])[:MAX_SYMBOLS]]
    print(f"[INFO] 标的列表: {len(symbols_raw)} 个  (1H 数据，首次运行需从 API 拉取，耗时较长)")

    exchange = build_exchange() if not NO_FETCH else None

    fetch_since = SIM_START - timedelta(days=WARMUP_DAYS)
    fetch_end   = SIM_END + timedelta(days=1)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached_count = sum(1 for s in symbols_raw if cache_path(s).exists())
    print(f"[INFO] 1H本地缓存: {cached_count}/{len(symbols_raw)} 个")
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
