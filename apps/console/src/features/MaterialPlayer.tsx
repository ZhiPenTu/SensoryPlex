import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button, Card, Checkbox, Select, Space, Tag, Typography } from 'antd';
import {
    CaretRightOutlined,
    UndoOutlined,
    VideoCameraOutlined,
    ReloadOutlined,
    EyeInvisibleOutlined,
} from '@ant-design/icons';
import { api } from '../api/client';
import type { MaterialUnit, Observation, TimeRange, Upload } from '../api/contracts';
import { ErrorNotice, Loading, Notice } from '../components';
import { usePermission } from '../session';
import { covers, formatTime, playableRange } from './material-utils';

const { Text } = Typography;

export default function MaterialPlayer({
    material,
    observation,
    seekSequence,
}: {
    material: MaterialUnit;
    observation?: Observation;
    seekSequence: number;
}) {
    const canRead = usePermission('assets:read');
    const selectionKey = `${observation?.observation_id || ''}:${seekSequence}`;
    const [choice, setChoice] = useState<{ key: string; index: number } | null>(null);
    const explicit = choice?.key === selectionKey;
    const index = explicit
        ? choice.index
        : observation
          ? material.source_refs.findIndex((ref) => covers(ref.time_range, observation.time_range))
          : material.source_refs.length
            ? 0
            : -1;
    const source = material.source_refs[index];
    const range = !explicit && observation ? observation.time_range : source?.time_range;

    const result = useQuery({
        queryKey: [
            'material-source',
            material.material_unit_id,
            material.revision,
            source?.asset_id,
        ],
        queryFn: ({ signal }) =>
            api<Upload>(
                `/v1/materials/${encodeURIComponent(material.material_unit_id)}/sources/${encodeURIComponent(source!.asset_id)}?revision=${material.revision}`,
                { signal },
            ),
        enabled: canRead && !!source,
        retry: false,
        staleTime: 0,
    });

    return (
        <Card
            title={
                <div
                    style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                    }}
                >
                    <Space size={8}>
                        <VideoCameraOutlined style={{ color: '#1668dc' }} />
                        <span style={{ fontWeight: 650, fontSize: 15 }}>原片视听回放</span>
                    </Space>
                    <Tag color="blue" style={{ margin: 0, fontSize: 11 }}>
                        {explicit ? '指定来源区间' : observation ? '当前观测对齐区间' : '来源区间'}
                    </Tag>
                </div>
            }
            style={{ marginBottom: 20 }}
            bodyStyle={{ padding: 0 }}
        >
            {material.source_refs.length ? (
                <div
                    style={{
                        padding: '10px 16px',
                        borderBottom: '1px solid #f1f5f9',
                        display: 'flex',
                        alignItems: 'center',
                        gap: 12,
                        background: '#f8fafc',
                    }}
                >
                    <Text type="secondary" style={{ fontSize: 12, flexShrink: 0 }}>
                        视频来源:
                    </Text>
                    <Select
                        aria-label="回看来源"
                        value={index}
                        onChange={(value) => setChoice({ key: selectionKey, index: Number(value) })}
                        style={{ flex: 1 }}
                        size="small"
                        options={material.source_refs.map((ref, i) => ({
                            label: `${ref.asset_id.slice(0, 16)}… · [${formatTime(ref.time_range?.start_ms)} ~ ${formatTime(ref.time_range?.end_ms)}]`,
                            value: i,
                        }))}
                    />
                </div>
            ) : null}

            {!canRead ? (
                <div style={{ padding: 20 }}>
                    <Notice>当前账户没有原片读取权限，可继续查看素材结构化观测。</Notice>
                </div>
            ) : !source ? (
                <div style={{ padding: '48px 24px', textAlign: 'center' }}>
                    <EyeInvisibleOutlined
                        style={{ fontSize: 32, color: '#94a3b8', marginBottom: 8 }}
                    />
                    <div style={{ fontWeight: 600, color: '#334155' }}>
                        {observation ? '没有覆盖该观测的来源区间' : '暂无原片来源'}
                    </div>
                    <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 4 }}>
                        已登记的其它来源可在上方单独选择回看。
                    </Text>
                </div>
            ) : result.isPending ? (
                <div style={{ padding: 32 }}>
                    <Loading tip="正在连接视频源…" />
                </div>
            ) : result.error ? (
                <div style={{ padding: 20 }}>
                    <ErrorNotice error={result.error} />
                    <Button
                        size="small"
                        icon={<ReloadOutlined />}
                        onClick={() => void result.refetch()}
                        disabled={result.isFetching}
                    >
                        重新检查原片
                    </Button>
                </div>
            ) : result.data ? (
                <VideoReview
                    key={result.data.id}
                    upload={result.data}
                    range={range}
                    seekSequence={seekSequence}
                />
            ) : null}
        </Card>
    );
}

