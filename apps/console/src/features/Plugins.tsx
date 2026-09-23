import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Box, Check, Settings2 } from 'lucide-react';
import { PluginFields, readConfig } from './PluginFields';
import { api, post } from '../api/client';
import type { PluginConfigList, PluginEntry, PluginList } from '../api/contracts';
import { Badge, date, Empty, ErrorNotice, Heading, Loading, Modal, Notice } from '../components';

export default function Plugins() {
    const cache = useQueryClient();
    const [selected, setSelected] = useState<PluginEntry | null>(null);
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
            />
            <Notice>
                当前目录来自本机插件 Manifest。源码可用不等于已经安装；安装、启停与卸载需要 Runtime
                执行器接入。
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
                                        className="primary"
                                        onClick={() => {
                                            save.reset();
                                            setSelected(item);
                                        }}
                                    >
                                        <Settings2 size={16} />
                                        配置插件
                                    </button>
                                    <button disabled title="安装执行器尚未接入">
                                        安装
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
        </>
    );
}
