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
import type { JobDraftList, PipelineList, UploadList } from '../api/contracts';
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

export default function Jobs() {
    const cache = useQueryClient();
    const [offset, setOffset] = useState(0);
    const [open, setOpen] = useState(false);
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
                            width: 36,
                            height: 36,
                            borderRadius: 8,
                            background: '#f8fafc',
                            border: '1px solid #e2e8f0',
                            color: '#475569',
                            display: 'grid',
                            placeItems: 'center',
                            fontSize: 16,
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
            width: 260,
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
                            正在处理…
                        </Button>
                    ) : null}

                    {item.state === 'completed' ? (
                        <Link to="/materials">
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

            <Row gutter={[16, 16]} style={{ marginBottom: 20 }}>
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
                点击【开始处理】后，控制面会校验数据本地性并派发给同机节点。当前未接入受控 Runtime
                执行器时，任务会明确失败并显示原因，不会持续显示“处理中”。
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
                                    marginBottom: 20,
                                    padding: '8px 12px',
                                    background: '#f8fafc',
                                    borderRadius: 6,
                                }}
                            >
                                提示：保存任务草稿后，点击列表中【开始处理】即可派发给当前可用算力节点执行。
                            </div>

                            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
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
        </div>
    );
}
