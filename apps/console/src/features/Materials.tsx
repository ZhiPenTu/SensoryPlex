import { useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button, Card, Col, Input, Row, Segmented, Space, Tag, Timeline, Typography } from 'antd';
import {
    SearchOutlined,
    ReloadOutlined,
    FilterOutlined,
    ArrowRightOutlined,
    ClockCircleOutlined,
    ClearOutlined,
    VideoCameraOutlined,
    DownOutlined,
    UpOutlined,
    PlayCircleOutlined,
    BarsOutlined,
    AppstoreOutlined,
} from '@ant-design/icons';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import type {
    MaterialUnit,
    SearchRequest,
    SearchResponse,
    Upload,
    UploadList,
} from '../api/contracts';
import { bytes, Empty, ErrorNotice, Heading, Loading, Modal } from '../components';
import {
    formatTime,
    modalityNames,
    payloadText,
    searchFields,
    searchParamsOnly,
    searchRequest,
    statusNames,
} from './material-utils';
import AlignmentTimeline from './AlignmentTimeline';
import './materials.css';

export { default as MaterialDetail } from './MaterialDetail';

const { Text } = Typography;

const statusTagColor: Record<string, string> = {
    fast_ready: 'blue',
    partial: 'warning',
    enriched: 'success',
    failed: 'error',
    rejected: 'error',
};

interface VideoGroup {
    streamId: string;
    asset?: Upload;
    title: string;
    materials: MaterialUnit[];
    startMs: bigint;
    endMs: bigint;
    tags: string[];
    modalities: string[];
}

function findAssetForStream(streamId: string, assets: Upload[] = []): Upload | undefined {
    const streamSuffix = streamId.replace(/^stream-/, '');
    return assets.find(
        (a) =>
            a.sha256.startsWith(`sha256:${streamSuffix}`) ||
            a.id === streamId ||
            a.sha256 === streamId,
    );
}

