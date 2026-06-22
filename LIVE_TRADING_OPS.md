# 实盘运维手册 — dev-os-eye-api2

## 快速连接

```bash
ssh dev-os-eye-api2
```

> SSH 配置已写入 `~/.ssh/config`，无需额外参数。  
> 服务器 IP：45.78.235.165，用户：root，端口：22

---

## 服务器布局

| 路径 | 说明 |
|------|------|
| `/opt/quantdinger/` | QuantDinger 项目根目录 |
| `/opt/quantdinger/scripts/score.py` | **实时评分 / 历史回溯**（主入口，支持 CLI 参数） |
| `/opt/quantdinger/scripts/scan.py` | **全量标的扫描**（扫 top_symbols_output.json 里所有标的） |
| `/opt/quantdinger/daily_review.py` | 每日复盘脚本（cron 自动同步到容器运行） |
| `/opt/quantdinger/indicator_code_v4.1.py` | 策略代码 v4.2（已移除 vol-pullback 信号，仅保留二买/三买，**已部署运行**） |
| `/opt/mgr/` | mgr 服务（数眼平台后端，独立） |

> 所有 Python 脚本均需在容器内运行：  
> `docker cp /opt/quantdinger/scripts/<script>.py quantdinger-backend:/app/scripts/<script>.py`  
> `docker exec quantdinger-backend python3 /app/scripts/<script>.py [参数]`  
> `score.py` 和 `scan.py` 已预先部署到容器，可直接 `docker exec` 运行。

| 容器名 | 作用 |
|--------|------|
| `quantdinger-backend` | 策略引擎主进程，every 300s 决策一次 |
| `quantdinger-frontend` | Web 界面，端口 8888 |
| `postgres` | 数据库，端口 5433（外部） |
| `redis` | 缓存 |

---

## 功能一：查看实盘策略运行状态

### 确认策略运行

```bash
ssh dev-os-eye-api2

# 容器状态
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

# 策略是否 running
docker exec postgres psql -U root -d quantdinger \
  -c "SELECT id, strategy_name, status, decide_interval FROM qd_strategies_trading;"
```

### 查看当前持仓

```bash
docker exec postgres psql -U root -d quantdinger \
  -c "SELECT symbol, side, size, entry_price, current_price, pnl_percent, stop_loss_price FROM qd_strategy_positions ORDER BY symbol;"
```

### 查看最新持仓同步日志（每 30s 刷一次）

```bash
docker logs quantdinger-backend --tail 20 2>&1 | grep PositionSync
```

### 查看最近信号触发记录

```bash
docker logs quantdinger-backend --since 24h 2>&1 \
  | grep -E '(开仓信号|平仓|止损触发|Skip signal)' | tail -30
```

### 策略逻辑说明

策略名：**缠论扫描策略**，基于缠中说禅理论，4H K 线，Binance 永续合约。

开仓三道门：

1. **趋势过滤** — 当前价 > 240 根 K 线均价（约 40 日均线），否则跳过
2. **量能过滤** — 近 14 根均量 / 近 60 根均量 ≥ 0.70，或当根量 ≥ 60 根均量的 2 倍
3. **买点评分** ≥ 60 分才触发
   - **二买**：中枢震荡后上升笔的底部高于 ZD，当前价在底部 12% 以内 → 最高 75 分，止损 = ZD × 0.98
   - **三买**：突破 ZG 后回踩仍高于 ZG，当前价在回踩底 8% 以内 → 最高 65 分，止损 = ZG × 0.98
   - RSI < 35 → 分数 × 1.25；RSI > 70 → 分数 × 0.6

> **v4.2 变更（2026-06-11）**：已移除 vol-pullback 信号。回测（2025 全年）显示 vol-pullback 胜率仅 12.5%，总亏损 -$2,635，劣于二买（22.2%），下线。

---

## 功能二：实时扫描是否有符合开仓的标的

### 扫默认四个标的（ETH/BTC/SOL/ZEC）

```bash
ssh dev-os-eye-api2
docker exec quantdinger-backend python3 /app/scripts/score.py
```

### 指定标的

```bash
docker exec quantdinger-backend python3 /app/scripts/score.py --symbols ETH/USDT,BTC/USDT,SOL/USDT
```

> `score.py` 和 `scan.py` 均使用 Binance **合约**客户端（`binanceusdm`）取 K 线，与策略一致。已于 2026-06-11 修复（原先默认走现货客户端，导致仅有合约的标的返回"数据不足"）。

