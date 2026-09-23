import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ArrowUpRight, RefreshCw, Search, SlidersHorizontal } from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import type { SearchRequest, SearchResponse } from '../api/contracts';
import { Empty, ErrorNotice, Heading, Loading } from '../components';
import {
    formatTime,
    modalityNames,
    payloadText,
    searchFields,
    searchParamsOnly,
    searchRequest,
    statusNames,
} from './material-utils';
import './materials.css';

export { default as MaterialDetail } from './MaterialDetail';

export default function Materials() {
    const [params, setParams] = useSearchParams();
    const [formError, setFormError] = useState<Error | null>(null);
    let request: SearchRequest | undefined, validationError: unknown;
    try {
        request = searchRequest(params);
    } catch (error) {
        validationError = error;
    }
    const result = useQuery({
        queryKey: ['materials', request],
        queryFn: ({ signal }) =>
            api<SearchResponse>('/v1/materials:search', {
                method: 'POST',
                body: JSON.stringify(request),
                signal,
            }),
        enabled: !!request,
        retry: false,
    });
    const values = result.data?.materials || [];
    return (
        <div className="materials-page">
            <Heading
                eyebrow="MATERIAL SEARCH"
                title="素材检索"
                description="从观测内容找到素材，沿着时间与版本回到真实来源。"
                action={
                    <button
                        disabled={result.isFetching || !request}
                        onClick={() => void result.refetch()}
                    >
                        <RefreshCw size={15} />
                        刷新结果
                    </button>
                }
            />
            <form
                key={params.toString()}
                className="card material-search"
                onSubmit={(event) => {
                    event.preventDefault();
                    const data = new FormData(event.currentTarget),
                        next = new URLSearchParams();
                    for (const key of searchFields) {
                        const value = String(data.get(key) || '').trim();
                        if (value) next.set(key, value);
                    }
                    try {
                        searchRequest(next);
                        setFormError(null);
                        setParams(next);
                        if (next.toString() === params.toString()) void result.refetch();
                    } catch (error) {
                        setFormError(error as Error);
                    }
                }}
            >
                <div className="material-search-line">
                    <Search size={21} />
                    <input
                        name="q"
                        aria-label="素材关键词"
                        placeholder="搜索转写、画面描述、画面文字…"
                        defaultValue={params.get('q') || ''}
                        maxLength={2000}
                    />
                    <button className="primary" disabled={result.isFetching}>
                        检索素材
                    </button>
                </div>
                <details
                    className="material-filters"
                    open={
                        searchFields.some(
                            (key) => !['q', 'limit'].includes(key) && params.has(key),
                        ) || undefined
                    }
                >
                    <summary>
                        <SlidersHorizontal size={15} />
                        筛选条件<span>来源流、时间、标签与置信度</span>
                    </summary>
                    <div className="material-filter-grid">
                        <label>
                            来源流 ID
                            <input
                                name="stream"
                                placeholder="全部来源流"
                                defaultValue={params.get('stream') || ''}
                                maxLength={256}
                            />
                        </label>
                        <label>
                            开始时间（秒）
                            <input
                                name="start"
                                inputMode="decimal"
                                placeholder="例如 0.000"
                                defaultValue={params.get('start') || ''}
                            />
                        </label>
                        <label>
                            结束时间（秒）
                            <input
                                name="end"
                                inputMode="decimal"
                                placeholder="例如 60.000"
                                defaultValue={params.get('end') || ''}
                            />
                        </label>
                        <label>
                            标签
                            <input
                                name="tags"
                                placeholder="多个标签用逗号分隔"
                                defaultValue={params.get('tags') || ''}
                                maxLength={2000}
                            />
                        </label>
                        <label>
                            观测模态
                            <input
                                name="modalities"
                                list="material-modalities"
                                placeholder="全部模态；可用逗号分隔"
                                defaultValue={params.get('modalities') || ''}
                                maxLength={2000}
                            />
                            <datalist id="material-modalities">
                                {Object.entries(modalityNames).map(([value, label]) => (
                                    <option key={value} value={value}>
                                        {label}
                                    </option>
                                ))}
                            </datalist>
                        </label>
                        <label>
                            最低置信度
                            <input
                                name="confidence"
                                type="number"
                                min="0"
                                max="1"
                                step="any"
                                placeholder="不限（含未知）"
                                defaultValue={params.get('confidence') || ''}
                            />
                        </label>
                    </div>
                    <p className="subtle">
                        时间按来源流偏移查询，与区间有交集即匹配。多个标签需同时满足；模态匹配任一项。设置最低置信度后，未知置信度不计为达标。
                    </p>
                </details>
                <div className="material-filter-footer">
                    <label>
                        最多显示
                        <select name="limit" defaultValue={params.get('limit') || '20'}>
                            <option value="20">20 条</option>
                            <option value="50">50 条</option>
                            <option value="100">100 条</option>
                        </select>
                    </label>
                    <button
                        type="button"
                        className="text-button"
                        onClick={() => {
                            setFormError(null);
                            setParams({});
                        }}
                    >
                        清除条件
                    </button>
                    <span>关键词 · 字面匹配</span>
                </div>
            </form>
            <ErrorNotice error={formError || validationError || result.error} />
            <div className="search-meta" role="status">
                <span>
                    {!validationError && !result.error && result.data
                        ? `找到 ${values.length} 条素材${values.length === request?.limit ? '（已达显示上限）' : ''}`
                        : '已入库素材'}
                    {result.isFetching ? ' · 正在更新…' : ''}
                </span>
                <span>按时间倒序 · 语义检索尚未接入</span>
            </div>
            {request && result.isPending ? (
                <Loading />
            ) : !validationError && !result.error && result.data ? (
                values.length ? (
                    <>
                        <div className="material-grid" aria-busy={result.isFetching}>
                            {values.map((item) => (
                                <Link
                                    className="card material-card"
                                    key={`${item.material_unit_id}:${item.revision}`}
                                    to={`/materials/${encodeURIComponent(item.material_unit_id)}?${searchParamsOnly(params)}`}
                                >
                                    <div className="row-between">
                                        <span className={`badge material-state-${item.status}`}>
                                            {statusNames[item.status] || item.status}
                                        </span>
                                        <ArrowUpRight size={18} />
                                    </div>
                                    <p>
                                        {item.observations
                                            .map((o) => payloadText(o.payload, 800))
                                            .filter(Boolean)
                                            .join(' ') || '暂无文字观测，可查看结构化数据与来源。'}
                                    </p>
                                    <div className="material-card-modality">
                                        {[...new Set(item.observations.map((o) => o.modality))].map(
                                            (m) => (
                                                <span key={m}>{modalityNames[m] || m}</span>
                                            ),
                                        )}
                                    </div>
                                    <div className="tags">
                                        {item.tags.map((tag) => (
                                            <span key={tag}>{tag}</span>
                                        ))}
                                    </div>
                                    <small className="mono material-stream" title={item.stream_id}>
                                        {item.stream_id}
                                    </small>
                                    <footer>
                                        {formatTime(item.time_range?.start_ms)} —{' '}
                                        {formatTime(item.time_range?.end_ms)}
                                        <span>版本 {item.revision}</span>
                                    </footer>
                                </Link>
                            ))}
                        </div>
                        {values.length === request?.limit ? (
                            <p className="subtle material-limit">
                                已显示前 {request.limit}{' '}
                                条。请缩小时间范围或增加筛选条件以查看更早的素材。
                            </p>
                        ) : null}
                    </>
                ) : (
                    <section className="card">
                        <Empty title={params.toString() ? '没有找到匹配的素材' : '素材库还是空的'}>
                            {params.toString()
                                ? '可调整时间范围、标签或关键词后重新检索。'
                                : '这里只展示已经入库的素材。上传视频不会自动生成观测结果。'}
                        </Empty>
                    </section>
                )
            ) : null}
        </div>
    );
}
