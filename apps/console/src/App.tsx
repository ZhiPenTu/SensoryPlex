import { lazy, Suspense, useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
    ConfigProvider,
    App as AntApp,
    Button,
    Form,
    Input,
    Alert,
    Tooltip,
    Avatar,
    Dropdown,
    Space,
    Typography,
} from 'antd';
import {
    VideoCameraOutlined,
    ScheduleOutlined,
    SearchOutlined,
    AppstoreOutlined,
    ClusterOutlined,
    ApartmentOutlined,
    TeamOutlined,
    SafetyCertificateOutlined,
    KeyOutlined,
    LogoutOutlined,
    MenuFoldOutlined,
    MenuUnfoldOutlined,
    UserOutlined,
    RightOutlined,
} from '@ant-design/icons';
import { Link, NavLink, Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom';
import { api, post, RequestError, setCsrf } from './api/client';
import type { DemoAccount, Identity } from './api/contracts';
import { Empty, ErrorNotice, Loading } from './components';
import { SessionContext, useSession } from './session';
import { antdTheme } from './theme';
import { Access, Audit, Pipelines, Users as UserPage } from './features/Management';

const Assets = lazy(() => import('./features/Assets'));
const Jobs = lazy(() => import('./features/Jobs'));
const Materials = lazy(() => import('./features/Materials'));
const Plugins = lazy(() => import('./features/Plugins'));
const Nodes = lazy(() => import('./features/Nodes'));
const MaterialDetail = lazy(() =>
    import('./features/Materials').then((m) => ({ default: m.MaterialDetail })),
);

const { Title, Text } = Typography;

const navigation = [
    {
        path: '/assets',
        label: '视频库',
        icon: VideoCameraOutlined,
        scope: 'assets:read',
        group: '工作空间',
    },
    {
        path: '/jobs',
        label: '处理任务',
        icon: ScheduleOutlined,
        scope: 'jobs:read',
        group: '工作空间',
    },
    {
        path: '/materials',
        label: '素材检索',
        icon: SearchOutlined,
        scope: 'materials:read',
        group: '工作空间',
    },
    {
        path: '/plugins',
        label: '插件中心',
        icon: AppstoreOutlined,
        scope: 'plugins:manage',
        group: '平台管理',
    },
    {
        path: '/nodes',
        label: '节点拓扑',
        icon: ClusterOutlined,
        scope: 'plugins:manage',
        group: '平台管理',
    },
    {
        path: '/pipelines',
        label: '处理方案',
        icon: ApartmentOutlined,
        scope: 'pipelines:manage',
        group: '平台管理',
    },
    {
        path: '/users',
        label: '用户与权限',
        icon: TeamOutlined,
        scope: 'users:manage',
        group: '平台管理',
    },
    {
        path: '/audit',
        label: '操作审计',
        icon: SafetyCertificateOutlined,
        scope: 'audit:read',
        group: '平台管理',
    },
    {
        path: '/access',
        label: '访问凭据',
        icon: KeyOutlined,
        scope: '',
        group: '设置',
    },
];

function Login({ onLogin }: { onLogin: (identity: Identity) => void }) {
    const [form] = Form.useForm();
    const [demoFilled, setDemoFilled] = useState(false);

    const demo = useQuery({
        queryKey: ['demo-account'],
        queryFn: ({ signal }) => api<DemoAccount>('/auth/v1/demo-account', { signal }),
        staleTime: 0,
    });

    const mutation = useMutation({
        mutationFn: (values: { username?: string; password?: string }) =>
            post<Identity>('/auth/v1/session', values),
        onSuccess: onLogin,
    });

    useEffect(() => {
        if (demo.data?.enabled && !demoFilled) {
            form.setFieldsValue({
                username: demo.data.username,
                password: demo.data.password,
            });
            setDemoFilled(true);
        }
    }, [demo.data, demoFilled, form]);

    const handleQuickLogin = () => {
        if (!demo.data?.enabled) return;
        mutation.reset();
        form.setFieldsValue({
            username: demo.data.username,
            password: demo.data.password,
        });
        mutation.mutate({
            username: demo.data.username,
            password: demo.data.password,
        });
    };

    return (
        <div className="login-page">
            <section className="login-story">
                <Link className="brand" to="/">
                    <span className="brand-mark">
                        <ApartmentOutlined style={{ fontSize: 22 }} />
                    </span>
                    SensoryPlex
                </Link>
                <div className="login-story-content">
                    <div style={{ marginBottom: 16 }}>
                        <span
                            style={{
                                color: '#38bdf8',
                                fontSize: 11,
                                fontWeight: 700,
                                letterSpacing: '2px',
                                textTransform: 'uppercase',
                            }}
                        >
                            High-Performance Multimodal Runtime
                        </span>
                    </div>
                    <h1>
                        让每一段画面，
                        <br />
                        都有据可循。
                    </h1>
                    <p>
                        连接边缘节点、多模态感知与毫秒时间轴，
                        <br />
                        将非结构化连续视频沉淀为可精确索引的素材单元。
                    </p>
                    <div className="login-pipeline">
                        <span>原始视频</span>
                        <RightOutlined style={{ fontSize: 11, color: '#64748b' }} />
                        <span>多模态感知</span>
                        <RightOutlined style={{ fontSize: 11, color: '#64748b' }} />
                        <span>结构化素材</span>
                    </div>
                    <div className="login-features">
                        <div>
                            <strong>OCR</strong>
                            <span>画面文字</span>
                        </div>
                        <div>
                            <strong>ASR</strong>
                            <span>语音转写</span>
                        </div>
                        <div>
                            <strong>VLM</strong>
                            <span>场景理解</span>
                        </div>
                    </div>
                </div>
                <div className="login-story-footer">
                    端侧就地处理 · 历史全量追溯 · 契约数据自主掌握
                </div>
            </section>

            <section className="login-panel">
                <div className="login-panel-inner">
                    <Link className="brand login-mobile-brand" to="/">
                        <span className="brand-mark">
                            <ApartmentOutlined />
                        </span>
                        SensoryPlex
                    </Link>
                    <div style={{ marginBottom: 28 }}>
                        <span
                            style={{
                                color: 'var(--sp-primary)',
                                fontSize: 11,
                                fontWeight: 700,
                                letterSpacing: '1.5px',
                                textTransform: 'uppercase',
                                display: 'block',
                                marginBottom: 6,
                            }}
                        >
                            Material Console
                        </span>
                        <Title level={3} style={{ margin: 0, fontWeight: 700 }}>
                            登录工作台
                        </Title>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                            使用当前边缘计算节点的本地账户验证继续。
                        </Text>
                    </div>

                    <ErrorNotice error={mutation.error} />

                    <Form
                        form={form}
                        layout="vertical"
                        size="large"
                        onFinish={(values) => mutation.mutate(values)}
                    >
                        <Form.Item
                            name="username"
                            label={<span style={{ fontWeight: 600, fontSize: 13 }}>用户名</span>}
                            rules={[{ required: true, message: '请输入用户名' }]}
                        >
                            <Input
                                placeholder="输入用户名"
                                autoFocus
                                autoComplete="username"
                                maxLength={64}
                                onChange={() => setDemoFilled(false)}
                            />
                        </Form.Item>

                        <Form.Item
                            name="password"
                            label={<span style={{ fontWeight: 600, fontSize: 13 }}>密码</span>}
                            rules={[{ required: true, message: '请输入密码' }]}
                        >
                            <Input.Password
                                placeholder="输入登录密码"
                                autoComplete="current-password"
                                maxLength={256}
                                onChange={() => setDemoFilled(false)}
                            />
                        </Form.Item>

                        <Form.Item style={{ marginTop: 24, marginBottom: 12 }}>
                            {demo.data?.enabled ? (
                                <Button
                                    type="primary"
                                    block
                                    loading={mutation.isPending}
                                    onClick={handleQuickLogin}
                                    style={{ height: 42, fontSize: 15 }}
                                    icon={<UserOutlined />}
                                >
                                    {mutation.isPending
                                        ? '正在快速登录…'
                                        : `Demo 快速登录 (${demo.data.username})`}
                                </Button>
                            ) : (
                                <Button
                                    type="primary"
                                    htmlType="submit"
                                    block
                                    loading={mutation.isPending}
                                    style={{ height: 42, fontSize: 15 }}
                                >
                                    {mutation.isPending ? '正在验证身份…' : '登录工作台'}
                                </Button>
                            )}
                        </Form.Item>

                        {demo.data?.enabled ? (
                            <Form.Item style={{ marginBottom: 16 }}>
                                <Button
                                    type="default"
                                    htmlType="submit"
                                    block
                                    disabled={mutation.isPending}
                                    style={{ height: 38, fontSize: 13 }}
                                >
                                    使用表单凭据登录
                                </Button>
                            </Form.Item>
                        ) : null}
                    </Form>

                    <Alert
                        type="info"
                        showIcon={false}
                        style={{ marginTop: 8, fontSize: 12, borderRadius: 6 }}
                        message={
                            <span style={{ color: '#475569' }}>
                                {demo.data?.enabled
                                    ? '当前单机环境已激活 Demo 快速登录，点击上方按钮即可一键进入工作台。'
                                    : '首次部署使用需由节点管理员分配系统凭据。'}
                            </span>
                        }
                    />

                    <div className="login-copyright">
                        SensoryPlex Multimodal Console · 工业预览版
                    </div>
                </div>
            </section>
        </div>
    );
}

function Guard({ scope }: { scope: string }) {
    const session = useSession();
    return !scope || session.permissions.includes(scope) ? (
        <Outlet />
    ) : (
        <div style={{ padding: '60px 0' }}>
            <Empty title="无权访问此页面">请联系节点系统管理员授予当前账户相应操作权限。</Empty>
        </div>
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

    useEffect(() => {
        if (!mobile) return;
        const closeOnEscape = (event: KeyboardEvent) => {
            if (event.key === 'Escape') setMobile(false);
        };
        window.addEventListener('keydown', closeOnEscape);
        return () => window.removeEventListener('keydown', closeOnEscape);
    }, [mobile]);

    const current = navigation.find((x) => location.pathname.startsWith(x.path));
    const userInitial =
        Array.from(session.display_name || session.principal)[0]?.toUpperCase() || 'S';
    const visible = navigation.filter((x) => !x.scope || session.permissions.includes(x.scope));
    const groups = Array.from(new Set(visible.map((x) => x.group)));

    const userMenuItems = [
        {
            key: 'user-info',
            label: (
                <div style={{ padding: '4px 0' }}>
                    <div style={{ fontWeight: 600 }}>
                        {session.display_name || session.principal}
                    </div>
                    <div style={{ fontSize: 11, color: '#94a3b8' }}>{session.roles.join(', ')}</div>
                </div>
            ),
            disabled: true,
        },
        { type: 'divider' as const },
        {
            key: 'logout',
            icon: <LogoutOutlined />,
            danger: true,
            label: '退出登录',
            onClick: () => logout.mutate(),
        },
    ];

    return (
        <div className="app-shell">
            {mobile ? (
                <button
                    className="sidebar-backdrop"
                    aria-label="关闭导航"
                    onClick={() => setMobile(false)}
                />
            ) : null}
            <aside id="console-navigation" className={`sidebar ${mobile ? 'mobile-open' : ''}`}>
                <Link to="/" className="brand">
                    <span className="brand-mark">
                        <ApartmentOutlined style={{ fontSize: 20 }} />
                    </span>
                    <span>SensoryPlex</span>
                </Link>

                <div className="workspace-pill">
                    <div style={{ flex: 1 }}>
                        <strong>本地工作空间</strong>
                        <small>SensoryPlex Console</small>
                    </div>
                    <span
                        style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: 5,
                            background: 'rgba(5, 150, 105, 0.15)',
                            border: '1px solid rgba(5, 150, 105, 0.25)',
                            padding: '2px 7px',
                            borderRadius: 12,
                        }}
                    >
                        <span
                            style={{
                                width: 5,
                                height: 5,
                                borderRadius: '50%',
                                background: '#10b981',
                                display: 'inline-block',
                            }}
                        />
                        <span style={{ color: '#6ee7b7', fontSize: 10, fontWeight: 600 }}>
                            已登录
                        </span>
                    </span>
                </div>

                <nav aria-label="主导航">
                    {groups.map((group) => (
                        <div key={group} className="nav-group">
                            <span>{group}</span>
                            {visible
                                .filter((item) => item.group === group)
                                .map((item) => {
                                    const Icon = item.icon;
                                    const isActive = location.pathname.startsWith(item.path);
                                    return (
                                        <NavLink
                                            key={item.path}
                                            to={item.path}
                                            className={isActive ? 'active' : ''}
                                        >
                                            <Icon style={{ fontSize: 15 }} />
                                            <span>{item.label}</span>
                                        </NavLink>
                                    );
                                })}
                        </div>
                    ))}
                </nav>

                <div className="sidebar-footer">
                    <div className="user-avatar">{userInitial}</div>
                    <div style={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
                        <div
                            style={{
                                color: '#f1f5f9',
                                fontSize: 12,
                                fontWeight: 600,
                                textOverflow: 'ellipsis',
                                overflow: 'hidden',
                                whiteSpace: 'nowrap',
                            }}
                        >
                            {session.display_name || session.principal}
                        </div>
                        <div style={{ color: '#64748b', fontSize: 11 }}>
                            {session.roles[0] || 'operator'}
                        </div>
                    </div>
                    <Tooltip title="退出登录">
                        <Button
                            aria-label="退出登录"
                            type="text"
                            size="small"
                            icon={<LogoutOutlined style={{ color: '#94a3b8' }} />}
                            onClick={() => logout.mutate()}
                        />
                    </Tooltip>
                </div>
            </aside>

            <div className="main-shell">
                <header className="topbar">
                    <div className="topbar-left">
                        <button
                            className="mobile-toggle"
                            aria-label="切换导航"
                            aria-expanded={mobile}
                            aria-controls="console-navigation"
                            onClick={() => setMobile(!mobile)}
                        >
                            {mobile ? (
                                <MenuFoldOutlined style={{ fontSize: 18 }} />
                            ) : (
                                <MenuUnfoldOutlined style={{ fontSize: 18 }} />
                            )}
                        </button>
                        <Text type="secondary" style={{ fontSize: 13 }}>
                            SensoryPlex
                        </Text>
                        <RightOutlined className="breadcrumb-divider" aria-hidden="true" />
                        <Text strong style={{ fontSize: 13 }}>
                            {current?.label || '控制台'}
                        </Text>
                    </div>

                    <Space size={10}>
                        <div className="environment">
                            <span className="status-dot" />
                            <span>本地控制台</span>
                            <span style={{ color: '#cbd5e1' }}>|</span>
                            <span style={{ color: '#64748b' }}>v0.1.0-alpha</span>
                        </div>

                        <Dropdown
                            menu={{ items: userMenuItems }}
                            placement="bottomRight"
                            trigger={['click']}
                        >
                            <button className="profile-button" aria-label="打开账户菜单">
                                <Avatar size={28}>{userInitial}</Avatar>
                            </button>
                        </Dropdown>
                    </Space>
                </header>

                <main>
                    <ErrorNotice error={logout.error} />
                    <Suspense fallback={<Loading tip="正在加载模块…" />}>
                        <Outlet />
                    </Suspense>
                </main>

                <footer className="page-footer">
                    <span>SensoryPlex · High Performance Multimodal Material Runtime</span>
                    <span>Console Edition 0.1.0</span>
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

    return (
        <ConfigProvider theme={antdTheme}>
            <AntApp>
                {(() => {
                    if (session.isPending) return <Loading tip="正在连接控制面…" />;
                    if (
                        session.error &&
                        !(session.error instanceof RequestError && session.error.status === 401)
                    ) {
                        return (
                            <div className="login-page">
                                <div style={{ margin: 'auto', maxWidth: 440, padding: 32 }}>
                                    <ErrorNotice error={session.error} />
                                    <Button
                                        type="primary"
                                        block
                                        onClick={() => void session.refetch()}
                                    >
                                        重新连接控制面
                                    </Button>
                                </div>
                            </div>
                        );
                    }
                    if (!session.data) {
                        return (
                            <Login
                                onLogin={(value) => {
                                    setCsrf(value.csrf_token);
                                    cache.removeQueries({
                                        predicate: (q) => q.queryKey[0] !== 'session',
                                    });
                                    cache.setQueryData(['session'], value);
                                }}
                            />
                        );
                    }

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
                })()}
            </AntApp>
        </ConfigProvider>
    );
}