### 扫全量标的（所有 top symbols，耗时约 2-5 分钟）

```bash
docker exec quantdinger-backend python3 /app/scripts/scan.py
# 同时看接近阈值的候选（评分 ≥ 40）
docker exec quantdinger-backend python3 /app/scripts/scan.py --min-score 40
```

**输出示例：**

```
标的               评分  类型               原始分  距离%   RSI        止损          当前价
BTC/USDT           67  二买(RSI×1.25)     53.6   4.2  29.3   58000.0000  62000.0000
ETH/USDT            0  均线以下(cur=1649 ma=2084)
SOL/USDT            0  量能萎缩
```

**字段说明：**

| 字段 | 含义 |
|------|------|
| 评分 | RSI 调整后分数，≥ 60 代表有信号 |
| 类型 | 二买 / 三买 / 均线以下 / 量能萎缩 / 未达阈值 |
| 原始分 | RSI 调整前评分 |
| 距离% | 当前价距离底部的百分比（越小越紧贴） |
| 止损 | 建议止损位 |

> 评分 ≥ 60 且有止损价 → 可考虑开仓

---

## 功能三：排查"达到评分但没有开仓"的情况

用 `score.py --date` 回溯历史时间点，替代旧版 `score_full.py`（不再需要手动改时间戳）。

### 回溯指定时间点

```bash
ssh dev-os-eye-api2

# 按日期时间回溯（CST）
docker exec quantdinger-backend python3 /app/scripts/score.py --date 2026-06-10T08:00

# 按 Unix 时间戳回溯
docker exec quantdinger-backend python3 /app/scripts/score.py --time 1780545141

# 回溯 + 指定标的
docker exec quantdinger-backend python3 /app/scripts/score.py \
  --date 2026-06-10T08:00 --symbols ETH/USDT,BTC/USDT,SOL/USDT

# 降低阈值看候选（调参用）
docker exec quantdinger-backend python3 /app/scripts/score.py \
  --date 2026-06-10T08:00 --min-score 40
```

```bash
ssh dev-os-eye-api2
# 查看所有被跳过的信号
docker logs quantdinger-backend --since 7d 2>&1 \
  | grep -E '(Skip signal|price unavailable|not found on binance)' | tail -20
```

---

## 功能四：每日复盘

### 自动触发（crontab）

每天 **14:00 UTC（北京时间 22:00）** 自动运行，结果写入 `/tmp/review_YYYY-MM-DD.txt`。

```bash
# crontab 配置（主机）
0 14 * * * docker cp /opt/quantdinger/daily_review.py quantdinger-backend:/app/scripts/daily_review.py && docker exec quantdinger-backend python3 /app/scripts/daily_review.py > /tmp/review_$(date +\%Y-\%m-\%d).txt 2>&1
```

### 手动运行（查看今日复盘）

```bash
ssh dev-os-eye-api2

# 今日复盘
docker exec quantdinger-backend python3 /app/scripts/daily_review.py

# 指定日期
docker exec quantdinger-backend python3 /app/scripts/daily_review.py --date 2026-06-10

# 显示涨幅前 15 名（默认 10）
docker exec quantdinger-backend python3 /app/scripts/daily_review.py --top 15

# 查看已保存的复盘结果
cat /tmp/review_2026-06-10.txt
```

### 输出解读

```
▲ BTC/USDT           涨幅 +8.3%  (60000.0000 → 64980.0000)
  开盘评分: 72  类型: 二买  RSI: 41.2  开仓: ✓ YES
  中枢: ZG=62000 ZD=58000  MA240=59500  笔数=8  中枢数=2

▲ XRP/USDT           涨幅 +5.1%  (0.5200 → 0.5465)
  开盘评分: 0  类型: 无信号  RSI: 55.0  开仓: ✗ NO
  ▶ 被拦截: 均线以下
```

**复盘关键字段：**

| 字段 | 含义 |
|------|------|
| 开仓: ✓ YES | 开盘时评分 ≥ 60，策略应该开仓 |
| 开仓: ✗ NO | 策略未触发，原因见下方 |
| 被拦截 | 均线以下 / 量能萎缩 |
| 结构原因 | 缠论结构不足（笔数不够、无中枢等） |

**复盘用于发现策略改进点：**
- 频繁出现"均线以下但大涨" → 趋势过滤是否太严？
- 频繁出现"评分 58~59 未达阈值但大涨" → 阈值 60 是否可降低？
- 某类型（二买/三买）胜率明显差 → 该类型是否要单独调参？

