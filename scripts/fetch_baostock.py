#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
用 BaoStock 获取历史分钟 K 线（2015-01-01 ~ 2024-08-12）

fzline 本地数据只覆盖最近 ~447 交易日（2024-08-13 起）；本脚本补全更早的历史。
历史数据写入独立目录 data/kline_30m_hist / data/kline_1h_hist，
跑完后用 --merge 合并到最终 data/kline_30m / data/kline_1h。

用法:
  python scripts/fetch_baostock.py             # 拉取历史（全量）
  python scripts/fetch_baostock.py --resume    # 跳过已拉取的文件
  python scripts/fetch_baostock.py --test      # 仅测试 5 只 SH 股票
  python scripts/fetch_baostock.py --merge     # 合并历史 + fzline → 最终 CSV
"""

import csv
import sys
import time
from pathlib import Path
import argparse

ZHONGSHU_DIR  = Path("D:/project/QuantDinger/data/zhongshu")
OUT_30M_HIST  = Path("D:/project/QuantDinger/data/kline_30m_hist")
OUT_1H_HIST   = Path("D:/project/QuantDinger/data/kline_1h_hist")
OUT_30M_FINAL = Path("D:/project/QuantDinger/data/kline_30m")
OUT_1H_FINAL  = Path("D:/project/QuantDinger/data/kline_1h")

# BaoStock 补全截止日期（fzline 从 2024-08-13 起）
BAO_START = "2015-01-01"
BAO_END   = "2024-08-12"

CSV_FIELDS = ["datetime", "open", "high", "low", "close", "vol", "amount"]


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
def tdx_to_bao(code: str) -> str | None:
    """sh600519 → sh.600519；BJ 股 BaoStock 不支持，返回 None"""
    return None if code.startswith("bj") else f"{code[:2]}.{code[2:]}"


def parse_bao_time(date_str: str, time_str: str) -> str:
    """
    date_str: "2024-07-01"
    time_str: "20240701100000000"（17位）
    → "2024-07-01 10:00"
    """
    h = time_str[8:10]
    m = time_str[10:12]
    return f"{date_str} {h}:{m}"


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------
# BaoStock 拉取
# --------------------------------------------------------------------------
def fetch_freq(bs, bao_code: str, freq: str) -> list[dict]:
    """拉取单只股票指定周期的 BAO_START ~ BAO_END 历史数据"""
    rs = bs.query_history_k_data_plus(
        bao_code,
        "date,time,open,high,low,close,volume,amount",
        start_date=BAO_START,
        end_date=BAO_END,
        frequency=freq,
        adjustflag="3",
    )
    rows = []
    while rs.error_code == "0" and rs.next():
        r = rs.get_row_data()
        try:
            dt = parse_bao_time(r[0], r[1])
            if not r[2]:       # 跳过空行
                continue
            rows.append({
                "datetime": dt,
                "open":     round(float(r[2]), 3),
                "high":     round(float(r[3]), 3),
                "low":      round(float(r[4]), 3),
                "close":    round(float(r[5]), 3),
                "vol":      int(float(r[6])),
                "amount":   round(float(r[7]), 2),
            })
        except (ValueError, IndexError):
            continue
    return rows


def process_stock(bs, code: str, resume: bool) -> tuple[str, int, int]:
    bao_code = tdx_to_bao(code)
    if not bao_code:
        return code, 0, 0

    path_30m = OUT_30M_HIST / f"{code}.csv"
    path_1h  = OUT_1H_HIST  / f"{code}.csv"

    skip_30m = resume and path_30m.exists()
    skip_1h  = resume and path_1h.exists()

    n30 = n1h = 0

    if not skip_30m:
        rows = fetch_freq(bs, bao_code, "30")
        if rows:
            write_csv(rows, path_30m)
            n30 = len(rows)
        time.sleep(0.1)

    if not skip_1h:
        rows = fetch_freq(bs, bao_code, "60")
        if rows:
            write_csv(rows, path_1h)
            n1h = len(rows)
        time.sleep(0.1)

    return code, n30, n1h


# --------------------------------------------------------------------------
# 合并：历史（BaoStock）+ 近期（fzline）→ 最终 CSV
# --------------------------------------------------------------------------
def merge_all() -> None:
    codes = sorted(p.stem for p in ZHONGSHU_DIR.glob("*.json"))
    merged = skipped = 0

    for code in codes:
        for hist_dir, final_dir in [
            (OUT_30M_HIST, OUT_30M_FINAL),
            (OUT_1H_HIST,  OUT_1H_FINAL),
        ]:
            hist_path  = hist_dir  / f"{code}.csv"
            final_path = final_dir / f"{code}.csv"

            hist_rows  = read_csv(hist_path)
            final_rows = read_csv(final_path)

            if not hist_rows and not final_rows:
                skipped += 1
                continue

            # 去重合并，按 datetime 升序
            combined = {r["datetime"]: r for r in hist_rows}
            combined.update({r["datetime"]: r for r in final_rows})
            sorted_rows = sorted(combined.values(), key=lambda r: r["datetime"])

            write_csv(sorted_rows, final_path)
            merged += 1

    print(f"合并完成：{merged} 个文件已更新，{skipped} 个跳过")


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main(resume: bool = False, test: bool = False) -> None:
    import baostock as bs

    OUT_30M_HIST.mkdir(parents=True, exist_ok=True)
    OUT_1H_HIST.mkdir(parents=True, exist_ok=True)

    codes = sorted(
        p.stem for p in ZHONGSHU_DIR.glob("*.json")
        if not p.stem.startswith("bj")
    )
    if test:
        codes = [c for c in codes if c.startswith("sh6")][:5]

    lg = bs.login()
    if lg.error_code != "0":
        print(f"BaoStock 登录失败: {lg.error_msg}", file=sys.stderr)
        sys.exit(1)
    print(f"BaoStock 登录成功，共 {len(codes)} 只股票（排除 BJ），"
          f"补全 {BAO_START} ~ {BAO_END}...", flush=True)

    total  = len(codes)
    done   = success = skipped = 0
    t0     = time.time()

    try:
        for code in codes:
            try:
                _, n30, n1h = process_stock(bs, code, resume)
                done += 1
                if n30 == 0 and n1h == 0:
                    skipped += 1
                else:
                    success += 1
            except Exception as e:
                print(f"[WARN] {code}: {e}", file=sys.stderr)
                done += 1
                skipped += 1

            if done % 200 == 0 or done == total:
                elapsed = time.time() - t0
                eta = elapsed / done * (total - done) if done else 0
                print(f"  [{done}/{total}]  成功:{success}  跳过:{skipped}"
                      f"  已用:{elapsed/60:.1f}min  剩余:{eta/60:.0f}min",
                      flush=True)
    finally:
        bs.logout()

    elapsed = time.time() - t0
    sz_30m = sum(p.stat().st_size for p in OUT_30M_HIST.glob("*.csv"))
    sz_1h  = sum(p.stat().st_size for p in OUT_1H_HIST.glob("*.csv"))
    print(f"\n完成。耗时 {elapsed/60:.1f} 分钟")
    print(f"  30min_hist: {len(list(OUT_30M_HIST.glob('*.csv')))} 文件 / {sz_30m/1e9:.2f} GB")
    print(f"  1h_hist:    {len(list(OUT_1H_HIST.glob('*.csv')))} 文件 / {sz_1h/1e9:.2f} GB")
    print(f"\n下一步：运行 python scripts/fetch_baostock.py --merge 合并到最终目录")


# --------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BaoStock 历史分钟 K 线补全工具")
    parser.add_argument("--resume", action="store_true", help="跳过已拉取的文件")
    parser.add_argument("--test",   action="store_true", help="仅处理 5 只 SH 股票")
    parser.add_argument("--merge",  action="store_true",
                        help="合并 kline_30m_hist + kline_30m → kline_30m（最终）")
    args = parser.parse_args()

    if args.merge:
        merge_all()
    else:
        main(resume=args.resume, test=args.test)
