import { lazy, Suspense, useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    Activity,
    ArrowRight,
    Box,
    Clapperboard,
    FileClock,
    KeyRound,
    Layers3,
    LogOut,
    Menu,
    Search,
    Server,
    ShieldCheck,
    Users,
    X,
} from 'lucide-react';
import { Link, NavLink, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { api, post, RequestError, setCsrf } from './api/client';
import type { DemoAccount, Identity } from './api/contracts';
import { Empty, ErrorNotice, Loading } from './components';
import { SessionContext, useSession } from './session';
import { Access, Audit, Pipelines, System, Users as UserPage } from './features/Management';

const Assets = lazy(() => import('./features/Assets'));
const Jobs = lazy(() => import('./features/Jobs'));
const Materials = lazy(() => import('./features/Materials'));
const Plugins = lazy(() => import('./features/Plugins'));
const Nodes = lazy(() => import('./features/Nodes'));
const MaterialDetail = lazy(() =>
    import('./features/Materials').then((m) => ({ default: m.MaterialDetail })),
);

const navigation = [
    {
        path: '/assets',
        label: '视频库',
        icon: Clapperboard,
        scope: 'assets:read',
        group: '工作空间',
    },
    { path: '/jobs', label: '处理任务', icon: FileClock, scope: 'jobs:read', group: '工作空间' },
    {
        path: '/materials',
        label: '素材检索',
        icon: Search,
        scope: 'materials:read',
        group: '工作空间',
    },
    { path: '/plugins', label: '插件中心', icon: Box, scope: 'plugins:manage', group: '平台管理' },
    { path: '/nodes', label: '节点拓扑', icon: Server, scope: 'plugins:manage', group: '平台管理' },
    {
        path: '/pipelines',
        label: '处理方案',
        icon: Layers3,
        scope: 'pipelines:manage',
        group: '平台管理',
    },
    { path: '/users', label: '用户与权限', icon: Users, scope: 'users:manage', group: '平台管理' },
    {
        path: '/audit',
        label: '操作审计',
        icon: ShieldCheck,
        scope: 'audit:read',
        group: '平台管理',
    },
    { path: '/access', label: '访问凭据', icon: KeyRound, scope: '', group: '设置' },
    { path: '/system', label: '系统能力', icon: Activity, scope: '', group: '设置' },
];

function Login({ onLogin }: { onLogin: (identity: Identity) => void }) {
    const [username, setUsername] = useState('');
    const [password, setPassword] = useState('');
    const [demoFilled, setDemoFilled] = useState(false);
    const demo = useQuery({
        queryKey: ['demo-account'],
        queryFn: ({ signal }) => api<DemoAccount>('/auth/v1/demo-account', { signal }),
        staleTime: 0,
    });
    const mutation = useMutation({
        mutationFn: (form: FormData) =>
            post<Identity>('/auth/v1/session', Object.fromEntries(form)),
        onSuccess: onLogin,
    });
    return (
        <div className="login-page">
            <section className="login-story">
                <Link className="brand" to="/">
                    <span className="brand-mark">
                        <Layers3 size={24} />
                    </span>
                    SensoryPlex
                </Link>
                <div>
                    <span className="eyebrow">YOUR MEDIA, CONNECTED.</span>
                    <h1>
                        让每一段画面，
                        <br />
                        都有据可循。
                    </h1>
                    <p>
                        从原始视频到结构化素材。
                        <br />
                        连接模型、时间轴与真实来源。
                    </p>
                    <div className="login-pipeline">
                        <span>视频</span>
                        <ArrowRight size={16} />
                        <span>感知</span>
                        <ArrowRight size={16} />
                        <span>素材</span>
                    </div>
                </div>
                <small>端侧处理 · 版本可追溯 · 数据由你掌握</small>
            </section>
            <section className="login-panel">
                <div>
                    <span className="eyebrow">MATERIAL WORKSPACE</span>
                    <h2>登录素材工作台</h2>
                    <p className="subtle">使用当前节点的本地账户继续。</p>
                    <ErrorNotice error={mutation.error} />
                    <form
                        onSubmit={(e) => {
                            e.preventDefault();
                            mutation.mutate(new FormData(e.currentTarget));
                        }}
                    >
                        <label>
                            用户名
                            <input
                                autoFocus
                                name="username"
                                value={username}
                                onChange={(e) => {
                                    setUsername(e.target.value);
                                    setDemoFilled(false);
                                }}
                                autoComplete="username"
                                required
                                maxLength={64}
                                placeholder="输入用户名"
                            />
                        </label>
                        <label>
                            密码
                            <input
                                name="password"
                                value={password}
                                onChange={(e) => {
                                    setPassword(e.target.value);
                                    setDemoFilled(false);
                                }}
                                type="password"
                                autoComplete="current-password"
                                required
                                maxLength={256}
                                placeholder="输入密码"
                            />
                        </label>
                        <button className="primary" disabled={mutation.isPending}>
                            {mutation.isPending ? '正在登录…' : '登录工作台'}
                            <ArrowRight size={17} />
                        </button>
                        {demo.data?.enabled ? (
                            <button
                                className="demo-account-button"
                                type="button"
                                disabled={mutation.isPending}
                                onClick={() => {
                                    if (!demo.data?.enabled) return;
                                    mutation.reset();
                                    setUsername(demo.data.username);
                                    setPassword(demo.data.password);
                                    setDemoFilled(true);
                                }}
                            >
                                <Users size={17} />
                                填入演示账号
                            </button>
                        ) : null}
                    </form>
                    <p className="login-help" role="status">
                        {demoFilled
                            ? '演示账号已填入，点击“登录工作台”即可体验。'
                            : demo.data?.enabled
                              ? '本地演示可一键填写账号，体验工作台功能。'
                              : '首次使用需由节点管理员创建账户。'}
                    </p>
                </div>
                <small>SensoryPlex Console · 开发预览</small>
            </section>
        </div>
    );
}

function Guard({ scope }: { scope: string }) {
    const session = useSession();
    return !scope || session.permissions.includes(scope) ? (
        <Outlet />
    ) : (
        <section className="card">
            <Empty title="无权访问此页面">请联系管理员授予相应权限。</Empty>
        </section>
    );
}

function Layout() {
    const session = useSession();
    const cache = useQueryClient();
    const location = useLocation();
    const [mobile, setMobile] = useState(false);
    const logout = useMutation({
        mutationFn: () => api('/auth/v1/session', { method: 'DELETE' }),
        onSuccess: () => {
            setCsrf('');
            cache.clear();
            window.location.assign('/');
        },
    });
    useEffect(() => {
        setMobile(false);
    }, [location.pathname]);
    const current = navigation.find((x) => location.pathname.startsWith(x.path));
    const visible = navigation.filter((x) => !x.scope || session.permissions.includes(x.scope));
    return (
        <div className="app-shell">
            <aside className={`sidebar ${mobile ? 'mobile-open' : ''}`}>
                <Link to="/" className="brand">
                    <span className="brand-mark">
                        <Layers3 size={22} />
                    </span>
                    SensoryPlex
                </Link>
                <div className="workspace-pill">
                    <span className="status-dot ready" />
                    <div>
                        <strong>本地素材工作台</strong>
                        <small>Local workspace</small>
                    </div>
                </div>
                <nav>
                    {['工作空间', '平台管理', '设置'].map((group) => (
                        <div className="nav-group" key={group}>
                            {visible.some((x) => x.group === group) ? <span>{group}</span> : null}
                            {visible
                                .filter((x) => x.group === group)
                                .map((x) => (
                                    <NavLink to={x.path} key={x.path}>
                                        <x.icon size={18} strokeWidth={1.7} />
                                        {x.label}
                                    </NavLink>
                                ))}
                        </div>
                    ))}
                </nav>
                <div className="sidebar-footer">
                    <span className="avatar">{session.display_name.slice(0, 1)}</span>
                    <div>
                        <strong>{session.display_name}</strong>
                        <small>{session.principal}</small>
                    </div>
                    <button
                        aria-label="退出登录"
                        title="退出登录"
                        onClick={() => logout.mutate()}
                        disabled={logout.isPending}
                    >
                        <LogOut size={18} />
                    </button>
                </div>
            </aside>
            <div className="main-shell">
                <header className="topbar">
                    <div>
                        <button
                            className="mobile-toggle"
                            aria-label="切换导航"
                            onClick={() => setMobile(!mobile)}
                        >
                            {mobile ? <X size={19} /> : <Menu size={19} />}
                        </button>
                        <span>SensoryPlex</span>
                        <span className="breadcrumb-divider">/</span>
                        <strong>{current?.label || '工作台'}</strong>
                    </div>
                    <Link to="/system" className="environment">
                        <span className="status-dot" />
                        应用预览<span className="env-separator">·</span>执行链路待接入
                    </Link>
                </header>
                <main>
                    <ErrorNotice error={logout.error} />
                    <Suspense fallback={<Loading />}>
                        <Outlet />
                    </Suspense>
                </main>
                <footer className="page-footer">
                    <span>SensoryPlex · Edge multimodal material runtime</span>
                    <span>Console 0.1</span>
                </footer>
            </div>
        </div>
    );
}

export default function App() {
    const cache = useQueryClient();
    const session = useQuery({
        queryKey: ['session'],
        queryFn: async ({ signal }) => {
            const value = await api<Identity>('/auth/v1/me', { signal });
            setCsrf(value.csrf_token);
            return value;
        },
        retry: false,
        staleTime: 60000,
    });
    useEffect(() => {
        const expired = () => {
            setCsrf('');
            cache.setQueryData(['session'], null);
        };
        window.addEventListener('session-expired', expired);
        return () => window.removeEventListener('session-expired', expired);
    }, [cache]);
    useEffect(() => {
        setCsrf(session.data?.csrf_token || '');
    }, [session.data]);
    if (session.isPending) return <Loading />;
    if (session.error && !(session.error instanceof RequestError && session.error.status === 401))
        return (
            <div className="login-page">
                <section className="card">
                    <ErrorNotice error={session.error} />
                    <button onClick={() => void session.refetch()}>重新连接</button>
                </section>
            </div>
        );
    if (!session.data)
        return (
            <Login
                onLogin={(value) => {
                    setCsrf(value.csrf_token);
                    cache.removeQueries({ predicate: (q) => q.queryKey[0] !== 'session' });
                    cache.setQueryData(['session'], value);
                }}
            />
        );
    const first = navigation.find(
        (x) => !x.scope || session.data!.permissions.includes(x.scope),
    )!.path;
    return (
        <SessionContext.Provider value={session.data}>
            <Routes>
                <Route element={<Layout />}>
                    <Route index element={<Navigate replace to={first} />} />
                    <Route element={<Guard scope="assets:read" />}>
                        <Route path="assets" element={<Assets />} />
                    </Route>
                    <Route element={<Guard scope="jobs:read" />}>
                        <Route path="jobs" element={<Jobs />} />
                    </Route>
                    <Route element={<Guard scope="materials:read" />}>
                        <Route path="materials" element={<Materials />} />
                        <Route path="materials/:id" element={<MaterialDetail />} />
                    </Route>
                    <Route element={<Guard scope="plugins:manage" />}>
                        <Route path="plugins" element={<Plugins />} />
                        <Route path="nodes" element={<Nodes />} />
                    </Route>
                    <Route element={<Guard scope="pipelines:manage" />}>
                        <Route path="pipelines" element={<Pipelines />} />
                    </Route>
                    <Route element={<Guard scope="users:manage" />}>
                        <Route path="users" element={<UserPage />} />
                    </Route>
                    <Route element={<Guard scope="audit:read" />}>
                        <Route path="audit" element={<Audit />} />
                    </Route>
                    <Route path="access" element={<Access />} />
                    <Route path="system" element={<System />} />
                    <Route
                        path="*"
                        element={
                            <Empty title="页面不存在">
                                <Link to={first}>返回工作台</Link>
                            </Empty>
                        }
                    />
                </Route>
            </Routes>
        </SessionContext.Provider>
    );
}