---

## 常用命令速查

```bash
# 连接服务器
ssh dev-os-eye-api2

# 查容器状态
docker ps

# 查策略日志（实时）
docker logs -f quantdinger-backend 2>&1 | grep -E '(开仓|平仓|止损|signal)'

# 查当前持仓
docker exec postgres psql -U root -d quantdinger \
  -c "SELECT symbol, side, size, entry_price, current_price, pnl_percent FROM qd_strategy_positions;"

# 实时评分
docker cp /opt/quantdinger/score_check2.py quantdinger-backend:/tmp/score_check2.py
docker exec quantdinger-backend python3 /tmp/score_check2.py

# 今日复盘
docker exec quantdinger-backend python3 /app/scripts/daily_review.py

# 重启后端
docker restart quantdinger-backend

# 查看后端日志最后 50 行
docker logs quantdinger-backend --tail 50

# 查询 Binance 合约账户真实余额（total/free/used）
docker exec quantdinger-backend python3 -c "
import sys, json; sys.path.insert(0,'/app')
from app.utils.db import get_db_connection
import ccxt
with get_db_connection() as db:
    c = db.cursor()
    c.execute(\"SELECT exchange_config FROM qd_strategies_trading WHERE id=1\")
    cfg = json.loads((c.fetchone() or {}).get('exchange_config') or '{}')
    c.close()
ex = ccxt.binanceusdm({'apiKey': cfg['api_key'], 'secret': cfg['secret_key']})
u = ex.fetch_balance()['USDT']
print(f'total={u[\"total\"]:.2f}  free={u[\"free\"]:.2f}  used={u[\"used\"]:.2f}')
"

# 手动清理 48h 前的策略日志（cron 每 6h 自动执行，此命令用于立即清理）
docker exec postgres psql -U root -d quantdinger \
  -c "DELETE FROM qd_strategy_logs WHERE timestamp < NOW() - INTERVAL '48 hours';"
```

---

## 当前实盘状态快照（2026-06-11 15:38）

| 标的 | 方向 | 开仓价 | 当前价 | 浮盈% | 浮盈USD | 止损 | 备注 |
|------|------|--------|--------|-------|---------|------|------|
| ETH/USDT | 多 | 1809.27 | 1652.05 | -8.69% | -27.36 | 未设 ⚠️ | 策略开仓，240 均线以下 |
| SOL/USDT | 多 | 71.30 | 64.80 | -9.11% | -28.71 | 未设 ⚠️ | 策略开仓，240 均线以下 |
| JCT/USDT | 多 | 0.00645 | 0.00627 | -2.78% | -5.57 | 未设 | 手动开仓 |
| HOME/USDT | 多 | 0.03338 | 0.03650 | +9.34% | +24.40 | 0.02852 | 策略开仓，vol-pullback（旧代码误开） |
| CLO/USDT | 多 | 0.13909 | 0.14647 | +5.31% | +6.91 | 0.10604 | 策略开仓，vol-pullback（旧代码误开）；Binance 合约需确认 |

> USDT 余额：total=721.65  free=328.14  used=393.08
> BTC/USDT 已于 2026-06-11 手动平仓。
> PORTAL/USDT 已于 2026-06-10 22:07 策略止损平仓（成交价 0.01473，亏损 -29.08 USD）。
> PIPPIN/USDT 已于 2026-06-11 Binance 止损触发，PositionSync 自动同步删除。
> ETH/SOL/JCT 无止损，ETH/SOL 在 240 均线以下，策略不会自动新开仓。
> HOME/CLO 为 vol-pullback 信号在数据库代码更新前误开（已盈利），DB 代码已于 2026-06-11 15:37 更新，vol-pullback 下线完成。

---

## 已知问题与踩坑记录

### 坑 1：仅上合约未上现货的标的，信号会被静默跳过（已修复）

**现象**：`scan.py` 或 `score.py` 评分 ≥ 60，策略日志无任何开仓记录，`pending_orders` 也没有新记录，Docker 日志出现 `Skip signal ... exec_price <= 0`。

**根因**：`trading_executor.py` 的全量扫描循环（约第 5015 行）调用 `_fetch_current_price` 时未传 `exchange_id`，导致默认用 Binance **现货**客户端查价。只有合约没有现货的标的（如 JCT/USDT、PIPPIN/USDT、1000PEPE/USDT）会返回 price=0，进而被 `Skip signal` 静默跳过。ETH/BTC/SOL 因为现货也存在，不受影响。

