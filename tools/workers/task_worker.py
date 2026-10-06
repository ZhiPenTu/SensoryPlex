"""宿主任务工作器（Host Task Worker）：持续监听并执行控制台下发的真实媒体处理任务。

定位：
依据 AGENTS.md 规范第 2 条，子节点与多模态处理器允许在宿主机原生运行。
本工作器在宿主机前台或常驻运行，负责：
1. 保持同机算力节点（local-host）处于在线就绪状态（ready）；
2. 监听 Web 控制台派发的视频处理任务（task_process）；
3. 调度宿主 GStreamer + RapidOCR + Timeline 融合 + 事务性入库；
4. 任务完成后标记 completed，由底层常驻 relay 与 index 自动触发向量化与语义索引。
"""

import argparse
import json
import logging
import os
import pathlib
import signal
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins/python/common/src"))

import psycopg  # noqa: E402
from sensoryplex_gateway.settings import Settings  # noqa: E402

from tools.task_runner import run_video_task  # noqa: E402

PID_FILE = ROOT / ".data/task_worker.pid"
LOG_FILE = ROOT / ".data/task_worker.log"
NODE_ID = "local-host"
RUNNING = True


def _signal_handler(signum, frame):
    global RUNNING
    LOGGER.info("收到退出信号 (%s)，正在优雅收尾...", signum)
    RUNNING = False


# 忽略终端挂断信号，避免 shell 会话关闭导致工作器意外退出
if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

LOGGER = logging.getLogger("task_worker")


def start_nats_wake_listener(nats_url: str, wake_event: threading.Event) -> threading.Thread | None:
    """启动轻量 NATS 广播监听线程，在收到 tasks.ready 唤醒信号时触发 wake_event。"""
    try:
        import asyncio

        import nats
    except ImportError:
        LOGGER.info("未导入 nats-py 库，工作器沿用定时心跳对账模式")
        return None

    def _listen():
        async def _nats_sub():
            try:
                nc = await nats.connect(nats_url, connect_timeout=3.0, reconnect_time_wait=2.0)
                LOGGER.info("已建立 NATS 就绪任务事件唤醒通道: %s", nats_url)

                async def _on_msg(msg):
                    LOGGER.info("收到任务就绪唤醒广播 (%s)，立即触发认领", msg.subject)
                    wake_event.set()

                await nc.subscribe("sensoryplex.events.tasks.ready", cb=_on_msg)
                await nc.subscribe("sensoryplex.tasks.ready", cb=_on_msg)

                while RUNNING:
                    await asyncio.sleep(1.0)
                await nc.drain()
            except Exception as e:
                LOGGER.warning("NATS 事件唤醒监听未就绪 (%s)，工作器使用周期心跳对账", e)

        asyncio.run(_nats_sub())

    t = threading.Thread(target=_listen, daemon=True, name="nats-wake-listener")
    t.start()
    return t


def ensure_node_ready(conn) -> None:
    """兼容旧工作器节点；已登记 Agent 的心跳与下线状态只能由 Agent 管理。"""
    row = conn.execute(
        "SELECT node_id, status, session_token_hash FROM console_node WHERE node_id=%s",
        (NODE_ID,),
    ).fetchone()
    if not row:
        conn.execute(
            """
            INSERT INTO console_node(
                node_id, display_name, status, status_reason, platform, arch,
                cpu_cores, memory_bytes, unified_memory_bytes, is_co_located,
                last_heartbeat_at, enrolled_at, updated_at
            ) VALUES (
                %s, '本机数据面', 'ready', '', 'macos', 'aarch64',
                8, 17179869184, 17179869184, true,
                now(), now(), now()
            )
            """,
            (NODE_ID,),
        )
    elif not row["session_token_hash"]:
        conn.execute(
            """
            UPDATE console_node
            SET status='ready', status_reason='', last_heartbeat_at=now()
            WHERE node_id=%s
            """,
            (NODE_ID,),
        )


