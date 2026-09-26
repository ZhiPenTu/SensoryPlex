import { Fragment, useMemo, useRef, useState, type ReactNode } from 'react';
import { Button, Space, Tag, Tooltip, Typography } from 'antd';
import {
    AimOutlined,
    AudioOutlined,
    ClockCircleOutlined,
    EyeOutlined,
    FileTextOutlined,
    VideoCameraOutlined,
} from '@ant-design/icons';
import { Link } from 'react-router-dom';
import type { JsonObject, MaterialUnit } from '../api/contracts';
import { formatTime, modalityNames, payloadText, statusNames } from './material-utils';
import './alignment-timeline.css';

const { Text } = Typography;

/**
 * 已知端侧模态的固定轨道顺序。
 * 未列入的模态会追加到末尾，避免把真实存在的观测事实隐藏掉；
 * 计数为零的轨道保留占位，显式声明「未观测」，不做任何合成或插值。
 */
const PRIMARY_LANES: { key: string; icon: ReactNode; color: string }[] = [
    { key: 'asr_segment', icon: <AudioOutlined />, color: '#d97706' },
    { key: 'ocr_blocks', icon: <FileTextOutlined />, color: '#1d4ed8' },
    { key: 'vision.scene_description', icon: <EyeOutlined />, color: '#059669' },
];
/** 未知模态（后续新增的观测种类）使用中性色，既不隐藏也不伪造语义。 */
const DEFAULT_LANE_COLOR = '#64748b';

const unitColors: Record<string, string> = {
    fast_ready: '#1d4ed8',
    partial: '#d97706',
    enriched: '#059669',
    conflict: '#dc2626',
    failed: '#dc2626',
    rejected: '#dc2626',
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
}

