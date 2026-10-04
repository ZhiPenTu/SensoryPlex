import {
    Alert,
    Card,
    Descriptions,
    Drawer,
    Progress,
    Space,
    Tag,
    Typography,
} from 'antd';
import {
    BugOutlined,
    ClockCircleOutlined,
    CodeOutlined,
    ExclamationCircleFilled,
    SafetyCertificateOutlined,
    ToolOutlined,
} from '@ant-design/icons';
import type { PluginDeploymentOperation } from '../api/contracts';
import { time } from '../components';

const { Text, Paragraph } = Typography;

interface PluginDiagnosticsDrawerProps {
    open: boolean;
    operation: PluginDeploymentOperation | null;
    releaseVersion?: string;
    onClose: () => void;
}

/** 常见错误码诊断字典：包含根本原因与排查修复建议 */
const ERROR_DIAGNOSTICS: Record<
    string,
    { title: string; category: string; rootCause: string; recommendation: string }
> = {
    upgrade_headroom_insufficient: {
        title: '升级余量不足 (内存预算冲突)',
        category: '资源与容量',
        rootCause:
            '当前目标节点的可用内存不足以同时容纳旧版本与候选新版本。ADR-030 规定热部署必须双实例并存验证，严禁直接强制停机更新。',
        recommendation:
            '1. 检查该节点上是否有非活跃的测试任务或其它大模型槽位；\n2. 释放部分闲置系统缓存或增加节点物理内存；\n3. 确认新旧 release 声明的 declared_memory_bytes 是否存在虚高。',
    },
    candidate_start_timeout: {
        title: '候选实例启动超时',
        category: '进程与依赖',
        rootCause:
            '候选进程未能由平台服务（LaunchAgent / systemd）在时限内成功拉起，或者未能及时向 endpoint 文件写入动态端口。',
        recommendation:
            '1. 检查节点机上 Python 虚拟环境与离线 wheelhouse 是否完整，排查是否缺少系统 C/C++ 动态链接库；\n2. 查看本地 Supervisor 日志排查 ImportError 或模型权重加载卡死；\n3. 检查系统端口绑定权限及磁盘写入权限。',
    },
    candidate_health_timeout: {
        title: '候选实例探活超时 (未能连续 3 次就绪)',
        category: 'gRPC 通信与模型就绪',
        rootCause:
            '候选进程已启动并绑定端口，但在执行 Describe/Health 探测时超时或连续 3 次返回不可用（模型权重初次加载耗时过长或崩溃）。',
        recommendation:
            '1. 检查模型权重文件（如 MLX/ONNX 权重）是否存在且路径配置正确；\n2. 检查宿主机 GPU/Metal 加速器是否正被其它大任务独占锁定；\n3. 查看插件 Describe 请求与 Health 返回状态。',
    },
    bundle_digest_mismatch: {
        title: '制品包完整性摘要不匹配',
        category: '安全与完整性',
        rootCause:
            '下载或导入的 bundle 归档的 SHA256 摘要与 release 描述符声明不一致，已被防篡改逻辑硬拦截。',
        recommendation:
            '1. 重新在构建机执行 `make plugin-release` 并重新核对摘要；\n2. 检查存储卷是否存在传输损坏或未受控覆盖写入。',
    },
    plugin_release_platform_mismatch: {
        title: '平台与芯片架构不匹配',
        category: '环境准入',
        rootCause:
            '所选 release 的 platform/arch（如 linux-x86_64）与目标节点的真实架构（如 darwin-arm64）不匹配。',
        recommendation:
            '1. 选用为该节点原生构建的对应平台架构 release；\n2. 检查节点心跳上报的平台能力元数据。',
    },
    plugin_configuration_required: {
        title: '缺少必要运行配置方案',
        category: '配置契约',
        rootCause:
            '插件的 config.schema.json 中声明了必填字段（如本地模型路径），但在部署时未提供兼容的有效配置。',
        recommendation:
            '1. 在「参数方案配置」页签中为该插件新建配置方案；\n2. 填入合法的本地路径与推理参数后重新发起部署。',
    },
    node_offline: {
        title: '目标计算节点离线',
        category: '网络与拓扑',
        rootCause: '目标节点未在租约心跳周期内上报有效心跳，已被控制面标记为 offline。',
        recommendation:
            '1. 检查目标机器上的 Node Agent 常驻进程是否正常运行；\n2. 检查局域网通信以及与 API (8091) 的网络连通性。',
    },
    plugin_deployment_in_progress: {
        title: '已有进行中的部署操作冲突',
        category: '并发控制',
        rootCause: '该插件槽位当前正有一个处于飞行中的部署/升级意图尚未结算。',
        recommendation:
            '等待当前部署操作完成，或者在指针切换前由操作员取消上一个未结算操作。',
    },
};

