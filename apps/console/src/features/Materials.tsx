import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowLeft, ArrowUpRight, Search } from 'lucide-react';
import { Link, useParams } from 'react-router-dom';
import { api } from '../api/client';
import type { JsonObject, MaterialUnit, SearchResponse } from '../api/contracts';
import { Badge, Empty, ErrorNotice, Heading, Loading, time } from '../components';

function text(payload?: JsonObject): string {
    if (!payload) return '';
    return Object.values(payload)
        .flatMap((v) =>
            typeof v === 'string'
                ? [v]
                : v && typeof v === 'object' && !Array.isArray(v)
                  ? [text(v)]
                  : [],
        )
        .join(' ');
}

export default function Materials() {
    const [query, setQuery] = useState('');
    const [filters, setFilters] = useState({
        query: '',
        tags: [] as string[],
        mode: 'keyword',
        limit: 50,
    });
    const result = useQuery({
        queryKey: ['materials', filters],
        queryFn: ({ signal }) =>
            api<SearchResponse>('/v1/materials:search', {
                method: 'POST',
                body: JSON.stringify(filters),
                signal,
            }),
    });
    return (
        <>
            <Heading
                eyebrow="MATERIAL SEARCH"
                title="素材检索"
                description="查找已经入库的真实素材，沿着时间锚点回到它的来源。"
            />
            <form
                className="search-bar card"
                onSubmit={(e) => {
                    e.preventDefault();
                    setFilters({ ...filters, query });
                }}
            >
                <Search size={21} />
                <input
                    aria-label="素材关键词"
                    placeholder="输入画面描述、字幕或其他关键词…"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    maxLength={2000}
                />
                <button className="primary">检索素材</button>
            </form>
            <div className="search-meta">
                <span>关键词检索 · 字面匹配</span>
                <span>最多展示 50 条结果 · 语义检索待接入</span>
            </div>
            <ErrorNotice error={result.error} />
            {result.isPending ? (
                <Loading />
            ) : result.data?.materials.length ? (
                <div className="material-grid">
                    {result.data.materials.map((item) => (
                        <Link
                            className="card material-card"
                            key={`${item.material_unit_id}:${item.revision}`}
                            to={`/materials/${item.material_unit_id}`}
                        >
                            <div className="row-between">
                                <Badge state={item.status} />
                                <ArrowUpRight size={18} />
                            </div>
                            <p>{item.observations.map((o) => text(o.payload)).join(' ')}</p>
                            <div className="tags">
                                {item.tags.map((tag) => (
                                    <span key={tag}>{tag}</span>
                                ))}
                            </div>
                            <footer>
                                {time(item.time_range?.start_ms || '0')} —{' '}
                                {time(item.time_range?.end_ms || '0')}
                                <span>revision {item.revision}</span>
                            </footer>
                        </Link>
                    ))}
                </div>
            ) : !result.error ? (
                <section className="card">
                    <Empty title={filters.query ? '没有找到匹配的素材' : '素材库还是空的'}>
                        {filters.query
                            ? '试试其他关键词，或检查素材是否已入库。'
                            : '这里只展示已持久化的真实结果。上传视频不会自动生成素材。'}
                    </Empty>
                </section>
            ) : null}
        </>
    );
}

export function MaterialDetail() {
    const { id } = useParams();
    const [revision, setRevision] = useState('');
    const result = useQuery({
        queryKey: ['material', id, revision],
        queryFn: ({ signal }) =>
            api<MaterialUnit>(
                `/v1/materials/${encodeURIComponent(id || '')}${revision ? `?revision=${revision}` : ''}`,
                { signal },
            ),
    });
    const item = result.data;
    return (
        <>
            <Link to="/materials" className="back-link">
                <ArrowLeft size={16} />
                返回素材检索
            </Link>
            <Heading
                eyebrow="MATERIAL DETAIL"
                title="素材详情"
                description="原始观测、时间区间与模型血缘保持可追溯。"
                action={
                    <label className="revision">
                        历史版本
                        <input
                            aria-label="历史版本"
                            type="number"
                            min={1}
                            placeholder="最新"
                            value={revision}
                            onChange={(e) => setRevision(e.target.value)}
                        />
                    </label>
                }
            />
            <ErrorNotice error={result.error} />
            {result.isPending ? (
                <Loading />
            ) : item ? (
                <>
                    <section className="card detail-summary">
                        <Badge state={item.status} />
                        <h2 className="mono">{item.material_unit_id}</h2>
                        <p>
                            revision {item.revision} · {item.superseded ? '历史版本' : '当前版本'} ·
                            方案 {item.pipeline_version}
                        </p>
                    </section>
                    <div className="observations">
                        {item.observations.map((obs) => (
                            <article className="card observation" key={obs.observation_id}>
                                <span className="time-mark">
                                    {time(obs.time_range?.start_ms || '0')} –{' '}
                                    {time(obs.time_range?.end_ms || '0')}
                                </span>
                                <h3>{obs.modality}</h3>
                                <p>{text(obs.payload)}</p>
                                <dl className="details">
                                    <dt>置信度</dt>
                                    <dd>
                                        {obs.confidence == null
                                            ? `未知 · ${obs.confidence_unavailable_reason}`
                                            : obs.confidence}
                                    </dd>
                                    <dt>模型</dt>
                                    <dd>
                                        {obs.provenance?.model_id} · {obs.provenance?.model_version}
                                    </dd>
                                    <dt>插件</dt>
                                    <dd>
                                        {obs.provenance?.plugin} · {obs.provenance?.plugin_version}
                                    </dd>
                                    <dt>模型摘要</dt>
                                    <dd className="mono">
                                        {obs.provenance?.model_artifact_digest}
                                    </dd>
                                    <dt>配置摘要</dt>
                                    <dd className="mono">{obs.provenance?.config_hash}</dd>
                                </dl>
                            </article>
                        ))}
                    </div>
                    <section className="card detail-summary">
                        <h2>原始来源</h2>
                        {item.source_refs.map((ref) => (
                            <p key={ref.asset_id} className="mono">
                                {ref.asset_id} · {time(ref.time_range?.start_ms || '0')} –{' '}
                                {time(ref.time_range?.end_ms || '0')}
                            </p>
                        ))}
                        <p className="subtle">
                            底座素材与上传文件的来源映射尚未接入。不会用无关视频代替原片。
                        </p>
                    </section>
                </>
            ) : null}
        </>
    );
}
