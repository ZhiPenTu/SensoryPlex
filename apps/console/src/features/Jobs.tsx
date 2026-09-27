import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Button,
    Card,
    Col,
    Form,
    Input,
    Popconfirm,
    Row,
    Select,
    Space,
    Table,
    Tag,
    Typography,
} from 'antd';
import {
    ScheduleOutlined,
    PlusOutlined,
    CaretRightOutlined,
    ArrowRightOutlined,
    FolderOutlined,
    CheckCircleOutlined,
    SyncOutlined,
    CloseCircleOutlined,
} from '@ant-design/icons';
import { Link } from 'react-router-dom';
import { api, post } from '../api/client';
import type { JobDraft, JobDraftList, JsonObject, PipelineList, UploadList } from '../api/contracts';
import {
    Badge,
    bytes,
    date,
    Empty,
    ErrorNotice,
    Heading,
    Loading,
    Modal,
    Notice,
    StatSummary,
} from '../components';
import { usePermission } from '../session';

const { Text } = Typography;

type ExecutionTask = {
    task_id: string;
    node_id: string;
    attempt: number;
    max_attempts: number;
    required: boolean;
    state: string;
    reason_code?: string;
    output_ref?: string;
};

type ExecutionReceipt = {
    task_id: string;
    attempt: number;
    plugin_id: string;
    input_count: number;
    output_count: number;
    reason_code?: string;
    receipt_digest: string;
    completed_at?: string;
};

type ExecutionDetail = {
    execution: {
        execution_id: string;
        run_id: string;
        state: string;
        pipeline_revision: number;
        graph_digest: string;
        modality_summary?: JsonObject;
    };
    tasks: ExecutionTask[];
    receipts: ExecutionReceipt[];
};

function taskCountTags(summary?: JsonObject) {
    const counts = summary?.counts;
    if (!counts || typeof counts !== 'object' || Array.isArray(counts)) return null;
    return Object.entries(counts).map(([state, value]) => (
        <Tag key={state} style={{ marginInlineEnd: 2 }}>
            {state}: {String(value)}
        </Tag>
    ));
}

function ExecutionDetails({ job, onClose }: { job: JobDraft; onClose: () => void }) {
    const detail = useQuery({
        queryKey: ['job-execution', job.id, job.execution_id],
        enabled: !!job.execution_id,
        queryFn: ({ signal }) =>
            api<ExecutionDetail>(
                `/v1/jobs/${encodeURIComponent(job.id)}/execution?execution_id=${encodeURIComponent(job.execution_id)}`,
                { signal },
            ),
        refetchInterval: (query) =>
            query.state.data?.execution.state === 'running' ? 3000 : false,
    });

    const data = detail.data;
    return (
        <Modal title="编排执行详情" onClose={onClose} width={920}>
            <ErrorNotice error={detail.error} />
            {detail.isPending ? (
                <Loading tip="正在读取不可变 Revision、任务和执行回执…" />
            ) : data ? (
                <Space direction="vertical" size={14} style={{ width: '100%' }}>
                    <Card size="small">
                        <Row gutter={[12, 8]}>
                            <Col xs={24} md={12}>
                                <Text type="secondary">执行 ID</Text>
                                <div className="mono" style={{ fontSize: 12, overflowWrap: 'anywhere' }}>
                                    {data.execution.execution_id}
                                </div>
                            </Col>
                            <Col xs={24} md={12}>
                                <Text type="secondary">Run / Revision</Text>
                                <div className="mono" style={{ fontSize: 12, overflowWrap: 'anywhere' }}>
                                    {data.execution.run_id} · 图 v{data.execution.pipeline_revision}
                                </div>
                            </Col>
                            <Col xs={24} md={12}>
                                <Text type="secondary">执行状态</Text>
                                <div>
                                    <Badge state={data.execution.state} />
                                </div>
                            </Col>
                            <Col xs={24} md={12}>
                                <Text type="secondary">图摘要</Text>
                                <div className="mono" style={{ fontSize: 11, overflowWrap: 'anywhere' }}>
                                    {data.execution.graph_digest}
                                </div>
                            </Col>
                        </Row>
                    </Card>

                    {data.execution.state === 'succeeded_with_partial_enrichment' ? (
                        <Notice>
                            必需的 OCR / ASR 与 Timeline 已成功；可选 VLM 慢路径未完成。素材仍可检索，
                            但画面描述应按缺失状态解读，不能当作已补全结果。
                        </Notice>
                    ) : null}

                    <Card
                        size="small"
                        title={`任务事实（${data.tasks.length}）`}
                        extra={<Space size={[2, 2]} wrap>{taskCountTags(data.execution.modality_summary)}</Space>}
                        bodyStyle={{ padding: 0 }}
                    >
                        <Table
                            size="small"
                            rowKey={(task) => `${task.task_id}:${task.attempt}`}
                            pagination={false}
                            dataSource={data.tasks}
                            columns={[
                                { title: '节点', dataIndex: 'node_id', key: 'node_id' },
                                {
                                    title: '要求',
                                    dataIndex: 'required',
                                    key: 'required',
                                    width: 90,
                                    render: (required: boolean) => (
                                        <Tag color={required ? 'blue' : 'default'}>
                                            {required ? '必需' : '慢路径'}
                                        </Tag>
                                    ),
                                },
                                {
                                    title: '尝试',
                                    key: 'attempt',
                                    width: 100,
                                    render: (_: unknown, task: ExecutionTask) =>
                                        `${task.attempt}/${task.max_attempts}`,
                                },
                                {
                                    title: '状态',
                                    dataIndex: 'state',
                                    key: 'state',
                                    render: (state: string) => (
                                        <Badge state={state === 'pending' ? 'task_pending' : state} />
                                    ),
                                },
                                {
                                    title: '原因',
                                    dataIndex: 'reason_code',
                                    key: 'reason_code',
                                    render: (reason?: string) => reason || '—',
                                },
                            ]}
                        />
                    </Card>

                    <Card size="small" title={`不可变执行回执（${data.receipts.length}）`} bodyStyle={{ padding: 0 }}>
                        <Table
                            size="small"
                            rowKey={(receipt) => `${receipt.task_id}:${receipt.attempt}`}
                            pagination={false}
                            dataSource={data.receipts}
                            columns={[
                                { title: '插件', dataIndex: 'plugin_id', key: 'plugin_id' },
                                {
                                    title: '输入 / 输出',
                                    key: 'io',
                                    width: 120,
                                    render: (_: unknown, receipt: ExecutionReceipt) =>
                                        `${receipt.input_count} / ${receipt.output_count}`,
                                },
                                {
                                    title: '结果',
                                    dataIndex: 'reason_code',
                                    key: 'reason_code',
                                    render: (reason?: string) => reason || '成功',
                                },
                                {
                                    title: '摘要',
                                    dataIndex: 'receipt_digest',
                                    key: 'receipt_digest',
                                    render: (digest: string) => (
                                        <span className="mono" style={{ fontSize: 11 }}>
                                            {digest.slice(0, 22)}…
                                        </span>
                                    ),
                                },
                            ]}
                        />
                    </Card>
                </Space>
            ) : null}
        </Modal>
    );
}

