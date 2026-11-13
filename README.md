# 知衡 · A 股量化交易平台

当前已开始第一阶段开发，完成可运行的 A 股多因子选股与历史回测核心。

- [完整技术方案](./TECHNICAL_DESIGN.md)
- [数据库选型记录](./DATABASE_DECISION.md)
- [行情数据格式与运行方法](./data/README.md)
- [A 股行情接入说明](./docs/DATA_SOURCE.md)
- [最新选股研究报告](./docs/SELECTION_REPORT.md)
- [2026年10万元资金模拟报告](./docs/CAPITAL_REPORT_2026.md)
- [策略参数优化报告](./docs/OPTIMIZATION_REPORT.md)

当前选股模块采用动量、趋势、低波动、低回撤、流动性和可选财务质量因子，并通过配置检查年化收益、最大回撤和调仓周期胜率。历史门槛仅用于研发验收，不构成未来收益保证。

