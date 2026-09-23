# ADR-016：OCR 插件、组合模型身份与 ONNX 执行后端（CoreML）

**状态：** Accepted（2026-09-23）
**上游决策：** ADR-008（Apple Silicon 一等目标）、ADR-009（媒体格式矩阵与显式拒绝）、ADR-010（跨进程数据面安全边界）、ADR-012（模型插件与端侧推理边界）、ADR-014（ASR 插件与音频样本布局契约）、ADR-015（macOS 常驻形态与统一内存分级）
**相关文档：** [实现状态](../implementation-status.md)、[验证记录](../verification.md)、[契约](../contracts/README.md)、[待办](../TODO.md)

---

## 1. 背景与问题

ADR-012 用 VLM 回答"插件怎么拿到帧、怎么被信任"，ADR-014 用 ASR 回答"字节怎么被解释"。OCR（画面文字 → 带坐标的文本块）**不能照抄这两条**，因为它在四处不同：

1. **输出不是一个值，而是一组带几何的结构。** "第 3 个块在第 87 行"是结果的一部分。坐标属于哪个
   坐标系（帧像素？归一化？显示坐标系？源编码坐标系？）如果不说，读者只能猜，而猜错会让
   **下游裁图/对齐全部偏掉**，且不会有任何断言报错。
2. **模型不是一个文件，是三个模型的组合。** PP-OCR 由检测（det）、方向分类（cls）、识别（rec）
   三份权重组成。"模型版本号"这种单一标识符覆盖不了三份字节，换掉其中之一必须让身份变化。
3. **分数容易被误读成置信度。** 检测框分数与识别分数是模型自己的输出，**没有做过校准**，
   把它们填进 `confidence` 就是把"看起来像概率的浮点数"当成"校准后的概率"用。
4. **OCR 的 ONNX 图是动态 shape**（可变的图像尺寸 + NMS 子图）。这条直接决定 Apple Silicon 的
   加速后端**能不能真的用上**——不是"有 CoreML EP 就能加速"。

目标平台是 ADR-008/015 定下的一等目标：**Apple Silicon macOS**（本轮验收机为 M2 Max / 32 GiB；
另一个目标形态是拿 Mac mini 当家庭工作站的端侧部署），本机推理、媒体不出网。

本决策把上述四条落成规则，落点是 `plugins/python/processors/ocr-rapidocr`。
**这一条只承诺"接上真实 OCR 模型且链路语义可用"，不承诺"识别质量达标"，更不承诺 CoreML 加速**
——见 §5 与 §9。

## 2. 决策一：模型身份 = 三份 ONNX 权重的**组合摘要**，且必须可被第三方复算

- `plugins/.../ocr-rapidocr/src/.../models.py` 定义三个角色与**实际加载的文件**：
  `det → PP-OCRv6_det_small.onnx`、`cls → ch_ppocr_mobile_v2.0_cls_mobile.onnx`、
  `rec → PP-OCRv6_rec_small.onnx`。这三份权重**随 `rapidocr` 轮子携带**
  （`site-packages/rapidocr/models/`），正常路径**不需要联网下权重**。
- 身份不是配置里写的版本号，而是**从会话读出来的真实文件路径**：插件逐角色取
  `engine.<role>.session.session.get_providers()` 与 ORT 实际加载的 `_model_path`，
  对这个文件算 SHA-256。读不到真实路径就没有真实身份：`model_artifact_digest_unavailable`。
- `combined_digest` 把 `{角色: 单文件摘要}` 按**角色名排序**折叠成一个规范摘要（顺序无关），
  换掉任意一份权重都会得到新的 `model_artifact_digest`；`release_id` 里带它的前 12 位十六进制，
  所以"同一次配置、换了一份 rec 权重"不会看起来像同一个 release。
- 容器在 Start 阶段**真实读一次**：ONNX 是 protobuf，首字节必须是 `ir_version`（field 1, varint）
  的 tag `0x08`。空文件（`model_container_empty`）、非 protobuf（`model_container_not_onnx`）、
  读不出来（`model_container_unreadable`）都在 Start 被拒，而不是等到第一次识别。
- `--model-dir` 指向本机目录时走同一条路：`Global.model_root_dir` 换成该目录，身份仍按**实际加载的
  文件**实测；目录不存在是 `model_dir_not_found`（原因串里不带路径——路径是运营配置，
  不该出现在控制面字符串里）。

本机实测（2026-09-23，`rapidocr 3.9.2` + `onnxruntime 1.30.0`，任何第三方可用同一批文件复算）：

