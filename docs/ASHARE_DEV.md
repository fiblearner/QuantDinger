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

## 脚本一览

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

## 典型工作流

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

| 指标 | 数值 |
|------|------|
| 分析股票总数 | 9,215 只 |
| 识别中枢总数 | 63,514 个 |
| 平均每股中枢数 | 6.9 个 |
| 单股数据大小（平均） | 1.2 KB |
| 归档大小 | 1.2 MB |

---

## 开发计划

> 待开发的 A 股相关功能（按优先级排序）

- [ ] **当前中枢扫描器** — 筛选 `end_date` 为近期、价格仍在 ZD~ZG 内的活跃中枢
- [ ] **中枢突破信号** — 检测价格从中枢方向突破，输出信号列表
- [ ] **线段级别分析** — 在日线笔的基础上识别线段，构建更高级别中枢
- [ ] **增量更新脚本** — 仅重新分析近 N 日有新数据的股票，提升效率
- [ ] **批量查询工具** — 按交易所 / 板块 / 中枢笔数过滤和导出

---
