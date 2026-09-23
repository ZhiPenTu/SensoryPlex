import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, ChevronLeft, ChevronRight, Clock3, RefreshCw } from 'lucide-react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import type { MaterialUnit, Observation } from '../api/contracts';
import { ErrorNotice, Heading, Loading, Notice } from '../components';
import {
    formatTime,
    modalityNames,
    payloadText,
    searchParamsOnly,
    statusNames,
    validRevision,
} from './material-utils';
import MaterialPlayer from './MaterialPlayer';
import './materials.css';

export default function MaterialDetail() {
    const { id = '' } = useParams();
    const [params, setParams] = useSearchParams();
    const revision = params.get('revision'),
        valid = validRevision(revision);
    const latest = useQuery({
        queryKey: ['material', id, 'latest'],
        queryFn: ({ signal }) =>
            api<MaterialUnit>(`/v1/materials/${encodeURIComponent(id)}`, { signal }),
        retry: false,
    });
    const historical = useQuery({
        queryKey: ['material', id, revision],
        queryFn: ({ signal }) =>
            api<MaterialUnit>(`/v1/materials/${encodeURIComponent(id)}?revision=${revision}`, {
                signal,
            }),
        enabled: valid && revision !== null,
        retry: false,
    });
    const result = revision === null ? latest : historical;
    const item = valid ? result.data : undefined;
    function selectRevision(value: string) {
        const next = new URLSearchParams(params);
        if (value) next.set('revision', value);
        else next.delete('revision');
        next.delete('observation');
        setParams(next);
    }
    return (
        <div className="materials-page">
            <Link to={`/materials?${searchParamsOnly(params)}`} className="back-link">
                <ArrowLeft size={16} />
                返回查询结果
            </Link>
            <Heading
                eyebrow="MATERIAL DETAIL"
                title="素材详情"
                description="查看原始观测、定位来源片段，并追溯每一个历史版本。"
                action={
                    <button
                        disabled={result.isFetching || !valid}
                        onClick={() => {
                            void result.refetch();
                            if (revision) void latest.refetch();
                        }}
                    >
                        <RefreshCw size={15} />
                        刷新
                    </button>
                }
            />
            <section className="card material-revisions">
                <span>
                    <Clock3 size={17} />
                    {item
                        ? `版本 ${item.revision} · ${item.superseded ? '历史快照' : '当前版本'}`
                        : '版本导航'}
                </span>
                <div className="row-actions">
                    <button
                        aria-label="上一版本"
                        disabled={!item || item.revision <= 1}
                        onClick={() => item && selectRevision(String(item.revision - 1))}
                    >
                        <ChevronLeft size={15} />
                    </button>
                    <button
                        aria-label="下一版本"
                        disabled={!item || !latest.data || item.revision >= latest.data.revision}
                        onClick={() => item && selectRevision(String(item.revision + 1))}
                    >
                        <ChevronRight size={15} />
                    </button>
                    <form
                        key={`${id}:${revision}`}
                        onSubmit={(event) => {
                            event.preventDefault();
                            selectRevision(
                                String(new FormData(event.currentTarget).get('revision') || ''),
                            );
                        }}
                    >
                        <label>
                            指定版本
                            <input
                                name="revision"
                                aria-label="指定版本"
                                type="number"
                                min="1"
                                max="2147483647"
                                step="1"
                                placeholder="最新"
                                defaultValue={revision || ''}
                            />
                        </label>
                        <button>查看</button>
                    </form>
                    <button
                        className="text-button"
                        onClick={() => selectRevision('')}
                        disabled={revision === null}
                    >
                        最新{latest.data ? ` · ${latest.data.revision}` : ''}
                    </button>
                </div>
            </section>
            <ErrorNotice
                error={
                    !valid ? new Error('版本号必须为 1 到 2147483647 之间的整数。') : result.error
                }
            />
            {revision && latest.error ? (
                <Notice>
                    最新版本信息暂不可用，当前仅展示指定版本。版本导航不会猜测不存在的历史。
                </Notice>
            ) : null}
            {valid && result.isPending ? (
                <Loading />
            ) : item && !result.error ? (
                <DetailContent
                    key={`${item.material_unit_id}:${item.revision}`}
                    item={item}
                    selectedId={params.get('observation')}
                    select={(value) => {
                        const next = new URLSearchParams(params);
                        next.set('observation', value);
                        setParams(next, { replace: true });
                    }}
                />
            ) : null}
        </div>
    );
}

