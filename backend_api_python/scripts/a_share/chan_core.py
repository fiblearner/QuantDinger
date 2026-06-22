"""
chan_core.py — 完整缠论分析引擎（纯算法，无IO）

实现层级：
  原始K线 → 包含处理（合并K线）→ 分型识别 → 笔 → 线段 → 中枢 → 背驰 → 买卖点

多级别联立（由调用方组织）：
  周线：大趋势方向（线段方向 + 中枢抬升/下移）
  日线：识别中枢、一/二/三买卖点
  30分钟：精确入场（低级别背驰 / 一买确认）

数据约定（输入 DataFrame 必须包含的列）：
  ts    : int，毫秒时间戳（或 datetime，函数内部统一）
  open, high, low, close : float
  volume : float

返回的买卖点字典字段：
  bar_idx   : int，信号在合并K序列中的索引
  type      : str，'b1'/'b2'/'b3'/'s1'/'s2'/'s3'
  price     : float，触发时收盘价
  stop      : float，建议止损价（0 = 无）
  bi_dir    : int，1=向上笔 -1=向下笔
  zs_ZG     : float，所属中枢高点
  zs_ZD     : float，所属中枢低点
  desc      : str，可读描述
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MergedBar:
    """包含处理后的合并K线"""
    idx: int           # 在原始序列中的最后一根K的索引
    high: float
    low: float
    close: float
    volume: float
    raw_indices: List[int] = field(default_factory=list)  # 包含的原始K索引


@dataclass
class Fractal:
    """分型（顶/底）"""
    bar_idx: int       # 分型中间K在合并K序列的索引
    kind: str          # 'top' | 'bot'
    price: float       # 顶分型取 high，底分型取 low
    raw_idx: int       # 对应原始K索引（近似用中间K）


@dataclass
class Bi:
    """笔"""
    start: Fractal
    end: Fractal
    direction: int     # 1=向上  -1=向下


@dataclass
class Duan:
    """线段"""
    start_bi_idx: int  # 在 bi_list 中的起始笔索引
    end_bi_idx: int
    direction: int     # 1=向上  -1=向下
    start_price: float
    end_price: float


@dataclass
class Zhongshu:
    """中枢"""
    ZG: float          # 中枢顶
    ZD: float          # 中枢底
    start_bi_idx: int
    end_bi_idx: int
    level: str         # 'bi'=笔中枢  'duan'=线段中枢
    above_count: int = 0  # 离开中枢向上的笔数（用于背驰）
    below_count: int = 0  # 离开中枢向下的笔数


@dataclass
class Signal:
    """买卖点信号"""
    bar_idx: int       # 合并K序列的索引
    raw_idx: int       # 原始K索引
    kind: str          # 'b1'/'b2'/'b3'/'s1'/'s2'/'s3'
    price: float
    stop: float
    zs_ZG: float
    zs_ZD: float
    bi_dir: int
    desc: str


# ─────────────────────────────────────────────────────────────────────────────
# 步骤 1：包含处理
# ─────────────────────────────────────────────────────────────────────────────

def merge_bars(df: pd.DataFrame) -> List[MergedBar]:
    """
    对原始OHLC序列做包含处理，返回合并K序列。

    包含规则：
      当前K high<=前K high 且 low>=前K low（当前K被前K包含）→ 合并
      当前K high>=前K high 且 low<=前K low（前K被当前K包含）→ 合并
    上升趋势取高高、低高；下降趋势取低低、高低。
    趋势方向由未包含的相邻K决定。
    """
    highs = df["high"].tolist()
    lows  = df["low"].tolist()
    closes = df["close"].tolist()
    vols  = df["volume"].tolist()
    n = len(highs)

    merged: List[MergedBar] = []
    direction = 1  # 初始假设上升

    for i in range(n):
        raw_h, raw_l = highs[i], lows[i]

        if not merged:
            merged.append(MergedBar(
                idx=i, high=raw_h, low=raw_l,
                close=closes[i], volume=vols[i], raw_indices=[i]
            ))
            continue

        prev = merged[-1]
        ph, pl = prev.high, prev.low

        # 判断是否包含
        cur_contains_prev = (raw_h >= ph and raw_l <= pl)
        prev_contains_cur = (raw_h <= ph and raw_l >= pl)

        if cur_contains_prev or prev_contains_cur:
            # 合并
            if direction >= 0:
                new_h = max(ph, raw_h)
                new_l = max(pl, raw_l)
            else:
                new_h = min(ph, raw_h)
                new_l = min(pl, raw_l)
            prev.high = new_h
            prev.low  = new_l
            prev.close = closes[i]
            prev.volume = prev.volume + vols[i]
            prev.raw_indices.append(i)
            prev.idx = i
        else:
            # 不包含，更新方向
            direction = 1 if raw_h > ph else -1
            merged.append(MergedBar(
                idx=i, high=raw_h, low=raw_l,
                close=closes[i], volume=vols[i], raw_indices=[i]
            ))

    return merged


# ─────────────────────────────────────────────────────────────────────────────
# 步骤 2：分型识别（严格3K定型）
# ─────────────────────────────────────────────────────────────────────────────

def find_fractals(bars: List[MergedBar]) -> List[Fractal]:
    """
    顶分型：bars[i-1].high < bars[i].high > bars[i+1].high
            且 bars[i-1].low < bars[i].low > bars[i+1].low
    底分型：bars[i-1].high > bars[i].high < bars[i+1].high
            且 bars[i-1].low > bars[i].low < bars[i+1].low
    相邻同类分型只保留极值最优的一个。
    """
    fractals: List[Fractal] = []
    n = len(bars)

    for i in range(1, n - 1):
        b0, b1, b2 = bars[i - 1], bars[i], bars[i + 1]
        is_top = (b1.high > b0.high and b1.high > b2.high
                  and b1.low  > b0.low  and b1.low  > b2.low)
        is_bot = (b1.high < b0.high and b1.high < b2.high
                  and b1.low  < b0.low  and b1.low  < b2.low)

        if not is_top and not is_bot:
            continue

        kind  = 'top' if is_top else 'bot'
        price = b1.high if is_top else b1.low

        # 与上一个分型合并（相邻同类取极值）
        if fractals and fractals[-1].kind == kind:
            if (kind == 'top' and price >= fractals[-1].price) or \
               (kind == 'bot' and price <= fractals[-1].price):
                fractals[-1] = Fractal(bar_idx=i, kind=kind, price=price, raw_idx=b1.idx)
        else:
            fractals.append(Fractal(bar_idx=i, kind=kind, price=price, raw_idx=b1.idx))

    return fractals


# ─────────────────────────────────────────────────────────────────────────────
# 步骤 3：笔识别（分型间至少 5 根独立合并K）
# ─────────────────────────────────────────────────────────────────────────────

MIN_BI_GAP = 4  # 两个分型之间至少有 4 根独立K（即分型间隔 ≥ 5 根合并K）

def find_bi(fractals: List[Fractal]) -> List[Bi]:
    """
    从分型序列中识别笔。
    约束：
      1. 顶底交替
      2. 相邻顶底之间（bar_idx 差值）≥ MIN_BI_GAP+1
    """
    if len(fractals) < 2:
        return []

    # 过滤保证顶底交替
    pivots: List[Fractal] = []
    for f in fractals:
        if not pivots:
            pivots.append(f)
            continue
        last = pivots[-1]
        if f.kind == last.kind:
            # 同类取极值
            if (f.kind == 'top' and f.price > last.price) or \
               (f.kind == 'bot' and f.price < last.price):
                pivots[-1] = f
        else:
            if f.bar_idx - last.bar_idx >= MIN_BI_GAP + 1:
                pivots.append(f)
            else:
                # 间距不足，同类取极值替换
                if (f.kind == 'top' and f.price > last.price) or \
                   (f.kind == 'bot' and f.price < last.price):
                    pivots[-1] = f

    bi_list: List[Bi] = []
    for i in range(1, len(pivots)):
        s, e = pivots[i - 1], pivots[i]
        direction = 1 if e.kind == 'top' else -1
        bi_list.append(Bi(start=s, end=e, direction=direction))

    return bi_list


# ─────────────────────────────────────────────────────────────────────────────
# 步骤 4：线段识别（3笔 + 特征序列）
# ─────────────────────────────────────────────────────────────────────────────

def find_duan(bi_list: List[Bi]) -> List[Duan]:
    """
    线段识别：
      - 至少由 3 笔组成
      - 向上线段：第1笔向上，第3笔的高点超过第1笔高点
      - 向下线段：第1笔向下，第3笔的低点低于第1笔低点
      - 线段结束：被对向线段打破特征序列
    简化实现：3笔越过条件 + 逐步扩展。
    """
    n = len(bi_list)
    if n < 3:
        return []

    duan_list: List[Duan] = []
    i = 0

    while i + 2 < n:
        b0, b1, b2 = bi_list[i], bi_list[i + 1], bi_list[i + 2]
        d = b0.direction

        # 向上线段：b0向上，b2向上且b2高点>b0高点
        # 向下线段：b0向下，b2向下且b2低点<b0低点
        b0_start = b0.start.price
        b0_end   = b0.end.price
        b2_start = b2.start.price
        b2_end   = b2.end.price

        if d == 1:
            start_price, end_price = b0_start, b0_end
            cond = (b2.direction == 1 and b2_end > b0_end)
        else:
            start_price, end_price = b0_start, b0_end
            cond = (b2.direction == -1 and b2_end < b0_end)

        if not cond:
            i += 1
            continue

        # 找到初始3笔线段，尝试向右扩展
        seg_end_bi = i + 2
        cur_end = b2_end

        j = seg_end_bi + 1
        while j + 1 < n:
            nxt0, nxt1 = bi_list[j], bi_list[j + 1]
            # 同向笔且超过当前线段终点 → 扩展
            if nxt1.direction == d:
                if d == 1 and nxt1.end.price > cur_end:
                    cur_end = nxt1.end.price
                    seg_end_bi = j + 1
                elif d == -1 and nxt1.end.price < cur_end:
                    cur_end = nxt1.end.price
                    seg_end_bi = j + 1
            j += 2

        seg = Duan(
            start_bi_idx=i,
            end_bi_idx=seg_end_bi,
            direction=d,
            start_price=start_price,
            end_price=cur_end,
        )
        duan_list.append(seg)
        i = seg_end_bi  # 下一段从本段结束笔开始

    return duan_list


# ─────────────────────────────────────────────────────────────────────────────
# 步骤 5：中枢识别（基于笔，用于买卖点判断）
# ─────────────────────────────────────────────────────────────────────────────

def find_zhongshu(bi_list: List[Bi]) -> List[Zhongshu]:
    """
    从笔列表中识别中枢（≥3笔重叠区间）。
    ZG = 三笔高点中最低值，ZD = 三笔低点中最高值，ZG > ZD 才构成中枢。
    相邻中枢若重叠则合并（扩展中枢）。
    """
    zs_list: List[Zhongshu] = []
    n = len(bi_list)

    if n < 3:
        return zs_list

    for i in range(n - 2):
        b1, b2, b3 = bi_list[i], bi_list[i + 1], bi_list[i + 2]
        highs = [max(b.start.price, b.end.price) for b in [b1, b2, b3]]
        lows  = [min(b.start.price, b.end.price) for b in [b1, b2, b3]]
        ZG, ZD = min(highs), max(lows)

        if ZG <= ZD:
            continue

        if zs_list and zs_list[-1].end_bi_idx >= i:
            # 扩展已有中枢（保守：只延伸，不缩小范围）
            zs_list[-1].ZG = min(zs_list[-1].ZG, ZG)
            zs_list[-1].ZD = max(zs_list[-1].ZD, ZD)
            zs_list[-1].end_bi_idx = i + 2
        else:
            zs_list.append(Zhongshu(
                ZG=ZG, ZD=ZD,
                start_bi_idx=i, end_bi_idx=i + 2,
                level='bi',
            ))

    return zs_list


# ─────────────────────────────────────────────────────────────────────────────
# 步骤 6：背驰判断（MACD 面积法 / 简化斜率法）
# ─────────────────────────────────────────────────────────────────────────────

def calc_macd_histogram(close: pd.Series,
                        fast: int = 12, slow: int = 26, signal: int = 9
                        ) -> pd.Series:
    """返回 MACD 柱状图（DIF - DEA）"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif      = ema_fast - ema_slow
    dea      = dif.ewm(span=signal, adjust=False).mean()
    hist     = dif - dea
    return hist


