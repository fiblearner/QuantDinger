# 通达信本地日线数据说明

## 数据位置

通达信客户端安装目录：`D:\tongdaxin\vipdoc\`

| 交易所 | 子路径 | 示例文件 | 文件数量 |
|--------|--------|----------|----------|
| 上交所（沪） | `sh/lday/sh{code}.day` | `sh000001.day` | ~4768 |
| 深交所（深） | `sz/lday/sz{code}.day` | `sz000001.day` | ~4269 |
| 北交所（京） | `bj/lday/bj{code}.day` | `bj810011.day` | 少量 |

> 文件名中的 `{code}` 为 6 位数字代码，前缀与交易所一致。

---

## 文件格式

每个 `.day` 文件为**通达信标准二进制格式**，无文件头，由连续的定长记录组成。

### 单条记录（32 字节，小端序）

| 字节偏移 | 字节数 | 类型 | 说明 |
|----------|--------|------|------|
| 0 | 4 | `uint32` | 日期（`YYYYMMDD`，如 `20260622`） |
| 4 | 4 | `uint32` | 开盘价 × 100（整数，÷100 得实际价格） |
| 8 | 4 | `uint32` | 最高价 × 100 |
| 12 | 4 | `uint32` | 最低价 × 100 |
| 16 | 4 | `uint32` | 收盘价 × 100 |
| 20 | 4 | `float32` | 成交额（元，浮点） |
| 24 | 4 | `uint32` | 成交量（手） |
| 28 | 4 | `uint32` | 保留字段 |

### Python 解析示例

```python
import struct

def load_day_file(filepath: str) -> list[dict]:
    records = []
    with open(filepath, 'rb') as f:
        data = f.read()
    record_size = 32
    for i in range(0, len(data) - record_size + 1, record_size):
        d, o, h, l, c, amt, vol, _ = struct.unpack('<IIIIIfII', data[i:i+record_size])
        if d == 0:
            continue
        records.append({
            'date':   d,
            'open':   o / 100,
            'high':   h / 100,
            'low':    l / 100,
            'close':  c / 100,
            'amount': amt,      # 元
            'volume': vol,      # 手
        })
    return records
```

---

## 数据覆盖范围

- **sh000001**（上证指数）：20210802 ~ 20260622，共 1182 条
- 个股起始日期各异，部分历史数据更早
- 通达信客户端每日收盘后自动更新

---

## 衍生分析

基于本数据集，项目已实现：

- **缠论日线中枢分析** → 见 [`scripts/analyze_zhongshu.py`](../scripts/analyze_zhongshu.py)
  - 输出目录：`data/zhongshu/{code}.json`
  - 涵盖：包含关系处理 → 分型识别 → 笔识别 → 震荡中枢识别