| 角色 | 文件 | 字节 | SHA-256 |
| --- | --- | --- | --- |
| det | `PP-OCRv6_det_small.onnx` | 9 929 594 | `090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f` |
| cls | `ch_ppocr_mobile_v2.0_cls_mobile.onnx` | 585 532 | `e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c` |
| rec | `PP-OCRv6_rec_small.onnx` | 21 234 383 | `6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884` |
| **组合** | — | — | `31df9f5afcc7dacbf15acc380117833d2fece9f894f8e7f533f8b89cd3dc2cf6` |

`docs/verification.md` 的 M8 OCR 一节用**独立实现**（`tools/verify_ocr.py` 自己算，不调用插件代码）
复算这四行，并要求 observation 的 `provenance.model_artifact_digest` 逐字相等。

## 3. 决策二：坐标空间写进结果，像素布局显式拒绝

- payload 必须带 `coordinate_space = "frame_pixels_top_left"`：像素是**这一帧解码后**的坐标系，
  左上原点，不是显示坐标系，也不是源编码坐标系。
- 每个块带 `box`（4 点四边形 `[[x,y]×4]`，不是轴对齐矩形——OCR 的框是斜的）与
  `box_normalized`；归一化用**同一个坐标系**的 `image.width/height` 换算，读者不需要自己猜分母。
- 输入只认 `RGBA`（`media.video_frame` descriptor 的 `BufferFormat.pixel_format` 实测值）：
  其他格式是 `unsupported_pixel_format:<值>`。尺寸不可用（`frame_dimensions_unusable`）、
  stride 小于一行（`frame_stride_smaller_than_row`）、载荷不足（`frame_payload_too_small`）
  都是**显式失败**——不猜 stride、不按行宽补零、不"读到多少算多少"。
- RGBA → BGR 是**唯一入口**（`_to_bgr`）。通道顺序写错不会被任何断言发现，只会让识别结果悄悄变差，
  所以这一条被契约测试用已知字节钉住。

## 4. 决策三：检测/识别分数不是校准置信度

- `Observation.confidence` **必须缺省**，并写
  `confidence_unavailable_reason = "detector_and_recognizer_scores_are_not_calibrated_confidence"`。
- 每个块的 `score` 原样带出（模型自己的输出），`engine.text_score` 也带出——它只是引擎的**过滤阈值
  参数**，不是"这个结果的概率"。
- 这条与 ADR-012/014 同源：**缺失置信度必须显式表示未知**，不许用"看起来像概率的浮点数"顶替。

## 5. 决策四：请求了加速后端就必须真的选中，且不把它说成"加速可用"

**机制。** `provider=coreml` 时插件向引擎传 `EngineConfig.onnxruntime.use_coreml = True`
（参数键必须是**扁平点号**；嵌套 dict 会被引擎拒绝：`EngineConfig is not a valid key`）。

**断言（不静默回退）。** Start 阶段逐角色核对会话的 provider 列表：**首选 provider 必须逐字等于
请求的 EP**，否则以 `execution_provider_not_selected:<role>:<actual>` 显式失败。
`provider=cpu` 时同样断言 `CPUExecutionProvider`。这不是"尽力而为"：请求了 CoreML 却拿到 CPU
就是失败，而不是"降级可用"。该断言由契约测试
`test_requested_provider_must_actually_be_selected` 钉住（含"会话缺失"这一档）。

**实测约束（本 ADR 最重要的一段，不许读成"CoreML 已加速可用"）。** 在本机
（M2 Max、macOS 26.5.2、`onnxruntime 1.30.0`、`rapidocr 3.9.2`）实测：

- 三个角色的会话 provider 列表是 `['CoreMLExecutionProvider', 'CPUExecutionProvider']`，
  首选确实是 `CoreMLExecutionProvider` → 上面的断言**通过**（本机可达；
  非 Apple 平台或未编译 CoreML EP 的构建上会走到显式失败那条路）。
- **但 ORT 在 stderr 打印大量 `E5RT ... unbounded dimension which is not supported ...` 与
  `p2o_pd_op_*`**：PP-OCR 的动态 shape 与 NMS 子图无法编译成 CoreML 网络，ORT 把这些子图
  **分区回退到 CPU**。因此"会话首选是 CoreML"**不等于**"全部算子跑在 ANE/GPU"。
  Python 侧也没有稳定的分区查询 API，这个事实只能从 ORT 的 stderr 观察到。
- 同一帧（854×480）的推理耗时实测：**CPU 216–228 ms  vs  CoreML 1163 ms**（约 5 倍慢）。
  两者的文字块结果一致（都是 6 块，首块 `Jak nahrát video do Commons`）。
- 常驻内存（单引擎 + 一帧）：**CPU ≈ 610 MiB，CoreML ≈ 2.4 GiB**（CoreML EP 的编译缓存）。
  manifest 的 `resources.memory` 因此取两者上界再加余量：`3Gi`。

