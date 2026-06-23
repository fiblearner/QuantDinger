#!/usr/bin/env bash
# 等待 fzline + BaoStock 两个任务都完成，然后自动合并 + 打包
set -e

LOG_FZ="D:/project/QuantDinger/scripts/fetch_intraday_log.txt"
LOG_BAO="D:/project/QuantDinger/scripts/fetch_baostock_log.txt"
OUT_DIR="D:/project/QuantDinger/data"

echo "[auto_finalize] waiting for fzline..."
# grep for ASCII pattern that appears in the completion summary line
until grep -q "30min:" "$LOG_FZ" 2>/dev/null; do sleep 30; done
echo "[auto_finalize] fzline done"

echo "[auto_finalize] waiting for BaoStock..."
until grep -q "30min_hist:" "$LOG_BAO" 2>/dev/null; do sleep 60; done
echo "[auto_finalize] BaoStock done"

echo "[auto_finalize] 开始合并历史 + 近期数据..."
cd D:/project/QuantDinger
python scripts/fetch_baostock.py --merge
echo "[auto_finalize] 合并完成 ✓"

echo "[auto_finalize] 打包 kline_30m..."
tar -czf "$OUT_DIR/kline_30m.tar.gz" -C "$OUT_DIR" kline_30m/
SIZE_30M=$(du -sh "$OUT_DIR/kline_30m.tar.gz" | cut -f1)
echo "[auto_finalize] kline_30m.tar.gz 完成 ($SIZE_30M) ✓"

echo "[auto_finalize] 打包 kline_1h..."
tar -czf "$OUT_DIR/kline_1h.tar.gz" -C "$OUT_DIR" kline_1h/
SIZE_1H=$(du -sh "$OUT_DIR/kline_1h.tar.gz" | cut -f1)
echo "[auto_finalize] kline_1h.tar.gz 完成 ($SIZE_1H) ✓"

echo ""
echo "======================================="
echo " 全部完成，可以传文件了："
echo "   $OUT_DIR/kline_30m.tar.gz  ($SIZE_30M)"
echo "   $OUT_DIR/kline_1h.tar.gz   ($SIZE_1H)"
echo "======================================="