def process_pending_task(conn) -> bool:
    """检索并原子认领一个待处理的 task_process 意图。"""
    intent = conn.execute(
        """
        SELECT id, node_id, config, job_id
        FROM console_deployment_intent
        WHERE action='task_process' AND state='pending'
          -- v2 任务必须由 Node Agent 的 TaskExecutor 按 assignment/receipt 协议执行；
          -- 旧旁路不能领取它，更不能将其误写为 legacy OCR 的失败。
          AND COALESCE(config->>'execution_mode', 'legacy_ocr_v1') = 'legacy_ocr_v1'
        ORDER BY created_at ASC
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """
    ).fetchone()

    if not intent:
        # 同时检查是否有直接处于 processing 状态但 intent 漏领的任务
        draft = conn.execute(
            """
            SELECT draft.id, draft.owner, draft.asset_id, draft.pipeline_id, draft.name
            FROM console_job_draft AS draft
            JOIN console_pipeline AS pipeline ON pipeline.id=draft.pipeline_id
            WHERE draft.state='processing'
              AND (draft.target_node_id=%s OR draft.target_node_id IS NULL)
              -- 直接扫描 Job 只是遗留兼容入口，同样不能越过 v2 执行器。
              AND pipeline.execution_mode='legacy_ocr_v1'
            ORDER BY draft.dispatched_at ASC NULLS LAST, draft.created_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """,
            (NODE_ID,),
        ).fetchone()
        if not draft:
            return False

        job_id = draft[0]
        upload = conn.execute(
            "SELECT id, sha256, filename FROM console_upload WHERE id=%s",
            (draft[2],),
        ).fetchone()
        if not upload:
            LOGGER.error("任务 %s 关联的视频资产未找到: %s", job_id, draft[2])
            conn.execute(
                """
                UPDATE console_job_draft
                SET state='failed', error_code='asset_not_found', completed_at=now()
                WHERE id=%s
                """,
                (job_id,),
            )
            return True

        blob_path = ROOT / ".data/console-media" / upload[1][7:]
        task_config = {
            "task_type": "process_video",
            "job_id": job_id,
            "asset_id": upload[0],
            "sha256": upload[1],
            "filename": upload[2],
            "owner": draft[1],
            "blob_path": str(blob_path),
            "pipeline_id": draft[3],
        }
        intent_id = None
    else:
        intent_id = intent[0]
        job_id = intent[3]
        task_config = intent[2] if isinstance(intent[2], dict) else json.loads(intent[2])
        conn.execute(
            """
            UPDATE console_deployment_intent
            SET state='dispatched', dispatched_at=now()
            WHERE id=%s
            """,
            (intent_id,),
        )

    LOGGER.info("==> 认领视频处理任务: %s (原片: %s)", job_id, task_config.get("filename"))
    t0 = time.time()
    try:
        result = run_video_task(task_config)
        elapsed = time.time() - t0
        LOGGER.info(
            "<== 任务 %s 处理成功 (耗时 %.1fs): 产出素材 %d 条, 追加事实 %d 条, 事件 %d 条",
            job_id,
            elapsed,
            result.get("materials_count", 0),
            result.get("appended", 0),
            result.get("outbox_events", 0),
        )

        conn.execute(
            """
            UPDATE console_job_draft
            SET state='completed', completed_at=now(), error_code=NULL, error_detail=NULL
            WHERE id=%s
            """,
            (job_id,),
        )
        if intent_id:
            conn.execute(
                """
                UPDATE console_deployment_intent
                SET state='completed', completed_at=now(), error_code=NULL, error_detail=NULL
                WHERE id=%s
                """,
                (intent_id,),
            )
        conn.commit()
    except Exception as e:
        elapsed = time.time() - t0
        err_msg = str(e)
        LOGGER.error(
            "<== 任务 %s 处理失败 (耗时 %.1fs): %s", job_id, elapsed, err_msg, exc_info=True
        )
        conn.execute(
            """
            UPDATE console_job_draft
            SET state='failed', error_code='execution_failed',
                error_detail=%s, completed_at=now()
            WHERE id=%s
            """,
            (err_msg[:500], job_id),
        )
        if intent_id:
            conn.execute(
                """
                UPDATE console_deployment_intent
                SET state='failed', error_code='execution_failed',
                    error_detail=%s, completed_at=now()
                WHERE id=%s
                """,
                (err_msg[:500], intent_id),
            )
        conn.commit()

    return True


