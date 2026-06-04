"""
fetch_top_symbols.py
从交易所拉取主要 USDT 交易对，按 24h 成交额排序，输出截面策略用的 symbol_list。

用法:
  # 在项目容器内运行
  docker exec quantdinger-backend python scripts/fetch_top_symbols.py

  # 本地直接运行 (需要 pip install ccxt)
  cd backend_api_python
  python scripts/fetch_top_symbols.py

可选参数(环境变量):
  EXCHANGE=binance        交易所 (binance / okx / bybit / gate / bitget)
  MARKET_TYPE=swap        行情类型 (swap=永续合约 / spot=现货)
  TOP_N=60                保留前 N 个标的
  MIN_VOLUME_USDT=5000000 最低 24h 成交额过滤 (USDT)
  PROXY_URL=              代理地址, 例如 http://127.0.0.1:7890
"""
import os
import sys
import json
import time

try:
    import ccxt
except ImportError:
    print("[ERROR] 未找到 ccxt，请先安装: pip install ccxt")
    sys.exit(1)

# ── 配置 ────────────────────────────────────────────────────────────────────
EXCHANGE_ID    = os.environ.get("EXCHANGE", "binance").lower()
MARKET_TYPE    = os.environ.get("MARKET_TYPE", "swap").lower()   # swap=合约 spot=现货
TOP_N          = int(os.environ.get("TOP_N", "100"))
MIN_VOLUME     = float(os.environ.get("MIN_VOLUME_USDT", "5_000_000"))
PROXY_URL      = os.environ.get("PROXY_URL", "")

# 固定排除的标的：稳定币对、杠杆代币、指数合约等噪音标的
EXCLUDE_BASES = {
    "USDC", "BUSD", "TUSD", "USDP", "DAI", "FDUSD",  # 稳定币
    "BTCDOM", "DEFI", "ALTDOM",                         # 指数
    "UP", "DOWN", "BULL", "BEAR",                       # 杠杆代币后缀
}

EXCLUDE_SUFFIXES = ("UP", "DOWN", "3L", "3S", "5L", "5S", "BULL", "BEAR")

# 质量过滤参数
MIN_BASE_LEN   = int(os.environ.get("MIN_BASE_LEN", "2"))    # base symbol 最少字符数，过滤 H/A 等单字母垃圾
MAX_BASE_LEN   = int(os.environ.get("MAX_BASE_LEN", "10"))   # base symbol 最多字符数
MAX_CHANGE_PCT = float(os.environ.get("MAX_CHANGE_PCT", "50"))  # 过滤24h涨跌幅绝对值超过此值的(暴涨暴跌的新币/meme)
MIN_PRICE      = float(os.environ.get("MIN_PRICE", "0.000001"))  # 最低价格，过滤接近归零的垃圾币


def _build_exchange() -> ccxt.Exchange:
    opts: dict = {"enableRateLimit": True}

    if MARKET_TYPE == "swap":
        if EXCHANGE_ID == "binance":
            cls = ccxt.binanceusdm
        elif EXCHANGE_ID == "okx":
            cls = ccxt.okx
            opts["defaultType"] = "swap"
        elif EXCHANGE_ID == "bybit":
            cls = ccxt.bybit
            opts["defaultType"] = "linear"
        elif EXCHANGE_ID == "gate":
            cls = ccxt.gate
            opts["defaultType"] = "swap"
        elif EXCHANGE_ID == "bitget":
            cls = ccxt.bitget
            opts["defaultType"] = "swap"
        else:
            cls = getattr(ccxt, EXCHANGE_ID)
    else:
        cls = getattr(ccxt, EXCHANGE_ID)

    if PROXY_URL:
        opts["proxies"] = {"http": PROXY_URL, "https": PROXY_URL}
        opts["aiohttp_proxy"] = PROXY_URL

    return cls(opts)


def _is_excluded(symbol: str, price: float = 0, change_pct: float = 0) -> bool:
    base = symbol.split("/")[0]

    # 固定黑名单
    if base in EXCLUDE_BASES:
        return True
    if any(base.endswith(s) for s in EXCLUDE_SUFFIXES):
        return True

    # 非 ASCII 字符 (e.g. 币安人生)
    if not base.isascii():
        return True

    # 只允许字母和数字组合，过滤含特殊字符的
    if not all(c.isalnum() for c in base):
        return True

    # 长度过滤：太短(单字母如 H)或太长都排除
    if len(base) < MIN_BASE_LEN or len(base) > MAX_BASE_LEN:
        return True

    # 价格过低
    if price > 0 and price < MIN_PRICE:
        return True

    # 24h 涨跌幅过大：暴涨暴跌通常是新上线 meme 币，缠论信号不稳定
    if abs(change_pct) > MAX_CHANGE_PCT:
        return True

    return False


