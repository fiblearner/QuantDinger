#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
从通达信本地 fzline/*.lc5 读取 5 分钟 K 线，合成 30 分钟 / 1 小时数据。

数据源: D:/tongdaxin/vipdoc/{sh,sz,bj}/fzline/*.lc5（通达信本地文件）
覆盖范围: 最近约 447 个交易日（~1.8 年）
输出: data/kline_30m/{code}.csv
      data/kline_1h/{code}.csv

用法:
  python scripts/fetch_intraday.py             # 全部股票
  python scripts/fetch_intraday.py --resume    # 跳过已存在的文件
  python scripts/fetch_intraday.py --test      # 仅测试前 5 只股票
"""

import csv
import struct
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import argparse

TDX_ROOT  = Path("D:/tongdaxin/vipdoc")
OUT_30M   = Path("D:/project/QuantDinger/data/kline_30m")
OUT_1H    = Path("D:/project/QuantDinger/data/kline_1h")
EXCHANGES = ["sh", "sz", "bj"]
MAX_WORKERS = 8

CSV_FIELDS = ["datetime", "open", "high", "low", "close", "vol", "amount"]

# lc5 record: date(H) time(H) open high low close vol(f) amount(I) reserved(I)
LC5_FMT  = "<HHfffffII"
LC5_SIZE = struct.calcsize(LC5_FMT)  # 32 bytes


# --------------------------------------------------------------------------
# 解码 TDX 日期字段
# --------------------------------------------------------------------------
def decode_tdx_date(v: int) -> tuple[int, int, int]:
    """TDX lc5 日期编码: year = 2004 + v//2048, month/day 在余数中"""
    year  = 2004 + v // 2048
    md    = v % 2048
    month = md // 100
    day   = md % 100
    return year, month, day


# --------------------------------------------------------------------------
# 读取单只股票的 lc5 文件 → 5 分钟 bar 列表
# --------------------------------------------------------------------------
def read_lc5(path: Path) -> list[dict]:
    """读取 lc5 文件，返回按时间升序的 5 分钟 bar 列表（datetime 为字符串）。"""
    data = path.read_bytes()
    n    = len(data) // LC5_SIZE
    bars = []
    for i in range(n):
        chunk = data[i * LC5_SIZE : (i + 1) * LC5_SIZE]
        d = struct.unpack(LC5_FMT, chunk)
        date_raw, time_min = d[0], d[1]
        yr, mo, dy = decode_tdx_date(date_raw)
        hr, mi     = divmod(time_min, 60)
        dt_str     = f"{yr:04d}-{mo:02d}-{dy:02d} {hr:02d}:{mi:02d}"
        bars.append({
            "datetime": dt_str,
            "open":     round(float(d[2]), 3),
            "high":     round(float(d[3]), 3),
            "low":      round(float(d[4]), 3),
            "close":    round(float(d[5]), 3),
            "vol":      int(d[6]),
            "amount":   int(d[7]),
        })
    # lc5 已是升序，但以防万一
    bars.sort(key=lambda r: r["datetime"])
    return bars


# --------------------------------------------------------------------------
# 5 分钟 → N 分钟 合成（手工 resample，不依赖 pandas）
# --------------------------------------------------------------------------
def resample(bars: list[dict], minutes: int) -> list[dict]:
    """
    将 5 分钟 bar 按 `minutes` 合成。
    A 股合并规则: 取同一分钟窗口内的 O/H/L/C/Vol/Amount。
    窗口以 close time 右对齐（与通达信显示一致）。

    对于 30 分钟 (minutes=30):
      [09:35..10:00] → 10:00
      [10:05..10:30] → 10:30  ...
    对于 60 分钟 (minutes=60):
      [09:35..10:30] → 10:30
      [10:35..11:30] → 11:30
      [13:05..14:00] → 14:00
      [14:05..15:00] → 15:00
    """
    if not bars:
        return []

    def bucket_key(dt_str: str) -> str:
        """将 bar 的收盘时间映射到所属周期的最终时间（YYYY-MM-DD HH:MM）。"""
        date_part = dt_str[:10]
        hh, mm    = int(dt_str[11:13]), int(dt_str[14:16])
        total_min = hh * 60 + mm
        # 向上对齐到 minutes 的整倍数（以分钟计）
        rounded = ((total_min - 1) // minutes + 1) * minutes
        rh, rm  = divmod(rounded, 60)
        return f"{date_part} {rh:02d}:{rm:02d}"

    from collections import defaultdict
    buckets: dict[str, list[dict]] = defaultdict(list)
    for b in bars:
        buckets[bucket_key(b["datetime"])].append(b)

    result = []
    for key in sorted(buckets):
        grp = buckets[key]
        result.append({
            "datetime": key,
            "open":     grp[0]["open"],
            "high":     max(b["high"] for b in grp),
            "low":      min(b["low"]  for b in grp),
            "close":    grp[-1]["close"],
            "vol":      sum(b["vol"]    for b in grp),
            "amount":   sum(b["amount"] for b in grp),
        })
    return result


# --------------------------------------------------------------------------
# 保存 CSV
# --------------------------------------------------------------------------
def save_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------
# 单股处理
# --------------------------------------------------------------------------
def process_stock(code: str, resume: bool) -> tuple[str, int, int]:
    """返回 (code, bars_30m, bars_1h)"""
    ex   = code[:2]
    lc5  = TDX_ROOT / ex / "fzline" / f"{code}.lc5"
    if not lc5.exists():
        return code, 0, 0

    path_30m = OUT_30M / f"{code}.csv"
    path_1h  = OUT_1H  / f"{code}.csv"

    skip_30m = resume and path_30m.exists()
    skip_1h  = resume and path_1h.exists()

    if skip_30m and skip_1h:
        return code, 0, 0

    try:
        bars_5m = read_lc5(lc5)
    except Exception as e:
        print(f"[WARN] {code}: {e}", file=sys.stderr)
        return code, 0, 0

    n30, n1h = 0, 0

    if not skip_30m:
        bars = resample(bars_5m, 30)
        if bars:
            save_csv(bars, path_30m)
            n30 = len(bars)

    if not skip_1h:
        bars = resample(bars_5m, 60)
        if bars:
            save_csv(bars, path_1h)
            n1h = len(bars)

    return code, n30, n1h


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main(resume: bool = False, test: bool = False) -> None:
    OUT_30M.mkdir(parents=True, exist_ok=True)
    OUT_1H.mkdir(parents=True, exist_ok=True)

    # 收集有 lc5 文件的股票列表
    codes = []
    for ex in EXCHANGES:
        for f in sorted((TDX_ROOT / ex / "fzline").glob(f"{ex}*.lc5")):
            codes.append(f.stem)
    if test:
        # 取 SH 普通股以便测试时有实际数据
        sh_codes = [c for c in codes if c.startswith("sh6")]
        codes = sh_codes[:5] if sh_codes else codes[:5]

    total  = len(codes)
    done   = success = skipped = 0
    t0     = time.time()

    print(f"共 {total} 只股票，{'resume 模式' if resume else '全量'}，"
          f"{MAX_WORKERS} 线程...", flush=True)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_stock, c, resume): c for c in codes}
        for fut in as_completed(futures):
            code, n30, n1h = fut.result()
            done += 1
            if n30 == 0 and n1h == 0:
                skipped += 1
            else:
                success += 1

            if done % 500 == 0 or done == total:
                elapsed = time.time() - t0
                eta = elapsed / done * (total - done) if done else 0
                print(f"  [{done}/{total}]  成功:{success}  空/跳过:{skipped}"
                      f"  已用:{elapsed/60:.1f}min  剩余:{eta/60:.0f}min",
                      flush=True)

    elapsed = time.time() - t0
    sz_30m = sum(p.stat().st_size for p in OUT_30M.glob("*.csv"))
    sz_1h  = sum(p.stat().st_size for p in OUT_1H.glob("*.csv"))
    print(f"\n完成。耗时 {elapsed:.1f} 秒")
    print(f"  30min: {len(list(OUT_30M.glob('*.csv')))} 文件 / {sz_30m/1e6:.1f} MB")
    print(f"  1h:    {len(list(OUT_1H.glob('*.csv')))} 文件 / {sz_1h/1e6:.1f} MB")


# --------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="从通达信 lc5 合成分钟 K 线")
    parser.add_argument("--resume", action="store_true",
                        help="跳过已存在的文件（断点续建）")
    parser.add_argument("--test",   action="store_true",
                        help="仅处理前 5 只 SH 股票（测试）")
    args = parser.parse_args()
    main(resume=args.resume, test=args.test)
