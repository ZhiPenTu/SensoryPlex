# 独立 Python 插件（v2）

**已验证范围：** Python 3.12、macOS Apple Silicon、`local_native`、同机文件媒体、视频帧与上游
Observation、同步插件图与异步补全。音频适配器已读取真实原片音轨；第三方音频算法的完整业务链路和
Linux 执行尚未验收。共享解码、跨机原始媒体、容器插件、Source/Sink 不在首期范围。

底座从已验证的注册信息发现外部处理器。新增插件无需修改内置名单、模型分支、结果解析或控制台页面。
既有 v1 制品和已发布方案继续保留原始身份。

## 在独立仓库创建与发布

把交付的 SDK wheel 安装到 Python 3.12 虚拟环境。本次交付 wheel，不宣称已发布到公开包仓库。
每个插件维护自己的 `pyproject.toml` 和 `uv.lock`；插件开发环境需要 `uv` 来锁定依赖和构建制品。

```sh
python3.12 -m venv .venv
.venv/bin/pip install edge_material_sdk-0.1.4-py3-none-any.whl
.venv/bin/sensoryplex-plugin create my-plugin --id com.example.my-plugin
cd my-plugin
uv add ../edge_material_sdk-0.1.4-py3-none-any.whl
uv lock
cd ..
.venv/bin/sensoryplex-plugin validate my-plugin
.venv/bin/sensoryplex-plugin compat my-plugin
.venv/bin/sensoryplex-plugin build my-plugin --out releases/my-plugin --sdk-wheel edge_material_sdk-0.1.4-py3-none-any.whl
.venv/bin/sensoryplex-plugin keygen --private-key publisher.private.pem --public-key publisher.public.pem
.venv/bin/sensoryplex-plugin sign releases/my-plugin --private-key publisher.private.pem
.venv/bin/sensoryplex-plugin verify releases/my-plugin --public-key publisher.public.pem
```

生成的实现明确返回无检测，仅是起步模板；需要实现读取真实输入的算法并验收，才能声明业务完成。
构建器不导入业务代码，核对 SDK wheel 与插件锁文件后生成平台定向 bundle，包含源码、schema、SBOM、
锁文件、SDK 和离线 wheelhouse。签名后不可原地替换字节；变更必须发布新版本。私钥不得进源码库和交付包。

## Manifest、类型与结果

使用 `edge.material.plugin/v2`、`ProcessorPlugin` 和反向域名插件 ID。入口声明模块与工厂；工厂收到配置、
注册与制品身份并返回 `ProcessorPlugin`。插件不导入底座 services；交付的独立示例包含完整实现。

```yaml
spec:
  sdk: { runtime: '>=0.1.1,<0.2', protocol: v1 }
  entrypoint: { transport: grpc, module: custom_plugin.plugin, factory: create_plugin }
  artifacts: { form: local_native }
  config: { schema: config.schema.json }
  modelApplicability: not_applicable
  executionModes: [sync, async_enrichment]
  ordering: ordered
  resources:
    maxConcurrency: 1
    maxBatchSize: 1
    defaultDeadlineMs: 30000
    memoryBytes: 268435456
    cpuMillicores: 1000
  inputs: [{ modality: media.video_frame }]
  outputs:
    - modality: com.example.brightness.measurement
      schema: { id: com.example.brightness.measurement, version: 1.0.0, path: measurement.schema.json }
      textFields: []
```

首期每个节点接收一种输入：`media.video_frame`、`media.audio_segment` 或声明的上游 Observation 类型。
Observation 输入也声明 schema。自定义输出声明命名空间、schema 的 ID/版本/包内路径，以及可选的
JSON Pointer `textFields`。schema 随包发布并锁定摘要，拒绝远程引用；SDK 和底座均检查输入输出及上限。
配置对象、数组、枚举、基础值、默认值、整数规范化和 null 统一遵循锁定的 JSON Schema。

