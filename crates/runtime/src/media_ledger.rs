//! 逐帧语义账本的 JSONL 落盘。
//!
//! 账本是"输入完整性"的证据：每一个被解码且可用的视频帧都必须恰好出现一行，结论只能是
//! 选中（带角色）或未选中（带稳定原因码）。窗口单独成行，通过 `frame_buffer_ids` 引用
//! 已经交接的 descriptor，因此账本里**没有**原始帧、音频、tensor 或密钥，只有引用与判别结论。
//!
//! 文件格式为一行一个 JSON 对象，`record_type` 区分 `frame` 与 `window`。媒体 crate 不依赖
//! serde，所以序列化在这里实现；写入用 `Mutex` 串行化，保证多线程调用下每一行都完整。

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use sensoryplex_media::evidence::{FrameLedgerSink, FrameRecord, WindowRecord};
use serde_json::json;

/// 把账本写到 `path` 的 JSONL sink。文件在构造时创建（截断），任何写入失败都返回错误，
/// 由调用方决定是否让本次运行失败——账本丢了就不能声称"每一帧都有记录"。
pub struct JsonlLedgerSink {
    path: PathBuf,
    writer: Mutex<BufWriter<File>>,
}

impl JsonlLedgerSink {
    pub fn open(path: &str) -> Result<Self, String> {
        let file = File::create(Path::new(path))
            .map_err(|error| format!("evidence_ledger_create_failed: {path}: {error}"))?;
        Ok(Self {
            path: PathBuf::from(path),
            writer: Mutex::new(BufWriter::new(file)),
        })
    }

    fn write_line(&self, value: serde_json::Value) -> Result<(), String> {
        let mut line = serde_json::to_string(&value)
            .map_err(|error| format!("evidence_ledger_encode_failed: {error}"))?;
        line.push('\n');
        let mut writer = self
            .writer
            .lock()
            .map_err(|_| "evidence_ledger_poisoned".to_string())?;
        writer
            .write_all(line.as_bytes())
            .and_then(|_| writer.flush())
            .map_err(|error| {
                format!(
                    "evidence_ledger_write_failed: {}: {error}",
                    self.path.display()
                )
            })
    }
}

impl FrameLedgerSink for JsonlLedgerSink {
    fn write_frame(&self, record: &FrameRecord) -> Result<(), String> {
        self.write_line(json!({
            "record_type": "frame",
            "buffer_id": record.buffer_id,
            "frame_index": record.frame_index,
            "start_ms": record.start_ms,
            "end_ms": record.end_ms,
            "signature_delta": record.signature_delta,
            "text_signature_delta": record.text_signature_delta,
            "signature_available": record.signature_available,
            "decision": record.decision.name(),
            "selection": record.selection.name(),
            "window_id": record.window_id,
            "skip_reason": record.skip_reason,
        }))
    }

    fn write_window(&self, record: &WindowRecord) -> Result<(), String> {
        self.write_line(json!({
            "record_type": "window",
            "window_id": record.window_id,
            "start_ms": record.start_ms,
            "end_ms": record.end_ms,
            "anchor_ms": record.anchor_ms,
            "trigger": record.trigger,
            "frame_buffer_ids": record.frame_buffer_ids,
            "context_before": record.context_before,
            "context_after": record.context_after,
            "handed_off_frames": record.handed_off_frames,
            // 窗口里最大的帧序号：消费方据此复核"窗口出现时它的帧已经全部落账"。
            "last_frame_index": record.last_frame_index,
        }))
    }
}