export default function Materials() {
    const [params, setParams] = useSearchParams();
    const [formError, setFormError] = useState<Error | null>(null);
    const [viewMode, setViewMode] = useState<'timeline' | 'grid'>('timeline');
    const [expandedStreams, setExpandedStreams] = useState<Record<string, boolean>>({});
    const [detailsOpen, setDetailsOpen] = useState<Record<string, boolean>>({});
    const [previewAsset, setPreviewAsset] = useState<{ asset: Upload; seekMs?: number } | null>(
        null,
    );
    const previewRef = useRef<HTMLVideoElement | null>(null);

    const [advancedOpen, setAdvancedOpen] = useState(
        searchFields.some((key) => !['q', 'limit'].includes(key) && params.has(key)),
    );

    let request: SearchRequest | undefined, validationError: unknown;
    try {
        request = searchRequest(params);
    } catch (error) {
        validationError = error;
    }

    const assetsListing = useQuery({
        queryKey: ['assets-for-materials'],
        queryFn: ({ signal }) => api<UploadList>('/v1/assets?limit=100', { signal }),
        staleTime: 60000,
    });

    const result = useQuery({
        queryKey: ['materials', request],
        queryFn: ({ signal }) =>
            api<SearchResponse>('/v1/materials:search', {
                method: 'POST',
                body: JSON.stringify(request),
                signal,
            }),
        enabled: !!request,
        retry: false,
    });

    const values = result.data?.materials || [];

    // 按视频母带 (Stream / Video Asset) 进行聚合分组
    const videoGroups = useMemo<VideoGroup[]>(() => {
        const map = new Map<string, VideoGroup>();
        const assets = assetsListing.data?.items || [];

        for (const m of values) {
            const sid = m.stream_id || 'unknown';
            if (!map.has(sid)) {
                const asset = findAssetForStream(sid, assets);
                map.set(sid, {
                    streamId: sid,
                    asset,
                    title: asset?.filename || `视频母带 (${sid})`,
                    materials: [],
                    startMs: BigInt(m.time_range?.start_ms || '0'),
                    endMs: BigInt(m.time_range?.end_ms || '0'),
                    tags: [],
                    modalities: [],
                });
            }
            const group = map.get(sid)!;
            group.materials.push(m);

            const curStart = BigInt(m.time_range?.start_ms || '0');
            const curEnd = BigInt(m.time_range?.end_ms || '0');
            if (curStart < group.startMs) group.startMs = curStart;
            if (curEnd > group.endMs) group.endMs = curEnd;

            for (const t of m.tags) {
                if (!group.tags.includes(t)) group.tags.push(t);
            }
            for (const o of m.observations) {
                if (!group.modalities.includes(o.modality)) group.modalities.push(o.modality);
            }
        }

        // 内部切片按起始毫秒升序（从小到大）排列，还原时间线顺序
        for (const group of map.values()) {
            group.materials.sort((a, b) => {
                const diff =
                    BigInt(a.time_range?.start_ms || '0') - BigInt(b.time_range?.start_ms || '0');
                if (diff !== 0n) return diff > 0n ? 1 : -1;
                return (b.revision || 0) - (a.revision || 0);
            });
        }

        return Array.from(map.values());
    }, [values, assetsListing.data?.items]);

    // 视频维度检索：把已上传资产按摘要映射到对应视频母带 (stream)，
    // 同一摘要的重复上传只保留一个入口，避免同名视频在列表里重复出现。
    const videoOptions = useMemo(() => {
        const options = new Map<string, string>();
        for (const asset of assetsListing.data?.items || []) {
            const digest = asset.sha256.replace(/^sha256:/, '');
            if (digest.length < 12) continue;
            const streamId = `stream-${digest.slice(0, 12)}`;
            if (!options.has(streamId))
                options.set(streamId, `${asset.filename} · ${asset.content_type}`);
        }
        for (const group of videoGroups) {
            if (!options.has(group.streamId)) options.set(group.streamId, group.title);
        }
        const selected = params.get('stream');
        if (selected && !options.has(selected)) options.set(selected, `视频母带 ${selected}`);
        return [...options.entries()].map(([streamId, label]) => ({ streamId, label }));
    }, [assetsListing.data?.items, videoGroups, params]);

    const isStreamExpanded = (streamId: string): boolean => {
        if (expandedStreams[streamId] !== undefined) return expandedStreams[streamId];
        // 默认展开所有匹配到的视频
        return true;
    };

    const toggleStream = (streamId: string) => {
        setExpandedStreams((prev) => ({
            ...prev,
            [streamId]: !isStreamExpanded(streamId),
        }));
    };

    const expandAll = () => {
        const next: Record<string, boolean> = {};
        for (const g of videoGroups) next[g.streamId] = true;
        setExpandedStreams(next);
    };

    const collapseAll = () => {
        const next: Record<string, boolean> = {};
        for (const g of videoGroups) next[g.streamId] = false;
        setExpandedStreams(next);
    };

    const handleSearchSubmit = (e: React.FormEvent<HTMLFormElement>) => {
        e.preventDefault();
        const data = new FormData(e.currentTarget);
        const next = new URLSearchParams();
        for (const key of searchFields) {
            const value = String(data.get(key) || '').trim();
            if (value) next.set(key, value);
        }
        try {
            searchRequest(next);
            setFormError(null);
            setParams(next);
            if (next.toString() === params.toString()) void result.refetch();
        } catch (error) {
            setFormError(error as Error);
        }
    };

    return (
        <div className="materials-page">
            <Heading
                eyebrow="Material Retrieval"
                title="素材检索"
                description="以视频母带为检索维度，把画面文字、画面描述、语音等观测事实对齐到同一条毫秒时间轴，逐时间节点读取并直达原片切片回看。"
                action={
                    <Button
                        icon={<ReloadOutlined />}
                        disabled={result.isFetching || !request}
                        onClick={() => void result.refetch()}
                    >
                        刷新结果
                    </Button>
                }
            />

            <form key={params.toString()} onSubmit={handleSearchSubmit}>
                <Card style={{ marginBottom: 20 }} bodyStyle={{ padding: 18 }}>
                    <div
                        style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}
                    >
                        <div className="material-video-picker">
                            <VideoCameraOutlined />
                            <select
                                name="stream"
                                aria-label="视频母带"
                                defaultValue={params.get('stream') || ''}
                            >
                                <option value="">全部视频母带</option>
                                {videoOptions.map((option) => (
                                    <option key={option.streamId} value={option.streamId}>
                                        {option.label}
                                    </option>
                                ))}
                            </select>
                        </div>
                        <Input
                            prefix={<SearchOutlined style={{ color: '#1668dc', fontSize: 16 }} />}
                            name="q"
                            aria-label="素材关键词"
                            placeholder="输入关键词检索画面文字 (OCR)、语音转写、视觉描述或语义意图…"
                            defaultValue={params.get('q') || ''}
                            maxLength={2000}
                            size="large"
                            allowClear
                            style={{ flex: 1 }}
                        />
                        <Button
                            type="primary"
                            htmlType="submit"
                            size="large"
                            loading={result.isFetching}
                            style={{ minWidth: 100 }}
                        >
                            检索素材
                        </Button>
                        <Button
                            size="large"
                            icon={<FilterOutlined />}
                            type={advancedOpen ? 'dashed' : 'default'}
                            onClick={() => setAdvancedOpen(!advancedOpen)}
                        >
                            高级筛选
                        </Button>
                    </div>

                    {advancedOpen ? (
                        <div
                            style={{
                                marginTop: 16,
                                paddingTop: 16,
                                borderTop: '1px solid #f1f5f9',
                            }}
                        >
                            <Row gutter={[16, 12]}>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            开始时间 (秒，例如 0.000)
                                        </Text>
                                        <Input
                                            name="start"
                                            inputMode="decimal"
                                            placeholder="例如 0.000"
                                            defaultValue={params.get('start') || ''}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            结束时间 (秒)
                                        </Text>
                                        <Input
                                            name="end"
                                            inputMode="decimal"
                                            placeholder="例如 12.500"
                                            defaultValue={params.get('end') || ''}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            最低置信度 (0.00 ~ 1.00)
                                        </Text>
                                        <Input
                                            name="confidence"
                                            inputMode="decimal"
                                            placeholder="例如 0.70"
                                            defaultValue={params.get('confidence') || ''}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            观测模态 (逗号分隔)
                                        </Text>
                                        <Input
                                            name="modalities"
                                            placeholder="例如 asr_segment,ocr_blocks"
                                            defaultValue={params.get('modalities') || ''}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            包含标签 (逗号分隔)
                                        </Text>
                                        <Input
                                            name="tags"
                                            placeholder="例如 会议,财务"
                                            defaultValue={params.get('tags') || ''}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                            </Row>

                            <div
                                style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    alignItems: 'center',
                                    marginTop: 16,
                                    paddingTop: 12,
                                    borderTop: '1px solid #f1f5f9',
                                }}
                            >
                                <Space size={16} align="center">
                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                        显示上限:
                                    </Text>
                                    <select
                                        name="limit"
                                        defaultValue={params.get('limit') || '20'}
                                        style={{
                                            padding: '4px 8px',
                                            borderRadius: 6,
                                            border: '1px solid #d9d9d9',
                                            fontSize: 12,
                                        }}
                                    >
                                        <option value="20">20 条</option>
                                        <option value="50">50 条</option>
                                        <option value="100">100 条</option>
                                    </select>

                                    <Button
                                        type="link"
                                        size="small"
                                        icon={<ClearOutlined />}
                                        onClick={() => {
                                            setFormError(null);
                                            setParams({});
                                        }}
                                    >
                                        重置清空条件
                                    </Button>
                                </Space>

                                <Text type="secondary" style={{ fontSize: 12 }}>
                                    按毫秒时间轴精准对齐检索
                                </Text>
                            </div>
                        </div>
                    ) : null}
                </Card>
            </form>

            <ErrorNotice error={formError || validationError || result.error} />

            {values.length >= Number(params.get('limit') || 20) ? (
                <div className="alignment-limit-note">
                    当前返回 {values.length}{' '}
                    条时间轴单元，已达到显示上限；长视频可能只覆盖部分区间，请缩小时间范围或提高显示上限后再读取时间轴。
                </div>
            ) : null}

            {/* 列表控制栏：视图切换与聚合统计 */}
            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: 16,
                    padding: '0 4px',
                    flexWrap: 'wrap',
                    gap: 12,
                }}
            >
                <Space size={12} align="center">
                    <div
                        style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: 8,
                            background: '#f8fafc',
                            padding: '4px 12px',
                            borderRadius: 6,
                            border: '1px solid #e2e8f0',
                        }}
                    >
                        <VideoCameraOutlined style={{ color: '#1668dc' }} />
                        <Text strong style={{ fontSize: 13, color: '#0f172a' }}>
                            {videoGroups.length} 部视频母带
                        </Text>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                            (共 {values.length} 条时间轴切片)
                        </Text>
                    </div>

                    {viewMode === 'timeline' && videoGroups.length > 0 ? (
                        <Space size={8}>
                            <Button size="small" type="text" onClick={expandAll}>
                                全部展开
                            </Button>
                            <Button size="small" type="text" onClick={collapseAll}>
                                全部收起
                            </Button>
                        </Space>
                    ) : null}
                </Space>

                <Space size={12} align="center">
                    <Segmented
                        value={viewMode}
                        onChange={(v) => setViewMode(v as 'timeline' | 'grid')}
                        options={[
                            {
                                label: (
                                    <Space size={4}>
                                        <BarsOutlined />
                                        <span>多模态时间轴</span>
                                    </Space>
                                ),
                                value: 'timeline',
                            },
                            {
                                label: (
                                    <Space size={4}>
                                        <AppstoreOutlined />
                                        <span>全量切片平铺</span>
                                    </Space>
                                ),
                                value: 'grid',
                            },
                        ]}
                    />
                </Space>
            </div>

            {request && result.isPending ? (
                <Loading tip="正在检索素材单元…" />
            ) : !validationError && !result.error && result.data ? (
                values.length ? (
                    viewMode === 'timeline' ? (
                        /* ── 按视频母带分组的时间线视图 ────────────────────────────────────────── */
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
                            {videoGroups.map((group) => {
                                const expanded = isStreamExpanded(group.streamId);
                                return (
                                    <Card
                                        key={group.streamId}
                                        style={{
                                            borderRadius: 12,
                                            borderColor: '#cbd5e1',
                                            boxShadow: '0 1px 3px 0 rgba(0, 0, 0, 0.05)',
                                            overflow: 'hidden',
                                        }}
                                        bodyStyle={{ padding: 0 }}
                                    >
                                        {/* 视频母带头部概要卡片 */}
                                        <div
                                            style={{
                                                padding: '16px 20px',
                                                background: '#f8fafc',
                                                borderBottom: expanded
                                                    ? '1px solid #e2e8f0'
                                                    : 'none',
                                                display: 'flex',
                                                justifyContent: 'space-between',
                                                alignItems: 'center',
                                                cursor: 'pointer',
                                                transition: 'background 0.2s',
                                            }}
                                            onClick={() => toggleStream(group.streamId)}
                                        >
                                            <Space align="center" size={14}>
                                                <div
                                                    style={{
                                                        width: 44,
                                                        height: 44,
                                                        borderRadius: 10,
                                                        background: '#eff6ff',
                                                        border: '1px solid #bfdbfe',
                                                        color: '#1d4ed8',
                                                        display: 'grid',
                                                        placeItems: 'center',
                                                        fontSize: 22,
                                                        flexShrink: 0,
                                                    }}
                                                >
                                                    <VideoCameraOutlined />
                                                </div>
                                                <div>
                                                    <div
                                                        style={{
                                                            display: 'flex',
                                                            alignItems: 'center',
                                                            gap: 8,
                                                        }}
                                                    >
                                                        <Text
                                                            strong
                                                            style={{
                                                                fontSize: 15,
                                                                color: '#0f172a',
                                                            }}
                                                        >
                                                            {group.title}
                                                        </Text>
                                                        {group.asset ? (
                                                            <Tag
                                                                color="geekblue"
                                                                style={{ margin: 0, fontSize: 11 }}
                                                            >
                                                                {group.asset.content_type}
                                                            </Tag>
                                                        ) : null}
                                                    </div>
                                                    <Space size={12} style={{ marginTop: 4 }} wrap>
                                                        <span
                                                            className="mono"
                                                            style={{
                                                                fontSize: 11,
                                                                color: '#64748b',
                                                            }}
                                                        >
                                                            ID: {group.streamId}
                                                        </span>
                                                        {group.asset ? (
                                                            <Text
                                                                type="secondary"
                                                                style={{ fontSize: 11 }}
                                                            >
                                                                大小:{' '}
                                                                {bytes(group.asset.size_bytes)}
                                                            </Text>
                                                        ) : null}
                                                        <Space
                                                            size={4}
                                                            style={{
                                                                color: '#059669',
                                                                fontSize: 11,
                                                            }}
                                                        >
                                                            <ClockCircleOutlined />
                                                            <span>
                                                                覆盖区间:{' '}
                                                                {formatTime(
                                                                    group.startMs.toString(),
                                                                )}{' '}
                                                                ~{' '}
                                                                {formatTime(group.endMs.toString())}
                                                            </span>
                                                        </Space>
                                                        <Tag
                                                            color="blue"
                                                            style={{ margin: 0, fontSize: 11 }}
                                                        >
                                                            {group.materials.length} 个时间轴单元
                                                        </Tag>
                                                    </Space>
                                                </div>
                                            </Space>

                                            <Space size={12} onClick={(e) => e.stopPropagation()}>
                                                {group.asset ? (
                                                    <Button
                                                        size="small"
                                                        icon={<PlayCircleOutlined />}
                                                        onClick={() =>
                                                            setPreviewAsset(
                                                                group.asset
                                                                    ? { asset: group.asset }
                                                                    : null,
                                                            )
                                                        }
                                                    >
                                                        原片预览
                                                    </Button>
                                                ) : null}
                                                <Button
                                                    size="small"
                                                    type="text"
                                                    icon={
                                                        expanded ? <UpOutlined /> : <DownOutlined />
                                                    }
                                                    onClick={() => toggleStream(group.streamId)}
                                                >
                                                    {expanded ? '收起时间线' : '展开时间线'}
                                                </Button>
                                            </Space>
                                        </div>

                                        {/* 时间线列表展示 */}
                                        {expanded ? (
                                            <>
                                                {/* 多模态时间轴对齐：观测事实与素材单元共用同一条毫秒时间轴 */}
                                                <AlignmentTimeline
                                                    materials={group.materials}
                                                    startMs={group.startMs}
                                                    endMs={group.endMs}
                                                    detailHref={(materialUnitId) =>
                                                        `/materials/${encodeURIComponent(materialUnitId)}?${searchParamsOnly(params)}`
                                                    }
                                                    onLocate={(ms) =>
                                                        setPreviewAsset(
                                                            group.asset
                                                                ? { asset: group.asset, seekMs: ms }
                                                                : null,
                                                        )
                                                    }
                                                />

                                                <div className="alignment-details-toggle">
                                                    <Button
                                                        type="text"
                                                        size="small"
                                                        icon={
                                                            detailsOpen[group.streamId] ? (
                                                                <UpOutlined />
                                                            ) : (
                                                                <DownOutlined />
                                                            )
                                                        }
                                                        onClick={() =>
                                                            setDetailsOpen((prev) => ({
                                                                ...prev,
                                                                [group.streamId]:
                                                                    !prev[group.streamId],
                                                            }))
                                                        }
                                                    >
                                                        {detailsOpen[group.streamId]
                                                            ? '收起切片明细'
                                                            : `展开 ${group.materials.length} 条切片明细`}
                                                    </Button>
                                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                                        时间轴对齐已在面板中呈现，逐条切片明细默认省略。
                                                    </Text>
                                                </div>

                                                {detailsOpen[group.streamId] ? (
                                                    <div
                                                        style={{
                                                            padding: '0 24px 8px 24px',
                                                            background: '#ffffff',
                                                        }}
                                                    >
                                                        <Timeline
                                                            mode="left"
                                                            items={group.materials.map(
                                                                (item, idx) => {
                                                                    const startStr = formatTime(
                                                                        item.time_range?.start_ms,
                                                                    );
                                                                    const endStr = formatTime(
                                                                        item.time_range?.end_ms,
                                                                    );
                                                                    const textExcerpt =
                                                                        item.observations
                                                                            .map((o) =>
                                                                                payloadText(
                                                                                    o.payload,
                                                                                    400,
                                                                                ),
                                                                            )
                                                                            .filter(Boolean)
                                                                            .join(' ') || '';

                                                                    return {
                                                                        color:
                                                                            item.status ===
                                                                                'fast_ready' ||
                                                                            item.status ===
                                                                                'enriched'
                                                                                ? '#10b981'
                                                                                : '#1668dc',
                                                                        label: (
                                                                            <div
                                                                                style={{
                                                                                    paddingRight: 16,
                                                                                }}
                                                                            >
                                                                                <div
                                                                                    className="mono"
                                                                                    style={{
                                                                                        fontSize: 12,
                                                                                        fontWeight: 600,
                                                                                        color: '#0f172a',
                                                                                    }}
                                                                                >
                                                                                    {startStr}
                                                                                </div>
                                                                                <div
                                                                                    className="mono"
                                                                                    style={{
                                                                                        fontSize: 11,
                                                                                        color: '#64748b',
                                                                                    }}
                                                                                >
                                                                                    至 {endStr}
                                                                                </div>
                                                                                <Tag
                                                                                    style={{
                                                                                        marginTop: 4,
                                                                                        marginRight: 0,
                                                                                        fontSize: 10,
                                                                                        background:
                                                                                            '#f1f5f9',
                                                                                        color: '#475569',
                                                                                        border: 'none',
                                                                                    }}
                                                                                >
                                                                                    #{idx + 1}
                                                                                </Tag>
                                                                            </div>
                                                                        ),
                                                                        children: (
                                                                            <Card
                                                                                size="small"
                                                                                style={{
                                                                                    borderRadius: 8,
                                                                                    borderColor:
                                                                                        '#e2e8f0',
                                                                                    background:
                                                                                        '#fafcfc',
                                                                                    marginBottom: 16,
                                                                                }}
                                                                                bodyStyle={{
                                                                                    padding: 14,
                                                                                }}
                                                                            >
                                                                                <div
                                                                                    style={{
                                                                                        display:
                                                                                            'flex',
                                                                                        justifyContent:
                                                                                            'space-between',
                                                                                        alignItems:
                                                                                            'center',
                                                                                        marginBottom: 8,
                                                                                        flexWrap:
                                                                                            'wrap',
                                                                                        gap: 6,
                                                                                    }}
                                                                                >
                                                                                    <Space size={8}>
                                                                                        <Tag
                                                                                            color={
                                                                                                statusTagColor[
                                                                                                    item
                                                                                                        .status
                                                                                                ] ||
                                                                                                'default'
                                                                                            }
                                                                                            style={{
                                                                                                margin: 0,
                                                                                                fontWeight: 500,
                                                                                            }}
                                                                                        >
                                                                                            {statusNames[
                                                                                                item
                                                                                                    .status
                                                                                            ] ||
                                                                                                item.status}
                                                                                        </Tag>
                                                                                        <Tag
                                                                                            color="purple"
                                                                                            style={{
                                                                                                margin: 0,
                                                                                                fontSize: 10,
                                                                                            }}
                                                                                        >
                                                                                            v
                                                                                            {
                                                                                                item.revision
                                                                                            }
                                                                                        </Tag>
                                                                                        {[
                                                                                            ...new Set(
                                                                                                item.observations.map(
                                                                                                    (
                                                                                                        o,
                                                                                                    ) =>
                                                                                                        o.modality,
                                                                                                ),
                                                                                            ),
                                                                                        ].map(
                                                                                            (m) => (
                                                                                                <Tag
                                                                                                    key={
                                                                                                        m
                                                                                                    }
                                                                                                    color="cyan"
                                                                                                    style={{
                                                                                                        fontSize: 10,
                                                                                                        margin: 0,
                                                                                                        borderRadius: 4,
                                                                                                    }}
                                                                                                >
                                                                                                    {modalityNames[
                                                                                                        m
                                                                                                    ] ||
                                                                                                        m}
                                                                                                </Tag>
                                                                                            ),
                                                                                        )}
                                                                                    </Space>

                                                                                    <Link
                                                                                        to={`/materials/${encodeURIComponent(item.material_unit_id)}?${searchParamsOnly(params)}`}
                                                                                    >
                                                                                        <Button
                                                                                            type="link"
                                                                                            size="small"
                                                                                            icon={
                                                                                                <ArrowRightOutlined />
                                                                                            }
                                                                                            style={{
                                                                                                padding: 0,
                                                                                            }}
                                                                                        >
                                                                                            切片回放与详情
                                                                                        </Button>
                                                                                    </Link>
                                                                                </div>

                                                                                <div
                                                                                    style={{
                                                                                        background:
                                                                                            '#ffffff',
                                                                                        padding:
                                                                                            '10px 12px',
                                                                                        borderRadius: 6,
                                                                                        border: '1px solid #f1f5f9',
                                                                                        color: textExcerpt
                                                                                            ? '#1e293b'
                                                                                            : '#94a3b8',
                                                                                        fontSize: 13,
                                                                                        lineHeight: 1.6,
                                                                                    }}
                                                                                >
                                                                                    {textExcerpt ||
                                                                                        '（该时间段未提取到文字观测事实）'}
                                                                                </div>

                                                                                {item.tags
                                                                                    .length ? (
                                                                                    <div
                                                                                        style={{
                                                                                            marginTop: 8,
                                                                                        }}
                                                                                    >
                                                                                        <Space
                                                                                            size={[
                                                                                                4,
                                                                                                4,
                                                                                            ]}
                                                                                            wrap
                                                                                        >
                                                                                            {item.tags.map(
                                                                                                (
                                                                                                    tag,
                                                                                                ) => (
                                                                                                    <Tag
                                                                                                        key={
                                                                                                            tag
                                                                                                        }
                                                                                                        style={{
                                                                                                            fontSize: 10,
                                                                                                            margin: 0,
                                                                                                            background:
                                                                                                                '#f8fafc',
                                                                                                        }}
                                                                                                    >
                                                                                                        #
                                                                                                        {
                                                                                                            tag
                                                                                                        }
                                                                                                    </Tag>
                                                                                                ),
                                                                                            )}
                                                                                        </Space>
                                                                                    </div>
                                                                                ) : null}
                                                                            </Card>
                                                                        ),
                                                                    };
                                                                },
                                                            )}
                                                        />
                                                    </div>
                                                ) : null}
                                            </>
                                        ) : null}
                                    </Card>
                                );
                            })}
                        </div>
                    ) : (
                        /* ── 全量切片平铺卡片视图 ────────────────────────────────────────── */
                        <Row gutter={[16, 16]}>
                            {values.map((item) => (
                                <Col
                                    xs={24}
                                    md={12}
                                    lg={8}
                                    key={`${item.material_unit_id}:${item.revision}`}
                                >
                                    <Link
                                        to={`/materials/${encodeURIComponent(item.material_unit_id)}?${searchParamsOnly(params)}`}
                                        style={{ display: 'block', height: '100%' }}
                                    >
                                        <Card
                                            hoverable
                                            style={{
                                                height: '100%',
                                                display: 'flex',
                                                flexDirection: 'column',
                                                borderRadius: 10,
                                                borderColor: '#e2e8f0',
                                            }}
                                            bodyStyle={{
                                                padding: 16,
                                                flex: 1,
                                                display: 'flex',
                                                flexDirection: 'column',
                                            }}
                                        >
                                            <div
                                                style={{
                                                    display: 'flex',
                                                    justifyContent: 'space-between',
                                                    alignItems: 'center',
                                                    marginBottom: 10,
                                                }}
                                            >
                                                <Tag
                                                    color={statusTagColor[item.status] || 'default'}
                                                    style={{ margin: 0, fontWeight: 500 }}
                                                >
                                                    {statusNames[item.status] || item.status}
                                                </Tag>
                                                <ArrowRightOutlined
                                                    style={{ color: '#94a3b8', fontSize: 12 }}
                                                />
                                            </div>

                                            <div
                                                style={{
                                                    fontSize: 13,
                                                    lineHeight: 1.6,
                                                    color: '#1e293b',
                                                    marginBottom: 12,
                                                    flex: 1,
                                                    display: '-webkit-box',
                                                    WebkitLineClamp: 3,
                                                    WebkitBoxOrient: 'vertical',
                                                    overflow: 'hidden',
                                                }}
                                            >
                                                {item.observations
                                                    .map((o) => payloadText(o.payload, 400))
                                                    .filter(Boolean)
                                                    .join(' ') ||
                                                    '暂无文本摘要，可点击查看多模态向量特征与原始时间轴。'}
                                            </div>

                                            <div style={{ marginBottom: 10 }}>
                                                <Space size={[4, 4]} wrap>
                                                    {[
                                                        ...new Set(
                                                            item.observations.map(
                                                                (o) => o.modality,
                                                            ),
                                                        ),
                                                    ].map((m) => (
                                                        <Tag
                                                            key={m}
                                                            color="cyan"
                                                            style={{
                                                                fontSize: 11,
                                                                margin: 0,
                                                                borderRadius: 4,
                                                            }}
                                                        >
                                                            {modalityNames[m] || m}
                                                        </Tag>
                                                    ))}
                                                </Space>
                                            </div>

                                            {item.tags.length ? (
                                                <div style={{ marginBottom: 10 }}>
                                                    <Space size={[4, 4]} wrap>
                                                        {item.tags.map((tag) => (
                                                            <Tag
                                                                key={tag}
                                                                style={{
                                                                    fontSize: 11,
                                                                    margin: 0,
                                                                    background: '#f8fafc',
                                                                }}
                                                            >
                                                                #{tag}
                                                            </Tag>
                                                        ))}
                                                    </Space>
                                                </div>
                                            ) : null}

                                            <div
                                                style={{
                                                    marginTop: 'auto',
                                                    paddingTop: 10,
                                                    borderTop: '1px solid #f1f5f9',
                                                    display: 'flex',
                                                    justifyContent: 'space-between',
                                                    alignItems: 'center',
                                                    fontSize: 12,
                                                }}
                                            >
                                                <Space size={4} style={{ color: '#059669' }}>
                                                    <ClockCircleOutlined style={{ fontSize: 12 }} />
                                                    <span className="mono" style={{ fontSize: 11 }}>
                                                        {formatTime(item.time_range?.start_ms)} ~{' '}
                                                        {formatTime(item.time_range?.end_ms)}
                                                    </span>
                                                </Space>
                                                <Tag
                                                    color="purple"
                                                    style={{ margin: 0, fontSize: 11 }}
                                                >
                                                    v{item.revision}
                                                </Tag>
                                            </div>
                                        </Card>
                                    </Link>
                                </Col>
                            ))}
                        </Row>
                    )
                ) : (
                    <Card>
                        <Empty
                            title={params.toString() ? '未找到符合条件的素材' : '素材库暂无数据'}
                        >
                            {params.toString()
                                ? '可尝试放宽时间范围、降低置信度阈值或更换关键词。'
                                : '这里展示已完成推理与切片的素材单元。请在处理任务中下发视频流水线。'}
                        </Empty>
                    </Card>
                )
            ) : null}

            {/* 视频母带全量原片预览弹窗 */}
            {previewAsset ? (
                <Modal
                    title={`原片预览 · ${previewAsset.asset.filename}${
                        previewAsset.seekMs == null
                            ? ''
                            : ` · 时间轴定位 ${formatTime(String(previewAsset.seekMs))}`
                    }`}
                    onClose={() => setPreviewAsset(null)}
                    width={760}
                >
                    <div className="video-preview-box" style={{ marginBottom: 16 }}>
                        <video
                            key={`${previewAsset.asset.id}-${previewAsset.seekMs ?? 0}`}
                            ref={previewRef}
                            controls
                            preload="metadata"
                            src={`/v1/assets/${previewAsset.asset.id}/content`}
                            onLoadedMetadata={() => {
                                // 时间轴游标定位：落在素材单元窗口内即为该切片回放起点。
                                if (previewAsset.seekMs != null && previewRef.current) {
                                    previewRef.current.currentTime = previewAsset.seekMs / 1000;
                                }
                            }}
                            style={{ width: '100%', maxHeight: 480, background: '#000' }}
                        />
                    </div>
                    <div
                        style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            gap: 12,
                        }}
                    >
                        <Text type="secondary" style={{ fontSize: 12 }}>
                            {previewAsset.seekMs == null
                                ? '原片回放用于核对时间轴对齐结果。'
                                : '已按时间轴游标定位到对应原片时刻，用于核对对齐结果。'}
                        </Text>
                        <Button type="primary" onClick={() => setPreviewAsset(null)}>
                            关闭
                        </Button>
                    </div>
                </Modal>
            ) : null}
        </div>
    );
}
