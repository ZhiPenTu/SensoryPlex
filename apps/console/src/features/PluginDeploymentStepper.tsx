import {
    Alert,
    Card,
    Col,
    Progress,
    Row,
    Space,
    Steps,
    Tag,
    Typography,
} from 'antd';
import {
    CheckCircleFilled,
    CheckCircleOutlined,
    ClockCircleOutlined,
    DesktopOutlined,
    SwapOutlined,
    ThunderboltOutlined,
} from '@ant-design/icons';
import type { PluginDeploymentOperation } from '../api/contracts';
import { date, time } from '../components';

const { Text } = Typography;

interface PluginDeploymentStepperProps {
    operation: PluginDeploymentOperation;
    releaseVersion?: string;
    onViewDiagnostics?: (op: PluginDeploymentOperation) => void;
}

/** 8个主要蓝绿阶段有序列表（不含终态 failed / cancelled） */
const PIPELINE_STAGES = [
    { key: 'PLUGIN_OPERATION_STAGE_ACCEPTED', title: '受理意图', desc: '控制面生成不可变代际' },
    { key: 'PLUGIN_OPERATION_STAGE_STAGING', title: '取制品/安装', desc: '验包防逃逸并离线安装' },
    { key: 'PLUGIN_OPERATION_STAGE_STARTING', title: '服务托管', desc: '平台服务拉起候选进程' },
    { key: 'PLUGIN_OPERATION_STAGE_VALIDATING', title: '候选验证', desc: 'Describe与连续3次探活' },
    { key: 'PLUGIN_OPERATION_STAGE_CANDIDATE_READY', title: '候选就绪', desc: '验证通过，准备原子CAS' },
    { key: 'PLUGIN_OPERATION_STAGE_CUTTING_OVER', title: '指针切换', desc: '事务切换Active指针' },
    { key: 'PLUGIN_OPERATION_STAGE_DRAINING_OLD', title: '排空旧版', desc: 'Grace Period优雅停机' },
    { key: 'PLUGIN_OPERATION_STAGE_SUCCEEDED', title: '部署就绪', desc: '蓝绿全链路完成' },
];

