import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button, Card, Form, Input, Popconfirm, Space, Table, Tag, message } from 'antd';
import { api, post } from '../api/client';
import type { PluginSigner, PluginSignerList } from '../api/contracts';
import { ErrorNotice } from '../components';

export default function PluginTrust() {
    const cache = useQueryClient();
    const [signerForm] = Form.useForm();
    const listing = useQuery({
        queryKey: ['plugin-signers'],
        queryFn: ({ signal }) => api<PluginSignerList>('/admin/v1/plugin-signers', { signal }),
    });
    const refresh = () => {
        void cache.invalidateQueries({ queryKey: ['plugin-signers'] });
    };
    const approve = useMutation({
        mutationFn: (values: { display_name: string; public_key: string }) =>
            post('/admin/v1/plugin-signers', values),
        onSuccess: () => {
            refresh();
            signerForm.resetFields();
        },
    });
    const revoke = useMutation({
        mutationFn: (id: string) => post(`/admin/v1/plugin-signers/${id}:revoke`),
        onSuccess: refresh,
    });
    const importRelease = useMutation({
        mutationFn: async (form: HTMLFormElement) => {
            const data = new FormData(form);
            const bundle = data.get('bundle') as File;
            const descriptor = data.get('descriptor') as File;
            const signature = data.get('signature') as File;
            if (
                !bundle.size ||
                bundle.size > 512 * 1024 * 1024 ||
                descriptor.size > 16000 ||
                signature.size > 128
            ) {
                throw new Error('制品或签名文件超过允许大小');
            }
            const bytes = new Uint8Array(await descriptor.arrayBuffer());
            const encoded = btoa(Array.from(bytes, (byte) => String.fromCharCode(byte)).join(''));
            return api(
                '/admin/v1/plugin-releases:import',
                {
                    method: 'POST',
                    body: bundle,
                    headers: {
                        'Content-Type': 'application/octet-stream',
                        'X-Plugin-Descriptor': encoded,
                        'X-Plugin-Signature': (await signature.text()).trim(),
                        'X-Plugin-Signer': String(data.get('signer')),
                    },
                },
                300000,
            );
        },
        onSuccess: () => {
            message.success('制品已验证并加入插件目录');
            void cache.invalidateQueries({ queryKey: ['catalog'] });
            void cache.invalidateQueries({ queryKey: ['plugin-releases'] });
        },
    });
    const approved = listing.data?.items.filter((s) => !s.revoked_at) || [];
    return (
        <Space orientation="vertical" style={{ width: '100%' }} size="large">
            <ErrorNotice
                error={listing.error || approve.error || revoke.error || importRelease.error}
            />
            <Card title="批准发布者">
                <Form
                    form={signerForm}
                    layout="vertical"
                    onFinish={(values) => approve.mutate(values)}
                >
                    <Form.Item name="display_name" label="发布者名称" rules={[{ required: true }]}>
                        <Input maxLength={120} />
                    </Form.Item>
                    <Form.Item name="public_key" label="Ed25519 公钥" rules={[{ required: true }]}>
                        <Input.TextArea rows={3} maxLength={512} />
                    </Form.Item>
                    <Button type="primary" htmlType="submit" loading={approve.isPending}>
                        批准公钥
                    </Button>
                </Form>
                <Table
                    rowKey="signer_id"
                    dataSource={listing.data?.items || []}
                    loading={listing.isPending}
                    pagination={{ pageSize: 10 }}
                    columns={[
                        { title: '发布者', dataIndex: 'display_name' },
                        {
                            title: '信任状态',
                            render: (_: unknown, signer: PluginSigner) => (
                                <Tag color={signer.revoked_at ? 'red' : 'green'}>
                                    {signer.revoked_at ? '已撤销' : '已批准'}
                                </Tag>
                            ),
                        },
                        {
                            title: '操作',
                            render: (_: unknown, signer: PluginSigner) => (
                                <Popconfirm
                                    title="撤销后将阻止新的导入和部署，现有实例保留供管理员处置。"
                                    onConfirm={() => revoke.mutate(signer.signer_id)}
                                >
                                    <Button danger disabled={Boolean(signer.revoked_at)}>
                                        撤销
                                    </Button>
                                </Popconfirm>
                            ),
                        },
                    ]}
                />
            </Card>
            <Card title="导入独立插件制品">
                <form
                    onSubmit={(event) => {
                        event.preventDefault();
                        importRelease.mutate(event.currentTarget);
                    }}
                >
                    <Space orientation="vertical" style={{ width: '100%' }}>
                        <label>
                            已批准发布者
                            <select name="signer" required style={{ width: '100%', padding: 8 }}>
                                <option value="">请选择发布者</option>
                                {approved.map((s) => (
                                    <option key={s.signer_id} value={s.signer_id}>
                                        {s.display_name}
                                    </option>
                                ))}
                            </select>
                        </label>
                        <label>
                            Bundle 压缩包 <input name="bundle" type="file" accept=".gz" required />
                        </label>
                        <label>
                            发布描述符 release.json{' '}
                            <input name="descriptor" type="file" accept=".json" required />
                        </label>
                        <label>
                            分离式签名 release.sig{' '}
                            <input name="signature" type="file" accept=".sig" required />
                        </label>
                        <Button type="primary" htmlType="submit" loading={importRelease.isPending}>
                            验证并导入
                        </Button>
                    </Space>
                </form>
            </Card>
        </Space>
    );
}