**修复**：在该调用处补上 `exchange_id=kline_exchange_id, kline_market_type=kline_market_type`，让价格查询走合约客户端（`binanceusdm`），`_symbol_for_scoped_market` 会自动把 `JCT/USDT` 转成 `JCT/USDT:USDT`。已于 2026-06-11 部署到容器。

**排查命令**：
```bash
# 确认标的是否只有合约没有现货
docker exec quantdinger-backend python3 -c "
import ccxt, asyncio
async def check(sym):
    ex = ccxt.binance()
    await ex.load_markets()
    spot = sym in ex.markets
    ex2 = ccxt.binanceusdm()
    await ex2.load_markets()
    swap = sym+':USDT' in ex2.markets
    print(f'spot={spot} swap={swap}')
    await ex.close(); await ex2.close()
asyncio.run(check('JCT/USDT'))
"

# 查看被跳过的信号
docker logs quantdinger-backend --since 24h 2>&1 | grep 'Skip signal'
```

---

### 坑 2：`timedelta` NameError 导致每轮都触发调仓（已修复）

**现象**：策略日志每 300s 就触发一次全量扫描，`_should_rebalance` 永远返回 True，CPU/API 调用偏高。日志中有 `Failed to check rebalance: name 'timedelta' is not defined`（2026-06-08 起出现）。

**根因**：`trading_executor.py` 中 `_should_rebalance` 方法内部使用了 `timedelta` 但作用域内未导入，抛出 `NameError` 被 `except Exception: return True` 吞掉，导致逻辑始终认为需要调仓。

**历史影响**：已导致 2026-06-13 FOLKS/USDT 和 GWEI/USDT 各重复开仓 1~2 笔。

**修复**：在 `_should_rebalance` 方法顶部补 `from datetime import timedelta`，已部署。

---

### 坑 3：scan.py 读取标的列表为空（已修复）

**现象**：运行 `scan.py` 时打印"标的列表为空，请检查文件格式"后退出，不扫描任何标的。

**根因**：`top_symbols_output.json` 的顶层键名是 `symbol_list`，但 `scan.py` 只尝试 `symbols` 和 `data`，匹配不到，返回空列表。

**修复**：在 `scan.py` 的 `raw.get(...)` 链中加入 `symbol_list` 作为第一候选。已于 2026-06-11 修复并部署。

---

### 坑 4：持仓深度浮亏导致保证金不足，新信号无法开仓

**现象**：策略找到信号、创建 `pending_order`，但 Binance 返回 HTTP 400 `{"code":-2019,"msg":"Margin is insufficient."}`，订单最终 `status='failed'`。

**根因**：已持有多个深度浮亏仓位（ETH -19%、SOL -19%、PORTAL -15%），占用了大量保证金，账户可用余额不足以开新仓。

**排查命令**：
```bash
# 查看失败订单及错误信息
docker exec postgres psql -U root -d quantdinger \
  -c "SELECT symbol, signal_type, status, last_error, created_at FROM pending_orders WHERE status='failed' ORDER BY created_at DESC LIMIT 10;"

# 查看策略日志中的错误
docker exec postgres psql -U root -d quantdinger \
  -c "SELECT timestamp, message FROM qd_strategy_logs WHERE strategy_id=1 AND level='error' ORDER BY timestamp DESC LIMIT 20;"
```

**处理方式**：手动减仓/平仓浮亏仓位，释放保证金，或向账户充入 USDT。

> ⚠️ 注意：调高策略 `leverage` 字段**不能**减少保证金需求。因为开仓公式是 `amount = capital × ratio × leverage / price`，Binance 收取的保证金 = notional / leverage = capital × ratio，leverage 在分子分母抵消，实际保证金不变。

---

### 坑 5：部分标的在 Binance 合约市场不存在

**现象**：`scan.py` 或策略日志出现 `CCXT fetch_ohlcv failed: binance does not have market symbol XXX/USDT`。

**已知案例**：
- `PHAROS/USDT`：在 Binance 上完全不存在合约（可能是其他交易所或已下架）
- `1000PEPE/USDT`：需要写成 `1000PEPE/USDT:USDT` 格式才能在合约市场找到

**处理方式**：从 `top_symbols_output.json` 中移除无效标的，或在扫描脚本中增加异常跳过逻辑（目前已有 `except Exception: pass`，不影响整体运行）。

