import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus } from 'lucide-react';
import { api, post } from '../api/client';
import type { JobDraftList, PipelineList, UploadList } from '../api/contracts';
import {
    Badge,
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

export default function Jobs() {
    const cache = useQueryClient();
    const [offset, setOffset] = useState(0);
    const [open, setOpen] = useState(false);
    const canWrite = usePermission('jobs:write');
    const query = useQuery({
        queryKey: ['jobs', offset],
        queryFn: ({ signal }) =>
            api<JobDraftList>(`/v1/job-drafts?limit=20&offset=${offset}`, { signal }),
    });
    const options = useQuery({
        queryKey: ['job-options'],
        enabled: open,
        queryFn: async ({ signal }) => {
            const [assets, pipelines] = await Promise.all([
                api<UploadList>('/v1/assets?limit=100', { signal }),
                api<PipelineList>('/v1/pipelines?limit=100', { signal }),
            ]);
            return {
                assets: assets.items.filter((x) => x.state === 'awaiting_admission'),
                pipelines: pipelines.items.filter((x) => x.state === 'draft'),
            };
        },
    });
    const save = useMutation({
        mutationFn: (form: FormData) => post('/v1/job-drafts', Object.fromEntries(form)),
        onSuccess: () => {
            setOpen(false);
            void cache.invalidateQueries({ queryKey: ['jobs'] });
        },
    });
    const archive = useMutation({
        mutationFn: (id: string) => post(`/v1/job-drafts/${id}:archive`),
        onSuccess: () => {
            void cache.invalidateQueries({ queryKey: ['jobs'] });
        },
    });
    return (
        <>
            <Heading
                eyebrow="PROCESSING"
                title="处理任务"
                description="提前准备视频和处理方案，保存每一次工作的上下文。"
                action={
                    canWrite ? (
                        <button
                            className="primary"
                            onClick={() => {
                                save.reset();
                                setOpen(true);
                            }}
                        >
                            <Plus size={17} />
                            新建草稿
                        </button>
                    ) : null
                }
            />
            <Notice>
                任务执行服务尚未接入。这里保存的是任务草稿，不会自动排队、推理或生成素材。
            </Notice>
            <ErrorNotice error={query.error || archive.error} />
            <section className="card">
                <div className="section-heading">
                    <h2>任务草稿</h2>
                    <span className="subtle">执行能力待接入</span>
                </div>
                {query.isPending ? (
                    <Loading />
                ) : query.data?.items.length ? (
                    <div className="table-wrap">
                        <table>
                            <thead>
                                <tr>
                                    <th>任务名称</th>
                                    <th>状态</th>
                                    <th>创建时间</th>
                                    <th>操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {query.data.items.map((item) => (
                                    <tr key={item.id}>
                                        <td>
                                            <strong>{item.name}</strong>
                                            <small className="mono">{item.id.slice(0, 20)}</small>
                                        </td>
                                        <td>
                                            <Badge state={item.state} />
                                        </td>
                                        <td>{date(item.created_at)}</td>
                                        <td>
                                            <div className="row-actions">
                                                <button disabled title="任务执行服务尚未接入">
                                                    开始处理
                                                </button>
                                                {canWrite && item.state === 'draft' ? (
                                                    <button
                                                        disabled={archive.isPending}
                                                        onClick={() => archive.mutate(item.id)}
                                                    >
                                                        归档
                                                    </button>
                                                ) : null}
                                            </div>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                ) : !query.error ? (
                    <Empty title="还没有处理任务">
                        上传视频并保存处理方案后，可以创建任务草稿。
                    </Empty>
                ) : null}
                {query.data ? (
                    <Pager offset={offset} total={query.data.total} onChange={setOffset} />
                ) : null}
            </section>
            {open ? (
                <Modal title="新建任务草稿" onClose={() => setOpen(false)}>
                    <ErrorNotice error={save.error || options.error} />
                    {options.isPending ? (
                        <Loading />
                    ) : (
                        <form
                            onSubmit={(e) => {
                                e.preventDefault();
                                save.mutate(new FormData(e.currentTarget));
                            }}
                        >
                            <label>
                                任务名称
                                <input
                                    autoFocus
                                    name="name"
                                    required
                                    maxLength={120}
                                    placeholder="例如：产品演示视频整理"
                                />
                            </label>
                            <label>
                                视频
                                <select name="asset_id" required defaultValue="">
                                    <option value="" disabled>
                                        选择已上传的视频
                                    </option>
                                    {options.data?.assets.map((x) => (
                                        <option key={x.id} value={x.id}>
                                            {x.filename}
                                        </option>
                                    ))}
                                </select>
                            </label>
                            <label>
                                处理方案
                                <select name="pipeline_id" required defaultValue="">
                                    <option value="" disabled>
                                        选择方案草稿
                                    </option>
                                    {options.data?.pipelines.map((x) => (
                                        <option key={x.id} value={x.id}>
                                            {x.name} · v{x.revision}
                                        </option>
                                    ))}
                                </select>
                            </label>
                            <p className="subtle">
                                需要先在视频库上传文件，并在管理区保存处理方案。
                            </p>
                            <button
                                className="primary"
                                disabled={
                                    save.isPending ||
                                    !options.data?.assets.length ||
                                    !options.data?.pipelines.length
                                }
                            >
                                保存草稿
                            </button>
                        </form>
                    )}
                </Modal>
            ) : null}
        </>
    );
}
