# SensoryPlex MCP Server & AI Skills 集成指南

SensoryPlex 提供了标准 **Model Context Protocol (MCP)** Server 与 **AI Skills**（支持 **Codex CLI / Desktop**、**Claude Desktop / Code**、**Cursor / Windsurf**），使大模型能够无缝调用 SensoryPlex 的多模态音视频能力：

- 🔍 **多模态搜索**：精准按关键词或语义向量检索视频中的 OCR 屏幕文字、ASR 对话转写与 VLM 场景描述；
- ⏱️ **秒级时间轴定位**：获取事实发生的精确毫秒区间 `[start_ms, end_ms)` 与 OCR 画面矩形坐标；
- 🎬 **流式切片回放**：生成原片 HTTP 206 流媒体切片回放链接，供模型和用户在浏览器中即时复核画面；
- ⚡ **作业智能调度**：通过大模型直接关联视频与 DAG 流水线并监控任务进度；
- 🖥️ **集群与算力洞察**：查看边缘计算节点、硬件加速器（Apple Silicon Metal、NVIDIA CUDA、CoreML）在线状态。

---

## 一键自动化安装 (推荐)

在仓库根目录下运行自动化配置脚本：

```bash
# 自动检测本地 Claude Desktop 配置并注入，同时自动安装 Codex Skill
make mcp-setup

# 或者直接运行 Python 脚本
python tools/setup_mcp.py --auto
```

脚本将自动执行：
1. 探测底座运行端口与 API 令牌；
2. 更新 Claude Desktop 的 `claude_desktop_config.json`，无损合并 `sensoryplex` 服务项；
3. 将 SensoryPlex 技能同步至 `~/.codex/skills/sensoryplex`；
4. 打印供 Codex CLI 与 Cursor 一键使用的命令。

---

## 手动配置指南

### 1. Claude Desktop 配置

编辑 Claude Desktop 配置文件：
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

在 `mcpServers` 下添加 `sensoryplex`：

```json
{
  "mcpServers": {
    "sensoryplex": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/SensoryPlex/services/mcp-server",
        "sensoryplex-mcp"
      ],
      "env": {
        "SENSORYPLEX_BASE_URL": "http://127.0.0.1:8091",
        "SENSORYPLEX_API_TOKEN": "YOUR_API_TOKEN"
      }
    }
  }
}
```

> **免密提示**：本地开发环境下，如果未配置 `SENSORYPLEX_API_TOKEN`，MCP Server 会自动探测底座演示账号并登录，零配置即可使用！

### 2. Codex CLI 与 Desktop 配置

#### 注册 MCP Server
在终端运行：
```bash
codex mcp add sensoryplex -- uv run --directory /absolute/path/to/SensoryPlex/services/mcp-server sensoryplex-mcp
```

#### 安装 Codex Skill
```bash
make skill-install
# 或手动复制
mkdir -p ~/.codex/skills
cp -r skills/sensoryplex ~/.codex/skills/sensoryplex
```
安装后，Codex 在遇到视频检索、多模态事实问答等需求时，会自动路由并激活 `sensoryplex` 技能与对应 MCP 工具。

### 3. Cursor / Windsurf 配置

在项目根目录 `.cursor/mcp.json` 中配置：

```json
{
  "mcpServers": {
    "sensoryplex": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/SensoryPlex/services/mcp-server",
        "sensoryplex-mcp"
      ],
      "env": {
        "SENSORYPLEX_BASE_URL": "http://127.0.0.1:8091",
        "SENSORYPLEX_API_TOKEN": "YOUR_API_TOKEN"
      }
    }
  }
}
```

---

## 提供的 MCP 工具清单

| 工具名称 (Tool) | 分类 | 说明 |
| :--- | :--- | :--- |
| `get_system_status` | 系统 | 查看 SensoryPlex 服务健康状态、数据库迁移版本、关键词及语义搜索能力 |
| `search_materials` | 检索 | 搜索视频中的多模态素材，支持 `keyword` 和 `semantic` 模式，支持时间区间与模态过滤 |
| `get_material_detail` | 检索 | 获取聚合素材单元的完整多模态观测事实（OCR 边框、ASR 文字、VLM 描述） |
| `get_timeline_coverage`| 时间轴 | 查看视频处理执行的 1 秒网格连续切片覆盖度（已出事实 vs 待补全） |
| `list_media_assets` | 视频资产 | 列出媒体库中的视频文件、时长、SHA-256 与准入状态 |
| `get_playback_info` | 视频回放 | 获取视频片段的 HTTP 206 流式切片回放 URL 与起止时间戳说明 |
| `list_pipelines` | 任务编排 | 查看系统中已发布或草稿状态的 DAG 多模态处理流水线 |
| `submit_job_run` | 任务编排 | 针对指定视频发起多模态处理作业 |
| `get_job_run_status` | 任务编排 | 查询作业处理状态（`pending`、`running`、`ready_for_review`、`succeeded`）与子任务详情 |
| `cancel_job_run` | 任务编排 | 取消正在运行的多模态流水线执行 |
| `list_nodes` | 算力拓扑 | 查看集群中各计算节点、在线状态、硬件加速器（Metal/CUDA/CoreML）与内存 |
| `approve_candidate_node`| 算力拓扑 | 审批局域网中新接入的候选子节点 |
| `list_plugin_catalog` | 插件目录 | 查询底座注册的多模态模型插件列表 |
| `get_audit_events` | 审计日志 | 查询系统安全与管理审计日志 |

---

## 交互示例 (Claude / Codex 对话演示)

**用户**：
> "帮我找一下 1.mp4 视频里讲到系统架构的那段内容，并给我播放链接。"

**大模型（通过 MCP 交互）**：
1. 自动调用 `search_materials(query="系统架构", mode="keyword")`，命中第 `00:01:24.000` 至 `00:01:38.000` 秒的素材：
   - `[OCR 屏幕文字]`: `SensoryPlex 核心底座与子节点插件架构`
   - `[ASR 语音转写]`: `接下来我们看一下核心底座的三个服务分层...`
2. 自动调用 `get_playback_info(asset_id="upload_...", start_ms=84000, end_ms=98000)` 生成回放链接。
3. **输出回答**：
   > 在视频 **00:01:24 至 00:01:38** 处，演讲者详细阐述了架构设计：
   > - **PPT 标题**：`SensoryPlex 核心底座与子节点插件架构`
   > - **讲解语音**：`接下来我们看一下核心底座的三个服务分层...`
   >
   > 🔗 **点击直达原片切片回看**：
   > [播放原片 (00:01:24 - 00:01:38)](http://127.0.0.1:8091/v1/uploads/upload_.../stream)