输出沿用公共 Observation 包，附真实来源、`[start_ms, end_ms)` 时间区间、内容摘要与 schema 身份。
确定性处理器声明 `model_applicability=NOT_APPLICABLE` 和 `processor_release_id`，模型字段留空；
模型处理器继续保留完整模型血缘。处理完成而没有命中时返回 `PROCESS_OUTCOME_NO_OBSERVATIONS` 和
`brightness_below_threshold` 等机器原因码；该调用计入回执和覆盖，但不创建空 Observation。
空响应且没有明确 outcome 属于错误。

## 信任、安装与发布方案

管理员在【插件中心 → 发布者信任】登记 Ed25519 公钥，或调用 `POST /admin/v1/plugin-signers`。
导入表单接收 `bundle.tar.gz`、`release.json` 和 `release.sig`。API 为
`POST /admin/v1/plugin-releases:import`：body 传原始 bundle，`X-Plugin-Descriptor` 传 base64 JSON，
`X-Plugin-Signature` 传签名，`X-Plugin-Signer` 传已批准的签名者 ID，并带认证会话和 CSRF token。
服务器重新核对签名、身份、schema、摘要和平台条件，不相信客户端给出的 trust 标记。

为指定 release 保存配置，再通过原有蓝绿流程安装到同机已接纳节点。在【处理方案 → 插件图】逐节点
选择 release、配置、输入和执行模式，校验连接、查看只读图并发布。API 使用
`POST /admin/v1/plugin-graphs` 的 `nodes`/`edges`，再复用原方案发布和任务提交入口。
`input_selector` 使用 `media` 或 `node:<上游 ID>`。首期支持同步上游后的异步叶节点；不支持的异步依赖链
会明确拒绝。

发布锁定 release、制品、schema、配置摘要和策略。升级逻辑槽位不会改变旧方案的版本；被发布方案固定的
实例继续保留，直到显式停用 revision 且没有执行引用后才能排空。停用不删除事实，驻留版本数量仍有上限。
候选失败保留旧 active；回滚创建反向操作并恢复目标版本自己的配置。撤销阻止新的导入与部署，保留已运行
实例供管理员处置。发布者信任是原生代码安装授权，不代表强进程沙箱。

## 执行、查询与证据

通过部署脚本启动容器底座与事件栈，再执行 `./deploy/up-enrichments.sh`。在媒体节点运行宿主 Node Agent
及 `tools/enrichment_worker.py --state-file <Agent 状态> --nats-url <宿主 NATS 地址>`。
Consumer 使用带认证和租约隔离的输入接口及标准 `Process`，不导入特定模型，不持数据库凭据。
PostgreSQL 仍是唯一任务权威；有界 outbox/JetStream 只传不可变身份与受控引用，不携带原始媒体或宿主路径。
取消优先于迟到结果，重复融合不重复追加事实。补全失败记录原因和窗口影响，快路径仍可查看。

声明的文本字段进入现有向量链路；无文本字段的结果仍可查询与回看，索引状态明确为 `not_declared`。
素材详情展示字段表、受限 JSON 和血缘。验收归属时按 `execution_id` 查询，再核对素材 revision 与
Observation ID。当前语义搜索明确拒绝执行过滤；验收将语义命中与该执行的具体身份对账，不能把任意命中
算作本次成功。

仓库 `docs/verification-plugin-platform-v2.md` 记录真实制品、执行、故障与剩余边界。
签名制品和宿主 Worker 就绪后，`make plugin-platform-check MEDIA=... RELEASE_VERSION=...` 驱动
隔离栈的真实媒体控制端验收。`make plugin-platform-fault-check` 验证签名拒绝矩阵；崩溃、超时、取消、
重复、停用和回滚的命令见 `docs/runbooks/plugin-platform-v2.md`。

保留的旧 API 见 [v1 打包](/zh/plugins/package-and-manifest) 与 [SDK 生命周期](/zh/plugins/python-sdk)。