export default function PluginDeploymentStepper({
    operation,
    releaseVersion,
    onViewDiagnostics,
}: PluginDeploymentStepperProps) {
    const isFailed = operation.stage === 'PLUGIN_OPERATION_STAGE_FAILED';
    const isCancelled = operation.stage === 'PLUGIN_OPERATION_STAGE_CANCELLED';
    const isSucceeded = operation.stage === 'PLUGIN_OPERATION_STAGE_SUCCEEDED';

    // 计算当前处于哪个步骤索引
    const currentStageIndex = PIPELINE_STAGES.findIndex((s) => s.key === operation.stage);
    const activeStep = isFailed || isCancelled
        ? Math.max(0, currentStageIndex >= 0 ? currentStageIndex : 1)
        : currentStageIndex >= 0
          ? currentStageIndex
          : 0;

    // Deadline 剩余时间计算
    const deadlineMs = Number(operation.deadline_unix_ms) || 0;
    const nowMs = Date.now();
    const remainingSeconds = Math.max(0, Math.floor((deadlineMs - nowMs) / 1000));
    const isOverdue = deadlineMs > 0 && nowMs > deadlineMs && !isSucceeded && !isFailed && !isCancelled;

    // 总阶段耗时
    const totalDurationMs =
        (Number(operation.staging_ms) || 0) +
        (Number(operation.starting_ms) || 0) +
        (Number(operation.validating_ms) || 0) +
        (Number(operation.draining_ms) || 0);

    return (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            {/* 顶部状态与时效信息 */}
            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    background: '#f8fafc',
                    padding: '8px 12px',
                    borderRadius: 6,
                    border: '1px solid #e2e8f0',
                }}
            >
                <Space size={8} wrap>
                    <Tag color={operation.kind === 'rollback' ? 'orange' : 'geekblue'}>
                        {operation.kind === 'rollback'
                            ? '反向回滚'
                            : operation.kind === 'upgrade'
                              ? '蓝绿升级'
                              : '首次安装'}
                    </Tag>
                    <Text strong style={{ fontSize: 13 }}>
                        目标代际: 第 {operation.generation} 代
                    </Text>
                    <span className="mono" style={{ fontSize: 11, color: '#64748b' }}>
                        ID: {operation.operation_id}
                    </span>
                </Space>

                <Space size={12} wrap>
                    {deadlineMs > 0 && !isSucceeded && !isFailed && !isCancelled ? (
                        <Space size={4}>
                            <ClockCircleOutlined style={{ color: isOverdue ? '#dc2626' : '#2563eb' }} />
                            <Text
                                style={{
                                    fontSize: 12,
                                    color: isOverdue ? '#dc2626' : '#475569',
                                    fontWeight: isOverdue ? 600 : 400,
                                }}
                            >
                                {isOverdue
                                    ? `已超出预算时限 (${date(new Date(deadlineMs).toISOString())})`
                                    : `预算剩余: ${remainingSeconds}s (截止 ${date(new Date(deadlineMs).toISOString())})`}
                            </Text>
                        </Space>
                    ) : null}

                    {totalDurationMs > 0 ? (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                            累计耗时: <Text strong>{time(totalDurationMs)}</Text>
                        </Text>
                    ) : null}
                </Space>
            </div>

            {/* 失败或取消告警 */}
            {isFailed ? (
                <Alert
                    type="error"
                    showIcon
                    message={`部署在【${PIPELINE_STAGES[activeStep]?.title || '执行'}】阶段失败 (${operation.error_code || '未知错误'})`}
                    description={
                        <div>
                            <div style={{ marginBottom: 6 }}>
                                {operation.error_detail || '执行器报告异常，旧活跃版本继续承载业务，未发生破坏性切换。'}
                            </div>
                            {onViewDiagnostics ? (
                                <a
                                    onClick={() => onViewDiagnostics(operation)}
                                    style={{ fontSize: 12, fontWeight: 500 }}
                                >
                                    查看排障诊断与修复建议 →
                                </a>
                            ) : null}
                        </div>
                    }
                />
            ) : null}

            {isCancelled ? (
                <Alert
                    type="warning"
                    showIcon
                    message="部署已在指针切换前由操作员主动取消"
                    description="候选进程已安全注销并停止，槽位原 Active 实例未受影响，保持无损运行。"
                />
            ) : null}

            {/* 蓝绿阶段流水线 Stepper */}
            <Card
                size="small"
                title={
                    <Space size={6}>
                        <ThunderboltOutlined style={{ color: '#2563eb' }} />
                        <span style={{ fontSize: 13 }}>ADR-030 蓝绿状态机推进流水线</span>
                    </Space>
                }
                style={{ borderRadius: 6, border: '1px solid #e2e8f0' }}
            >
                <Steps
                    size="small"
                    current={activeStep}
                    status={isFailed ? 'error' : isCancelled ? 'wait' : isSucceeded ? 'finish' : 'process'}
                    items={PIPELINE_STAGES.map((s, idx) => {
                        let stepStatus: 'finish' | 'process' | 'wait' | 'error' = 'wait';
                        if (isFailed && idx === activeStep) {
                            stepStatus = 'error';
                        } else if (idx < activeStep || isSucceeded) {
                            stepStatus = 'finish';
                        } else if (idx === activeStep) {
                            stepStatus = isCancelled ? 'wait' : 'process';
                        }

                        // 各阶段专用耗时标注
                        let phaseTime = '';
                        if (s.key === 'PLUGIN_OPERATION_STAGE_STAGING' && operation.staging_ms) {
                            phaseTime = time(operation.staging_ms);
                        } else if (s.key === 'PLUGIN_OPERATION_STAGE_STARTING' && operation.starting_ms) {
                            phaseTime = time(operation.starting_ms);
                        } else if (s.key === 'PLUGIN_OPERATION_STAGE_VALIDATING' && operation.validating_ms) {
                            phaseTime = time(operation.validating_ms);
                        } else if (s.key === 'PLUGIN_OPERATION_STAGE_DRAINING_OLD' && operation.draining_ms) {
                            phaseTime = time(operation.draining_ms);
                        }

                        return {
                            title: (
                                <span style={{ fontSize: 12, fontWeight: idx === activeStep ? 600 : 400 }}>
                                    {s.title}
                                </span>
                            ),
                            description: (
                                <div style={{ fontSize: 11, color: '#64748b' }}>
                                    {phaseTime ? (
                                        <Tag color="cyan" style={{ margin: '2px 0 0', fontSize: 10 }}>
                                            {phaseTime}
                                        </Tag>
                                    ) : (
                                        s.desc
                                    )}
                                </div>
                            ),
                            status: stepStatus,
                        };
                    })}
                />

                {/* 候选验证阶段展开：Describe / Digest / Start / Health 连续3次 */}
                {operation.stage === 'PLUGIN_OPERATION_STAGE_VALIDATING' ||
                operation.stage === 'PLUGIN_OPERATION_STAGE_CANDIDATE_READY' ? (
                    <div
                        style={{
                            marginTop: 14,
                            padding: '8px 12px',
                            background: '#f1f5f9',
                            borderRadius: 6,
                            border: '1px dashed #cbd5e1',
                        }}
                    >
                        <Text strong style={{ fontSize: 12, display: 'block', marginBottom: 6 }}>
                            候选实例验证硬门禁进展:
                        </Text>
                        <Space size={16} wrap>
                            <Space size={4}>
                                <CheckCircleFilled style={{ color: '#16a34a' }} />
                                <span style={{ fontSize: 11.5 }}>1. Describe 契约声明</span>
                            </Space>
                            <Space size={4}>
                                <CheckCircleFilled style={{ color: '#16a34a' }} />
                                <span style={{ fontSize: 11.5 }}>2. 代码摘要与身份核验</span>
                            </Space>
                            <Space size={4}>
                                <CheckCircleFilled style={{ color: '#16a34a' }} />
                                <span style={{ fontSize: 11.5 }}>3. ValidateConfig 参数测试</span>
                            </Space>
                            <Space size={4}>
                                <CheckCircleFilled style={{ color: '#16a34a' }} />
                                <span style={{ fontSize: 11.5 }}>4. Start 带配置挂载</span>
                            </Space>
                            <Space size={4}>
                                {operation.stage === 'PLUGIN_OPERATION_STAGE_CANDIDATE_READY' ? (
                                    <CheckCircleFilled style={{ color: '#16a34a' }} />
                                ) : (
                                    <Progress type="circle" size={14} percent={66} strokeWidth={12} />
                                )}
                                <span style={{ fontSize: 11.5 }}>
                                    5. 连续3次 Health=ready 探活
                                    {operation.stage === 'PLUGIN_OPERATION_STAGE_CANDIDATE_READY' ? ' (已达成)' : ' (验证中)'}
                                </span>
                            </Space>
                        </Space>
                    </div>
                ) : null}
            </Card>

            {/* 双槽位实时拓扑对照卡片 (Dual-Slot Topology View) */}
            <Row gutter={[10, 10]}>
                {/* 槽位 A: 当前活跃旧版本 (Active Slot) */}
                <Col xs={24} md={12}>
                    <Card
                        size="small"
                        title={
                            <Space size={6}>
                                <DesktopOutlined style={{ color: '#059669' }} />
                                <span style={{ fontSize: 12.5 }}>现役槽位 (Active Slot)</span>
                                {operation.active ? (
                                    <Tag color="green" style={{ margin: 0, fontSize: 10 }}>
                                        在线服务中
                                    </Tag>
                                ) : (
                                    <Tag color="default" style={{ margin: 0, fontSize: 10 }}>
                                        首次安装无旧实例
                                    </Tag>
                                )}
                            </Space>
                        }
                        style={{
                            borderRadius: 6,
                            borderColor: operation.stage === 'PLUGIN_OPERATION_STAGE_DRAINING_OLD' ? '#f59e0b' : '#e2e8f0',
                            height: '100%',
                        }}
                    >
                        {operation.active ? (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 12 }}>
                                <div>
                                    <Text type="secondary">实例标识: </Text>
                                    <span className="mono">{operation.active.runtime_instance_id}</span>
                                </div>
                                <div>
                                    <Text type="secondary">运行版本: </Text>
                                    <Text strong>{releaseVersion || operation.active.release_id}</Text>
                                </div>
                                <div>
                                    <Text type="secondary">服务端点: </Text>
                                    <span className="mono" style={{ color: '#047857', fontWeight: 500 }}>
                                        {operation.active.endpoint || '未上报'}
                                    </span>
                                </div>
                                <div>
                                    <Text type="secondary">运行状态: </Text>
                                    <Tag
                                        color={
                                            operation.stage === 'PLUGIN_OPERATION_STAGE_DRAINING_OLD'
                                                ? 'gold'
                                                : 'green'
                                        }
                                    >
                                        {operation.stage === 'PLUGIN_OPERATION_STAGE_DRAINING_OLD'
                                            ? '正在排空 (Draining)'
                                            : operation.active.state}
                                    </Tag>
                                </div>
                                {operation.active.unit_name ? (
                                    <div>
                                        <Text type="secondary">系统托管: </Text>
                                        <span className="mono" style={{ fontSize: 11 }}>
                                            {operation.active.unit_name}
                                        </span>
                                    </div>
                                ) : null}
                            </div>
                        ) : (
                            <div style={{ padding: '16px 0', textAlign: 'center', color: '#94a3b8', fontSize: 12 }}>
                                该槽位此前未部署插件，本次操作为全新首次安装 (Provision)
                            </div>
                        )}
                    </Card>
                </Col>

                {/* 槽位 B: 新版候选实例 (Candidate Slot) */}
                <Col xs={24} md={12}>
                    <Card
                        size="small"
                        title={
                            <Space size={6}>
                                <SwapOutlined style={{ color: '#2563eb' }} />
                                <span style={{ fontSize: 12.5 }}>候选槽位 (Candidate Slot)</span>
                                {isSucceeded ? (
                                    <Tag color="green" style={{ margin: 0, fontSize: 10 }}>
                                        已成功接管 Active
                                    </Tag>
                                ) : isFailed ? (
                                    <Tag color="red" style={{ margin: 0, fontSize: 10 }}>
                                        验证失败已隔离
                                    </Tag>
                                ) : (
                                    <Tag color="purple" style={{ margin: 0, fontSize: 10 }}>
                                        验证测试中
                                    </Tag>
                                )}
                            </Space>
                        }
                        style={{
                            borderRadius: 6,
                            borderColor: isFailed ? '#ef4444' : isSucceeded ? '#10b981' : '#3b82f6',
                            height: '100%',
                        }}
                    >
                        {operation.candidate ? (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 12 }}>
                                <div>
                                    <Text type="secondary">候选标识: </Text>
                                    <span className="mono">{operation.candidate.runtime_instance_id}</span>
                                </div>
                                <div>
                                    <Text type="secondary">目标版本: </Text>
                                    <Text strong>{releaseVersion || operation.release_id}</Text>
                                </div>
                                <div>
                                    <Text type="secondary">动态端口端点: </Text>
                                    <span className="mono" style={{ color: '#1d4ed8', fontWeight: 500 }}>
                                        {operation.candidate.endpoint || '正在等待内核绑定与原子落盘…'}
                                    </span>
                                </div>
                                <div>
                                    <Text type="secondary">候选状态: </Text>
                                    <Tag color={isFailed ? 'red' : isSucceeded ? 'green' : 'purple'}>
                                        {operation.candidate.state}
                                    </Tag>
                                </div>
                                {operation.candidate.verified_artifact_digest ? (
                                    <div>
                                        <Text type="secondary">自证摘要: </Text>
                                        <span className="mono" style={{ fontSize: 11, color: '#475569' }}>
                                            {operation.candidate.verified_artifact_digest.slice(0, 20)}…
                                        </span>
                                        <CheckCircleOutlined style={{ color: '#16a34a', marginLeft: 4 }} />
                                    </div>
                                ) : null}
                            </div>
                        ) : (
                            <div style={{ padding: '16px 0', textAlign: 'center', color: '#94a3b8', fontSize: 12 }}>
                                尚未创建候选实例（等待受理意图下发…）
                            </div>
                        )}
                    </Card>
                </Col>
            </Row>
        </div>
    );
}
