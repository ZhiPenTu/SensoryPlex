---
name: sensoryplex
description: Search, retrieve, inspect, and analyze multimodal video materials (OCR visual text, ASR audio speech, VLM scene descriptions, and streaming video playback) via SensoryPlex MCP tools or REST API.
metadata:
  short-description: Multimodal video search and analysis with SensoryPlex
---

# SensoryPlex Multimodal Video Intelligence Skill

Use this skill when answering questions about video contents, locating specific moments or dialogue in video files, verifying visual/textual observations, or triggering video processing workflows with SensoryPlex.

## Core Mental Model

SensoryPlex treats video analysis as an immutable multimodal timeline:
1. **Continuous 1-Second Grid**: All observations are anchored to millisecond offsets `[start_ms, end_ms)` along the media stream timeline.
2. **Synchronous Fast-Path (L1 Facts)**:
   - **OCR (`ocr`)**: On-screen text with bounding box coordinates, useful for slides, banners, screen recordings, subtitles, and document text.
   - **ASR (`asr`)**: Audio speech recognition transcripts with timestamp alignment.
3. **Asynchronous Slow-Path (L2 Enrichment)**:
   - **VLM (`vlm`)**: Deep visual-language scene captions, object relationships, and descriptive summaries.
4. **Search Modes**:
   - `keyword`: Literal substring matching across OCR and ASR facts. Fast and exact for names, terms, or known phrases.
   - `semantic`: Vector embedding retrieval (BGE + Milvus Lite). Best for thematic questions, fuzzy descriptions, or conceptual topics.

---

## Operating Workflows

### 1. Video Question Answering & Fact Retrieval

When the user asks about something inside a video (e.g. "What did the speaker say about architecture?", "Find the slide showing the benchmark chart"):

Reuse authenticated material evidence already available for the same source, execution, and question scope; query only facts or references that are missing.

1. **Check Capabilities When Needed**:
   - Call `get_system_status` when capability information is needed to choose a search mode or diagnose a failure. `semantic_search: true` reports configuration; the search response establishes whether the requested search works.
2. **Search Materials**:
   - Call `search_materials`:
     - If the user asks for exact terms, names, or code snippets, use `mode='keyword'`.
     - If the user asks conceptual or descriptive questions, use `mode='semantic'`. Report failures with their reason and retryability; change to keyword only when the user's intent or existing authorization permits it, and explicitly label the changed mode.
     - Keyword mode supports `start_ms` / `end_ms`; semantic mode currently accepts only `query` and `limit`. Do not silently drop requested filters or change an explicitly requested mode. If filtering retrieved semantic candidates satisfies the request, disclose that filtering happens after retrieval and does not establish exhaustive coverage of the requested window; otherwise report the limitation.
3. **Inspect Detailed Observations**:
   - If bounding boxes or high-confidence verification is needed, call `get_material_detail` using the returned `material_unit_id`.
4. **Present Findings with Timestamp Grounding**:
   - Always quote the exact time range (e.g., `00:03:14 - 00:03:22`).
   - Mention the modality source (e.g., `[ASR 语音转写]` or `[OCR 屏幕文字]`).
   - Follow the playback-link guidance below when clip review helps verify the answer.

### 2. Video Playback & Clip Verification

When the user wants to watch a video clip or confirm a search result:

1. Call `get_playback_info(asset_id=..., start_ms=..., end_ms=...)`.
2. Return a direct Markdown link with readable timestamp formatting:
   `[点击播放视频片段 (00:01:30 - 00:01:45)](http://127.0.0.1:8091/v1/uploads/upload_.../stream)`

### 3. Video Processing & Job Ingestion

For a new video processing request:

1. **Resolve Assets and Pipelines**: Reuse known `asset_id` and `pipeline_id`; call `list_media_assets` or `list_pipelines` only for missing identifiers or a needed pipeline choice.
2. **Submit Job Run**: Call `submit_job_run(asset_id=..., pipeline_id=...)` within the user's requested processing scope.

Retain the returned identifiers and inspect execution status after submission. An accepted submission is not completed processing; continue to the requested review or execution outcome under the monitoring rule below, and label pending work explicitly if that outcome has not been reached.

For an existing job progress request, call `get_job_run_status` with its known identifier; do not submit a new job merely to inspect progress.

- Prefer `execution_id` to include fast-path and delayed-enrichment status. `run_id` alone reports the orchestration Run and its tasks; its success does not establish delayed-enrichment completion.
- `ready_for_review`: Fast-path facts are available for review; delayed enrichment may remain pending. This state does not establish semantic index readiness, which requires separate index or search evidence.
- `succeeded` / `succeeded_with_partial_enrichment`: Execution reached a terminal outcome; report partial-enrichment failures explicitly. Report `failed` or `cancelled` as those outcomes, without treating them as success.
- For a progress request, return the current snapshot. For fast-path review, use available facts without waiting for VLM. Track full completion only when the requested outcome requires it, using a bounded monitoring period; report unresolved work when that period ends and stop on failure or cancellation.
- Call `get_timeline_coverage(execution_id=...)` when coverage is requested or needed to verify a completeness claim.

### 4. Cluster Health & Diagnostic Inspection

When troubleshooting performance or checking worker nodes:
1. Call `list_nodes` to inspect active compute nodes, platform architectures, and hardware accelerators (e.g. Apple Silicon Metal, NVIDIA CUDA, CoreML).
2. Report pending candidate nodes during inspection. Call `approve_candidate_node(node_id=...)` only when the user's request or existing session authorization includes accepting that node, subject to the API's `plugins:manage` permission requirement. Existing authorization does not require another confirmation.

---

## Output Best Practices

- **Strict Temporal Integrity**: Never guess timestamps or synthesize observations. If an event or word is not found, report that clearly.
- **Bilingual Support**: Present summaries and explanations in the user's preferred language (Chinese or English), while preserving technical IDs, field names, and time formats.
- **Verifiable Links**: Reuse an existing playback reference or call `get_playback_info` when playback is requested or helps verify the answer. Keep source IDs, modality, and exact time ranges for cited facts. A generated URL alone does not prove playback succeeded; identify local-only links as such.