function VideoReview({
    upload,
    range,
    seekSequence,
}: {
    upload: Upload;
    range?: TimeRange;
    seekSequence: number;
}) {
    const video = useRef<HTMLVideoElement>(null);
    const [error, setError] = useState<Error | null>(null);
    const [position, setPosition] = useState<number | null>(null);
    const [seeking, setSeeking] = useState(false);
    const [loaded, setLoaded] = useState(false);
    const [clipOnly, setClipOnly] = useState(true);
    const target = playableRange(range);
    const currentTarget = useRef(target);
    currentTarget.current = target;
    const start = target ? target[0] : undefined;
    const end = target ? target[1] : undefined;
    const source = `/v1/assets/${encodeURIComponent(upload.id)}/content`;

    function locate(play = false) {
        const player = video.current,
            window = currentTarget.current;
        if (!player || !window) return;
        if (
            !Number.isFinite(player.duration) ||
            window[0] >= player.duration ||
            window[1] > player.duration + 0.1
        ) {
            setError(new Error('来源时间区间超出原片可播放时长，无法准确定位。'));
            return;
        }
        try {
            setError(null);
            player.currentTime = window[0];
            if (play)
                player.play().catch(() => setError(new Error('播放未开始，请通过播放器重试。')));
        } catch {
            setError(new Error('浏览器暂时无法定位到该时间点，请重新载入原片。'));
        }
    }

    useEffect(() => {
        const player = video.current,
            window = currentTarget.current;
        if (!player || player.readyState < 1) return;
        player.pause();
        if (
            !window ||
            !Number.isFinite(player.duration) ||
            window[0] >= player.duration ||
            window[1] > player.duration + 0.1
        ) {
            setError(new Error('来源时间区间超出原片可播放时长，无法准确定位。'));
            return;
        }
        try {
            setError(null);
            player.currentTime = window[0];
        } catch {
            setError(new Error('浏览器暂时无法定位到该时间点，请重新载入原片。'));
        }
    }, [start, end, seekSequence]);

    return (
        <div>
            <div className="video-preview-box">
                <video
                    ref={video}
                    controls
                    playsInline
                    preload="metadata"
                    aria-label="原片播放器"
                    src={source}
                    onLoadedMetadata={() => {
                        setLoaded(true);
                        locate();
                    }}
                    onSeeking={() => setSeeking(true)}
                    onSeeked={() => {
                        setSeeking(false);
                        if (video.current) setPosition(video.current.currentTime);
                    }}
                    onTimeUpdate={() => {
                        const p = video.current;
                        if (!p) return;
                        setPosition(p.currentTime);
                        if (clipOnly && end != null && p.currentTime >= end && !p.paused) p.pause();
                    }}
                    onPlay={() => {
                        const p = video.current;
                        if (
                            p &&
                            clipOnly &&
                            start != null &&
                            end != null &&
                            (p.currentTime < start || p.currentTime >= end)
                        )
                            p.currentTime = start;
                    }}
                    onError={() => {
                        setLoaded(false);
                        setError(
                            new Error(
                                '原片无法播放。文件可能暂不可用，或当前浏览器不支持它的编码。可重载原片或在视频库核查。',
                            ),
                        );
                    }}
                />
            </div>

            <div
                style={{
                    padding: '12px 16px',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    flexWrap: 'wrap',
                    gap: 12,
                    background: '#ffffff',
                    borderTop: '1px solid #f1f5f9',
                }}
            >
                <Space size={8}>
                    <Button
                        type="primary"
                        size="small"
                        icon={<CaretRightOutlined />}
                        disabled={!loaded || !target || !!error}
                        onClick={() => locate(true)}
                        style={{ background: '#10b981', borderColor: '#10b981' }}
                    >
                        播放此区间
                    </Button>
                    <Button
                        size="small"
                        icon={<UndoOutlined />}
                        disabled={!loaded || !target}
                        onClick={() => locate()}
                    >
                        回到起点
                    </Button>
                </Space>

                <Checkbox
                    checked={clipOnly}
                    onChange={(e) => setClipOnly(e.target.checked)}
                    style={{ fontSize: 12 }}
                >
                    区间结束时自动暂停
                </Checkbox>
            </div>

            <div
                style={{
                    padding: '10px 16px 14px',
                    borderTop: '1px solid #f1f5f9',
                    fontSize: 12,
                    background: '#f8fafc',
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    flexWrap: 'wrap',
                    gap: 8,
                }}
            >
                <div>
                    <Text strong style={{ fontSize: 12 }}>
                        {upload.filename}
                    </Text>
                    <span style={{ margin: '0 8px', color: '#cbd5e1' }}>|</span>
                    <span style={{ color: '#059669', fontFamily: 'monospace' }}>
                        目标范围 {formatTime(range?.start_ms)} ~ {formatTime(range?.end_ms)}
                    </span>
                </div>
                <div>
                    <Tag
                        color="default"
                        style={{ margin: 0, fontFamily: 'monospace', fontSize: 11 }}
                    >
                        {seeking
                            ? '正在寻帧…'
                            : position == null
                              ? '等待原片载入'
                              : `播放位置 ${formatTime(String(Math.round(position * 1000)))}`}
                    </Tag>
                </div>
            </div>

            {!target ? (
                <div style={{ padding: 12 }}>
                    <Notice>当前区间无法安全转换为浏览器播放时间，已停用定位。</Notice>
                </div>
            ) : null}

            {error ? (
                <div style={{ padding: 12 }}>
                    <ErrorNotice error={error} />
                    <Button
                        size="small"
                        icon={<ReloadOutlined />}
                        onClick={() => {
                            setError(null);
                            setLoaded(false);
                            video.current?.load();
                        }}
                    >
                        重新载入原片
                    </Button>
                </div>
            ) : null}
        </div>
    );
}
