import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button, Card, Col, Input, Row, Space, Tag, Typography } from 'antd';
import {
    SearchOutlined,
    ReloadOutlined,
    FilterOutlined,
    ArrowRightOutlined,
    ClockCircleOutlined,
    ClearOutlined,
    DatabaseOutlined,
} from '@ant-design/icons';
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

const { Text } = Typography;

export default function Materials() {
    const [params, setParams] = useSearchParams();
    const [formError, setFormError] = useState<Error | null>(null);
    const [advancedOpen, setAdvancedOpen] = useState(
        searchFields.some((key) => !['q', 'limit'].includes(key) && params.has(key)),
    );

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

    const handleSearchSubmit = (e: React.FormEvent<HTMLFormElement>) => {
        e.preventDefault();
        const data = new FormData(e.currentTarget);
        const next = new URLSearchParams();
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
    };

    const statusTagColor: Record<string, string> = {
        fast_ready: 'blue',
        partial: 'warning',
        enriched: 'success',
        failed: 'error',
        rejected: 'error',
    };

    return (
        <div className="materials-page">
            <Heading
                eyebrow="Material Retrieval"
                title="素材检索"
                description="基于时间轴对齐的多模态观测数据，沿毫秒时序与确定性版本回溯真实母带来源。"
                action={
                    <Button
                        icon={<ReloadOutlined />}
                        disabled={result.isFetching || !request}
                        onClick={() => void result.refetch()}
                    >
                        刷新结果
                    </Button>
                }
            />

            <form key={params.toString()} onSubmit={handleSearchSubmit}>
                <Card style={{ marginBottom: 20 }} bodyStyle={{ padding: 18 }}>
                    <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
                        <Input
                            prefix={<SearchOutlined style={{ color: '#1668dc', fontSize: 16 }} />}
                            name="q"
                            aria-label="素材关键词"
                            placeholder="输入关键词检索转写文本、画面文字 (OCR)、视觉描述 (Caption)…"
                            defaultValue={params.get('q') || ''}
                            maxLength={2000}
                            size="large"
                            allowClear
                            style={{ flex: 1 }}
                        />
                        <Button
                            type="primary"
                            htmlType="submit"
                            size="large"
                            loading={result.isFetching}
                            style={{ minWidth: 100 }}
                        >
                            检索素材
                        </Button>
                        <Button
                            size="large"
                            icon={<FilterOutlined />}
                            type={advancedOpen ? 'dashed' : 'default'}
                            onClick={() => setAdvancedOpen(!advancedOpen)}
                        >
                            高级筛选
                        </Button>
                    </div>

                    {advancedOpen ? (
                        <div
                            style={{
                                marginTop: 16,
                                paddingTop: 16,
                                borderTop: '1px solid #f1f5f9',
                            }}
                        >
                            <Row gutter={[16, 12]}>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            来源流 ID (Stream ID)
                                        </Text>
                                        <Input
                                            name="stream"
                                            placeholder="全部来源流"
                                            defaultValue={params.get('stream') || ''}
                                            maxLength={256}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            开始时间 (秒，例如 0.000)
                                        </Text>
                                        <Input
                                            name="start"
                                            inputMode="decimal"
                                            placeholder="例如 0.000"
                                            defaultValue={params.get('start') || ''}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            结束时间 (秒)
                                        </Text>
                                        <Input
                                            name="end"
                                            inputMode="decimal"
                                            placeholder="例如 12.500"
                                            defaultValue={params.get('end') || ''}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            最低置信度 (0.00 ~ 1.00)
                                        </Text>
                                        <Input
                                            name="confidence"
                                            inputMode="decimal"
                                            placeholder="例如 0.70"
                                            defaultValue={params.get('confidence') || ''}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            观测模态 (逗号分隔)
                                        </Text>
                                        <Input
                                            name="modalities"
                                            placeholder="例如 asr_segment,ocr_blocks"
                                            defaultValue={params.get('modalities') || ''}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                                <Col xs={24} sm={8}>
                                    <div>
                                        <Text type="secondary" style={{ fontSize: 12 }}>
                                            包含标签 (逗号分隔)
                                        </Text>
                                        <Input
                                            name="tags"
                                            placeholder="例如 会议,财务"
                                            defaultValue={params.get('tags') || ''}
                                            style={{ marginTop: 4 }}
                                        />
                                    </div>
                                </Col>
                            </Row>

                            <div
                                style={{
                                    display: 'flex',
                                    justifyContent: 'space-between',
                                    alignItems: 'center',
                                    marginTop: 16,
                                    paddingTop: 12,
                                    borderTop: '1px solid #f1f5f9',
                                }}
                            >
                                <Space size={16} align="center">
                                    <Text type="secondary" style={{ fontSize: 12 }}>
                                        显示上限:
                                    </Text>
                                    <select
                                        name="limit"
                                        defaultValue={params.get('limit') || '20'}
                                        style={{
                                            padding: '4px 8px',
                                            borderRadius: 6,
                                            border: '1px solid #d9d9d9',
                                            fontSize: 12,
                                        }}
                                    >
                                        <option value="20">20 条</option>
                                        <option value="50">50 条</option>
                                        <option value="100">100 条</option>
                                    </select>

                                    <Button
                                        type="link"
                                        size="small"
                                        icon={<ClearOutlined />}
                                        onClick={() => {
                                            setFormError(null);
                                            setParams({});
                                        }}
                                    >
                                        重置清空条件
                                    </Button>
                                </Space>

                                <Text type="secondary" style={{ fontSize: 12 }}>
                                    按时间轴精确倒序匹配
                                </Text>
                            </div>
                        </div>
                    ) : null}
                </Card>
            </form>

            <ErrorNotice error={formError || validationError || result.error} />

            <div
                style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    marginBottom: 16,
                    padding: '0 4px',
                }}
            >
                <Space size={8}>
                    <DatabaseOutlined style={{ color: '#1668dc' }} />
                    <Text strong style={{ fontSize: 13 }}>
                        {!validationError && !result.error && result.data
                            ? `检索到 ${values.length} 条素材单元${values.length === request?.limit ? '（已达单页显示上限）' : ''}`
                            : '已入库素材库'}
                    </Text>
                    {result.isFetching ? (
                        <Text type="secondary" style={{ fontSize: 12 }}>
                            · 正在实时索引…
                        </Text>
                    ) : null}
                </Space>
                <Text type="secondary" style={{ fontSize: 12 }}>
                    时间轴对齐 · 毫秒级防漂移
                </Text>
            </div>

            {request && result.isPending ? (
                <Loading tip="正在检索素材单元…" />
            ) : !validationError && !result.error && result.data ? (
                values.length ? (
                    <>
                        <Row gutter={[16, 16]}>
                            {values.map((item) => (
                                <Col
                                    xs={24}
                                    md={12}
                                    lg={8}
                                    key={`${item.material_unit_id}:${item.revision}`}
                                >
                                    <Link
                                        to={`/materials/${encodeURIComponent(item.material_unit_id)}?${searchParamsOnly(params)}`}
                                        style={{ display: 'block', height: '100%' }}
                                    >
                                        <Card
                                            hoverable
                                            style={{
                                                height: '100%',
                                                display: 'flex',
                                                flexDirection: 'column',
                                                borderRadius: 10,
                                                borderColor: '#e2e8f0',
                                            }}
                                            bodyStyle={{
                                                padding: 16,
                                                flex: 1,
                                                display: 'flex',
                                                flexDirection: 'column',
                                            }}
                                        >
                                            <div
                                                style={{
                                                    display: 'flex',
                                                    justifyContent: 'space-between',
                                                    alignItems: 'center',
                                                    marginBottom: 10,
                                                }}
                                            >
                                                <Tag
                                                    color={statusTagColor[item.status] || 'default'}
                                                    style={{ margin: 0, fontWeight: 500 }}
                                                >
                                                    {statusNames[item.status] || item.status}
                                                </Tag>
                                                <ArrowRightOutlined
                                                    style={{ color: '#94a3b8', fontSize: 12 }}
                                                />
                                            </div>

                                            <div
                                                style={{
                                                    fontSize: 13,
                                                    lineHeight: 1.6,
                                                    color: '#1e293b',
                                                    marginBottom: 12,
                                                    flex: 1,
                                                    display: '-webkit-box',
                                                    WebkitLineClamp: 3,
                                                    WebkitBoxOrient: 'vertical',
                                                    overflow: 'hidden',
                                                }}
                                            >
                                                {item.observations
                                                    .map((o) => payloadText(o.payload, 400))
                                                    .filter(Boolean)
                                                    .join(' ') ||
                                                    '暂无文本摘要，可点击查看多模态向量特征与原始时间轴。'}
                                            </div>

                                            <div style={{ marginBottom: 10 }}>
                                                <Space size={[4, 4]} wrap>
                                                    {[
                                                        ...new Set(
                                                            item.observations.map(
                                                                (o) => o.modality,
                                                            ),
                                                        ),
                                                    ].map((m) => (
                                                        <Tag
                                                            key={m}
                                                            color="cyan"
                                                            style={{
                                                                fontSize: 11,
                                                                margin: 0,
                                                                borderRadius: 4,
                                                            }}
                                                        >
                                                            {modalityNames[m] || m}
                                                        </Tag>
                                                    ))}
                                                </Space>
                                            </div>

                                            {item.tags.length ? (
                                                <div style={{ marginBottom: 10 }}>
                                                    <Space size={[4, 4]} wrap>
                                                        {item.tags.map((tag) => (
                                                            <Tag
                                                                key={tag}
                                                                style={{
                                                                    fontSize: 11,
                                                                    margin: 0,
                                                                    background: '#f8fafc',
                                                                }}
                                                            >
                                                                #{tag}
                                                            </Tag>
                                                        ))}
                                                    </Space>
                                                </div>
                                            ) : null}

                                            <div
                                                style={{
                                                    marginTop: 'auto',
                                                    paddingTop: 10,
                                                    borderTop: '1px solid #f1f5f9',
                                                    display: 'flex',
                                                    justifyContent: 'space-between',
                                                    alignItems: 'center',
                                                    fontSize: 12,
                                                }}
                                            >
                                                <Space size={4} style={{ color: '#059669' }}>
                                                    <ClockCircleOutlined style={{ fontSize: 12 }} />
                                                    <span className="mono" style={{ fontSize: 11 }}>
                                                        {formatTime(item.time_range?.start_ms)} ~{' '}
                                                        {formatTime(item.time_range?.end_ms)}
                                                    </span>
                                                </Space>
                                                <Tag
                                                    color="purple"
                                                    style={{ margin: 0, fontSize: 11 }}
                                                >
                                                    v{item.revision}
                                                </Tag>
                                            </div>
                                        </Card>
                                    </Link>
                                </Col>
                            ))}
                        </Row>

                        {values.length === request?.limit ? (
                            <div
                                style={{
                                    textAlign: 'center',
                                    padding: '24px 0',
                                    color: '#94a3b8',
                                    fontSize: 12,
                                }}
                            >
                                已显示前 {request.limit}{' '}
                                条记录。建议缩小时间范围或增加关键词以查看更早的历史素材。
                            </div>
                        ) : null}
                    </>
                ) : (
                    <Card>
                        <Empty
                            title={params.toString() ? '未找到符合条件的素材' : '素材库暂无数据'}
                        >
                            {params.toString()
                                ? '可尝试放宽时间范围、降低置信度阈值或更换关键词。'
                                : '这里展示已完成推理与切片的素材单元。请在处理任务中下发视频流水线。'}
                        </Empty>
                    </Card>
                )
            ) : null}
        </div>
    );
}