def _verify_kline(exchange: "ccxt.Exchange", symbol: str) -> bool:
    """验证该标的在当前交易所+市场类型下确实有可拉取的 OHLCV K 线数据。"""
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe="4h", limit=5)
        return bool(bars and len(bars) >= 3)
    except Exception:
        return False


def fetch_top_symbols() -> list[dict]:
    print(f"[INFO] 连接 {EXCHANGE_ID} ({MARKET_TYPE}) ...")
    exchange = _build_exchange()

    # 拉取全量 ticker
    print("[INFO] 拉取 24h ticker 数据 (可能需要 10-30 秒) ...")
    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"[ERROR] fetch_tickers 失败: {e}")
        sys.exit(1)

    candidates = []
    for symbol, t in tickers.items():
        # 只要 USDT 计价（合约格式 BASE/USDT:USDT 或现货格式 BASE/USDT）
        if not symbol.endswith("/USDT") and not symbol.endswith("/USDT:USDT"):
            continue

        price      = float(t.get("last") or 0)
        change_pct = float(t.get("percentage") or 0)
        quote_vol  = float(t.get("quoteVolume") or 0)

        if quote_vol < MIN_VOLUME:
            continue
        if _is_excluded(symbol, price, change_pct):
            continue

        # 统一成 BASE/USDT 格式（去掉合约后缀 :USDT）
        clean = symbol.split(":")[0]
        candidates.append({
            "symbol":      clean,
            "raw_symbol":  symbol,          # 保留原始格式用于 K 线验证
            "base":        clean.split("/")[0],
            "volume_usdt": quote_vol,
            "price":       price,
            "change_24h":  round(change_pct, 2),
        })

    # 按成交额降序，先取 TOP_N * 2 作为候选，再逐一验证 K 线可用性
    candidates.sort(key=lambda x: x["volume_usdt"], reverse=True)
    oversample = min(len(candidates), TOP_N * 2)
    candidates = candidates[:oversample]

    print(f"[INFO] 候选标的 {len(candidates)} 个，正在验证 K 线可用性...")
    results = []
    skipped = []
    for c in candidates:
        if len(results) >= TOP_N:
            break
        # 用原始 symbol 格式（含 :USDT 后缀）做验证，确保走合约市场
        raw = c["raw_symbol"]
        if _verify_kline(exchange, raw):
            results.append({k: v for k, v in c.items() if k != "raw_symbol"})
        else:
            skipped.append(c["symbol"])

    if skipped:
        print(f"[INFO] 跳过 {len(skipped)} 个无 K 线数据的标的: {', '.join(skipped)}")

    return results


def main() -> None:
    symbols = fetch_top_symbols()

    if not symbols:
        print("[WARN] 未获取到任何标的，请检查网络或代理设置。")
        sys.exit(1)

    print(f"\n[INFO] 共获取 {len(symbols)} 个标的\n")
    print(f"{'排名':<5} {'交易对':<18} {'24h成交额(亿U)':<18} {'涨跌幅'}")
    print("-" * 55)
    for i, s in enumerate(symbols, 1):
        vol_b = s["volume_usdt"] / 1e8
        print(f"{i:<5} {s['symbol']:<18} {vol_b:<18.2f} {s['change_24h']:+.2f}%")

    # 生成截面策略格式的 symbol_list
    symbol_list = [f"Crypto:{s['symbol']}" for s in symbols]

    print("\n" + "=" * 60)
    print("截面策略 symbol_list (复制到策略配置中):")
    print("=" * 60)
    print(json.dumps(symbol_list, indent=2, ensure_ascii=False))

    # 保存到文件
    output = {
        "generated_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
        "exchange":       EXCHANGE_ID,
        "market_type":    MARKET_TYPE,
        "top_n":          TOP_N,
        "min_volume_usdt": MIN_VOLUME,
        "total":          len(symbols),
        "symbol_list":    symbol_list,
        "detail":         symbols,
    }

    out_path = os.path.join(os.path.dirname(__file__), "top_symbols_output.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n[INFO] 完整结果已保存到: {out_path}")
    print(f"[INFO] 可将以上 symbol_list 直接粘贴到截面策略的 trading_config 中")


if __name__ == "__main__":
    main()
