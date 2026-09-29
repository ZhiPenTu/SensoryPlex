import { Button, Card, Pagination, Space, Tag, Typography } from 'antd';
import { formatTime } from './material-utils';

export interface SecondReference {
    material_unit_id: string;
    material_revision: number;
    stream_id: string;
    start_ms: number;
    end_ms: number;
    status: string;
    observation_count: number;
}

export interface ExecutionTimeline {
    windows: {
        stream_id: string;
        start_ms: number;
        end_ms: number;
        modality_states: Record<string, string>;
    }[];
    material_references: SecondReference[];
}

export default function ExecutionSeconds({
    timeline,
    page,
    onPage,
    onComplete,
    completing,
}: {
    timeline: ExecutionTimeline;
    page: number;
    onPage: (page: number) => void;
    onComplete: () => void;
    completing: boolean;
}) {
    const windows = timeline.windows;
    const references = new Map(
        timeline.material_references.map((unit) => [`${unit.stream_id}:${unit.start_ms}`, unit]),
    );
    const described = windows.filter(
        (window) => window.modality_states['vision.scene_description'] === 'observed',
    ).length;
    const queued = windows.filter((window) =>
        ['queued', 'running'].includes(window.modality_states['vision.scene_description']),
    ).length;
    const failed = windows.filter(
        (window) => window.modality_states['vision.scene_description'] === 'failed',
    ).length;
    const duration = Math.max(0, ...windows.map((window) => window.end_ms));
    return (
        <Card
            size="small"
            style={{ marginBottom: 12 }}
            title={`完整视频 · ${windows.length} 个逐秒切片`}
        >
            <Space wrap style={{ marginBottom: 10 }}>
                <Typography.Text>
                    00:00.000 — {formatTime(String(duration))} · 每秒一片，保留最后不足一秒
                </Typography.Text>
                <Tag color="green">
                    画面摘要 {described}/{windows.length}
                </Tag>
                <Tag color="blue">排队中 {queued}</Tag>
                {failed ? <Tag color="red">分析失败 {failed}</Tag> : null}
                {queued + described + failed < windows.length ||
                references.size < windows.length ? (
                    <Button size="small" loading={completing} onClick={onComplete}>
                        补齐逐秒切片与摘要
                    </Button>
                ) : null}
            </Space>
            <div
                aria-label="完整逐秒索引"
                style={{
                    display: 'grid',
                    gridTemplateColumns: 'repeat(auto-fill, minmax(32px, 1fr))',
                    gap: 3,
                    maxHeight: 200,
                    overflowY: 'auto',
                    padding: 3,
                }}
            >
                {windows.map((window, index) => {
                    const unit = references.get(`${window.stream_id}:${window.start_ms}`);
                    const ready = window.modality_states['vision.scene_description'] === 'observed';
                    const rejected =
                        window.modality_states['vision.scene_description'] === 'failed';
                    const current = Math.floor(index / 100) + 1 === page;
                    const label = ready
                        ? '已有摘要'
                        : rejected
                          ? '分析失败'
                          : unit?.observation_count
                            ? '已有部分观测，摘要待补充'
                            : '待分析';
                    return (
                        <button
                            key={`${window.stream_id}:${window.start_ms}`}
                            aria-label={`第 ${index + 1} 个切片，${window.start_ms / 1000} 秒，${label}`}
                            title={`${formatTime(String(window.start_ms))} — ${formatTime(String(window.end_ms))} · ${label}`}
                            onClick={() => onPage(Math.floor(index / 100) + 1)}
                            style={{
                                cursor: 'pointer',
                                fontSize: 10,
                                padding: '3px 0',
                                borderRadius: 3,
                                border: `1px solid ${current ? '#1677ff' : '#cbd5e1'}`,
                                background: ready
                                    ? '#dcfce7'
                                    : rejected
                                      ? '#fee2e2'
                                      : unit?.observation_count
                                        ? '#e0f2fe'
                                        : '#f1f5f9',
                                color: '#334155',
                            }}
                        >
                            {window.start_ms / 1000}
                        </button>
                    );
                })}
            </div>
            <Space wrap style={{ marginTop: 12 }}>
                <Typography.Text type="secondary">
                    下方按时间分页显示详情，每页最多 100 片。灰色为待分析，绿色为已有摘要。
                </Typography.Text>
                <Pagination
                    current={page}
                    total={windows.length}
                    pageSize={100}
                    showSizeChanger={false}
                    onChange={onPage}
                />
            </Space>
        </Card>
    );
}
