import { useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Alert,
    Button,
    Card,
    Col,
    Drawer,
    Input,
    Row,
    Segmented,
    Space,
    Tag,
    Typography,
} from 'antd';
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
    AimOutlined,
    UnorderedListOutlined,
    InfoCircleOutlined,
    ExclamationCircleOutlined,
    LoadingOutlined,
} from '@ant-design/icons';
import { Link, useSearchParams } from 'react-router-dom';
import { api, RequestError } from '../api/client';
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
import ExecutionSeconds, { type ExecutionTimeline } from './ExecutionSeconds';
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

function isIndexUnavailableError(error: unknown): boolean {
    if (!error) return false;
    if (error instanceof RequestError) {
        return (
            [
                'semantic_search_unavailable',
                'semantic_index_unreachable',
                'semantic_index_unauthenticated',
                'semantic_index_protocol_error',
                'vector_store_locked',
                'vector_store_not_ready',
                'query_encoder_failed',
                'dimension_mismatch',
                'vector_index_key_mismatch',
            ].includes(error.reason) ||
            error.status === 502 ||
            error.status === 503
        );
    }
    return false;
}

export default function Materials() {
    const [params, setParams] = useSearchParams();
    const queryClient = useQueryClient();
    const executionId = params.get('execution') || '';
    const activeMode = params.get('mode') === 'semantic' ? 'semantic' : 'keyword';
    const [showConflictModal, setShowConflictModal] = useState(false);
    const secondPage = Math.max(1, Math.min(72, Math.floor(Number(params.get('page')) || 1)));
    const fullExecution =
        !!executionId &&
        !['q', 'stream', 'start', 'end', 'tags', 'modalities', 'confidence'].some((key) =>
            params.get(key),
        );

    const hasConflictingFilters = useMemo(() => {
        return ['stream', 'start', 'end', 'confidence', 'modalities', 'tags', 'execution'].some(
            (key) => Boolean(params.get(key)),
        );
    }, [params]);

    const handleModeChange = (targetMode: 'keyword' | 'semantic') => {
        if (targetMode === activeMode) return;
        if (targetMode === 'semantic') {
            if (hasConflictingFilters) {
                setShowConflictModal(true);
                return;
            }
            const next = new URLSearchParams(params);
            next.set('mode', 'semantic');
            setFormError(null);
            setParams(next);
        } else {
            const next = new URLSearchParams(params);
            next.delete('mode');
            setFormError(null);
            setParams(next);
        }
    };

    const handleConfirmClearAndSwitch = () => {
        const next = new URLSearchParams();
        const q = params.get('q');
        if (q) next.set('q', q);
        const limit = params.get('limit');
        if (limit) next.set('limit', limit);
        next.set('mode', 'semantic');
        setShowConflictModal(false);
        setFormError(null);
        setParams(next);
    };
    const timeline = useQuery({
        queryKey: ['execution-timeline', executionId],
        queryFn: ({ signal }) =>
            api<ExecutionTimeline>(`/v1/executions/${encodeURIComponent(executionId)}/timeline`, {
                signal,
            }),
        enabled: fullExecution,
        refetchInterval: 10000,
    });
    const completeSeconds = useMutation({
        mutationFn: () =>
            api(`/v1/executions/${encodeURIComponent(executionId)}:segment-seconds`, {
                method: 'POST',
            }),
        onSuccess: () => {
            void queryClient.invalidateQueries({ queryKey: ['execution-timeline'] });
            void queryClient.invalidateQueries({ queryKey: ['materials'] });
        },
    });
    const [formError, setFormError] = useState<Error | null>(null);
    const [viewMode, setViewMode] = useState<'timeline' | 'grid'>('timeline');
    const [expandedStreams, setExpandedStreams] = useState<Record<string, boolean>>({});
    const [viewingSliceGroup, setViewingSliceGroup] = useState<VideoGroup | null>(null);
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

    const query = params.get('q') || '';
    const shouldFetch = fullExecution
        ? true
        : activeMode === 'semantic'
          ? Boolean(request && query.trim())
          : Boolean(request);

    const result = useQuery({
        queryKey: ['materials', request, fullExecution ? secondPage : 0],
        queryFn: ({ signal }) =>
            fullExecution
                ? api<SearchResponse>(
                      `/v1/executions/${encodeURIComponent(executionId)}/materials?offset=${(secondPage - 1) * 100}&limit=100`,
                      { signal },
                  )
                : api<SearchResponse>('/v1/materials:search', {
                      method: 'POST',
                      body: JSON.stringify(request),
                      signal,
                  }),
        enabled: shouldFetch,
        retry: false,
        refetchInterval: fullExecution ? 10000 : false,
    });

    const values = result.data?.materials || [];

    const semanticHits = useMemo(() => {
        if (result.data?.mode !== 'semantic' || !result.data.hits) return [];
        const hits = result.data.hits;
        const mats = result.data.materials || [];
        return hits
            .map((h, i) => ({
                hit: h,
                material: mats[i] || mats.find((m) => m.material_unit_id === h.material_unit_id),
                index: i,
            }))
            .filter((item): item is { hit: typeof item.hit; material: MaterialUnit; index: number } =>
                Boolean(item.material),
            );
    }, [result.data]);

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
        if (activeMode === 'semantic') {
            next.set('mode', 'semantic');
            for (const f of [
                'stream',
                'start',
                'end',
                'confidence',
                'modalities',
                'tags',
                'execution',
            ]) {
                next.delete(f);
            }
            const q = next.get('q') || '';
            if (!q.trim()) {
                setFormError(
                    new Error(
                        '语义检索请输入自然语言描述内容（例如“有人正在黑板前写字”、“红色轿车开过”、“系统报错提示”）。',
                    ),
                );
                return;
            }
        } else {
            next.delete('mode');
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
                        onClick={() => {
                            void result.refetch();
                            if (fullExecution) void timeline.refetch();
                        }}
                    >
                        刷新结果
                    </Button>
                }
            />

            <form key={params.toString()} onSubmit={handleSearchSubmit}>
                <Card style={{ marginBottom: 10 }} bodyStyle={{ padding: 10 }}>
                    {/* 关键词 / 语义双模式切换栏 */}
                    <div
                        style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            marginBottom: 12,
                            flexWrap: 'wrap',
                            gap: 10,
                        }}
                    >
                        <Space size={12} align="center">
                            <Segmented
                                value={activeMode}
                                onChange={(v) => handleModeChange(v as 'keyword' | 'semantic')}
                                options={[
                                    { label: '关键词检索 (字面)', value: 'keyword' },
                                    { label: '语义检索 (自然语言/向量)', value: 'semantic' },
                                ]}
                            />
                            {activeMode === 'semantic' ? (
                                <Tag color="blue" style={{ margin: 0 }}>
                                    全局跨视频向量特征索引
                                </Tag>
                            ) : (
                                <Tag color="default" style={{ margin: 0 }}>
                                    时间节点文字/语音精确匹配
                                </Tag>
                            )}
                        </Space>
                        {activeMode === 'semantic' ? (
                            <span style={{ fontSize: 12, color: '#64748b' }}>
                                支持用自然语言直接定位场景、台词或视觉特征；前置标量筛选已禁用
                            </span>
                        ) : null}
                    </div>

                    {activeMode === 'semantic' ? (
                        <div
                            style={{
                                marginBottom: 12,
                                padding: '8px 12px',
                                background: '#eff6ff',
                                border: '1px solid #bfdbfe',
                                borderRadius: 6,
                                fontSize: 12,
                                color: '#1e40af',
                                display: 'flex',
                                alignItems: 'center',
                                gap: 8,
                            }}
                        >
                            <InfoCircleOutlined />
                            <span>
                                语义检索说明：通过自然语言描述直接查找多模态视频片段与观测。后端语义检索基于全局向量空间检索，暂不支持单视频、任务批次或起止时间预筛选。
                            </span>
                        </div>
                    ) : null}

                    <div
                        style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}
                    >
                        <div className={`material-video-picker ${activeMode === 'semantic' ? 'semantic-filter-disabled' : ''}`}>
                            <VideoCameraOutlined />
                            <select
                                name="stream"
                                aria-label="视频母带"
                                defaultValue={activeMode === 'semantic' ? '' : (params.get('stream') || '')}
                                disabled={activeMode === 'semantic'}
                            >
                                <option value="">
                                    {activeMode === 'semantic'
                                        ? '全部视频母带（语义模式下不支持单视频预过滤）'
                                        : '全部视频母带'}
                                </option>
                                {activeMode !== 'semantic'
                                    ? videoOptions.map((option) => (
                                          <option key={option.streamId} value={option.streamId}>
                                              {option.label}
                                          </option>
                                      ))
                                    : null}
                            </select>
                        </div>
                        <Input
                            prefix={<SearchOutlined style={{ color: '#1668dc', fontSize: 16 }} />}
                            name="q"
                            aria-label="素材关键词"
                            placeholder={
                                activeMode === 'semantic'
                                    ? '输入自然语言描述寻找视频片段（例如：“有人正在黑板前写字”、“红色轿车开过”、“系统报错提示”）…'
                                    : '输入关键词检索画面文字 (OCR)、语音转写、视觉描述或语义意图…'
                            }
                            defaultValue={params.get('q') || ''}
                            maxLength={2000}
                            allowClear
                            style={{ flex: 1 }}
                        />
                        <Button
                            type="primary"
                            htmlType="submit"
                            loading={result.isFetching}
                            style={{ minWidth: 100 }}
                        >
                            {activeMode === 'semantic' ? '语义检索' : '检索素材'}
                        </Button>
                        <Button
                            icon={<FilterOutlined />}
                            disabled={activeMode === 'semantic'}
                            type={advancedOpen && activeMode !== 'semantic' ? 'dashed' : 'default'}
                            onClick={() => activeMode !== 'semantic' && setAdvancedOpen(!advancedOpen)}
                        >
                            {activeMode === 'semantic' ? '高级筛选（已禁用）' : '高级筛选'}
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
                            <Row gutter={[8, 8]}>
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
                                            执行批次（从任务详情跳转时自动带入）
                                        </Text>
                                        <Input
                                            name="execution"
                                            placeholder="例如 execution-…"
                                            defaultValue={params.get('execution') || ''}
                                            maxLength={128}
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
                                            setParams(activeMode === 'semantic' ? { mode: 'semantic' } : {});
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

            {/* 筛选冲突拦截弹窗 */}
            {showConflictModal ? (
                <Modal
                    title="切换至语义检索"
                    onClose={() => setShowConflictModal(false)}
                    width={520}
                >
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                        <Alert
                            type="warning"
                            showIcon
                            message="后端语义检索限制提示"
                            description="后端语义检索基于全局跨视频向量索引，暂不支持指定视频母带、执行批次、起止时间或置信度等前置筛选条件。切换将清空上述筛选条件以发起全局语义检索。"
                        />
                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 8 }}>
                            <Button onClick={() => setShowConflictModal(false)}>
                                取消（保留当前关键词筛选）
                            </Button>
                            <Button type="primary" onClick={handleConfirmClearAndSwitch}>
                                确认清空并切换为语义检索
                            </Button>
                        </div>
                    </div>
                </Modal>
            ) : null}

            {/* 索引不可用专属警示卡片 */}
            {activeMode === 'semantic' && isIndexUnavailableError(result.error) ? (
                <Card style={{ marginBottom: 16, borderColor: '#fca5a5', background: '#fff5f5' }}>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                        <Space size={8} align="center">
                            <ExclamationCircleOutlined style={{ color: '#ef4444', fontSize: 18 }} />
                            <Text strong style={{ fontSize: 15, color: '#991b1b' }}>
                                向量语义检索服务不可用
                            </Text>
                            <Tag color="error">
                                {result.error instanceof RequestError ? result.error.reason : 'error'}
                            </Tag>
                        </Space>
                        <Text style={{ fontSize: 13, color: '#7f1d1d' }}>
                            {result.error instanceof Error ? result.error.message : '向量检索服务暂时不可用。'}
                            {' '}在本地或开发环境中，请确认已通过 <code>./deploy/up-events.sh</code> 启动常驻事件与检索服务（index:50077）。
                        </Text>
                        <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                            <Button
                                type="primary"
                                onClick={() => {
                                    const next = new URLSearchParams(params);
                                    next.delete('mode');
                                    setParams(next);
                                }}
                            >
                                一键切换至关键词检索
                            </Button>
                            <Button onClick={() => void result.refetch()}>重试连接</Button>
                        </div>
                    </div>
                </Card>
            ) : (
                <ErrorNotice
                    error={
                        formError ||
                        validationError ||
                        result.error ||
                        timeline.error ||
                        completeSeconds.error
                    }
                />
            )}
            {fullExecution && timeline.data ? (
                <ExecutionSeconds
                    timeline={timeline.data}
                    page={secondPage}
                    onPage={(page) => {
                        const next = new URLSearchParams(params);
                        next.set('page', String(page));
                        setParams(next);
                    }}
                    onComplete={() => completeSeconds.mutate()}
                    completing={completeSeconds.isPending}
                />
            ) : null}

            {/* 列表控制栏：视图切换与聚合统计 */}
            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: 10,
                    padding: '0 4px',
                    flexWrap: 'wrap',
                    gap: 8,
                }}
            >
                {activeMode === 'semantic' ? (
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
                            <AimOutlined style={{ color: '#1668dc' }} />
                            <Text strong style={{ fontSize: 13, color: '#0f172a' }}>
                                语义相关性结果流
                            </Text>
                            <Text type="secondary" style={{ fontSize: 12 }}>
                                (共 {semanticHits.length} 条命中片段)
                            </Text>
                        </div>
                        <Tag color="purple" style={{ margin: 0, fontSize: 11 }}>
                            向量相关性降序排序 · 相似度非置信度
                        </Tag>
                    </Space>
                ) : (
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
                                (
                                {fullExecution
                                    ? `本页 ${values.length} 条 / 共 ${timeline.data?.material_references.length ?? 0} 条逐秒切片`
                                    : `共 ${values.length} 条时间轴切片`}
                                )
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
                )}

                {activeMode !== 'semantic' ? (
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
                                            <span>切片卡片</span>
                                        </Space>
                                    ),
                                    value: 'grid',
                                },
                            ]}
                        />
                    </Space>
                ) : (
                    <span style={{ fontSize: 12, color: '#64748b' }}>
                        点击「定位回看」可在原片中精确跳转至该证据毫秒时间点
                    </span>
                )}
            </div>

            {request && result.isPending ? (
                <Loading tip="正在检索素材单元…" />
            ) : !validationError && !result.error && result.data ? (
                <>
                    {/* 慢路径补全或未索引就绪提示 */}
                    {result.data.unindexed_hits > 0 && values.length > 0 ? (
                        <Alert
                            type="info"
                            showIcon
                            style={{ marginBottom: 12, borderRadius: 6 }}
                            message={
                                <span>
                                    提示：检索结果中包含部分处理中的切片（<strong>{result.data.unindexed_hits}</strong> 条潜在命中仍在索引构建或慢路径融合中，已就绪的素材可直接回看）。
                                </span>
                            }
                        />
                    ) : null}

                    {values.length ? (
                        activeMode === 'semantic' ? (
                            /* ── 语义相关性结果流（严格保序，不重排为时间轴） ─────────────── */
                            <div className="semantic-results-container">
                                {semanticHits.map(({ hit, material, index }) => {
                                    const asset = findAssetForStream(material.stream_id, assetsListing.data?.items);
                                    const targetObs = material.observations.find((o) => o.observation_id === hit.observation_id) || material.observations[0];
                                    const range = targetObs?.time_range || material.time_range;
                                    const startStr = formatTime(range?.start_ms);
                                    const endStr = formatTime(range?.end_ms);
                                    const durationSec = range?.start_ms && range?.end_ms
                                        ? ((Number(range.end_ms) - Number(range.start_ms)) / 1000).toFixed(1)
                                        : '1.0';
                                    const textExcerpt = targetObs ? payloadText(targetObs.payload, 600) : '';

                                    return (
                                        <Card
                                            key={`${hit.material_unit_id}:${hit.revision}:${hit.embedding_id || index}`}
                                            className="semantic-hit-card"
                                            bodyStyle={{ padding: 16 }}
                                        >
                                            {/* 头部：排名、来源视频母带、相关度指标、状态 */}
                                            <div
                                                style={{
                                                    display: 'flex',
                                                    justifyContent: 'space-between',
                                                    alignItems: 'center',
                                                    flexWrap: 'wrap',
                                                    gap: 10,
                                                    marginBottom: 12,
                                                }}
                                            >
                                                <Space size={10} align="center">
                                                    <Tag
                                                        color={index === 0 ? 'gold' : index < 3 ? 'blue' : 'default'}
                                                        style={{ fontWeight: 700, fontSize: 12, margin: 0, padding: '2px 8px' }}
                                                    >
                                                        #{index + 1} {index === 0 ? '最佳匹配' : '语义命中'}
                                                    </Tag>
                                                    <Space size={6} align="center">
                                                        <VideoCameraOutlined style={{ color: '#1668dc' }} />
                                                        <Text strong style={{ fontSize: 14, color: '#0f172a' }}>
                                                            {asset?.filename || `视频母带 (${material.stream_id})`}
                                                        </Text>
                                                        {asset ? (
                                                            <Tag color="geekblue" style={{ margin: 0, fontSize: 11 }}>
                                                                {asset.content_type}
                                                            </Tag>
                                                        ) : null}
                                                    </Space>
                                                </Space>

                                                <Space size={8} align="center">
                                                    {/* 严格标注为语义相关度 / 向量距离，杜绝标注为置信度 */}
                                                    <Tag color="purple" style={{ margin: 0, fontWeight: 600, fontSize: 12 }}>
                                                        语义相关度 {(Math.max(0, 1 - hit.distance) * 100).toFixed(1)}% (距离 {hit.distance.toFixed(4)})
                                                    </Tag>
                                                    <Tag color={statusTagColor[material.status] || 'default'} style={{ margin: 0 }}>
                                                        {statusNames[material.status] || material.status}
                                                    </Tag>
                                                    <Tag color="default" style={{ margin: 0, fontSize: 11 }}>
                                                        v{material.revision}
                                                    </Tag>
                                                </Space>
                                            </div>

                                            {/* 时序与片段区间 */}
                                            <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 10, fontSize: 12, flexWrap: 'wrap' }}>
                                                <Space size={4} style={{ color: '#059669', fontWeight: 600 }}>
                                                    <ClockCircleOutlined />
                                                    <span className="mono">
                                                        命中片段: {startStr} ~ {endStr} ({durationSec} 秒)
                                                    </span>
                                                </Space>
                                                <span className="mono" style={{ color: '#94a3b8', fontSize: 11 }}>
                                                    素材 ID: {material.material_unit_id}
                                                </span>
                                            </div>

                                            {/* 观测证据引文区域 */}
                                            <div className="semantic-evidence-quote" style={{ marginBottom: 14 }}>
                                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6, flexWrap: 'wrap', gap: 6 }}>
                                                    <Space size={6} wrap>
                                                        <Tag color="cyan" style={{ margin: 0, fontWeight: 600 }}>
                                                            {targetObs ? (modalityNames[targetObs.modality] || targetObs.modality) : '观测事实'}
                                                        </Tag>
                                                        {targetObs?.provenance?.model_id ? (
                                                            <span style={{ fontSize: 11, color: '#64748b' }}>
                                                                来源模型: {targetObs.provenance.model_id}
                                                            </span>
                                                        ) : null}
                                                        {/* 观测自身的模型识别置信度，与相关度严格区分 */}
                                                        {targetObs?.confidence != null ? (
                                                            <Tag color="green" style={{ margin: 0, fontSize: 11 }}>
                                                                模型识别置信度: {(targetObs.confidence * 100).toFixed(1)}%
                                                            </Tag>
                                                        ) : null}
                                                    </Space>
                                                    <span className="mono" style={{ fontSize: 11, color: '#94a3b8' }}>
                                                        {hit.observation_id ? `观测: ${hit.observation_id}` : ''}
                                                    </span>
                                                </div>
                                                <div style={{ color: textExcerpt ? '#1e293b' : '#94a3b8', fontStyle: textExcerpt ? 'normal' : 'italic' }}>
                                                    {textExcerpt || '（该观测未提取到文本描述，请在详情页查看结构化特征）'}
                                                </div>
                                            </div>

                                            {/* 底部动作：回看入口接入 */}
                                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', borderTop: '1px solid #f1f5f9', paddingTop: 10, flexWrap: 'wrap', gap: 10 }}>
                                                <Space size={6} wrap>
                                                    {material.tags.length ? material.tags.map((t) => (
                                                        <Tag key={t} style={{ fontSize: 10, margin: 0, background: '#f1f5f9' }}>
                                                            #{t}
                                                        </Tag>
                                                    )) : null}
                                                </Space>

                                                <Space size={10}>
                                                    {asset ? (
                                                        <Button
                                                            type="primary"
                                                            size="small"
                                                            icon={<AimOutlined />}
                                                            onClick={() => {
                                                                const seekMs = Number(range?.start_ms ?? 0);
                                                                setPreviewAsset({
                                                                    asset,
                                                                    seekMs,
                                                                });
                                                            }}
                                                            style={{ background: '#10b981', borderColor: '#10b981' }}
                                                        >
                                                            定位回看 ({startStr})
                                                        </Button>
                                                    ) : (
                                                        <Button
                                                            size="small"
                                                            disabled
                                                            icon={<PlayCircleOutlined />}
                                                            title="未找到对应视频母带资产文件"
                                                        >
                                                            原片不可用
                                                        </Button>
                                                    )}

                                                    <Link
                                                        to={`/materials/${encodeURIComponent(material.material_unit_id)}?revision=${material.revision}&observation=${encodeURIComponent(hit.observation_id || '')}&${searchParamsOnly(params)}`}
                                                    >
                                                        <Button
                                                            size="small"
                                                            icon={<ArrowRightOutlined />}
                                                        >
                                                            切片详情与观测追溯
                                                        </Button>
                                                    </Link>
                                                </Space>
                                            </div>
                                        </Card>
                                    );
                                })}
                            </div>
                        ) : viewMode === 'timeline' ? (
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
                                                        icon={<UnorderedListOutlined />}
                                                        onClick={() => setViewingSliceGroup(group)}
                                                    >
                                                        切片明细 ({group.materials.length})
                                                    </Button>
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
                                                <AlignmentTimeline
                                                    key={`${group.streamId}:${fullExecution ? secondPage : 0}`}
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
                                                    onOpenDetails={() => setViewingSliceGroup(group)}
                                                    asset={group.asset}
                                                    streamId={group.streamId}
                                                    streamTitle={group.title}
                                                />
                                            ) : null}
                                        </Card>
                                    );
                                })}
                            </div>
                        ) : (
                            /* ── 切片卡片卡片视图 ────────────────────────────────────────── */
                            <Row gutter={[8, 8]}>
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
                                                    borderRadius: 6,
                                                    borderColor: '#e2e8f0',
                                                }}
                                                bodyStyle={{
                                                    padding: 10,
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
                    ) : (result.data.unindexed_hits ?? 0) > 0 ? (
                        /* ── 处理尚未完成状态（有命中向量但索引尚未就绪） ───────── */
                        <Card style={{ borderColor: '#93c5fd', background: '#eff6ff' }}>
                            <div
                                style={{
                                    display: 'flex',
                                    flexDirection: 'column',
                                    gap: 10,
                                    alignItems: 'center',
                                    padding: '24px 0',
                                }}
                            >
                                <LoadingOutlined style={{ fontSize: 28, color: '#2563eb' }} />
                                <Text strong style={{ fontSize: 15, color: '#1e40af' }}>
                                    素材向量索引正在处理中
                                </Text>
                                <Text style={{ fontSize: 13, color: '#3b82f6', textAlign: 'center' }}>
                                    检测到 {result.data.unindexed_hits} 条潜在匹配素材正在建立向量索引或等待快慢路径融合（state != 'ready'）。
                                    <br />数据处理完成后将自动生效，请稍后刷新查看。
                                </Text>
                                <Button
                                    type="primary"
                                    icon={<ReloadOutlined />}
                                    onClick={() => void result.refetch()}
                                    style={{ marginTop: 8 }}
                                >
                                    刷新结果
                                </Button>
                            </div>
                        </Card>
                    ) : (
                        /* ── 没有匹配状态 ────────────────────────────────────────── */
                        <Card>
                            <Empty
                                title={
                                    activeMode === 'semantic'
                                        ? (params.get('q') ? '未找到符合语义的视频片段' : '请输入自然语言描述开始检索')
                                        : (params.toString() ? '未找到符合条件的素材' : '素材库暂无数据')
                                }
                            >
                                {activeMode === 'semantic' ? (
                                    params.get('q') ? (
                                        <div
                                            style={{
                                                display: 'flex',
                                                flexDirection: 'column',
                                                alignItems: 'center',
                                                gap: 8,
                                            }}
                                        >
                                            <span>
                                                在已索引的多模态观测中，未匹配到与“{params.get('q')}”语义相近的片段。
                                            </span>
                                            <span style={{ fontSize: 12, color: '#64748b' }}>
                                                建议尝试更换自然语言描述（如描述具体画面动作、物体或文字），或切换至关键词检索。
                                            </span>
                                            <Button
                                                type="link"
                                                onClick={() => {
                                                    const next = new URLSearchParams(params);
                                                    next.delete('mode');
                                                    setParams(next);
                                                }}
                                            >
                                                切换至关键词检索
                                            </Button>
                                        </div>
                                    ) : (
                                        '语义检索基于多模态向量特征匹配，请输入如“有人正在写字”、“会议总结汇报”等自然语言语句。'
                                    )
                                ) : params.toString() ? (
                                    '可尝试放宽时间范围、降低置信度阈值或更换关键词。'
                                ) : (
                                    '这里展示已完成推理与切片的素材单元。请在处理任务中下发视频流水线。'
                                )}
                            </Empty>
                        </Card>
                    )}
                </>
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
                    <div className="video-preview-box" style={{ marginBottom: 10 }}>
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
            {/* 视频切片明细抽屉（侧边栏滑出，主卡片不再展开冗长列表） */}
            <Drawer
                title={
                    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                        <UnorderedListOutlined style={{ color: '#0284c7', fontSize: 16 }} />
                        <span style={{ fontSize: 15, fontWeight: 600 }}>切片明细清单</span>
                        {viewingSliceGroup ? (
                            <Tag color="geekblue" style={{ margin: 0, fontFamily: 'monospace' }}>
                                {viewingSliceGroup.materials.length} 条时间轴切片
                            </Tag>
                        ) : null}
                    </div>
                }
                open={Boolean(viewingSliceGroup)}
                onClose={() => setViewingSliceGroup(null)}
                width={620}
                styles={{
                    header: {
                        borderBottom: '1px solid #e2e8f0',
                        padding: '16px 20px',
                    },
                    body: {
                        padding: '16px 20px',
                        background: '#f8fafc',
                    },
                }}
            >
                {viewingSliceGroup ? (
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                        {/* 顶部视频母带上下文概要 */}
                        <div
                            style={{
                                padding: '12px 14px',
                                borderRadius: 8,
                                background: '#ffffff',
                                border: '1px solid #e2e8f0',
                                display: 'flex',
                                flexDirection: 'column',
                                gap: 6,
                            }}
                        >
                            <div
                                style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    alignItems: 'center',
                                    gap: 8,
                                }}
                            >
                                <Text strong style={{ fontSize: 14, color: '#0f172a' }}>
                                    {viewingSliceGroup.title}
                                </Text>
                                {viewingSliceGroup.asset ? (
                                    <Button
                                        size="small"
                                        type="primary"
                                        icon={<PlayCircleOutlined />}
                                        onClick={() => {
                                            setPreviewAsset({ asset: viewingSliceGroup.asset! });
                                        }}
                                    >
                                        原片全量回放
                                    </Button>
                                ) : null}
                            </div>
                            <Space size={12} wrap style={{ fontSize: 11, color: '#64748b' }}>
                                <span className="mono">ID: {viewingSliceGroup.streamId}</span>
                                <span style={{ color: '#059669' }}>
                                    覆盖: {formatTime(viewingSliceGroup.startMs.toString())} ~{' '}
                                    {formatTime(viewingSliceGroup.endMs.toString())}
                                </span>
                                {viewingSliceGroup.asset ? (
                                    <span>大小: {bytes(viewingSliceGroup.asset.size_bytes)}</span>
                                ) : null}
                            </Space>
                        </div>

                        {/* 切片列表卡片 */}
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                            {viewingSliceGroup.materials.map((item, idx) => {
                                const startStr = formatTime(item.time_range?.start_ms);
                                const endStr = formatTime(item.time_range?.end_ms);
                                const textExcerpt =
                                    item.observations
                                        .map((o) => payloadText(o.payload, 400))
                                        .filter(Boolean)
                                        .join(' ') || '';

                                return (
                                    <div
                                        key={item.material_unit_id}
                                        style={{
                                            background: '#ffffff',
                                            border: '1px solid #e2e8f0',
                                            borderRadius: 8,
                                            padding: 14,
                                            display: 'flex',
                                            flexDirection: 'column',
                                            gap: 10,
                                            boxShadow: '0 1px 2px 0 rgba(0, 0, 0, 0.03)',
                                        }}
                                    >
                                        {/* 切片头 */}
                                        <div
                                            style={{
                                                display: 'flex',
                                                justifyContent: 'space-between',
                                                alignItems: 'center',
                                                flexWrap: 'wrap',
                                                gap: 6,
                                            }}
                                        >
                                            <Space size={8} align="center">
                                                <Tag
                                                    style={{
                                                        fontFamily: 'monospace',
                                                        fontWeight: 700,
                                                        margin: 0,
                                                        background: '#f1f5f9',
                                                        color: '#334155',
                                                        border: 'none',
                                                    }}
                                                >
                                                    #{idx + 1}
                                                </Tag>
                                                <span
                                                    className="mono"
                                                    style={{
                                                        fontSize: 12,
                                                        fontWeight: 600,
                                                        color: '#0f172a',
                                                    }}
                                                >
                                                    {startStr} ~ {endStr}
                                                </span>
                                            </Space>

                                            <Space size={6}>
                                                <Tag
                                                    color={statusTagColor[item.status] || 'default'}
                                                    style={{ margin: 0, fontWeight: 500 }}
                                                >
                                                    {statusNames[item.status] || item.status}
                                                </Tag>
                                                <Tag
                                                    color="purple"
                                                    style={{ margin: 0, fontSize: 10 }}
                                                >
                                                    v{item.revision}
                                                </Tag>
                                            </Space>
                                        </div>

                                        {/* 模态标签 */}
                                        <Space size={[4, 4]} wrap>
                                            {[
                                                ...new Set(
                                                    item.observations.map((o) => o.modality),
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

                                        {/* 观测事实文本 */}
                                        <div
                                            style={{
                                                padding: '8px 12px',
                                                borderRadius: 6,
                                                background: '#f8fafc',
                                                border: '1px solid #f1f5f9',
                                                color: textExcerpt ? '#1e293b' : '#94a3b8',
                                                fontSize: 12,
                                                lineHeight: 1.6,
                                            }}
                                        >
                                            {textExcerpt || '（该时间段未提取到文字观测事实）'}
                                        </div>

                                        {/* 标签 */}
                                        {item.tags.length ? (
                                            <Space size={[4, 4]} wrap>
                                                {item.tags.map((tag) => (
                                                    <Tag
                                                        key={tag}
                                                        style={{
                                                            fontSize: 10,
                                                            margin: 0,
                                                            background: '#f1f5f9',
                                                        }}
                                                    >
                                                        #{tag}
                                                    </Tag>
                                                ))}
                                            </Space>
                                        ) : null}

                                        {/* 底部动作 */}
                                        <div
                                            style={{
                                                display: 'flex',
                                                justifyContent: 'flex-end',
                                                alignItems: 'center',
                                                gap: 12,
                                                paddingTop: 8,
                                                borderTop: '1px solid #f8fafc',
                                            }}
                                        >
                                            {viewingSliceGroup.asset ? (
                                                <Button
                                                    size="small"
                                                    icon={<AimOutlined />}
                                                    onClick={() => {
                                                        const seekMs = Number(
                                                            item.time_range?.start_ms ?? 0,
                                                        );
                                                        setPreviewAsset({
                                                            asset: viewingSliceGroup.asset!,
                                                            seekMs,
                                                        });
                                                    }}
                                                >
                                                    定位回放
                                                </Button>
                                            ) : null}
                                            <Link
                                                to={`/materials/${encodeURIComponent(item.material_unit_id)}?${searchParamsOnly(params)}`}
                                            >
                                                <Button
                                                    type="link"
                                                    size="small"
                                                    icon={<ArrowRightOutlined />}
                                                    style={{ padding: 0 }}
                                                >
                                                    切片详情
                                                </Button>
                                            </Link>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>
                    </div>
                ) : null}
            </Drawer>
        </div>
    );
}
