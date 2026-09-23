import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
    covers,
    formatTime,
    payloadText,
    playableRange,
    searchParamsOnly,
    searchRequest,
    secondsToMs,
    validRevision,
} from '../.data/material-tests/features/material-utils.js';

test('filters preserve explicit zero and absent values separately', () => {
    const empty = searchRequest(new URLSearchParams());
    assert.equal(empty.start_ms, undefined);
    assert.equal(empty.min_confidence, undefined);
    const result = searchRequest(new URLSearchParams('start=0&end=1.001&confidence=0'));
    assert.equal(result.start_ms, '0');
    assert.equal(result.end_ms, '1001');
    assert.equal(result.min_confidence, 0);
});

test('time conversion is exact to milliseconds and refuses rounding or overflow', () => {
    assert.equal(secondsToMs('1.001'), '1001');
    assert.equal(secondsToMs('0001.2'), '1200');
    assert.equal(secondsToMs(''), undefined);
    for (const value of ['-1', '1.0001', 'NaN', 'Infinity', '1e3', '0x10', '9007199254741']) {
        assert.throws(() => secondsToMs(value));
    }
});

test('invalid filter URLs cannot issue broad fallback queries', () => {
    for (const value of [
        'start=2&end=2',
        'end=0',
        'start=-1',
        'confidence=NaN',
        'confidence=1.1',
        'confidence=-0.1',
        'limit=1000',
    ]) {
        assert.throws(() => searchRequest(new URLSearchParams(value)));
    }
});

test('modality and tag values are deduplicated and bounded', () => {
    const value = searchRequest(
        new URLSearchParams('tags=财务,季度，财务&modalities=asr_segment,ocr_blocks'),
    );
    assert.deepEqual(value.tags, ['财务', '季度']);
    assert.deepEqual(value.modalities, ['asr_segment', 'ocr_blocks']);
    assert.throws(() =>
        searchRequest(
            new URLSearchParams({
                tags: Array.from({ length: 33 }, (_, i) => String(i)).join(','),
            }),
        ),
    );
});

test('back link retains search but removes revision and observation', () => {
    const value = searchParamsOnly(
        new URLSearchParams(
            'q=预算&start=0&revision=2&observation=obs&redirect=https://example.invalid',
        ),
    );
    assert.equal(value.get('q'), '预算');
    assert.equal(value.get('start'), '0');
    assert.equal(value.has('revision'), false);
    assert.equal(value.has('observation'), false);
    assert.equal(value.has('redirect'), false);
});

test('payload text handles ASR segments and OCR arrays without dropping text', () => {
    assert.equal(payloadText({ blocks: [{ text: '标题' }, { text: '副标题' }] }), '标题\n副标题');
    assert.equal(payloadText({ text: '完整转写', segments: [{ text: '完整转写' }] }), '完整转写');
    assert.equal(payloadText({ caption: '画面描述', internal: '不要重复展示' }), '画面描述');
    assert.equal(payloadText({ text: '12345678' }, 4), '1234…');
});

test('missing times remain unknown, real long timelines retain exact display', () => {
    assert.equal(formatTime(undefined), '未知时间');
    assert.equal(formatTime('3601001'), '01:00:01.001');
    assert.equal(formatTime('0'), '00:00.000');
    assert.equal(formatTime('bad'), '未知时间');
});

test('only a fully covering source range can be selected for an observation', () => {
    const outer = { start_ms: '100', end_ms: '200' };
    assert.equal(covers(outer, { start_ms: '100', end_ms: '200' }), true);
    assert.equal(covers(outer, { start_ms: '200', end_ms: '201' }), false);
    assert.equal(covers(outer, { start_ms: '99', end_ms: '101' }), false);
    assert.equal(covers(outer, undefined), false);
    assert.equal(playableRange({ start_ms: '0', end_ms: '9007199254740992' }), null);
});

test('revision URLs only accept positive bounded integers', () => {
    assert.equal(validRevision(null), true);
    assert.equal(validRevision('2147483647'), true);
    for (const value of ['', '0', '-1', '1.1', '1e2', 'NaN', '2147483648'])
        assert.equal(validRevision(value), false);
});
