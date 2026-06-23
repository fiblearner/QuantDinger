# A 股量化开发指南

> 本文档记录 QuantDinger 项目中 A 股相关的数据来源、格式、脚本及工作流。
> 每次新增数据源或脚本后请同步更新此文档。

---

## 数据源

### 通达信本地日线数据

| 属性 | 值 |
|------|-----|
| 来源 | 通达信客户端（本地自动更新） |
| 路径 | `D:\tongdaxin\vipdoc\` |
| 格式 | 二进制 `.day` 文件（32 字节/记录，小端序） |
| 覆盖 | 沪 ~4768 只 / 深 ~4269 只 / 京 少量，共 ~9362 只 |
| 更新 | 每个交易日收盘后通达信客户端自动更新 |

```
D:\tongdaxin\vipdoc\
├── sh\lday\sh000001.day   # 上证指数
├── sh\lday\sh600000.day   # 浦发银行
├── sz\lday\sz000001.day   # 深证成指
└── bj\lday\bj810011.day   # 北交所个股
```

#### .day 文件格式

无文件头，由连续的定长记录组成，每条记录 **32 字节，小端序**：

| 字节偏移 | 字节数 | 类型 | 说明 |
|----------|--------|------|------|
| 0 | 4 | `uint32` | 日期（`YYYYMMDD`） |
| 4 | 4 | `uint32` | 开盘价 × 100 |
| 8 | 4 | `uint32` | 最高价 × 100 |
| 12 | 4 | `uint32` | 最低价 × 100 |
| 16 | 4 | `uint32` | 收盘价 × 100 |
| 20 | 4 | `float32` | 成交额（元） |
| 24 | 4 | `uint32` | 成交量（手） |
| 28 | 4 | `uint32` | 保留字段 |

```python
import struct

def load_day_file(filepath: str) -> list[dict]:
    records = []
    with open(filepath, 'rb') as f:
        data = f.read()
    for i in range(0, len(data) - 31, 32):
        d, o, h, l, c, amt, vol, _ = struct.unpack('<IIIIIfII', data[i:i+32])
        if d == 0:
            continue
        records.append({'date': d, 'open': o/100, 'high': h/100,
                         'low': l/100, 'close': c/100, 'amount': amt, 'volume': vol})
    return records
```

sh000001（上证指数）数据范围：20210802 ~ 20260622，共 1182 条。

---

### 通达信本地 5 分钟数据（fzline）

| 属性 | 值 |
|------|-----|
| 来源 | 通达信客户端（本地自动更新） |
| 路径 | `D:\tongdaxin\vipdoc\{sh,sz,bj}\fzline\` |
| 格式 | 二进制 `.lc5` 文件（32 字节/记录，小端序） |
| 覆盖 | 沪 ~4769 只 / 深 ~4282 只 / 京 ~326 只，共 ~9377 只 |
| 时间范围 | 最近约 **447 个交易日（~1.8 年）** |
| 频率 | 5 分钟 K 线，每日 48 根（09:35~11:30, 13:05~15:00） |
| 更新 | 每个交易日收盘后通达信客户端自动更新 |

#### .lc5 文件格式

无文件头，由连续的定长记录组成，每条记录 **32 字节，小端序**（`struct.unpack('<HHfffffII', ...)`）：

| 字节偏移 | 字节数 | 类型 | 说明 |
|----------|--------|------|------|
| 0 | 2 | `uint16` | 日期（TDX 编码，见下） |
| 2 | 2 | `uint16` | 时间（分钟数，从 0 点算起，如 9:35 = 575） |
| 4 | 4 | `float32` | 开盘价 |
| 8 | 4 | `float32` | 最高价 |
| 12 | 4 | `float32` | 最低价 |
| 16 | 4 | `float32` | 收盘价 |
| 20 | 4 | `float32` | 成交量 |
| 24 | 4 | `uint32` | 成交额（元） |
| 28 | 4 | `uint32` | 保留字段 |

**TDX 日期编码：**  
`year = 2004 + date_raw // 2048`  
`month = (date_raw % 2048) // 100`  
`day = date_raw % 100`