export default function AlignmentTimeline({
    materials,
    startMs,
    endMs,
    detailHref,
    onLocate,
}: AlignmentTimelineProps) {
    const trackRef = useRef<HTMLDivElement>(null);
    const [cursorMs, setCursorMs] = useState<number | null>(null);

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

    const handleMove = (event: React.MouseEvent<HTMLDivElement>) => {
        const rect = trackRef.current?.getBoundingClientRect();
        if (!rect || rect.width <= 0) return;
        const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
        setCursorMs(Math.round(start + ratio * span));
    };

    const ticks = [0, 0.25, 0.5, 0.75, 1];
    // 游标线在每一层轨道上各自绘制，保证刻度尺与各模态轨道共用同一几何基准。
    const cursorLine =
        cursorMs == null ? null : (
            <span className="alignment-cursor-line" style={{ left: `${percent(cursorMs)}%` }} />
        );

    return (
        <section className="alignment-panel">
            <div className="alignment-head">
                <div className="alignment-flow">
                    <span>视频流</span>
                    <span className="alignment-flow-arrow">→</span>
                    <span>端侧多模态感知</span>
                    <span className="alignment-flow-arrow">→</span>
                    <span className="alignment-flow-strong">多模态时间轴对齐</span>
                    <span className="alignment-flow-arrow">→</span>
                    <span>素材单元</span>
                </div>
                <Space size={[6, 6]} wrap>
                    {lanes.map((lane) => {
                        const count = factsByLane.get(lane.key)?.length ?? 0;
                        return (
                            <Tag
                                key={lane.key}
                                className={
                                    count ? 'alignment-count-tag' : 'alignment-count-tag empty'
                                }
                            >
                                <span className="alignment-axis-icon" style={{ color: lane.color }}>
                                    {lane.icon}
                                </span>
                                {modalityNames[lane.key] || lane.key} {count}
                            </Tag>
                        );
                    })}
                    <Tag className="alignment-count-tag">{facts.length} 条观测事实</Tag>
                </Space>
            </div>

            <div className="alignment-grid">
                <div className="alignment-axis-label">时间刻度</div>
                <div className="alignment-ruler" ref={trackRef} onMouseMove={handleMove}>
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
                        <>
                            <span
                                className="alignment-cursor-bubble"
                                style={{ left: `${percent(cursorMs)}%` }}
                            >
                                {formatTime(String(cursorMs))}
                            </span>
                            {cursorLine}
                        </>
                    )}
                </div>

                <div className="alignment-axis-label">素材单元</div>
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

                {lanes.map((lane) => {
                    const list = factsByLane.get(lane.key) ?? [];
                    return (
                        <Fragment key={lane.key}>
                            <div className="alignment-axis-label">
                                <span className="alignment-axis-icon" style={{ color: lane.color }}>
                                    {lane.icon}
                                </span>
                                {modalityNames[lane.key] || lane.key}
                            </div>
                            <div className="alignment-track">
                                {cursorLine}
                                {list.length ? (
                                    list.map((fact) => {
                                        const left = percent(fact.startMs);
                                        const width = Math.max(percent(fact.endMs) - left, 0.22);
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
                                                        opacity: active ? 1 : 0.45,
                                                    }}
                                                    onClick={() =>
                                                        setCursorMs(
                                                            Math.round(
                                                                (fact.startMs + fact.endMs) / 2,
                                                            ),
                                                        )
                                                    }
                                                />
                                            </Tooltip>
                                        );
                                    })
                                ) : (
                                    <span className="alignment-lane-empty">未观测到该模态事实</span>
                                )}
                            </div>
                        </Fragment>
                    );
                })}
            </div>

            <div className="alignment-readout">
                <div className="alignment-readout-head">
                    <Space size={10} align="center" wrap>
                        <ClockCircleOutlined style={{ color: '#1d4ed8' }} />
                        <Text strong style={{ fontSize: 13 }}>
                            {cursorMs == null
                                ? '时间节点读取窗'
                                : `时间节点 ${formatTime(String(cursorMs))}`}
                        </Text>
                        {cursorMs == null ? (
                            <Text type="secondary" style={{ fontSize: 12 }}>
                                将鼠标移到时间轴上，读取该时间节点的画面文字、画面描述与语音等观测事实
                            </Text>
                        ) : null}
                    </Space>
                    <Space size={8}>
                        {cursorMs != null ? (
                            <Button size="small" type="text" onClick={() => setCursorMs(null)}>
                                清除读取
                            </Button>
                        ) : null}
                        {cursorMs != null && onLocate ? (
                            <Button
                                size="small"
                                icon={<AimOutlined />}
                                onClick={() => onLocate(cursorMs)}
                            >
                                定位回放
                            </Button>
                        ) : null}
                    </Space>
                </div>

                {cursorMs == null ? null : (
                    <div className="alignment-readout-grid">
                        {readout.map(({ lane, fact, relation, offsetMs }) => (
                            <div className="alignment-readout-cell" key={lane.key}>
                                <div className="alignment-readout-title">
                                    <span
                                        className="alignment-axis-icon"
                                        style={{ color: lane.color }}
                                    >
                                        {lane.icon}
                                    </span>
                                    <span>{modalityNames[lane.key] || lane.key}</span>
                                    {relation === 'nearest' ? (
                                        <Tag
                                            color="warning"
                                            style={{ margin: 0, fontSize: 10, lineHeight: '16px' }}
                                        >
                                            邻近 {offsetMs > 0 ? '+' : ''}
                                            {offsetMs} ms
                                        </Tag>
                                    ) : null}
                                    {relation === 'absent' ? (
                                        <Tag
                                            style={{ margin: 0, fontSize: 10, lineHeight: '16px' }}
                                        >
                                            未观测
                                        </Tag>
                                    ) : null}
                                </div>

                                {fact ? (
                                    <>
                                        <div className="alignment-readout-time">
                                            {formatTime(String(fact.startMs))} ~{' '}
                                            {formatTime(String(fact.endMs))}
                                        </div>
                                        <div className="alignment-readout-text">
                                            {fact.text || '（该观测事实无文本载荷）'}
                                        </div>
                                        <div className="alignment-readout-meta">
                                            {fact.confidence == null
                                                ? `置信度未知${fact.confidenceReason ? ` · ${fact.confidenceReason}` : ''}`
                                                : `置信度 ${fact.confidence.toFixed(3)}`}
                                            {fact.qualityState
                                                ? ` · 质量 ${fact.qualityState}`
                                                : ''}
                                            {fact.timingSource
                                                ? ` · 计时来源 ${fact.timingSource}`
                                                : ''}
                                        </div>
                                        {fact.provenance ? (
                                            <div className="alignment-readout-meta mono">
                                                {fact.provenance}
                                            </div>
                                        ) : null}
                                    </>
                                ) : (
                                    <div className="alignment-readout-text muted">
                                        本视频未观测到该模态事实，时间轴不做任何插值或合成。
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                )}
            </div>
        </section>
    );
}
