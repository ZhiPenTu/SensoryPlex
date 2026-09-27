import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Button,
    Card,
    Descriptions,
    Progress,
    Space,
    Table,
    Typography,
    Row,
    Col,
    Popconfirm,
} from 'antd';
import {
    VideoCameraOutlined,
    UploadOutlined,
    EyeOutlined,
    DeleteOutlined,
    ArrowRightOutlined,
    CheckCircleOutlined,
    ClockCircleOutlined,
    InboxOutlined,
} from '@ant-design/icons';
import { Link } from 'react-router-dom';
import { api, post, uploadFile } from '../api/client';
import type { Upload, UploadList } from '../api/contracts';
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

export default function Assets() {
    const cache = useQueryClient();
    const [offset, setOffset] = useState(0);
    const [selected, setSelected] = useState<Upload | null>(null);
    const [progress, setProgress] = useState(0);
    const input = useRef<HTMLInputElement>(null);
    const canUpload = usePermission('assets:write');

    const listing = useQuery({
        queryKey: ['assets', offset],
        queryFn: ({ signal }) =>
            api<UploadList>(`/v1/assets?limit=20&offset=${offset}`, { signal }),
    });

    const upload = useMutation({
        mutationFn: async (file: File) => {
            const type =
                file.type ||
                (file.name.toLowerCase().endsWith('.webm') ? 'video/webm' : 'video/mp4');
            if (file.size > 1024 ** 3) throw new Error('单个文件最多 1 GiB。');
            setProgress(0);
            const item = await post<Upload>('/v1/uploads', {
                filename: file.name,
                size_bytes: String(file.size),
                content_type: type,
            });
            await uploadFile(item.id, file, setProgress);
        },
        onSettled: () => {
            void cache.invalidateQueries({ queryKey: ['assets'] });
        },
    });

    const cancel = useMutation({
        mutationFn: (id: string) => api(`/v1/uploads/${id}`, { method: 'DELETE' }),
        onSuccess: () => cache.invalidateQueries({ queryKey: ['assets'] }),
    });

    const totalCount = listing.data?.total || 0;
    const awaitingAdmissionCount =
        listing.data?.items.filter((x) => x.state === 'awaiting_admission').length || 0;

    const columns = [
        {
            title: '视频名称',
            dataIndex: 'filename',
            key: 'filename',
            render: (name: string, item: Upload) => (
                <Space align="center" size={12}>
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
                            fontSize: 14,
                            flexShrink: 0,
                        }}
                    >
                        <VideoCameraOutlined />
                    </div>
                    <div>
                        <Text
                            strong
                            style={{ fontSize: 13, display: 'block', maxWidth: 360 }}
                            ellipsis={{ tooltip: name }}
                        >
                            {name}
                        </Text>
                        <Text type="secondary" style={{ fontSize: 11 }}>
                            {item.content_type} · ID: {item.id.slice(0, 16)}…
                        </Text>
                    </div>
                </Space>
            ),
        },
        {
            title: '文件大小',
            dataIndex: 'size_bytes',
            key: 'size_bytes',
            width: 120,
            render: (size: string) => <Text style={{ fontSize: 13 }}>{bytes(size)}</Text>,
        },
        {
            title: '状态',
            dataIndex: 'state',
            key: 'state',
            width: 140,
            render: (state: string) => <Badge state={state} />,
        },
        {
            title: '导入时间',
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
            width: 160,
            align: 'right' as const,
            render: (_: unknown, item: Upload) => (
                <Space size={8}>
                    {item.state === 'pending' && canUpload ? (
                        <Popconfirm
                            title="确定取消该视频的上传？"
                            onConfirm={() => cancel.mutate(item.id)}
                            okText="确定"
                            cancelText="取消"
                        >
                            <Button
                                size="small"
                                danger
                                type="text"
                                icon={<DeleteOutlined />}
                                loading={cancel.isPending}
                            >
                                取消
                            </Button>
                        </Popconfirm>
                    ) : null}
                    <Button
                        size="small"
                        type="link"
                        icon={<EyeOutlined />}
                        disabled={item.state === 'pending'}
                        onClick={() => setSelected(item)}
                    >
                        详情
                    </Button>
                </Space>
            ),
        },
    ];

    return (
        <div>
            <Heading
                eyebrow="Media Library"
                title="视频库"
                description="存储原始视频母带，为每一份多模态感知与切片素材保留精准可溯的真实来源。"
                action={
                    canUpload ? (
                        <Button
                            type="primary"
                            icon={<UploadOutlined />}
                            loading={upload.isPending}
                            onClick={() => input.current?.click()}
                        >
                            导入视频
                        </Button>
                    ) : null
                }
            />

            <input
                className="sr-only"
                aria-label="选择视频文件"
                ref={input}
                type="file"
                accept="video/mp4,video/webm"
                style={{ display: 'none' }}
                onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) upload.mutate(f);
                    e.target.value = '';
                }}
            />

            <ErrorNotice error={listing.error || upload.error || cancel.error} />

            <Row gutter={[8, 8]} style={{ marginBottom: 10 }}>
                <Col xs={24} sm={8}>
                    <StatSummary
                        title="视频总数"
                        value={totalCount}
                        prefix={<VideoCameraOutlined style={{ color: '#1668dc' }} />}
                    />
                </Col>
                <Col xs={24} sm={8}>
                    <StatSummary
                        title="当前页待准入"
                        value={awaitingAdmissionCount}
                        prefix={<ClockCircleOutlined style={{ color: '#f59e0b' }} />}
                        color="#f59e0b"
                    />
                </Col>
                <Col xs={24} sm={8}>
                    <StatSummary
                        title="支持格式"
                        value="MP4 / WebM"
                        prefix={<CheckCircleOutlined style={{ color: '#10b981' }} />}
                    />
                </Col>
            </Row>

            <Notice>
                视频母带已在边缘存储落盘；当前阶段视频可直接上传和流式预览，媒体准入与自动编排完成后状态将自动流转。
            </Notice>

            {upload.isPending ? (
                <Card style={{ marginBottom: 10, borderColor: '#e2e8f0', background: '#ffffff' }}>
                    <Space direction="vertical" style={{ width: '100%' }} size={12}>
                        <div
                            style={{
                                display: 'flex',
                                justifyContent: 'space-between',
                                alignItems: 'center',
                            }}
                        >
                            <Space size={10}>
                                <InboxOutlined style={{ fontSize: 24, color: '#1d4ed8' }} />
                                <div>
                                    <Text strong style={{ color: '#0f172a' }}>
                                        正在上传视频母带至边缘存储…
                                    </Text>
                                    <div style={{ fontSize: 12, color: '#64748b' }}>
                                        数据直通端侧存储，请保持当前页面开启
                                    </div>
                                </div>
                            </Space>
                            <Text strong style={{ color: '#0f172a', fontSize: 16 }}>
                                {progress}%
                            </Text>
                        </div>
                        <Progress percent={progress} status="active" strokeColor="#1668dc" />
                    </Space>
                </Card>
            ) : null}

            <Card
                title={
                    <Space size={8}>
                        <span style={{ fontWeight: 650, fontSize: 15 }}>视频资产列表</span>
                        <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
                            单文件上限 1 GiB
                        </Text>
                    </Space>
                }
                bodyStyle={{ padding: 0 }}
            >
                {listing.isPending ? (
                    <Loading tip="正在载入视频资产…" />
                ) : listing.data?.items.length ? (
                    <Table
                        columns={columns}
                        dataSource={listing.data.items}
                        rowKey="id"
                        pagination={{
                            current: Math.floor(offset / 20) + 1,
                            pageSize: 20,
                            total: totalCount,
                            showTotal: (total) => `共 ${total} 段视频`,
                            onChange: (page) => setOffset((page - 1) * 20),
                        }}
                    />
                ) : !listing.error ? (
                    <Empty title="暂无视频资产">
                        导入授权的 MP4 或 WebM 视频文件，原片将安全存储于当前边缘节点。
                    </Empty>
                ) : null}
            </Card>

            {selected ? (
                <Modal
                    title={`视频详情 · ${selected.filename}`}
                    onClose={() => setSelected(null)}
                    width={720}
                >
                    <div className="video-preview-box" style={{ marginBottom: 10 }}>
                        <video
                            controls
                            preload="metadata"
                            src={`/v1/assets/${selected.id}/content`}
                        />
                    </div>

                    <Notice>
                        浏览器本地预览由源视频编码决定。准入完成后将提取关键帧与多模态流。
                    </Notice>

                    <Descriptions
                        bordered
                        size="small"
                        column={1}
                        style={{ marginTop: 10, marginBottom: 12 }}
                        labelStyle={{ width: 120, fontWeight: 500, background: '#f8fafc' }}
                    >
                        <Descriptions.Item label="文件名称">{selected.filename}</Descriptions.Item>
                        <Descriptions.Item label="文件大小">
                            {bytes(selected.size_bytes)}
                        </Descriptions.Item>
                        <Descriptions.Item label="媒体类型">
                            {selected.content_type}
                        </Descriptions.Item>
                        <Descriptions.Item label="当前状态">
                            <Badge state={selected.state} />
                        </Descriptions.Item>
                        <Descriptions.Item label="SHA-256 摘要">
                            <span className="mono">{selected.sha256}</span>
                        </Descriptions.Item>
                        <Descriptions.Item label="导入时间">
                            {date(selected.created_at)}
                        </Descriptions.Item>
                    </Descriptions>

                    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 12 }}>
                        <Button onClick={() => setSelected(null)}>关闭</Button>
                        <Link to="/jobs">
                            <Button type="primary" icon={<ArrowRightOutlined />}>
                                准备处理任务
                            </Button>
                        </Link>
                    </div>
                </Modal>
            ) : null}
        </div>
    );
}
