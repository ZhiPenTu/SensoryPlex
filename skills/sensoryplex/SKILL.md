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

1. **Check System Readiness**:
   - If not yet checked in this session, call `get_system_status` to see if vector semantic search is available (`semantic_search: true`).
2. **Search Materials**:
   - Call `search_materials`:
     - If the user asks for exact terms, names, or code snippets, use `mode='keyword'`.
     - If the user asks conceptual or descriptive questions, use `mode='semantic'` (fallback to `keyword` if semantic search reports unavailable).
     - Specify time boundaries `start_ms` / `end_ms` if the user provided an approximate window.
3. **Inspect Detailed Observations**:
   - If bounding boxes or high-confidence verification is needed, call `get_material_detail` using the returned `material_unit_id`.
4. **Present Findings with Timestamp Grounding**:
   - Always quote the exact time range (e.g., `00:03:14 - 00:03:22`).
   - Mention the modality source (e.g., `[ASR 语音转写]` or `[OCR 屏幕文字]`).
   - Provide a playable stream link via `get_playback_info` so the user can verify the moment in one click.

### 2. Video Playback & Clip Verification

When the user wants to watch a video clip or confirm a search result:

1. Call `get_playback_info(asset_id=..., start_ms=..., end_ms=...)`.
2. Return a direct Markdown link with readable timestamp formatting:
   `[点击播放视频片段 (00:01:30 - 00:01:45)](http://127.0.0.1:8091/v1/uploads/upload_.../stream)`

### 3. Video Processing & Job Ingestion

When the user wants to process a new video or check job progress:

1. **List Assets**: Call `list_media_assets` to locate the target `asset_id`.
2. **List Pipelines**: Call `list_pipelines` to choose a suitable pipeline (such as the default `OCR+Timeline` pipeline).
3. **Submit Job Run**: Call `submit_job_run(asset_id=..., pipeline_id=...)`.
4. **Track Progress**: Call `get_job_run_status(run_id=...)`.
   - `ready_for_review`: Fast-path OCR/ASR completed; materials are immediately searchable and reviewable!
   - `succeeded`: Full execution complete, including background VLM enrichments if applicable.
5. **Inspect Coverage**: Call `get_timeline_coverage(execution_id=...)` to inspect the 1-second grid completion status.

### 4. Cluster Health & Diagnostic Inspection

When troubleshooting performance or checking worker nodes:
1. Call `list_nodes` to inspect active compute nodes, platform architectures, and hardware accelerators (e.g. Apple Silicon Metal, NVIDIA CUDA, CoreML).
2. If an edge node is pending approval, call `approve_candidate_node(node_id=...)`.

---

## Output Best Practices

- **Strict Temporal Integrity**: Never guess timestamps or synthesize observations. If an event or word is not found, report that clearly.
- **Bilingual Support**: Present summaries and explanations in the user's preferred language (Chinese or English), while preserving technical IDs, field names, and time formats.
- **Verifiable Links**: Include the stream URL from `get_playback_info` whenever citing specific video intervals.
