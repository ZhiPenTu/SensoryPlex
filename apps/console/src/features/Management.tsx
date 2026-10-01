import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Alert,
    Button,
    Card,
    Checkbox,
    Col,
    Form,
    Input,
    InputNumber,
    Popconfirm,
    Row,
    Select,
    Space,
    Table,
    Tag,
    Typography,
    message,
    Descriptions,
    Tabs,
    Tooltip,
} from 'antd';
import {
    ApartmentOutlined,
    PlusOutlined,
    FolderOutlined,
    EditOutlined,
    KeyOutlined,
    CopyOutlined,
    CheckOutlined,
    StopOutlined,
    CheckCircleOutlined,
    UserOutlined,
    EyeOutlined,
    BranchesOutlined,
    ClockCircleOutlined,
    FileTextOutlined,
} from '@ant-design/icons';
import { api, post } from '../api/client';
import type {
    AccessToken,
    AuditList,
    PipelineList,
    PluginConfigList,
    TokenList,
    UserList,
    User,
    Pipeline,
    PipelineDetail,
} from '../api/contracts';
import {
    Badge,
    date,
    Empty,
    ErrorNotice,
    Heading,
    Loading,
    Modal,
    Notice,
} from '../components';
import { usePermission, useSession } from '../session';

const { Text } = Typography;

const MULTIMODAL_PLUGIN_IDS = {
    ocr: 'org.sensoryplex.ocr-rapidocr',
    asr: 'org.sensoryplex.asr-whisper-mlx',
    vlm: 'org.sensoryplex.vlm-moondream',
} as const;

const multimodalInitialValues = {
    policy: {
        // Coverage 是不允许被调低的事实契约；其余参数进入不可变 Revision。
        window_ms: 1000,
        sample_interval_ms: 1000,
        audio_segment_ms: 6000,
        // 音频默认重叠 500 毫秒，与执行器默认策略保持一致。
        audio_overlap_ms: 500,
        vlm_sample_interval_ms: 1000,
    },
};

type MultimodalFormValues = {
    name: string;
    description?: string;
    ocr_config_id: string;
    asr_config_id: string;
    vlm_config_id?: string;
    policy: {
        window_ms: number;
        sample_interval_ms: number;
        audio_segment_ms: number;
        audio_overlap_ms: number;
        vlm_sample_interval_ms: number;
    };
};

type MultimodalValidation = {
    valid: boolean;
    errors: string[];
    graph_digest: string;
    topological_order: string[];
    fingerprint?: string;
};

function multimodalPayload(values: MultimodalFormValues) {
    return {
        name: values.name,
        description: values.description || '',
        components: [
            { node_id: 'ocr_fast', config_id: values.ocr_config_id },
            { node_id: 'asr_fast', config_id: values.asr_config_id },
            ...(values.vlm_config_id
                ? [{ node_id: 'vlm_enrich', config_id: values.vlm_config_id, required: false }]
                : []),
        ],
        policy: values.policy,
    };
}

/**
 * 处理方案管理 (Pipelines)
 */
