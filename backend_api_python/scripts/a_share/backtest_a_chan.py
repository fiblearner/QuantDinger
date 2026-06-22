"""
backtest_a_chan.py — A股缠论多级别回测

策略逻辑（三级别联立）：
  周线级别  → 判断大趋势方向（get_trend_direction）
  日线级别  → 扫描买卖点信号（b1/b2/b3/s1/s2/s3）
  30分钟级  → 确认入场时机（30min 出现同向买点或底背驰）

开仓条件（同时满足）：
  1. 周线趋势方向 ≥ 0（不做空头市场）
  2. 日线出现 b1/b2/b3 信号，分数 ≥ 阈值
  3. 30min 出现任意买点（可选，strict_mode=True 时强制）
  4. T+1：当日买入，次日才能卖出
  5. 涨停当日不买（价格已封死，无法成交）

平仓条件（满足其一即平）：
  1. 日线出现 s1/s2/s3 卖点
  2. 止损触发（低于止损价）
  3. 持仓超过最大持仓天数（MAX_HOLD_DAYS）

资金管理：
  每笔开仓占总资金的 ENTRY_PCT（不加杠杆）
  最多同时持仓 MAX_POSITIONS 个标的

用法:
  python backtest_a_chan.py                      # 用内置股票池
  python backtest_a_chan.py --symbols 000001,600036,300750
  python backtest_a_chan.py --start 2023-01-01 --end 2025-01-01
  python backtest_a_chan.py --no-30min           # 关闭30分钟确认
  NO_CACHE=1 python backtest_a_chan.py           # 强制重新拉取
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# 把 scripts/ 目录加入 sys.path，使得 a_share/ 模块可以直接 import
_SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from a_share.chan_core import (
    analyze, find_signals, get_trend_direction,
    MergedBar, Signal, merge_bars,
)
from a_share.data_fetcher import fetch_multi_timeframe, normalize_code

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_SYMBOLS = [
    "000001",  # 平安银行
    "600036",  # 招商银行
    "300750",  # 宁德时代
    "000858",  # 五粮液
    "601318",  # 中国平安
    "600519",  # 贵州茅台
    "000333",  # 美的集团
    "600276",  # 恒瑞医药
    "002415",  # 海康威视
    "601888",  # 中国中免
]

SIM_START       = datetime(2023, 1, 1)
SIM_END         = datetime.now()
INITIAL_CAPITAL = 100_000.0     # 初始资金（人民币）
ENTRY_PCT       = 0.20          # 单笔仓位 20%
MAX_POSITIONS   = 5             # 最多同时持仓数
MIN_SCORE_B1    = 60            # 一买最低分数
MIN_SCORE_B2    = 55            # 二买最低分数
MIN_SCORE_B3    = 50            # 三买最低分数
MAX_HOLD_DAYS   = 60            # 最长持仓天数，强制平仓
STRICT_30MIN    = False         # True: 必须有30min买点才能开仓

# 优先级：b2 > b3 > b1（b2最确定性最高）
BUY_PRIORITY    = {"b2": 3, "b3": 2, "b1": 1}
SELL_TYPES      = {"s1", "s2", "s3"}

NO_CACHE = os.environ.get("NO_CACHE", "0") == "1"

# ─────────────────────────────────────────────────────────────────────────────
# 数据缓存（单次回测内复用）
# ─────────────────────────────────────────────────────────────────────────────

_data_cache: Dict[str, dict] = {}


def get_data(code: str) -> dict:
    if code not in _data_cache:
        print(f"  [数据] 拉取 {code} ...", end=" ", flush=True)
        data = fetch_multi_timeframe(
            code,
            timeframes=["1w", "1d", "30m"],
            limit_map={"1w": 300, "1d": 1000, "30m": 1500},
            no_cache=NO_CACHE,
        )
        _data_cache[code] = data
        sizes = {tf: len(df) for tf, df in data.items()}
        print(f"周线={sizes['1w']} 日线={sizes['1d']} 30min={sizes['30m']}")
    return _data_cache[code]


# ─────────────────────────────────────────────────────────────────────────────
# 涨跌停判断（A股规则）
# ─────────────────────────────────────────────────────────────────────────────

def is_limit_up(row: pd.Series) -> bool:
    """价格封在涨停，无法买入"""
    if row["open"] <= 0 or row["close"] <= 0:
        return False
    return (row["close"] - row["open"]) / row["open"] >= 0.095


def is_limit_down(row: pd.Series) -> bool:
    """价格封在跌停，无法卖出"""
    if row["open"] <= 0 or row["close"] <= 0:
        return False
    return (row["close"] - row["open"]) / row["open"] <= -0.095


# ─────────────────────────────────────────────────────────────────────────────
# 单标的回测
# ─────────────────────────────────────────────────────────────────────────────

class Position:
    def __init__(self, code: str, entry_date: datetime, entry_price: float,
                 shares: int, cost: float, stop: float, signal_kind: str):
        self.code        = code
        self.entry_date  = entry_date
        self.entry_price = entry_price
        self.shares      = shares
        self.cost        = cost          # 实际花费（含手续费）
        self.stop        = stop
        self.signal_kind = signal_kind
        self.highest     = entry_price
        self.exit_date: Optional[datetime] = None
        self.exit_price: float = 0.0
        self.pnl: float = 0.0
        self.exit_reason: str = ""

    @property
    def hold_days(self) -> int:
        if self.exit_date:
            return (self.exit_date - self.entry_date).days
        return 0


COMMISSION = 0.001   # 手续费 0.1%（买卖各）
STAMP_TAX  = 0.001   # 印花税 0.1%（仅卖出）
SLIPPAGE   = 0.002   # 滑点 0.2%


def simulate_symbol(
    code: str,
    daily_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    min30_df:  pd.DataFrame,
    sim_start: datetime,
    sim_end:   datetime,
    initial_capital: float,
    strict_30min: bool = STRICT_30MIN,
) -> Tuple[List[Position], List[dict]]:
    """
    对单一标的逐日模拟，返回 (已完成仓位列表, 每日净值列表)。
    """
    if daily_df.empty or len(daily_df) < 60:
        return [], []

    # 过滤回测区间内的日线
    daily_df = daily_df.copy()
    daily_df["date"] = pd.to_datetime(daily_df["dt"]).dt.date

    sim_start_date = sim_start.date()
    sim_end_date   = sim_end.date()

    # 找到日线中回测区间的索引
    date_arr = daily_df["date"].values
    closed_positions: List[Position] = []
    daily_equity: List[dict] = []

    cash     = initial_capital
    position: Optional[Position] = None   # 单标的只持一个仓

    for i, row in daily_df.iterrows():
        today = row["date"]
        if today < sim_start_date or today > sim_end_date:
            continue

        close_px = float(row["close"])

        # ── 止损 / 强制平仓 ───────────────────────────────────────────────
        if position is not None:
            position.highest = max(position.highest, close_px)

            # 追踪止损：浮盈 > 30% 后止损上移至成本价
            if close_px > position.entry_price * 1.30 and position.stop < position.entry_price:
                position.stop = round(position.entry_price * 1.005, 4)  # 保本+0.5%

            hit_stop   = (position.stop > 0 and close_px <= position.stop)
            max_hold   = (today - position.entry_date.date()).days >= MAX_HOLD_DAYS

            if hit_stop or max_hold:
                exit_px = close_px * (1 - SLIPPAGE)
                gross   = exit_px * position.shares
                fee     = gross * (COMMISSION + STAMP_TAX)
                pnl     = gross - fee - position.cost
                position.exit_date   = datetime.combine(today, datetime.min.time())
                position.exit_price  = exit_px
                position.pnl         = pnl
                position.exit_reason = "止损" if hit_stop else "持仓超限"
                cash += gross - fee
                closed_positions.append(position)
                position = None

        # ── 卖点检查（有持仓时）──────────────────────────────────────────
        if position is not None:
            # 用截至今日的日线做缠论
            slice_d = daily_df.iloc[:i + 1]
            sigs    = find_signals(slice_d)
            sell_sigs = [s for s in sigs if s.kind in SELL_TYPES]

            if sell_sigs and not is_limit_down(row):
                exit_px = close_px * (1 - SLIPPAGE)
                gross   = exit_px * position.shares
                fee     = gross * (COMMISSION + STAMP_TAX)
                pnl     = gross - fee - position.cost
                position.exit_date   = datetime.combine(today, datetime.min.time())
                position.exit_price  = exit_px
                position.pnl         = pnl
                position.exit_reason = sell_sigs[0].kind
                cash += gross - fee
                closed_positions.append(position)
                position = None

        # ── 买点检查（无持仓时）──────────────────────────────────────────
        if position is None and not is_limit_up(row):
            slice_d = daily_df.iloc[:i + 1]

            # 周线趋势
            # 找到周线中不晚于今日的数据
            wk_mask = weekly_df["date"] <= today if "date" in weekly_df.columns \
                      else weekly_df["dt"].dt.date <= today
            slice_w = weekly_df[wk_mask] if not weekly_df.empty else weekly_df
            weekly_trend = get_trend_direction(slice_w) if len(slice_w) >= 20 else 0

            if weekly_trend < 0:
                # 周线下跌趋势，不买
                daily_equity.append({"date": today, "cash": cash, "equity": cash})
                continue

            # 日线信号
            buy_sigs = find_signals(slice_d)
            buy_sigs = [s for s in buy_sigs if s.kind in ("b1", "b2", "b3")]
            if not buy_sigs:
                daily_equity.append({"date": today, "cash": cash, "equity": cash})
                continue

            # 按优先级排序
            buy_sigs.sort(key=lambda s: BUY_PRIORITY.get(s.kind, 0), reverse=True)
            chosen = buy_sigs[0]

            # 30分钟确认
            if strict_30min and not min30_df.empty:
                m30_mask = min30_df["dt"].dt.date <= today
                slice_m  = min30_df[m30_mask]
                m30_sigs = find_signals(slice_m) if len(slice_m) >= 30 else []
                m30_buy  = [s for s in m30_sigs if s.kind in ("b1", "b2", "b3")]
                if not m30_buy:
                    daily_equity.append({"date": today, "cash": cash, "equity": cash})
                    continue

            # T+1：今日信号，次日开盘买（简化：按今日收盘价+滑点模拟）
            entry_px = close_px * (1 + SLIPPAGE)
            alloc    = cash * ENTRY_PCT
            shares   = int(alloc / entry_px / 100) * 100   # A股单位手（100股）
            if shares < 100:
                daily_equity.append({"date": today, "cash": cash, "equity": cash})
                continue

            cost = entry_px * shares * (1 + COMMISSION)
            if cost > cash:
                daily_equity.append({"date": today, "cash": cash, "equity": cash})
                continue

            cash -= cost
            position = Position(
                code=code,
                entry_date=datetime.combine(today, datetime.min.time()),
                entry_price=entry_px,
                shares=shares,
                cost=cost,
                stop=chosen.stop,
                signal_kind=chosen.kind,
            )

        # 每日净值快照
        mkt_val = (position.shares * close_px) if position else 0
        daily_equity.append({"date": today, "cash": cash, "equity": cash + mkt_val})

    # 回测结束，强制平仓
    if position is not None:
        last_row = daily_df[daily_df["date"] <= sim_end_date].iloc[-1]
        exit_px  = float(last_row["close"]) * (1 - SLIPPAGE)
        gross    = exit_px * position.shares
        fee      = gross * (COMMISSION + STAMP_TAX)
        pnl      = gross - fee - position.cost
        position.exit_date   = datetime.combine(last_row["date"], datetime.min.time())
        position.exit_price  = exit_px
        position.pnl         = pnl
        position.exit_reason = "回测结束"
        closed_positions.append(position)

    return closed_positions, daily_equity


# ─────────────────────────────────────────────────────────────────────────────
# 多标的汇总
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(
    symbols: List[str],
    sim_start: datetime = SIM_START,
    sim_end:   datetime = SIM_END,
    initial_capital: float = INITIAL_CAPITAL,
    strict_30min: bool = STRICT_30MIN,
) -> None:
    print(f"\n{'='*65}")
    print(f"  A股缠论多级别回测")
    print(f"  回测区间: {sim_start.date()} ~ {sim_end.date()}")
    print(f"  标的数量: {len(symbols)}")
    print(f"  初始资金: ¥{initial_capital:,.0f}")
    print(f"  30分钟确认: {'开启' if strict_30min else '关闭'}")
    print(f"{'='*65}\n")

    all_positions: List[Position] = []
    symbol_stats = []

    for code in symbols:
        pure, _, _ = normalize_code(code)
        data = get_data(pure)

        daily_df  = data.get("1d", pd.DataFrame())
        weekly_df = data.get("1w", pd.DataFrame())
        min30_df  = data.get("30m", pd.DataFrame())

        # 补充 date 列（周线需要）
        for df in (weekly_df, min30_df):
            if not df.empty and "date" not in df.columns:
                df["date"] = pd.to_datetime(df["dt"]).dt.date

        positions, equity_curve = simulate_symbol(
            code=pure,
            daily_df=daily_df,
            weekly_df=weekly_df,
            min30_df=min30_df,
            sim_start=sim_start,
            sim_end=sim_end,
            initial_capital=initial_capital / len(symbols),  # 按标的数均分资金
            strict_30min=strict_30min,
        )
        all_positions.extend(positions)

        # 单标的统计
        if positions:
            wins    = [p for p in positions if p.pnl > 0]
            total   = sum(p.pnl for p in positions)
            wr      = len(wins) / len(positions) * 100
            avg_hd  = np.mean([p.hold_days for p in positions])
            symbol_stats.append({
                "code": pure, "trades": len(positions),
                "win_rate": wr, "total_pnl": total, "avg_hold": avg_hd,
            })
        else:
            symbol_stats.append({
                "code": pure, "trades": 0,
                "win_rate": 0, "total_pnl": 0, "avg_hold": 0,
            })

    # ── 汇总输出 ─────────────────────────────────────────────────────────────
    print(f"\n{'─'*65}")
    print(f"  单标的统计")
    print(f"{'─'*65}")
    print(f"  {'代码':>8} {'开仓数':>6} {'胜率%':>7} {'总盈亏¥':>12} {'均持天':>7}")
    print(f"  {'─'*8} {'─'*6} {'─'*7} {'─'*12} {'─'*7}")
    for s in sorted(symbol_stats, key=lambda x: x["total_pnl"], reverse=True):
        print(f"  {s['code']:>8} {s['trades']:>6} {s['win_rate']:>6.1f}%"
              f" {s['total_pnl']:>+12,.0f} {s['avg_hold']:>6.1f}d")

    # ── 全局统计 ─────────────────────────────────────────────────────────────
    if not all_positions:
        print("\n  无开仓记录")
        return

    total_trades = len(all_positions)
    winners      = [p for p in all_positions if p.pnl > 0]
    win_rate     = len(winners) / total_trades * 100
    total_pnl    = sum(p.pnl for p in all_positions)
    avg_win      = np.mean([p.pnl for p in winners]) if winners else 0
    losers       = [p for p in all_positions if p.pnl <= 0]
    avg_loss     = np.mean([p.pnl for p in losers]) if losers else 0
    profit_factor = abs(sum(p.pnl for p in winners)) / (abs(sum(p.pnl for p in losers)) + 1e-9)
    total_return  = total_pnl / initial_capital * 100
    avg_hold      = np.mean([p.hold_days for p in all_positions])

    # 按信号类型分组
    sig_stats: dict = {}
    for p in all_positions:
        k = p.signal_kind
        if k not in sig_stats:
            sig_stats[k] = {"n": 0, "wins": 0, "pnl": 0}
        sig_stats[k]["n"]    += 1
        sig_stats[k]["wins"] += 1 if p.pnl > 0 else 0
        sig_stats[k]["pnl"]  += p.pnl

    # 卖出原因分组
    exit_stats: dict = {}
    for p in all_positions:
        r = p.exit_reason
        exit_stats.setdefault(r, 0)
        exit_stats[r] += 1

    print(f"\n{'─'*65}")
    print(f"  全局汇总")
    print(f"{'─'*65}")
    print(f"  总开仓次数 : {total_trades}")
    print(f"  胜率       : {win_rate:.1f}%")
    print(f"  总盈亏     : ¥{total_pnl:+,.0f}  ({total_return:+.1f}%)")
    print(f"  盈利因子   : {profit_factor:.2f}")
    print(f"  平均盈利   : ¥{avg_win:+,.0f}")
    print(f"  平均亏损   : ¥{avg_loss:+,.0f}")
    print(f"  平均持仓   : {avg_hold:.1f} 天")

    print(f"\n  按信号类型:")
    for k in sorted(sig_stats.keys()):
        s = sig_stats[k]
        wr = s["wins"] / s["n"] * 100 if s["n"] else 0
        print(f"    {k:>4}: {s['n']:>3}次  胜率={wr:.0f}%  盈亏=¥{s['pnl']:+,.0f}")

    print(f"\n  平仓原因:")
    for reason, cnt in sorted(exit_stats.items(), key=lambda x: -x[1]):
        print(f"    {reason:12}: {cnt} 次")

    # ── 最优/最差交易 ─────────────────────────────────────────────────────────
    by_pnl = sorted(all_positions, key=lambda p: p.pnl, reverse=True)
    print(f"\n  最优3笔:")
    for p in by_pnl[:3]:
        print(f"    {p.code} {p.signal_kind} "
              f"{p.entry_date.date()}→{p.exit_date.date() if p.exit_date else '?'} "
              f"¥{p.pnl:+,.0f} ({p.exit_reason})")
    print(f"  最差3笔:")
    for p in by_pnl[-3:]:
        print(f"    {p.code} {p.signal_kind} "
              f"{p.entry_date.date()}→{p.exit_date.date() if p.exit_date else '?'} "
              f"¥{p.pnl:+,.0f} ({p.exit_reason})")

    print(f"\n{'='*65}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="A股缠论多级别回测")
    parser.add_argument("--symbols", type=str, default="",
                        help="逗号分隔的股票代码，如 000001,600036")
    parser.add_argument("--start", type=str, default="",
                        help="回测起始日 YYYY-MM-DD，默认 2023-01-01")
    parser.add_argument("--end", type=str, default="",
                        help="回测结束日 YYYY-MM-DD，默认今天")
    parser.add_argument("--capital", type=float, default=INITIAL_CAPITAL,
                        help=f"初始资金，默认 {INITIAL_CAPITAL:,.0f}")
    parser.add_argument("--no-30min", action="store_true",
                        help="关闭30分钟级别确认（默认关闭）")
    parser.add_argument("--strict-30min", action="store_true",
                        help="开启30分钟级别强制确认")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] \
              if args.symbols else DEFAULT_SYMBOLS

    sim_start = datetime.strptime(args.start, "%Y-%m-%d") if args.start else SIM_START
    sim_end   = datetime.strptime(args.end, "%Y-%m-%d") if args.end else SIM_END

    strict_30min = args.strict_30min and not args.no_30min

    run_backtest(
        symbols=symbols,
        sim_start=sim_start,
        sim_end=sim_end,
        initial_capital=args.capital,
        strict_30min=strict_30min,
    )