def _bi_macd_area(bi: Bi, bars: List[MergedBar], hist: pd.Series) -> float:
    """
    计算某笔对应合并K区间内 MACD 柱面积的绝对值。
    用于背驰判断：若当前离开中枢的笔面积 < 前一同向笔 → 背驰。
    """
    start_raw = bi.start.raw_idx
    end_raw   = bi.end.raw_idx
    lo, hi = min(start_raw, end_raw), max(start_raw, end_raw)
    if lo >= len(hist) or hi >= len(hist):
        return 0.0
    area = float(hist.iloc[lo:hi + 1].abs().sum())
    return area


def has_beichi(
    bi_list: List[Bi],
    zs: Zhongshu,
    bars: List[MergedBar],
    hist: pd.Series,
    direction: int,
) -> bool:
    """
    判断中枢后最新一段趋势是否背驰。

    direction=1（上涨背驰，用于顶背驰→卖点）：
      找中枢后向上的两段笔，若第二次向上的 MACD 面积 < 第一次 → 背驰

    direction=-1（下跌背驰，用于底背驰→买点）：
      找中枢后向下的两段笔，若第二次向下的 MACD 面积 < 第一次 → 背驰
    """
    post = bi_list[zs.end_bi_idx + 1:]
    same_dir = [b for b in post if b.direction == direction]
    if len(same_dir) < 2:
        return False

    area_prev = _bi_macd_area(same_dir[-2], bars, hist)
    area_last = _bi_macd_area(same_dir[-1], bars, hist)

    return area_last < area_prev * 0.9  # 允许 10% 误差