function PipelineDetailModal({
    pipeline,
    onClose,
}: {
    pipeline: Pipeline;
    onClose: () => void;
}) {
    const detail = useQuery({
        queryKey: ['pipeline-detail', pipeline.id],
        queryFn: ({ signal }) =>
            api<PipelineDetail>(`/v1/pipelines/${encodeURIComponent(pipeline.id)}`, { signal }),
    });

    const data = detail.data;
    const rev = data?.revision as Record<string, any> | undefined;
    const nodes = (rev?.nodes as Array<Record<string, any>>) || [];
    const edges = (rev?.edges as Array<Record<string, any>>) || [];
    const configs = (data?.configs as Record<string, any>) || {};

    const ocrNode = nodes.find((n) => n.id === 'ocr_fast');
    const asrNode = nodes.find((n) => n.id === 'asr_fast');
    const timelineNode = nodes.find((n) => n.id === 'timeline_fusion');
    const delayedEnrichments = (timelineNode?.delayed_enrichments as Array<Record<string, any>>) || [];
    const vlmNode = delayedEnrichments.find((n) => n.id === 'vlm_enrich');
    const policy = (timelineNode?.execution_policy as Record<string, any>) || {};

    const ocrConfig = ocrNode?.config_id ? configs[ocrNode.config_id] : null;
    const asrConfig = asrNode?.config_id ? configs[asrNode.config_id] : null;
    const vlmConfig = vlmNode?.config_id ? configs[vlmNode.config_id] : null;
    const legacyConfig = data?.pipeline?.config_id ? configs[data.pipeline.config_id] : null;

    const isOrchestrated = data?.pipeline?.execution_mode === 'orchestrated_v2';

    return (
        <Modal title="处理方案配置详情" onClose={onClose} width={920}>
            <ErrorNotice error={detail.error} />
            {detail.isPending ? (
                <Loading tip="正在读取方案与不可变编排图配置…" />
            ) : data ? (
                <Space direction="vertical" size={14} style={{ width: '100%' }}>
                    <Card size="small">
                        <Row gutter={[16, 10]}>
                            <Col xs={24} md={12}>
                                <Text type="secondary" style={{ fontSize: 12 }}>方案名称 / 描述</Text>
                                <div>
                                    <Text strong style={{ fontSize: 14 }}>{data.pipeline?.name}</Text>
                                    <div style={{ fontSize: 12, color: '#64748b', marginTop: 2 }}>
                                        {data.pipeline?.description || '无补充描述'}
                                    </div>
                                </div>
                            </Col>
                            <Col xs={12} md={6}>
                                <Text type="secondary" style={{ fontSize: 12 }}>版本 / 状态</Text>
                                <div style={{ marginTop: 2, display: 'flex', gap: 6, alignItems: 'center' }}>
                                    <Tag color="purple">v{data.pipeline?.revision}</Tag>
                                    <Badge state={data.pipeline?.state || 'draft'} />
                                </div>
                            </Col>
                            <Col xs={12} md={6}>
                                <Text type="secondary" style={{ fontSize: 12 }}>执行语义</Text>
                                <div style={{ marginTop: 2 }}>
                                    {isOrchestrated ? (
                                        <Tag color="blue">多模态编排 v2 (图 v{data.pipeline?.orchestration_revision})</Tag>
                                    ) : (
                                        <Tag>单模态兼容</Tag>
                                    )}
                                </div>
                            </Col>
                            <Col xs={24} md={12}>
                                <Text type="secondary" style={{ fontSize: 12 }}>不可变图摘要 (Graph Digest)</Text>
                                <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2 }}>
                                    <span className="mono" style={{ fontSize: 11, color: '#334155', wordBreak: 'break-all' }}>
                                        {data.pipeline?.graph_digest || data.pipeline?.plugin_digest || '—'}
                                    </span>
                                    {(data.pipeline?.graph_digest || data.pipeline?.plugin_digest) ? (
                                        <Tooltip title="复制摘要">
                                            <Button
                                                size="small"
                                                type="text"
                                                icon={<CopyOutlined />}
                                                onClick={() => {
                                                    void navigator.clipboard.writeText(
                                                        data.pipeline?.graph_digest || data.pipeline?.plugin_digest || ''
                                                    );
                                                    message.success('图摘要已复制');
                                                }}
                                            />
                                        </Tooltip>
                                    ) : null}
                                </div>
                            </Col>
                            <Col xs={24} md={12}>
                                <Text type="secondary" style={{ fontSize: 12 }}>保存时间</Text>
                                <div style={{ fontSize: 12, marginTop: 2, color: '#475569' }}>
                                    {data.pipeline?.created_at ? date(data.pipeline.created_at) : '—'}
                                </div>
                            </Col>
                        </Row>
                    </Card>

                    <Tabs
                        defaultActiveKey="modalities"
                        items={[
                            {
                                key: 'modalities',
                                label: (
                                    <span>
                                        <BranchesOutlined /> 模态插件与配置
                                    </span>
                                ),
                                children: isOrchestrated ? (
                                    <Space direction="vertical" size={12} style={{ width: '100%' }}>
                                        <Card
                                            size="small"
                                            title={
                                                <Space>
                                                    <span style={{ fontWeight: 600 }}>OCR 快速文本识别</span>
                                                    <Tag color="blue">必需快路径</Tag>
                                                    <Tag>节点: {ocrNode?.id || 'ocr_fast'}</Tag>
                                                </Space>
                                            }
                                        >
                                            <Descriptions bordered size="small" column={{ xs: 1, sm: 2, md: 3 }}>
                                                <Descriptions.Item label="算法插件">
                                                    <span className="mono" style={{ fontSize: 12 }}>
                                                        {ocrNode?.plugin_id || MULTIMODAL_PLUGIN_IDS.ocr} (v{ocrNode?.plugin_version || '0.1.1'})
                                                    </span>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="绑定配置">
                                                    <Text strong>{ocrConfig?.name || '默认配置'}</Text>
                                                    <span className="mono" style={{ fontSize: 11, color: '#64748b', display: 'block' }}>
                                                        {ocrNode?.config_id || '—'}
                                                    </span>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="数据面模式">
                                                    <Tag color="cyan">{ocrNode?.placement || 'data_plane_local'}</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="识别模型 (model_id)">
                                                    <Tag>{String(ocrConfig?.config?.model_id || 'PP-OCRv6_mobile')}</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="置信度阈值 (text_score)">
                                                    <Tag color="green">{String(ocrConfig?.config?.text_score ?? '0.5')}</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="推理后端 (provider)">
                                                    <Tag>{String(ocrConfig?.config?.provider || 'cpu')}</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="调度超时上限">
                                                    {ocrNode?.deadline_ms ? `${ocrNode.deadline_ms / 1000}s` : '120s'}
                                                </Descriptions.Item>
                                                <Descriptions.Item label="最大重试次数">
                                                    {ocrNode?.max_attempts ?? 2} 次
                                                </Descriptions.Item>
                                                <Descriptions.Item label="输出事实">
                                                    <span className="mono" style={{ fontSize: 11 }}>observation.ocr_blocks</span>
                                                </Descriptions.Item>
                                            </Descriptions>
                                            {ocrConfig?.config ? (
                                                <details style={{ marginTop: 8 }}>
                                                    <summary style={{ cursor: 'pointer', color: '#1668dc', fontSize: 12 }}>
                                                        查看完整配置参数 JSON
                                                    </summary>
                                                    <pre className="mono" style={{ background: '#f8fafc', padding: 8, borderRadius: 4, fontSize: 11, marginTop: 4, maxHeight: 150, overflow: 'auto' }}>
                                                        {JSON.stringify(ocrConfig.config, null, 2)}
                                                    </pre>
                                                </details>
                                            ) : null}
                                        </Card>

                                        <Card
                                            size="small"
                                            title={
                                                <Space>
                                                    <span style={{ fontWeight: 600 }}>ASR 语音转写对齐</span>
                                                    <Tag color="blue">必需快路径</Tag>
                                                    <Tag>节点: {asrNode?.id || 'asr_fast'}</Tag>
                                                </Space>
                                            }
                                        >
                                            <Descriptions bordered size="small" column={{ xs: 1, sm: 2, md: 3 }}>
                                                <Descriptions.Item label="算法插件">
                                                    <span className="mono" style={{ fontSize: 12 }}>
                                                        {asrNode?.plugin_id || MULTIMODAL_PLUGIN_IDS.asr} (v{asrNode?.plugin_version || '0.1.1'})
                                                    </span>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="绑定配置">
                                                    <Text strong>{asrConfig?.name || '默认配置'}</Text>
                                                    <span className="mono" style={{ fontSize: 11, color: '#64748b', display: 'block' }}>
                                                        {asrNode?.config_id || '—'}
                                                    </span>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="数据面模式">
                                                    <Tag color="cyan">{asrNode?.placement || 'data_plane_local'}</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="转写模型 (model)">
                                                    <Tag>{String(asrConfig?.config?.model || 'whisper-large-v3-turbo')}</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="任务类型 (task)">
                                                    <Tag>{String(asrConfig?.config?.task || 'transcribe')}</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="语言模式 (language)">
                                                    <Tag>{String(asrConfig?.config?.language ?? '自动检测 (null)')}</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="调度超时上限">
                                                    {asrNode?.deadline_ms ? `${asrNode.deadline_ms / 1000}s` : '180s'}
                                                </Descriptions.Item>
                                                <Descriptions.Item label="最大重试次数">
                                                    {asrNode?.max_attempts ?? 2} 次
                                                </Descriptions.Item>
                                                <Descriptions.Item label="输出事实">
                                                    <span className="mono" style={{ fontSize: 11 }}>observation.asr_segment</span>
                                                </Descriptions.Item>
                                            </Descriptions>
                                            {asrConfig?.config ? (
                                                <details style={{ marginTop: 8 }}>
                                                    <summary style={{ cursor: 'pointer', color: '#1668dc', fontSize: 12 }}>
                                                        查看完整配置参数 JSON
                                                    </summary>
                                                    <pre className="mono" style={{ background: '#f8fafc', padding: 8, borderRadius: 4, fontSize: 11, marginTop: 4, maxHeight: 150, overflow: 'auto' }}>
                                                        {JSON.stringify(asrConfig.config, null, 2)}
                                                    </pre>
                                                </details>
                                            ) : null}
                                        </Card>

                                        <Card
                                            size="small"
                                            title={
                                                <Space>
                                                    <span style={{ fontWeight: 600 }}>VLM 画面场景理解</span>
                                                    {vlmNode ? (
                                                        <>
                                                            <Tag color="purple">可选慢路径 · 异步解耦</Tag>
                                                            <Tag>节点: vlm_enrich</Tag>
                                                        </>
                                                    ) : (
                                                        <Tag>未启用慢路径</Tag>
                                                    )}
                                                </Space>
                                            }
                                        >
                                            {vlmNode ? (
                                                <>
                                                    <Notice>
                                                        慢路径采用独立 JetStream WorkQueue 异步延迟满足：快路径完成后立即标记 ready_for_review，
                                                        不阻塞用户检索文字与原片回看；宿主机工作器根据时间锚点按需解码单帧图像。
                                                    </Notice>
                                                    <Descriptions bordered size="small" column={{ xs: 1, sm: 2, md: 3 }} style={{ marginTop: 8 }}>
                                                        <Descriptions.Item label="算法插件">
                                                            <span className="mono" style={{ fontSize: 12 }}>
                                                                {vlmNode.plugin_id || MULTIMODAL_PLUGIN_IDS.vlm} (v{vlmNode.plugin_version || '0.1.3'})
                                                            </span>
                                                        </Descriptions.Item>
                                                        <Descriptions.Item label="绑定配置">
                                                            <Text strong>{vlmConfig?.name || '默认配置'}</Text>
                                                            <span className="mono" style={{ fontSize: 11, color: '#64748b', display: 'block' }}>
                                                                {vlmNode.config_id || '—'}
                                                            </span>
                                                        </Descriptions.Item>
                                                        <Descriptions.Item label="消费通道">
                                                            <Tag color="volcano">sensoryplex.tasks.vlm.v1</Tag>
                                                        </Descriptions.Item>
                                                        <Descriptions.Item label="场景模型 (model)">
                                                            <Tag>{String(vlmConfig?.config?.model || 'moondream:v2')}</Tag>
                                                        </Descriptions.Item>
                                                        <Descriptions.Item label="防 OOM 内存门限">
                                                            <Tag color="orange">
                                                                {vlmConfig?.config?.min_free_memory_bytes
                                                                    ? `${Math.round(Number(vlmConfig.config.min_free_memory_bytes) / 1024 / 1024)} MiB`
                                                                    : '256 MiB'}
                                                            </Tag>
                                                        </Descriptions.Item>
                                                        <Descriptions.Item label="端点 (endpoint)">
                                                            <span className="mono" style={{ fontSize: 11 }}>
                                                                {String(vlmConfig?.config?.endpoint || 'http://127.0.0.1:11434')}
                                                            </span>
                                                        </Descriptions.Item>
                                                        <Descriptions.Item label="场景提示词 (prompt)" span={3}>
                                                            <Text code style={{ fontSize: 12 }}>
                                                                {String(vlmConfig?.config?.prompt || 'Describe what is visible in this image in one sentence.')}
                                                            </Text>
                                                        </Descriptions.Item>
                                                    </Descriptions>
                                                    {vlmConfig?.config ? (
                                                        <details style={{ marginTop: 8 }}>
                                                            <summary style={{ cursor: 'pointer', color: '#1668dc', fontSize: 12 }}>
                                                                查看完整配置参数 JSON
                                                            </summary>
                                                            <pre className="mono" style={{ background: '#f8fafc', padding: 8, borderRadius: 4, fontSize: 11, marginTop: 4, maxHeight: 150, overflow: 'auto' }}>
                                                                {JSON.stringify(vlmConfig.config, null, 2)}
                                                            </pre>
                                                        </details>
                                                    ) : null}
                                                </>
                                            ) : (
                                                <div style={{ padding: '12px 0', color: '#64748b', fontSize: 13 }}>
                                                    当前方案未启用 VLM 场景描述插件。任务执行时仅运行 OCR 与 ASR 同步快路径，不会向异步慢路径队列分派任务。
                                                </div>
                                            )}
                                        </Card>
                                    </Space>
                                ) : (
                                    <Card size="small" title="单模态兼容插件配置">
                                        <Descriptions bordered size="small" column={2}>
                                            <Descriptions.Item label="算法插件">
                                                <span className="mono">{data.pipeline?.plugin_id}</span>
                                            </Descriptions.Item>
                                            <Descriptions.Item label="插件摘要">
                                                <span className="mono" style={{ fontSize: 11 }}>{data.pipeline?.plugin_digest}</span>
                                            </Descriptions.Item>
                                            <Descriptions.Item label="绑定配置 ID">
                                                <span className="mono">{data.pipeline?.config_id}</span>
                                            </Descriptions.Item>
                                            <Descriptions.Item label="配置名称">
                                                {legacyConfig?.name || '—'}
                                            </Descriptions.Item>
                                        </Descriptions>
                                        {legacyConfig?.config ? (
                                            <pre className="mono" style={{ background: '#f8fafc', padding: 8, borderRadius: 4, fontSize: 11, marginTop: 10 }}>
                                                {JSON.stringify(legacyConfig.config, null, 2)}
                                            </pre>
                                        ) : null}
                                    </Card>
                                ),
                            },
                            {
                                key: 'timeline',
                                label: (
                                    <span>
                                        <ClockCircleOutlined /> 时序策略与拓扑
                                    </span>
                                ),
                                children: (
                                    <Space direction="vertical" size={12} style={{ width: '100%' }}>
                                        <Card size="small" title="时序切片与事实网格策略 (Timeline Fusion Policy)">
                                            <Descriptions bordered size="small" column={{ xs: 1, sm: 2, md: 3 }}>
                                                <Descriptions.Item label="事实基准窗口 (window_ms)">
                                                    <Tag color="blue">{policy.window_ms ?? 1000} ms (固定 1 秒网格)</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="视频抽帧间隔 (sample_interval_ms)">
                                                    <Tag>{policy.sample_interval_ms ?? 1000} ms</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="音频分段长度 (audio_segment_ms)">
                                                    <Tag>{policy.audio_segment_ms ?? 6000} ms (6 秒)</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="音频滑动重叠 (audio_overlap_ms)">
                                                    <Tag>{policy.audio_overlap_ms ?? 500} ms</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="VLM 采样间隔 (vlm_sample_interval_ms)">
                                                    <Tag color="purple">{policy.vlm_sample_interval_ms ? `${policy.vlm_sample_interval_ms} ms` : '—'}</Tag>
                                                </Descriptions.Item>
                                                <Descriptions.Item label="时序融合算子">
                                                    <span className="mono" style={{ fontSize: 11 }}>org.sensoryplex.runtime.timeline-fusion</span>
                                                </Descriptions.Item>
                                            </Descriptions>
                                        </Card>

                                        <Card size="small" title={`DAG 模态数据流拓扑边（${edges.length} 条）`} bodyStyle={{ padding: 0 }}>
                                            <Table
                                                size="small"
                                                rowKey={(edge) => `${edge.from_node_id}->${edge.to_node_id}:${edge.modality}`}
                                                pagination={false}
                                                dataSource={edges}
                                                columns={[
                                                    {
                                                        title: '来源节点',
                                                        dataIndex: 'from_node_id',
                                                        key: 'from_node_id',
                                                        render: (node: string) => <Tag color="blue">{node}</Tag>,
                                                    },
                                                    {
                                                        title: '流动模态 (Modality)',
                                                        dataIndex: 'modality',
                                                        key: 'modality',
                                                        render: (m: string) => <span className="mono" style={{ fontSize: 12 }}>{m}</span>,
                                                    },
                                                    {
                                                        title: '汇聚节点',
                                                        dataIndex: 'to_node_id',
                                                        key: 'to_node_id',
                                                        render: (node: string) => <Tag color="green">{node}</Tag>,
                                                    },
                                                    {
                                                        title: '对齐策略 (Join Policy)',
                                                        dataIndex: 'join_policy',
                                                        key: 'join_policy',
                                                        render: (p: string) => <Tag>{p}</Tag>,
                                                    },
                                                    {
                                                        title: '约束要求',
                                                        dataIndex: 'required',
                                                        key: 'required',
                                                        width: 90,
                                                        render: (req: boolean) => (
                                                            <Tag color={req ? 'blue' : 'default'}>{req ? '必需' : '可选'}</Tag>
                                                        ),
                                                    },
                                                ]}
                                            />
                                        </Card>
                                    </Space>
                                ),
                            },
                            {
                                key: 'raw',
                                label: (
                                    <span>
                                        <FileTextOutlined /> 原始编排定义 (JSON)
                                    </span>
                                ),
                                children: (
                                    <Card
                                        size="small"
                                        title="不可变 Revision 图定义 (definition_json)"
                                        extra={
                                            <Button
                                                size="small"
                                                type="text"
                                                icon={<CopyOutlined />}
                                                onClick={() => {
                                                    void navigator.clipboard.writeText(JSON.stringify(rev || {}, null, 2));
                                                    message.success('编排定义 JSON 已复制');
                                                }}
                                            >
                                                复制 JSON
                                            </Button>
                                        }
                                    >
                                        <pre
                                            className="mono"
                                            style={{
                                                background: '#0f172a',
                                                color: '#e2e8f0',
                                                padding: 12,
                                                borderRadius: 6,
                                                fontSize: 12,
                                                lineHeight: 1.5,
                                                maxHeight: 400,
                                                overflow: 'auto',
                                                margin: 0,
                                            }}
                                        >
                                            {JSON.stringify(rev || {}, null, 2)}
                                        </pre>
                                    </Card>
                                ),
                            },
                        ]}
                    />
                </Space>
            ) : null}
        </Modal>
    );
}

