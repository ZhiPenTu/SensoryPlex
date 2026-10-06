"""真实媒体端到端（ADR-028 §6/§8/§9 续）：Timeline 融合出的素材走**常驻** relay/index，
再被运行中的 api `POST /v1/materials:search` 命中。

上一段验收（`tools/verify_timeline_handoff.py`）停在"事件进了 JetStream"，把
"relay → 常驻消费 → 向量 → 语义检索"整段写死成未验证。本脚本接的就是这一段：素材由
**常驻**（compose `events` profile 里的 `relay` / `index`）搬走、编码成向量，再由 api 的
语义检索按 `owner` 水合回素材事实。

角色（全部是真进程、真服务，没有替身数据）：

1. 本脚本（编排 + 对账）。固定在**主机**执行：runtime 二进制是主机 Mach-O（容器里
   `Exec format error`），真实 OCR 权重与本机模型端点也只存在于主机；
2. `sensoryplex-runtime replay --handoff-listen`（真解码 + 真描述符账本，跑**两遍**：
   一遍 OCR、一遍 VLM）；
3. `python -m edge_material_plugin_ocr_rapidocr`（真 PP-OCR，真权重，本机 ONNX Runtime）；
4. `python -m edge_material_plugin_vlm_moondream`（真 VLM 插件，本机 ollama）；
5. `tools/ai_worker.py`（真 worker，buffer 模式）；
6. `sensoryplex-runtime timeline`（真融合：按 pipeline 栅格选窗 → 逐条准入 → 写素材）；
7. `tools/timeline_handoff.py`（授权追加：事实 + 素材 + outbox 行同一个事务）；
8. **常驻** `relay`（outbox → JetStream）与**常驻** `index`（消费 → 向量 → 检索面）；
9. 运行中的 `api`（鉴权后的转发 + 事实水合）与真 PostgreSQL。

为什么两遍回放：数据面的保留表按 lease 发缓冲区，同一条 buffer 同一时刻只能被一个消费者
持有，所以多模态是"同一批描述符窗口跑多遍"，不是"多个消费者抢同一条 buffer"。两遍得到的
窗口由解码本身决定，必须逐点相同（本脚本直接比对两份报告的身份与帧窗口）。

判定标准（全部来自真实执行）：

- 常驻服务在跑（状态行新鲜）+ 分级背压准入自洽（同档、同上限、`declared <= capacity`）；
- 两遍回放拿到**同一份**源身份与同一批描述符窗口；插件调用失败的帧单独计数（不混进窗口校验）；
- 融合出的素材里**真的**带 `ocr_blocks`（块文本非空），且至少一条素材在同一窗口内同时带
  `ocr_blocks` 与 `vision.scene_description`（ADR-028 §9 里那条"同窗多模态共存"未验证项）；
- 追加是写侧干的：`appended + replayed == 素材数`，outbox 行数对得上；
- **常驻进程真的搬走了**：本次新增事件时，`published_total` / `consumed_total` /
  `embedded_total` 相对**写入前的基线**增长；没有新增事件时（素材身份固定 + JetStream
  `Nats-Msg-Id` 2h 去重窗口内不会重投）改判**逐事件的持久凭据**；
- 向量是常驻 index 算出来的：`embedding_record` 里本次素材的行 `state='ready'`、
  `indexed_at`/`vector_ref`(`milvus://`)/`model_release_id` 齐；**且只编码 `ocr_blocks`**——
  `vision.scene_description` 的观测没有向量行，这是真实边界，不是缺陷（见 §下方"已知边界"）；
- 检索真的命中：`POST /v1/materials:search`（`mode=semantic`）里**本次每一份有向量的素材
  都在命中里**，且用作查询的那条观测取得本页最好的一档相似度（查询文本就是它的整段文本，
  所以它自己的相似度必须是 `1.0`）；`unresolved_hits==0`（说明 api 按 owner 水合到了事实），
  `unindexed_hits` 不比基线更多。**不按名次断言"第一名是本次素材"**：`distance` 是 COSINE
  **相似度**（越大越近）、检索面按降序返回，而共享库里另一条素材可以持有同一段文字（相似度
  都是 `1.0`），谁在 `hits[0]` 只是并列关系；
- 不外泄：状态行与检索响应里没有 DSN、主机路径、令牌或向量库文件路径。

刻意不做的事：

- 不删自己写下的行。**素材行、`event_outbox`、`consumed_event` 与向量行就是这条链路的凭据**；
  清掉它们会让下一次运行落进"素材在、向量没算"的不可验证中间态（去重窗口内 JetStream 不会
  重投同一 `Nats-Msg-Id`），那时 grep 什么都对不上。要清场请用
  `./deploy/down-events.sh --volumes`（向量）+ 手写 SQL（事实行）；
- 不删共享 JetStream stream：常驻 index 的订阅不会自动重建，删流等于把它打瞎到重启为止；
- 不验 revision 前进、不验 SRT 实时源、不验向量 GC；
- `golden_path_verified` 保持 `false`：上面这几条还没走完。
"""

import argparse
import json
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import psycopg  # noqa: E402
from edge_material_plugin_embed_bge_onnx import text as bge_text  # noqa: E402
from sensoryplex_relay import contract  # noqa: E402

from tools.verify_event_pipeline import (  # noqa: E402
    INDEX_STATUS,
    LEAK_NEEDLES,
    RELAY_STATUS,
    admission_of,
    check,
    counters,
    fresh_status,
    read_json,
    search,
)
from tools.verify_index import describe_target  # noqa: E402
from tools.verify_timeline_handoff import (  # noqa: E402
    fuse_timeline,
    probe_identity,
    replay_pass,
    run_handoff,
    runtime_binary,
    verify_media_chain,
)

EVENTS_DIR = ROOT / ".data/events"
INDEX_READY = EVENTS_DIR / "index-ready.json"

STREAM = contract.DEFAULT_STREAM
SUBJECT_PREFIX = contract.DEFAULT_SUBJECT_PREFIX
OCR_MODULE = "edge_material_plugin_ocr_rapidocr"
VLM_MODULE = "edge_material_plugin_vlm_moondream"
# 常驻 index 的编码面只认这一种模态（`consumer.EMBEDDABLE_MODALITY`）：它决定"哪些观测
# 会变成向量"。取值直接取自**契约的持有者**（BGE 插件的 `text` 模块，ADR-017）——在这里
# 另抄一个字面量只会让"插件换了名字"表现成"这个样本一条 ocr 观测都没有"。
EMBEDDABLE_MODALITY = bge_text.INPUT_MODALITY
VLM_MODALITY = "vision.scene_description"
# 素材 readiness 由 pipeline 声明的 `fast_modalities` 决定；本验收不跑 ASR，所以
# `asr_segment` **必然**缺失，素材只能是 `partial`——这是真实取值，不许美化成 ready。
ABSENT_MODALITY = "asr_segment"
EXPECTED_BLOCKERS = {
    "metadata_append_not_exercised",
    "vector_index_not_exercised",
    "semantic_search_not_exercised",
    "golden_path_not_verified",
}
STATUS_FRESHNESS_S = 90.0
FRAMES = 6
# BGE 编码面与检索面的上限（`index-worker/src/sensoryplex_index_worker/service.py`）。
MAX_QUERY_LENGTH = 2000
MIN_QUERY_LENGTH = 4
# 检索请求的 `limit`：api 的上限就是 100（`req.limit > 100` 直接 422），取满这一档是因为
# 向量库是**共享**的——前面样本留下的向量同样在候选集里，页面窄一档就可能把本次某条挤出榜单。
# 这个值必须**同时**用在写入前的基线与写入后的判定上：检索面报的 `unindexed_hits` 是
# "这一页候选里回查不到 ready 事实的条数"，它**是 `limit` 的函数**（`worker.py` 里
# `len(hits) - len(results)`）。基线取 6 条、判定取 100 条，两者比的就不是同一个量，实测过：
# 基线 0 而判定 24，读起来像"这次写入制造了 24 条查不到的命中"，实际是两页不同宽的候选。
SEARCH_LIMIT = 100