# ─────────────────────────────────────────────────────────────────────────────
# 步骤 7：RSI
# ─────────────────────────────────────────────────────────────────────────────

def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rsi   = 100 - (100 / (1 + gain / loss.replace(0, float("nan"))))
    return rsi.fillna(50)


# ─────────────────────────────────────────────────────────────────────────────
# 步骤 8：买卖点识别
# ─────────────────────────────────────────────────────────────────────────────

STOP_BUFFER = 0.98   # 止损价 = 结构低点 × 0.98
DIST_2B     = 0.10   # 二买：当前价距底部 ≤ 10%（A股波动比加密小）
DIST_3B     = 0.07   # 三买：当前价距回踩低点 ≤ 7%
DIST_2S     = 0.10   # 二卖：当前价距顶部 ≤ 10%
DIST_3S     = 0.07   # 三卖：当前价距回踩高点 ≤ 7%


def find_signals(
    df: pd.DataFrame,
    bars: Optional[List[MergedBar]] = None,
) -> List[Signal]:
    """
    主入口：输入原始 OHLCV DataFrame，返回所有买卖点信号列表。

    流程：
      1. 包含处理
      2. 分型
      3. 笔
      4. 中枢
      5. MACD
      6. 买卖点

    只返回最新信号（当前截面），适合逐日回测时传入截至当天的 df。
    """
    if len(df) < 60:
        return []

    close = df["close"].reset_index(drop=True)
    hist  = calc_macd_histogram(close)
    rsi   = calc_rsi(close)
    rsi_v = float(rsi.iloc[-1])

    if bars is None:
        bars = merge_bars(df)

    fractals = find_fractals(bars)
    bi_list  = find_bi(fractals)
    zs_list  = find_zhongshu(bi_list)

    if not zs_list or len(bi_list) < 3:
        return []

    signals: List[Signal] = []
    cur_price = float(close.iloc[-1])
    cur_raw   = len(df) - 1
    cur_bar   = len(bars) - 1

    # ── 趋势过滤：用 MA240 或线段方向 ──────────────────────────────────────
    ma240 = float(close.rolling(240, min_periods=30).mean().iloc[-1]) if len(close) >= 30 else cur_price
    trend_up   = cur_price > ma240
    trend_down = cur_price < ma240

    lz = zs_list[-1]  # 最近中枢

    # ── 一类买点（b1）：中枢后下跌笔背驰 + 价格创新低 ──────────────────────
    if trend_up or (not trend_up and not trend_down):
        post_bi = bi_list[lz.end_bi_idx + 1:]
        if post_bi and post_bi[-1].direction == -1:
            last_bi = post_bi[-1]
            beichi = has_beichi(bi_list, lz, bars, hist, direction=-1)
            if beichi:
                stop = round(last_bi.end.price * STOP_BUFFER, 4)
                score = _rsi_adjust(70, rsi_v)
                signals.append(Signal(
                    bar_idx=cur_bar, raw_idx=cur_raw,
                    kind='b1', price=cur_price, stop=stop,
                    zs_ZG=lz.ZG, zs_ZD=lz.ZD, bi_dir=-1,
                    desc=f"一买：中枢后下跌笔背驰，底={last_bi.end.price:.4f}，止损={stop:.4f}",
                ))

    # ── 二类买点（b2）：中枢后上升笔，回调底 > ZD，未跌破 ZD ──────────────
    if len(bi_list) >= 3:
        post = bi_list[lz.end_bi_idx + 1:]
        if post:
            last = post[-1]
            if last.direction == 1:  # 最后笔向上
                bot = last.start.price
                if bot > lz.ZD:
                    dist = (cur_price - bot) / (bot + 1e-9)
                    if 0 <= dist <= DIST_2B:
                        stop = round(lz.ZD * STOP_BUFFER, 4)
                        raw_score = 75 * (1 - dist / DIST_2B)
                        score = _rsi_adjust(raw_score, rsi_v)
                        if score >= 55:
                            signals.append(Signal(
                                bar_idx=cur_bar, raw_idx=cur_raw,
                                kind='b2', price=cur_price, stop=stop,
                                zs_ZG=lz.ZG, zs_ZD=lz.ZD, bi_dir=1,
                                desc=f"二买：底部={bot:.4f} 距离={dist:.1%} 止损={stop:.4f}",
                            ))

    # ── 三类买点（b3）：突破 ZG 后回踩仍高于 ZG ──────────────────────────────
    if len(bi_list) >= 5:
        post = bi_list[lz.end_bi_idx + 1:]
        if len(post) >= 2:
            brk, pb = post[0], post[1]
            if brk.direction == 1 and brk.end.price > lz.ZG:
                if pb.direction == -1:
                    pbot = pb.end.price
                    if pbot > lz.ZG:
                        dist = (cur_price - pbot) / (pbot + 1e-9)
                        if 0 <= dist <= DIST_3B:
                            stop = round(lz.ZG * STOP_BUFFER, 4)
                            raw_score = 65 * (1 - dist / DIST_3B)
                            score = _rsi_adjust(raw_score, rsi_v)
                            if score >= 50:
                                signals.append(Signal(
                                    bar_idx=cur_bar, raw_idx=cur_raw,
                                    kind='b3', price=cur_price, stop=stop,
                                    zs_ZG=lz.ZG, zs_ZD=lz.ZD, bi_dir=1,
                                    desc=f"三买：回踩底={pbot:.4f} 距离={dist:.1%} 止损={stop:.4f}",
                                ))

    # ── 一类卖点（s1）：上涨笔背驰 + 顶背驰 ──────────────────────────────────
    post_bi = bi_list[lz.end_bi_idx + 1:]
    if post_bi and post_bi[-1].direction == 1:
        beichi = has_beichi(bi_list, lz, bars, hist, direction=1)
        if beichi:
            last_bi = post_bi[-1]
            signals.append(Signal(
                bar_idx=cur_bar, raw_idx=cur_raw,
                kind='s1', price=cur_price, stop=0.0,
                zs_ZG=lz.ZG, zs_ZD=lz.ZD, bi_dir=1,
                desc=f"一卖：上涨笔顶背驰，顶={last_bi.end.price:.4f}",
            ))

    # ── 二类卖点（s2）：中枢后反弹未突破 ZG，回落 ──────────────────────────
    if len(bi_list) >= 3:
        post = bi_list[lz.end_bi_idx + 1:]
        if post:
            last = post[-1]
            if last.direction == -1:  # 最后笔向下
                top = last.start.price
                if top < lz.ZG:
                    dist = (top - cur_price) / (top + 1e-9)
                    if 0 <= dist <= DIST_2S:
                        signals.append(Signal(
                            bar_idx=cur_bar, raw_idx=cur_raw,
                            kind='s2', price=cur_price, stop=0.0,
                            zs_ZG=lz.ZG, zs_ZD=lz.ZD, bi_dir=-1,
                            desc=f"二卖：顶部={top:.4f} 低于ZG={lz.ZG:.4f}",
                        ))

    # ── 三类卖点（s3）：跌破 ZD 后反弹仍低于 ZD ──────────────────────────────
    if len(bi_list) >= 5:
        post = bi_list[lz.end_bi_idx + 1:]
        if len(post) >= 2:
            brk, pb = post[0], post[1]
            if brk.direction == -1 and brk.end.price < lz.ZD:
                if pb.direction == 1:
                    ptop = pb.end.price
                    if ptop < lz.ZD:
                        dist = (ptop - cur_price) / (ptop + 1e-9)
                        if 0 <= dist <= DIST_3S:
                            signals.append(Signal(
                                bar_idx=cur_bar, raw_idx=cur_raw,
                                kind='s3', price=cur_price, stop=0.0,
                                zs_ZG=lz.ZG, zs_ZD=lz.ZD, bi_dir=-1,
                                desc=f"三卖：反弹顶={ptop:.4f} 低于ZD={lz.ZD:.4f}",
                            ))

    return signals


