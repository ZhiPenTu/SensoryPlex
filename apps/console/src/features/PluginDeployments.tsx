import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Alert,
    Button,
    Col,
    Descriptions,
    Popconfirm,
    Row,
    Select,
    Space,
    Table,
    Tag,
    Typography,
    message,
} from 'antd';
import {
    CloudUploadOutlined,
    ReloadOutlined,
    RollbackOutlined,
    StopOutlined,
} from '@ant-design/icons';
import { api, post } from '../api/client';
import type {
    NodeInfo,
    NodeList,
    PluginDeploymentOperation,
    PluginDeploymentOperationList,
    PluginInstance,
    PluginRelease,
    PluginReleaseList,
} from '../api/contracts';
import { Empty, ErrorNotice, Loading, Modal, StatSummary, bytes, date, time } from '../components';
import { usePermission } from '../session';

const { Text, Paragraph } = Typography;

/** 操作阶段的中文语义与展示色；键与 proto 枚举名逐字一致。 */
const STAGE: Record<string, { label: string; color: string }> = {
    PLUGIN_OPERATION_STAGE_UNSPECIFIED: { label: '未知阶段', color: 'default' },
    PLUGIN_OPERATION_STAGE_ACCEPTED: { label: '已受理', color: 'blue' },
    PLUGIN_OPERATION_STAGE_STAGING: { label: '取制品/离线安装', color: 'geekblue' },
    PLUGIN_OPERATION_STAGE_STARTING: { label: '平台服务装载', color: 'geekblue' },
    PLUGIN_OPERATION_STAGE_VALIDATING: { label: '候选验证', color: 'purple' },
    PLUGIN_OPERATION_STAGE_CANDIDATE_READY: { label: '候选就绪', color: 'purple' },
    PLUGIN_OPERATION_STAGE_CUTTING_OVER: { label: '切换中', color: 'gold' },
    PLUGIN_OPERATION_STAGE_DRAINING_OLD: { label: '排空旧版本', color: 'gold' },
    PLUGIN_OPERATION_STAGE_SUCCEEDED: { label: '已完成', color: 'green' },
    PLUGIN_OPERATION_STAGE_FAILED: { label: '已失败', color: 'red' },
    PLUGIN_OPERATION_STAGE_CANCELLED: { label: '已取消', color: 'default' },
};

/** 终态：到达之后既不能取消，也不再轮询。 */
const SETTLED = new Set([
    'PLUGIN_OPERATION_STAGE_SUCCEEDED',
    'PLUGIN_OPERATION_STAGE_FAILED',
    'PLUGIN_OPERATION_STAGE_CANCELLED',
]);

function StageTag({ stage }: { stage: string }) {
    const conf = STAGE[stage] || { label: stage, color: 'default' };
    return (
        <Tag color={conf.color} style={{ margin: 0 }}>
            {conf.label}
        </Tag>
    );
}

/**
 * 插件热部署（ADR-030）：受控 release 选择、升级确认、实时阶段、当前/上一版本、显式回滚。
 *
 * 所有写操作都需要 `plugins:manage`；只读状态沿用现有节点/插件读取权限。
 */