def setting(key: str, default: str = "") -> str:
    """先读仓库 `.env`，再退回环境变量：compose 与 api 容器读的都是 `.env` 那一份。

    反过来（环境变量优先）会让"开发者终端里恰好 export 过"变成一份不同的验收输入。
    """
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                name, _, value = line.partition("=")
                if name.strip() == key:
                    return value.strip()
    return os.getenv(key, default)


def derive_shared_url(explicit: str) -> tuple[str, str]:
    """主机侧 DSN：**共享开发库**（常驻 relay/index/api 读的那个库）。

    容器主机名换成宿主回环端口，这是本机验收的固定映射；容器内运行时请显式传
    `--database-url`。返回值只用于连接与 `describe_target`，绝不打印原文。
    """
    if explicit:
        return explicit, "argument"
    base = setting("SENSORYPLEX_DATABASE_URL")
    if not base:
        raise SystemExit("SENSORYPLEX_DATABASE_URL 为空：先 make configure 或显式传 --database-url")
    parts = psycopg.conninfo.conninfo_to_dict(base)
    parts["host"] = "127.0.0.1"
    parts["port"] = setting("POSTGRES_PORT", "5432")
    return psycopg.conninfo.make_conninfo(**parts), "env-file"


def ocr_model_dir() -> pathlib.Path:
    """独立定位 OCR 权重：不调用插件代码，避免"自己验证自己"（与 `verify_ocr` 同口径）。"""
    import rapidocr

    return pathlib.Path(rapidocr.__file__).parent / "models"


def merge_worker_reports(reports: list[dict]) -> dict:
    """把多遍回放的账目合成一份 worker 报告：**帧并集 + 观测并集**。

    `Ledger::build` 只按 `frames` 派生 item，观测靠 `source_item_id` 回指；两遍的帧窗口
    完全相同，所以并集不会造出新的 item，只会让同一个 item 同时登记两遍的观测。
    """
    merged: dict = {
        "worker_host": reports[0].get("worker_host"),
        "input_mode": reports[0].get("input_mode"),
        "data_plane": reports[0].get("data_plane"),
        "plugin": reports[0].get("plugin"),
        "input_kind": reports[0].get("input_kind"),
        "observations": [],
        "frames": [],
        # 合成的报告没有自己的 worker 级失败：逐帧失败已经在 `frames[].error` 里如实记着。
        "failures": [],
    }
    for report in reports:
        merged["observations"].extend(report.get("observations", []))
        merged["frames"].extend(report.get("frames", []))
    return merged


def ledger_items(frames: list[dict]) -> set[tuple]:
    """账本条目 = `{buffer_id}@{摘要前 16 位}`（`Ledger::build` 的口径）。

    两遍回放的帧落在**同一批** buffer 上，所以帧并集与条目数不是一回事：条目是去重后的
    `(buffer_id, source_digest)`，观测才是并集。把这件事写成可对账的数字，而不是靠读者记账。
    """
    items = set()
    for frame in frames:
        buffer_id = frame.get("buffer_id")
        digest = frame.get("source_digest")
        if buffer_id and digest:
            items.add((buffer_id, digest))
    return items


def modality_index(document: dict) -> dict[str, dict]:
    """`observationId -> 观测`：判定素材里到底有什么，必须回到观测本体，不看报告里的摘要。"""
    return {entry.get("observationId"): entry for entry in document.get("observations", [])}


def block_texts(observation: dict) -> list[str]:
    """观测里的块文本；不是该形态（或没有块）就是空列表，不猜别的字段。"""
    payload = observation.get("payload") or {}
    blocks = payload.get("blocks")
    if not isinstance(blocks, list):
        return []
    texts = []
    for block in blocks:
        text = (block or {}).get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts


def encode_source_text(observation: dict) -> str:
    """按**插件自己的拼接规则**复算这条观测的待编码整段文本（契约只有一份实现）。

    拼接规则是 `bge_text` 的（`JOIN_SEPARATOR` + 逐块 `strip` + 空块跳过），所以这里不另定一套：
    自己写一遍 `"\n".join(...)` 会在插件改了分隔符或空块语义时给出一个"看起来一样"的查询，
    而那种查询恰恰证明不了向量来自这段文本。
    """
    blocks = (observation.get("payload") or {}).get("blocks")
    if not isinstance(blocks, list):
        return ""
    pieces = []
    for block in blocks:
        text = (block or {}).get("text") if isinstance(block, dict) else None
        if isinstance(text, str) and text.strip():
            pieces.append(text.strip())
    return bge_text.JOIN_SEPARATOR.join(pieces)


def derive_query(observations: dict[str, dict], ocr_ids: list[str]) -> tuple[str, str]:
    """产出 `(query, self_observation_id)`，两条都来自**真实观测文本**。

    优先取某条可编码观测的**整段文本**（`encode_source_text`）：这段文本就是常驻 index 编码
    时喂给 BGE 的那一段，因此"查询向量与库里的向量同源"是契约推论，"这条观测必须被检索到、
    且相似度 = 1.0"才是可以说出口的断言——按名次断言"第一名是本次素材"不行（共享库里
    另一条素材可以持有**同一段文字**，两条相似度都等于 1.0，谁在 `hits[0]` 由并列关系决定）。

    整段文本超过查询上限（`MAX_QUERY_LENGTH`）时退到"最长的一块文字"，此时 `self_observation_id`
    返回空串：片段与整段不是同一段文本，"必然最近"没有依据，调用方据此**如实少报**一条断言，
    而不是把片段当整段报成"必然"。
    """
    exact: list[tuple[int, str, str]] = []
    fragments: list[tuple[int, str]] = []
    for observation_id in ocr_ids:
        joined = encode_source_text(observations[observation_id])
        if joined and len(joined) <= MAX_QUERY_LENGTH:
            exact.append((len(joined), joined, observation_id))
        for text in block_texts(observations[observation_id]):
            if MIN_QUERY_LENGTH <= len(text) <= MAX_QUERY_LENGTH:
                fragments.append((len(text), text))
    if exact:
        exact.sort(key=lambda item: (-item[0], item[2]))
        return exact[0][1], exact[0][2]
    if not fragments:
        return "", ""
    fragments.sort(key=lambda item: (-item[0], item[1]))
    return fragments[0][1], ""


