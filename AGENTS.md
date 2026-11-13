# 项目交付约定

- 用户明确要求：所有面向用户阅读的文档、报告和交割单都要提供 HTML 版本，不只交付 Markdown。
- 用户要求 HTML 和 Markdown 分开目录：阅读文档源文件统一放在 `docs/md/`，HTML 统一放在 `docs/html/`，两边保留对应子目录与文件名。新增或更新文档时同步生成 HTML；最终回复优先提供 HTML 链接。
- HTML 必须能通过浏览器直接打开本地文件，支持离线阅读，不依赖网络资源或本地服务；保留中文、标题、表格、列表等内容，宽表支持横向滚动。
- 当前简单 Markdown 报告可使用 `python scripts/render_strategy_summary.py <文档路径.md>` 转换。不带参数时默认转换策略汇总。该脚本仅支持已有文档使用的 Markdown 子集；遇到链接、图片、代码块或其他未支持语法时，应先完善转换能力，确保内容完整。
- 转换后核对正文和表格数据，避免 HTML 与 Markdown 源文件不同步。
- 批量转换使用 `python scripts/render_strategy_summary.py --all`，扫描 `docs/md/` 并同步生成 `docs/html/文档目录.html`。已有链接会指向相应 HTML，代码块保留原文（Mermaid 当前以代码块展示）。
- 本文件是代理操作指令，无需生成 HTML 阅读版。
