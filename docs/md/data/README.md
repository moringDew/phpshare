# 行情数据格式

输入为 UTF-8 CSV 长表，每行表示一只证券在一个交易日的数据。

必填列：

```text
date,symbol,close,volume
```

可选列：

```text
adj_factor,is_st,suspended,listing_days,roe,revenue_growth,debt_ratio
```

- `date` 使用 `YYYY-MM-DD`。
- `symbol` 建议使用 `600000.SH`、`000001.SZ`。
- `volume` 是成交股数，流动性按 `close * volume` 估算。
- `close` 保存原始收盘价，`adj_factor` 保存复权因子；收益使用二者乘积计算。
- 布尔值支持 `1/0`、`true/false`。
- 财务因子必须是该交易日当时已经公开可用的数据，禁止回填后来发布的财报。
- 同一日期和证券不能重复。

至少提供 123 个交易日；实际评估建议包含多个完整牛熊周期，并保留独立样本外区间。

运行：

```powershell
cd quant-platform
$env:PYTHONPATH = "backend"
python -m stock_selection.cli --input data/daily.csv --config configs/stock_selection.json --output output/backtest.json
```

输出含绩效门槛是否通过、最新选股、因子值和净值曲线。通过历史门槛不代表未来收益保证。