def replay_pair(
    media_path: pathlib.Path,
    workspace: pathlib.Path,
    failures: list,
    *,
    frames: int,
    with_vlm: bool,
) -> tuple[dict, dict]:
    """两遍真实回放（OCR 必需、VLM 可选），返回 (merged 报告, 融合报告)。

    VLM 一帧失败是按**可读事实**处理的（ollama 偶发 5xx 会真的走到）：`allow_frame_failures`
    把这些行单独计数，代价是"同窗多模态共存"的证据可能缺一条——那就如实少报，不伪造。
    """
    model_dir = ocr_model_dir()
    passes: list[dict] = []
    if with_vlm:
        passes.append(
            replay_pass(
                media_path,
                workspace,
                failures,
                plugin_module=VLM_MODULE,
                report_name="replay-vlm.pb",
                worker_report_name="ai-worker-vlm.json",
                label="vlm",
                max_frames=frames,
                allow_frame_failures=True,
            )
        )
    passes.append(
        replay_pass(
            media_path,
            workspace,
            failures,
            plugin_module=OCR_MODULE,
            report_name="replay-ocr.pb",
            worker_report_name="ai-worker-ocr.json",
            plugin_config={
                # 配置是输入的一部分（进插件身份的 `config_hash`），因此显式且稳定。
                "provider": "cpu",
                "model_dir": str(model_dir),
                "text_score": 0.5,
                "ttl_ms": 30_000,
                "timeout_s": 1800.0,
            },
            label="ocr",
            max_frames=frames,
        )
    )
    identities = [probe_identity(pathlib.Path(item["_replay_report"])) for item in passes]
    check(
        all(identity == identities[0] for identity in identities),
        f"两遍回放拿到的源身份不一致：{identities}",
        failures,
    )
    # 两遍必须落在**同一批描述符帧**上：身份是 `(buffer_id, source_digest)`，由解码与账本决定，
    # 与模型调用成败无关。窗口（`source_time_range_ms`）只有真的跑到模型才有——调用失败的帧是
    # 模型侧事实（带稳定原因码），由 `_failed_frames` 单独计数，不混进窗口校验：否则一次
    # ollama 5xx 会被读成"两遍解码结果不一致"（ADR-028 §9 的"同窗多模态共存"结论就会被误杀）。
    ledgers = [
        sorted((frame.get("buffer_id"), frame.get("source_digest")) for frame in item["frames"])
        for item in passes
    ]
    check(
        all(ledger == ledgers[0] for ledger in ledgers),
        f"两遍回放拿到的描述符帧不是同一批：帧数={[len(ledger) for ledger in ledgers]}",
        failures,
    )
    check(
        len(ledgers[0]) == len(set(ledgers[0])),
        f"描述符账本里有重复帧：{len(ledgers[0])} 条里只有 {len(set(ledgers[0]))} 个不同身份",
        failures,
    )
    for item in passes:
        windowless = [frame for frame in item["frames"] if not frame.get("source_time_range_ms")]
        label = item.get("plugin") or "pass"
        seen = [frame.get("buffer_id") for frame in windowless]
        failed_frames = len(item.get("_failed_frames", []))
        check(
            all((frame.get("error") or {}).get("reason") for frame in windowless),
            f"{label} 有帧没有描述符窗口却没有显式 error：{seen}",
            failures,
        )
        check(
            len(windowless) == failed_frames,
            f"{label} 的失败帧计数对不上：无窗口帧={len(windowless)} "
            f"_failed_frames={failed_frames}",
            failures,
        )
    windows = [
        {frame["buffer_id"]: frame.get("source_time_range_ms") for frame in item["frames"]}
        for item in passes
    ]
    mismatched = [
        buffer_id
        for buffer_id, window in windows[0].items()
        if any(item[buffer_id] != window for item in windows[1:] if buffer_id in item)
    ]
    check(
        not mismatched,
        f"两遍回放对同一帧给出了不同的描述符窗口：{mismatched}",
        failures,
    )

    merged = merge_worker_reports(passes)
    merged_path = workspace / "ai-worker-merged.json"
    merged_path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    failed = [reason for item in passes for reason in item.get("_failed_frames", [])]
    print(
        f"[replay] 观测={len(merged['observations'])} 帧={len(merged['frames'])} "
        f"未跑到模型的帧={len(failed)} {failed}"
    )
    fused = fuse_timeline(
        media_path,
        workspace,
        pathlib.Path(passes[0]["_replay_report"]),
        merged_path,
        failures,
        material_dir_name="materials",
        report_name="timeline.json",
    )
    fused["_failed_frames"] = failed
    return merged, fused


def derive_fragment_query(observations: dict[str, dict], ocr_ids: list[str]) -> str:
    """素材自己的一块文字里最长的那一块（判别力最强，且**不是**整段原文）。"""
    candidates: list[tuple[int, str]] = []
    for observation_id in ocr_ids:
        for text in block_texts(observations[observation_id]):
            if MIN_QUERY_LENGTH <= len(text) <= MAX_QUERY_LENGTH:
                candidates.append((len(text), text))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1]


def _query_kind(self_observation_id: str) -> str:
    return "，整段观测文本" if self_observation_id else "，最长的一块文字"


def verify_fused(merged: dict, fused: dict, failures: list) -> dict:
    """融合报告与观测本体的对账，返回 `{material_unit_id: 模态集合}`。"""
    if not fused:
        failures.append("timeline 没有产出报告")
        return {}
    verify_media_chain(
        fused,
        failures,
        items_expected=len(ledger_items(merged["frames"])),
        frames_without_window_expected=len(fused.get("_failed_frames", [])),
    )
    check(
        fused["counters"]["observations_rejected"] == 0,
        f"融合拒绝了 {fused['counters']['observations_rejected']} 条观测：{fused['rejected']}",
        failures,
    )
    check(
        fused["run"]["worker_observations"] == len(merged["observations"]),
        f"融合报告只看到 {fused['run']['worker_observations']} 条观测，"
        f"合成报告里有 {len(merged['observations'])} 条",
        failures,
    )
    check(
        fused["run"]["worker_frames_without_descriptor_window"] == len(fused["_failed_frames"]),
        f"没有描述符窗口的帧数对不上失败计数："
        f"{fused['run']['worker_frames_without_descriptor_window']} != "
        f"{len(fused['_failed_frames'])}",
        failures,
    )
    observations = modality_index(merged)
    modalities: dict[str, set[str]] = {}
    for entry in fused["materials"]:
        found = {observations.get(item, {}).get("modality") for item in entry["observation_ids"]}
        modalities[entry["material_unit_id"]] = {item for item in found if item}
        check(
            EMBEDDABLE_MODALITY not in entry["fusion"]["missing_required_modalities"],
            f"{entry['material_unit_id']} 会说缺 {EMBEDDABLE_MODALITY}，但它自己就挂着这个观测",
            failures,
        )
    # 一条观测只能挂一条素材：向量的 `embedding_id` 就是 `observation_id`（ADR-020 §2），
    # 用一条观测去支撑两条素材，"这份向量是谁的"就没有答案了。
    referenced = [item for entry in fused["materials"] for item in entry["observation_ids"]]
    check(
        len(referenced) == len(set(referenced)),
        f"有观测同时挂在多条素材上：{len(referenced)} 条引用里只有 "
        f"{len(set(referenced))} 个不同观测",
        failures,
    )
    return modalities


def classify_ocr(observations: dict[str, dict], ocr_ids: list[str]) -> dict[str, list[str]]:
    """把 `ocr_blocks` 观测分成三类：**能给向量**的、**这一帧没文字**的、**取值越过插件上限**的。

    后两类是**真实媒体的取值**（黑场/纯画面帧、密集屏录帧），消费侧按原因计数跳过、不写行：
    ADR-017 §4 的"越界是失败而不是截断"照旧成立，"失败"发生在**观测**这一层，插件行为一个字
    没改。验收必须能独立预测这个分类——否则分不清"常驻 index 少写了一条向量"与"这条观测本来
    就不该有向量"，而后者一旦被当成前者，就会退回"事件重投耗尽后被丢 + 常驻进程 exit 3"。

    文本抽取是本脚本自己的实现（`block_texts`，不调插件的 `collect_text`）；**分隔符与两个上限
    取自契约的持有者**——它们是 ADR-017 §4 写进契约的数字，这里不再抄一份。
    """
    embeddable: list[str] = []
    empty: list[str] = []
    over_bound: list[str] = []
    for observation_id in ocr_ids:
        texts = block_texts(observations[observation_id])
        blocks = (observations[observation_id].get("payload") or {}).get("blocks")
        # 块数按**全部块**算（空块也计入）：插件用的就是 `len(listed)`。
        block_count = len(blocks) if isinstance(blocks, list) else 0
        if not texts:
            empty.append(observation_id)
        elif (
            block_count > bge_text.MAX_TEXTS
            or len(bge_text.JOIN_SEPARATOR.join(texts)) > bge_text.MAX_TOTAL_CHARS
        ):
            over_bound.append(observation_id)
        else:
            embeddable.append(observation_id)
    return {"embeddable": embeddable, "empty": empty, "over_bound": over_bound}