**结论：** 本版本把 `coreml` 交付为**可选择的执行后端**——请求是显式的、实际 provider 可观测、
不一致会显式失败——**不交付"加速"**。`provider` 默认 `cpu`。要真正拿到 CoreML 的收益，
需要先解决动态 shape 与分区回退（例如换成静态 shape 的检测/识别模型），那是后续切片。

**Metal：** ONNX Runtime 在 macOS 上**没有独立的 Metal EP**（Apple 侧的执行后端就是 CoreML EP，
它自己决定算子落在 CPU/GPU/ANE 哪一块）。所以本 ADR **不引入 `metal` 后端**；Apple GPU 的使用
在本项目里是间接的：ASR 走 MLX（原生 Metal），VLM 走 ollama（自带 Metal），ONNX 路径走 CoreML EP。
`crates/runtime` 的能力表会为 `metal` 这类后端**给出可用性与原因**，但不会声称"已实现"
（见 §9 与运行时能力上报）。

## 6. 决策五："模型没找到文字"与"处理失败"必须能分开

- 空结果的形态是 `blocks=[]`、`block_count=0`、`char_count=0`，并带
  `empty_reason = "model_found_no_text"`——这一帧确实没有文字是**模型的真实结果**，不是错误。
- 处理失败是错误码（`ocr_inference_failed`、`data_plane_rpc_failed:<CODE>`、布局拒绝码……），
  绝不退化成"空结果"。把失败写成空结果等于把"我不知道"说成"没有"。
- 两条路都有**真实样本**证据：`video/1.mp4`（风电塔风景，540×960）实测 0 块 + `empty_reason`；
  `screencast-video2commons.480p.vp9.webm`（854×480）实测 6 块。

## 7. 决策六：输出有界，越界计数而不是静默截断

- `MAX_BLOCKS = 512`：一帧的文字块数超过上限时，列出的截到上限、超出部分写进 `blocks_omitted`
  并与 `block_count` 对账（`block_count = len(blocks) + blocks_omitted`）。
- `MAX_BLOCK_CHARS = 512`：单个块超长**直接失败**（`ocr_block_exceeds_bound`）——单块长到这种程度
  不是"识别得多"，是模型侧异常；静默截断会把异常伪装成正常输出。
- 所有队列与并发都有上限：manifest 声明 `maxConcurrency: 1`、`maxBatchSize: 4`，
  与 `ProcessorPlugin(max_concurrency=1, max_batch_size=4)` 一致。

## 8. 与数据面的关系（沿用 ADR-010/011，不改）

插件不认识文件名、不解析媒体、不接受"绕过 lease 的字节"：每帧通过 `LeaseBufferReader.read()`
按 lease 读窗口、按 lease 摘要校验后才送进模型；读完即归还 lease。因此"这一条 observation 来自
哪些字节"是可对账的（`content_hash` = 该帧 lease 窗口摘要），且帧字节不出现在 stdout/stderr、
控制消息或 payload 里。`acceptsMemoryKinds` 只声明 `cpu_shared_memory`（ADR-012 §6）。

## 9. 后果与仍未验证范围

- 插件包 `plugins/python/processors/ocr-rapidocr` 已加进工作区（`pyproject.toml` 的
  `members` / `dependencies` / `[tool.uv.sources]`），`uv.lock` 相应新增 11 个包
  （`rapidocr 3.9.2`、`onnxruntime 1.30.0`、`opencv-python`、`shapely`、`pyclipper`、
  `pillow`、`omegaconf`、`antlr4-python3-runtime`、`colorlog`、`flatbuffers`、`six`）。
- 验收：`make ocr-check MEDIA=...`（`tools/verify_ocr.py`，四进程）与
  `tests/contracts/test_ocr_plugin_contract.py`（23 项）。
- **仍未验证（不得当作完成）：**
  - 识别**质量**：只证明了链路与几何语义，没有准确率/召回基准，没有按语言或字号分层评测；
  - CoreML 的实际收益：见 §5，实测更慢；没有解决动态 shape/分区回退；
  - `metal`（ONNX 路径）：不存在独立 EP，本版本不提供；
  - `linux-x86_64`、Mac mini、跨机未验证；插件仍未签名（只写明白原因），SBOM 只有结构预检；
  - 权重缓存目录与"绝不联网"的强执行：本版本只在 manifest 里把网络限定到一个主机
    （`www.modelscope.cn`），并声明**不写盘**（`writablePaths: []`）；正常路径（随包权重或
    显式 `model_dir`）不联网，但没有 DNS/egress 层的强制执行。
