# 素材查询与原片回看

本功能消费已有 MaterialUnit，不启动模型或处理任务。M8、真实媒体端到端和语义冲突识别的
未完成状态见 `docs/TODO.md`，不能用本页面的查询/播放成功代替这些验收。

## 查询与版本

- 关键词是 PostgreSQL 字面子串匹配，无向量排名。`%`、`_` 按原文查找。
- 支持来源流 ID、开始/结束秒数（最多三位小数）、标签、modality、最低模型置信度。
  标签全部匹配；模态任一匹配；时间采用同一 stream 的半开区间交集。
- 时间使用媒体偏移而非日期。不存在的时间显示未知，不补成 0。
- 最低置信度留空包含未知；设置为 0 仍是显式过滤，未知置信度不因此成为达标。
- 显示上限可选 20/50/100，达到上限明确提示缩小范围，不伪造总数或下一页。
- 查询参数保存在 URL，进入详情、返回、刷新或浏览器前进后退均保留已提交条件。
- 详情使用现有 `GET /v1/materials/{id}?revision=N`。支持上一版、下一版、指定版本和最新版本。
  旧版本标记为只读历史快照，详情读取失败时不继续展示上次版本的数据。
- 观测按时间排序；点击后选择覆盖该观测的来源并定位。显示实际 payload、质量状态、
  缺失置信度原因、时序来源、模型/插件/配置摘要，以及待补全模态。

## 可回看的来源与安全边界

`GET /v1/materials/{id}/sources/{asset_id}?revision=N` 返回既有 `console.v1.Upload` Proto JSON。
接口同时要求 `materials:read` 与 `assets:read`，并按 owner 校验素材、媒体目录与上传记录。
没有新增跨语言消息、数据库表或任意路径读取接口。

媒体目录需要由可信 ingestion 显式登记如下映射：

```text
media_asset.object_uri = upload://<console_upload.id>
media_asset.stream_id = MaterialUnit.stream_id
media_asset.sha256 = SourceReference.content_hash = console_upload.sha256
media_source.type = file
```

`upload://` 的语义限定为：**该上传文件就是从当前 stream 零点开始的完整原片**。
不适用于直播分段、裁剪片段、转码派生文件或有非零起始偏移的资产；这些情况必须等后续
时间映射契约。本功能不按文件名/相同摘要猜测关系，也不提供浏览器手工修改映射入口。

解析前检查：

1. 指定 revision 确实引用了这个 asset；历史版本按自身引用处理。
2. asset 同 stream、同 owner，且全部该 asset 的引用摘要一致。
3. 原片时长已登记为正值；各来源区间合法且不超过时长。
4. 上传已经保存、同 owner、摘要相符；平台摘要路径存在、长度一致且不是符号链接。

解析后通过现有 `/v1/assets/{upload_id}/content` 播放；该路径每次请求仍校验 assets 权限和 owner，
支持 GET/HEAD/Range，响应不缓存。API 从不暴露 `object_uri`、主机路径或密钥，不发起远程媒体请求。
摘要一致性采用可信入库元数据，读取检查文件长度与类型；不对每个 Range 请求重新计算整片 SHA-256。

未映射来源返回 `material_source_unmapped`；摘要/时间错误、缺失文件、权限不足分别显示明确错误，
不会用其它视频替代。浏览器不支持编码时显示失败和重载入口。

## 播放定位语义

- 点击观测后暂停并 seek 到其真实 `start_ms / 1000`；界面显示浏览器实际 `currentTime`。
- “播放此区间”从起点播放；默认在 `end_ms` 后的媒体时间更新时暂停，可取消该选项继续看原片。
- 多来源时只自动选择完整覆盖当前观测的引用；手动选择来源按该来源区间播放。
- 版本/文件变化时播放器重建；同一原片的其它观测重新定位，不沿用之前的区间。
- 原片实际时长不足时停止定位并提示。时间超过 JS 安全整数范围时也拒绝转换，不四舍五入后播放。
- 浏览器 seek 和 `timeupdate` 暂停不是逐帧剪辑边界保证；本功能不导出剪辑，不宣称帧级精度。

## 验证

所有 Python/前端验证在容器内执行；Rust 沿项目例外使用主机 Cargo。

```sh
make check
make integration
make console-build
docker compose --env-file .env -f deploy/compose/docker-compose.poc.yml exec -T console \
  sh -c 'cd /workspace/apps/console && npm run test:materials && npm run format:check'
```

`tests/integration/test_material_review.py` 使用随机测试 schema 和明确的契约 fixture。
`apps/console/tests/material-utils.test.mjs` 验证毫秒转换、查询边界、来源覆盖、URL/版本和嵌套文字展示。
浏览器人工/自动验收需实际登录、读取真实 API，并用授权原片检查播放位置、历史版本与错误状态。
测试 fixture 不得写进业务库或被称作模型识别结果。
