#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
缠论日线级别震荡中枢分析

数据源: D:/tongdaxin/vipdoc/{sh,sz,bj}/lday/
输出:   data/zhongshu/{code}.json

算法步骤:
  1. 读取通达信 .day 二进制文件
  2. 处理 K 线包含关系
  3. 识别顶底分型
  4. 识别笔（至少间隔 5 根处理后 K 线）
  5. 识别震荡中枢（连续三笔价格区间有重叠）并尝试延伸
"""

import argparse
import struct
import json
import sys
import tarfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

TDX_ROOT   = Path("D:/tongdaxin/vipdoc")
OUTPUT_DIR = Path("D:/project/QuantDinger/data/zhongshu")
EXCHANGES  = ["sh", "sz", "bj"]
MIN_BARS   = 30   # 少于此数量的股票跳过
MAX_WORKERS = 16


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------

def load_day_file(filepath: Path) -> list[dict]:
    """读取通达信 .day 文件，返回 OHLC 列表（按日期升序）"""
    records = []
    with open(filepath, "rb") as f:
        data = f.read()
    record_size = 32
    for i in range(0, len(data) - record_size + 1, record_size):
        d, o, h, l, c, amt, vol, _ = struct.unpack("<IIIIIfII", data[i : i + record_size])
        if d == 0:
            continue
        records.append({
            "date":  d,
            "open":  o / 100,
            "high":  h / 100,
            "low":   l / 100,
            "close": c / 100,
        })
    return records


# ---------------------------------------------------------------------------
# 包含关系处理
# ---------------------------------------------------------------------------

def remove_containment(bars: list[dict]) -> list[dict]:
    """
    处理 K 线包含关系，返回无包含的处理序列。

    包含: K1.high >= K2.high and K1.low <= K2.low（或反向）
    合并: 上升趋势取较高值，下降趋势取较低值
    """
    if len(bars) < 2:
        return [{"high": b["high"], "low": b["low"], "date": b["date"]} for b in bars]

    result: list[dict] = [{"high": bars[0]["high"], "low": bars[0]["low"], "date": bars[0]["date"]}]

    for bar in bars[1:]:
        prev = result[-1]
        h1, l1 = prev["high"], prev["low"]
        h2, l2 = bar["high"], bar["low"]

        # 检查包含关系
        if (h1 >= h2 and l1 <= l2) or (h2 >= h1 and l2 <= l1):
            if len(result) >= 2:
                trend_up = prev["high"] > result[-2]["high"]
            else:
                trend_up = True  # 默认上升

            if trend_up:
                result[-1] = {
                    "high": max(h1, h2),
                    "low":  max(l1, l2),
                    "date": bar["date"] if h2 >= h1 else prev["date"],
                }
            else:
                result[-1] = {
                    "high": min(h1, h2),
                    "low":  min(l1, l2),
                    "date": bar["date"] if l2 <= l1 else prev["date"],
                }
        else:
            result.append({"high": h2, "low": l2, "date": bar["date"]})

    return result


# ---------------------------------------------------------------------------
# 分型识别
# ---------------------------------------------------------------------------

def find_fractals(bars: list[dict]) -> list[dict]:
    """
    在无包含的 K 线序列中识别顶底分型。

    顶分型: 中间 K 的最高价严格大于两侧
    底分型: 中间 K 的最低价严格小于两侧
    """
    fractals = []
    n = len(bars)
    for i in range(1, n - 1):
        prev, cur, nxt = bars[i - 1], bars[i], bars[i + 1]
        if cur["high"] > prev["high"] and cur["high"] > nxt["high"]:
            fractals.append({"type": "top",    "idx": i, "date": cur["date"], "price": cur["high"]})
        elif cur["low"] < prev["low"] and cur["low"] < nxt["low"]:
            fractals.append({"type": "bottom", "idx": i, "date": cur["date"], "price": cur["low"]})
    return fractals


# ---------------------------------------------------------------------------
# 笔识别
# ---------------------------------------------------------------------------

def build_bi(fractals: list[dict]) -> list[dict]:
    """
    从分型序列构建笔列表。

    规则:
      - 相邻分型必须交替（顶→底 or 底→顶）
      - 两端分型 idx 差 >= 4（处理后序列中至少 5 根 K 线含两端）
      - 若同向分型连续出现，保留更极端者
    """
    if not fractals:
        return []

    # 筛选有效分型序列
    valid: list[dict] = [fractals[0]]
    for f in fractals[1:]:
        last = valid[-1]
        if f["type"] == last["type"]:
            # 同向：取更极端
            if f["type"] == "top" and f["price"] >= last["price"]:
                valid[-1] = f
            elif f["type"] == "bottom" and f["price"] <= last["price"]:
                valid[-1] = f
        else:
            if f["idx"] - last["idx"] >= 4:
                valid.append(f)
            else:
                # 距离不足：取更极端
                if f["type"] == "top" and f["price"] > last["price"]:
                    valid[-1] = f
                elif f["type"] == "bottom" and f["price"] < last["price"]:
                    valid[-1] = f

    # 构建笔
    bi_list = []
    for i in range(len(valid) - 1):
        s, e = valid[i], valid[i + 1]
        if s["type"] == e["type"]:
            continue
        bi_list.append({
            "start_date":  s["date"],
            "end_date":    e["date"],
            "start_price": s["price"],
            "end_price":   e["price"],
            "direction":   "down" if s["type"] == "top" else "up",
            "start_idx":   s["idx"],
            "end_idx":     e["idx"],
        })
    return bi_list


# ---------------------------------------------------------------------------
# 中枢识别
# ---------------------------------------------------------------------------

def find_zhongshu(bi_list: list[dict]) -> list[dict]:
    """
    从笔列表中识别震荡中枢并尝试延伸。

    中枢定义（三笔重叠）:
      向下进入: top→bottom→top→bottom  ZD=max(两底), ZG=min(两顶)
      向上进入: bottom→top→bottom→top  ZD=max(两底), ZG=min(两顶)
      有效条件: ZG > ZD

    延伸规则:
      - 后续下行笔 end_price >= ZD → 仍在中枢内
      - 后续上行笔 end_price <= ZG → 仍在中枢内
      - 否则中枢结束
    """
    result = []
    i = 0
    n = len(bi_list)

    while i <= n - 3:
        b1, b2, b3 = bi_list[i], bi_list[i + 1], bi_list[i + 2]

        if b1["direction"] == "down":
            zd = max(b1["end_price"],   b3["end_price"])
            zg = min(b1["start_price"], b2["end_price"])
        else:
            zd = max(b1["start_price"], b2["end_price"])
            zg = min(b1["end_price"],   b3["end_price"])

        if zg <= zd:
            i += 1
            continue

        # 有效中枢，尝试延伸
        end_i = i + 2
        while end_i + 1 < n:
            nb = bi_list[end_i + 1]
            if nb["direction"] == "down":
                if nb["end_price"] >= zd:
                    end_i += 1
                else:
                    break
            else:
                if nb["end_price"] <= zg:
                    end_i += 1
                else:
                    break

        result.append({
            "start_date": int(bi_list[i]["start_date"]),
            "end_date":   int(bi_list[end_i]["end_date"]),
            "zd":         round(zd, 3),
            "zg":         round(zg, 3),
            "direction":  b1["direction"],   # 进入中枢的方向
            "bi_count":   end_i - i + 1,     # 中枢内笔数
        })

        i = end_i + 1

    return result


# ---------------------------------------------------------------------------
# 单股分析
# ---------------------------------------------------------------------------

def analyze_stock(args: tuple) -> tuple[str, list | None, int | None]:
    """分析单只股票，返回 (code, zhongshu_list, last_date)"""
    filepath, code = args
    try:
        bars = load_day_file(filepath)
        if len(bars) < MIN_BARS:
            return code, None, None

        clean   = remove_containment(bars)
        fracs   = find_fractals(clean)
        bi_list = build_bi(fracs)
        zs_list = find_zhongshu(bi_list)

        return code, zs_list, bars[-1]["date"]
    except Exception:
        return code, None, None


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 收集所有 .day 文件
    tasks: list[tuple[Path, str]] = []
    for ex in EXCHANGES:
        lday_dir = TDX_ROOT / ex / "lday"
        if not lday_dir.exists():
            continue
        for f in sorted(lday_dir.glob(f"{ex}*.day")):
            tasks.append((f, f.stem))

    total = len(tasks)
    print(f"找到 {total} 只股票，使用 {MAX_WORKERS} 线程开始分析...", flush=True)

    done = success = skipped = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(analyze_stock, t): t[1] for t in tasks}
        for future in as_completed(futures):
            code, zs_list, last_date = future.result()
            done += 1

            if zs_list is None:
                skipped += 1
            else:
                out = {
                    "code":           code,
                    "last_date":      last_date,
                    "zhongshu_count": len(zs_list),
                    "zhongshu":       zs_list,
                }
                (OUTPUT_DIR / f"{code}.json").write_text(
                    json.dumps(out, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                success += 1

            if done % 1000 == 0 or done == total:
                pct = done / total * 100
                print(f"  [{pct:5.1f}%] {done}/{total}  成功:{success}  跳过:{skipped}", flush=True)

    print(f"\n完成。成功:{success}  跳过:{skipped}  输出:{OUTPUT_DIR}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="缠论日线中枢分析工具")
    parser.add_argument("--pack",   action="store_true", help="打包 data/zhongshu/ → data/zhongshu.tar.gz")
    parser.add_argument("--unpack", action="store_true", help="解压 data/zhongshu.tar.gz → data/zhongshu/")
    args = parser.parse_args()

    ARCHIVE = Path("D:/project/QuantDinger/data/zhongshu.tar.gz")

    if args.pack:
        if not OUTPUT_DIR.exists():
            print(f"错误: {OUTPUT_DIR} 不存在，请先运行分析")
            sys.exit(1)
        ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(ARCHIVE, "w:gz") as tar:
            tar.add(OUTPUT_DIR, arcname="zhongshu")
        size_mb = ARCHIVE.stat().st_size / 1024 / 1024
        print(f"已打包 → {ARCHIVE}  ({size_mb:.1f} MB)")

    elif args.unpack:
        if not ARCHIVE.exists():
            print(f"错误: {ARCHIVE} 不存在")
            sys.exit(1)
        OUTPUT_DIR.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(ARCHIVE, "r:gz") as tar:
            tar.extractall(OUTPUT_DIR.parent)
        count = len(list(OUTPUT_DIR.glob("*.json")))
        print(f"已解压 → {OUTPUT_DIR}  ({count} 个文件)")

    else:
        main()