export function Pipelines() {
    const cache = useQueryClient();
    const [offset, setOffset] = useState(0);
    const [open, setOpen] = useState(false);
    const [form] = Form.useForm();
    const [validation, setValidation] = useState<MultimodalValidation | null>(null);
    const [selectedPipeline, setSelectedPipeline] = useState<Pipeline | null>(null);

    const listing = useQuery({
        queryKey: ['pipelines', offset],
        queryFn: ({ signal }) =>
            api<PipelineList>(`/v1/pipelines?limit=20&offset=${offset}`, { signal }),
    });

    const configs = useQuery({
        queryKey: ['configs'],
        queryFn: ({ signal }) =>
            api<PluginConfigList>('/admin/v1/plugin-configurations?limit=100', { signal }),
    });

    const validate = useMutation({
        mutationFn: (body: ReturnType<typeof multimodalPayload>) =>
            post<MultimodalValidation>('/admin/v1/multimodal-pipelines:validate', body),
        onSuccess: (result, body) => {
            setValidation({ ...result, fingerprint: JSON.stringify(body) });
            if (result.valid) message.success('多模态编排校验通过，已锁定本次图摘要。');
        },
    });

    const save = useMutation({
        mutationFn: (body: ReturnType<typeof multimodalPayload>) =>
            post('/admin/v1/multimodal-pipelines', body),
        onSuccess: () => {
            message.success('多模态处理方案已保存为不可变编排版本');
            setOpen(false);
            form.resetFields();
            setValidation(null);
            void cache.invalidateQueries({ queryKey: ['pipelines'] });
        },
    });

    const publish = useMutation({
        mutationFn: (id: string) => post(`/admin/v1/pipelines/${id}:publish`),
        onSuccess: () => {
            message.success('方案已成功发布');
            void cache.invalidateQueries({ queryKey: ['pipelines'] });
        },
    });

    const archive = useMutation({
        mutationFn: (id: string) => post(`/admin/v1/pipelines/${id}:archive`),
        onSuccess: () => {
            message.success('方案已归档');
            void cache.invalidateQueries({ queryKey: ['pipelines'] });
        },
    });

    const items = listing.data?.items || [];
    const totalCount = listing.data?.total || 0;
    const configsByPlugin = (pluginId: string) =>
        configs.data?.items.filter((item) => item.plugin_id === pluginId) || [];
    const missingConfigs = Object.entries(MULTIMODAL_PLUGIN_IDS)
        .filter(([, pluginId]) => !configsByPlugin(pluginId).length)
        .map(([modality]) => modality.toUpperCase());

    const validateForm = async () => {
        try {
            const values = (await form.validateFields()) as MultimodalFormValues;
            validate.mutate(multimodalPayload(values));
        } catch {
            // Form 已在字段旁展示校验错误，避免再额外弹出同一条提示。
        }
    };

    const saveForm = (values: MultimodalFormValues) => {
        const body = multimodalPayload(values);
        if (!validation?.valid || validation.fingerprint !== JSON.stringify(body)) {
            message.warning('当前配置尚未校验，或校验后已被修改；请先重新校验。');
            return;
        }
        save.mutate(body);
    };

    const columns = [
        {
            title: '方案名称',
            dataIndex: 'name',
            key: 'name',
            render: (name: string, row: (typeof items)[0]) => (
                <Space size={10} align="start">
                    <div
                        style={{
                            width: 34,
                            height: 34,
                            borderRadius: 6,
                            background: '#eff6ff',
                            color: '#1668dc',
                            display: 'grid',
                            placeItems: 'center',
                            fontSize: 16,
                            flexShrink: 0,
                        }}
                    >
                        <ApartmentOutlined />
                    </div>
                    <div>
                        <Button
                            type="link"
                            style={{
                                padding: 0,
                                height: 'auto',
                                fontSize: 13,
                                fontWeight: 600,
                                color: '#0f172a',
                                textAlign: 'left',
                            }}
                            onClick={() => setSelectedPipeline(row)}
                        >
                            {name}
                        </Button>
                        <div style={{ fontSize: 12, color: '#64748b' }}>
                            {row.description || `插件: ${row.plugin_id}`}
                        </div>
                    </div>
                </Space>
            ),
        },
        {
            title: '版本',
            dataIndex: 'revision',
            key: 'revision',
            width: 100,
            render: (rev: number) => <Tag color="purple">v{rev}</Tag>,
        },
        {
            title: '执行语义',
            key: 'execution',
            width: 180,
            render: (_: unknown, row: (typeof items)[0]) =>
                row.execution_mode === 'orchestrated_v2' ? (
                    <Space size={[4, 4]} wrap>
                        <Tag color="blue">多模态编排 v2</Tag>
                        <Text type="secondary" style={{ fontSize: 11 }}>
                            图 v{row.orchestration_revision}
                        </Text>
                    </Space>
                ) : (
                    <Tag>兼容草稿</Tag>
                ),
        },
        {
            title: '状态',
            dataIndex: 'state',
            key: 'state',
            width: 120,
            render: (state: string) => <Badge state={state} />,
        },
        {
            title: '保存时间',
            dataIndex: 'created_at',
            key: 'created_at',
            width: 180,
            render: (time: string) => (
                <Text type="secondary" style={{ fontSize: 12 }}>
                    {date(time)}
                </Text>
            ),
        },
        {
            title: '操作',
            key: 'actions',
            width: 140,
            align: 'right' as const,
            render: (_: unknown, row: (typeof items)[0]) => (
                <Space size={8}>
                    <Button
                        size="small"
                        type="link"
                        icon={<EyeOutlined />}
                        onClick={() => setSelectedPipeline(row)}
                        style={{ padding: 0 }}
                    >
                        详情
                    </Button>
                    {row.state === 'draft' ? (
                        <>
                            <Popconfirm
                                title="确定发布此处理方案？发布后不可变，可直接用于任务编排。"
                                onConfirm={() => publish.mutate(row.id)}
                                okText="发布"
                                cancelText="取消"
                            >
                                <Button
                                    size="small"
                                    type="link"
                                    icon={<CheckCircleOutlined />}
                                    loading={publish.isPending}
                                    style={{ padding: 0 }}
                                >
                                    发布
                                </Button>
                            </Popconfirm>
                            <Popconfirm
                                title="确定归档此方案草稿？"
                                onConfirm={() => archive.mutate(row.id)}
                                okText="归档"
                                cancelText="取消"
                            >
                                <Button
                                    size="small"
                                    danger
                                    type="text"
                                    icon={<FolderOutlined />}
                                    loading={archive.isPending}
                                >
                                    归档
                                </Button>
                            </Popconfirm>
                        </>
                    ) : null}
                </Space>
            ),
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="Pipeline Templates"
                title="处理方案管理"
                description="将基础插件、模型配置参数与时序编排固化为可追溯、可审计的方案版本。"
                action={
                    <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        onClick={() => {
                            save.reset();
                            form.resetFields();
                            setValidation(null);
                            void configs.refetch();
                            setOpen(true);
                        }}
                    >
                        新建方案
                    </Button>
                }
            />

            <Notice>
                方案以 OCR 与 ASR 为必需快路径，VLM 为可选慢路径；每秒 coverage 是固定事实网格，
                采样与分段策略会写入不可变 Revision，而不是依赖全局 YAML。
            </Notice>

            <ErrorNotice
                error={
                    listing.error ||
                    configs.error ||
                    archive.error ||
                    publish.error ||
                    validate.error ||
                    save.error
                }
            />

            <Card bodyStyle={{ padding: 0 }}>
                {listing.isPending ? (
                    <Loading tip="正在载入处理方案…" />
                ) : items.length ? (
                    <Table
                        columns={columns}
                        dataSource={items}
                        rowKey="id"
                        pagination={{
                            current: Math.floor(offset / 20) + 1,
                            pageSize: 20,
                            total: totalCount,
                            showTotal: (total) => `共 ${total} 个方案`,
                            onChange: (page) => setOffset((page - 1) * 20),
                        }}
                    />
                ) : !listing.error ? (
                    <Empty title="暂无处理方案">
                        配置好插件参数后，可以在此处新建方案并指定关联的参数模版。
                    </Empty>
                ) : null}
            </Card>

            {selectedPipeline ? (
                <PipelineDetailModal
                    pipeline={selectedPipeline}
                    onClose={() => setSelectedPipeline(null)}
                />
            ) : null}

            {open ? (
                <Modal title="新建多模态处理方案" onClose={() => setOpen(false)} width={680}>
                    <ErrorNotice error={configs.error || validate.error || save.error} />
                    {!configs.isPending && !configs.error && missingConfigs.length ? (
                        <Alert
                            type="info"
                            showIcon
                            message={`${missingConfigs.join('、')} 暂无已保存配置`}
                            description={
                                <>
                                    请到<Link to="/plugins">插件中心</Link>
                                    一键装配或保存插件配置，再返回选择。
                                    已安装插件再次装配会补齐配置，不会重复安装。VLM 为可选项。
                                </>
                            }
                            action={
                                <Button onClick={() => void configs.refetch()}>刷新配置</Button>
                            }
                            style={{ marginBottom: 16 }}
                        />
                    ) : null}
                    <Form
                        form={form}
                        layout="vertical"
                        initialValues={multimodalInitialValues}
                        onValuesChange={() => setValidation(null)}
                        onFinish={saveForm}
                    >
                        <Form.Item
                            name="name"
                            label={<span style={{ fontWeight: 500 }}>方案名称</span>}
                            rules={[{ required: true, message: '请输入方案名称' }]}
                        >
                            <Input placeholder="例如：全模态标准分析流水线" maxLength={120} />
                        </Form.Item>

                        <Form.Item
                            name="description"
                            label={<span style={{ fontWeight: 500 }}>方案描述 (可选)</span>}
                        >
                            <Input.TextArea
                                rows={2}
                                placeholder="输入方案的应用场景或说明"
                                maxLength={400}
                            />
                        </Form.Item>

                        <Row gutter={12}>
                            <Col xs={24} md={12}>
                                <Form.Item
                                    name="ocr_config_id"
                                    label={<span style={{ fontWeight: 500 }}>OCR 快路径配置</span>}
                                    rules={[{ required: true, message: '请选择 OCR 配置' }]}
                                >
                                    <Select
                                        loading={configs.isPending}
                                        placeholder="选择 RapidOCR 配置"
                                        notFoundContent="暂无 OCR 配置，请先在插件中心保存配置"
                                        options={configsByPlugin(MULTIMODAL_PLUGIN_IDS.ocr).map(
                                            (item) => ({
                                                label: `${item.name} · v${item.revision}`,
                                                value: item.id,
                                            }),
                                        )}
                                    />
                                </Form.Item>
                            </Col>
                            <Col xs={24} md={12}>
                                <Form.Item
                                    name="asr_config_id"
                                    label={<span style={{ fontWeight: 500 }}>ASR 快路径配置</span>}
                                    rules={[{ required: true, message: '请选择 ASR 配置' }]}
                                >
                                    <Select
                                        loading={configs.isPending}
                                        placeholder="选择 Whisper 配置"
                                        notFoundContent="暂无 ASR 配置，请先在插件中心保存配置"
                                        options={configsByPlugin(MULTIMODAL_PLUGIN_IDS.asr).map(
                                            (item) => ({
                                                label: `${item.name} · v${item.revision}`,
                                                value: item.id,
                                            }),
                                        )}
                                    />
                                </Form.Item>
                            </Col>
                        </Row>

                        <Form.Item
                            name="vlm_config_id"
                            label={<span style={{ fontWeight: 500 }}>VLM 慢路径配置（可选）</span>}
                            extra="不选择 VLM 时，执行仍会产生完整 coverage；画面描述会明确显示为未配置，而不会伪造结果。"
                        >
                            <Select
                                allowClear
                                loading={configs.isPending}
                                placeholder="可选：选择 Moondream 画面描述配置"
                                notFoundContent="暂无 VLM 配置，可暂不启用画面描述"
                                options={configsByPlugin(MULTIMODAL_PLUGIN_IDS.vlm).map((item) => ({
                                    label: `${item.name} · v${item.revision}`,
                                    value: item.id,
                                }))}
                            />
                        </Form.Item>

                        <Card size="small" title="时间轴与采样策略" style={{ marginBottom: 16 }}>
                            <Row gutter={12}>
                                <Col xs={24} md={8}>
                                    <Form.Item
                                        name={['policy', 'window_ms']}
                                        label="Coverage 窗口（毫秒）"
                                    >
                                        <InputNumber disabled style={{ width: '100%' }} />
                                    </Form.Item>
                                </Col>
                                <Col xs={24} md={8}>
                                    <Form.Item
                                        name={['policy', 'sample_interval_ms']}
                                        label="视频采样最小间隔（毫秒）"
                                        rules={[
                                            {
                                                required: true,
                                                type: 'number',
                                                min: 250,
                                                max: 60000,
                                            },
                                        ]}
                                    >
                                        <InputNumber
                                            min={250}
                                            max={60000}
                                            step={250}
                                            style={{ width: '100%' }}
                                        />
                                    </Form.Item>
                                </Col>
                                <Col xs={24} md={8}>
                                    <Form.Item
                                        name={['policy', 'audio_segment_ms']}
                                        label="ASR 音频切段（毫秒）"
                                        rules={[
                                            {
                                                required: true,
                                                type: 'number',
                                                min: 1000,
                                                max: 60000,
                                            },
                                        ]}
                                    >
                                        <InputNumber
                                            min={1000}
                                            max={60000}
                                            step={1000}
                                            style={{ width: '100%' }}
                                        />
                                    </Form.Item>
                                </Col>
                                <Col xs={24} md={12}>
                                    <Form.Item
                                        name={['policy', 'audio_overlap_ms']}
                                        label="ASR 重叠（毫秒）"
                                    >
                                        <InputNumber disabled style={{ width: '100%' }} />
                                    </Form.Item>
                                </Col>
                                <Col xs={24} md={12}>
                                    <Form.Item
                                        name={['policy', 'vlm_sample_interval_ms']}
                                        label="VLM 采样间隔（毫秒）"
                                        rules={[
                                            {
                                                required: true,
                                                type: 'number',
                                                min: 1000,
                                                max: 60000,
                                            },
                                        ]}
                                    >
                                        <InputNumber
                                            min={1000}
                                            max={60000}
                                            step={1000}
                                            style={{ width: '100%' }}
                                        />
                                    </Form.Item>
                                </Col>
                            </Row>
                            <Text type="secondary" style={{ fontSize: 12 }}>
                                Coverage 窗口固定为 1 秒，音频切段重叠为 500
                                毫秒；这些策略会随方案版本保存。
                            </Text>
                        </Card>

                        {validation ? (
                            <Alert
                                type={validation.valid ? 'success' : 'error'}
                                showIcon
                                message={validation.valid ? '编排校验通过' : '编排校验未通过'}
                                description={
                                    validation.valid
                                        ? `图摘要：${validation.graph_digest}；执行顺序：${validation.topological_order.join(' → ')}`
                                        : validation.errors.join('；')
                                }
                                style={{ marginBottom: 16 }}
                            />
                        ) : null}

                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'flex-end',
                                gap: 10,
                                marginTop: 24,
                            }}
                        >
                            <Button onClick={() => setOpen(false)}>取消</Button>
                            <Button
                                loading={validate.isPending}
                                onClick={() => void validateForm()}
                            >
                                校验方案
                            </Button>
                            <Button
                                type="primary"
                                htmlType="submit"
                                loading={save.isPending}
                                disabled={!validation?.valid}
                            >
                                保存不可变方案
                            </Button>
                        </div>
                    </Form>
                </Modal>
            ) : null}
        </div>
    );
}

