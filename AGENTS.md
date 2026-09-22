# 务必按照高性能框架底层级项目规范进行开发

# SensoryPlex 工程约定

- 开发前阅读根目录两份需求文档，以及 docs/implementation-status.md。
- proto/ 是跨进程、跨语言唯一契约源；修改后运行 make proto，禁止手改生成代码。
- plugins 不得依赖 services 内部模块。Rust 管运行时，Python 管模型适配。
- 时间轴固定为同一 stream 的 [start_ms, end_ms) 毫秒偏移。
- 缺失模型置信度必须显式表示未知；禁止合成业务成功数据或静默 fallback。
- 数据库迁移仅追加，通过 tools/migrate.py 显式执行；历史 revision 不可覆盖。
- 不把原始帧、音频、tensor 或密钥放入控制消息/日志；只传受控引用。
- 所有队列与并发必须有上限，timeout、取消、重试与失败需要可观察语义。
- 修改后运行适用的 make check / make integration；媒体 E2E 必须用真实授权样本。
- 不用健康检查成功、未执行或跳过的测试宣称完整 Golden Path 已完成。
- 手写代码（Rust 的 `///`、`//!`、`//` 与 Python 的 docstring、`#`）的注释默认中文；
  proto 与生成代码保持英文，避免破坏跨语言契约与下游插件读取。
- 注释中英文边界：API、协议、容器、库与 ADR 编号（GStreamer、ffprobe、Matroska、Opus、
  segment event、unified_memory、gRPC、protobuf、ADR-003 等）保留英文原名；只翻译叙述性中文。

