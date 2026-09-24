import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertCircle, Box, Check, CheckCircle2, Download, Settings2, Zap } from 'lucide-react';
import { PluginFields, readConfig } from './PluginFields';
import { api, post } from '../api/client';
import type { NodeList, PluginConfigList, PluginEntry, PluginList, PreflightResponse } from '../api/contracts';
import { Badge, date, Empty, ErrorNotice, Heading, Loading, Modal, Notice } from '../components';

export default function Plugins() {
    const cache = useQueryClient();
    const [selected, setSelected] = useState<PluginEntry | null>(null);
    const [installingPlugin, setInstallingPlugin] = useState<PluginEntry | null>(null);
    const [targetNodeId, setTargetNodeId] = useState<string>('');
    const [selectedConfigId, setSelectedConfigId] = useState<string>('');
    const [tab, setTab] = useState('catalog');

    const catalog = useQuery({
        queryKey: ['catalog'],
        queryFn: ({ signal }) => api<PluginList>('/admin/v1/catalog', { signal }),
    });

    const configs = useQuery({
        queryKey: ['configs'],
        queryFn: ({ signal }) =>
            api<PluginConfigList>('/admin/v1/plugin-configurations?limit=100', { signal }),
    });

    const nodes = useQuery({
        queryKey: ['nodes'],
        queryFn: ({ signal }) => api<NodeList>('/admin/v1/nodes?limit=100', { signal }),
    });

    const preflight = useQuery({
        queryKey: ['preflight', targetNodeId, installingPlugin?.id, selectedConfigId],
        queryFn: () =>
            post<PreflightResponse>(
                `/admin/v1/nodes/${targetNodeId}/preflight`,
                {
                    node_id: targetNodeId,
                    plugin_id: installingPlugin!.id,
                    config_id: selectedConfigId || undefined,
                },
            ),
        enabled: !!targetNodeId && !!installingPlugin,
        retry: false,
    });

    const deploy = useMutation({
        mutationFn: () =>
            post(`/admin/v1/nodes/${targetNodeId}/plugins/${installingPlugin!.id}:deploy`, {
                config_id: selectedConfigId || undefined,
            }),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['nodes'] });
            setInstallingPlugin(null);
            setTargetNodeId('');
            setSelectedConfigId('');
        },
    });

    const [batchNotice, setBatchNotice] = useState<string>("");
    const batchDeploy = useMutation({
        mutationFn: () =>
            post<{ node_id: string; deployed: string[]; rejected: any[] }>(
                "/admin/v1/nodes/local-host/plugins:batch-deploy",
                {},
            ),
        onSuccess: (data) => {
            void cache.invalidateQueries({ queryKey: ["nodes"] });
            setBatchNotice(
                `已向 ${data.node_id} 下发 ${data.deployed.length} 个插件安装意图！` +
                    (data.rejected.length ? ` (另有 ${data.rejected.length} 个受预检限制未部署)` : ""),
            );
            setTimeout(() => setBatchNotice(""), 6000);
        },
    });

    const save = useMutation({
        mutationFn: (form: FormData) =>
            post('/admin/v1/plugin-configurations', {
                plugin_id: selected!.id,
                name: form.get('name'),
                config: readConfig(form, selected!.config_schema),
            }),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['configs'] });
            setSelected(null);
            setTab('configs');
        },
    });

    return (
        <>
            <Heading
                eyebrow="PLUGIN CENTER"
                title="插件中心"
                description="按契约扩展处理能力，让模型与工作流保持独立。"
                action={
                    <button
                        className="primary"
                        disabled={batchDeploy.isPending}
                        onClick={() => {
                            if (
                                confirm(
                                    "确定向同机数据面节点 (local-host) 一键装配全部基础处理插件（VLM、ASR、OCR、Embedding）吗？",
                                )
                            ) {
                                batchDeploy.mutate();
                            }
                        }}
                        title="向本机节点一键安装全部基础处理插件"
                    >
                        <Zap size={16} />
                        一键装配本地流水线
                    </button>
                }
            />
            {batchNotice ? (
                <div className="notice" style={{ borderColor: "#4ade80", background: "rgba(74, 222, 128, 0.08)" }}>
                    <CheckCircle2 size={18} color="#4ade80" />
                    <span style={{ color: "#86efac", fontSize: "0.88rem" }}>{batchNotice}</span>
                </div>
            ) : null}
            <Notice>
                依据 ADR-026 拓扑设计：插件不再全局泛化安装，而是由管理员选择目标计算节点。控制面预检硬件加速、容器/原生运行时、制品摘要与数据本地性；数据面仅限同机共享内存。
            </Notice>
            <div className="tabs">
                <button
                    className={tab === 'catalog' ? 'active' : ''}
                    onClick={() => setTab('catalog')}
                >
                    插件目录
                </button>
                <button
                    className={tab === 'configs' ? 'active' : ''}
                    onClick={() => setTab('configs')}
                >
                    配置版本
                </button>
            </div>
            <ErrorNotice error={catalog.error || configs.error} />
            {tab === 'catalog' ? (
                catalog.isPending ? (
                    <Loading />
                ) : (
                    <div className="plugin-grid">
                        {catalog.data?.items.map((item) => (
                            <article
                                className="card plugin-card"
                                key={item.id}
                                data-plugin-id={item.id}
                            >
                                <div className="plugin-top">
                                    <span className="plugin-icon">
                                        <Box size={27} />
                                    </span>
                                    <span className="version">v{item.version}</span>
                                </div>
                                <h2>{item.name}</h2>
                                <p>{item.description}</p>
                                <div className="tags">
                                    {item.produces.map((x) => (
                                        <span key={x}>{x.replace('observation.', '')}</span>
                                    ))}
                                </div>
                                <div className="plugin-meta">
                                    <Badge state={item.state} />
                                    <Badge state={item.trust} />
                                </div>
                                <small className="mono">{item.digest.slice(0, 30)}…</small>
                                <footer>
                                    <button
                                        onClick={() => {
                                            save.reset();
                                            setSelected(item);
                                        }}
                                    >
                                        <Settings2 size={16} />
                                        配置
                                    </button>
                                    <button
                                        className="primary"
                                        onClick={() => {
                                            deploy.reset();
                                            setInstallingPlugin(item);
                                            const defaultNode = nodes.data?.items?.[0]?.node_id || '';
                                            setTargetNodeId(defaultNode);
                                        }}
                                    >
                                        <Download size={16} />
                                        安装到节点
                                    </button>
                                </footer>
                            </article>
                        ))}
                        {catalog.data?.items.length === 0 ? (
                            <Empty title="没有发现插件">受控目录中暂时没有插件 Manifest。</Empty>
                        ) : null}
                    </div>
                )
            ) : (
                <section className="card">
                    {configs.isPending ? (
                        <Loading />
                    ) : configs.data?.items.length ? (
                        <div className="table-wrap">
                            <table>
                                <thead>
                                    <tr>
                                        <th>配置名称</th>
                                        <th>版本</th>
                                        <th>模型</th>
                                        <th>保存时间</th>
                                        <th>状态</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {configs.data.items.map((item) => (
                                        <tr key={item.id}>
                                            <td>
                                                <strong>{item.name}</strong>
                                                <small>{item.plugin_id}</small>
                                            </td>
                                            <td>v{item.revision}</td>
                                            <td>{String(item.config?.model || '未配置')}</td>
                                            <td>{date(item.created_at)}</td>
                                            <td>
                                                <span className="saved">
                                                    <Check size={14} />
                                                    已保存
                                                </span>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    ) : !configs.error ? (
                        <Empty title="还没有配置版本">
                            在插件目录中配置模型参数，每次保存都会生成不可变版本。
                        </Empty>
                    ) : null}
                </section>
            )}

            {selected ? (
                <Modal title="保存插件配置" onClose={() => setSelected(null)}>
                    <ErrorNotice error={save.error} />
                    <form
                        onSubmit={(e) => {
                            e.preventDefault();
                            save.mutate(new FormData(e.currentTarget));
                        }}
                    >
                        <label>
                            配置名称
                            <input
                                autoFocus
                                required
                                maxLength={120}
                                name="name"
                                placeholder="例如：本地场景描述"
                            />
                        </label>
                        <PluginFields schema={selected.config_schema} />
                        <p className="subtle">
                            保存配置不会验证模型就绪或启动插件。运行时传输地址由节点执行器绑定。
                        </p>
                        <button className="primary" disabled={save.isPending}>
                            保存配置版本
                        </button>
                    </form>
                </Modal>
            ) : null}

            {installingPlugin ? (
                <Modal title={`部署插件 · ${installingPlugin.name}`} onClose={() => setInstallingPlugin(null)}>
                    <ErrorNotice error={deploy.error} />
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
                        <div>
                            <span className="eyebrow">PLUGIN ARTIFACT</span>
                            <div className="card" style={{ padding: '10px', marginTop: '4px' }}>
                                <strong>{installingPlugin.id}</strong> (v{installingPlugin.version})
                                <br />
                                <small className="mono" style={{ color: '#a1a1aa' }}>{installingPlugin.digest}</small>
                            </div>
                        </div>

                        <label>
                            选择目标计算节点
                            <select
                                value={targetNodeId}
                                onChange={(e) => setTargetNodeId(e.target.value)}
                            >
                                <option value="" disabled>-- 请选择部署节点 --</option>
                                {nodes.data?.items?.map((n) => (
                                    <option key={n.node_id} value={n.node_id}>
                                        {n.display_name || n.node_id} ({n.is_co_located ? '同机数据面' : '局域网子节点'}) - {n.capabilities?.platform || "unknown"}/{n.capabilities?.arch || "unknown"} [{n.status}]
                                    </option>
                                ))}
                            </select>
                        </label>

                        {configs.data?.items?.filter((c) => c.plugin_id === installingPlugin.id).length ? (
                            <label>
                                绑定配置版本（可选）
                                <select
                                    value={selectedConfigId}
                                    onChange={(e) => setSelectedConfigId(e.target.value)}
                                >
                                    <option value="">默认配置</option>
                                    {configs.data.items
                                        .filter((c) => c.plugin_id === installingPlugin.id)
                                        .map((c) => (
                                            <option key={c.id} value={c.id}>
                                                {c.name} (v{c.revision})
                                            </option>
                                        ))}
                                </select>
                            </label>
                        ) : null}

                        {targetNodeId ? (
                            <div className="card" style={{ padding: '12px', background: '#121215' }}>
                                <small style={{ fontWeight: 600, display: 'block', marginBottom: '6px' }}>控制面预检 (ADR-026 Preflight)</small>
                                {preflight.isPending ? (
                                    <div style={{ fontSize: '0.85rem', color: '#a1a1aa' }}>正在执行硬件能力与数据本地性预检…</div>
                                ) : preflight.data ? (
                                    <div>
                                        {preflight.data.eligible ? (
                                            <div style={{ color: '#4ade80', display: 'flex', alignItems: 'center', gap: '8px', fontSize: '0.88rem' }}>
                                                <CheckCircle2 size={18} />
                                                <span>预检通过：目标节点算力、架构与数据本地性满足要求。</span>
                                            </div>
                                        ) : (
                                            <div style={{ color: '#f87171', display: 'flex', alignItems: 'flex-start', gap: '8px', fontSize: '0.88rem' }}>
                                                <AlertCircle size={18} style={{ marginTop: '2px', flexShrink: 0 }} />
                                                <div>
                                                    <strong>预检拒绝 ({preflight.data.reason_code})</strong>
                                                    <p style={{ margin: '4px 0 0 0', fontSize: '0.82rem', color: '#fca5a5' }}>
                                                        {preflight.data.detail}
                                                    </p>
                                                </div>
                                            </div>
                                        )}
                                    </div>
                                ) : (
                                    <div style={{ fontSize: '0.85rem', color: '#f87171' }}>无法获取预检状态</div>
                                )}
                            </div>
                        ) : (
                            <p className="subtle">请先选择一个节点以触发控制面能力预检。</p>
                        )}

                        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px', marginTop: '12px' }}>
                            <button onClick={() => setInstallingPlugin(null)}>取消</button>
                            <button
                                className="primary"
                                disabled={!targetNodeId || !preflight.data?.eligible || deploy.isPending}
                                onClick={() => deploy.mutate()}
                            >
                                <Download size={16} />
                                下发部署意图
                            </button>
                        </div>
                    </div>
                </Modal>
            ) : null}
        </>
    );
}