/**
 * 访问凭据管理 (Access Tokens)
 */
export function Access() {
    const cache = useQueryClient();
    const session = useSession();
    const [open, setOpen] = useState(false);
    const [secret, setSecret] = useState('');
    const [copied, setCopied] = useState(false);
    const [form] = Form.useForm();

    const query = useQuery({
        queryKey: ['tokens'],
        queryFn: ({ signal }) => api<TokenList>('/auth/v1/access-tokens', { signal }),
    });

    const save = useMutation({
        mutationFn: (values: { name: string; days: number; scopes: string[] }) =>
            post<AccessToken>('/auth/v1/access-tokens', {
                name: values.name,
                expires_in_days: Number(values.days || 30),
                scopes: values.scopes || [],
            }),
        onSuccess: (result) => {
            message.success('访问凭据创建成功');
            setSecret(result.token);
            setOpen(false);
            form.resetFields();
            void cache.invalidateQueries({ queryKey: ['tokens'] });
        },
    });

    const revoke = useMutation({
        mutationFn: (id: string) => post(`/auth/v1/access-tokens/${id}:revoke`),
        onSuccess: () => {
            message.success('已撤销该访问凭据');
            void cache.invalidateQueries({ queryKey: ['tokens'] });
        },
    });

    const availableScopes = session.permissions.filter((x) => /^(assets|materials|jobs):/.test(x));

    const copySecret = () => {
        void navigator.clipboard.writeText(secret);
        setCopied(true);
        message.success('密钥已复制到剪贴板');
        setTimeout(() => setCopied(false), 2000);
    };

    const tokenList = query.data?.items || [];

    const columns = [
        {
            title: '凭据名称',
            dataIndex: 'name',
            key: 'name',
            render: (name: string, row: (typeof tokenList)[0]) => (
                <Space size={8}>
                    <KeyOutlined style={{ color: '#1668dc' }} />
                    <Text strong style={{ fontSize: 13 }}>
                        {name}
                    </Text>
                    <span className="mono" style={{ fontSize: 11, color: '#94a3b8' }}>
                        ({row.id.slice(0, 16)}…)
                    </span>
                </Space>
            ),
        },
        {
            title: '授权范围 (Scopes)',
            dataIndex: 'scopes',
            key: 'scopes',
            render: (scopes: string[]) => (
                <Space size={[4, 4]} wrap>
                    {scopes?.map((s) => (
                        <Tag key={s} color="blue" style={{ fontSize: 11 }}>
                            {s}
                        </Tag>
                    ))}
                </Space>
            ),
        },
        {
            title: '过期时间',
            dataIndex: 'expires_at',
            key: 'expires_at',
            width: 180,
            render: (time: string) => (
                <Text type="secondary" style={{ fontSize: 12 }}>
                    {date(time)}
                </Text>
            ),
        },
        {
            title: '操作',
            key: 'actions',
            width: 100,
            align: 'right' as const,
            render: (_: unknown, row: (typeof tokenList)[0]) => (
                <Popconfirm
                    title="确定撤销此访问凭据？撤销后外部应用将无法再使用该密钥调用 API。"
                    onConfirm={() => revoke.mutate(row.id)}
                    okText="撤销"
                    cancelText="取消"
                >
                    <Button
                        size="small"
                        danger
                        type="text"
                        icon={<StopOutlined />}
                        loading={revoke.isPending}
                    >
                        撤销
                    </Button>
                </Popconfirm>
            ),
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="Access Control"
                title="访问凭据 (API Tokens)"
                description="为 Agent 智能体、自动化脚本及外部客户端颁发可细粒度撤销的访问令牌。"
                action={
                    <Button
                        type="primary"
                        icon={<PlusOutlined />}
                        disabled={!availableScopes.length}
                        onClick={() => {
                            save.reset();
                            form.resetFields();
                            setOpen(true);
                        }}
                    >
                        创建访问凭据
                    </Button>
                }
            />

            <ErrorNotice error={query.error || revoke.error} />

            {secret ? (
                <Alert
                    type="warning"
                    showIcon
                    message="请立即保存凭据密钥，它仅在此刻展示一次！"
                    description={
                        <div style={{ marginTop: 8 }}>
                            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                                <code
                                    className="mono"
                                    style={{
                                        flex: 1,
                                        padding: '8px 12px',
                                        fontSize: 13,
                                        background: '#ffffff',
                                    }}
                                >
                                    {secret}
                                </code>
                                <Button
                                    type="primary"
                                    icon={copied ? <CheckOutlined /> : <CopyOutlined />}
                                    onClick={copySecret}
                                >
                                    {copied ? '已复制' : '复制密钥'}
                                </Button>
                            </div>
                        </div>
                    }
                    style={{ marginBottom: 10 }}
                />
            ) : null}

            <Card bodyStyle={{ padding: 0 }}>
                {query.isPending ? (
                    <Loading tip="正在载入凭据列表…" />
                ) : tokenList.length ? (
                    <Table
                        columns={columns}
                        dataSource={tokenList}
                        rowKey="id"
                        pagination={{ pageSize: 15 }}
                    />
                ) : (
                    <Empty title="暂无生效的访问凭据">
                        点击右上角【创建访问凭据】为外部程序或自动化任务生成 API Token。
                    </Empty>
                )}
            </Card>

            {open ? (
                <Modal title="创建新访问凭据" onClose={() => setOpen(false)} width={520}>
                    <Form form={form} layout="vertical" onFinish={(values) => save.mutate(values)}>
                        <Form.Item
                            name="name"
                            label={<span style={{ fontWeight: 500 }}>凭据名称 / 客户端说明</span>}
                            rules={[{ required: true, message: '请输入凭据名称' }]}
                        >
                            <Input placeholder="例如：CI 任务调用、外部分析 Agent" maxLength={64} />
                        </Form.Item>

                        <Form.Item
                            name="days"
                            label={<span style={{ fontWeight: 500 }}>有效天数</span>}
                            initialValue={30}
                        >
                            <InputNumber min={1} max={365} style={{ width: '100%' }} />
                        </Form.Item>

                        <Form.Item
                            name="scopes"
                            label={<span style={{ fontWeight: 500 }}>授权权限范围 (Scopes)</span>}
                            rules={[{ required: true, message: '请至少勾选一项权限' }]}
                            initialValue={availableScopes.slice(0, 1)}
                        >
                            <Checkbox.Group
                                style={{ display: 'flex', flexDirection: 'column', gap: 8 }}
                            >
                                {availableScopes.map((scope) => (
                                    <Checkbox key={scope} value={scope}>
                                        <span className="mono">{scope}</span>
                                    </Checkbox>
                                ))}
                            </Checkbox.Group>
                        </Form.Item>

                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'flex-end',
                                gap: 10,
                                marginTop: 24,
                            }}
                        >
                            <Button onClick={() => setOpen(false)}>取消</Button>
                            <Button type="primary" htmlType="submit" loading={save.isPending}>
                                确定生成
                            </Button>
                        </div>
                    </Form>
                </Modal>
            ) : null}
        </div>
    );
}

