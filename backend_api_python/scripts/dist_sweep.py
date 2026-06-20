"""
dist_sweep.py  —  扫描二买/三买入场距离阈值对回测收益的影响
用法: NO_FETCH=1 python3 dist_sweep.py
"""
import os, sys, subprocess, re

os.chdir(os.path.dirname(os.path.abspath(__file__)))

DIST_2B_VALS = [0.08, 0.12, 0.15, 0.20, 0.25, 0.30]
DIST_3B_VALS = [0.05, 0.08, 0.12, 0.15, 0.20]

print(f"{'DIST_2B':>8} {'DIST_3B':>8} {'收益%':>8} {'开仓数':>6} {'胜率%':>7} {'总盈亏$':>10}")
print("-" * 60)

for d2 in DIST_2B_VALS:
    for d3 in DIST_3B_VALS:
        env = os.environ.copy()
        env["NO_FETCH"] = "1"
        env["DIST_2B"] = str(d2)
        env["DIST_3B"] = str(d3)
        env["PROXY_URL"] = ""

        r = subprocess.run(
            [sys.executable, "backtest_chan.py"],
            capture_output=True, text=True, env=env
        )
        out = r.stdout

        equity = re.search(r'期末净值\s*:\s*\S+\s*\(([+-]?\d+\.?\d*)%\)', out)
        opens  = re.search(r'开仓次数\s*:\s*(\d+)', out)
        wr     = re.search(r'胜率\s*:\s*([\d.]+)%', out)
        pnl    = re.search(r'总盈亏\s*:\s*\$([\d,.-]+)', out)

        eq  = equity.group(1) if equity else "?"
        op  = opens.group(1)  if opens  else "?"
        w   = wr.group(1)     if wr     else "?"
        pv  = pnl.group(1).replace(',','') if pnl else "?"

        print(f"  {d2:>6.0%}   {d3:>6.0%}   {eq:>7}%  {op:>5}   {w:>5}%  {pv:>10}")