def _rsi_adjust(score: float, rsi: float) -> float:
    """RSI 调整：超卖加分，超买减分"""
    if rsi < 30:
        return min(100, score * 1.30)
    if rsi < 40:
        return min(100, score * 1.15)
    if rsi > 70:
        return score * 0.6
    if rsi > 60:
        return score * 0.85
    return score


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：趋势判断（用于多级别联立）
# ─────────────────────────────────────────────────────────────────────────────

def get_trend_direction(df: pd.DataFrame) -> int:
    """
    判断当前级别的趋势方向，用于多级别过滤。

    返回：
      1  = 上涨趋势（线段向上 + 中枢抬升）
     -1  = 下跌趋势
      0  = 震荡 / 不明

    算法：
      1. 最近两个中枢的 ZD 对比：ZD 抬升 → 上涨；ZD 下移 → 下跌
      2. 若中枢不足两个，看最后线段方向
      3. 若线段不足，看 MA20 vs 收盘价
    """
    if len(df) < 30:
        return 0

    bars      = merge_bars(df)
    fractals  = find_fractals(bars)
    bi_list   = find_bi(fractals)
    zs_list   = find_zhongshu(bi_list)
    duan_list = find_duan(bi_list)

    if len(zs_list) >= 2:
        if zs_list[-1].ZD > zs_list[-2].ZD and zs_list[-1].ZG > zs_list[-2].ZG:
            return 1
        if zs_list[-1].ZD < zs_list[-2].ZD and zs_list[-1].ZG < zs_list[-2].ZG:
            return -1
        return 0

    if duan_list:
        return duan_list[-1].direction

    close = df["close"].reset_index(drop=True)
    ma20  = float(close.rolling(20, min_periods=10).mean().iloc[-1])
    return 1 if float(close.iloc[-1]) > ma20 else -1


# ─────────────────────────────────────────────────────────────────────────────
# 辅助：完整分析结果（调试 / 可视化用）
# ─────────────────────────────────────────────────────────────────────────────

def analyze(df: pd.DataFrame) -> dict:
    """
    返回完整分析结果字典，供调试和可视化使用。

    Keys:
      bars      : List[MergedBar]
      fractals  : List[Fractal]
      bi        : List[Bi]
      duan      : List[Duan]
      zhongshu  : List[Zhongshu]
      signals   : List[Signal]
      trend     : int
    """
    bars      = merge_bars(df)
    fractals  = find_fractals(bars)
    bi_list   = find_bi(fractals)
    duan_list = find_duan(bi_list)
    zs_list   = find_zhongshu(bi_list)
    signals   = find_signals(df, bars=bars)
    trend     = get_trend_direction(df)

    return {
        "bars":     bars,
        "fractals": fractals,
        "bi":       bi_list,
        "duan":     duan_list,
        "zhongshu": zs_list,
        "signals":  signals,
        "trend":    trend,
    }
