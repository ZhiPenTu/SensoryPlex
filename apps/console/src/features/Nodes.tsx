import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, Cpu, HardDrive, KeyRound, Plus, RefreshCw, Server, ShieldAlert } from 'lucide-react';
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
        refetchInterval: 5000,
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

    return (
        <>
            <Heading
                eyebrow="LAN WORKER TOPOLOGY"
                title="节点拓扑"
                description="统一调度同机与局域网算力节点，隔离管理面与执行面。"
                action={
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
                }
            />
            <Notice>
                主节点是唯一控制面权威；子节点 agent 负责真实模型下载与生命周期。共享内存、DMA 与 Metal/CUDA
                句柄仅在同机数据面有效，远程节点仅处理结构化观测与文本。
            </Notice>

            <ErrorNotice
                error={
                    nodesQuery.error ||
                    createTokenMutation.error ||
                    drainMutation.error ||
                    revokeMutation.error
                }
            />

            {nodesQuery.isPending ? (
                <Loading />
            ) : nodesQuery.data?.items.length ? (
                <div className="plugin-grid" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))' }}>
                    {nodesQuery.data.items.map((node: NodeInfo) => (
                        <article className="card plugin-card" key={node.node_id}>
                            <div className="plugin-top">
                                <span className="plugin-icon">
                                    <Server size={24} />
                                </span>
                                <span className="version">
                                    {node.is_co_located ? '同机数据面' : '局域网子节点'}
                                </span>
                            </div>
                            <h2>{node.display_name || node.node_id}</h2>
                            <p className="subtle mono">{node.node_id}</p>

                            <div className="plugin-meta">
                                <Badge state={node.status} />
                                <span className="badge muted">
                                    {node.capabilities?.platform || "unknown"}/{node.capabilities?.arch || "unknown"}
                                </span>
                            </div>

                            <div style={{ margin: '12px 0', fontSize: '0.85rem', display: 'flex', flexDirection: 'column', gap: '6px' }}>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                    <Cpu size={15} />
                                    <span>
                                        {node.capabilities?.cpu_cores || 1} 核 CPU · 内存 {formatBytes(node.capabilities?.memory_bytes)}
                                        {Number(node.capabilities?.unified_memory_bytes || 0) > 0 ? ` (统一内存 ${formatBytes(node.capabilities?.unified_memory_bytes)})` : ''}
                                    </span>
                                </div>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                    <HardDrive size={15} />
                                    <span>
                                        加速器:{' '}
                                        {node.capabilities?.accelerators?.length
                                            ? node.capabilities.accelerators.map((a) => `${a.accelerator} (${a.runtime_version || '可用'})`).join(', ')
                                            : '无 / CPU'}
                                    </span>
                                </div>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                                    <RefreshCw size={15} />
                                    <span>最近心跳: {node.last_heartbeat_at ? date(node.last_heartbeat_at) : '尚未收到'}</span>
                                </div>
                            </div>

                            {node.instances && node.instances.length > 0 ? (
                                <div style={{ borderTop: '1px solid var(--border-color, #27272a)', paddingTop: '10px', marginTop: '10px' }}>
                                    <small style={{ fontWeight: 600, display: 'block', marginBottom: '6px' }}>已部署插件实例 ({node.instances.length})</small>
                                    <ul style={{ margin: 0, paddingLeft: '18px', fontSize: '0.82rem' }}>
                                        {node.instances.map((inst) => (
                                            <li key={inst.instance_id}>
                                                <strong>{inst.plugin_id.split('.').pop()}</strong> v{inst.plugin_version} · <Badge state={inst.actual_state} />
                                            </li>
                                        ))}
                                    </ul>
                                </div>
                            ) : null}

                            <footer>
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
                                            if (confirm(`确定撤销节点 ${node.node_id} 吗？撤销后凭据即刻失效。`)) {
                                                revokeMutation.mutate(node.node_id);
                                            }
                                        }}
                                        title="撤销节点凭据"
                                    >
                                        <ShieldAlert size={15} />
                                        撤销
                                    </button>
                                ) : (
                                    <span className="subtle">凭据已作废</span>
                                )}
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
                            <div className="card" style={{ background: '#09090b', padding: '12px', margin: '12px 0' }}>
                                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                    <code className="mono" style={{ wordBreak: 'break-all', fontSize: '0.88rem' }}>
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
                                const host = window.location.hostname || "127.0.0.1";
                                const oneLine = `curl -fsSL http://${host}:8091/v1/agent/install.sh | bash -s -- --token ${createdToken.token} --main-url http://${host}:8091 --node-id ${createdToken.node_id} --daemon`;
                                return (
                                    <div className="card" style={{ background: '#18181b', padding: '12px', margin: '12px 0', border: '1px solid #27272a' }}>
                                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
                                            <strong style={{ fontSize: '0.85rem', color: '#4ade80' }}>⚡ 推荐：单行命令一键安装（自动后台守护）</strong>
                                            <button
                                                className="ghost"
                                                onClick={() => copyToken(oneLine)}
                                                title="复制单行命令"
                                            >
                                                <Copy size={15} />
                                                {copied ? '已复制' : '复制单行命令'}
                                            </button>
                                        </div>
                                        <code className="mono" style={{ fontSize: '0.78rem', display: 'block', wordBreak: 'break-all', background: '#09090b', padding: '8px', borderRadius: '4px' }}>
                                            {oneLine}
                                        </code>
                                        <small style={{ display: 'block', marginTop: '6px', color: '#a1a1aa', fontSize: '0.75rem' }}>
                                            在目标机器终端执行：自动探测硬件画像、入网认证并作为后台守护服务常驻运行。
                                        </small>
                                    </div>
                                );
                            })()}
                            <p className="subtle" style={{ fontSize: '0.78rem', margin: '6px 0' }}>
                                💡 提示：与主节点同机的主机在执行 <code>./deploy/up.sh</code> 时已自动完成自纳管并常驻后台，无需手动注册。
                            </p>
                            <small className="subtle">令牌有效期至: {date(createdToken.expires_at)}</small>
                            <div style={{ marginTop: '16px', display: 'flex', justifyContent: 'flex-end' }}>
                                <button className="primary" onClick={() => setTokenModalOpen(false)}>
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
                                令牌为一次性短期凭据，用于子节点 Agent 首次入网注册并换取长效 Session Token。
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
