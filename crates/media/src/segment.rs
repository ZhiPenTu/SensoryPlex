//! 对已解码 PCM 做音频分段。
//!
//! 分段边界由该段实际包含的样本决定，因此报告的 `[start_ms, end_ms)` 不会
//! 覆盖 payload 实际不包含的音频。输入时间轴上出现空洞时会关闭当前段，
//! 不会被吸收进段内。

use std::collections::BTreeSet;

use crate::MediaError;

pub const DEFAULT_AUDIO_SEGMENT_MS: u32 = 5_000;
pub const MIN_AUDIO_SEGMENT_MS: u32 = 200;
pub const MAX_AUDIO_SEGMENT_MS: u32 = 30_000;
/// 解码后的音频强制为 `F32LE`，因此一帧字节数为 `channels * 4`。
pub const AUDIO_SAMPLE_BYTES: usize = 4;
/// 样本时长按整毫秒四舍五入传入，因此相邻样本几乎不会精确对齐。
/// 只有达到此阈值的空洞才算真正的 discontinuity；更小的只是算术误差，
/// 若据此关闭 segment 会无谓地切碎流。
pub const DISCONTINUITY_THRESHOLD_MS: i64 = 250;

#[derive(Debug, Clone, PartialEq)]
pub struct PendingSegment {
    pub start_ms: i64,
    pub end_ms: i64,
    pub bytes: Vec<u8>,
    /// 当流结束时该段短于配置长度，则为 true。
    pub partial: bool,
}

#[derive(Debug, Default)]
pub struct AudioSegmenter {
    segment_ms: u32,
    sample_rate: u32,
    channels: u32,
    start_ms: Option<i64>,
    end_ms: i64,
    /// 预期的下一个连续点。只有与该点对齐才能让 segment 保持开启。
    cursor_ms: Option<i64>,
    pending: Vec<u8>,
    dropped_samples: u64,
    discontinuities: u64,
    drop_reasons: BTreeSet<&'static str>,
}

impl AudioSegmenter {
    pub fn new(segment_ms: u32) -> Result<Self, MediaError> {
        if !(MIN_AUDIO_SEGMENT_MS..=MAX_AUDIO_SEGMENT_MS).contains(&segment_ms) {
            return Err(MediaError::UnsupportedSource(
                "audio_segment_ms_out_of_range".into(),
            ));
        }
        Ok(Self {
            segment_ms,
            ..Default::default()
        })
    }

    pub fn segment_ms(&self) -> u32 {
        self.segment_ms
    }

    pub fn sample_rate(&self) -> u32 {
        self.sample_rate
    }

    pub fn channels(&self) -> u32 {
        self.channels
    }

    pub fn dropped_samples(&self) -> u64 {
        self.dropped_samples
    }

    pub fn discontinuities(&self) -> u64 {
        self.discontinuities
    }

    pub fn drop_reasons(&self) -> Vec<String> {
        self.drop_reasons
            .iter()
            .map(|reason| (*reason).to_string())
            .collect()
    }

    fn frame_bytes(&self) -> usize {
        self.channels as usize * AUDIO_SAMPLE_BYTES
    }