def verify_observations(merged: dict, modalities: dict[str, set[str]], failures: list) -> dict:
    """`ocr_blocks` 观测必须分清"能给向量"与"取值本身就编不了向量"（ADR-028 §9）。"""
    observations = modality_index(merged)
    ocr_ids = [
        observation_id
        for observation_id, entry in observations.items()
        if entry.get("modality") == EMBEDDABLE_MODALITY
    ]
    check(bool(ocr_ids), "这一遍回放一条 ocr_blocks 观测都没有产出", failures)
    classified = classify_ocr(observations, ocr_ids)
    check(
        bool(classified["embeddable"]),
        "没有任何 ocr_blocks 观测能给向量（全部是空文本或越过插件上限）："
        "这个样本做不了语义命中验收",
        failures,
    )
    ocr_materials = [
        identifier for identifier, found in modalities.items() if EMBEDDABLE_MODALITY in found
    ]
    # 一个窗口里可以并进不止一帧（ADR-028 §9 的同窗多模态共存），所以**不能**要求
    # "素材数 == 观测数"：实测 6 条观测落在 5 条素材上，其中一条素材带两帧 ocr。
    check(
        len(ocr_ids) >= len(ocr_materials),
        f"带 ocr_blocks 的素材数（{len(ocr_materials)}）多于 ocr 观测数（{len(ocr_ids)}）："
        "每一条带 ocr 的素材都必须由至少一条观测支撑",
        failures,
    )
    coexisting = [identifier for identifier, found in modalities.items() if VLM_MODALITY in found]
    both = sorted(set(ocr_materials) & set(coexisting))
    if any(VLM_MODALITY in found for found in modalities.values()):
        check(
            bool(both),
            f"没有任何窗口同时带 ocr_blocks 与 {VLM_MODALITY}：共存的窗口={both}",
            failures,
        )
    print(
        f"[timeline] 素材={len(modalities)} 带 ocr_blocks={len(ocr_materials)} "
        f"同窗含 VLM={len(both)} 可编码观测={len(classified['embeddable'])} "
        f"空文本={len(classified['empty'])} 越过上限={len(classified['over_bound'])} "
        f"缺失模态样例={sorted({tuple(sorted(found)) for found in modalities.values()})}"
    )
    return classified


def verify_handoff(
    report_path: pathlib.Path,
    material_dir: pathlib.Path,
    database_url: str,
    owner: str,
    workspace: pathlib.Path,
    failures: list,
) -> dict:
    """授权追加：写侧是唯一判官，脚本只负责把它的账目读出来。"""
    out = workspace / "handoff.json"
    completed = run_handoff(
        report_path,
        material_dir,
        database_url,
        owner=owner,
        out=out,
        trace_id="timeline-resident-acceptance",
    )
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr).strip().splitlines()
        failures.append(
            f"授权追加失败（exit={completed.returncode}）：{detail[-1][:300] if detail else ''}"
        )
        if "media_source_conflict" in completed.stdout + completed.stderr:
            failures.append(
                "这个样本的 media_source 已经以别的 owner 写进共享库了："
                "改用 --owner 那个主体，或换一个授权样本"
            )
        return {}
    if not out.is_file():
        failures.append("授权追加没有产出报告")
        return {}
    return json.loads(out.read_text(encoding="utf-8"))


def verify_outbox(database_url: str, material_ids: list[str], failures: list) -> dict:
    """outbox 是"事件真的被落下"的证据；`published_at` 是**常驻 relay** 留下的。"""
    event_ids = [f"material:{identifier}:1" for identifier in material_ids]
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT event_id, published_at FROM event_outbox WHERE event_id = ANY(%s)",
            (event_ids,),
        ).fetchall()
        consumed = conn.execute(
            "SELECT event_id, consumer_name FROM consumed_event WHERE event_id = ANY(%s)",
            (event_ids,),
        ).fetchall()
    return {
        "event_ids": event_ids,
        "outbox": {row[0]: row[1] for row in rows},
        "consumed": {row[0]: row[1] for row in consumed},
    }


def wait_for_resident(
    material_ids: list[str],
    counts: dict,
    classified: dict,
    failures: list,
    *,
    fresh: bool,
    timeout: float,
) -> tuple[str, dict, dict]:
    """等**常驻** relay/index 真的搬走这批事件；没有新增事件时改判持久凭据。

    返回 (增长判定, index 最后一份状态行, 跳过增量)。两个常驻服务的期望计数**一次等完**：
    relay 的 `published_total`、index 的 `consumed_total` / `embedded_total` 与两个跳过计数。
    """
    if not fresh:
        # 没有新增事件：素材身份固定（`material-<摘要>-<窗口起点>`），`Nats-Msg-Id` 就是 event_id，
        # 所以去重窗口（2h）内不会重投。这时**不能**读成"常驻链路又跑了一遍"，只能改判逐事件的
        # 持久凭据：outbox 的 `published_at`（relay 写的）+ `consumed_event`（index 写的）。
        print(
            "[resident] 本次没有新增事件（素材身份固定 + JetStream 去重窗口内不会重投）；"
            "改用逐事件的持久凭据判定常驻链路曾经搬过它们"
        )
        return "already_published", counts["index_document"], {}

    _, relay_reached = wait_for_counter_growth(
        RELAY_STATUS,
        {"published_total": (counts["relay_before"], len(material_ids))},
        counts["relay_document"],
        failures,
        label="relay",
        timeout=timeout,
    )
    if not relay_reached:
        return "not_observed", counts["index_document"], {}
    index_after, index_reached = wait_for_counter_growth(
        INDEX_STATUS,
        {
            "consumed_total": (counts["index_before"], len(material_ids)),
            "embedded_total": (counts["embedded_before"], counts["ocrable"]),
            "skipped_empty_text_total": (
                counts["skipped_empty_text_total_before"],
                len(classified["empty"]),
            ),
            "skipped_over_bound_total": (
                counts["skipped_over_bound_total_before"],
                len(classified["over_bound"]),
            ),
        },
        counts["index_document"],
        failures,
        label="index",
        timeout=timeout,
    )
    if not index_reached:
        return "not_observed", index_after, {}
    skip_deltas = verify_skip_counters(index_after, counts, classified, failures)
    print(
        f"[resident] 常驻服务搬走了这批事件：published +{len(material_ids)} / "
        f"consumed +{len(material_ids)} / embedded +{counts['ocrable']} / "
        f"skipped_empty +{skip_deltas['skipped_empty_text_total']} / "
        f"skipped_over_bound +{skip_deltas['skipped_over_bound_total']}"
    )
    return "observed", index_after, skip_deltas


