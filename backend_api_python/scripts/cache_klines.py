"""
cache_klines.py
拉取 top_symbols_output.json 中所有标的的 4H OHLCV 历史数据，
保存到 kline_cache/ 目录（每个标的一个 CSV 文件）。

已有缓存时自动增量更新（只拉最后一根 K 线之后的新数据）。

用法:
  docker exec quantdinger-backend python scripts/cache_klines.py

可选环境变量:
  PROXY_URL=http://127.0.0.1:7890
  CACHE_SINCE=2025-11-01        从此日期开始拉取（首次初始化用）
  SYMBOLS_FILE=                 自定义标的文件（默认 top_symbols_output.json）
  FORCE_REFRESH=1               强制全量重拉（忽略已有缓存）
"""
import os, sys, json, time as _time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

try:
    import ccxt
except ImportError:
    print("[ERROR] pip install ccxt")
    sys.exit(1)

# ── 配置 ─────────────────────────────────────────────────────────────────────
PROXY_URL     = os.environ.get("PROXY_URL", "")
SYMBOLS_FILE  = os.environ.get(
    "SYMBOLS_FILE",
    os.path.join(os.path.dirname(__file__), "top_symbols_output.json"),
)
CACHE_DIR     = Path(os.path.dirname(__file__)) / "kline_cache"
TIMEFRAME     = "4h"
# 默认从 60 天前开始（回测暖机用），可通过环境变量覆盖
_default_since = (datetime.now(tz=timezone.utc) - timedelta(days=210)).strftime("%Y-%m-%d")
CACHE_SINCE   = os.environ.get("CACHE_SINCE", _default_since)
FORCE_REFRESH = os.environ.get("FORCE_REFRESH", "0") == "1"
MAX_SYMBOLS   = 100

# ─────────────────────────────────────────────────────────────────────────────

def build_exchange() -> ccxt.Exchange:
    opts = {"enableRateLimit": True}
    if PROXY_URL:
        opts["proxies"] = {"http": PROXY_URL, "https": PROXY_URL}
    return ccxt.binanceusdm(opts)


def cache_path(symbol: str) -> Path:
    """BTC/USDT → kline_cache/BTC_USDT.csv"""
    return CACHE_DIR / (symbol.replace("/", "_") + ".csv")


def load_cache(symbol: str) -> pd.DataFrame:
    """读取本地缓存，返回 DataFrame；不存在则返回空 DataFrame。"""
    p = cache_path(symbol)
    if not p.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(p, dtype={"ts": int})
        df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        return df.sort_values("ts").reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def save_cache(symbol: str, df: pd.DataFrame) -> None:
    """将 DataFrame 保存到本地缓存。"""
    if df.empty:
        return
    p = cache_path(symbol)
    # 只保存数值列，dt 可以从 ts 重建
    df[["ts", "open", "high", "low", "close", "volume"]].to_csv(p, index=False)


def fetch_since(exchange: ccxt.Exchange, symbol: str,
                since_ms: int, end_ms: int) -> pd.DataFrame:
    """从 since_ms 到 end_ms 分页拉取 OHLCV，返回 DataFrame。"""
    all_bars = []
    cur = since_ms
    while cur < end_ms:
        try:
            bars = exchange.fetch_ohlcv(symbol, TIMEFRAME, since=cur, limit=500)
        except Exception as e:
            print(f"      [WARN] fetch_ohlcv error: {e}")
            break
        if not bars:
            break
        all_bars.extend(bars)
        if bars[-1][0] >= end_ms or len(bars) < 500:
            break
        cur = bars[-1][0] + 1
        _time.sleep(0.15)

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df[df["ts"] < end_ms]
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def update_symbol(exchange: ccxt.Exchange, symbol: str,
                  global_since_ms: int, now_ms: int) -> tuple[int, str]:
    """
    增量更新单个标的缓存。
    返回 (bar_count, status_str)。
    """
    existing = pd.DataFrame() if FORCE_REFRESH else load_cache(symbol)

    if existing.empty:
        # 全量拉取
        since_ms = global_since_ms
        status = "全量"
    else:
        last_ts = int(existing["ts"].iloc[-1])
        since_ms = last_ts + 1   # 从上次最后一根之后拉
        if since_ms >= now_ms:
            # 已是最新，无需更新
            return len(existing), "已是最新"
        status = "增量"

    new_df = fetch_since(exchange, symbol, since_ms, now_ms)

    if new_df.empty:
        if not existing.empty:
            return len(existing), f"{status}（无新数据）"
        return 0, "无数据"

    # 合并
    combined = pd.concat([existing, new_df], ignore_index=True) if not existing.empty else new_df
    combined = combined.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    save_cache(symbol, combined)
    return len(combined), f"{status}+{len(new_df)}根"


def main():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if not os.path.exists(SYMBOLS_FILE):
        print(f"[ERROR] 找不到 {SYMBOLS_FILE}，请先运行 fetch_top_symbols.py")
        sys.exit(1)

    with open(SYMBOLS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    symbols = [s.split(":")[-1] for s in raw.get("symbol_list", [])[:MAX_SYMBOLS]]
    print(f"[INFO] 标的数量 : {len(symbols)}")
    print(f"[INFO] 缓存目录 : {CACHE_DIR}")
    print(f"[INFO] 起始日期 : {CACHE_SINCE}  强制重拉: {FORCE_REFRESH}")
    print()

    exchange = build_exchange()

    since_dt  = datetime.fromisoformat(CACHE_SINCE).replace(tzinfo=timezone.utc)
    now_dt    = datetime.now(tz=timezone.utc)
    since_ms  = int(since_dt.timestamp() * 1000)
    now_ms    = int(now_dt.timestamp() * 1000)

    ok, skip, fail = 0, 0, 0
    for i, sym in enumerate(symbols, 1):
        print(f"  [{i:>3}/{len(symbols)}] {sym:<22}", end=" ", flush=True)
        try:
            count, status = update_symbol(exchange, sym, since_ms, now_ms)
            if count == 0:
                print(f"⚠  {status}")
                fail += 1
            elif status == "已是最新":
                print(f"✓  {count} 根 K线  ({status})")
                skip += 1
            else:
                # 读缓存以显示日期范围
                df = load_cache(sym)
                if not df.empty:
                    d0 = df["dt"].iloc[0].strftime("%Y-%m-%d")
                    d1 = df["dt"].iloc[-1].strftime("%Y-%m-%d")
                    print(f"✓  {count} 根 K线  {d0} ~ {d1}  [{status}]")
                else:
                    print(f"✓  {count} 根 K线  [{status}]")
                ok += 1
        except Exception as e:
            print(f"✗  错误: {e}")
            fail += 1

    print(f"\n[INFO] 完成: 更新 {ok}  跳过 {skip}  失败 {fail}")
    print(f"[INFO] 缓存目录: {CACHE_DIR}")


if __name__ == "__main__":
    main()