/**
 * 用户与权限管理 (Users)
 */
export function Users() {
    const cache = useQueryClient();
    const canManage = usePermission('users:manage');
    const [open, setOpen] = useState(false);
    const [editing, setEditing] = useState<User | null>(null);
    const [addForm] = Form.useForm();
    const [editForm] = Form.useForm();

    const query = useQuery({
        queryKey: ['users'],
        queryFn: ({ signal }) => api<UserList>('/admin/v1/users', { signal }),
    });

    const save = useMutation({
        mutationFn: (values: {
            username: string;
            display_name: string;
            password?: string;
            roles: string[];
        }) =>
            post('/admin/v1/users', {
                username: values.username,
                display_name: values.display_name,
                password: values.password,
                roles: values.roles || ['viewer'],
            }),
        onSuccess: () => {
            message.success('用户创建成功');
            setOpen(false);
            addForm.resetFields();
            void cache.invalidateQueries({ queryKey: ['users'] });
        },
    });

    const update = useMutation({
        mutationFn: (values: {
            display_name: string;
            password?: string;
            roles: string[];
            disabled?: boolean;
        }) =>
            post(`/admin/v1/users/${editing!.username}`, {
                display_name: values.display_name,
                password: values.password || undefined,
                roles: values.roles || ['viewer'],
                disabled: !!values.disabled,
            }),
        onSuccess: () => {
            message.success('用户配置已更新');
            setEditing(null);
            editForm.resetFields();
            void cache.invalidateQueries({ queryKey: ['users'] });
        },
    });

    const userList = query.data?.items || [];

    const roleNameMap: Record<string, string> = {
        admin: '平台管理员',
        operator: '业务操作员',
        viewer: '素材查看者',
    };

    const columns = [
        {
            title: '用户',
            dataIndex: 'display_name',
            key: 'display_name',
            render: (name: string, row: User) => (
                <Space size={10} align="center">
                    <div
                        style={{
                            width: 34,
                            height: 34,
                            borderRadius: '50%',
                            background: '#eff6ff',
                            color: '#1668dc',
                            display: 'grid',
                            placeItems: 'center',
                            fontSize: 15,
                        }}
                    >
                        <UserOutlined />
                    </div>
                    <div>
                        <Text strong style={{ fontSize: 13 }}>
                            {name}
                        </Text>
                        <div style={{ fontSize: 12, color: '#64748b' }}>@{row.username}</div>
                    </div>
                </Space>
            ),
        },
        {
            title: '角色与权限组',
            dataIndex: 'roles',
            key: 'roles',
            render: (roles: string[]) => (
                <Space size={[4, 4]} wrap>
                    {roles.map((r) => (
                        <Tag
                            key={r}
                            color={
                                r === 'admin' ? 'magenta' : r === 'operator' ? 'blue' : 'default'
                            }
                        >
                            {roleNameMap[r] || r}
                        </Tag>
                    ))}
                </Space>
            ),
        },
        {
            title: '账号状态',
            dataIndex: 'disabled',
            key: 'disabled',
            width: 120,
            render: (disabled: boolean) => (
                <Tag color={disabled ? 'error' : 'success'}>{disabled ? '已停用' : '正常'}</Tag>
            ),
        },
        {
            title: '创建时间',
            dataIndex: 'created_at',
            key: 'created_at',
            width: 180,
            render: (time: string) => (
                <Text type="secondary" style={{ fontSize: 12 }}>
                    {date(time)}
                </Text>
            ),
        },
        {
            title: '操作',
            key: 'actions',
            width: 100,
            align: 'right' as const,
            render: (_: unknown, row: User) =>
                canManage ? (
                    <Button
                        size="small"
                        type="link"
                        icon={<EditOutlined />}
                        onClick={() => {
                            setEditing(row);
                            editForm.setFieldsValue({
                                display_name: row.display_name,
                                roles: row.roles,
                                disabled: row.disabled,
                            });
                        }}
                    >
                        编辑
                    </Button>
                ) : null,
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="User & Permissions"
                title="用户与权限管理"
                description="节点用户 RBAC 角色分配与认证安全，支持管理员、操作员与审计人员的多角色权限隔离。"
                action={
                    canManage ? (
                        <Button
                            type="primary"
                            icon={<PlusOutlined />}
                            onClick={() => {
                                save.reset();
                                addForm.resetFields();
                                setOpen(true);
                            }}
                        >
                            创建新用户
                        </Button>
                    ) : null
                }
            />

            <ErrorNotice error={query.error || save.error || update.error} />

            <Card bodyStyle={{ padding: 0 }}>
                {query.isPending ? (
                    <Loading tip="正在载入系统用户…" />
                ) : userList.length ? (
                    <Table
                        columns={columns}
                        dataSource={userList}
                        rowKey="username"
                        pagination={false}
                    />
                ) : null}
            </Card>

            {/* 新建用户 Modal */}
            {open ? (
                <Modal title="创建新系统用户" onClose={() => setOpen(false)} width={500}>
                    <Form
                        form={addForm}
                        layout="vertical"
                        onFinish={(values) => save.mutate(values)}
                    >
                        <Form.Item
                            name="display_name"
                            label={<span style={{ fontWeight: 500 }}>显示姓名</span>}
                            rules={[{ required: true, message: '请输入显示姓名' }]}
                        >
                            <Input placeholder="例如：张工" maxLength={120} />
                        </Form.Item>

                        <Form.Item
                            name="username"
                            label={<span style={{ fontWeight: 500 }}>账户登录名</span>}
                            rules={[
                                { required: true, message: '请输入用户名' },
                                {
                                    pattern: /^[a-zA-Z0-9_.-]{3,64}$/,
                                    message: '3–64 位字母、数字或 . _ -',
                                },
                            ]}
                        >
                            <Input placeholder="例如：zhang_san" />
                        </Form.Item>

                        <Form.Item
                            name="password"
                            label={<span style={{ fontWeight: 500 }}>初始登录密码</span>}
                            rules={[
                                { required: true, message: '请输入初始密码' },
                                { min: 12, message: '初始密码至少 12 位' },
                            ]}
                        >
                            <Input.Password placeholder="至少 12 位高强度密码" />
                        </Form.Item>

                        <Form.Item
                            name="roles"
                            label={<span style={{ fontWeight: 500 }}>分配系统角色</span>}
                            initialValue={['viewer']}
                        >
                            <Checkbox.Group
                                style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
                            >
                                <Checkbox value="viewer">素材查看者 (只读查看视频与素材)</Checkbox>
                                <Checkbox value="operator">
                                    业务操作员 (上传视频、下发处理任务)
                                </Checkbox>
                                <Checkbox value="admin">
                                    平台管理员 (纳管计算节点、插件与系统配置)
                                </Checkbox>
                            </Checkbox.Group>
                        </Form.Item>

                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'flex-end',
                                gap: 10,
                                marginTop: 20,
                            }}
                        >
                            <Button onClick={() => setOpen(false)}>取消</Button>
                            <Button type="primary" htmlType="submit" loading={save.isPending}>
                                创建用户
                            </Button>
                        </div>
                    </Form>
                </Modal>
            ) : null}

            {/* 编辑用户 Modal */}
            {editing ? (
                <Modal
                    title={`编辑用户 · @${editing.username}`}
                    onClose={() => setEditing(null)}
                    width={500}
                >
                    <Form
                        form={editForm}
                        layout="vertical"
                        onFinish={(values) => update.mutate(values)}
                    >
                        <Form.Item
                            name="display_name"
                            label={<span style={{ fontWeight: 500 }}>显示姓名</span>}
                            rules={[{ required: true, message: '请输入显示姓名' }]}
                        >
                            <Input maxLength={120} />
                        </Form.Item>

                        <Form.Item
                            name="password"
                            label={<span style={{ fontWeight: 500 }}>重设密码 (可选)</span>}
                        >
                            <Input.Password placeholder="留空则保持当前登录密码不变" />
                        </Form.Item>

                        <Form.Item
                            name="roles"
                            label={<span style={{ fontWeight: 500 }}>角色与权限</span>}
                        >
                            <Checkbox.Group
                                style={{ display: 'flex', flexDirection: 'column', gap: 6 }}
                            >
                                <Checkbox value="viewer">素材查看者</Checkbox>
                                <Checkbox value="operator">业务操作员</Checkbox>
                                <Checkbox value="admin">平台管理员</Checkbox>
                            </Checkbox.Group>
                        </Form.Item>

                        <Form.Item name="disabled" valuePropName="checked">
                            <Checkbox>
                                <span style={{ color: '#ef4444', fontWeight: 500 }}>
                                    停用此账户 (立即撤销所有会话与访问凭据)
                                </span>
                            </Checkbox>
                        </Form.Item>

                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'flex-end',
                                gap: 10,
                                marginTop: 20,
                            }}
                        >
                            <Button onClick={() => setEditing(null)}>取消</Button>
                            <Button type="primary" htmlType="submit" loading={update.isPending}>
                                保存修改
                            </Button>
                        </div>
                    </Form>
                </Modal>
            ) : null}
        </div>
    );
}