def wait_for_counter_growth(
    path: pathlib.Path,
    expectations: dict[str, tuple[int, int]],
    baseline_document: dict,
    failures: list,
    *,
    label: str,
    timeout: float,
) -> tuple[dict, bool]:
    """等某个常驻服务的累计计数走到期望值；**进程重启一律显式报出来**。

    `expectations` 是 `{累计计数字段: (baseline, 期望增量)}`。累计计数只活在进程内，所以一次
    fail-stop（ADR-025 §5 的 exit 3）会把它们全部归零——那时"等增长"永远等不到，超时消息会把
    真正的现象（常驻 index 重启过、事件被丢）盖掉。两个信号都判成重启：`cycle` 回退，
    以及任何累计计数**低于**基线（同一个进程里它们只增不减）。

    返回 (最后一次读到的状态行, 是否走到期望值)。
    """
    deadline = time.monotonic() + timeout
    document = baseline_document
    highest_cycle = counters(baseline_document, "cycle")
    while time.monotonic() < deadline:
        document = read_json(path)
        cycle = counters(document, "cycle")
        regressed = [
            key for key, (baseline, _) in expectations.items() if counters(document, key) < baseline
        ]
        if cycle < highest_cycle or regressed:
            failures.append(
                f"常驻 {label} 在验收窗口里重启过（cycle {highest_cycle}→{cycle}，"
                f"回退的累计计数={regressed}）：fail-stop 被触发过一次，事件已经不在队列里，"
                f"这次验收不成立（最后一份状态行={document}）"
            )
            return document, False
        highest_cycle = max(highest_cycle, cycle)
        if all(
            counters(document, key) - baseline >= delta
            for key, (baseline, delta) in expectations.items()
        ):
            return document, True
        time.sleep(0.5)
    detail = "，".join(
        f"{key} 期望 +{delta}（baseline={baseline}，现在={counters(document, key)}）"
        for key, (baseline, delta) in expectations.items()
    )
    failures.append(
        f"{label}: 等了 {timeout:.0f}s，累计计数没有走到期望值：{detail}；最后一份状态行={document}"
    )
    return document, False


def verify_skip_counters(document: dict, counts: dict, classified: dict, failures: list) -> dict:
    """跳过必须是**被计数的**跳过：空文本与越界文本的条数要与回放侧的分类逐一对上。

    "不给这条观测写向量"是允许的（ADR-017 §4 让插件在观测这一层失败），但它必须是**看得见的**
    丢失：对不上就等于丢失不可观测，那正是"用跳过掩盖没接线"的样子。
    """
    wanted = {
        "skipped_empty_text_total": len(classified["empty"]),
        "skipped_over_bound_total": len(classified["over_bound"]),
    }
    deltas: dict[str, int] = {}
    for key, expected in wanted.items():
        delta = counters(document, key) - counts[f"{key}_before"]
        deltas[key] = delta
        check(
            delta == expected,
            f"常驻 index 的 {key} 增长 {delta}，但回放侧分类出 {expected} 条："
            "“跳过按原因计数”是这部分丢失唯一的观测面，对不上就说明它没被真正计过",
            failures,
        )
    return deltas


def verify_vectors(
    material_ids: list[str],
    embeddable_ids: list[str],
    skipped_ids: list[str],
    database_url: str,
    ready: dict,
    failures: list,
) -> list[tuple]:
    """向量必须由常驻 index 算出来，并且**恰好**覆盖"能给向量"的那些观测。

    少一条（重投耗尽后被丢）或多一条（给空文本/越界文本编了向量）都要能看见：前者会退化成
    `event_retry_exhausted`，后者是"给没有内容的东西编语义"。
    """
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            "SELECT embedding_id, material_unit_id, observation_id, state, vector_ref, "
            "indexed_at, model_release_id, vector_index_key FROM embedding_record "
            "WHERE material_unit_id = ANY(%s) ORDER BY embedding_id",
            (material_ids,),
        ).fetchall()
    check(
        len(rows) == len(embeddable_ids),
        f"向量行数 {len(rows)} 与“能给向量”的观测数 {len(embeddable_ids)} 不一致："
        "常驻 index 的编码面只认 ocr_blocks，且空文本/越界文本那条本来就该没有向量",
        failures,
    )
    expected_key = (ready.get("encoder") or {}).get("vector_index_key") or ""
    for row in rows:
        embedding_id, _, observation_id, state, vector_ref, indexed_at, release, index_key = row
        check(
            state == "ready" and indexed_at is not None,
            f"{embedding_id}: 消费出来的向量不是 ready：{(state, indexed_at)}",
            failures,
        )
        check(
            str(vector_ref or "").startswith("milvus://") and len(str(vector_ref)) < 200,
            f"{embedding_id}: vector_ref 不是受控引用：{vector_ref!r}",
            failures,
        )
        check(bool(release), f"{embedding_id}: 没有登记模型身份（model_release_id 为空）", failures)
        check(
            observation_id in embeddable_ids,
            f"{embedding_id} 的向量来路不对（不是本次“能给向量”的 ocr_blocks 观测）："
            f"{observation_id}",
            failures,
        )
        check(
            index_key == expected_key,
            f"{embedding_id}: 向量键 {index_key!r} 与常驻 index 报的 {expected_key!r} 不一致",
            failures,
        )
    # 可编码模态之外**不能**有向量行：这是"常驻 index 只编码 ocr_blocks"的正面证据；
    # 被跳过的那些观测（空文本 / 越界文本）同样**不能**有向量行——不截断、也不编语义。
    encoded = {row[2] for row in rows}
    stray = sorted(set(encoded) - set(embeddable_ids))
    check(not stray, f"出现了非 ocr_blocks 观测的向量行：{stray}", failures)
    skipped_with_rows = sorted(set(encoded) & set(skipped_ids))
    check(
        not skipped_with_rows,
        f"这些观测按契约不该有向量，却落了行：{skipped_with_rows}",
        failures,
    )
    return rows


