import { useMemo } from 'react';
import { Alert, Table, Tag, Typography } from 'antd';
import type { JsonObject, ObservationIndexStatus } from '../api/contracts';

const indexLabels: Record<string, string> = {
    not_declared: '未声明检索文本',
    empty_text: '没有可检索文本',
    pending: '等待索引',
    ready: '已可检索',
    failed: '索引失败',
};

export function ResultIndex({ status }: { status?: ObservationIndexStatus }) {
    if (!status) return <Tag>索引状态待查询</Tag>;
    return (
        <Tag
            color={
                status.state === 'ready'
                    ? 'success'
                    : status.state === 'failed'
                      ? 'error'
                      : 'default'
            }
        >
            {indexLabels[status.state] || status.state}
            {status.reason_code ? ` · ${status.reason_code}` : ''}
        </Tag>
    );
}

export default function PluginResult({ payload }: { payload?: JsonObject }) {
    const view = useMemo(() => {
        const rows: { path: string; value: string }[] = [];
        let limited = false;
        function visit(value: unknown, path: string, depth: number) {
            if (rows.length >= 128) {
                limited = true;
                return;
            }
            if (depth >= 8 && value !== null && typeof value === 'object') {
                limited = true;
                rows.push({ path, value: '嵌套内容请查看 JSON' });
                return;
            }
            if (value !== null && typeof value === 'object') {
                for (const [key, child] of Object.entries(value)) {
                    visit(child, path ? `${path}.${key}` : key, depth + 1);
                    if (rows.length >= 128) {
                        limited = true;
                        break;
                    }
                }
            } else {
                const text = value === null ? 'null' : String(value);
                rows.push({ path, value: text.length > 1000 ? text.slice(0, 1000) + '…' : text });
            }
        }
        visit(payload || {}, '', 0);
        const json = JSON.stringify(payload || {}, null, 2);
        return {
            rows,
            limited,
            json: new TextEncoder().encode(json).length <= 65536 ? json : null,
        };
    }, [payload]);
    return (
        <>
            {view.limited ? (
                <Alert type="info" message="字段表最多展示 128 项、8 层嵌套。" />
            ) : null}
            <Table
                rowKey="path"
                size="small"
                dataSource={view.rows}
                pagination={{ pageSize: 20 }}
                columns={[
                    { title: '字段', dataIndex: 'path', width: '45%' },
                    {
                        title: '值',
                        dataIndex: 'value',
                        render: (value: string) => (
                            <Typography.Text style={{ overflowWrap: 'anywhere' }}>
                                {value}
                            </Typography.Text>
                        ),
                    },
                ]}
            />
            {view.json === null ? (
                <Alert type="info" message="结构化结果超过 64 KiB，页面仅展示字段表。" />
            ) : (
                <pre
                    style={{
                        margin: 0,
                        padding: 12,
                        background: '#f8fafc',
                        maxHeight: 260,
                        overflow: 'auto',
                        whiteSpace: 'pre-wrap',
                        overflowWrap: 'anywhere',
                    }}
                >
                    {view.json}
                </pre>
            )}
        </>
    );
}
