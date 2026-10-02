---
layout: home

hero:
  name: SensoryPlex
  text: 端侧 AI 多模态素材预处理框架
  tagline: 用 Rust 核心、Python AI SDK 与版本化 Protobuf 契约，把 SRT 直播流与媒体文件变成可溯源、按时间轴对齐的素材库。
  actions:
    - theme: brand
      text: 快速上手
      link: /zh/guide/quickstart
    - theme: alt
      text: 当前真实验证了什么
      link: /zh/reference/status
    - theme: alt
      text: GitHub
      link: https://github.com/ZhiPenTu/SensoryPlex

features:
  - icon: 🧩
    title: 契约先行
    details: proto/ 是跨语言的唯一契约源。Rust 侧构建期生成、Python SDK 由 make proto 生成，生成代码禁止手改。
  - icon: ⏱️
    title: 时间轴是硬性契约
    details: 每个观测都落在同一 stream 的 [start_ms, end_ms) 上。PTS 未知、时长未知与丢弃点都带原因上报，绝不夹取成看起来合法的区间。
  - icon: 🔒
    title: 原始媒体只留在数据面
    details: 帧、PCM 与 tensor 不进控制消息、事件与日志。跨进程访问是有界共享内存 lease，带摘要校验与显式释放。
  - icon: 🧠
    title: 端侧插件，而不是单体
    details: OCR、ASR、VLM 与文本向量都以 gRPC 处理器插件形态跑在持有加速器的宿主上，MLX/Metal 与 CoreML 保持原生，不被硬塞进 Linux 容器。
  - icon: 🧵
    title: 有界队列，失败可观察
    details: 并发、队列深度与保留窗口都有硬上限。越过本档上限的配置以 event_inflight_exceeds_tier_cap 拒绝启动，不夹取、不降级。
  - icon: 📉
    title: 能力要如实上报
    details: 不可用的能力必须带必填原因与 retryable 标记。健康检查通过从不被用来宣称端到端链路可用。

---

## 项目现在处在什么位置

SensoryPlex **0.1.0** 是**可运行工程底座**，不是完成态产品。已经落地的是：带真实 GStreamer 解码的
Rust 运行时、Python AI SDK 与四个端侧模型插件、只追加的 PostgreSQL 素材模型（不可变 revision）、
事务性 outbox 到 NATS JetStream、常驻向量索引与 gRPC 检索面、Web 控制台，以及面向局域网子节点的
节点拓扑。

::: warning 本站的状态描述都是有范围的
默认报告里的 `ReplayReport.golden_path_verified` 当前恒为 **false**。真实媒体端到端验收、模型到素材的
来源映射、服务端 Milvus 形态均**未验收**。本站凡写“未验收”的地方就是字面意思——在依赖链路的任何
一部分之前，请先看 [能力实现状态](/zh/reference/status) 里逐项的验收边界。
:::

## 它适合谁

- **你要一个带证据的本地素材库。** 需要回答“这段视频里有什么、发生在什么时候、哪个模型说的”，
  并且原片对应区间还能回看。
- **你的算力是异构的。** 加速器可能在一台 Mac mini、一台 NVIDIA 机器或厂商 NPU 主机上，你不想为了
  适配一种容器运行时把每个模型重写一遍。
- **你更在意可审计性而不是功能数量。** 你宁可看到带原因的 `unavailable`，也不要一个“零结果的成功”。

如果你要的是开箱即用的托管式媒体 AI 产品，那这一层不是你要的东西——它正是那个产品下面的一层。

## 从这里开始

| 你的目标 | 建议阅读 |
| --- | --- |
| 理解问题边界与设计取舍 | [项目简介](/zh/guide/introduction) |
| 把容器跑起来并登录控制台 | [快速上手](/zh/guide/quickstart) |
| 走一遍上传 → 任务 → 素材 → 回看 | [控制台全流程](/zh/guide/console-walkthrough) |
| 写一个新的 OCR/ASR/VLM 插件 | [插件体系](/zh/plugins/overview) |
| 确认到底真实验证了什么 | [能力实现状态](/zh/reference/status) |
| 判断能不能部署 | [部署](/zh/operations/deployment) |
| 参与社区共建与贡献维护 | [贡献指南与社区治理](/zh/project/contributing) |
