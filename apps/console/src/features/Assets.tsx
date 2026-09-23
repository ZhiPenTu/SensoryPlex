import { useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowUpRight, FileVideo, Plus, UploadCloud } from 'lucide-react';
import { Link } from 'react-router-dom';
import { api, post, uploadFile } from '../api/client';
import type { Upload, UploadList } from '../api/contracts';
import {
    Badge,
    bytes,
    date,
    Empty,
    ErrorNotice,
    Heading,
    Loading,
    Modal,
    Notice,
    Pager,
} from '../components';
import { usePermission } from '../session';

export default function Assets() {
    const cache = useQueryClient();
    const [offset, setOffset] = useState(0);
    const [selected, setSelected] = useState<Upload | null>(null);
    const [progress, setProgress] = useState(0);
    const input = useRef<HTMLInputElement>(null);
    const canUpload = usePermission('assets:write');
    const listing = useQuery({
        queryKey: ['assets', offset],
        queryFn: ({ signal }) =>
            api<UploadList>(`/v1/assets?limit=20&offset=${offset}`, { signal }),
    });
    const upload = useMutation({
        mutationFn: async (file: File) => {
            const type =
                file.type ||
                (file.name.toLowerCase().endsWith('.webm') ? 'video/webm' : 'video/mp4');
            if (file.size > 1024 ** 3) throw new Error('单个文件最多 1 GiB。');
            setProgress(0);
            const item = await post<Upload>('/v1/uploads', {
                filename: file.name,
                size_bytes: String(file.size),
                content_type: type,
            });
            await uploadFile(item.id, file, setProgress);
        },
        onSettled: () => {
            void cache.invalidateQueries({ queryKey: ['assets'] });
        },
    });
    const cancel = useMutation({
        mutationFn: (id: string) => api(`/v1/uploads/${id}`, { method: 'DELETE' }),
        onSuccess: () => cache.invalidateQueries({ queryKey: ['assets'] }),
    });
    return (
        <>
            <Heading
                eyebrow="MEDIA LIBRARY"
                title="视频库"
                description="保存原始视频，为每一份素材保留可以回溯的来源。"
                action={
                    canUpload ? (
                        <button
                            className="primary"
                            disabled={upload.isPending}
                            onClick={() => input.current?.click()}
                        >
                            <Plus size={17} />
                            导入视频
                        </button>
                    ) : null
                }
            />
            <input
                className="sr-only"
                aria-label="选择视频文件"
                ref={input}
                type="file"
                accept="video/mp4,video/webm"
                onChange={(e) => {
                    const f = e.target.files?.[0];
                    if (f) upload.mutate(f);
                    e.target.value = '';
                }}
            />
            <ErrorNotice error={listing.error || upload.error || cancel.error} />
            <Notice>
                文件可上传和预览；媒体准入与自动处理尚未接入，上传完成后会保留为“待媒体准入”。
            </Notice>
            {upload.isPending ? (
                <div className="card upload-progress">
                    <UploadCloud size={22} />
                    <div>
                        <strong>正在上传 · {progress}%</strong>
                        <progress max={100} value={progress} />
                        <small>请保持当前页面打开</small>
                    </div>
                </div>
            ) : null}
            <section className="card">
                <div className="section-heading">
                    <h2>全部视频</h2>
                    <span className="subtle">MP4 / WebM · 最大 1 GiB</span>
                </div>
                {listing.isPending ? (
                    <Loading />
                ) : listing.data?.items.length ? (
                    <div className="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>视频名称</th>
                                    <th>大小</th>
                                    <th>状态</th>
                                    <th>导入时间</th>
                                    <th />
                                </tr>
                            </thead>
                            <tbody>
                                {listing.data.items.map((item) => (
                                    <tr key={item.id}>
                                        <td>
                                            <div className="file-name">
                                                <span className="file-icon">
                                                    <FileVideo size={20} />
                                                </span>
                                                <div>
                                                    <strong>{item.filename}</strong>
                                                    <small>{item.content_type}</small>
                                                </div>
                                            </div>
                                        </td>
                                        <td>{bytes(item.size_bytes)}</td>
                                        <td>
                                            <Badge state={item.state} />
                                        </td>
                                        <td className="subtle">{date(item.created_at)}</td>
                                        <td>
                                            {item.state === 'pending' && canUpload ? (
                                                <button
                                                    className="text-button"
                                                    disabled={cancel.isPending || upload.isPending}
                                                    onClick={() => cancel.mutate(item.id)}
                                                >
                                                    取消上传
                                                </button>
                                            ) : null}
                                            <button
                                                className="text-button"
                                                disabled={item.state === 'pending'}
                                                onClick={() => setSelected(item)}
                                            >
                                                查看 <ArrowUpRight size={15} />
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                ) : !listing.error ? (
                    <Empty title="从第一段视频开始">
                        导入授权的 MP4 或 WebM 文件，原片将存储在当前节点。
                    </Empty>
                ) : null}
                {listing.data ? (
                    <Pager offset={offset} total={listing.data.total} onChange={setOffset} />
                ) : null}
            </section>
            {selected ? (
                <Modal title={selected.filename} onClose={() => setSelected(null)}>
                    <video
                        className="video"
                        controls
                        preload="metadata"
                        src={`/v1/assets/${selected.id}/content`}
                    />
                    <Notice>浏览器预览取决于源文件编码。当前尚未经过媒体准入。</Notice>
                    <dl className="details">
                        <dt>文件大小</dt>
                        <dd>{bytes(selected.size_bytes)}</dd>
                        <dt>内容摘要</dt>
                        <dd className="mono">{selected.sha256}</dd>
                        <dt>导入时间</dt>
                        <dd>{date(selected.created_at)}</dd>
                    </dl>
                    <Link className="button primary" to="/jobs">
                        准备处理任务 <ArrowUpRight size={16} />
                    </Link>
                </Modal>
            ) : null}
        </>
    );
}
