import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import {
    Alert,
    Button,
    Card,
    Checkbox,
    Col,
    Divider,
    InputNumber,
    List,
    Popconfirm,
    Row,
    Select,
    Space,
    Statistic,
    Tag,
    Typography,
    message,
} from 'antd';
import {
    CheckCircleOutlined,
    DeleteOutlined,
    HddOutlined,
    InfoCircleOutlined,
    SearchOutlined,
} from '@ant-design/icons';
import { post } from '../api/client';
import { ErrorNotice, Modal } from '../components';

const { Text } = Typography;

export interface PruneNodeResponse {
    success: boolean;
    node_id: string;
    dry_run: boolean;
    keep: number;
    include_tasks: boolean;
    include_bundles: boolean;
    freed_bytes: number;
    freed_human: string;
    actions: string[];
    action_count: number;
}

export interface PluginPruneModalProps {
    nodeId?: string;
    availableNodes?: Array<{ node_id: string; display_name?: string; is_co_located?: boolean }>;
    onClose: () => void;
    onSuccess?: () => void;
}

export default function PluginPruneModal({
    nodeId,
    availableNodes = [],
    onClose,
    onSuccess,
}: PluginPruneModalProps) {
    const cache = useQueryClient();

    // 默认选传入的 nodeId，或可用节点里的第一个同机节点，或 local-host
    const coLocatedNodes = availableNodes.filter((n) => n.is_co_located || n.node_id === 'local-host');
    const defaultNodeId =
        nodeId ||
        (coLocatedNodes.length > 0 ? coLocatedNodes[0].node_id : 'local-host');

    const [selectedNodeId, setSelectedNodeId] = useState<string>(defaultNodeId);
    const [keep, setKeep] = useState<number>(1);
    const [includeTasks, setIncludeTasks] = useState<boolean>(false);
    const [includeBundles, setIncludeBundles] = useState<boolean>(false);
    const [pruneResult, setPruneResult] = useState<PruneNodeResponse | null>(null);

    const pruneMutation = useMutation({
        mutationFn: async ({ dryRun }: { dryRun: boolean }) => {
            return post<PruneNodeResponse>(`/admin/v1/nodes/${selectedNodeId}:prune`, {
                keep,
                include_tasks: includeTasks,
                include_bundles: includeBundles,
                dry_run: dryRun,
            });
        },
        onSuccess: (data, variables) => {
            setPruneResult(data);
            if (variables.dryRun) {
                if (data.action_count > 0) {
                    message.info(`预检分析完成：预计可释放 ${data.freed_human} 空间`);
                } else {
                    message.success('预检完成：当前节点环境整洁，无需清理');
                }
            } else {
                message.success(`物理清理完成！成功释放 ${data.freed_human} 磁盘空间`);
                void cache.invalidateQueries({ queryKey: ['nodes'] });
                void cache.invalidateQueries({ queryKey: ['plugin-deployments'] });
                if (onSuccess) {
                    onSuccess();
                }
            }
        },
    });

    const renderActionTag = (actionText: string) => {
        let tagColor = 'blue';
        let prefix = '条目';
        let content = actionText;

        const match = actionText.match(/^\[(.*?)\]\s*(.*)$/);
        if (match) {
            prefix = match[1];
            content = match[2];
            if (prefix === 'Release') tagColor = 'purple';
            else if (prefix === 'Runtime') tagColor = 'cyan';
            else if (prefix === 'LaunchAgent' || prefix === 'Systemd') tagColor = 'orange';
            else if (prefix === 'BundleCache') tagColor = 'magenta';
            else if (prefix === 'TaskExec') tagColor = 'geekblue';
        }

        return (
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6, fontSize: 12 }}>
                <Tag color={tagColor} style={{ margin: 0, fontSize: 10, flexShrink: 0 }}>
                    {prefix}
                </Tag>
                <Text style={{ wordBreak: 'break-all', fontSize: 12, color: '#334155' }}>
                    {content}
                </Text>
            </div>
        );
    };

    return (
        <Modal
            title="算力节点旧安装与旧版本清理 (Installations Pruning)"
            onClose={onClose}
            width={680}
        >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
                <Alert
                    type="info"
                    showIcon
                    icon={<InfoCircleOutlined />}
                    message="安全清理说明"
                    description="系统会自动识别并强保护所有处于运行中（running）的 active release、runtime 目录及服务单位。仅安全清理历史淘汰版本的虚拟环境、已停止的 runtime 缓存及孤立服务。"
                />

                {/* 参数配置表单区 */}
                <Card
                    size="small"
                    style={{ background: '#f8fafc', borderColor: '#e2e8f0', borderRadius: 8 }}
                    bodyStyle={{ padding: '12px 16px' }}
                >
                    <Row gutter={[16, 12]} align="middle">
                        <Col span={24}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                                <Text strong style={{ minWidth: 80 }}>目标节点:</Text>
                                {nodeId ? (
                                    <Space size={8}>
                                        <Text code style={{ fontSize: 13 }}>{selectedNodeId}</Text>
                                        <Tag color="geekblue">同机执行面</Tag>
                                    </Space>
                                ) : (
                                    <Select
                                        value={selectedNodeId}
                                        onChange={(val) => {
                                            setSelectedNodeId(val);
                                            setPruneResult(null);
                                        }}
                                        style={{ width: 260 }}
                                        options={
                                            coLocatedNodes.length > 0
                                                ? coLocatedNodes.map((n) => ({
                                                      label: `${n.display_name || n.node_id} (${n.node_id})`,
                                                      value: n.node_id,
                                                  }))
                                                : [{ label: 'local-host (本机默认)', value: 'local-host' }]
                                        }
                                    />
                                )}
                            </div>
                        </Col>

                        <Col span={24}>
                            <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                                <Text strong style={{ minWidth: 80 }}>保留备用版:</Text>
                                <InputNumber
                                    min={0}
                                    max={10}
                                    value={keep}
                                    onChange={(v) => {
                                        setKeep(v ?? 1);
                                        setPruneResult(null);
                                    }}
                                    style={{ width: 100 }}
                                />
                                <Text type="secondary" style={{ fontSize: 12 }}>
                                    个非活跃版本（推荐设为 1，供故障时秒级蓝绿回滚）
                                </Text>
                            </div>
                        </Col>

                        <Col span={24}>
                            <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', marginLeft: 92 }}>
                                <Checkbox
                                    checked={includeTasks}
                                    onChange={(e) => {
                                        setIncludeTasks(e.target.checked);
                                        setPruneResult(null);
                                    }}
                                >
                                    清理任务临时产物 (task-executions)
                                </Checkbox>
                                <Checkbox
                                    checked={includeBundles}
                                    onChange={(e) => {
                                        setIncludeBundles(e.target.checked);
                                        setPruneResult(null);
                                    }}
                                >
                                    清理制品构建缓存 (.data/releases)
                                </Checkbox>
                            </div>
                        </Col>
                    </Row>
                </Card>

                {/* 错误提示 */}
                <ErrorNotice error={pruneMutation.error} />

                {/* 操作按钮区 */}
                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10 }}>
                    <Button
                        icon={<SearchOutlined />}
                        loading={pruneMutation.isPending && pruneMutation.variables?.dryRun}
                        onClick={() => pruneMutation.mutate({ dryRun: true })}
                    >
                        预检分析 (Dry Run)
                    </Button>

                    <Popconfirm
                        title="确定执行物理清理？"
                        description={
                            <div style={{ maxWidth: 280 }}>
                                将物理删除所选节点上的历史废弃版本与目录。
                                {pruneResult && pruneResult.dry_run
                                    ? ` 预计释放 ${pruneResult.freed_human} 空间。`
                                    : ' 活跃版本受保护不受影响。'}
                            </div>
                        }
                        onConfirm={() => pruneMutation.mutate({ dryRun: false })}
                        okText="确认清理"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                    >
                        <Button
                            type="primary"
                            danger
                            icon={<DeleteOutlined />}
                            loading={pruneMutation.isPending && !pruneMutation.variables?.dryRun}
                        >
                            执行物理清理
                        </Button>
                    </Popconfirm>
                </div>

                {/* 结果展示卡片 */}
                {pruneResult ? (
                    <Card
                        size="small"
                        style={{
                            borderRadius: 8,
                            borderColor: pruneResult.dry_run ? '#93c5fd' : '#86efac',
                            background: pruneResult.dry_run ? '#eff6ff' : '#f0fdf4',
                        }}
                        bodyStyle={{ padding: '14px 18px' }}
                    >
                        <Row gutter={[16, 12]} align="middle" style={{ marginBottom: 12 }}>
                            <Col span={14}>
                                <Statistic
                                    title={
                                        <Space size={6}>
                                            <HddOutlined
                                                style={{
                                                    color: pruneResult.dry_run ? '#2563eb' : '#16a34a',
                                                }}
                                            />
                                            <span style={{ fontSize: 13, fontWeight: 600 }}>
                                                {pruneResult.dry_run ? '预检预计释放空间' : '物理清理成功释放'}
                                            </span>
                                        </Space>
                                    }
                                    value={pruneResult.freed_human}
                                    valueStyle={{
                                        color: pruneResult.dry_run ? '#1d4ed8' : '#15803d',
                                        fontWeight: 700,
                                        fontSize: 22,
                                    }}
                                />
                            </Col>
                            <Col span={10} style={{ textAlign: 'right' }}>
                                <Tag
                                    color={pruneResult.dry_run ? 'blue' : 'green'}
                                    style={{ fontSize: 12, padding: '2px 8px' }}
                                >
                                    {pruneResult.dry_run ? '预检分析模式' : '物理清理已落盘'}
                                </Tag>
                                <div style={{ fontSize: 11, color: '#64748b', marginTop: 4 }}>
                                    发现待处理项: {pruneResult.action_count} 项
                                </div>
                            </Col>
                        </Row>

                        <Divider style={{ margin: '8px 0 12px 0' }} />

                        {pruneResult.action_count === 0 ? (
                            <div
                                style={{
                                    textAlign: 'center',
                                    padding: '16px 0',
                                    color: '#059669',
                                    fontWeight: 500,
                                }}
                            >
                                <CheckCircleOutlined style={{ fontSize: 24, marginBottom: 6, display: 'block' }} />
                                当前节点环境非常干净，没有发现需要清理的历史旧安装或孤立文件！
                            </div>
                        ) : (
                            <div style={{ maxHeight: 220, overflowY: 'auto' }}>
                                <List
                                    size="small"
                                    dataSource={pruneResult.actions}
                                    renderItem={(action) => (
                                        <List.Item style={{ padding: '6px 0', borderBottom: '1px solid #f1f5f9' }}>
                                            {renderActionTag(action)}
                                        </List.Item>
                                    )}
                                />
                            </div>
                        )}
                    </Card>
                ) : null}
            </div>
        </Modal>
    );
}
