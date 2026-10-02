import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
    Button,
    Card,
    Col,
    Collapse,
    Descriptions,
    InputNumber,
    Row,
    Space,
    Table,
    Tag,
    Typography,
} from 'antd';
import {
    ArrowLeftOutlined,
    ReloadOutlined,
    LeftOutlined,
    RightOutlined,
    HistoryOutlined,
    ClockCircleOutlined,
    BranchesOutlined,
    FileTextOutlined,
    CheckCircleOutlined,
} from '@ant-design/icons';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import type {
    MaterialIndexStatus,
    MaterialUnit,
    Observation,
    ObservationIndexStatus,
    SourceReference,
} from '../api/contracts';
import { ErrorNotice, Heading, Loading } from '../components';
import {
    formatTime,
    modalityNames,
    payloadText,
    searchParamsOnly,
    statusNames,
    validRevision,
} from './material-utils';
import MaterialPlayer from './MaterialPlayer';
import PluginResult, { ResultIndex } from './PluginResult';
import './materials.css';

const { Text } = Typography;

export default function MaterialDetail() {
    const { id = '' } = useParams();
    const [params, setParams] = useSearchParams();
    const revision = params.get('revision'),
        valid = validRevision(revision);

    const latest = useQuery({
        queryKey: ['material', id, 'latest'],
        queryFn: ({ signal }) =>
            api<MaterialUnit>(`/v1/materials/${encodeURIComponent(id)}`, { signal }),
        retry: false,
    });

    const historical = useQuery({
        queryKey: ['material', id, revision],
        queryFn: ({ signal }) =>
            api<MaterialUnit>(`/v1/materials/${encodeURIComponent(id)}?revision=${revision}`, {
                signal,
            }),
        enabled: valid && revision !== null,
        retry: false,
    });

    const result = revision === null ? latest : historical;
    const item = valid ? result.data : undefined;
    const indexing = useQuery({
        queryKey: ['material-index', id, item?.revision],
        queryFn: ({ signal }) =>
            api<MaterialIndexStatus>(
                `/v1/materials/${encodeURIComponent(id)}/index-status?revision=${item?.revision}`,
                { signal },
            ),
        enabled: !!item,
        retry: false,
        refetchInterval: (query) =>
            query.state.data?.items.some((status) => status.state === 'pending') ? 10000 : false,
    });

    function selectRevision(value: string) {
        const next = new URLSearchParams(params);
        if (value) next.set('revision', value);
        else next.delete('revision');
        next.delete('observation');
        setParams(next);
    }

    const observationId = params.get('observation');
    const selectedObservation =
        item?.observations.find((o) => o.observation_id === observationId) || item?.observations[0];

    const [seekSeq, setSeekSeq] = useState(0);

    const statusTagColor: Record<string, string> = {
        fast_ready: 'blue',
        partial: 'warning',
        enriched: 'success',
        failed: 'error',
        rejected: 'error',
    };

    const sourceColumns = [
        {
            title: '母带资产 ID',
            dataIndex: 'asset_id',
            key: 'asset_id',
            render: (text: string) => <span className="mono">{text}</span>,
        },
        {
            title: '时序区间',
            key: 'time_range',
            render: (_: unknown, row: SourceReference) => (
                <span className="mono" style={{ color: '#059669' }}>
                    {formatTime(row.time_range?.start_ms)} ~ {formatTime(row.time_range?.end_ms)}
                </span>
            ),
        },
        {
            title: '内容哈希摘要',
            dataIndex: 'content_hash',
            key: 'content_hash',
            render: (hash: string) => (
                <span className="mono" style={{ color: '#64748b' }}>
                    {hash || '摘要未知'}
                </span>
            ),
        },
    ];

    return (
        <div className="materials-page">
            <div style={{ marginBottom: 14 }}>
                <Link to={`/materials?${searchParamsOnly(params)}`}>
                    <Button type="link" icon={<ArrowLeftOutlined />} style={{ padding: 0 }}>
                        返回素材检索列表
                    </Button>
                </Link>
            </div>

            <Heading
                eyebrow="Material Unit Detail"
                title="素材详情与观测追溯"
                description="多模态观测特征剖析、母带视听切片回看，并追溯每一次模型富化与置信度版本。"
                action={
                    <Button
                        icon={<ReloadOutlined />}
                        disabled={result.isFetching || !valid}
                        onClick={() => {
                            void result.refetch();
                            if (revision) void latest.refetch();
                        }}
                    >
                        刷新素材
                    </Button>
                }
            />

            <ErrorNotice error={result.error} />
            <ErrorNotice error={indexing.error} />

            {item ? (
                <>
                    {/* 版本切换卡片 */}
                    <Card
                        size="small"
                        style={{ marginBottom: 10, borderRadius: 6, background: '#f8fafc' }}
                        bodyStyle={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            flexWrap: 'wrap',
                            gap: 12,
                        }}
                    >
                        <Space size={10}>
                            <HistoryOutlined style={{ color: '#1668dc', fontSize: 16 }} />
                            <Text strong style={{ fontSize: 13 }}>
                                版本导航:
                            </Text>
                            <Tag color="purple" style={{ margin: 0, fontWeight: 600 }}>
                                v{item.revision}
                            </Tag>
                            <Tag
                                color={item.superseded ? 'default' : 'success'}
                                style={{ margin: 0 }}
                            >
                                {item.superseded ? '历史快照' : '当前激活最新版本'}
                            </Tag>
                        </Space>

                        <Space size={8}>
                            <Button
                                size="small"
                                icon={<LeftOutlined />}
                                disabled={!item || item.revision <= 1}
                                onClick={() => selectRevision(String(item.revision - 1))}
                            >
                                上一版本
                            </Button>
                            <Button
                                size="small"
                                icon={<RightOutlined />}
                                disabled={
                                    !item || !latest.data || item.revision >= latest.data.revision
                                }
                                onClick={() => selectRevision(String(item.revision + 1))}
                            >
                                下一版本
                            </Button>

                            <InputNumber
                                size="small"
                                min={1}
                                max={2147483647}
                                placeholder="输入版本号"
                                defaultValue={revision ? Number(revision) : undefined}
                                onPressEnter={(e) => {
                                    const target = e.target as HTMLInputElement;
                                    if (target.value) selectRevision(target.value);
                                }}
                                style={{ width: 100 }}
                            />

                            {revision !== null ? (
                                <Button size="small" type="link" onClick={() => selectRevision('')}>
                                    恢复最新版
                                </Button>
                            ) : null}
                        </Space>
                    </Card>

                    {/* 素材单元概览卡片 */}
                    <Card
                        title={
                            <div
                                style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    alignItems: 'center',
                                }}
                            >
                                <Space size={8}>
                                    <span style={{ fontWeight: 650, fontSize: 15 }}>
                                        素材元数据
                                    </span>
                                    <Tag color={statusTagColor[item.status] || 'default'}>
                                        {statusNames[item.status] || item.status}
                                    </Tag>
                                </Space>
                                <span className="mono" style={{ fontSize: 11, color: '#64748b' }}>
                                    UID: {item.material_unit_id}
                                </span>
                            </div>
                        }
                        style={{ marginBottom: 10 }}
                    >
                        <Descriptions bordered size="small" column={{ xs: 1, sm: 2, md: 4 }}>
                            <Descriptions.Item label="来源流 ID">
                                <span className="mono">{item.stream_id}</span>
                            </Descriptions.Item>
                            <Descriptions.Item label="时序对齐区间">
                                <Space size={4} style={{ color: '#059669' }}>
                                    <ClockCircleOutlined />
                                    <span className="mono">
                                        {formatTime(item.time_range?.start_ms)} ~{' '}
                                        {formatTime(item.time_range?.end_ms)}
                                    </span>
                                </Space>
                            </Descriptions.Item>
                            <Descriptions.Item label="流水线版本">
                                <Text strong style={{ color: '#1668dc' }}>
                                    {item.pipeline_version || 'v1.0'}
                                </Text>
                            </Descriptions.Item>
                            <Descriptions.Item label="观测记录数">
                                <Text strong>{item.observations.length} 条多模态切片</Text>
                            </Descriptions.Item>
                        </Descriptions>

                        {item.tags.length ? (
                            <div style={{ marginTop: 14 }}>
                                <Space size={6} wrap>
                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                        标签:
                                    </Text>
                                    {item.tags.map((tag) => (
                                        <Tag key={tag} color="blue">
                                            #{tag}
                                        </Tag>
                                    ))}
                                </Space>
                            </div>
                        ) : null}
                    </Card>

                    {/* 核心双栏：左侧播放器与来源，右侧观测记录与证据 */}
                    <Row gutter={[10, 10]} style={{ marginBottom: 10 }}>
                        <Col xs={24} lg={12}>
                            <MaterialPlayer
                                material={item}
                                observation={selectedObservation}
                                seekSequence={seekSeq}
                            />

                            <Card
                                title={
                                    <Space size={8}>
                                        <BranchesOutlined style={{ color: '#1668dc' }} />
                                        <span style={{ fontWeight: 650, fontSize: 14 }}>
                                            关联原始视频源
                                        </span>
                                        <Tag style={{ margin: 0 }}>
                                            {item.source_refs.length} 个引用
                                        </Tag>
                                    </Space>
                                }
                                bodyStyle={{ padding: 0 }}
                            >
                                {item.source_refs.length ? (
                                    <Table
                                        columns={sourceColumns}
                                        dataSource={item.source_refs}
                                        rowKey={(r, i) => `${r.asset_id}:${i}`}
                                        pagination={false}
                                        size="small"
                                    />
                                ) : (
                                    <div
                                        style={{
                                            padding: 24,
                                            textAlign: 'center',
                                            color: '#94a3b8',
                                        }}
                                    >
                                        当前版本无原始视频来源引用，无法进行画面回看。
                                    </div>
                                )}
                            </Card>
                        </Col>

                        <Col xs={24} lg={12}>
                            <Card
                                title={
                                    <div
                                        style={{
                                            display: 'flex',
                                            justifyContent: 'space-between',
                                            alignItems: 'center',
                                        }}
                                    >
                                        <Space size={8}>
                                            <FileTextOutlined style={{ color: '#1668dc' }} />
                                            <span style={{ fontWeight: 650, fontSize: 15 }}>
                                                多模态观测列表 (Observations)
                                            </span>
                                        </Space>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            点击观测项同步定位时间轴
                                        </Text>
                                    </div>
                                }
                                style={{ marginBottom: 10 }}
                                bodyStyle={{ padding: 10 }}
                            >
                                <div
                                    style={{
                                        display: 'flex',
                                        flexDirection: 'column',
                                        gap: 8,
                                        maxHeight: 400,
                                        overflowY: 'auto',
                                    }}
                                >
                                    {item.observations.map((obs) => {
                                        const isSelected =
                                            selectedObservation?.observation_id ===
                                            obs.observation_id;
                                        return (
                                            <div
                                                key={obs.observation_id}
                                                onClick={() => {
                                                    const next = new URLSearchParams(params);
                                                    next.set('observation', obs.observation_id);
                                                    setParams(next);
                                                    setSeekSeq((s) => s + 1);
                                                }}
                                                style={{
                                                    padding: '12px 14px',
                                                    borderRadius: 8,
                                                    border: `1px solid ${isSelected ? '#1668dc' : '#e2e8f0'}`,
                                                    background: isSelected ? '#eff6ff' : '#ffffff',
                                                    cursor: 'pointer',
                                                    transition: 'all 0.15s ease',
                                                    boxShadow: isSelected
                                                        ? '0 1px 4px rgba(22, 104, 220, 0.15)'
                                                        : 'none',
                                                }}
                                            >
                                                <div
                                                    style={{
                                                        display: 'flex',
                                                        justifyContent: 'space-between',
                                                        alignItems: 'center',
                                                        marginBottom: 6,
                                                    }}
                                                >
                                                    <Space size={6}>
                                                        <Tag
                                                            color="cyan"
                                                            style={{ margin: 0, fontWeight: 500 }}
                                                        >
                                                            {modalityNames[obs.modality] ||
                                                                obs.modality}
                                                        </Tag>
                                                        <Tag
                                                            color={
                                                                statusTagColor[obs.quality_state] ||
                                                                'default'
                                                            }
                                                            style={{ margin: 0, fontSize: 11 }}
                                                        >
                                                            {statusNames[obs.quality_state] ||
                                                                obs.quality_state}
                                                        </Tag>
                                                    </Space>
                                                    <span
                                                        className="mono"
                                                        style={{ fontSize: 11, color: '#059669' }}
                                                    >
                                                        {formatTime(obs.time_range?.start_ms)} ~{' '}
                                                        {formatTime(obs.time_range?.end_ms)}
                                                    </span>
                                                </div>

                                                <div
                                                    style={{
                                                        fontSize: 13,
                                                        color: isSelected ? '#0f172a' : '#334155',
                                                        lineHeight: 1.5,
                                                        display: '-webkit-box',
                                                        WebkitLineClamp: 2,
                                                        WebkitBoxOrient: 'vertical',
                                                        overflow: 'hidden',
                                                    }}
                                                >
                                                    {payloadText(obs.payload) ||
                                                        '（无文字负载内容）'}
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            </Card>

                            {selectedObservation ? (
                                <ObservationDetail
                                    observation={selectedObservation}
                                    indexStatus={indexing.data?.items.find(
                                        (status) =>
                                            status.observation_id ===
                                            selectedObservation.observation_id,
                                    )}
                                />
                            ) : null}
                        </Col>
                    </Row>
                </>
            ) : result.isPending ? (
                <Loading tip="正在载入素材详情与时序观测…" />
            ) : null}
        </div>
    );
}

function ObservationDetail({
    observation: obs,
    indexStatus,
}: {
    observation: Observation;
    indexStatus?: ObservationIndexStatus;
}) {
    const p = obs.provenance;
    const nonModel = p?.model_applicability === 'MODEL_APPLICABILITY_NOT_APPLICABLE';

    return (
        <Card
            title={
                <Space size={8}>
                    <CheckCircleOutlined style={{ color: '#10b981' }} />
                    <span style={{ fontWeight: 650, fontSize: 14 }}>
                        选中观测详情 · {modalityNames[obs.modality] || obs.modality}
                    </span>
                </Space>
            }
        >
            <div
                style={{
                    fontSize: 14,
                    lineHeight: 1.7,
                    padding: '8px 12px',
                    background: '#f8fafc',
                    borderRadius: 5,
                    marginBottom: 10,
                    border: '1px solid #e2e8f0',
                    color: '#0f172a',
                    whiteSpace: 'pre-wrap',
                }}
            >
                {payloadText(obs.payload) || '该观测没有文字预览，请在下方查看结构化 JSON 数据。'}
            </div>

            <Descriptions
                bordered
                size="small"
                column={{ xs: 1, sm: 2 }}
                style={{ marginBottom: 10 }}
            >
                <Descriptions.Item label="质量评估状态">
                    <Tag color="green">{statusNames[obs.quality_state] || obs.quality_state}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label="模型置信度">
                    <Text strong style={{ color: '#1668dc' }}>
                        {nonModel
                            ? '模型不适用'
                            : obs.confidence == null
                              ? '未知'
                              : String(obs.confidence)}
                    </Text>
                </Descriptions.Item>
                <Descriptions.Item label="检索状态">
                    <ResultIndex status={indexStatus} />
                </Descriptions.Item>
                {obs.schema_id ? (
                    <Descriptions.Item label="结果 Schema">
                        {obs.schema_id} · {obs.schema_version}
                    </Descriptions.Item>
                ) : null}
                <Descriptions.Item label="观测时序">
                    <span className="mono" style={{ color: '#059669' }}>
                        {formatTime(obs.time_range?.start_ms)} ~{' '}
                        {formatTime(obs.time_range?.end_ms)}
                    </span>
                </Descriptions.Item>
                <Descriptions.Item label="时序来源">
                    <Text>{obs.timing_source || '未知'}</Text>
                    {obs.timing_confidence != null ? (
                        <div style={{ fontSize: 11, color: '#94a3b8' }}>
                            时序置信度: {String(obs.timing_confidence)}
                        </div>
                    ) : null}
                </Descriptions.Item>
            </Descriptions>

            <Collapse
                size="small"
                defaultActiveKey={['lineage']}
                items={[
                    {
                        key: 'lineage',
                        label: <span style={{ fontWeight: 600 }}>处理器与来源血缘链路</span>,
                        children: (
                            <Descriptions
                                bordered
                                size="small"
                                column={1}
                                labelStyle={{ width: 140 }}
                            >
                                <Descriptions.Item label="观测 ID">
                                    <span className="mono">{obs.observation_id}</span>
                                </Descriptions.Item>
                                <Descriptions.Item label="模型信息">
                                    {nonModel
                                        ? '模型不适用（确定性处理器）'
                                        : `${p?.model_id || '未知'} · ${p?.model_version || '未知'}`}{' '}
                                    · 后端 {p?.execution_backend || '未知'}
                                </Descriptions.Item>
                                {p?.processor_release_id ? (
                                    <Descriptions.Item label="处理器 Release">
                                        <span className="mono">{p.processor_release_id}</span>
                                    </Descriptions.Item>
                                ) : null}
                                {obs.schema_digest ? (
                                    <Descriptions.Item label="Schema 摘要">
                                        <span className="mono">{obs.schema_digest}</span>
                                    </Descriptions.Item>
                                ) : null}
                                <Descriptions.Item label="插件来源">
                                    {p?.plugin || '未知'} · {p?.plugin_version || '未知'}
                                </Descriptions.Item>
                                <Descriptions.Item label="输入内容摘要">
                                    <span className="mono">{obs.content_hash || '未知'}</span>
                                </Descriptions.Item>
                                <Descriptions.Item label="模型产物摘要">
                                    <span className="mono">
                                        {nonModel
                                            ? '模型不适用'
                                            : p?.model_artifact_digest || '未知'}
                                    </span>
                                </Descriptions.Item>
                            </Descriptions>
                        ),
                    },
                    {
                        key: 'payload',
                        label: (
                            <span style={{ fontWeight: 600 }}>结构化原始数据 (Payload JSON)</span>
                        ),
                        children: <PluginResult payload={obs.payload} />,
                    },
                ]}
            />
        </Card>
    );
}