---

### 坑 6：手动平仓后数据库自动同步，无需手动更新

**现象**：在 Binance 手动平仓某标的后，DB 中 `qd_strategy_positions` 仍有该记录。

**结论**：策略每 30s 运行一次 `PositionSync`，会自动将 Binance 持仓与 DB 同步，约 1 分钟内自动删除已平仓记录，**无需手动 DELETE**。

**强制触发重新调仓**：
```bash
# 将 last_rebalance_at 置空，下一轮决策会立即触发全量扫描
docker exec postgres psql -U root -d quantdinger \
  -c "UPDATE qd_strategies_trading SET last_rebalance_at = NULL WHERE id = 1;"
```

---

### 坑 7：手动开仓不会自动写入数据库，需手动 INSERT

**现象**：在 Binance 手动开了某仓位，但 DB 的 `qd_strategy_positions` 里没有这条记录，策略的止损逻辑和持仓统计也不会覆盖它。

**结论**：PositionSync 只同步**策略已知**的持仓（策略开仓时写入的），不会自动把手动开的仓位添加进来。需要手动 INSERT。

**手动写入持仓模板**：
```sql
INSERT INTO qd_strategy_positions
  (strategy_id, symbol, side, size, entry_price, current_price,
   highest_price, lowest_price, unrealized_pnl, pnl_percent, equity,
   stop_loss_price, updated_at)
VALUES
  (1, 'XXX/USDT', 'long', <数量>, <开仓价>, <当前价>,
   <开仓价>, <当前价>, (<当前价> - <开仓价>) * <数量>,
   ((<当前价> / <开仓价>) - 1) * 100, <当前价> * <数量>,
   <止损价>, NOW())
ON CONFLICT (strategy_id, symbol, side) DO NOTHING;
```

---

## 数据库参考

数据库：`quantdinger`，用户：`root`，端口：5433（宿主机）或 5432（容器内）

连接方式：
```bash
# 宿主机执行（最常用）
docker exec postgres psql -U root -d quantdinger -c "SQL语句"

# 进入交互式 psql
docker exec -it postgres psql -U root -d quantdinger
```

### 核心表一览

| 表名 | 说明 |
|------|------|
| `qd_strategies_trading` | 策略配置（主策略表） |
| `qd_strategy_positions` | 当前持仓 |
| `qd_strategy_logs` | 策略运行日志 |
| `qd_strategy_trades` | 成交记录 |
| `pending_orders` | 待执行订单队列 |
| `qd_exchange_credentials` | 交易所 API Key |
| `qd_users` | 用户账号 |
| `qd_backtest_runs` | 回测记录 |

---

### qd_strategies_trading — 策略配置

| 列名 | 类型 | 说明 |
|------|------|------|
| id | int | 主键，当前策略 id=1 |
| strategy_name | varchar | 策略名称（如"缠论扫描策略"） |
| status | varchar | `running` / `stopped` |
| decide_interval | int | 决策间隔秒数（当前 300） |
| timeframe | varchar | K 线周期（当前 `4H`） |
| symbol | varchar | 主标的（扫描策略留空） |
| market_type | varchar | `swap`（永续合约） |
| leverage | int | 杠杆倍数 |
| initial_capital | numeric | 初始资金 |
| strategy_code | text | 策略 Python 代码 |

```sql
-- 查看策略状态
SELECT id, strategy_name, status, decide_interval, timeframe, leverage
FROM qd_strategies_trading;
```

---

### qd_strategy_positions — 当前持仓

| 列名 | 类型 | 说明 |
|------|------|------|
| id | int | 主键 |
| strategy_id | int | 关联策略 id |
| symbol | varchar | 标的，如 `ETH/USDT` |
| side | varchar | `long` / `short` |
| size | numeric | 持仓数量 |
| entry_price | numeric | 开仓均价 |
| current_price | numeric | 当前价 |
| highest_price | numeric | 持仓期间最高价 |
| lowest_price | numeric | 持仓期间最低价 |
| unrealized_pnl | numeric | 未实现盈亏（USD） |
| pnl_percent | numeric | 浮盈% |
| equity | numeric | 持仓价值 |
| stop_loss_price | numeric | 止损价（0 表示未设置） |
| trail_level | int | 追踪止损级别 |
| breakeven_activated | bool | 是否已激活保本止损 |
| updated_at | timestamp | 最后更新时间 |

