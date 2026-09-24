import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Check,
    Copy,
    Cpu,
    HardDrive,
    KeyRound,
    Plus,
    Radio,
    RefreshCw,
    Server,
    ShieldAlert,
    Trash2,
    X,
} from 'lucide-react';
import { api, post } from '../api/client';
import type { EnrollmentToken, NodeInfo, NodeList } from '../api/contracts';
import { Badge, date, Empty, ErrorNotice, Heading, Loading, Modal, Notice } from '../components';

export default function Nodes() {
    const cache = useQueryClient();
    const [tokenModalOpen, setTokenModalOpen] = useState(false);
    const [createdToken, setCreatedToken] = useState<EnrollmentToken | null>(null);
    const [copied, setCopied] = useState(false);

    const nodesQuery = useQuery({
        queryKey: ['nodes'],
        queryFn: ({ signal }) => api<NodeList>('/admin/v1/nodes?limit=100', { signal }),
        refetchInterval: 4000,
    });

    const createTokenMutation = useMutation({
        mutationFn: (form: FormData) =>
            post<EnrollmentToken>('/admin/v1/nodes/enrollment-tokens', {
                node_id: form.get('node_id'),
                expires_in_minutes: Number(form.get('expires_in_minutes') || 60),
            }),
        onSuccess: (data) => {
            setCreatedToken(data);
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const acceptMutation = useMutation({
        mutationFn: (nodeId: string) => post(`/admin/v1/nodes/${nodeId}:accept`),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const rejectMutation = useMutation({
        mutationFn: (nodeId: string) => post(`/admin/v1/nodes/${nodeId}:reject`),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const drainMutation = useMutation({
        mutationFn: (nodeId: string) => post(`/admin/v1/nodes/${nodeId}:drain`),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const revokeMutation = useMutation({
        mutationFn: (nodeId: string) => post(`/admin/v1/nodes/${nodeId}:revoke`),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const deleteMutation = useMutation({
        mutationFn: (nodeId: string) => api(`/admin/v1/nodes/${nodeId}`, { method: 'DELETE' }),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const purgeMutation = useMutation({
        mutationFn: () => post('/admin/v1/nodes:purge-stale'),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['nodes'] });
        },
    });

    const copyToken = (text: string) => {
        void navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
    };

    const formatBytes = (bytesStr?: string | number) => {
        const n = Number(bytesStr || 0);
        if (!n) return '0 GB';
        return `${(n / (1024 * 1024 * 1024)).toFixed(1)} GB`;
    };

    const acceleratorText = (node: NodeInfo) =>
        node.capabilities?.accelerators?.length
            ? node.capabilities.accelerators
                  .map((a) => `${a.accelerator} (${a.runtime_version || '可用'})`)
                  .join(', ')
            : 'CPU';

    const candidates =
        nodesQuery.data?.items?.filter((n) => n.status === 'NODE_STATUS_CANDIDATE') || [];
    const activeNodes =
        nodesQuery.data?.items?.filter((n) => n.status !== 'NODE_STATUS_CANDIDATE') || [];
    const readyCount = activeNodes.filter((node) => node.status === 'NODE_STATUS_READY').length;
    const coLocatedCount = activeNodes.filter((node) => node.is_co_located).length;
    const instanceCount = activeNodes.reduce(
        (total, node) => total + (node.instances?.length || 0),
        0,
    );

    return (
        <>
            <Heading
                eyebrow="LAN WORKER TOPOLOGY"
                title="节点拓扑"
                description="统一调度同机与局域网算力节点，隔离管理面与执行面。"
                action={
                    <div style={{ display: 'flex', gap: '10px' }}>
                        <button
                            onClick={() => {
                                if (confirm('确定清理所有已离线或已撤销的旧测试节点记录吗？')) {
                                    purgeMutation.mutate();
                                }
                            }}
                            disabled={purgeMutation.isPending}
                            title="清理已下线的测试节点"
                        >
                            <Trash2 size={16} />
                            清理离线节点
                        </button>
                        <button
                            className="primary"
                            onClick={() => {
                                createTokenMutation.reset();
                                setCreatedToken(null);
                                setTokenModalOpen(true);
                            }}
                        >
                            <Plus size={17} />
                            注册子节点
                        </button>
                    </div>
                }
            />
            <Notice>
                主节点是唯一控制面权威；子节点 agent 负责真实模型下载与生命周期。共享内存、DMA 与
                Metal/CUDA 句柄仅在同机数据面有效，远程节点仅处理结构化观测与文本。
            </Notice>
            <section className="control-strip" aria-label="节点拓扑概览">
                <div>
                    <span>登记节点</span>
                    <strong>{activeNodes.length}</strong>
                </div>
                <div>
                    <span>可调度</span>
                    <strong>{readyCount}</strong>
                </div>
                <div>
                    <span>同机数据面</span>
                    <strong>{coLocatedCount}</strong>
                </div>
                <div>
                    <span>插件实例</span>
                    <strong>{instanceCount}</strong>
                </div>
                <div>
                    <span>待接纳</span>
                    <strong>{candidates.length}</strong>
                </div>
            </section>

            <ErrorNotice
                error={
                    nodesQuery.error ||
                    createTokenMutation.error ||
                    acceptMutation.error ||
                    rejectMutation.error ||
                    drainMutation.error ||
                    revokeMutation.error ||
                    deleteMutation.error ||
                    purgeMutation.error
                }
            />

            {/* 发现待接纳节点横幅 (ADR-026 Candidate Discovery) */}
            {candidates.length > 0 ? (
                <section className="candidate-panel">
                    <div className="candidate-heading">
                        <Radio size={20} color="#eab308" />
                        <strong>发现待接纳的局域网计算节点 ({candidates.length})</strong>
                        <span className="badge warning">等待管理员审批</span>
                    </div>
                    <div className="candidate-list">
                        {candidates.map((cand) => (
                            <div key={cand.node_id} className="candidate-row">
                                <div>
                                    <div className="candidate-title">
                                        <strong>{cand.display_name || cand.node_id}</strong>
                                        <span className="badge muted">
                                            {cand.capabilities?.platform}/{cand.capabilities?.arch}
                                        </span>
                                    </div>
                                    <small className="mono subtle">{cand.node_id}</small>
                                    <small>
                                        {cand.capabilities?.cpu_cores} 核 CPU · 内存{' '}
                                        {formatBytes(cand.capabilities?.memory_bytes)}
                                        {cand.capabilities?.accelerators?.length
                                            ? ` · 加速器: ${cand.capabilities.accelerators.map((a) => a.accelerator).join(', ')}`
                                            : ''}
                                    </small>
                                </div>
                                <div className="candidate-actions">
                                    <button
                                        disabled={rejectMutation.isPending}
                                        onClick={() => rejectMutation.mutate(cand.node_id)}
                                        title="拒绝该节点"
                                    >
                                        <X size={15} />
                                        拒绝
                                    </button>
                                    <button
                                        className="primary"
                                        disabled={acceptMutation.isPending}
                                        onClick={() => acceptMutation.mutate(cand.node_id)}
                                        title="接纳此节点进入集群调度"
                                    >
                                        <Check size={15} />
                                        一键接纳
                                    </button>
                                </div>
                            </div>
                        ))}
                    </div>
                </section>
            ) : null}

            {nodesQuery.isPending ? (
                <Loading />
            ) : activeNodes.length ? (
                <div className="node-list">
                    {activeNodes.map((node: NodeInfo) => (
                        <article className="node-row" key={node.node_id}>
                            <div className="node-identity">
                                <span className="node-icon">
                                    <Server size={22} />
                                </span>
                                <div>
                                    <div className="node-title">
                                        <h2>{node.display_name || node.node_id}</h2>
                                        <Badge state={node.status} />
                                        <span className="badge muted">
                                            {node.is_co_located ? '同机数据面' : '局域网子节点'}
                                        </span>
                                    </div>
                                    <small className="mono">{node.node_id}</small>
                                    {node.status_reason ? (
                                        <small className="subtle">{node.status_reason}</small>
                                    ) : null}
                                </div>
                            </div>

                            <div className="node-facts">
                                <div>
                                    <Cpu size={15} />
                                    <span>
                                        {node.capabilities?.cpu_cores || 1} 核 CPU · 内存{' '}
                                        {formatBytes(node.capabilities?.memory_bytes)}
                                        {Number(node.capabilities?.unified_memory_bytes || 0) > 0
                                            ? ` · 统一内存 ${formatBytes(node.capabilities?.unified_memory_bytes)}`
                                            : ''}
                                    </span>
                                </div>
                                <div>
                                    <HardDrive size={15} />
                                    <span>加速器: {acceleratorText(node)}</span>
                                </div>
                                <div>
                                    <RefreshCw size={15} />
                                    <span>
                                        最近心跳:{' '}
                                        {node.last_heartbeat_at
                                            ? date(node.last_heartbeat_at)
                                            : '尚未收到'}
                                    </span>
                                </div>
                            </div>

                            <div className="node-instances">
                                <small>已部署插件实例 ({node.instances?.length || 0})</small>
                                {node.instances && node.instances.length > 0 ? (
                                    <div className="instance-chips">
                                        {node.instances.map((inst) => (
                                            <span key={inst.instance_id} className="instance-chip">
                                                <strong>{inst.plugin_id.split('.').pop()}</strong>
                                                <span>v{inst.plugin_version}</span>
                                                <Badge state={inst.actual_state} />
                                            </span>
                                        ))}
                                    </div>
                                ) : (
                                    <small className="subtle">尚未下发插件实例</small>
                                )}
                            </div>

                            <footer className="node-actions">
                                {node.status === 'NODE_STATUS_READY' ? (
                                    <button
                                        disabled={drainMutation.isPending}
                                        onClick={() => drainMutation.mutate(node.node_id)}
                                        title="排空节点：不再下发新任务"
                                    >
                                        排空
                                    </button>
                                ) : null}
                                {node.status !== 'NODE_STATUS_REVOKED' ? (
                                    <button
                                        className="danger"
                                        disabled={revokeMutation.isPending}
                                        onClick={() => {
                                            if (
                                                confirm(
                                                    `确定撤销节点 ${node.node_id} 吗？撤销后凭据即刻失效。`,
                                                )
                                            ) {
                                                revokeMutation.mutate(node.node_id);
                                            }
                                        }}
                                        title="撤销节点凭据"
                                    >
                                        <ShieldAlert size={15} />
                                        撤销
                                    </button>
                                ) : null}
                                {node.status === 'NODE_STATUS_REVOKED' ||
                                node.status === 'NODE_STATUS_OFFLINE' ? (
                                    <button
                                        onClick={() => {
                                            if (
                                                confirm(`确定删除该节点记录（${node.node_id}）吗？`)
                                            ) {
                                                deleteMutation.mutate(node.node_id);
                                            }
                                        }}
                                        disabled={deleteMutation.isPending}
                                        title="删除节点记录"
                                    >
                                        <Trash2 size={15} />
                                        删除
                                    </button>
                                ) : null}
                            </footer>
                        </article>
                    ))}
                </div>
            ) : !nodesQuery.error ? (
                <Empty title="暂无登记节点">
                    集群中尚未登记任何计算节点。点击右上角“注册子节点”获取一次性令牌。
                </Empty>
            ) : null}

            {tokenModalOpen ? (
                <Modal title="注册新计算节点" onClose={() => setTokenModalOpen(false)}>
                    <ErrorNotice error={createTokenMutation.error} />
                    {createdToken ? (
                        <div>
                            <p>一次性注册令牌已签发。请在目标机器运行 Node Agent 并传入此令牌：</p>
                            <div
                                className="card"
                                style={{ background: '#09090b', padding: '12px', margin: '12px 0' }}
                            >
                                <div
                                    style={{
                                        display: 'flex',
                                        justifyContent: 'space-between',
                                        alignItems: 'center',
                                    }}
                                >
                                    <code
                                        className="mono"
                                        style={{ wordBreak: 'break-all', fontSize: '0.88rem' }}
                                    >
                                        {createdToken.token}
                                    </code>
                                    <button
                                        className="ghost"
                                        onClick={() => copyToken(createdToken.token)}
                                        title="复制令牌"
                                    >
                                        <Copy size={16} />
                                        {copied ? '已复制' : '复制'}
                                    </button>
                                </div>
                            </div>
                            {(() => {
                                const host = window.location.hostname || '127.0.0.1';
                                const oneLine = `curl -fsSL http://${host}:8091/v1/agent/install.sh | bash -s -- --token ${createdToken.token} --main-url http://${host}:8091 --node-id ${createdToken.node_id} --daemon`;
                                const candidateCmd = `curl -fsSL http://${host}:8091/v1/agent/install.sh | bash -s -- --candidate --main-url http://${host}:8091 --daemon`;
                                return (
                                    <div
                                        style={{
                                            display: 'flex',
                                            flexDirection: 'column',
                                            gap: '10px',
                                        }}
                                    >
                                        <div
                                            className="card"
                                            style={{
                                                background: '#18181b',
                                                padding: '12px',
                                                border: '1px solid #27272a',
                                            }}
                                        >
                                            <div
                                                style={{
                                                    display: 'flex',
                                                    justifyContent: 'space-between',
                                                    alignItems: 'center',
                                                    marginBottom: '8px',
                                                }}
                                            >
                                                <strong
                                                    style={{
                                                        fontSize: '0.85rem',
                                                        color: '#4ade80',
                                                    }}
                                                >
                                                    ⚡ 推荐：带令牌单行安装（自动后台守护）
                                                </strong>
                                                <button
                                                    className="ghost"
                                                    onClick={() => copyToken(oneLine)}
                                                    title="复制单行命令"
                                                >
                                                    <Copy size={15} />
                                                    {copied ? '已复制' : '复制命令'}
                                                </button>
                                            </div>
                                            <code
                                                className="mono"
                                                style={{
                                                    fontSize: '0.78rem',
                                                    display: 'block',
                                                    wordBreak: 'break-all',
                                                    background: '#09090b',
                                                    padding: '8px',
                                                    borderRadius: '4px',
                                                }}
                                            >
                                                {oneLine}
                                            </code>
                                            <small
                                                style={{
                                                    display: 'block',
                                                    marginTop: '6px',
                                                    color: '#a1a1aa',
                                                    fontSize: '0.75rem',
                                                }}
                                            >
                                                目标机器上直接执行：完成硬件探测、入网并自启后台守护。
                                            </small>
                                        </div>

                                        <div
                                            className="card"
                                            style={{
                                                background: '#18181b',
                                                padding: '12px',
                                                border: '1px solid #27272a',
                                            }}
                                        >
                                            <div
                                                style={{
                                                    display: 'flex',
                                                    justifyContent: 'space-between',
                                                    alignItems: 'center',
                                                    marginBottom: '8px',
                                                }}
                                            >
                                                <strong
                                                    style={{
                                                        fontSize: '0.85rem',
                                                        color: '#eab308',
                                                    }}
                                                >
                                                    📡 零令牌模式：广播候选自报到（网页端一键接纳）
                                                </strong>
                                                <button
                                                    className="ghost"
                                                    onClick={() => copyToken(candidateCmd)}
                                                    title="复制候选命令"
                                                >
                                                    <Copy size={15} />
                                                    {copied ? '已复制' : '复制候选命令'}
                                                </button>
                                            </div>
                                            <code
                                                className="mono"
                                                style={{
                                                    fontSize: '0.78rem',
                                                    display: 'block',
                                                    wordBreak: 'break-all',
                                                    background: '#09090b',
                                                    padding: '8px',
                                                    borderRadius: '4px',
                                                }}
                                            >
                                                {candidateCmd}
                                            </code>
                                            <small
                                                style={{
                                                    display: 'block',
                                                    marginTop: '6px',
                                                    color: '#a1a1aa',
                                                    fontSize: '0.75rem',
                                                }}
                                            >
                                                在任意机器执行后，回到本页面点击“一键接纳”即可入网。
                                            </small>
                                        </div>
                                    </div>
                                );
                            })()}
                            <p
                                className="subtle"
                                style={{ fontSize: '0.78rem', margin: '10px 0 6px 0' }}
                            >
                                💡 提示：与主节点同机的主机在执行 <code>./deploy/up.sh</code>{' '}
                                时已自动完成自纳管并常驻后台，无需手动注册。
                            </p>
                            <small className="subtle">
                                令牌有效期至: {date(createdToken.expires_at)}
                            </small>
                            <div
                                style={{
                                    marginTop: '16px',
                                    display: 'flex',
                                    justifyContent: 'flex-end',
                                }}
                            >
                                <button
                                    className="primary"
                                    onClick={() => setTokenModalOpen(false)}
                                >
                                    完成
                                </button>
                            </div>
                        </div>
                    ) : (
                        <form
                            onSubmit={(e) => {
                                e.preventDefault();
                                createTokenMutation.mutate(new FormData(e.currentTarget));
                            }}
                        >
                            <label>
                                节点标识 (node_id)
                                <input
                                    autoFocus
                                    required
                                    name="node_id"
                                    placeholder="例如：node-mac-mini-01 或 node-gpu-3090"
                                    pattern="[a-zA-Z0-9_.-]{3,64}"
                                />
                            </label>
                            <label>
                                令牌有效时长（分钟）
                                <input
                                    name="expires_in_minutes"
                                    type="number"
                                    defaultValue={60}
                                    min={5}
                                    max={1440}
                                />
                            </label>
                            <p className="subtle">
                                令牌为一次性短期凭据，用于子节点 Agent 首次入网注册并换取长效
                                Session Token。
                            </p>
                            <button className="primary" disabled={createTokenMutation.isPending}>
                                <KeyRound size={16} />
                                生成注册令牌
                            </button>
                        </form>
                    )}
                </Modal>
            ) : null}
        </>
    );
}
