# 打包与 Manifest

本页描述保留的 v1 格式。签名外部制品见 [独立插件（v2）](/zh/plugins/independent-v2)。

每个插件都在包根目录提供 `plugin.yaml`。运行时**只**依据 manifest 做发现与预检——它绝不 import 插件代码来读元数据。

## 推荐目录

```text
my-ocr-plugin/
├── plugin.yaml          # 必需：唯一的发现输入
├── config.schema.json   # 普通配置的 JSON Schema
├── README.md
├── pyproject.toml
├── sbom.cdx.json        # manifest 引用的 SBOM
├── src/my_ocr/
│   ├── plugin.py        # ProcessorPlugin 实现
│   ├── config.py
│   ├── adapter.py
│   └── health.py
└── tests/
    ├── contract/
    ├── integration/
    └── fixtures/
```

## 带注释的 manifest

下面是一份真实随包处理器（OCR）的形状，裁到值得解释的部分：

```yaml
apiVersion: edge.material.plugin/v1   # 本协议 major 的常量
kind: ProcessorPlugin                 # Source | Processor | Sink | ExecutionBackend | Enricher
metadata:
  name: org.sensoryplex.ocr-rapidocr  # 反向域名，至少 3 段，发布后不可改名
  version: 0.1.0                      # 语义化版本
  displayName: 画面文字识别（随包 PP-OCR / ONNX Runtime）
  vendor: SensoryPlex
  license: Apache-2.0                 # 必填
  description: 读取数据面里的真实视频帧，用随包 PP-OCR ONNX 权重产出带帧像素坐标与来源的文字块。
spec:
  sdk:
    runtime: ">=0.1.0 <0.2.0"         # 本插件构建时对应的 SDK 范围
    protocol: v1
  entrypoint:
    transport: grpc                   # v1 唯一的 transport
    command:                          # 运行时会原样启动的命令
      - uv
      - run
      - --frozen
      - python
      - -m
      - edge_material_plugin_ocr_rapidocr
    port: 50073
  capabilities:
    consumes: [media.video_frame]     # 作为图边被校验，不是调用顺序
    produces: [observation.ocr_blocks]
    acceptsMemoryKinds: [cpu_shared_memory]   # 不要声明自己映射不了的种类
    ordering: per_stream
    supports:
      batch: false
      cancellation: true              # v1 必须为 true
      retry: idempotent               # v1 必须为 "idempotent"
  resources:                          # 准入与隔离输入，不是建议
    cpu: "2"
    memory: 3Gi                       # 实测常驻内存加上余量
    maxConcurrency: 1
    maxBatchSize: 4
    defaultDeadlineMs: 120000
    # gpu: { required: false, count: 0 }
  config:
    schema: config.schema.json
    secretRefs: []                    # 只写 secret 名称，绝不写值
  security:
    network: allowlist                # none | allowlist
    allowedHosts: [www.modelscope.cn] # network: allowlist 时必填
    dataEgress: local_only            # v1 唯一允许值
    filesystem:
      readOnly: true                  # v1 必须为 true
      writablePaths: []
  artifacts:
    form: local_native                # container | local_native
    image: local:plugins/python/processors/ocr-rapidocr
    digest: sha256:008baa66a2e8baa9f8b7d4a410c94f2a17f1195680600a70546dcaf73b9736fc
    sbom: sbom.cdx.json
    signatureUnavailableReason: local_native_plugin_is_not_signed_in_v1
```

## 会被强制执行的字段规则

| 位置 | 规则 |
| --- | --- |
| `apiVersion` | 常量 `edge.material.plugin/v1` |
| `metadata.name` | 反向域名、至少三段；发布后不可改 |
| `metadata.version` | 语义化版本：兼容修复升 patch、兼容新增能力升 minor、破坏 SDK 或结果语义升 major |
| `spec.entrypoint.transport` | v1 只允许 `grpc` |
| `spec.capabilities.supports` | `cancellation: true` 与 `retry: idempotent` 是常量 |
| `spec.capabilities.acceptsMemoryKinds` | 取值来自固定集合 `cpu_shared_memory`、`file_object_ref`、`cuda_ipc`、`dma_buf` |
| `spec.resources` | `cpu`、`memory`、`maxConcurrency`、`maxBatchSize`、`defaultDeadlineMs` 全部必填；`gpu` 可选 |
| `spec.resources.cpu` | 数字字符串，且不能为 0 |
| `spec.resources.memory` | `Mi` 或 `Gi`，最小 `1Mi` |
| `spec.security.network` | `none` 或 `allowlist`；`allowlist` 时 `allowedHosts` 变为必填且不能为空 |
| `spec.security.dataEgress` | 只能是 `local_only` |
| `spec.security.filesystem.readOnly` | 恒为 `true` |
| `spec.artifacts.digest` | 必须是 `sha256:` 加 64 位十六进制 |
| `spec.artifacts`（`container`） | `image` 不得以 `:latest` 结尾；必须提供 `signature` |
| `spec.artifacts`（`local_native`） | `image` 必须以 `local:` 开头；用 `signatureUnavailableReason` 代替签名 |

## 不可变性与身份

- 所有发布产物必须锁定内容 digest。只用 `latest` 或可变 tag 发布会被拒绝。
- 插件的运行时唯一键是 `(name, version, artifact_digest)`，**不是**显示名。
- `capabilities`、资源、网络放行列表与外发声明都是策略输入。运行镜像的实际行为若与声明不符，那是安全问题，
  不是文档缺口。

## 配置与密钥

- 普通配置以 JSON 注入并按 `config.schema.json` 校验，其 hash 折进发布身份。
- 密钥**只**经 `secretRef` 注入临时文件、环境变量或宿主密钥机制。它们绝不出现于 manifest、镜像层、日志、
  观测或事件中。
- 任何会改变输出语义的东西——prompt、语言、采样阈值、预处理版本——都必须进入
  `model_release_id` / `processor_release_id` 的配置 hash。

## 校验

```sh
uv run python tools/validate_plugin.py /path/to/plugin.yaml
make plugin-artifact          # SDK 与 VLM 产物（ASR：仅结构验证）
make plugin-artifact-check
```

::: warning 校验器不做什么
`tools/validate_plugin.py` 是**结构预检**。它不做 SDK 版本协商、镜像签名或 SBOM 验证、沙箱，也不执行外发策略。
Remote 与原生 ABI 形态属于后续工作。不要把 manifest 校验通过描述成“已认证”或“已签名”的插件。
:::

## 发布检查清单

- [ ] `plugin.yaml` 通过 schema 校验；名称、版本、SDK 范围与 capabilities 完整。
- [ ] 产物已锁定 digest，并带 SBOM、许可证与变更说明。
- [ ] 代码、日志与镜像层里没有密钥、推流 URL、内网路径或真实用户样本。
- [ ] `Process` 处理了 deadline、取消、重复投递、短暂失败与资源不足。
- [ ] 输入输出带正确的 stream 时间锚点、置信度、发布身份与配置 hash。
- [ ] 不向控制面发送原始 buffer；不在 lease 之外使用数据面句柄。
- [ ] 所有异常都映射到标准错误码；不存在“捕获后返回空成功”的路径。
- [ ] 已在真实样本上回放，并跑过性能基线、安全扫描与最小权限部署。
- [ ] README 说明能力边界、已知限制、资源需求、外发行为与升级/回滚步骤。
