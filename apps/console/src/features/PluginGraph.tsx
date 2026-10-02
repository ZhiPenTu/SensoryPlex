import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Alert, Button, Card, Form, Input, Select, Space, Table } from 'antd';
import { api, post } from '../api/client';
import type { Pipeline, PluginConfigList, PluginList } from '../api/contracts';
import { ErrorNotice, Modal } from '../components';

type GraphNode = {
    id: string;
    release_id: string;
    config_id: string;
    input_selector: string;
    execution_mode: string;
};
type GraphForm = {
    name: string;
    nodes: GraphNode[];
    edges: { from_node_id: string; to_node_id: string; modality: string }[];
};
type Validation = { valid: boolean; topological_order: string[]; graph_digest: string };

export default function PluginGraph({ onClose }: { onClose: () => void }) {
    const cache = useQueryClient();
    const [form] = Form.useForm<GraphForm>();
    const [validation, setValidation] = useState<Validation | null>(null);
    const nodes = Form.useWatch('nodes', form) || [];
    const catalog = useQuery({
        queryKey: ['catalog'],
        queryFn: ({ signal }) => api<PluginList>('/admin/v1/catalog', { signal }),
    });
    const configs = useQuery({
        queryKey: ['configs'],
        queryFn: ({ signal }) =>
            api<PluginConfigList>('/admin/v1/plugin-configurations?limit=100', { signal }),
    });
    const releases =
        catalog.data?.items.filter(
            (p) => p.manifest_version === 'edge.material.plugin/v2' && p.state !== 'revoked',
        ) || [];
    const check = useMutation({
        mutationFn: (values: GraphForm) =>
            post<Validation>('/admin/v1/plugin-graphs:validate', values),
        onSuccess: setValidation,
    });
    const publish = useMutation({
        mutationFn: async (values: GraphForm) => {
            const checked = await post<Validation>('/admin/v1/plugin-graphs:validate', values);
            if (!checked.valid) throw new Error('插件连接校验未通过');
            const created = await post<Pipeline>('/admin/v1/plugin-graphs', values);
            return post(`/admin/v1/pipelines/${created.id}:publish`);
        },
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['pipelines'] });
            onClose();
        },
    });
    return (
        <Modal title="新建插件图" onClose={onClose} width={1000}>
            <ErrorNotice error={catalog.error || configs.error || check.error || publish.error} />
            <Form
                form={form}
                layout="vertical"
                initialValues={{
                    nodes: [{ id: 'processor', input_selector: 'media', execution_mode: 'sync' }],
                    edges: [],
                }}
                onValuesChange={() => setValidation(null)}
                onFinish={(values) => publish.mutate(values)}
            >
                <Form.Item name="name" label="方案名称" rules={[{ required: true }]}>
                    <Input maxLength={120} />
                </Form.Item>
                <Card size="small" title="节点与锁定版本">
                    <Form.List name="nodes">
                        {(fields, { add, remove }) => (
                            <>
                                {fields.map((field) => {
                                    const selected = releases.find(
                                        (p) => p.release_id === nodes[field.name]?.release_id,
                                    );
                                    return (
                                        <Space
                                            key={field.key}
                                            align="start"
                                            wrap
                                            style={{ display: 'flex', marginBottom: 12 }}
                                        >
                                            <Form.Item
                                                name={[field.name, 'id']}
                                                label="节点名称"
                                                rules={[{ required: true }]}
                                            >
                                                <Input
                                                    placeholder="measurement"
                                                    style={{ width: 130 }}
                                                    maxLength={64}
                                                />
                                            </Form.Item>
                                            <Form.Item
                                                name={[field.name, 'release_id']}
                                                label="插件版本"
                                                rules={[{ required: true }]}
                                            >
                                                <Select
                                                    style={{ width: 240 }}
                                                    onChange={() =>
                                                        form.setFieldValue(
                                                            ['nodes', field.name, 'config_id'],
                                                            undefined,
                                                        )
                                                    }
                                                    options={releases.map((p) => ({
                                                        value: p.release_id,
                                                        label: `${p.name} · ${p.version} · ${p.release_id.slice(-8)}`,
                                                    }))}
                                                />
                                            </Form.Item>
                                            <Form.Item
                                                name={[field.name, 'config_id']}
                                                label="参数方案"
                                                rules={[{ required: true }]}
                                            >
                                                <Select
                                                    style={{ width: 170 }}
                                                    options={configs.data?.items
                                                        .filter((c) => c.plugin_id === selected?.id)
                                                        .map((c) => ({
                                                            value: c.id,
                                                            label: c.name,
                                                        }))}
                                                />
                                            </Form.Item>
                                            <Form.Item
                                                name={[field.name, 'input_selector']}
                                                label="输入来源"
                                                rules={[{ required: true }]}
                                            >
                                                <Select
                                                    style={{ width: 150 }}
                                                    options={[
                                                        { value: 'media', label: '原始媒体' },
                                                        ...nodes
                                                            .filter(
                                                                (_: GraphNode, i: number) =>
                                                                    i !== field.name,
                                                            )
                                                            .filter((n: GraphNode) => n.id)
                                                            .map((n: GraphNode) => ({
                                                                value: `node:${n.id}`,
                                                                label: `上游 ${n.id}`,
                                                            })),
                                                    ]}
                                                />
                                            </Form.Item>
                                            <Form.Item
                                                name={[field.name, 'execution_mode']}
                                                label="执行方式"
                                                rules={[{ required: true }]}
                                            >
                                                <Select
                                                    style={{ width: 135 }}
                                                    options={(
                                                        selected?.execution_modes || [
                                                            'sync',
                                                            'async_enrichment',
                                                        ]
                                                    ).map((mode) => ({
                                                        value: mode,
                                                        label:
                                                            mode === 'sync'
                                                                ? '同步处理'
                                                                : '异步补全',
                                                    }))}
                                                />
                                            </Form.Item>
                                            <Button
                                                danger
                                                onClick={() => remove(field.name)}
                                                disabled={fields.length <= 1}
                                            >
                                                移除
                                            </Button>
                                        </Space>
                                    );
                                })}
                                <Button
                                    onClick={() =>
                                        add({
                                            id: `processor_${fields.length + 1}`,
                                            input_selector: 'media',
                                            execution_mode: 'sync',
                                        })
                                    }
                                    disabled={fields.length >= 32}
                                >
                                    添加节点
                                </Button>
                            </>
                        )}
                    </Form.List>
                </Card>
                <Card
                    size="small"
                    title="连接表（输入来源会自动建立依赖，可在此校验明确的同步连接）"
                    style={{ marginTop: 12 }}
                >
                    <Form.List name="edges">
                        {(fields, { add, remove }) => (
                            <>
                                {fields.map((field) => (
                                    <Space key={field.key} align="start" wrap>
                                        <Form.Item
                                            name={[field.name, 'from_node_id']}
                                            label="上游"
                                            rules={[{ required: true }]}
                                        >
                                            <Select
                                                style={{ width: 180 }}
                                                options={nodes.map((n: GraphNode) => ({
                                                    label: n.id,
                                                    value: n.id,
                                                }))}
                                            />
                                        </Form.Item>
                                        <Form.Item
                                            name={[field.name, 'to_node_id']}
                                            label="下游"
                                            rules={[{ required: true }]}
                                        >
                                            <Select
                                                style={{ width: 180 }}
                                                options={nodes.map((n: GraphNode) => ({
                                                    label: n.id,
                                                    value: n.id,
                                                }))}
                                            />
                                        </Form.Item>
                                        <Form.Item
                                            name={[field.name, 'modality']}
                                            label="数据类型"
                                            rules={[{ required: true }]}
                                        >
                                            <Input style={{ width: 280 }} />
                                        </Form.Item>
                                        <Button onClick={() => remove(field.name)}>移除连接</Button>
                                    </Space>
                                ))}
                                <Button onClick={() => add({})} disabled={fields.length >= 128}>
                                    添加连接
                                </Button>
                            </>
                        )}
                    </Form.List>
                </Card>
                <Card size="small" title="只读图预览" style={{ marginTop: 12 }}>
                    <Table
                        rowKey={(_, i) => String(i)}
                        dataSource={nodes}
                        pagination={false}
                        size="small"
                        columns={[
                            {
                                title: '来源 → 处理器 → Timeline',
                                render: (_, n: GraphNode) =>
                                    `${n.input_selector === 'media' ? '原片' : n.input_selector?.slice(5)} → ${n.id || '待命名'} → Timeline`,
                            },
                            {
                                title: '执行',
                                render: (_, n: GraphNode) =>
                                    n.execution_mode === 'sync' ? '同步' : '异步补全',
                            },
                            { title: '绑定制品', dataIndex: 'release_id' },
                        ]}
                    />
                </Card>
                {validation?.valid ? (
                    <Alert
                        style={{ marginTop: 12 }}
                        type="success"
                        showIcon
                        message="连接、数据类型与 schema 校验通过"
                        description={`执行顺序：${validation.topological_order.join(' → ')}`}
                    />
                ) : null}
                <Space style={{ marginTop: 16 }}>
                    <Button
                        onClick={() =>
                            void form.validateFields().then((values) => check.mutate(values))
                        }
                        loading={check.isPending}
                    >
                        校验连接
                    </Button>
                    <Button type="primary" htmlType="submit" loading={publish.isPending}>
                        创建并发布
                    </Button>
                </Space>
            </Form>
        </Modal>
    );
}