    /// 拒绝猜测：没有 sample rate 与 channel count 就没有字节预算。
    pub fn push(
        &mut self,
        sample_rate: u32,
        channels: u32,
        pts_ms: i64,
        duration_ms: i64,
        bytes: &[u8],
    ) -> Result<Option<PendingSegment>, MediaError> {
        if sample_rate == 0 || channels == 0 {
            return Err(MediaError::UnsupportedSource(
                "audio_segment_format_unknown".into(),
            ));
        }
        if (self.sample_rate, self.channels) == (0, 0) {
            self.sample_rate = sample_rate;
            self.channels = channels;
        } else if (self.sample_rate, self.channels) != (sample_rate, channels) {
            return Err(MediaError::UnsupportedSource(
                "audio_format_changed_mid_stream".into(),
            ));
        }
        if bytes.is_empty() {
            return Ok(None);
        }
        if !bytes.len().is_multiple_of(self.frame_bytes()) {
            self.dropped_samples += 1;
            self.drop_reasons.insert("audio_bytes_not_frame_aligned");
            return Ok(None);
        }
        if pts_ms < 0 || duration_ms <= 0 {
            self.dropped_samples += 1;
            self.drop_reasons.insert("audio_timing_unavailable");
            return Ok(None);
        }

        // 时间轴上的空洞会关闭当前 segment，绝不会被跨过填补。
        let mut flushed = None;
        let drifted = self
            .cursor_ms
            .is_some_and(|expected| (pts_ms - expected).abs() > DISCONTINUITY_THRESHOLD_MS);
        if drifted {
            self.discontinuities += 1;
            self.drop_reasons.insert("audio_timeline_discontinuity");
            if let Some(start) = self.start_ms {
                flushed = self.take(start);
            }
        }
        if self.start_ms.is_none() {
            self.start_ms = Some(pts_ms);
        }

        self.pending.extend_from_slice(bytes);
        self.end_ms = pts_ms + duration_ms;
        self.cursor_ms = Some(self.end_ms);

        // flush 判断按毫秒级四舍五入后的时间戳进行，因此单段可以比名义长度多
        // 容纳一点音频。上限取为名义长度的 2 倍：既不会被舍入误触发，
        // 也确保卡住的生产者不能无限增长这段缓冲。
        let budget = 2 * self.segment_ms as usize * sample_rate as usize * self.frame_bytes()
            / 1_000
            + 2 * self.frame_bytes();
        if self.pending.len() > budget {
            return Err(MediaError::DecodeFailed(format!(
                "audio_segment_overflow: {} bytes for a {}-byte budget",
                self.pending.len(),
                budget
            )));
        }

        let start = self.start_ms.unwrap_or(pts_ms);
        if self.end_ms - start >= i64::from(self.segment_ms) {
            let closed = self.take(start);
            return Ok(flushed.or(closed));
        }
        Ok(flushed)
    }

    /// flush 末尾可能存在的短段。
    pub fn finish(&mut self) -> Option<PendingSegment> {
        let start = self.start_ms?;
        self.take(start)
    }