export default function Jobs() {
    const cache = useQueryClient();
    const [offset, setOffset] = useState(0);
    const [open, setOpen] = useState(false);
    const [executionJob, setExecutionJob] = useState<JobDraft | null>(null);
    const [form] = Form.useForm();
    const canWrite = usePermission('jobs:write');

    const query = useQuery({
        queryKey: ['jobs', offset],
        queryFn: ({ signal }) =>
            api<JobDraftList>(`/v1/job-drafts?limit=20&offset=${offset}`, { signal }),
        refetchInterval: (q) => {
            const hasProcessing = q.state.data?.items?.some((x) => x.state === 'processing');
            return hasProcessing ? 3000 : 15000;
        },
    });

    const options = useQuery({
        queryKey: ['job-options'],
        enabled: open,
        queryFn: async ({ signal }) => {
            const [assets, pipelines] = await Promise.all([
                api<UploadList>('/v1/assets?limit=100', { signal }),
                api<PipelineList>('/v1/pipelines?limit=100', { signal }),
            ]);
            return {
                assets: assets.items.filter((x) => x.state === 'awaiting_admission'),
                pipelines: pipelines.items.filter(
                    (x) => x.state === 'draft' || x.state === 'published',
                ),
            };
        },
    });

    const save = useMutation({
        mutationFn: (values: { name: string; asset_id: string; pipeline_id: string }) =>
            post('/v1/job-drafts', values),
        onSuccess: () => {
            setOpen(false);
            form.resetFields();
            void cache.invalidateQueries({ queryKey: ['jobs'] });
        },
    });

    const dispatchMutation = useMutation({
        mutationFn: (id: string) => post(`/v1/job-drafts/${id}:dispatch`),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['jobs'] });
            void cache.invalidateQueries({ queryKey: ['materials'] });
        },
    });

    const archive = useMutation({
        mutationFn: (id: string) => post(`/v1/job-drafts/${id}:archive`),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['jobs'] });
        },
    });

    const items = query.data?.items || [];
    const totalCount = query.data?.total || 0;
    const processingCount = items.filter((x) => x.state === 'processing').length;
    const completedCount = items.filter((x) => x.state === 'completed').length;
    const failedCount = items.filter((x) => x.state === 'failed').length;

    const columns = [
        {
            title: '任务名称',
            dataIndex: 'name',
            key: 'name',
            render: (name: string, item: (typeof items)[0]) => (
                <Space align="start" size={12}>
                    <div
                        style={{
                            width: 28,
                            height: 28,
                            borderRadius: 5,
                            background: '#f8fafc',
                            border: '1px solid #e2e8f0',
                            color: '#475569',
                            display: 'grid',
                            placeItems: 'center',
                            fontSize: 13,
                            flexShrink: 0,
                            marginTop: 2,
                        }}
                    >
                        <ScheduleOutlined />
                    </div>
                    <div>
                        <Text
                            strong
                            style={{ fontSize: 13, display: 'block', maxWidth: 360 }}
                            ellipsis={{ tooltip: name }}
                        >
                            {name}
                        </Text>
                        <div style={{ marginTop: 2 }}>
                            <span className="mono" style={{ fontSize: 11, color: '#64748b' }}>
                                ID: {item.id.slice(0, 18)}…
                            </span>
                        </div>
                        {item.reason ? (
                            <div style={{ marginTop: 4 }}>
                                <Text type="danger" style={{ fontSize: 11 }}>
                                    失败原因: {item.reason}
                                </Text>
                            </div>
                        ) : null}
                    </div>
                </Space>
            ),
        },
        {
            title: '调度状态',
            dataIndex: 'state',
            key: 'state',
            width: 140,
            render: (state: string) => <Badge state={state} />,
        },
        {
            title: '编排执行',
            key: 'execution',
            width: 210,
            render: (_: unknown, item: (typeof items)[0]) =>
                item.execution_id ? (
                    <Space direction="vertical" size={2}>
                        <Space size={[4, 4]} wrap>
                            <Tag color="blue">编排 v2</Tag>
                            <Badge state={item.execution_state || 'running'} />
                        </Space>
                        <Text type="secondary" style={{ fontSize: 11 }}>
                            图 v{item.pipeline_revision} · {item.run_id.slice(0, 16)}…
                        </Text>
                        <Space size={[2, 2]} wrap>
                            {taskCountTags(item.modality_summary)}
                        </Space>
                    </Space>
                ) : (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                        兼容草稿（不可执行）
                    </Text>
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
            width: 340,
            align: 'right' as const,
            render: (_: unknown, item: (typeof items)[0]) => (
                <Space size={8}>
                    {canWrite && (item.state === 'draft' || item.state === 'failed') ? (
                        <Button
                            size="small"
                            type="primary"
                            icon={<CaretRightOutlined />}
                            loading={dispatchMutation.isPending}
                            onClick={() => dispatchMutation.mutate(item.id)}
                        >
                            开始处理
                        </Button>
                    ) : null}

                    {item.state === 'processing' ? (
                        <Button size="small" disabled icon={<SyncOutlined spin />}>
                            {item.execution_id ? '正在执行…' : '正在处理…'}
                        </Button>
                    ) : null}

                    {item.execution_id ? (
                        <Button size="small" onClick={() => setExecutionJob(item)}>
                            执行详情
                        </Button>
                    ) : null}

                    {item.state === 'completed' ? (
                        <Link
                            to={
                                item.execution_id
                                    ? `/materials?execution=${encodeURIComponent(item.execution_id)}`
                                    : '/materials'
                            }
                        >
                            <Button size="small" type="default" icon={<ArrowRightOutlined />}>
                                查看素材
                            </Button>
                        </Link>
                    ) : null}

                    {canWrite &&
                    (item.state === 'draft' ||
                        item.state === 'completed' ||
                        item.state === 'failed') ? (
                        <Popconfirm
                            title="确定归档此任务？"
                            onConfirm={() => archive.mutate(item.id)}
                            okText="归档"
                            cancelText="取消"
                        >
                            <Button
                                size="small"
                                type="text"
                                danger
                                icon={<FolderOutlined />}
                                loading={archive.isPending}
                            >
                                归档
                            </Button>
                        </Popconfirm>
                    ) : null}
                </Space>
            ),
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="Task Processing"
                title="处理任务"
                description="向计算节点提交视频分析任务，并展示可核验的执行状态。"
                action={
                    canWrite ? (
                        <Button
                            type="primary"
                            icon={<PlusOutlined />}
                            onClick={() => {
                                save.reset();
                                form.resetFields();
                                setOpen(true);
                            }}
                        >
                            新建任务草稿
                        </Button>
                    ) : null
                }
            />

            <Row gutter={[8, 8]} style={{ marginBottom: 10 }}>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="任务总数"
                        value={totalCount}
                        prefix={<ScheduleOutlined style={{ color: '#1668dc' }} />}
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="当前页处理中"
                        value={processingCount}
                        prefix={<SyncOutlined spin style={{ color: '#3b82f6' }} />}
                        color="#3b82f6"
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="当前页已完成"
                        value={completedCount}
                        prefix={<CheckCircleOutlined style={{ color: '#10b981' }} />}
                        color="#10b981"
                    />
                </Col>
                <Col xs={24} sm={6}>
                    <StatSummary
                        title="当前页失败"
                        value={failedCount}
                        prefix={<CloseCircleOutlined style={{ color: '#ef4444' }} />}
                        color="#ef4444"
                    />
                </Col>
            </Row>

            <Notice>
                多模态方案会先执行 OCR / ASR，再由 Timeline 写入每秒 coverage；VLM 是可选慢路径。
                任务完成必须已有可核验的 Task receipt 与 coverage，缺失任一事实会明确失败，不会伪装成完成。
            </Notice>

            <ErrorNotice error={query.error || archive.error || dispatchMutation.error} />

            <Card
                title={
                    <Space size={8}>
                        <span style={{ fontWeight: 650, fontSize: 15 }}>任务调度执行队列</span>
                        <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                            支持状态动态轮询与自动流转
                        </Text>
                    </Space>
                }
                bodyStyle={{ padding: 0 }}
            >
                {query.isPending ? (
                    <Loading tip="正在载入任务队列…" />
                ) : items.length ? (
                    <Table
                        columns={columns}
                        dataSource={items}
                        rowKey="id"
                        pagination={{
                            current: Math.floor(offset / 20) + 1,
                            pageSize: 20,
                            total: totalCount,
                            showTotal: (total) => `共 ${total} 项任务`,
                            onChange: (page) => setOffset((page - 1) * 20),
                        }}
                    />
                ) : !query.error ? (
                    <Empty title="暂无处理任务">
                        在视频库导入视频并配置处理方案后，可以创建任务草稿并派发执行。
                    </Empty>
                ) : null}
            </Card>

            {open ? (
                <Modal title="新建任务草稿" onClose={() => setOpen(false)} width={580}>
                    <ErrorNotice error={save.error || options.error} />

                    {options.isPending ? (
                        <Loading tip="正在载入关联配置…" />
                    ) : (
                        <Form
                            form={form}
                            layout="vertical"
                            onFinish={(values) => save.mutate(values)}
                        >
                            <Form.Item
                                name="name"
                                label={<span style={{ fontWeight: 500 }}>任务名称</span>}
                                rules={[{ required: true, message: '请输入任务名称' }]}
                            >
                                <Input
                                    autoFocus
                                    maxLength={120}
                                    placeholder="例如：产品演示视频多模态解析"
                                />
                            </Form.Item>

                            <Form.Item
                                name="asset_id"
                                label={<span style={{ fontWeight: 500 }}>待处理视频母带</span>}
                                rules={[{ required: true, message: '请选择视频' }]}
                            >
                                <Select
                                    placeholder="选择已在视频库上传的视频"
                                    options={options.data?.assets.map((x) => ({
                                        label: `${x.filename} (${bytes(x.size_bytes)})`,
                                        value: x.id,
                                    }))}
                                    notFoundContent={
                                        <div style={{ padding: 12, textAlign: 'center' }}>
                                            暂无待准入视频，请先在视频库导入
                                        </div>
                                    }
                                />
                            </Form.Item>

                            <Form.Item
                                name="pipeline_id"
                                label={<span style={{ fontWeight: 500 }}>处理方案 (Pipeline)</span>}
                                rules={[{ required: true, message: '请选择处理方案' }]}
                            >
                                <Select
                                    placeholder="选择启用的处理方案"
                                    options={options.data?.pipelines.map((x) => ({
                                        label: `${x.name} · v${x.revision} [${x.state}]`,
                                        value: x.id,
                                    }))}
                                    notFoundContent={
                                        <div style={{ padding: 12, textAlign: 'center' }}>
                                            暂无可用方案，请先在管理区配置
                                        </div>
                                    }
                                />
                            </Form.Item>

                            <div
                                style={{
                                    fontSize: 12,
                                    color: '#64748b',
                                    marginBottom: 10,
                                    padding: '6px 10px',
                                    background: '#f8fafc',
                                    borderRadius: 5,
                                }}
                            >
                                提示：保存任务草稿后，点击列表中【开始处理】即可派发给当前可用算力节点执行。
                            </div>

                            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                                <Button onClick={() => setOpen(false)}>取消</Button>
                                <Button
                                    type="primary"
                                    htmlType="submit"
                                    loading={save.isPending}
                                    disabled={
                                        !options.data?.assets.length ||
                                        !options.data?.pipelines.length
                                    }
                                >
                                    保存草稿
                                </Button>
                            </div>
                        </Form>
                    )}
                </Modal>
            ) : null}

            {executionJob ? (
                <ExecutionDetails job={executionJob} onClose={() => setExecutionJob(null)} />
            ) : null}
        </div>
    );
}