```python
import struct
from pathlib import Path

def load_lc5(filepath: str) -> list[dict]:
    data = Path(filepath).read_bytes()
    bars = []
    for i in range(0, len(data) - 31, 32):
        d = struct.unpack('<HHfffffII', data[i:i+32])
        date_raw, time_min = d[0], d[1]
        year  = 2004 + date_raw // 2048
        md    = date_raw % 2048
        month = md // 100
        day   = md % 100
        h, m  = divmod(time_min, 60)
        bars.append({
            'datetime': f'{year:04d}-{month:02d}-{day:02d} {h:02d}:{m:02d}',
            'open': d[2], 'high': d[3], 'low': d[4], 'close': d[5],
            'vol': int(d[6]), 'amount': d[7],
        })
    return bars
```

---

### BaoStock 历史分钟数据

| 属性 | 值 |
|------|-----|
| 来源 | [BaoStock](http://www.baostock.com/) 免费 HTTP API |
| 覆盖 | 沪深两市个股（不含 BJ），共 ~7,300 只（ETF/指数无分钟数据） |
| 时间范围 | 2015-01-01 ~ 2024-08-12（与 fzline 衔接） |
| 频率 | 原生 30 分钟 / 1 小时 K 线 |
| 限制 | 仅支持单线程拉取，全量约需 6~8 小时 |

**时间戳格式：**  
`time` 字段为 17 位字符串，如 `"20240701100000000"`，取 `[8:10]` 为小时，`[10:12]` 为分钟。  
采用**收盘时刻**标签（与 fzline 合成的 30m/1h 一致，可无缝合并）。

```python
def parse_bao_time(date_str: str, time_str: str) -> str:
    # date_str: "2024-07-01", time_str: "20240701100000000"
    h = time_str[8:10]
    m = time_str[10:12]
    return f"{date_str} {h}:{m}"
```

---

## 分钟 K 线完整工作流

fzline 本地数据仅覆盖最近约 **447 个交易日**，BaoStock 补全更早历史，两者合并得到 2015 年至今的完整数据。

```
通达信 fzline (*.lc5)          BaoStock HTTP API
2024-08-13 ~ 至今              2015-01-01 ~ 2024-08-12
        ↓ fetch_intraday.py          ↓ fetch_baostock.py
  data/kline_30m/{code}.csv    data/kline_30m_hist/{code}.csv
  data/kline_1h/{code}.csv     data/kline_1h_hist/{code}.csv
                    ↓ fetch_baostock.py --merge
              data/kline_30m/{code}.csv  (合并后，2015~至今)
              data/kline_1h/{code}.csv   (合并后，2015~至今)
                    ↓ auto_finalize.sh
              data/kline_30m.tar.gz
              data/kline_1h.tar.gz
```

两个任务可并行运行；`auto_finalize.sh` 会自动等待两者完成后执行合并与打包。

---

## 脚本一览

### `scripts/fetch_intraday.py`

**用途：** 从通达信本地 `fzline/*.lc5` 读取 5 分钟 K 线，合成 30 分钟 / 1 小时数据，写入 `data/kline_30m/` 和 `data/kline_1h/`。

**数据来源 / 时间范围：**  
通达信本地存储约 **447 个交易日（~1.8 年）** 的 5 分钟数据（`fzline` 目录），无需联网。

**运行方式：**

```bash
# 全量生成（约 20-30 分钟，9200+ 只股票）
python scripts/fetch_intraday.py

# 断点续建（跳过已存在的文件）
python scripts/fetch_intraday.py --resume

# 测试模式（仅处理 5 只 SH 股票）
python scripts/fetch_intraday.py --test
```

**输入：** `D:\tongdaxin\vipdoc\{sh,sz,bj}\fzline\*.lc5`  
**输出：** `data/kline_30m/{code}.csv` / `data/kline_1h/{code}.csv`（均已加入 `.gitignore`）

---

### `scripts/fetch_baostock.py`

**用途：** 通过 BaoStock HTTP API 拉取 2015-01-01 ~ 2024-08-12 的历史分钟 K 线，写入独立目录，并支持与 fzline 数据合并。

**依赖：** `pip install baostock`

**运行方式：**

```bash
# 全量拉取（单线程，约 6~8 小时，9000+ 只股票）
python scripts/fetch_baostock.py

# 断点续建（跳过已拉取的文件）
python scripts/fetch_baostock.py --resume

# 测试模式（仅处理 5 只 SH 股票）
python scripts/fetch_baostock.py --test

# 合并历史 + fzline → 最终 CSV（两个任务都完成后运行）
python scripts/fetch_baostock.py --merge
```

**输入：** BaoStock HTTP API + `data/zhongshu/*.json`（股票列表来源）  
**中间输出：** `data/kline_30m_hist/{code}.csv` / `data/kline_1h_hist/{code}.csv`（不入库）  
**合并后：** `data/kline_30m/{code}.csv` / `data/kline_1h/{code}.csv`（覆盖写入）

**注意：** BJ 股（北交所）BaoStock 不支持，自动跳过；指数/ETF 无分钟数据，跳过属正常。

---

### `scripts/auto_finalize.sh`

**用途：** 自动化编排脚本，等待 fzline 和 BaoStock 两个任务都完成后，自动执行合并与打包。

**运行方式：**

```bash
# 在 fzline 和 BaoStock 并行运行时，同步启动此脚本
bash scripts/auto_finalize.sh >> scripts/auto_finalize_log.txt 2>&1 &
```

**流程：**

1. 轮询 `fetch_intraday_log.txt`，检测到 `"30min:"` 字样（fzline 完成标志）
2. 轮询 `fetch_baostock_log.txt`，检测到 `"30min_hist:"` 字样（BaoStock 完成标志）
3. 运行 `python scripts/fetch_baostock.py --merge`
4. 打包 `data/kline_30m.tar.gz` 和 `data/kline_1h.tar.gz`

---

### `scripts/analyze_zhongshu.py`

**用途：** 对全 A 股日线数据运行缠论分析，识别震荡中枢，结果写入 `data/zhongshu/`。

**算法流程：**
```
.day 文件读取
  → 包含关系处理（上升趋势取高值，下降取低值）
  → 顶底分型识别（三根 K 线中间最高/最低）
  → 笔识别（至少 5 根处理后 K 线，交替顶底分型）
  → 震荡中枢识别（连续三笔价格区间重叠，ZD < ZG）
  → 中枢延伸（后续笔未突破中枢则继续纳入）
```

**运行方式：**

```bash
# 全量分析（约 2-3 分钟，生成 ~9200 个 JSON 文件）
python scripts/analyze_zhongshu.py

# 打包结果用于入库 / 跨设备同步
python scripts/analyze_zhongshu.py --pack

# 在没有通达信的设备上解压
python scripts/analyze_zhongshu.py --unpack
```

**输入：** `D:\tongdaxin\vipdoc\{sh,sz,bj}\lday\*.day`  
**输出：** `data/zhongshu/{code}.json`（单股）/ `data/zhongshu.tar.gz`（归档）

---

## 数据输出格式

### `data/zhongshu/{code}.json`

每只股票一个 JSON 文件，结构如下：

```json
{
  "code":           "sh000001",
  "last_date":      20260622,
  "zhongshu_count": 9,
  "zhongshu": [
    {
      "start_date": 20210811,
      "end_date":   20220419,
      "zd":         3493.38,
      "zg":         3544.09,
      "direction":  "down",
      "bi_count":   4
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `code` | 股票代码（含交易所前缀） |
| `last_date` | 数据最后日期（YYYYMMDD） |
| `zhongshu_count` | 识别到的中枢总数 |
| `start_date` | 中枢起始日期 |
| `end_date` | 中枢结束日期（最后一笔端点日期） |
| `zd` | 中枢下沿（两底较大值） |
| `zg` | 中枢上沿（两顶较小值） |
| `direction` | 进入中枢的笔方向（`"down"` 或 `"up"`） |
| `bi_count` | 中枢内笔的数量（≥ 3） |

### `data/zhongshu.tar.gz`

所有单股 JSON 的 gzip 压缩归档，约 **1.2 MB**，用于 git 入库及跨设备分发。

---

### `data/kline_30m/{code}.csv` / `data/kline_1h/{code}.csv`

由 `fetch_intraday.py`（fzline 来源）生成，**不入库**（已加入 `.gitignore`）。  
运行 `fetch_baostock.py --merge` 后会合并 BaoStock 历史数据，覆盖为 2015 年至今的完整版本。

CSV 列：`datetime, open, high, low, close, vol, amount`

- `datetime`：`YYYY-MM-DD HH:MM`（收盘时刻标签，30m 每日 8 根，1h 每日 4 根）
- `vol`：成交量（原始单位，与通达信一致）
- `amount`：成交额（元）

时间范围（fzline 单独）：最近约 **447 个交易日**（2024-08-13 ~ 至今）  
时间范围（合并后）：**2015-01-01 ~ 至今**（BaoStock 历史 + fzline 近期，去重合并）



### 初次设置（有通达信的主力设备）

```bash
# 1. 全量分析
python scripts/analyze_zhongshu.py

# 2. 打包
python scripts/analyze_zhongshu.py --pack

# 3. 入库并推送
git add data/zhongshu.tar.gz
git commit -m "chore: 更新缠论中枢数据 $(date +%Y%m%d)"
git push
```

### 日常增量更新

```bash
# 通达信更新完成后重新分析 → 打包 → 推送
python scripts/analyze_zhongshu.py
python scripts/analyze_zhongshu.py --pack
git add data/zhongshu.tar.gz && git commit -m "chore: 更新中枢数据" && git push
```

### 其他设备（无通达信）

```bash
git pull
python scripts/analyze_zhongshu.py --unpack
# data/zhongshu/*.json 即可使用
```

### Python 读取示例

```python
import json
from pathlib import Path

def load_zhongshu(code: str) -> dict:
    path = Path(f"data/zhongshu/{code}.json")
    return json.loads(path.read_text(encoding="utf-8"))

# 获取上证指数所有中枢
zs = load_zhongshu("sh000001")
for z in zs["zhongshu"]:
    print(f"{z['start_date']}~{z['end_date']}  [{z['zd']}, {z['zg']}]  {z['bi_count']} 笔")
```

---

## 数据规模（2026-06-23 快照）

| 数据集 | 指标 | 数值 |
|--------|------|------|
| 缠论中枢 | 分析股票总数 | 9,215 只 |
| 缠论中枢 | 识别中枢总数 | 63,514 个 |
| 缠论中枢 | 平均每股中枢数 | 6.9 个 |
| 缠论中枢 | 归档大小 | ~1.2 MB |
| kline_30m（fzline，近1.8年） | 文件数 | 9,377 个 |
| kline_30m（fzline，近1.8年） | 原始大小 | ~1,805 MB |
| kline_1h（fzline，近1.8年） | 文件数 | 9,377 个 |
| kline_1h（fzline，近1.8年） | 原始大小 | ~1,137 MB |
| kline_30m_hist（BaoStock历史） | 文件数 | ~7,300 个（个股，不含指数ETF） |

---

## 开发计划

> 待开发的 A 股相关功能（按优先级排序）

- [ ] **当前中枢扫描器** — 筛选 `end_date` 为近期、价格仍在 ZD~ZG 内的活跃中枢
- [ ] **中枢突破信号** — 检测价格从中枢方向突破，输出信号列表
- [ ] **线段级别分析** — 在日线笔的基础上识别线段，构建更高级别中枢
- [ ] **增量更新脚本** — 仅重新分析近 N 日有新数据的股票，提升效率
- [ ] **批量查询工具** — 按交易所 / 板块 / 中枢笔数过滤和导出

---