/**
 * 操作审计 (Audit Trail)
 */
export function Audit() {
    const [offset, setOffset] = useState(0);

    const query = useQuery({
        queryKey: ['audit', offset],
        queryFn: ({ signal }) =>
            api<AuditList>(`/admin/v1/audit-events?limit=20&offset=${offset}`, { signal }),
    });

    const items = query.data?.items || [];
    const totalCount = query.data?.total || 0;

    const columns = [
        {
            title: '时间',
            dataIndex: 'created_at',
            key: 'created_at',
            width: 180,
            render: (time: string) => (
                <Text type="secondary" style={{ fontSize: 12 }}>
                    {date(time)}
                </Text>
            ),
        },
        {
            title: '操作者',
            dataIndex: 'actor',
            key: 'actor',
            width: 160,
            render: (actor: string) => <Text strong>{actor}</Text>,
        },
        {
            title: '动作类型',
            dataIndex: 'action',
            key: 'action',
            width: 200,
            render: (action: string) => <Tag color="blue">{action}</Tag>,
        },
        {
            title: '操作目标 / 资源',
            dataIndex: 'target',
            key: 'target',
            render: (target: string) => <span className="mono">{target}</span>,
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="Audit Trail"
                title="操作审计日志"
                description="全量留痕配置下发、身份授权与数据资产流转操作，严格脱敏保证零业务介质与密钥泄漏。"
            />

            <ErrorNotice error={query.error} />

            <Card bodyStyle={{ padding: 0 }}>
                {query.isPending ? (
                    <Loading tip="正在载入审计事件…" />
                ) : items.length ? (
                    <Table
                        columns={columns}
                        dataSource={items}
                        rowKey="id"
                        pagination={{
                            current: Math.floor(offset / 20) + 1,
                            pageSize: 20,
                            total: totalCount,
                            showTotal: (total) => `共 ${total} 条审计日志`,
                            onChange: (page) => setOffset((page - 1) * 20),
                        }}
                    />
                ) : !query.error ? (
                    <Empty title="暂无操作审计日志" />
                ) : null}
            </Card>
        </div>
    );
}