export default function PluginDiagnosticsDrawer({
    open,
    operation,
    releaseVersion,
    onClose,
}: PluginDiagnosticsDrawerProps) {
    if (!operation) return null;

    const isFailed = operation.stage === 'PLUGIN_OPERATION_STAGE_FAILED';
    const diag = operation.error_code ? ERROR_DIAGNOSTICS[operation.error_code] : null;

    // 阶段耗时统计
    const stagingMs = Number(operation.staging_ms) || 0;
    const startingMs = Number(operation.starting_ms) || 0;
    const validatingMs = Number(operation.validating_ms) || 0;
    const drainingMs = Number(operation.draining_ms) || 0;
    const totalMs = stagingMs + startingMs + validatingMs + drainingMs;

    return (
        <Drawer
            title={
                <Space size={8}>
                    <BugOutlined style={{ color: isFailed ? '#dc2626' : '#2563eb' }} />
                    <span>部署操作排障与诊断 · {operation.kind}</span>
                    {releaseVersion ? <Tag color="blue">{releaseVersion}</Tag> : null}
                </Space>
            }
            width={680}
            open={open}
            onClose={onClose}
            extra={
                <Tag color={isFailed ? 'red' : 'green'}>
                    第 {operation.generation} 代 · {operation.stage}
                </Tag>
            }
        >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
                {/* 错误诊断与修复建议卡片 */}
                {isFailed ? (
                    <Card
                        size="small"
                        title={
                            <Space size={6}>
                                <ExclamationCircleFilled style={{ color: '#dc2626' }} />
                                <span style={{ color: '#b91c1c', fontWeight: 600 }}>
                                    {diag?.title || `错误异常: ${operation.error_code || '未分类故障'}`}
                                </span>
                            </Space>
                        }
                        style={{
                            borderColor: '#fca5a5',
                            background: '#fef2f2',
                            borderRadius: 8,
                        }}
                    >
                        {diag?.category ? (
                            <div style={{ marginBottom: 8 }}>
                                <Tag color="error">{diag.category}</Tag>
                            </div>
                        ) : null}

                        <div style={{ marginBottom: 10 }}>
                            <Text strong style={{ color: '#991b1b', fontSize: 12 }}>
                                根本原因分析 (Root Cause):
                            </Text>
                            <Paragraph style={{ margin: '4px 0 0', fontSize: 12.5, color: '#450a0a' }}>
                                {diag?.rootCause || operation.error_detail || '执行器报告未知异常，建议查验本地日志。'}
                            </Paragraph>
                        </div>

                        {diag?.recommendation ? (
                            <div
                                style={{
                                    background: '#ffffff',
                                    padding: '8px 12px',
                                    borderRadius: 6,
                                    border: '1px solid #fecaca',
                                }}
                            >
                                <Space size={4} style={{ marginBottom: 4 }}>
                                    <ToolOutlined style={{ color: '#b91c1c' }} />
                                    <Text strong style={{ color: '#991b1b', fontSize: 12 }}>
                                        推荐排障动作 (Remediation):
                                    </Text>
                                </Space>
                                <pre
                                    style={{
                                        margin: 0,
                                        fontSize: 11.5,
                                        fontFamily: 'monospace',
                                        color: '#334155',
                                        whiteSpace: 'pre-wrap',
                                    }}
                                >
                                    {diag.recommendation}
                                </pre>
                            </div>
                        ) : null}
                    </Card>
                ) : (
                    <Alert
                        type="success"
                        showIcon
                        message="当前操作运行正常"
                        description="未检测到阻断性故障，候选实例与平台托管链路均符合契约规范。"
                    />
                )}

                {/* 阶段耗时瀑布剖析 (Stage Duration Breakdown) */}
                <Card
                    size="small"
                    title={
                        <Space size={6}>
                            <ClockCircleOutlined style={{ color: '#2563eb' }} />
                            <span style={{ fontSize: 13 }}>各阶段耗时剖析 (Duration Breakdown)</span>
                        </Space>
                    }
                    style={{ borderRadius: 8, border: '1px solid #e2e8f0' }}
                >
                    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                        <div>
                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 2 }}>
                                <span>取制品与离线安装 (Staging):</span>
                                <span className="mono">{time(stagingMs)}</span>
                            </div>
                            <Progress
                                percent={totalMs > 0 ? Math.round((stagingMs / totalMs) * 100) : 0}
                                strokeColor="#3b82f6"
                                size="small"
                            />
                        </div>

                        <div>
                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 2 }}>
                                <span>平台服务进程装载 (Starting):</span>
                                <span className="mono">{time(startingMs)}</span>
                            </div>
                            <Progress
                                percent={totalMs > 0 ? Math.round((startingMs / totalMs) * 100) : 0}
                                strokeColor="#6366f1"
                                size="small"
                            />
                        </div>

                        <div>
                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 2 }}>
                                <span>候选实例探活验证 (Validating):</span>
                                <span className="mono">{time(validatingMs)}</span>
                            </div>
                            <Progress
                                percent={totalMs > 0 ? Math.round((validatingMs / totalMs) * 100) : 0}
                                strokeColor="#8b5cf6"
                                size="small"
                            />
                        </div>

                        <div>
                            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 12, marginBottom: 2 }}>
                                <span>旧版本优雅排空 (Draining):</span>
                                <span className="mono">{time(drainingMs)}</span>
                            </div>
                            <Progress
                                percent={totalMs > 0 ? Math.round((drainingMs / totalMs) * 100) : 0}
                                strokeColor="#eab308"
                                size="small"
                            />
                        </div>
                    </div>
                </Card>

                {/* 候选实例物理与托管元数据 */}
                <Card
                    size="small"
                    title={
                        <Space size={6}>
                            <SafetyCertificateOutlined style={{ color: '#0891b2' }} />
                            <span style={{ fontSize: 13 }}>候选实例系统托管证据 (Host Evidence)</span>
                        </Space>
                    }
                    style={{ borderRadius: 8, border: '1px solid #e2e8f0' }}
                >
                    <Descriptions column={1} size="small" bordered>
                        <Descriptions.Item label="候选实例 ID">
                            <span className="mono">{operation.candidate?.runtime_instance_id || '—'}</span>
                        </Descriptions.Item>
                        <Descriptions.Item label="系统托管 Unit">
                            <span className="mono">{operation.candidate?.unit_name || '未上报'}</span>
                        </Descriptions.Item>
                        <Descriptions.Item label="Supervisor 标识">
                            <span className="mono">{operation.candidate?.supervisor_id || '未上报'}</span>
                        </Descriptions.Item>
                        <Descriptions.Item label="动态绑定端点">
                            <span className="mono" style={{ color: '#0284c7', fontWeight: 500 }}>
                                {operation.candidate?.endpoint || '—'}
                            </span>
                        </Descriptions.Item>
                        <Descriptions.Item label="可执行代码摘要">
                            <span className="mono" style={{ fontSize: 11 }}>
                                {operation.candidate?.verified_artifact_digest || '—'}
                            </span>
                        </Descriptions.Item>
                        <Descriptions.Item label="离线安装目录">
                            <span className="mono" style={{ fontSize: 11 }}>
                                {operation.candidate?.install_dir || '—'}
                            </span>
                        </Descriptions.Item>
                    </Descriptions>
                </Card>

                {/* 宿主机运维排查指令指南 */}
                <Card
                    size="small"
                    title={
                        <Space size={6}>
                            <CodeOutlined style={{ color: '#475569' }} />
                            <span style={{ fontSize: 13 }}>宿主机本地排障命令行指引</span>
                        </Space>
                    }
                    style={{ borderRadius: 8, border: '1px solid #e2e8f0', background: '#f8fafc' }}
                >
                    <div style={{ fontSize: 12, color: '#475569', marginBottom: 6 }}>
                        若需在目标节点宿主机直接排查该实例的 LaunchAgent / systemd 状态：
                    </div>
                    <pre
                        style={{
                            background: '#0f172a',
                            color: '#38bdf8',
                            padding: 10,
                            borderRadius: 6,
                            fontSize: 11,
                            fontFamily: 'monospace',
                            margin: 0,
                            overflowX: 'auto',
                        }}
                    >
                        {`# 查看宿主常驻服务运行状态\nlaunchctl list | grep sensoryplex\n\n# 检查候选实例日志（默认位于用户日志目录）\ntail -n 100 ~/Library/Logs/SensoryPlex/${operation.plugin_id}/*.log\n\n# 检查端口与 endpoint 原子文件\nls -l /tmp/sensoryplex-endpoints/`}
                    </pre>
                </Card>
            </div>
        </Drawer>
    );
}