def verify_search(
    base_url: str,
    token: str,
    query: str,
    self_observation_id: str,
    fragment_query: str,
    material_ids: list[str],
    ready: dict,
    baseline: dict,
    expected_texts: dict[str, list[str]],
    search_texts: dict,
    failures: list,
) -> dict:
    """`POST /v1/materials:search` 必须命中本次**每一份有向量的**素材。

    `distance` 是 COSINE **相似度**（越大越近，检索面按**降序**返回），不是距离——
    `proto/index/v1/index.proto` 的字段注释原先写成"距离（越小越近）"，与本轮实测相反
    （拿一条观测的整段文本当查询，它自己的相似度就是 `1.0`，且排在第一位），注释已一并改正。
    因此这里断言的是"降序排列"与"本次素材取得最好的一档"，不是"某个下标恰好是谁"。
    """
    # 与基线**同一个** `limit`（见 `SEARCH_LIMIT`）：判定是"全都命中"，而检索面是共享库。
    status, body = search(base_url, token, query, limit=SEARCH_LIMIT)
    check(status == 200, f"语义检索没有成功：HTTP {status} {body}", failures)
    if status != 200:
        return {}
    entries = body.get("hits") or []
    hits = [hit.get("material_unit_id") for hit in entries]
    searchable = [identifier for identifier in material_ids if expected_texts.get(identifier)]
    unsearchable = [identifier for identifier in material_ids if not expected_texts.get(identifier)]
    check(body.get("mode") == "semantic", f"检索模式不是 semantic：{body.get('mode')!r}", failures)
    # 相似度的排列是检索面的契约：`hits` 必须按相似度**降序**（最好的在最前）。
    # 这条对"名次"的断言不依赖任何并列关系，因此共享库里出现同分素材也不会变红。
    similarities = [float(hit.get("distance") or 0.0) for hit in entries]
    check(
        similarities == sorted(similarities, reverse=True),
        f"命中没有按相似度降序排列：{[round(value, 6) for value in similarities]}"
        "（`distance` 是 COSINE 相似度，越大越近）",
        failures,
    )
    # 查询就是这条观测的整段文本 ⇒ 它在库里的向量与查询向量同源，**必然**是第一位那一档。
    # 若这里不是 1.0，说明"向量不是这段文本算出来的"或"检索面拿错了 collection"。
    if self_observation_id:
        own = [hit for hit in entries if hit.get("observation_id") == self_observation_id]
        check(
            len(own) == 1,
            f"用作查询的那条观测没有被检索到：{self_observation_id}"
            f"（hits={hits} unindexed={body.get('unindexed_hits')}）",
            failures,
        )
        if own:
            score = float(own[0].get("distance") or 0.0)
            check(
                abs(score - 1.0) < 1e-3,
                f"{self_observation_id}: 查询文本就是这条观测的整段文本，相似度却不是 1.0：{score}",
                failures,
            )
            check(
                abs(score - max(similarities)) < 1e-6,
                f"本次素材没有取得最好的一档相似度：自身={score} 本页最好={max(similarities)}",
                failures,
            )
    check(
        bool(searchable) and set(hits) >= set(searchable),
        f"这些有向量的素材没有被命中：{sorted(set(searchable) - set(hits))}"
        f"（hits={hits} 有向量的素材={searchable}）",
        failures,
    )
    # 没有向量的素材**不可能**被语义检索命中：它没进向量库，检索面只按向量回榜。
    # 这条断言把"哪个窗口没进检索面"从"悄悄少一条"变成一次明确的核对。
    check(
        not (set(hits) & set(unsearchable)),
        f"没有向量的素材出现在命中里：{sorted(set(hits) & set(unsearchable))}"
        f"（它们的 ocr 观测全部是空文本或越过插件上限，不该有向量）",
        failures,
    )
    check(
        int(body.get("unresolved_hits") or 0) == 0,
        f"检索面给出了调用方解释不了的命中：{body.get('unresolved_hits')}"
        "（api 按 owner 水合不到事实时会落在这里，检查 owner 是否等于 api 的 principal）",
        failures,
    )
    check(
        int(body.get("unindexed_hits") or 0) <= int(baseline.get("unindexed_hits") or 0),
        f"本次写入后出现了新的查不到命中：{body.get('unindexed_hits')} > "
        f"基线 {baseline.get('unindexed_hits')}",
        failures,
    )
    # 再用"素材自己的一块文字"查一遍（**不是**整段文本）：它同样必须命中本次素材。
    # 这条不是"必然"（片段与整段不是同一段文本），但它是一条真实的检索能力断言——
    # 素材自己的一块文字找不到素材本身，说明检索面里存的不是这些文字。
    if fragment_query and fragment_query != query:
        fragment_status, fragment_body = search(base_url, token, fragment_query, limit=SEARCH_LIMIT)
        check(
            fragment_status == 200,
            f"片段查询没有成功：HTTP {fragment_status} {fragment_body}",
            failures,
        )
        fragment_hits = [hit.get("material_unit_id") for hit in (fragment_body.get("hits") or [])]
        check(
            set(fragment_hits) >= set(searchable),
            f"用素材自己的一块文字查不到本次素材：{sorted(set(searchable) - set(fragment_hits))}"
            f"（fragment_hits={fragment_hits}）",
            failures,
        )
        print(
            f"[search] 片段查询（最长的一块文字，{len(fragment_query)} 字符）"
            f"命中={len(fragment_hits)} 条，包含本次全部有向量的素材"
        )
    hydrated = [item.get("material_unit_id") for item in (body.get("materials") or [])]
    check(
        hydrated == hits,
        f"api 水合出来的素材与命中不同序同长：materials={hydrated} hits={hits}",
        failures,
    )
    expected_key = (ready.get("encoder") or {}).get("vector_index_key") or ""
    check(
        body.get("vector_index_key") == expected_key and bool(body.get("index_version")),
        f"检索面没有给出 index_version / vector_index_key：{body}",
        failures,
    )
    check(
        body.get("index_version") == ready.get("index_version"),
        f"检索面报的 index_version 与常驻 index 的 ready 文档不一致："
        f"{body.get('index_version')!r} != {ready.get('index_version')!r}",
        failures,
    )
    # 检索面文本必须就是块文字拼出来的：素材侧的 `search_text` 是 payload 里所有字符串值。
    # 比对**按素材**做：拿全样本的块文字去比某一条素材的 `search_text`，会在"不同窗口的字不同"
    # 时全部报假失败（实测踩过：5 条素材全被判成"没有块文字"，而它们其实各自都有）。
    for material_id, blocks in expected_texts.items():
        surface = search_texts.get(material_id)
        check(
            bool(surface),
            f"{material_id}: 没有 search_text——检索面文本不是真实观测派生出来的",
            failures,
        )
        check(
            all(block in (surface or "") for block in blocks),
            f"{material_id}: search_text 里没有它自己的块文字（{len(blocks)} 块）——"
            "检索面文本不是真实观测派生出来的",
            failures,
        )
    print(
        f"[search] query={query!r}（{len(query)} 字符{_query_kind(self_observation_id)}）"
        f" hits={len(hits)} 最好相似度={max(similarities) if similarities else 0.0:.4f}"
        f" index_version={body.get('index_version')}"
    )
    return body


def verify_evidence(
    database_url: str, material_ids: list[str], event_ids: list[str], failures: list
) -> dict:
    """本次写下的行就是证据：逐表计数，少一行都要能看见。"""
    with psycopg.connect(database_url) as conn:
        counts = {
            "material_unit": conn.execute(
                "SELECT count(*) FROM material_unit WHERE material_unit_id = ANY(%s)",
                (material_ids,),
            ).fetchone()[0],
            "observation": conn.execute(
                # `observation` 没有 material_unit_id 列：观测挂在 timeline_item 上，
                # 素材与观测的对应关系在写侧同事务落下的 material_observation 里。
                "SELECT count(*) FROM observation o JOIN material_observation mo"
                " ON mo.observation_id = o.observation_id"
                " WHERE mo.material_unit_id = ANY(%s)",
                (material_ids,),
            ).fetchone()[0],
            "material_observation": conn.execute(
                "SELECT count(*) FROM material_observation WHERE material_unit_id = ANY(%s)",
                (material_ids,),
            ).fetchone()[0],
            "material_source_reference": conn.execute(
                "SELECT count(*) FROM material_source_reference WHERE material_unit_id = ANY(%s)",
                (material_ids,),
            ).fetchone()[0],
            "event_outbox": conn.execute(
                "SELECT count(*) FROM event_outbox WHERE event_id = ANY(%s)", (event_ids,)
            ).fetchone()[0],
            "consumed_event": conn.execute(
                "SELECT count(*) FROM consumed_event WHERE event_id = ANY(%s)", (event_ids,)
            ).fetchone()[0],
            "embedding_record": conn.execute(
                "SELECT count(*) FROM embedding_record WHERE material_unit_id = ANY(%s)",
                (material_ids,),
            ).fetchone()[0],
        }
    check(
        counts["material_unit"] == len(material_ids),
        f"素材行数与报告不一致：{counts['material_unit']} != {len(material_ids)}",
        failures,
    )
    check(
        counts["event_outbox"] == len(material_ids),
        f"outbox 行数与素材数不一致：{counts['event_outbox']} != {len(material_ids)}",
        failures,
    )
    check(
        counts["consumed_event"] == len(material_ids),
        f"consumed_event 行数与素材数不一致：{counts['consumed_event']}"
        "（常驻 index 没消费过这批事件，或消费去重表被清过）",
        failures,
    )
    check(
        counts["embedding_record"] > 0,
        "没有任何向量行：常驻 index 没有把这次事件变成向量",
        failures,
    )
    return counts


def verify_no_leak(surfaces: dict[str, str], failures: list) -> None:
    for label, payload in surfaces.items():
        for needle in LEAK_NEEDLES:
            check(needle not in payload, f"{label} 里出现了不该出现的东西：{needle!r}", failures)


