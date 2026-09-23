import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Play, RotateCcw, VideoOff } from 'lucide-react';
import { api } from '../api/client';
import type { MaterialUnit, Observation, TimeRange, Upload } from '../api/contracts';
import { ErrorNotice, Loading, Notice } from '../components';
import { usePermission } from '../session';
import { covers, formatTime, playableRange } from './material-utils';

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
        <section className="card material-player" aria-label="原片回看">
            <div className="section-heading">
                <h2>原片回看</h2>
                <span className="subtle">
                    {explicit ? '来源区间' : observation ? '当前观测区间' : '来源区间'}
                </span>
            </div>
            {material.source_refs.length ? (
                <label className="material-source-select">
                    选择来源
                    <select
                        aria-label="回看来源"
                        value={index}
                        onChange={(event) =>
                            setChoice({ key: selectionKey, index: Number(event.target.value) })
                        }
                    >
                        <option value={-1} disabled>
                            请选择原片来源
                        </option>
                        {material.source_refs.map((ref, i) => (
                            <option key={`${ref.asset_id}:${i}`} value={i}>
                                {ref.asset_id} · {formatTime(ref.time_range?.start_ms)} —{' '}
                                {formatTime(ref.time_range?.end_ms)}
                            </option>
                        ))}
                    </select>
                </label>
            ) : null}
            {!canRead ? (
                <div className="material-padding">
                    <Notice>当前账户没有原片读取权限，可继续查看素材观测。</Notice>
                </div>
            ) : !source ? (
                <div className="material-player-empty">
                    <VideoOff size={32} />
                    <strong>{observation ? '没有覆盖该观测的来源区间' : '暂无原片来源'}</strong>
                    <p>已登记的其它来源可在上方单独选择回看。</p>
                </div>
            ) : result.isPending ? (
                <Loading />
            ) : result.error ? (
                <div className="material-padding">
                    <ErrorNotice error={result.error} />
                    <button onClick={() => void result.refetch()} disabled={result.isFetching}>
                        重新检查原片
                    </button>
                </div>
            ) : result.data ? (
                <VideoReview
                    key={result.data.id}
                    upload={result.data}
                    range={range}
                    seekSequence={seekSequence}
                />
            ) : null}
        </section>
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
    const start = target?.[0],
        end = target?.[1];
    const currentTarget = useRef(target);
    currentTarget.current = target;
    const source = `/v1/assets/${encodeURIComponent(upload.id)}/content`;

    function locate(play = false) {
        const player = video.current,
            window = currentTarget.current;
        if (!player || player.readyState < 1) return;
        if (
            !window ||
            !Number.isFinite(player.duration) ||
            window[0] >= player.duration ||
            window[1] > player.duration + 0.1
        ) {
            player.pause();
            setError(new Error('来源时间区间超出原片可播放时长，无法准确定位。'));
            return;
        }
        try {
            player.pause();
            setError(null);
            player.currentTime = window[0];
            if (play)
                void player
                    .play()
                    .catch(() => setError(new Error('播放未开始，请通过播放器重试。')));
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
        <>
            <video
                ref={video}
                className="material-video"
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
            <div className="material-player-controls">
                <div className="row-actions">
                    <button
                        className="primary"
                        disabled={!loaded || !target || !!error}
                        onClick={() => locate(true)}
                    >
                        <Play size={15} />
                        播放此区间
                    </button>
                    <button disabled={!loaded || !target} onClick={() => locate()}>
                        <RotateCcw size={15} />
                        回到起点
                    </button>
                </div>
                <label>
                    <input
                        type="checkbox"
                        checked={clipOnly}
                        onChange={(e) => setClipOnly(e.target.checked)}
                    />
                    区间结束时暂停
                </label>
            </div>
            <div className="material-playback-info">
                <strong>{upload.filename}</strong>
                <span>
                    选中 {formatTime(range?.start_ms)} — {formatTime(range?.end_ms)}
                </span>
                <span role="status">
                    {seeking
                        ? '正在定位…'
                        : position == null
                          ? '等待原片载入'
                          : `播放位置 ${formatTime(String(Math.round(position * 1000)))}`}
                </span>
            </div>
            {!target ? (
                <div className="material-padding">
                    <Notice>当前区间无法安全转换为浏览器播放时间，已停用定位。</Notice>
                </div>
            ) : null}
            {error ? (
                <div className="material-padding">
                    <ErrorNotice error={error} />
                    <button
                        onClick={() => {
                            setError(null);
                            setLoaded(false);
                            video.current?.load();
                        }}
                    >
                        重新载入原片
                    </button>
                </div>
            ) : null}
        </>
    );
}