export default function PluginDeployments() {
    const cache = useQueryClient();
    const canManage = usePermission('plugins:manage');
    const [nodeId, setNodeId] = useState('');
    const [pluginId, setPluginId] = useState('');
    const [releaseId, setReleaseId] = useState('');
    const [detail, setDetail] = useState<PluginDeploymentOperation | null>(null);

    const releases = useQuery({
        queryKey: ['plugin-releases'],
        queryFn: ({ signal }) =>
            api<PluginReleaseList>('/admin/v1/plugin-releases?limit=100', { signal }),
    });

    const nodes = useQuery({
        queryKey: ['nodes'],
        queryFn: ({ signal }) => api<NodeList>('/admin/v1/nodes?limit=100', { signal }),
    });

    const operations = useQuery({
        queryKey: ['plugin-deployments'],
        queryFn: ({ signal }) =>
            api<PluginDeploymentOperationList>('/admin/v1/plugin-deployments?limit=20', { signal }),
        // 只在有未结算操作时轮询：阶段、错误码与耗时都是服务端事实，不在这里推断。
        refetchInterval: (q) => {
            const pending = q.state.data?.items?.some((item) => !SETTLED.has(item.stage));
            return pending ? 2500 : 15000;
        },
    });

    const verifiedReleases = useMemo(
        () =>
            (releases.data?.items || []).filter(
                (item) => item.authenticated && item.trust === 'first_party',
            ),
        [releases.data],
    );
    const releaseById = useMemo(() => {
        const map = new Map<string, PluginRelease>();
        for (const item of releases.data?.items || []) map.set(item.release_id, item);
        return map;
    }, [releases.data]);

    const targetNode: NodeInfo | undefined = nodes.data?.items?.find((n) => n.node_id === nodeId);

    // 只允许选"与该节点平台/架构一致"的已认证 release：制品装不上去时应当在这一步就说清楚。
    const nodeReleases = useMemo(() => {
        if (!targetNode?.capabilities) return [];
        return verifiedReleases.filter(
            (item) =>
                item.platform === targetNode.capabilities?.platform &&
                item.arch === targetNode.capabilities?.arch,
        );
    }, [verifiedReleases, targetNode]);

    const pluginOptions = useMemo(() => {
        const ids = Array.from(new Set(nodeReleases.map((item) => item.plugin_id)));
        return ids.map((id) => ({ label: id, value: id }));
    }, [nodeReleases]);

    const releaseOptions = useMemo(
        () =>
            nodeReleases
                .filter((item) => item.plugin_id === pluginId)
                .map((item) => ({
                    value: item.release_id,
                    label: `${item.plugin_version} · ${item.platform}-${item.arch} · ${bytes(
                        item.bundle_bytes,
                    )} · 声明内存 ${bytes(item.declared_memory_bytes)}`,
                })),
        [nodeReleases, pluginId],
    );

    const activeInstance: PluginInstance | undefined = targetNode?.instances?.find(
        (inst) => inst.plugin_id === pluginId,
    );
    const isUpgrade = Boolean(activeInstance?.active_runtime_instance_id);

    const deploy = useMutation({
        mutationFn: () =>
            post(
                `/admin/v1/nodes/${nodeId}/plugins/${pluginId}:${isUpgrade ? 'upgrade' : 'provision'}`,
                { release_id: releaseId },
            ),
        onSuccess: () => {
            message.success(
                isUpgrade
                    ? '已受理升级：候选实例验证通过后才会切换 active 指针。'
                    : '已受理部署：候选实例验证通过后才会接管服务。',
            );
            setReleaseId('');
            void cache.invalidateQueries({ queryKey: ['plugin-deployments'] });
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const cancel = useMutation({
        mutationFn: (operationId: string) =>
            post(`/admin/v1/plugin-deployments/${operationId}:cancel`),
        onSuccess: () => {
            message.success('已在切换前取消：旧 active 保持服务，候选实例进入 failed。');
            void cache.invalidateQueries({ queryKey: ['plugin-deployments'] });
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const rollback = useMutation({
        mutationFn: (operationId: string) =>
            post(`/admin/v1/plugin-deployments/${operationId}:rollback`),
        onSuccess: () => {
            message.success('已创建反向部署操作：历史操作不改写，回滚走同一套蓝绿流程。');
            void cache.invalidateQueries({ queryKey: ['plugin-deployments'] });
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const items = operations.data?.items || [];
    const pendingCount = items.filter((item) => !SETTLED.has(item.stage)).length;
    const failedCount = items.filter(
        (item) => item.stage === 'PLUGIN_OPERATION_STAGE_FAILED',
    ).length;

    const version = (id: string) => {
        if (!id) return '—';
        const release = releaseById.get(id);
        return release ? release.plugin_version : `${id.slice(0, 12)}…`;
    };

    const columns = [
        {
            title: '部署操作',
            key: 'operation',
            width: 190,
            render: (_: unknown, row: PluginDeploymentOperation) => (
                <Space direction="vertical" size={2}>
                    <Space size={6}>
                        <Tag
                            color={row.kind === 'rollback' ? 'orange' : 'geekblue'}
                            style={{ margin: 0 }}
                        >
                            {row.kind === 'rollback'
                                ? '回滚'
                                : row.kind === 'upgrade'
                                  ? '升级'
                                  : '首次部署'}
                        </Tag>
                        <Text strong style={{ fontSize: 12 }}>
                            第 {row.generation} 代
                        </Text>
                    </Space>
                    <span className="mono" style={{ fontSize: 11, color: '#94a3b8' }}>
                        {row.operation_id}
                    </span>
                    <Text type="secondary" style={{ fontSize: 11 }}>
                        {date(row.created_at)}
                    </Text>
                </Space>
            ),
        },
        {
            title: '目标',
            key: 'target',
            width: 180,
            render: (_: unknown, row: PluginDeploymentOperation) => (
                <Space direction="vertical" size={2}>
                    <Text style={{ fontSize: 12 }}>{row.node_id}</Text>
                    <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace' }}>
                        {row.plugin_id}
                    </Text>
                </Space>
            ),
        },
        {
            title: '实时阶段',
            key: 'stage',
            width: 170,
            render: (_: unknown, row: PluginDeploymentOperation) => (
                <Space direction="vertical" size={4}>
                    <StageTag stage={row.stage} />
                    <Text type="secondary" style={{ fontSize: 11 }}>
                        截止 {date(new Date(Number(row.deadline_unix_ms)).toISOString())}
                    </Text>
                </Space>
            ),
        },
        {
            title: '当前 / 上一版本',
            key: 'versions',
            render: (_: unknown, row: PluginDeploymentOperation) => (
                <Space direction="vertical" size={2}>
                    <Text style={{ fontSize: 12 }}>
                        目标 <Text strong>{version(row.release_id)}</Text>
                    </Text>
                    <Text type="secondary" style={{ fontSize: 11 }}>
                        当前 active {row.active ? version(row.active.release_id) : '—'} · 上一版本{' '}
                        {row.previous ? version(row.previous.release_id) : '—'}
                    </Text>
                </Space>
            ),
        },
        {
            title: '耗时',
            key: 'timing',
            width: 170,
            render: (_: unknown, row: PluginDeploymentOperation) => (
                <Text type="secondary" style={{ fontSize: 11 }}>
                    下载/安装 {time(row.staging_ms)} · 启动 {time(row.starting_ms)} · 验证{' '}
                    {time(row.validating_ms)} · 排空 {time(row.draining_ms)}
                </Text>
            ),
        },
        {
            title: '结果',
            key: 'error',
            width: 190,
            render: (_: unknown, row: PluginDeploymentOperation) =>
                row.error_code ? (
                    <Space direction="vertical" size={2}>
                        <Tag color="red" style={{ margin: 0 }}>
                            {row.error_code}
                        </Tag>
                        {row.error_detail ? (
                            <Text
                                type="secondary"
                                style={{ fontSize: 11 }}
                                title={row.error_detail}
                            >
                                {row.error_detail.length > 60
                                    ? `${row.error_detail.slice(0, 60)}…`
                                    : row.error_detail}
                            </Text>
                        ) : null}
                    </Space>
                ) : (
                    <Text type="secondary" style={{ fontSize: 11 }}>
                        —
                    </Text>
                ),
        },
        {
            title: '操作',
            key: 'actions',
            width: 170,
            render: (_: unknown, row: PluginDeploymentOperation) => (
                <Space size={4} wrap>
                    <Button size="small" onClick={() => setDetail(row)}>
                        详情
                    </Button>
                    {canManage && row.cancellable ? (
                        <Popconfirm
                            title="取消这次部署？"
                            description="只在切换前允许取消：旧 active 继续服务，候选实例进入 failed。"
                            okText="取消部署"
                            cancelText="返回"
                            onConfirm={() => cancel.mutate(row.operation_id)}
                        >
                            <Button size="small" icon={<StopOutlined />} loading={cancel.isPending}>
                                取消
                            </Button>
                        </Popconfirm>
                    ) : null}
                    {canManage && SETTLED.has(row.stage) ? (
                        <Popconfirm
                            title="回滚到上一版本？"
                            description="会创建一个反向部署操作（不改写本次历史），并走同一套蓝绿切换。"
                            okText="创建回滚"
                            cancelText="返回"
                            onConfirm={() => rollback.mutate(row.operation_id)}
                        >
                            <Button
                                size="small"
                                icon={<RollbackOutlined />}
                                loading={rollback.isPending}
                            >
                                回滚
                            </Button>
                        </Popconfirm>
                    ) : null}
                </Space>
            ),
        },
    ];

    return (
        <div>
            <Row gutter={[8, 8]} style={{ marginBottom: 10 }}>
                <Col xs={24} sm={8}>
                    <StatSummary title="已认证首方制品" value={verifiedReleases.length} />
                </Col>
                <Col xs={24} sm={8}>
                    <StatSummary title="进行中的部署操作" value={pendingCount} color="#b45309" />
                </Col>
                <Col xs={24} sm={8}>
                    <StatSummary title="最近失败的部署" value={failedCount} color="#dc2626" />
                </Col>
            </Row>

            <Alert
                type="info"
                showIcon
                style={{ marginBottom: 10, padding: '6px 10px', fontSize: 12 }}
                message="热部署 = 版本化蓝绿切换：候选实例通过 Describe → 身份/摘要核对 → ValidateConfig → Start → 连续三次 Health=ready 之后，才会切换 active 指针；切换前失败一律保留旧 active。列表里的「当前 active」永远读槽位实时指针，不按本次操作的历史推断。"
            />

            <ErrorNotice
                error={
                    releases.error ||
                    nodes.error ||
                    operations.error ||
                    deploy.error ||
                    cancel.error ||
                    rollback.error
                }
            />

            {canManage ? (
                <div
                    style={{
                        padding: '8px 12px',
                        marginBottom: 10,
                        background: '#f8fafc',
                        border: '1px solid #e2e8f0',
                        borderRadius: 6,
                    }}
                >
                    <Text strong style={{ fontSize: 12.5, display: 'block', marginBottom: 6 }}>
                        选择已认证 release 发起部署 / 升级
                    </Text>
                    <Row gutter={[8, 8]} align="bottom">
                        <Col xs={24} md={6}>
                            <Text type="secondary" style={{ fontSize: 12 }}>
                                目标节点
                            </Text>
                            <Select
                                value={nodeId || undefined}
                                onChange={(value) => {
                                    setNodeId(value);
                                    setPluginId('');
                                    setReleaseId('');
                                }}
                                placeholder="选择可调度节点"
                                style={{ width: '100%' }}
                                options={(nodes.data?.items || []).map((node) => ({
                                    label: `${node.node_id} [${
                                        node.status === 'NODE_STATUS_READY' ? '可调度' : node.status
                                    }]`,
                                    value: node.node_id,
                                    disabled: node.status !== 'NODE_STATUS_READY',
                                }))}
                            />
                        </Col>
                        <Col xs={24} md={6}>
                            <Text type="secondary" style={{ fontSize: 12 }}>
                                插件
                            </Text>
                            <Select
                                value={pluginId || undefined}
                                onChange={(value) => {
                                    setPluginId(value);
                                    setReleaseId('');
                                }}
                                placeholder={nodeId ? '选择该节点可用的插件' : '请先选择节点'}
                                disabled={!nodeId}
                                style={{ width: '100%' }}
                                options={pluginOptions}
                            />
                        </Col>
                        <Col xs={24} md={10}>
                            <Text type="secondary" style={{ fontSize: 12 }}>
                                已认证 release（平台/架构必须与节点一致）
                            </Text>
                            <Select
                                value={releaseId || undefined}
                                onChange={setReleaseId}
                                placeholder={pluginId ? '选择 release' : '请先选择插件'}
                                disabled={!pluginId}
                                style={{ width: '100%' }}
                                options={releaseOptions}
                            />
                        </Col>
                        <Col xs={24} md={2}>
                            <Popconfirm
                                title={isUpgrade ? '确认升级该插件？' : '确认部署该插件？'}
                                description={
                                    isUpgrade
                                        ? '将创建候选实例并做蓝绿切换；升级余量不足会显式失败，不会停机更新。'
                                        : '将创建候选实例；验证通过前不会接管任何服务。'
                                }
                                okText="确认"
                                cancelText="取消"
                                onConfirm={() => deploy.mutate()}
                            >
                                <Button
                                    type="primary"
                                    icon={<CloudUploadOutlined />}
                                    disabled={
                                        !nodeId || !pluginId || !releaseId || deploy.isPending
                                    }
                                    loading={deploy.isPending}
                                    style={{ width: '100%' }}
                                >
                                    {isUpgrade ? '发起升级' : '发起部署'}
                                </Button>
                            </Popconfirm>
                        </Col>
                    </Row>
                    {isUpgrade ? (
                        <Paragraph
                            type="secondary"
                            style={{ fontSize: 12, marginTop: 10, marginBottom: 0 }}
                        >
                            该节点已存在该插件槽位（当前 active{' '}
                            {activeInstance?.plugin_version || '未知'}，端点{' '}
                            {activeInstance?.endpoint || '未上报'}），本次将走 upgrade 而不是首次
                            provision。
                        </Paragraph>
                    ) : null}
                </div>
            ) : (
                <Alert
                    type="warning"
                    showIcon
                    style={{ marginBottom: 10 }}
                    message="当前账户没有 plugins:manage 权限，只能查看部署状态。"
                />
            )}

            <Space style={{ marginBottom: 8 }}>
                <Button
                    size="small"
                    icon={<ReloadOutlined />}
                    onClick={() =>
                        void cache.invalidateQueries({ queryKey: ['plugin-deployments'] })
                    }
                >
                    刷新
                </Button>
                <Text type="secondary" style={{ fontSize: 12 }}>
                    进行中的操作每 2.5 秒自动刷新；阶段与耗时都来自控制面回报，不做本地推断。
                </Text>
            </Space>

            {operations.isPending ? (
                <Loading tip="正在载入部署操作…" />
            ) : items.length ? (
                <Table
                    rowKey="operation_id"
                    columns={columns}
                    dataSource={items}
                    pagination={{ pageSize: 10 }}
                    size="small"
                    scroll={{ x: 1320 }}
                />
            ) : (
                <Empty title="暂无热部署操作">
                    选择节点、插件与已认证 release
                    后发起部署，这里会显示每次操作的阶段、耗时与错误码。
                </Empty>
            )}

            {detail ? (
                <Modal
                    title={`部署操作详情 · ${detail.kind}`}
                    width={720}
                    onClose={() => setDetail(null)}
                >
                    <Descriptions column={1} size="small" bordered>
                        <Descriptions.Item label="操作 ID">
                            <span className="mono">{detail.operation_id}</span>
                        </Descriptions.Item>
                        <Descriptions.Item label="阶段">
                            <Space size={8}>
                                <StageTag stage={detail.stage} />
                                <Text type="secondary" style={{ fontSize: 12 }}>
                                    第 {detail.generation} 代
                                    {detail.rollback_of_operation_id
                                        ? ` · 回滚自 ${detail.rollback_of_operation_id}`
                                        : ''}
                                </Text>
                            </Space>
                        </Descriptions.Item>
                        <Descriptions.Item label="节点 / 插件">
                            {detail.node_id} · <span className="mono">{detail.plugin_id}</span>
                        </Descriptions.Item>
                        <Descriptions.Item label="目标 release">
                            {detail.release_id}（{version(detail.release_id)}）
                        </Descriptions.Item>
                        <Descriptions.Item label="候选实例">
                            {detail.candidate ? (
                                <Space direction="vertical" size={2}>
                                    <Text style={{ fontSize: 12 }}>
                                        <span className="mono">
                                            {detail.candidate.runtime_instance_id}
                                        </span>{' '}
                                        · {detail.candidate.state}
                                    </Text>
                                    <Text type="secondary" style={{ fontSize: 11 }}>
                                        端点 {detail.candidate.endpoint || '未上报'} · supervisor{' '}
                                        {detail.candidate.supervisor_id || '未上报'}
                                    </Text>
                                    <Text type="secondary" style={{ fontSize: 11 }}>
                                        验证身份 {detail.candidate.verified_plugin_id || '—'} /{' '}
                                        {detail.candidate.verified_artifact_digest
                                            ? detail.candidate.verified_artifact_digest.slice(
                                                  0,
                                                  24,
                                              ) + '…'
                                            : '—'}
                                    </Text>
                                </Space>
                            ) : (
                                '—'
                            )}
                        </Descriptions.Item>
                        <Descriptions.Item label="当前 active">
                            {detail.active
                                ? `${version(detail.active.release_id)} · ${
                                      detail.active.endpoint || '端点未上报'
                                  }`
                                : '—'}
                        </Descriptions.Item>
                        <Descriptions.Item label="本次操作替换">
                            {detail.from_runtime_instance_id ? (
                                <span className="mono">{detail.from_runtime_instance_id}</span>
                            ) : (
                                '无（首次部署）'
                            )}
                        </Descriptions.Item>
                        <Descriptions.Item label="上一版本">
                            {detail.previous
                                ? `${version(detail.previous.release_id)} · ${detail.previous.state}`
                                : '—'}
                        </Descriptions.Item>
                        <Descriptions.Item label="耗时">
                            下载/安装 {time(detail.staging_ms)} · 启动 {time(detail.starting_ms)} ·
                            验证 {time(detail.validating_ms)} · 排空 {time(detail.draining_ms)}
                        </Descriptions.Item>
                        <Descriptions.Item label="错误">
                            {detail.error_code ? (
                                <Space direction="vertical" size={2}>
                                    <Tag color="red" style={{ margin: 0 }}>
                                        {detail.error_code}
                                    </Tag>
                                    <Text style={{ fontSize: 12 }}>{detail.error_detail}</Text>
                                </Space>
                            ) : (
                                '—'
                            )}
                        </Descriptions.Item>
                        <Descriptions.Item label="时间">
                            创建 {date(detail.created_at)}
                            {detail.completed_at ? ` · 完成 ${date(detail.completed_at)}` : ''}
                        </Descriptions.Item>
                    </Descriptions>
                </Modal>
            ) : null}
        </div>
    );
}