    fn take(&mut self, start_ms: i64) -> Option<PendingSegment> {
        let bytes = std::mem::take(&mut self.pending);
        self.start_ms = None;
        if bytes.is_empty() {
            return None;
        }
        Some(PendingSegment {
            start_ms,
            end_ms: self.end_ms,
            partial: self.end_ms - start_ms < i64::from(self.segment_ms),
            bytes,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 1 kHz 单声道 10 ms 的 f32 音频样本，方便测试推理。
    const RATE: u32 = 1_000;
    const MONO_10MS: usize = 40;

    fn push(segmenter: &mut AudioSegmenter, pts_ms: i64) -> Option<PendingSegment> {
        segmenter
            .push(RATE, 1, pts_ms, 10, &[0u8; MONO_10MS])
            .unwrap()
    }

    #[test]
    fn cuts_on_the_sample_boundary_and_flushes_the_tail() {
        let mut segmenter = AudioSegmenter::new(200).unwrap();
        let mut segments = Vec::new();
        for index in 0..25 {
            if let Some(segment) = push(&mut segmenter, index * 10) {
                segments.push(segment);
            }
        }
        segments.extend(segmenter.finish());
        assert_eq!(segments.len(), 2);
        assert_eq!((segments[0].start_ms, segments[0].end_ms), (0, 200));
        assert_eq!(segments[0].bytes.len(), 20 * MONO_10MS);
        assert!(!segments[0].partial);
        assert_eq!((segments[1].start_ms, segments[1].end_ms), (200, 250));
        assert!(
            segments[1].partial,
            "the tail is shorter than the configured length"
        );
        assert_eq!(segments[1].bytes.len(), 5 * MONO_10MS);
    }

    #[test]
    fn a_timeline_hole_closes_the_segment_instead_of_widening_it() {
        let mut segmenter = AudioSegmenter::new(200).unwrap();
        assert!(push(&mut segmenter, 0).is_none());
        assert!(push(&mut segmenter, 10).is_none());
        let closed = push(&mut segmenter, 5_000).expect("gap closes a segment");
        assert_eq!((closed.start_ms, closed.end_ms), (0, 20));
        assert!(closed.partial);
        assert_eq!(segmenter.discontinuities(), 1);
        assert_eq!(
            segmenter.drop_reasons(),
            vec!["audio_timeline_discontinuity"]
        );
        let tail = segmenter.finish().unwrap();
        assert_eq!((tail.start_ms, tail.end_ms), (5_000, 5_010));
    }

    #[test]
    fn millisecond_rounding_does_not_fragment_a_continuous_stream() {
        // 44.1 kHz 下 1024 个样本为 23.22 ms，按 23 ms 上报：每帧 cursor 漂移
        // 不到一毫秒，绝不能解读为音频丢失。
        let mut segmenter = AudioSegmenter::new(1_000).unwrap();
        let mut segments = Vec::new();
        let mut pts_ms = 0i64;
        for _ in 0..430 {
            if let Some(segment) = segmenter
                .push(44_100, 1, pts_ms, 23, &[0u8; 4_096])
                .unwrap()
            {
                segments.push(segment);
            }
            pts_ms += 23;
        }
        segments.extend(segmenter.finish());
        assert_eq!(segments.len(), 10, "~10 seconds of audio is ~10 segments");
        assert_eq!(
            segmenter.discontinuities(),
            0,
            "rounding is not a discontinuity"
        );
        assert_eq!(segments[0].start_ms, 0);
        for pair in segments.windows(2) {
            assert_eq!(pair[0].end_ms, pair[1].start_ms, "segments stay contiguous");
        }
        assert_eq!(segments.last().unwrap().end_ms, pts_ms);
    }

    #[test]
    fn refuses_to_segment_without_a_known_format_or_a_sane_length() {
        assert!(AudioSegmenter::new(0).is_err());
        assert!(AudioSegmenter::new(MIN_AUDIO_SEGMENT_MS - 1).is_err());
        assert!(AudioSegmenter::new(MAX_AUDIO_SEGMENT_MS + 1).is_err());
        let mut segmenter = AudioSegmenter::new(200).unwrap();
        assert!(segmenter.push(0, 0, 0, 10, &[0u8; 4]).is_err());
        let long = AudioSegmenter::new(DEFAULT_AUDIO_SEGMENT_MS).unwrap();
        assert_eq!(long.segment_ms(), DEFAULT_AUDIO_SEGMENT_MS);
    }

    #[test]
    fn misaligned_or_untimed_samples_are_dropped_with_a_reason() {
        let mut segmenter = AudioSegmenter::new(200).unwrap();
        assert!(segmenter.push(RATE, 2, 0, 10, &[0u8; 3]).unwrap().is_none());
        assert!(segmenter.push(RATE, 2, 10, 0, &[0u8; 8]).unwrap().is_none());
        assert_eq!(segmenter.dropped_samples(), 2);
        assert_eq!(
            segmenter.drop_reasons(),
            vec!["audio_bytes_not_frame_aligned", "audio_timing_unavailable"]
        );
    }

    #[test]
    fn a_producer_that_never_advances_is_caught_instead_of_buffering_forever() {
        let mut segmenter = AudioSegmenter::new(200).unwrap();
        let mut error = None;
        for _ in 0..200 {
            // 每次都是同一个时间戳：segment 无法自行关闭。
            error = segmenter.push(RATE, 1, 0, 10, &[0u8; MONO_10MS]).err();
            if error.is_some() {
                break;
            }
        }
        assert!(
            matches!(error, Some(MediaError::DecodeFailed(ref reason)) if reason.starts_with("audio_segment_overflow")),
            "a stalled timeline must hit the bound: {error:?}"
        );
    }

    #[test]
    fn a_format_change_mid_stream_is_rejected() {
        let mut segmenter = AudioSegmenter::new(200).unwrap();
        push(&mut segmenter, 0);
        assert!(matches!(
            segmenter.push(48_000, 2, 10, 10, &[0u8; 8]),
            Err(MediaError::UnsupportedSource(reason)) if reason == "audio_format_changed_mid_stream"
        ));
    }
}
