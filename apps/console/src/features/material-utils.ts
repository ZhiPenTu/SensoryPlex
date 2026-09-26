import type { JsonObject, SearchRequest, TimeRange } from '../api/contracts';

export const searchFields = [
    'q',
    'stream',
    'start',
    'end',
    'tags',
    'modalities',
    'execution',
    'confidence',
    'limit',
] as const;
export const modalityNames: Record<string, string> = {
    asr_segment: '语音转写',
    'vision.scene_description': '画面描述',
    ocr_blocks: '画面文字',
};
export const statusNames: Record<string, string> = {
    partial: '部分结果',
    fast_ready: '基础素材',
    enriched: '已补全',
    conflict: '存在冲突',
    low_confidence: '低置信度',
    failed: '处理失败',
    final: '最终观测',
    accepted: '已接受',
    rejected: '已拒绝',
};

export function secondsToMs(value: string): string | undefined {
    if (!value.trim()) return undefined;
    if (!/^\d+(?:\.\d{1,3})?$/.test(value))
        throw new Error('时间请输入非负秒数，最多保留三位小数。');
    const [whole, fraction = ''] = value.split('.');
    const ms = BigInt(whole) * 1000n + BigInt(fraction.padEnd(3, '0'));
    if (ms > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error('时间超出浏览器可精确处理的范围。');
    return ms.toString();
}

export function formatTime(value?: string): string {
    if (value == null || !/^\d+$/.test(value)) return '未知时间';
    const ms = BigInt(value),
        seconds = ms / 1000n;
    const hours = seconds / 3600n;
    return `${hours ? `${hours.toString().padStart(2, '0')}:` : ''}${((seconds / 60n) % 60n).toString().padStart(2, '0')}:${(seconds % 60n).toString().padStart(2, '0')}.${(ms % 1000n).toString().padStart(3, '0')}`;
}

export function playableRange(range?: TimeRange): [number, number] | null {
    if (!range || !/^\d+$/.test(range.start_ms) || !/^\d+$/.test(range.end_ms)) return null;
    const start = Number(range.start_ms),
        end = Number(range.end_ms);
    return Number.isSafeInteger(start) && Number.isSafeInteger(end) && end > start
        ? [start / 1000, end / 1000]
        : null;
}

export function covers(outer?: TimeRange, inner?: TimeRange): boolean {
    const a = playableRange(outer),
        b = playableRange(inner);
    return !!a && !!b && a[0] <= b[0] && a[1] >= b[1];
}

export function payloadText(payload?: JsonObject, maximum = 4000): string {
    const parts: string[] = [];
    let count = 0,
        length = 0;
    function visit(value: unknown, depth: number) {
        if (depth > 12 || count++ > 500 || length >= maximum) return;
        if (typeof value === 'string') {
            parts.push(value.slice(0, maximum - length));
            length += value.length;
        } else if (Array.isArray(value)) {
            for (const child of value) visit(child, depth + 1);
        } else if (value && typeof value === 'object') {
            const object = value as Record<string, unknown>;
            const main = ['text', 'caption', 'description', 'transcript'].find(
                (key) => typeof object[key] === 'string' && object[key],
            );
            if (main) visit(object[main], depth + 1);
            else for (const child of Object.values(object)) visit(child, depth + 1);
        }
    }
    visit(payload, 0);
    return parts.join('\n') + (length >= maximum || count > 500 ? '…' : '');
}

function list(value: string | null): string[] {
    const result = [
        ...new Set(
            (value || '')
                .split(/[,，]/)
                .map((s) => s.trim())
                .filter(Boolean),
        ),
    ];
    if (result.length > 32 || result.some((s) => s.length > 256))
        throw new Error('标签或模态最多 32 项，每项最多 256 个字符。');
    return result;
}

export function searchRequest(params: URLSearchParams): SearchRequest {
    const start = secondsToMs(params.get('start') || ''),
        end = secondsToMs(params.get('end') || '');
    if (end != null && BigInt(end) <= BigInt(start || '0'))
        throw new Error('结束时间需要晚于开始时间。');
    const confidence = params.get('confidence') || '';
    if (
        confidence &&
        (!/^\d*(?:\.\d+)?$/.test(confidence) ||
            !Number.isFinite(Number(confidence)) ||
            Number(confidence) < 0 ||
            Number(confidence) > 1)
    )
        throw new Error('最低置信度需要在 0 到 1 之间。');
    const limit = params.get('limit') || '20';
    if (!['20', '50', '100'].includes(limit)) throw new Error('显示数量请选择 20、50 或 100。');
    const query = params.get('q') || '',
        stream = params.get('stream') || '',
        execution = params.get('execution') || '';
    if (query.length > 2000 || stream.length > 256 || execution.length > 128)
        throw new Error('查询条件过长。');
    return {
        query,
        stream_id: stream,
        ...(start == null ? {} : { start_ms: start }),
        ...(end == null ? {} : { end_ms: end }),
        modalities: list(params.get('modalities')),
        tags: list(params.get('tags')),
        ...(confidence ? { min_confidence: Number(confidence) } : {}),
        limit: Number(limit),
        mode: 'keyword',
        execution_id: execution,
    };
}

export function searchParamsOnly(params: URLSearchParams): URLSearchParams {
    const result = new URLSearchParams();
    for (const key of searchFields) {
        const value = params.get(key);
        if (value) result.set(key, value);
    }
    return result;
}

export function validRevision(value: string | null): boolean {
    return value === null || (/^[1-9]\d*$/.test(value) && Number(value) <= 2147483647);
}
