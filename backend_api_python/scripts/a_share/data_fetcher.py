"""
data_fetcher.py — A股K线拉取（独立脚本用，不依赖容器内 app 模块）

数据源优先级：
  日线/周线 → AkShare（东方财富，国内直连）→ yfinance（备用）
  30分钟     → AkShare（东方财富分钟线）→ yfinance

本地缓存：
  scripts/a_share/kline_cache/{code}_{timeframe}.csv
  格式: ts(ms), open, high, low, close, volume

用法:
  from data_fetcher import fetch_klines
  df = fetch_klines('000001', '1d', limit=500)  # 平安银行日线

股票代码格式（输入自动规范化）：
  '000001' / '600036' / 'SZ000001' / 'SH600036'
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
CACHE_DIR = Path(__file__).parent / "kline_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# AkShare 分钟级周期映射
_AK_MINUTE_PERIOD = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60", "1h": "60",
}

# yfinance 周期映射
_YF_INTERVAL = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "60m": "1h",
    "1d": "1d", "1w": "1wk", "1W": "1wk",
}

# yfinance 需要完整 ticker（加交易所后缀）
_YF_SUFFIX = {}  # filled in normalize_code


# ─────────────────────────────────────────────────────────────────────────────
# 代码规范化
# ─────────────────────────────────────────────────────────────────────────────

def normalize_code(code: str) -> tuple[str, str, str]:
    """
    返回 (pure_code, exchange, yf_ticker)

    pure_code : '000001'
    exchange  : 'SZ' | 'SH'
    yf_ticker : '000001.SZ' | '600036.SS'
    """
    code = code.upper().strip()
    # 去掉已有前缀
    for prefix in ("SZ", "SH", "600", "000", "002", "300", "688"):
        if code.startswith(prefix) and len(code) > 6:
            code = code.replace("SZ", "").replace("SH", "").strip()
            break

    # 保留纯数字部分
    pure = "".join(c for c in code if c.isdigit()).zfill(6)

    if pure.startswith(("6", "9")):
        exchange = "SH"
        yf_ticker = f"{pure}.SS"
    elif pure.startswith(("0", "2", "3")):
        exchange = "SZ"
        yf_ticker = f"{pure}.SZ"
    elif pure.startswith("688"):
        exchange = "SH"
        yf_ticker = f"{pure}.SS"
    else:
        exchange = "SZ"
        yf_ticker = f"{pure}.SZ"

    return pure, exchange, yf_ticker


# ─────────────────────────────────────────────────────────────────────────────
# 缓存 IO
# ─────────────────────────────────────────────────────────────────────────────

def _cache_path(code: str, timeframe: str) -> Path:
    return CACHE_DIR / f"{code}_{timeframe}.csv"


def _load_cache(code: str, timeframe: str) -> pd.DataFrame:
    p = _cache_path(code, timeframe)
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(p, dtype={"ts": int})
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.sort_values("ts").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def _save_cache(df: pd.DataFrame, code: str, timeframe: str) -> None:
    if df.empty:
        return
    cols = ["ts", "open", "high", "low", "close", "volume"]
    df[cols].to_csv(_cache_path(code, timeframe), index=False)


# ─────────────────────────────────────────────────────────────────────────────
# AkShare 拉取
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_akshare_daily(code: str, start: str, end: str,
                          adj: str = "qfq") -> pd.DataFrame:
    """
    拉取日线（前复权）
    code: 纯6位代码
    start/end: 'YYYYMMDD'
    """
    try:
        import akshare as ak
        raw = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=start, end_date=end,
            adjust=adj,
        )
        if raw is None or raw.empty:
            return pd.DataFrame()
        # 列名：日期 开盘 收盘 最高 最低 成交量 ...
        col_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
        }
        raw = raw.rename(columns=col_map)
        raw["ts"] = pd.to_datetime(raw["date"]).astype("int64") // 10**6
        return raw[["ts", "open", "high", "low", "close", "volume"]].copy()
    except Exception as e:
        print(f"[akshare daily] {code}: {e}")
        return pd.DataFrame()


def _fetch_akshare_weekly(code: str, start: str, end: str,
                           adj: str = "qfq") -> pd.DataFrame:
    try:
        import akshare as ak
        raw = ak.stock_zh_a_hist(
            symbol=code, period="weekly",
            start_date=start, end_date=end,
            adjust=adj,
        )
        if raw is None or raw.empty:
            return pd.DataFrame()
        col_map = {
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
        }
        raw = raw.rename(columns=col_map)
        raw["ts"] = pd.to_datetime(raw["date"]).astype("int64") // 10**6
        return raw[["ts", "open", "high", "low", "close", "volume"]].copy()
    except Exception as e:
        print(f"[akshare weekly] {code}: {e}")
        return pd.DataFrame()


def _fetch_akshare_minute(code: str, period: str = "30",
                           adj: str = "qfq") -> pd.DataFrame:
    """
    拉取分钟线（默认30分钟），akshare 不支持指定起始日期，返回近期数据。
    """
    try:
        import akshare as ak
        raw = ak.stock_zh_a_hist_min_em(
            symbol=code, period=period, adjust=adj,
            start_date="1970-01-01 09:30:00",
            end_date=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        if raw is None or raw.empty:
            return pd.DataFrame()
        col_map = {
            "时间": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
        }
        raw = raw.rename(columns=col_map)
        raw["ts"] = pd.to_datetime(raw["date"]).astype("int64") // 10**6
        return raw[["ts", "open", "high", "low", "close", "volume"]].copy()
    except Exception as e:
        print(f"[akshare minute] {code}: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# yfinance 备用
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_yfinance(yf_ticker: str, interval: str,
                    start: str, end: str) -> pd.DataFrame:
    try:
        import yfinance as yf
        raw = yf.download(
            yf_ticker, start=start, end=end,
            interval=interval, progress=False, auto_adjust=True,
        )
        if raw is None or raw.empty:
            return pd.DataFrame()

        # 新版 yfinance 返回 MultiIndex 列，需要先压平
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = [col[0] for col in raw.columns]

        raw = raw.reset_index()
        date_col = "Datetime" if "Datetime" in raw.columns else "Date"
        raw["ts"] = pd.to_datetime(raw[date_col]).astype("int64") // 10**6
        col_map = {"Open": "open", "High": "high", "Low": "low",
                   "Close": "close", "Volume": "volume"}
        raw = raw.rename(columns=col_map)
        return raw[["ts", "open", "high", "low", "close", "volume"]].copy()
    except Exception as e:
        print(f"[yfinance] {yf_ticker} {interval}: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────────────────────────────────────────

def fetch_klines(
    code: str,
    timeframe: str = "1d",
    limit: int = 500,
    no_cache: bool = False,
    adj: str = "qfq",
) -> pd.DataFrame:
    """
    拉取A股K线，自动缓存。

    Args:
        code      : 股票代码（'000001' / '600036' 等）
        timeframe : '30m' / '1d' / '1w'
        limit     : 最多返回的K线根数
        no_cache  : True 时跳过缓存直接拉取
        adj       : 复权方式（'qfq'=前复权, 'hfq'=后复权, ''=不复权）

    Returns:
        DataFrame, 列: ts(ms), open, high, low, close, volume, dt(datetime)
        按时间升序，仅返回 limit 根。
    """
    pure, exchange, yf_ticker = normalize_code(code)
    tf = timeframe.lower()
    cache_key = f"{adj}_{tf}"

    # 计算拉取起始日期（多拿一些用于指标预热）
    today = datetime.now()
    warmup_extra = max(limit * 2, 500)

    if tf in ("1d", "d"):
        start_dt = today - timedelta(days=warmup_extra)
        end_dt   = today + timedelta(days=1)
        start_s  = start_dt.strftime("%Y%m%d")
        end_s    = end_dt.strftime("%Y%m%d")
        yf_int   = "1d"
        ak_fetch = lambda: _fetch_akshare_daily(pure, start_s, end_s, adj)
        yf_fetch = lambda: _fetch_yfinance(yf_ticker, yf_int,
                                            start_dt.strftime("%Y-%m-%d"),
                                            end_dt.strftime("%Y-%m-%d"))
    elif tf in ("1w", "w"):
        start_dt = today - timedelta(weeks=warmup_extra)
        end_dt   = today + timedelta(days=7)
        start_s  = start_dt.strftime("%Y%m%d")
        end_s    = end_dt.strftime("%Y%m%d")
        yf_int   = "1wk"
        ak_fetch = lambda: _fetch_akshare_weekly(pure, start_s, end_s, adj)
        yf_fetch = lambda: _fetch_yfinance(yf_ticker, yf_int,
                                            start_dt.strftime("%Y-%m-%d"),
                                            end_dt.strftime("%Y-%m-%d"))
    elif tf in _AK_MINUTE_PERIOD:
        period = _AK_MINUTE_PERIOD[tf]
        ak_fetch = lambda: _fetch_akshare_minute(pure, period, adj)
        yf_int   = _YF_INTERVAL.get(tf, "30m")
        start_dt = today - timedelta(days=60)  # 分钟线通常只能拿60天内
        end_dt   = today + timedelta(days=1)
        yf_fetch = lambda: _fetch_yfinance(yf_ticker, yf_int,
                                            start_dt.strftime("%Y-%m-%d"),
                                            end_dt.strftime("%Y-%m-%d"))
    else:
        raise ValueError(f"不支持的周期: {timeframe}，支持: 30m/1d/1w")

    # 尝试加载缓存
    cached = pd.DataFrame() if no_cache else _load_cache(pure, cache_key)

    if cached.empty:
        # 无缓存：按优先级拉取
        df = ak_fetch()
        if df.empty:
            print(f"[data_fetcher] AkShare 拉取失败，尝试 yfinance: {code}")
            df = yf_fetch()
        if not df.empty:
            _save_cache(df, pure, cache_key)
    else:
        # 有缓存：检查是否需要增量更新（最新缓存超过1个K线周期）
        last_ts_ms = int(cached["ts"].iloc[-1])
        last_dt    = datetime.fromtimestamp(last_ts_ms / 1000)

        if tf in ("1d", "d"):
            stale = (today - last_dt).days >= 1 and today.hour >= 16
        elif tf in ("1w", "w"):
            stale = (today - last_dt).days >= 7
        else:
            stale = (today - last_dt).total_seconds() >= 30 * 60

        if stale:
            print(f"[data_fetcher] 缓存过期，增量更新: {code} {timeframe}")
            fresh = ak_fetch()
            if fresh.empty:
                fresh = yf_fetch()
            if not fresh.empty:
                merged = pd.concat([cached, fresh], ignore_index=True)
                merged = merged.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
                _save_cache(merged, pure, cache_key)
                cached = merged

        df = cached

    if df.empty:
        return pd.DataFrame()

    df = df.sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)

    # 确保数值类型（兼容 yfinance MultiIndex 遗留列）
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            continue
        val = df[col]
        if isinstance(val, pd.DataFrame):
            val = val.iloc[:, 0]
        df[col] = pd.to_numeric(val, errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])

    return df.tail(limit).reset_index(drop=True)


def fetch_multi_timeframe(
    code: str,
    timeframes: Optional[list] = None,
    limit_map: Optional[dict] = None,
    no_cache: bool = False,
) -> dict[str, pd.DataFrame]:
    """
    同时拉取多个周期，返回 {timeframe: DataFrame}。

    默认：
      weekly  (1w)  : 200 根
      daily   (1d)  : 500 根
      30min   (30m) : 500 根
    """
    from typing import Optional  # noqa: F811 (local)
    if timeframes is None:
        timeframes = ["1w", "1d", "30m"]
    if limit_map is None:
        limit_map = {"1w": 200, "1d": 500, "30m": 500}

    result = {}
    for tf in timeframes:
        lim = limit_map.get(tf, 500)
        result[tf] = fetch_klines(code, tf, limit=lim, no_cache=no_cache)
    return result