def run_worker_loop(poll_interval_s: float = 30.0, nats_url: str | None = None):
    settings = Settings()
    db_url = settings.database_url.get_secret_value()
    nats_url = nats_url or os.getenv("SENSORYPLEX_NATS_URL", "nats://127.0.0.1:24222")

    LOGGER.info("正在启动 SensoryPlex 宿主任务工作器 (Host Task Worker)...")
    LOGGER.info("监听节点: %s | 数据库: %s", NODE_ID, db_url.split("@")[-1])

    wake_event = threading.Event()
    start_nats_wake_listener(nats_url, wake_event)

    while RUNNING:
        try:
            with psycopg.connect(db_url, autocommit=False) as conn:
                ensure_node_ready(conn)
                conn.commit()

                # 循环处理所有待办任务，直到当前批次全部完成
                processed_any = False
                while RUNNING and process_pending_task(conn):
                    processed_any = True

                if not processed_any:
                    # 空载时挂起等待 NATS 事件唤醒；若无事件则按 poll_interval_s 兜底心跳
                    wake_event.wait(timeout=poll_interval_s)
                    wake_event.clear()
        except Exception as e:
            if not RUNNING:
                break
            LOGGER.error("工作器循环异常: %s，将在 3 秒后重试", e)
            time.sleep(3.0)

    LOGGER.info("SensoryPlex 宿主任务工作器已安全停止。")


def daemonize(log_path: pathlib.Path, pid_path: pathlib.Path):
    """标准 double-fork 脱离终端会话后台运行。"""
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)

    sys.stdout.flush()
    sys.stderr.flush()

    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    log_file = open(log_path, "a", encoding="utf-8")
    os.dup2(log_file.fileno(), sys.stdout.fileno())
    os.dup2(log_file.fileno(), sys.stderr.fileno())

    with open("/dev/null") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())

    pid_path.write_text(str(os.getpid()))


def stop_daemon(pid_path: pathlib.Path):
    if not pid_path.is_file():
        print(f"[task_worker] PID 文件不存在 ({pid_path})，工作器未在运行")
        return
    pid_str = pid_path.read_text().strip()
    try:
        pid = int(pid_str)
        os.kill(pid, signal.SIGTERM)
        print(f"[task_worker] 已向 PID {pid} 发送 SIGTERM 退出信号")
        for _ in range(20):
            time.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                break
        pid_path.unlink(missing_ok=True)
        print(f"[task_worker] 工作器 (PID {pid}) 已停止")
    except (ValueError, ProcessLookupError):
        pid_path.unlink(missing_ok=True)
        print(f"[task_worker] 进程 {pid_str} 不存在，已清理过期 PID 文件")


def check_status(pid_path: pathlib.Path) -> bool:
    if not pid_path.is_file():
        print(f"[task_worker] 工作器未在运行 (无 PID 文件: {pid_path})")
        return False
    pid_str = pid_path.read_text().strip()
    try:
        pid = int(pid_str)
        os.kill(pid, 0)
        print(f"[task_worker] 工作器运行中 (PID: {pid})")
        return True
    except OSError:
        print(f"[task_worker] 进程 {pid_str} 不存在 (僵尸 PID 文件)")
        return False


def main():
    parser = argparse.ArgumentParser(description="SensoryPlex 宿主任务执行工作器")
    parser.add_argument("--daemon", action="store_true", help="以独立后台守护进程方式运行")
    parser.add_argument("--stop", action="store_true", help="停止已运行的后台守护进程")
    parser.add_argument("--status", action="store_true", help="查看后台守护进程运行状态")
    parser.add_argument(
        "--interval", type=float, default=30.0, help="事件兜底轮询与心跳间隔（秒，默认 30.0）"
    )
    args = parser.parse_args()

    if args.stop:
        stop_daemon(PID_FILE)
        return

    if args.status:
        alive = check_status(PID_FILE)
        sys.exit(0 if alive else 1)

    if args.daemon:
        if PID_FILE.is_file():
            try:
                old_pid = int(PID_FILE.read_text().strip())
                os.kill(old_pid, 0)
                print(f"[task_worker] 错误: 工作器已在运行 (PID: {old_pid})")
                sys.exit(1)
            except OSError:
                PID_FILE.unlink(missing_ok=True)

        daemonize(LOG_FILE, PID_FILE)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] [task_worker] %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] [task_worker] %(message)s",
            datefmt="%H:%M:%S",
        )

    try:
        run_worker_loop(args.interval)
    finally:
        if args.daemon and PID_FILE.is_file():
            PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