def verify(
    media_path: pathlib.Path,
    database_url: str,
    base_url: str,
    token: str,
    owner: str,
    workspace: pathlib.Path,
    *,
    frames: int,
    with_vlm: bool,
    timeout: float,
) -> tuple[list[str], dict]:
    failures: list[str] = []

    # ── 1. 常驻服务在跑 + 分级背压准入 ──────────────────────────────────────
    relay_document = fresh_status(RELAY_STATUS, "relay", failures)
    index_document = fresh_status(INDEX_STATUS, "index", failures)
    relay_tier, relay_declared, relay_capacity = admission_of(relay_document, "relay", failures)
    index_tier, index_declared, index_capacity = admission_of(index_document, "index", failures)
    check(
        relay_tier == index_tier and relay_capacity == index_capacity,
        f"两个常驻服务报的不是同一档位：relay=({relay_tier},{relay_capacity})"
        f" index=({index_tier},{index_capacity})",
        failures,
    )
    check(
        relay_document.get("stream") == STREAM
        and relay_document.get("subject_prefix") == SUBJECT_PREFIX,
        f"常驻 relay 读的不是共享 stream 与前缀："
        f"{relay_document.get('stream')}/{relay_document.get('subject_prefix')}",
        failures,
    )
    check(
        index_document.get("stream") == STREAM,
        f"常驻 index 读的不是共享 stream：{index_document.get('stream')}",
        failures,
    )
    check(
        {"skipped_empty_text_total", "skipped_over_bound_total"} <= set(index_document),
        "常驻 index 状态行里没有 skipped_empty_text_total / skipped_over_bound_total："
        "它跑的还是旧镜像，"
        "先 ./deploy/up.sh api 重建，再 make events-down && make events-up",
        failures,
    )
    ready = read_json(INDEX_READY)
    durable = str((ready.get("consume") or {}).get("durable") or "")
    check(bool(durable), f"常驻 index 的 ready 文档里没有 durable：{INDEX_READY.name}", failures)
    print(
        f"[1] 常驻服务：relay 档位={relay_tier} 上限={relay_capacity} 每轮认领={relay_declared}；"
        f"index 在飞={index_declared} durable={durable}"
    )

    # ── 2. 两遍真实回放 + 真融合 ────────────────────────────────────────────
    merged, fused = replay_pair(media_path, workspace, failures, frames=frames, with_vlm=with_vlm)
    if not fused:
        return failures, {}
    modalities = verify_fused(merged, fused, failures)
    classified = verify_observations(merged, modalities, failures)
    observations = modality_index(merged)
    embeddable_ids = classified["embeddable"]
    skipped_ids = classified["empty"] + classified["over_bound"]
    # 查询只从**真的拿到了向量的**那些观测派生：拿一条越界观测的字去查，第一名凭什么一定是
    # 本次素材就没有依据了（那条字根本没进向量库）。
    query, self_observation_id = derive_query(observations, embeddable_ids)
    check(bool(query), "没有可用于检索的块文字：这个样本不适合做语义命中验收", failures)
    if not query:
        return failures, {}
    # 片段查询：这条素材里最长的一块文字。整段文本恰好也等于某一块时（单块观测）就没有第二个
    # 视角可言，这时如实说"只有一个查询"，不硬造一条同义断言。
    fragment_query = derive_fragment_query(observations, embeddable_ids)
    if not self_observation_id:
        print(
            "[search] 注意：没有一条可编码观测的整段文本能在查询上限内复算出来，"
            '查询退化为最长的一块文字——本次**不**断言"自身相似度 = 1.0"，只断言素材被命中'
        )

    material_ids = [entry["material_unit_id"] for entry in fused["materials"]]
    embeddable_set = set(embeddable_ids)
    # 素材 → 它自己那些“能给向量”的观测的块文字：检索面文本的比对必须按素材做（见 verify_search）。
    expected_texts = {
        entry["material_unit_id"]: [
            text
            for item in entry["observation_ids"]
            if item in embeddable_set
            for text in block_texts(observations.get(item, {}))
        ]
        for entry in fused["materials"]
    }
    searchable = [identifier for identifier in material_ids if expected_texts[identifier]]
    ocr_materials = [
        identifier for identifier, found in modalities.items() if EMBEDDABLE_MODALITY in found
    ]
    if len(searchable) != len(material_ids):
        print(
            f"[timeline] 注意：{len(material_ids) - len(searchable)} 条素材没有可编码观测"
            f"（{sorted(set(material_ids) - set(searchable))}），它们不会出现在向量检索里——"
            "这是 ADR-017 §4 的观测层失败被如实计数后的结果，不是静默丢弃"
        )
    material_dir = pathlib.Path(fused["_workspace"]["materials"])
    report_path = pathlib.Path(fused["_workspace"]["report"])

    # ── 3. 写入前的基线：查询、累计计数 ─────────────────────────────────────
    # 基线与判定必须同宽（见 `SEARCH_LIMIT`）：`unindexed_hits` 是 `limit` 的函数。
    baseline_status, baseline = search(base_url, token, query, limit=SEARCH_LIMIT)
    check(
        baseline_status == 200,
        f"写入之前语义检索就不可用：HTTP {baseline_status} {baseline}"
        "（api 容器的检索面配置：./deploy/up.sh api 重建后重试）",
        failures,
    )
    relay_before = counters(relay_document, "published_total")
    index_before = counters(index_document, "consumed_total")
    embedded_before = counters(index_document, "embedded_total")
    empty_before = counters(index_document, "skipped_empty_text_total")
    over_bound_before = counters(index_document, "skipped_over_bound_total")
    print(
        f"[3] 基线：published_total={relay_before} consumed_total={index_before} "
        f"embedded_total={embedded_before} skipped_empty={empty_before} "
        f"skipped_over_bound={over_bound_before} "
        f"命中={len(baseline.get('hits') or [])}（本次判定在同一 limit 下比较）"
        f" unindexed_hits={baseline.get('unindexed_hits')}"
    )

    # ── 4. 真实追加（写侧干的事） ───────────────────────────────────────────
    handoff = verify_handoff(report_path, material_dir, database_url, owner, workspace, failures)
    if not handoff:
        return failures, {}
    written = handoff["counters"]["appended"]
    replayed = handoff["counters"]["replayed"]
    check(
        written + replayed == len(material_ids),
        f"追加账目对不上：appended={written} replayed={replayed} 素材={len(material_ids)}",
        failures,
    )
    check(
        handoff["counters"]["outbox_events"] == len(material_ids),
        f"outbox 行数与素材数不一致：{handoff['counters']['outbox_events']}",
        failures,
    )
    check(handoff["owner"] == owner, f"追加被记到了别的 owner：{handoff['owner']}", failures)
    check(handoff["golden_path_verified"] is False, "handoff 报告谎称 golden path 已验", failures)
    print(f"[4] 授权追加：appended={written} replayed={replayed} outbox={len(material_ids)}")

    # ── 5. 常驻进程真的搬走了这批事件 ───────────────────────────────────────
    counts = {
        "relay_before": relay_before,
        "index_before": index_before,
        "embedded_before": embedded_before,
        "skipped_empty_text_total_before": empty_before,
        "skipped_over_bound_total_before": over_bound_before,
        "ocrable": len(embeddable_ids),
        "index_document": index_document,
        "relay_document": relay_document,
    }
    growth, _, skip_deltas = wait_for_resident(
        material_ids, counts, classified, failures, fresh=written > 0, timeout=timeout
    )
    # 逐事件凭据必须在**等完之后**读：`published_at` 由 relay 写、`consumed_event` 由 index 写，
    # 追加刚落地时它们必然还是空的（读早了会把"还没搬"记成"没搬过"）。
    outbox = verify_outbox(database_url, material_ids, failures)
    unpublished = [key for key, value in outbox["outbox"].items() if value is None]
    check(
        not unpublished,
        f"这些事件还没有被常驻 relay 发布：{unpublished}",
        failures,
    )
    unread = [key for key in outbox["event_ids"] if key not in outbox["consumed"]]
    check(
        not unread,
        f"这些事件没有被常驻 index 消费过（consumed_event 里没有行）：{unread}",
        failures,
    )
    print(
        f"[5] 事件凭据：outbox={len(outbox['outbox'])} "
        f"已发布={len(outbox['outbox']) - len(unpublished)} "
        f"已消费={len(outbox['consumed'])} 本次增长={growth}"
    )

    # ── 6. 向量由常驻 index 算出来 ──────────────────────────────────────────
    rows = verify_vectors(material_ids, embeddable_ids, skipped_ids, database_url, ready, failures)
    print(
        f"[6] 向量落库：{len(rows)} 行 ready（可编码模态只有 {EMBEDDABLE_MODALITY}；"
        f"被跳过的 {len(skipped_ids)} 条观测按契约没有向量行）"
    )

    # ── 7. HTTP 语义检索命中 ────────────────────────────────────────────────
    with psycopg.connect(database_url) as conn:
        search_texts = dict(
            conn.execute(
                "SELECT material_unit_id, search_text FROM material_unit "
                "WHERE material_unit_id = ANY(%s)",
                (material_ids,),
            ).fetchall()
        )
    body = verify_search(
        base_url,
        token,
        query,
        self_observation_id,
        fragment_query,
        material_ids,
        ready,
        baseline,
        expected_texts,
        search_texts,
        failures,
    )

    # ── 8. 证据留存 + 不外泄 ────────────────────────────────────────────────
    evidence = verify_evidence(database_url, material_ids, outbox["event_ids"], failures)
    surfaces = {
        "timeline 报告": json.dumps(
            {key: value for key, value in fused.items() if not key.startswith("_")},
            ensure_ascii=False,
        ),
        "handoff 报告": json.dumps(handoff, ensure_ascii=False),
        "检索响应": json.dumps(body, ensure_ascii=False),
        "relay 状态行": RELAY_STATUS.read_text(encoding="utf-8") if RELAY_STATUS.is_file() else "",
        "index 状态行": INDEX_STATUS.read_text(encoding="utf-8") if INDEX_STATUS.is_file() else "",
    }
    verify_no_leak(surfaces, failures)

    result = {
        "event": "timeline.resident.acceptance",
        "mode": "semantic",
        "media_sha256": fused["source"]["content_hash"],
        "materials": len(material_ids),
        "ocr_materials": len(ocr_materials),
        "embeddable_observations": len(embeddable_ids),
        "skipped_empty_observations": len(classified["empty"]),
        "skipped_over_bound_observations": len(classified["over_bound"]),
        "unsearchable_materials": sorted(set(material_ids) - set(searchable)),
        "skip_deltas": skip_deltas,
        "vectors": len(rows),
        # "有向量的素材"条数：`vectors` 是**向量行数**（一条素材可以有多条 ocr 观测 ⇒ 多行），
        # 两者不是同一个数。混用会把"命中 6 条素材、素材共 5 条"这种自相矛盾的话写进结论。
        "searchable_materials": len(searchable),
        "appended": written,
        "replayed": replayed,
        "resident_growth": growth,
        "tier": relay_tier,
        "capacity": relay_capacity,
        "relay_inflight": relay_declared,
        "index_inflight": index_declared,
        "durable": durable,
        "query": query,
        # 查询是"整段文本"还是"退化的片段"：只报计数，不把观测 id 之外的载荷写进产物。
        "query_kind": "exact_observation_text" if self_observation_id else "longest_block",
        "query_chars": len(query),
        "query_hits": len(body.get("hits") or []),
        "query_best_similarity": max(
            (float(hit.get("distance") or 0.0) for hit in (body.get("hits") or [])),
            default=0.0,
        ),
        "unindexed_hits_baseline": int(baseline.get("unindexed_hits") or 0),
        "evidence_rows": evidence,
        "failures": failures,
    }
    return failures, result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", type=pathlib.Path, required=True)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--base-url", default="http://127.0.0.1:8091", help="api 基址")
    parser.add_argument("--owner", default="", help="默认取 .env 的 SENSORYPLEX_PRINCIPAL")
    parser.add_argument("--frames", type=int, default=FRAMES)
    parser.add_argument(
        "--no-vlm",
        action="store_true",
        help="只跑 OCR 一遍（省掉本机 ollama），代价是丢掉同窗多模态共存的证据",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--keep-workspace", action="store_true")
    arguments = parser.parse_args()

    media_path = arguments.media.expanduser()
    if not media_path.is_file():
        raise SystemExit(f"authorized sample not found: {media_path}")
    token = setting("SENSORYPLEX_API_TOKEN")
    if not token:
        raise SystemExit("SENSORYPLEX_API_TOKEN 为空：先 make configure 或显式设置")
    owner = arguments.owner or setting("SENSORYPLEX_PRINCIPAL", "local-developer")
    database_url, origin = derive_shared_url(arguments.database_url)
    workspace = pathlib.Path(tempfile.mkdtemp(prefix="sensoryplex-timeline-semantic-"))
    runtime_binary()  # 缺二进制时在跑链路之前就失败，而不是在中途

    print(f"media: {media_path.name}")
    print(f"database: {describe_target(database_url)} (from {origin})")
    print(f"api: {arguments.base_url} owner/principal: {owner}")
    print(f"workspace: {workspace}{' (kept)' if arguments.keep_workspace else ''}")
    started = time.monotonic()
    failures: list[str] = []
    result: dict = {}
    try:
        failures, result = verify(
            media_path,
            database_url,
            arguments.base_url,
            token,
            owner,
            workspace,
            frames=arguments.frames,
            with_vlm=not arguments.no_vlm,
            timeout=arguments.timeout,
        )
    except Exception as error:  # noqa: BLE001 - 起不来就是验收失败，不跳过
        # 追加而不是覆盖：半途抛异常时，前面几步已经判定的失败同样要报出来——
        # 否则"证据查询写错列"这类尾部故障会把前面真实的失败盖掉。
        failures.append(f"{type(error).__name__}: {str(error)[:300]}")

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if failures:
        print("\n时间轴 → 常驻 relay/index → 语义检索验收失败：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    elapsed = round(time.monotonic() - started, 1)
    print(
        "验收通过：真实媒体 → Runtime 两遍回放 → 时间轴融合（含 ocr_blocks）→ 授权追加 → "
        f"常驻 relay/index → 向量 → api 语义检索命中"
        f"（命中有向量的全部 {result.get('searchable_materials')} 条素材，"
        f"素材共 {result.get('materials')} 条、向量 {result.get('vectors')} 行），"
        f"用时 {elapsed}s。"
    )
    if result.get("unsearchable_materials"):
        print(
            f"注意：{result['unsearchable_materials']} 没有可编码的 ocr 观测"
            "（空文本或越过 ADR-017 §4 的插件上限），它们按契约没有向量、因此不在检索面里；"
            f"被跳过的观测={result.get('skipped_empty_observations')} 条空文本 + "
            f"{result.get('skipped_over_bound_observations')} 条越界，已按原因计数核对过。"
        )
    print("golden_path_verified=false：revision 前进、SRT 实时源、查询回看与向量 GC 仍未验收。")
    if result.get("resident_growth") == "already_published":
        print(
            "注意：本次没有新增事件，常驻搬运读的是逐事件的持久凭据"
            "（其余判定都在本次运行里真实执行）。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