function DetailContent({
    item,
    selectedId,
    select,
}: {
    item: MaterialUnit;
    selectedId: string | null;
    select: (id: string) => void;
}) {
    const observations = [...item.observations].sort((a, b) => {
        const start = (o: Observation) =>
            /^\d+$/.test(o.time_range?.start_ms || '') ? BigInt(o.time_range!.start_ms) : 0n;
        return start(a) < start(b)
            ? -1
            : start(a) > start(b)
              ? 1
              : a.observation_id.localeCompare(b.observation_id);
    });
    const selected =
        observations.find((o) => o.observation_id === selectedId) ||
        (selectedId ? undefined : observations[0]);
    const [seekSequence, setSeekSequence] = useState(0);
    return (
        <>
            <section className="card material-overview">
                <div>
                    <span className={`badge material-state-${item.status}`}>
                        {statusNames[item.status] || item.status}
                    </span>
                    {item.superseded ? <span className="badge muted">只读历史版本</span> : null}
                    <h2 className="mono">{item.material_unit_id}</h2>
                </div>
                <dl>
                    <div>
                        <dt>来源流</dt>
                        <dd className="mono">{item.stream_id}</dd>
                    </div>
                    <div>
                        <dt>素材区间</dt>
                        <dd>
                            {formatTime(item.time_range?.start_ms)} —{' '}
                            {formatTime(item.time_range?.end_ms)}
                        </dd>
                    </div>
                    <div>
                        <dt>处理方案</dt>
                        <dd>{item.pipeline_version || '未知'}</dd>
                    </div>
                    <div>
                        <dt>生成时间</dt>
                        <dd>
                            {Number.isFinite(Number(item.created_at_unix_ms)) &&
                            Number(item.created_at_unix_ms) > 0
                                ? new Date(Number(item.created_at_unix_ms)).toLocaleString(
                                      'zh-CN',
                                      { hour12: false },
                                  )
                                : '未知'}
                        </dd>
                    </div>
                </dl>
                {item.tags.length ? (
                    <div className="tags">
                        {item.tags.map((tag) => (
                            <span key={tag}>{tag}</span>
                        ))}
                    </div>
                ) : null}
                {item.pending_enrichments.length ? (
                    <p className="material-pending">
                        待补全：
                        {item.pending_enrichments.map((m) => modalityNames[m] || m).join('、')}
                    </p>
                ) : null}
            </section>
            {selectedId && !selected ? (
                <Notice>当前版本中没有指定的观测。请选择下方时间轴中的观测。</Notice>
            ) : null}
            <div className="material-review-layout">
                <div className="material-viewer-column">
                    <MaterialPlayer
                        material={item}
                        observation={selected}
                        seekSequence={seekSequence}
                    />
                    <section className="card material-timeline">
                        <div className="section-heading">
                            <h2>观测时间轴</h2>
                            <span className="subtle">{observations.length} 条 · 按起点排序</span>
                        </div>
                        {observations.length ? (
                            <div className="material-observation-list">
                                {observations.map((obs) => (
                                    <button
                                        key={obs.observation_id}
                                        className={`material-observation-button ${obs.observation_id === selected?.observation_id ? 'selected' : ''}`}
                                        aria-pressed={
                                            obs.observation_id === selected?.observation_id
                                        }
                                        onClick={() => {
                                            select(obs.observation_id);
                                            setSeekSequence((n) => n + 1);
                                        }}
                                    >
                                        <span className="material-observation-time">
                                            {formatTime(obs.time_range?.start_ms)}
                                            <small>{formatTime(obs.time_range?.end_ms)}</small>
                                        </span>
                                        <span>
                                            <strong>
                                                {modalityNames[obs.modality] || obs.modality}
                                            </strong>
                                            <span className="material-observation-excerpt">
                                                {payloadText(obs.payload, 160) || '查看结构化观测'}
                                            </span>
                                        </span>
                                        <span
                                            className={`badge material-state-${obs.quality_state}`}
                                        >
                                            {statusNames[obs.quality_state] || obs.quality_state}
                                        </span>
                                    </button>
                                ))}
                            </div>
                        ) : (
                            <p className="material-padding subtle">当前版本没有观测数据。</p>
                        )}
                    </section>
                </div>
                <section className="card material-evidence" aria-label="观测详情">
                    {selected ? (
                        <ObservationDetail observation={selected} />
                    ) : (
                        <p className="subtle">选择一条观测查看内容与血缘。</p>
                    )}
                </section>
            </div>
            <section className="card material-sources">
                <div className="section-heading">
                    <h2>原始来源引用</h2>
                    <span className="subtle">{item.source_refs.length} 条</span>
                </div>
                {item.source_refs.length ? (
                    item.source_refs.map((ref, index) => (
                        <div
                            className="material-source-row"
                            key={`${ref.asset_id}:${ref.time_range?.start_ms}:${ref.time_range?.end_ms}:${index}`}
                        >
                            <strong className="mono">{ref.asset_id}</strong>
                            <span>
                                {formatTime(ref.time_range?.start_ms)} —{' '}
                                {formatTime(ref.time_range?.end_ms)}
                            </span>
                            <code>{ref.content_hash || '摘要未知'}</code>
                        </div>
                    ))
                ) : (
                    <p className="material-padding subtle">
                        当前版本没有原始来源引用，暂时无法回看。
                    </p>
                )}
            </section>
        </>
    );
}

