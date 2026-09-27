import { Fragment, useMemo, useRef, useState, type ReactNode } from 'react';
import { Button, Space, Tooltip } from 'antd';
import {
    AimOutlined,
    AudioOutlined,
    BackwardOutlined,
    ClearOutlined,
    ClockCircleOutlined,
    DashboardOutlined,
    EyeOutlined,
    FileTextOutlined,
    ForwardOutlined,
    UnorderedListOutlined,
    VideoCameraOutlined,
} from '@ant-design/icons';
import { Link } from 'react-router-dom';
import type { JsonObject, MaterialUnit, Upload } from '../api/contracts';
import { bytes } from '../components';
import { formatTime, modalityNames, payloadText, statusNames } from './material-utils';
import './alignment-timeline.css';

/**
 * 已知端侧模态的固定轨道顺序。
 * 未列入的模态会追加到末尾，避免把真实存在的观测事实隐藏掉；
 * 计数为零的轨道保留占位，显式声明「未观测」，不做任何合成或插值。
 */
const PRIMARY_LANES: { key: string; icon: ReactNode; color: string }[] = [
    { key: 'asr_segment', icon: <AudioOutlined />, color: '#f59e0b' },
    { key: 'ocr_blocks', icon: <FileTextOutlined />, color: '#38bdf8' },
    { key: 'vision.scene_description', icon: <EyeOutlined />, color: '#10b981' },
];
/** 未知模态（后续新增的观测种类）使用中性色，既不隐藏也不伪造语义。 */
const DEFAULT_LANE_COLOR = '#64748b';

const unitColors: Record<string, string> = {
    fast_ready: 'linear-gradient(90deg, #1d4ed8 0%, #2563eb 100%)',
    partial: 'linear-gradient(90deg, #b45309 0%, #d97706 100%)',
    enriched: 'linear-gradient(90deg, #047857 0%, #059669 100%)',
    conflict: 'linear-gradient(90deg, #b91c1c 0%, #dc2626 100%)',
    failed: 'linear-gradient(90deg, #b91c1c 0%, #dc2626 100%)',
    rejected: 'linear-gradient(90deg, #b91c1c 0%, #dc2626 100%)',
};

const unitBorders: Record<string, string> = {
    fast_ready: '#3b82f6',
    partial: '#f59e0b',
    enriched: '#10b981',
    conflict: '#ef4444',
    failed: '#ef4444',
    rejected: '#ef4444',
};

interface UnitBlock {
    materialUnitId: string;
    startMs: number;
    endMs: number;
    status: string;
    revision: number;
    observationCount: number;
    modalities: string[];
}

interface ObservationFact {
    observationId: string;
    modality: string;
    startMs: number;
    endMs: number;
    text: string;
    confidence?: number;
    confidenceReason: string;
    qualityState: string;
    provenance: string;
    timingSource: string;
}

/**
 * 观测事实的可读文本：OCR 载荷优先取 blocks[].text，
 * 避免把 engine / execution_backend 之类的非叙述字段混入时间轴读取内容。
 */
function factText(payload?: JsonObject): string {
    const blocks = (payload as { blocks?: unknown } | undefined)?.blocks;
    if (Array.isArray(blocks)) {
        const texts = blocks
            .map((block) =>
                block && typeof block === 'object' ? (block as { text?: unknown }).text : undefined,
            )
            .filter((text): text is string => typeof text === 'string' && text.length > 0);
        if (texts.length) return texts.join(' ').slice(0, 600);
    }
    return payloadText(payload, 600);
}

export interface AlignmentTimelineProps {
    /** 该视频母带下后端已返回的素材单元 */
    materials: MaterialUnit[];
    startMs: bigint;
    endMs: bigint;
    detailHref: (materialUnitId: string) => string;
    onLocate?: (ms: number) => void;
    onOpenDetails?: () => void;
    asset?: Upload;
    streamId?: string;
    streamTitle?: string;
}