```sql
-- 查看当前持仓
SELECT symbol, side, size, entry_price, current_price,
       pnl_percent, stop_loss_price, updated_at
FROM qd_strategy_positions
ORDER BY symbol;

-- 查看有浮亏的持仓
SELECT symbol, entry_price, current_price, pnl_percent, stop_loss_price
FROM qd_strategy_positions
WHERE pnl_percent < 0
ORDER BY pnl_percent;

-- 查看无止损的持仓（⚠️ 风险）
SELECT symbol, side, entry_price, current_price, pnl_percent
FROM qd_strategy_positions
WHERE stop_loss_price = 0 OR stop_loss_price IS NULL;
```

---

### qd_strategy_logs — 策略日志

| 列名 | 类型 | 说明 |
|------|------|------|
| id | int | 主键 |
| strategy_id | int | 关联策略 id |
| level | varchar | `info` / `warn` / `error` / `trade` |
| message | text | 日志内容 |
| timestamp | timestamp | 记录时间 |

```sql
-- 查看最近 50 条日志
SELECT timestamp, level, message
FROM qd_strategy_logs
WHERE strategy_id = 1
ORDER BY timestamp DESC
LIMIT 50;

-- 查看错误日志
SELECT timestamp, message
FROM qd_strategy_logs
WHERE strategy_id = 1 AND level = 'error'
ORDER BY timestamp DESC
LIMIT 20;

-- 查看交易信号日志（开平仓记录）
SELECT timestamp, message
FROM qd_strategy_logs
WHERE strategy_id = 1 AND level = 'trade'
ORDER BY timestamp DESC
LIMIT 30;

-- 查看今日日志
SELECT timestamp, level, message
FROM qd_strategy_logs
WHERE strategy_id = 1
  AND timestamp >= CURRENT_DATE
ORDER BY timestamp DESC;
```

---

### qd_strategy_trades — 成交记录

| 列名 | 类型 | 说明 |
|------|------|------|
| id | int | 主键 |
| strategy_id | int | 关联策略 id |
| symbol | varchar | 标的 |
| type | varchar | `open_long` / `close_long` / `open_short` / `close_short` |
| price | numeric | 成交价 |
| amount | numeric | 成交数量 |
| value | numeric | 成交金额（USD） |
| commission | numeric | 手续费 |
| profit | numeric | 本次平仓盈亏（开仓为 0） |
| created_at | timestamp | 成交时间 |

```sql
-- 查看最近 20 笔成交
SELECT created_at, symbol, type, price, amount, value, profit
FROM qd_strategy_trades
WHERE strategy_id = 1
ORDER BY created_at DESC
LIMIT 20;

-- 查看盈亏统计
SELECT symbol,
       COUNT(*) FILTER (WHERE type = 'open_long') AS 开仓次数,
       SUM(profit) AS 总盈亏,
       AVG(profit) FILTER (WHERE profit != 0) AS 平均盈亏
FROM qd_strategy_trades
WHERE strategy_id = 1
GROUP BY symbol
ORDER BY 总盈亏 DESC;

-- 查看某标的完整交易历史
SELECT created_at, type, price, amount, profit
FROM qd_strategy_trades
WHERE strategy_id = 1 AND symbol = 'ETH/USDT'
ORDER BY created_at DESC;
```

---

### pending_orders — 待执行订单

| 列名 | 类型 | 说明 |
|------|------|------|
| id | int | 主键 |
| strategy_id | int | 关联策略 id |
| symbol | varchar | 标的 |
| signal_type | varchar | `open_long` / `close_long` |
| status | varchar | `pending` / `sent` / `failed` |
| attempts | int | 已尝试次数 |
| max_attempts | int | 最大重试次数 |
| last_error | text | 最后一次错误信息 |
| exchange_order_id | varchar | 交易所订单 ID |
| filled | numeric | 已成交数量 |
| avg_price | numeric | 平均成交价 |
| created_at | timestamp | 创建时间 |

```sql
-- 查看待执行/失败订单
SELECT id, symbol, signal_type, status, attempts, last_error, created_at
FROM pending_orders
WHERE strategy_id = 1 AND status IN ('pending', 'failed')
ORDER BY created_at DESC;

-- 查看最近订单（含已完成）
SELECT created_at, symbol, signal_type, status, filled, avg_price
FROM pending_orders
WHERE strategy_id = 1
ORDER BY created_at DESC
LIMIT 20;
```
