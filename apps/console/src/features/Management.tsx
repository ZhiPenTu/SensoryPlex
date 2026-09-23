import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, Plus } from 'lucide-react';
import { api, post } from '../api/client';
import type {
    AccessToken,
    AuditList,
    ConsoleStatus,
    PipelineList,
    PluginConfigList,
    TokenList,
    UserList,
    User,
} from '../api/contracts';
import {
    Badge,
    date,
    Empty,
    ErrorNotice,
    Heading,
    Loading,
    Modal,
    Notice,
    Pager,
} from '../components';
import { usePermission, useSession } from '../session';

export function Pipelines() {
    const cache = useQueryClient();
    const [offset, setOffset] = useState(0);
    const [open, setOpen] = useState(false);
    const listing = useQuery({
        queryKey: ['pipelines', offset],
        queryFn: ({ signal }) =>
            api<PipelineList>(`/v1/pipelines?limit=20&offset=${offset}`, { signal }),
    });
    const configs = useQuery({
        queryKey: ['configs'],
        queryFn: ({ signal }) =>
            api<PluginConfigList>('/admin/v1/plugin-configurations?limit=100', { signal }),
    });
    const save = useMutation({
        mutationFn: (form: FormData) => {
            const config = configs.data?.items.find((x) => x.id === form.get('config_id'));
            return post('/admin/v1/pipelines', {
                ...Object.fromEntries(form),
                plugin_id: config?.plugin_id,
            });
        },
        onSuccess: () => {
            setOpen(false);
            void cache.invalidateQueries({ queryKey: ['pipelines'] });
        },
    });
    const archive = useMutation({
        mutationFn: (id: string) => post(`/admin/v1/pipelines/${id}:archive`),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['pipelines'] });
        },
    });
    return (
        <>
            <Heading
                eyebrow="PIPELINE TEMPLATES"
                title="处理方案"
                description="把插件、模型配置和处理参数固定成可追溯的方案版本。"
                action={
                    <button
                        className="primary"
                        onClick={() => {
                            save.reset();
                            setOpen(true);
                        }}
                    >
                        <Plus size={17} />
                        新建方案
                    </button>
                }
            />
            <Notice>方案可以保存和归档。运行时校验接入后，才能发布为可执行版本。</Notice>
            <ErrorNotice error={listing.error || configs.error || archive.error} />
            <section className="card">
                {listing.isPending ? (
                    <Loading />
                ) : listing.data?.items.length ? (
                    <div className="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>方案</th>
                                    <th>版本</th>
                                    <th>状态</th>
                                    <th>保存时间</th>
                                    <th />
                                </tr>
                            </thead>
                            <tbody>
                                {listing.data.items.map((x) => (
                                    <tr key={x.id}>
                                        <td>
                                            <strong>{x.name}</strong>
                                            <small>{x.description || x.plugin_id}</small>
                                        </td>
                                        <td>v{x.revision}</td>
                                        <td>
                                            <Badge state={x.state} />
                                        </td>
                                        <td>{date(x.created_at)}</td>
                                        <td>
                                            <div className="row-actions">
                                                <button disabled title="运行时校验尚未接入">
                                                    发布
                                                </button>
                                                {x.state === 'draft' ? (
                                                    <button
                                                        disabled={archive.isPending}
                                                        onClick={() => archive.mutate(x.id)}
                                                    >
                                                        归档
                                                    </button>
                                                ) : null}
                                            </div>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                ) : !listing.error ? (
                    <Empty title="建立第一个处理方案">
                        先在插件中心保存配置，再将其绑定到一个方案版本。
                    </Empty>
                ) : null}
                {listing.data ? (
                    <Pager offset={offset} total={listing.data.total} onChange={setOffset} />
                ) : null}
            </section>
            {open ? (
                <Modal title="新建处理方案" onClose={() => setOpen(false)}>
                    <ErrorNotice error={save.error} />
                    <form
                        onSubmit={(e) => {
                            e.preventDefault();
                            save.mutate(new FormData(e.currentTarget));
                        }}
                    >
                        <label>
                            方案名称
                            <input
                                autoFocus
                                name="name"
                                required
                                maxLength={120}
                                placeholder="例如：视频场景描述"
                            />
                        </label>
                        <label>
                            说明
                            <textarea name="description" maxLength={2000} rows={3} />
                        </label>
                        <label>
                            插件配置版本
                            <select name="config_id" required defaultValue="">
                                <option value="" disabled>
                                    选择已保存的配置
                                </option>
                                {configs.data?.items.map((x) => (
                                    <option key={x.id} value={x.id}>
                                        {x.name} · v{x.revision}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <p className="subtle">同名方案再次保存会创建新版本，不会修改历史配置。</p>
                        <button
                            className="primary"
                            disabled={save.isPending || !configs.data?.items.length}
                        >
                            保存方案草稿
                        </button>
                    </form>
                </Modal>
            ) : null}
        </>
    );
}

export function Access() {
    const cache = useQueryClient();
    const session = useSession();
    const [open, setOpen] = useState(false);
    const [secret, setSecret] = useState('');
    const [copyState, setCopyState] = useState('');
    const query = useQuery({
        queryKey: ['tokens'],
        queryFn: ({ signal }) => api<TokenList>('/auth/v1/access-tokens', { signal }),
    });
    const save = useMutation({
        mutationFn: (form: FormData) =>
            post<AccessToken>('/auth/v1/access-tokens', {
                name: form.get('name'),
                expires_in_days: Number(form.get('days')),
                scopes: form.getAll('scope'),
            }),
        onSuccess: (result) => {
            setSecret(result.token);
            setOpen(false);
            void cache.invalidateQueries({ queryKey: ['tokens'] });
        },
    });
    const revoke = useMutation({
        mutationFn: (id: string) => post(`/auth/v1/access-tokens/${id}:revoke`),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['tokens'] });
        },
    });
    const available = session.permissions.filter((x) => /^(assets|materials|jobs):/.test(x));
    return (
        <>
            <Heading
                eyebrow="ACCESS CONTROL"
                title="访问凭据"
                description="为 Agent 和外部应用创建可撤销的业务访问凭据。"
                action={
                    <button
                        className="primary"
                        disabled={!available.length}
                        onClick={() => {
                            save.reset();
                            setOpen(true);
                        }}
                    >
                        <Plus size={17} />
                        创建凭据
                    </button>
                }
            />
            <ErrorNotice error={query.error || revoke.error} />
            {secret ? (
                <section className="card token-secret">
                    <h3>请保存凭据，它只显示一次</h3>
                    <code>{secret}</code>
                    <div className="row-actions">
                        <button
                            onClick={() => {
                                navigator.clipboard
                                    .writeText(secret)
                                    .then(() => setCopyState('已复制'))
                                    .catch(() => setCopyState('复制失败，请手动选择保存'));
                            }}
                        >
                            <Copy size={15} />
                            复制凭据
                        </button>
                        <button
                            onClick={() => {
                                setSecret('');
                                setCopyState('');
                            }}
                        >
                            我已保存
                        </button>
                        <span role="status">{copyState}</span>
                    </div>
                </section>
            ) : null}
            <section className="card">
                {query.isPending ? (
                    <Loading />
                ) : query.data?.items.length ? (
                    <div className="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>名称</th>
                                    <th>权限</th>
                                    <th>到期时间</th>
                                    <th>状态</th>
                                    <th />
                                </tr>
                            </thead>
                            <tbody>
                                {query.data.items.map((x) => (
                                    <tr key={x.id}>
                                        <td>{x.name}</td>
                                        <td>
                                            <small>{x.scopes.join(' · ')}</small>
                                        </td>
                                        <td>{date(x.expires_at)}</td>
                                        <td>
                                            {x.revoked
                                                ? '已撤销'
                                                : new Date(x.expires_at) < new Date()
                                                  ? '已过期'
                                                  : '有效'}
                                        </td>
                                        <td>
                                            {!x.revoked ? (
                                                <button
                                                    disabled={revoke.isPending}
                                                    onClick={() => revoke.mutate(x.id)}
                                                >
                                                    撤销
                                                </button>
                                            ) : null}
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                ) : !query.error ? (
                    <Empty title="尚未创建访问凭据">
                        凭据只能获得你已有业务权限的子集，不包含平台管理权限。
                    </Empty>
                ) : null}
            </section>
            {open ? (
                <Modal title="创建访问凭据" onClose={() => setOpen(false)}>
                    <ErrorNotice error={save.error} />
                    <form
                        onSubmit={(e) => {
                            e.preventDefault();
                            save.mutate(new FormData(e.currentTarget));
                        }}
                    >
                        <label>
                            名称
                            <input
                                autoFocus
                                name="name"
                                required
                                maxLength={120}
                                placeholder="例如：研究助手"
                            />
                        </label>
                        <label>
                            有效天数
                            <input name="days" type="number" min={1} max={90} defaultValue={30} />
                        </label>
                        <fieldset>
                            <legend>业务权限</legend>
                            {available.map((x) => (
                                <label className="checkbox" key={x}>
                                    <input
                                        type="checkbox"
                                        name="scope"
                                        value={x}
                                        defaultChecked={x.endsWith(':read')}
                                    />
                                    {x}
                                </label>
                            ))}
                        </fieldset>
                        <button className="primary" disabled={save.isPending}>
                            创建
                        </button>
                    </form>
                </Modal>
            ) : null}
        </>
    );
}

export function System() {
    const query = useQuery({
        queryKey: ['capabilities'],
        queryFn: ({ signal }) => api<ConsoleStatus>('/v1/capabilities', { signal }),
        staleTime: 30000,
    });
    const names: Record<string, string> = {
        console_metadata: '工作台与配置存储',
        keyword_search: '素材关键词查询',
        file_storage: '原文件存储与读取',
        media_admission: '媒体格式准入',
        task_execution: '处理任务执行',
        plugin_installation: '插件安装执行器',
        pipeline_publish: '处理方案发布',
        semantic_search: '向量语义检索',
    };
    return (
        <>
            <Heading
                eyebrow="SYSTEM CAPABILITIES"
                title="系统能力"
                description="每项能力独立报告状态，基础服务在线不代表完整处理链路可用。"
            />
            <ErrorNotice error={query.error} />
            <section className="card">
                {query.isPending ? (
                    <Loading />
                ) : query.data ? (
                    <>
                        <div className="section-heading">
                            <h2>当前应用接线</h2>
                            <span className="mono subtle">schema {query.data.schema_version}</span>
                        </div>
                        <div className="capability-list">
                            {query.data.capabilities.map((c) => (
                                <div key={c.name}>
                                    <span className={`status-dot ${c.available ? 'ready' : ''}`} />
                                    <div>
                                        <strong>{names[c.name] || c.name}</strong>
                                        <small>{c.available ? '已实现应用接口' : c.reason}</small>
                                    </div>
                                    <span className={`badge ${c.available ? 'good' : ''}`}>
                                        {c.available ? '可用' : '待接入'}
                                    </span>
                                </div>
                            ))}
                        </div>
                    </>
                ) : null}
            </section>
        </>
    );
}

export function Audit() {
    const [offset, setOffset] = useState(0);
    const query = useQuery({
        queryKey: ['audit', offset],
        queryFn: ({ signal }) =>
            api<AuditList>(`/admin/v1/audit-events?limit=20&offset=${offset}`, { signal }),
    });
    return (
        <>
            <Heading
                eyebrow="AUDIT TRAIL"
                title="操作审计"
                description="追踪配置、身份和资产操作，不记录媒体内容与秘密。"
            />
            <ErrorNotice error={query.error} />
            <section className="card">
                {query.isPending ? (
                    <Loading />
                ) : query.data?.items.length ? (
                    <div className="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>时间</th>
                                    <th>操作者</th>
                                    <th>动作</th>
                                    <th>目标</th>
                                </tr>
                            </thead>
                            <tbody>
                                {query.data.items.map((x) => (
                                    <tr key={x.id}>
                                        <td>{date(x.created_at)}</td>
                                        <td>{x.actor}</td>
                                        <td>
                                            <code>{x.action}</code>
                                        </td>
                                        <td className="mono truncate">{x.target}</td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                ) : !query.error ? (
                    <Empty title="暂无操作记录" />
                ) : null}
                {query.data ? (
                    <Pager offset={offset} total={query.data.total} onChange={setOffset} />
                ) : null}
            </section>
        </>
    );
}

export function Users() {
    const cache = useQueryClient();
    const canManage = usePermission('users:manage');
    const [open, setOpen] = useState(false);
    const [editing, setEditing] = useState<User | null>(null);
    const query = useQuery({
        queryKey: ['users'],
        queryFn: ({ signal }) => api<UserList>('/admin/v1/users', { signal }),
    });
    const save = useMutation({
        mutationFn: (form: FormData) =>
            post('/admin/v1/users', {
                username: form.get('username'),
                display_name: form.get('display_name'),
                password: form.get('password'),
                roles: form.getAll('role'),
            }),
        onSuccess: () => {
            setOpen(false);
            void cache.invalidateQueries({ queryKey: ['users'] });
        },
    });
    const update = useMutation({
        mutationFn: (form: FormData) =>
            api(`/admin/v1/users/${encodeURIComponent(editing!.username)}`, {
                method: 'PUT',
                body: JSON.stringify({
                    display_name: form.get('display_name'),
                    roles: form.getAll('role'),
                    disabled: form.get('disabled') === 'on',
                    ...(form.get('password') ? { password: form.get('password') } : {}),
                }),
            }),
        onSuccess: () => {
            setEditing(null);
            void cache.invalidateQueries({ queryKey: ['users'] });
            void cache.invalidateQueries({ queryKey: ['session'] });
        },
    });
    return (
        <>
            <Heading
                eyebrow="TEAM & PERMISSIONS"
                title="用户与权限"
                description="内容访问与平台管理分别授权。"
                action={
                    canManage ? (
                        <button
                            className="primary"
                            onClick={() => {
                                save.reset();
                                setOpen(true);
                            }}
                        >
                            <Plus size={17} />
                            创建用户
                        </button>
                    ) : null
                }
            />
            <ErrorNotice error={query.error} />
            <section className="card">
                {query.isPending ? (
                    <Loading />
                ) : (
                    <div className="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>用户</th>
                                    <th>用户名</th>
                                    <th>角色</th>
                                    <th>状态</th>
                                    <th>操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {query.data?.items.map((x) => (
                                    <tr key={x.username}>
                                        <td>
                                            <strong>{x.display_name}</strong>
                                        </td>
                                        <td>{x.username}</td>
                                        <td>
                                            {x.roles
                                                .map(
                                                    (r) =>
                                                        ({
                                                            viewer: '素材查看者',
                                                            operator: '业务操作员',
                                                            admin: '平台管理员',
                                                        })[r] || r,
                                                )
                                                .join(' / ')}
                                        </td>
                                        <td>{x.disabled ? '已停用' : '启用'}</td>
                                        <td>
                                            <button
                                                onClick={() => {
                                                    update.reset();
                                                    setEditing(x);
                                                }}
                                            >
                                                编辑
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                )}
            </section>
            {open ? (
                <Modal title="创建用户" onClose={() => setOpen(false)}>
                    <ErrorNotice error={save.error} />
                    <form
                        onSubmit={(e) => {
                            e.preventDefault();
                            save.mutate(new FormData(e.currentTarget));
                        }}
                    >
                        <label>
                            显示名称
                            <input autoFocus name="display_name" required maxLength={120} />
                        </label>
                        <label>
                            用户名
                            <input
                                name="username"
                                required
                                pattern="[a-zA-Z0-9_.-]{3,64}"
                                placeholder="3–64 位字母、数字或 . _ -"
                            />
                        </label>
                        <label>
                            初始密码
                            <input
                                name="password"
                                type="password"
                                required
                                minLength={12}
                                maxLength={256}
                                autoComplete="new-password"
                            />
                        </label>
                        <fieldset>
                            <legend>角色</legend>
                            <label className="checkbox">
                                <input name="role" value="viewer" type="checkbox" defaultChecked />
                                素材查看者
                            </label>
                            <label className="checkbox">
                                <input name="role" value="operator" type="checkbox" />
                                业务操作员
                            </label>
                            <label className="checkbox">
                                <input name="role" value="admin" type="checkbox" />
                                平台管理员
                            </label>
                        </fieldset>
                        <button className="primary" disabled={save.isPending}>
                            创建用户
                        </button>
                    </form>
                </Modal>
            ) : null}
            {editing ? (
                <Modal title={`编辑用户 · ${editing.username}`} onClose={() => setEditing(null)}>
                    <ErrorNotice error={update.error} />
                    <form
                        onSubmit={(e) => {
                            e.preventDefault();
                            update.mutate(new FormData(e.currentTarget));
                        }}
                    >
                        <label>
                            显示名称
                            <input
                                name="display_name"
                                required
                                maxLength={120}
                                defaultValue={editing.display_name}
                            />
                        </label>
                        <label>
                            重设密码
                            <input
                                name="password"
                                type="password"
                                minLength={12}
                                maxLength={256}
                                autoComplete="new-password"
                                placeholder="留空则保持当前密码"
                            />
                        </label>
                        <fieldset>
                            <legend>角色</legend>
                            {[
                                ['viewer', '素材查看者'],
                                ['operator', '业务操作员'],
                                ['admin', '平台管理员'],
                            ].map(([role, label]) => (
                                <label className="checkbox" key={role}>
                                    <input
                                        name="role"
                                        value={role}
                                        type="checkbox"
                                        defaultChecked={editing.roles.includes(role)}
                                    />
                                    {label}
                                </label>
                            ))}
                        </fieldset>
                        <label className="checkbox">
                            <input
                                name="disabled"
                                type="checkbox"
                                defaultChecked={editing.disabled}
                            />
                            停用账户
                        </label>
                        <p className="subtle">
                            更改权限、密码或停用账户会撤销该用户的会话与访问凭据。
                        </p>
                        <button className="primary" disabled={update.isPending}>
                            保存用户
                        </button>
                    </form>
                </Modal>
            ) : null}
        </>
    );
}