function ObservationDetail({ observation: obs }: { observation: Observation }) {
    const p = obs.provenance;
    return (
        <>
            <span className="eyebrow">OBSERVATION</span>
            <h2>{modalityNames[obs.modality] || obs.modality}</h2>
            <div className="material-observation-body">
                {payloadText(obs.payload) || '该观测没有可展示的文字，请查看结构化数据。'}
            </div>
            <dl className="details">
                <dt>质量状态</dt>
                <dd>
                    <span className={`badge material-state-${obs.quality_state}`}>
                        {statusNames[obs.quality_state] || obs.quality_state}
                    </span>
                </dd>
                <dt>模型置信度</dt>
                <dd>
                    {obs.confidence == null ? '未知' : String(obs.confidence)}
                    {obs.confidence == null ? (
                        <small>{obs.confidence_unavailable_reason || '未提供原因'}</small>
                    ) : null}
                </dd>
                <dt>时间区间</dt>
                <dd>
                    {formatTime(obs.time_range?.start_ms)} — {formatTime(obs.time_range?.end_ms)}
                </dd>
                <dt>时序来源</dt>
                <dd>
                    {obs.timing_source || '未知'}
                    <small>
                        时序置信度：
                        {obs.timing_confidence == null ? '未知' : String(obs.timing_confidence)}
                    </small>
                </dd>
                {obs.quality_reasons.length ? (
                    <>
                        <dt>质量说明</dt>
                        <dd>{obs.quality_reasons.join('、')}</dd>
                    </>
                ) : null}
            </dl>
            <details className="material-lineage" open>
                <summary>模型与来源血缘</summary>
                <dl className="details">
                    <dt>观测 ID</dt>
                    <dd className="mono">{obs.observation_id}</dd>
                    <dt>来源条目</dt>
                    <dd className="mono">{obs.source_item_id}</dd>
                    <dt>来源 ID</dt>
                    <dd className="mono">{obs.source_id}</dd>
                    <dt>输入内容摘要</dt>
                    <dd className="mono">{obs.content_hash}</dd>
                    <dt>模型</dt>
                    <dd>
                        {p?.model_id || '未知'} · {p?.model_version || '未知'}
                    </dd>
                    <dt>模型发布 ID</dt>
                    <dd className="mono">{p?.model_release_id || '未知'}</dd>
                    <dt>执行后端</dt>
                    <dd>{p?.execution_backend || '未知'}</dd>
                    <dt>插件</dt>
                    <dd>
                        {p?.plugin || '未知'} · {p?.plugin_version || '未知'}
                    </dd>
                    <dt>插件摘要</dt>
                    <dd className="mono">{p?.artifact_digest || '未知'}</dd>
                    <dt>模型摘要</dt>
                    <dd className="mono">{p?.model_artifact_digest || '未知'}</dd>
                    <dt>配置摘要</dt>
                    <dd className="mono">{p?.config_hash || '未知'}</dd>
                </dl>
            </details>
            <details className="material-lineage">
                <summary>结构化观测数据</summary>
                <pre>{JSON.stringify(obs.payload || {}, null, 2)}</pre>
            </details>
        </>
    );
}