export default function AlignmentTimeline({
    materials,
    startMs,
    endMs,
    detailHref,
    onLocate,
    onOpenDetails,
    asset,
    streamId,
    streamTitle,
}: AlignmentTimelineProps) {
    const trackRef = useRef<HTMLDivElement>(null);
    const videoRef = useRef<HTMLVideoElement | null>(null);

    const start = Number(startMs);
    const end = Number(endMs);
    const span = Math.max(end - start, 1);
    const percent = (ms: number) => Math.min(100, Math.max(0, ((ms - start) / span) * 100));

    const units = useMemo<UnitBlock[]>(
        () =>
            materials.map((m) => ({
                materialUnitId: m.material_unit_id,
                startMs: Number(m.time_range?.start_ms ?? start),
                endMs: Number(m.time_range?.end_ms ?? end),
                status: m.status,
                revision: m.revision,
                observationCount: m.observations.length,
                modalities: [...new Set(m.observations.map((o) => o.modality))],
            })),
        [materials, start, end],
    );

    // 默认游标定位到第一个素材单元起始位置，若无单元则为 null
    const initialCursorMs = useMemo(() => {
        if (units.length > 0) {
            return units[0].startMs;
        }
        return null;
    }, [units]);

    const [cursorMs, setCursorMs] = useState<number | null>(initialCursorMs);

    const seekVideoToMs = (ms: number) => {
        if (videoRef.current) {
            videoRef.current.currentTime = ms / 1000;
        }
    };

    const handleLocatePlay = () => {
        if (cursorMs != null) {
            seekVideoToMs(cursorMs);
            videoRef.current?.play().catch(() => {});
            onLocate?.(cursorMs);
        }
    };

    const handleTimeUpdate = () => {
        if (videoRef.current && !videoRef.current.paused) {
            const ms = Math.round(videoRef.current.currentTime * 1000);
            setCursorMs(ms);
        }
    };

    const handleStep = (deltaMs: number) => {
        const current = cursorMs ?? start;
        const target = Math.min(end, Math.max(start, current + deltaMs));
        setCursorMs(target);
        seekVideoToMs(target);
    };

    const facts = useMemo<ObservationFact[]>(() => {
        const collected: ObservationFact[] = [];
        for (const unit of units) {
            const source = materials.find((m) => m.material_unit_id === unit.materialUnitId);
            for (const o of source?.observations ?? []) {
                const factStart = Number(o.time_range?.start_ms ?? unit.startMs);
                const factEnd = Number(o.time_range?.end_ms ?? unit.endMs);
                if (!Number.isSafeInteger(factStart) || !Number.isSafeInteger(factEnd)) continue;
                collected.push({
                    observationId: o.observation_id,
                    modality: o.modality,
                    startMs: factStart,
                    endMs: Math.max(factEnd, factStart + 1),
                    text: factText(o.payload),
                    confidence: typeof o.confidence === 'number' ? o.confidence : undefined,
                    confidenceReason: o.confidence_unavailable_reason || '',
                    qualityState: o.quality_state || '',
                    provenance: [o.provenance?.plugin, o.provenance?.model_id]
                        .filter(Boolean)
                        .join(' · '),
                    timingSource: o.timing_source || '',
                });
            }
        }
        return collected.sort((a, b) => a.startMs - b.startMs);
    }, [materials, units]);

    const lanes = useMemo(() => {
        const present = new Set(facts.map((fact) => fact.modality));
        const known = PRIMARY_LANES.map((lane) => lane.key);
        const extra = [...present]
            .filter((key) => !known.includes(key))
            .sort()
            .map((key) => ({ key, icon: <VideoCameraOutlined />, color: DEFAULT_LANE_COLOR }));
        return [...PRIMARY_LANES, ...extra];
    }, [facts]);

    const factsByLane = useMemo(() => {
        const grouped = new Map<string, ObservationFact[]>();
        for (const fact of facts) {
            const list = grouped.get(fact.modality);
            if (list) list.push(fact);
            else grouped.set(fact.modality, [fact]);
        }
        return grouped;
    }, [facts]);

    // 游标读取：优先取覆盖该时刻的观测；否则给出时间最近的观测，并明确标注为「邻近」。
    const readout = useMemo(() => {
        if (cursorMs == null) return [];
        return lanes.map((lane) => {
            const list = factsByLane.get(lane.key) ?? [];
            if (!list.length) return { lane, fact: null, relation: 'absent' as const, offsetMs: 0 };
            const covering = list.find((fact) => fact.startMs <= cursorMs && cursorMs < fact.endMs);
            if (covering)
                return { lane, fact: covering, relation: 'covering' as const, offsetMs: 0 };
            let nearest = list[0];
            for (const fact of list) {
                if (Math.abs(fact.startMs - cursorMs) < Math.abs(nearest.startMs - cursorMs))
                    nearest = fact;
            }
            return {
                lane,
                fact: nearest,
                relation: 'nearest' as const,
                offsetMs: nearest.startMs - cursorMs,
            };
        });
    }, [cursorMs, lanes, factsByLane]);

    const asrReadout = useMemo(() => readout.find((r) => r.lane.key === 'asr_segment'), [readout]);
    const ocrReadout = useMemo(() => readout.find((r) => r.lane.key === 'ocr_blocks'), [readout]);
    const visionReadout = useMemo(
        () => readout.find((r) => r.lane.key === 'vision.scene_description'),
        [readout],
    );
    const extraReadouts = useMemo(
        () =>
            readout.filter(
                (r) =>
                    !['asr_segment', 'ocr_blocks', 'vision.scene_description'].includes(r.lane.key),
            ),
        [readout],
    );

    const handleMove = (event: React.MouseEvent<HTMLDivElement>) => {
        const rect = trackRef.current?.getBoundingClientRect();
        if (!rect || rect.width <= 0) return;
        if (event.clientX < rect.left) {
            return;
        }
        const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
        const newMs = Math.round(start + ratio * span);
        setCursorMs(newMs);
        seekVideoToMs(newMs);
    };

    const ticks = [0, 0.25, 0.5, 0.75, 1];
    // 游标线在每一层轨道上各自绘制，保证刻度尺与各模态轨道共用同一几何基准。
    const cursorLine =
        cursorMs == null ? null : (
            <span className="alignment-cursor-line" style={{ left: `${percent(cursorMs)}%` }} />
        );

    return (
        <section className="alignment-panel">
            {/* ── 顶部状态栏与流式指引 ─────────────────────────────────────────── */}
            <div className="alignment-head">
                <div className="alignment-flow">
                    <span className="alignment-flow-pulse" />
                    <span className="alignment-flow-label">SENSORYPLEX HUD</span>
                    <span className="alignment-flow-sep">|</span>
                    <span className="alignment-flow-item">视频母带</span>
                    <span className="alignment-flow-arrow">→</span>
                    <span className="alignment-flow-item">端侧感知</span>
                    <span className="alignment-flow-arrow">→</span>
                    <span className="alignment-flow-item active">时空对齐总线</span>
                    <span className="alignment-flow-arrow">→</span>
                    <span className="alignment-flow-item">素材切片</span>
                </div>

                <Space size={8} wrap align="center">
                    {lanes.map((lane) => {
                        const count = factsByLane.get(lane.key)?.length ?? 0;
                        return (
                            <div
                                key={lane.key}
                                className={`alignment-telemetry-tag${count ? '' : ' empty'}`}
                            >
                                <span
                                    className="alignment-telemetry-dot"
                                    style={{ background: count ? lane.color : '#475569' }}
                                />
                                <span>{modalityNames[lane.key] || lane.key}</span>
                                <span className="alignment-telemetry-num">{count}</span>
                            </div>
                        );
                    })}
                    <div className="alignment-telemetry-tag highlight">
                        <span>事实总数</span>
                        <span className="alignment-telemetry-num">{facts.length}</span>
                    </div>

                    {onOpenDetails ? (
                        <Button
                            size="small"
                            className="alignment-details-btn"
                            icon={<UnorderedListOutlined />}
                            onClick={onOpenDetails}
                        >
                            切片明细 ({materials.length})
                        </Button>
                    ) : null}
                </Space>
            </div>

            {/* ── 中部三栏控制台工作区 ─────────────────────────────────────────── */}
            <div className="alignment-cockpit-grid">
                {/* ── 左栏：顶部「其他信息」 + 底部「asr(语音转写)」 ── */}
                <div className="alignment-cockpit-col">
                    {/* 其他信息卡片 */}
                    <div className="alignment-tech-card accent-cyan">
                        <div>
                            <div className="alignment-card-head">
                                <div className="alignment-card-title">
                                    <DashboardOutlined style={{ color: '#38bdf8' }} />
                                    <span>其他信息 · TIMECODE</span>
                                </div>
                                <span className="alignment-flow-pulse" style={{ margin: 0 }} />
                            </div>

                            <div className="alignment-timecode-box">
                                <span className="alignment-timecode-digits">
                                    {cursorMs == null ? '--:--.---' : formatTime(String(cursorMs))}
                                </span>
                                <span className="alignment-timecode-desc">
                                    {cursorMs == null
                                        ? '监视器待命中 · 点击时间轴任意位置锁定'
                                        : '游标已锁定 · 毫秒级多模态对齐检视'}
                                </span>
                            </div>
                        </div>

                        <div className="alignment-meta-grid">
                            <div className="alignment-meta-grid-item">
                                跨度: <span>{(span / 1000).toFixed(1)}s</span>
                            </div>
                            <div className="alignment-meta-grid-item">
                                单元: <span>{units.length} 个</span>
                            </div>
                            <div className="alignment-meta-grid-item">
                                事实: <span>{facts.length} 条</span>
                            </div>
                            <div className="alignment-meta-grid-item">
                                覆盖: <span>{formatTime(String(start))}</span>
                            </div>
                        </div>
                    </div>

                    {/* ASR 语音转写卡片 */}
                    <div className="alignment-tech-card accent-amber">
                        <div>
                            <div className="alignment-card-head">
                                <div className="alignment-card-title">
                                    <AudioOutlined style={{ color: '#f59e0b' }} />
                                    <span>asr · 语音转写</span>
                                </div>
                                {asrReadout ? (
                                    <div>
                                        {asrReadout.relation === 'covering' ? (
                                            <span className="alignment-chip covering">
                                                精准覆盖 0ms
                                            </span>
                                        ) : null}
                                        {asrReadout.relation === 'nearest' ? (
                                            <span className="alignment-chip nearest">
                                                邻近 {asrReadout.offsetMs > 0 ? '+' : ''}
                                                {asrReadout.offsetMs} ms
                                            </span>
                                        ) : null}
                                        {asrReadout.relation === 'absent' ? (
                                            <span className="alignment-chip absent">未观测</span>
                                        ) : null}
                                    </div>
                                ) : null}
                            </div>

                            {asrReadout?.fact ? (
                                <>
                                    <div
                                        className="alignment-readout-time"
                                        style={{ color: '#f59e0b' }}
                                    >
                                        [ {formatTime(String(asrReadout.fact.startMs))} ~{' '}
                                        {formatTime(String(asrReadout.fact.endMs))} ]
                                    </div>
                                    <div className="alignment-readout-textbox">
                                        {asrReadout.fact.text || '（该观测事实无文本载荷）'}
                                    </div>
                                </>
                            ) : (
                                <div className="alignment-readout-empty">
                                    本视频未观测到该模态事实，时间轴严格不做任何插值或合成。
                                </div>
                            )}
                        </div>

                        {asrReadout?.fact ? (
                            <div>
                                <div className="alignment-readout-meta-row">
                                    <Tooltip title={asrReadout.fact.confidenceReason || undefined}>
                                        <span className="alignment-meta-pill">
                                            {asrReadout.fact.confidence == null
                                                ? '置信度未知'
                                                : `置信度: ${asrReadout.fact.confidence.toFixed(3)}`}
                                        </span>
                                    </Tooltip>
                                    {asrReadout.fact.qualityState ? (
                                        <span className="alignment-meta-pill">
                                            质量: {asrReadout.fact.qualityState}
                                        </span>
                                    ) : null}
                                    {asrReadout.fact.timingSource ? (
                                        <span className="alignment-meta-pill">
                                            pts: {asrReadout.fact.timingSource}
                                        </span>
                                    ) : null}
                                </div>
                                {asrReadout.fact.provenance ? (
                                    <Tooltip title={asrReadout.fact.provenance}>
                                        <div className="alignment-readout-prov">
                                            PROV: {asrReadout.fact.provenance}
                                        </div>
                                    </Tooltip>
                                ) : null}
                            </div>
                        ) : null}
                    </div>
                </div>

                {/* ── 中栏：「原视频播放窗口」 ── */}
                <div className="alignment-center-player-card">
                    <div className="alignment-card-head">
                        <div className="alignment-card-title">
                            <VideoCameraOutlined style={{ color: '#38bdf8' }} />
                            <span>原视频播放窗口 · MONITOR</span>
                        </div>
                        {asset ? (
                            <span className="alignment-chip covering">
                                {asset.content_type} · {bytes(asset.size_bytes)}
                            </span>
                        ) : (
                            <span className="alignment-chip absent">未挂载原片</span>
                        )}
                    </div>

                    <div className="alignment-video-wrapper">
                        {asset ? (
                            <video
                                ref={videoRef}
                                controls
                                preload="metadata"
                                src={`/v1/assets/${asset.id}/content`}
                                onTimeUpdate={handleTimeUpdate}
                                onLoadedMetadata={() => {
                                    if (cursorMs != null && videoRef.current) {
                                        videoRef.current.currentTime = cursorMs / 1000;
                                    }
                                }}
                            />
                        ) : (
                            <div className="alignment-video-empty">
                                <VideoCameraOutlined style={{ fontSize: 32, color: '#475569' }} />
                                <div style={{ color: '#94a3b8', fontWeight: 600 }}>
                                    未关联原片媒体文件
                                </div>
                                <div style={{ fontSize: 11 }}>
                                    {streamTitle || streamId || '视频母带'}
                                </div>
                            </div>
                        )}
                    </div>

                    <div className="alignment-video-controls">
                        <div className="alignment-video-sync-tag">
                            <ClockCircleOutlined />
                            <span>
                                游标同步:{' '}
                                {cursorMs == null ? '--:--.---' : formatTime(String(cursorMs))}
                            </span>
                        </div>
                        <Space size={6}>
                            <Button
                                size="small"
                                className="alignment-tech-ghost-btn"
                                icon={<BackwardOutlined />}
                                onClick={() => handleStep(-5000)}
                            >
                                -5s
                            </Button>
                            <Button
                                size="small"
                                className="alignment-tech-ghost-btn"
                                icon={<ForwardOutlined />}
                                onClick={() => handleStep(5000)}
                            >
                                +5s
                            </Button>
                            <Button
                                size="small"
                                className="alignment-tech-primary-btn"
                                icon={<AimOutlined />}
                                onClick={handleLocatePlay}
                            >
                                定位同步
                            </Button>
                        </Space>
                    </div>
                </div>

                {/* ── 右栏：顶部「画面描述(Vision)」 + 底部「画面文字(OCR)」 ── */}
                <div className="alignment-cockpit-col">
                    {/* 画面描述卡片（含 清除读取 与 定位回放 按钮） */}
                    <div className="alignment-tech-card accent-emerald">
                        <div>
                            <div className="alignment-card-head">
                                <div className="alignment-card-title">
                                    <EyeOutlined style={{ color: '#10b981' }} />
                                    <span>画面描述 · Vision</span>
                                </div>
                                <Space size={6} align="center">
                                    {cursorMs != null ? (
                                        <Button
                                            size="small"
                                            className="alignment-tech-ghost-btn"
                                            icon={<ClearOutlined />}
                                            onClick={() => setCursorMs(null)}
                                        >
                                            清除读取
                                        </Button>
                                    ) : null}
                                    <Button
                                        size="small"
                                        type="primary"
                                        className="alignment-tech-primary-btn"
                                        icon={<AimOutlined />}
                                        onClick={handleLocatePlay}
                                    >
                                        定位回放
                                    </Button>
                                </Space>
                            </div>

                            <div style={{ marginBottom: 4 }}>
                                {visionReadout ? (
                                    <div>
                                        {visionReadout.relation === 'covering' ? (
                                            <span className="alignment-chip covering">
                                                精准覆盖 0ms
                                            </span>
                                        ) : null}
                                        {visionReadout.relation === 'nearest' ? (
                                            <span className="alignment-chip nearest">
                                                邻近 {visionReadout.offsetMs > 0 ? '+' : ''}
                                                {visionReadout.offsetMs} ms
                                            </span>
                                        ) : null}
                                        {visionReadout.relation === 'absent' ? (
                                            <span className="alignment-chip absent">未观测</span>
                                        ) : null}
                                    </div>
                                ) : null}
                            </div>

                            {visionReadout?.fact ? (
                                <>
                                    <div
                                        className="alignment-readout-time"
                                        style={{ color: '#10b981' }}
                                    >
                                        [ {formatTime(String(visionReadout.fact.startMs))} ~{' '}
                                        {formatTime(String(visionReadout.fact.endMs))} ]
                                    </div>
                                    <div className="alignment-readout-textbox">
                                        {visionReadout.fact.text || '（该观测事实无文本载荷）'}
                                    </div>
                                </>
                            ) : (
                                <div className="alignment-readout-empty">
                                    本视频未观测到该模态事实，时间轴严格不做任何插值或合成。
                                </div>
                            )}
                        </div>

                        {visionReadout?.fact ? (
                            <div>
                                <div className="alignment-readout-meta-row">
                                    <span className="alignment-meta-pill">
                                        {visionReadout.fact.confidence == null
                                            ? '置信度未知'
                                            : `置信度: ${visionReadout.fact.confidence.toFixed(3)}`}
                                    </span>
                                    {visionReadout.fact.qualityState ? (
                                        <span className="alignment-meta-pill">
                                            质量: {visionReadout.fact.qualityState}
                                        </span>
                                    ) : null}
                                </div>
                                {visionReadout.fact.provenance ? (
                                    <Tooltip title={visionReadout.fact.provenance}>
                                        <div className="alignment-readout-prov">
                                            PROV: {visionReadout.fact.provenance}
                                        </div>
                                    </Tooltip>
                                ) : null}
                            </div>
                        ) : null}
                    </div>

                    {/* 画面文字 (OCR) 卡片 */}
                    <div className="alignment-tech-card accent-cyan">
                        <div>
                            <div className="alignment-card-head">
                                <div className="alignment-card-title">
                                    <FileTextOutlined style={{ color: '#38bdf8' }} />
                                    <span>画面文字 · OCR</span>
                                </div>
                                {ocrReadout ? (
                                    <div>
                                        {ocrReadout.relation === 'covering' ? (
                                            <span className="alignment-chip covering">
                                                精准覆盖 0ms
                                            </span>
                                        ) : null}
                                        {ocrReadout.relation === 'nearest' ? (
                                            <span className="alignment-chip nearest">
                                                邻近 {ocrReadout.offsetMs > 0 ? '+' : ''}
                                                {ocrReadout.offsetMs} ms
                                            </span>
                                        ) : null}
                                        {ocrReadout.relation === 'absent' ? (
                                            <span className="alignment-chip absent">未观测</span>
                                        ) : null}
                                    </div>
                                ) : null}
                            </div>

                            {ocrReadout?.fact ? (
                                <>
                                    <div
                                        className="alignment-readout-time"
                                        style={{ color: '#38bdf8' }}
                                    >
                                        [ {formatTime(String(ocrReadout.fact.startMs))} ~{' '}
                                        {formatTime(String(ocrReadout.fact.endMs))} ]
                                    </div>
                                    <div className="alignment-readout-textbox">
                                        {ocrReadout.fact.text || '（该观测事实无文本载荷）'}
                                    </div>
                                </>
                            ) : (
                                <div className="alignment-readout-empty">
                                    本视频未观测到该模态事实，时间轴严格不做任何插值或合成。
                                </div>
                            )}
                        </div>

                        {ocrReadout?.fact ? (
                            <div>
                                <div className="alignment-readout-meta-row">
                                    <Tooltip title={ocrReadout.fact.confidenceReason || undefined}>
                                        <span className="alignment-meta-pill">
                                            {ocrReadout.fact.confidence == null
                                                ? '置信度未知'
                                                : `置信度: ${ocrReadout.fact.confidence.toFixed(3)}`}
                                        </span>
                                    </Tooltip>
                                    {ocrReadout.fact.qualityState ? (
                                        <span className="alignment-meta-pill">
                                            质量: {ocrReadout.fact.qualityState}
                                        </span>
                                    ) : null}
                                    {ocrReadout.fact.timingSource ? (
                                        <span className="alignment-meta-pill">
                                            pts: {ocrReadout.fact.timingSource}
                                        </span>
                                    ) : null}
                                </div>
                                {ocrReadout.fact.provenance ? (
                                    <Tooltip title={ocrReadout.fact.provenance}>
                                        <div className="alignment-readout-prov">
                                            PROV: {ocrReadout.fact.provenance}
                                        </div>
                                    </Tooltip>
                                ) : null}
                            </div>
                        ) : null}
                    </div>
                </div>
            </div>

            {/* 附加自定义模态（若有） */}
            {extraReadouts.length ? (
                <div style={{ display: 'flex', gap: 12, marginBottom: 12 }}>
                    {extraReadouts.map(({ lane, fact, relation, offsetMs }) => (
                        <div
                            key={lane.key}
                            className="alignment-tech-card"
                            style={{ borderTop: `2px solid ${lane.color}` }}
                        >
                            <div className="alignment-card-head">
                                <div className="alignment-card-title">
                                    <span style={{ color: lane.color }}>{lane.icon}</span>
                                    <span>{modalityNames[lane.key] || lane.key}</span>
                                </div>
                                <span className={`alignment-chip ${relation}`}>
                                    {relation === 'covering'
                                        ? '覆盖中'
                                        : relation === 'nearest'
                                          ? `邻近 ${offsetMs}ms`
                                          : '未观测'}
                                </span>
                            </div>
                            <div className="alignment-readout-textbox">
                                {fact?.text || '（无文本载荷）'}
                            </div>
                        </div>
                    ))}
                </div>
            ) : null}

            {/* ── 底部：更紧凑的全宽多功能时间轴 (Compact Multi-function Timeline) ── */}
            <div className="alignment-tracks-deck">
                <div className="alignment-tracks-titlebar">
                    <span>多功能时间轴 · TIMELINE BUS</span>
                    <span className="alignment-tracks-range">
                        SPAN: {formatTime(String(start))} ~ {formatTime(String(end))} (
                        {(span / 1000).toFixed(1)}s)
                    </span>
                </div>

                <div className="alignment-grid" onMouseMove={handleMove} onClick={handleMove}>
                    {/* 时间刻度 */}
                    <div className="alignment-axis-label">
                        <ClockCircleOutlined style={{ color: '#38bdf8' }} />
                        <span>时间刻度</span>
                    </div>
                    <div className="alignment-ruler" ref={trackRef}>
                        {ticks.map((ratio) => {
                            const at = Math.round(start + ratio * span);
                            return (
                                <span
                                    key={ratio}
                                    className={`alignment-tick-label${ratio === 0 ? ' first' : ''}${ratio === 1 ? ' last' : ''}`}
                                    style={{ left: `${ratio * 100}%` }}
                                >
                                    {formatTime(String(at))}
                                </span>
                            );
                        })}
                        {cursorMs == null ? null : (
                            <span
                                className="alignment-cursor-bubble"
                                style={{ left: `${percent(cursorMs)}%` }}
                            >
                                {formatTime(String(cursorMs))}
                            </span>
                        )}
                        {cursorLine}
                    </div>

                    {/* 素材单元 */}
                    <div className="alignment-axis-label">
                        <VideoCameraOutlined style={{ color: '#818cf8' }} />
                        <span>素材单元</span>
                    </div>
                    <div className="alignment-track">
                        {units.map((unit) => {
                            const left = percent(unit.startMs);
                            const width = Math.max(percent(unit.endMs) - left, 0.6);
                            return (
                                <Tooltip
                                    key={unit.materialUnitId}
                                    title={`${formatTime(String(unit.startMs))} ~ ${formatTime(String(unit.endMs))} · ${statusNames[unit.status] || unit.status} · v${unit.revision} · ${unit.observationCount} 条观测`}
                                >
                                    <Link
                                        to={detailHref(unit.materialUnitId)}
                                        className="alignment-unit"
                                        style={{
                                            left: `${left}%`,
                                            width: `${width}%`,
                                            background: unitColors[unit.status] || '#1d4ed8',
                                            borderColor: unitBorders[unit.status] || '#3b82f6',
                                        }}
                                        onClick={() => {
                                            setCursorMs(unit.startMs);
                                            seekVideoToMs(unit.startMs);
                                        }}
                                    >
                                        {width >= 7 ? (
                                            <span className="alignment-unit-text">
                                                {statusNames[unit.status] || unit.status}
                                            </span>
                                        ) : null}
                                    </Link>
                                </Tooltip>
                            );
                        })}
                        {cursorLine}
                    </div>

                    {/* 各模态轨道 */}
                    {lanes.map((lane) => {
                        const list = factsByLane.get(lane.key) ?? [];
                        return (
                            <Fragment key={lane.key}>
                                <div className="alignment-axis-label">
                                    <span style={{ color: lane.color, fontSize: 11 }}>
                                        {lane.icon}
                                    </span>
                                    <span>{modalityNames[lane.key] || lane.key}</span>
                                </div>
                                <div className="alignment-track">
                                    {cursorLine}
                                    {list.length ? (
                                        list.map((fact) => {
                                            const left = percent(fact.startMs);
                                            const width = Math.max(percent(fact.endMs) - left, 0.3);
                                            const active =
                                                cursorMs != null &&
                                                fact.startMs <= cursorMs &&
                                                cursorMs < fact.endMs;
                                            return (
                                                <Tooltip
                                                    key={fact.observationId}
                                                    title={`${formatTime(String(fact.startMs))} ~ ${formatTime(String(fact.endMs))}｜${fact.text.slice(0, 120) || '（无文本载荷）'}`}
                                                >
                                                    <button
                                                        type="button"
                                                        aria-label={`${modalityNames[fact.modality] || fact.modality} ${formatTime(String(fact.startMs))}`}
                                                        className={`alignment-tick${active ? ' active' : ''}`}
                                                        style={{
                                                            left: `${left}%`,
                                                            width: `${width}%`,
                                                            background: lane.color,
                                                            opacity: active ? 1 : 0.65,
                                                        }}
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            const mid = Math.round(
                                                                (fact.startMs + fact.endMs) / 2,
                                                            );
                                                            setCursorMs(mid);
                                                            seekVideoToMs(mid);
                                                        }}
                                                    />
                                                </Tooltip>
                                            );
                                        })
                                    ) : (
                                        <span className="alignment-lane-empty">
                                            未观测到该模态事实
                                        </span>
                                    )}
                                </div>
                            </Fragment>
                        );
                    })}
                </div>
            </div>
        </section>
    );
}
