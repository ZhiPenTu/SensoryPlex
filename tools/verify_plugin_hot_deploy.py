#!/usr/bin/env python
"""ADR-030 插件热部署执行器验收。

按 AGENTS.md 的"底座强制容器 / 子节点插件允许宿主原生"分两层跑：

- `--scope api`（容器内）：控制面契约。受控制品仓同步与拒收、部署创建、fencing 拒绝、
  取消窗口、反向回滚、制品下载授权、升级余量预检。状态机由"虚拟 Agent 回报"驱动，
  不起真实插件进程。
- `--scope native`（宿主）：真实执行器。受控 bundle 的篡改/路径逃逸/摘要不符拒收、
  deploy-canary 状态机矩阵，以及预置模型首方插件的真机验收：
  Describe → ValidateConfig → Start → Health → 蓝绿 → Drain → Stop → 回滚。
  native 层**可重复运行**：槽位为空时首次部署走 `provision`，槽位已有 active 时自动改走
  蓝绿 `upgrade` 并在报告里注明（控制面的 `provision` 是引导动作，槽位一旦有 active 就不会
  再回到"空"，若把 `plugin_already_active` 当环境噪音就会让重复运行静默降级）。

两层共用同一组期望错误码，控制面与执行器的口径不会各写一套。

`--scope api` 会自建一个**验收专用节点**（`verify-hot-deploy`）并在收尾时吊销它，
不动 `local-host`；`--scope native` 必须用本机节点（平台服务适配器只托管**本机**进程）。
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import pathlib
import platform
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CANARY_PLUGIN = "org.sensoryplex.deploy-canary"
REAL_PLUGIN = "org.sensoryplex.embed-bge-onnx"
VERIFY_NODE = "verify-hot-deploy"
SQUEEZED_NODE = "verify-hot-deploy-squeezed"
CANDIDATE_NODE = "verify-hot-deploy-candidate"
VERIFY_FIXTURE_DIR = ".data/releases/_verify"
CANARY_INTERPRETER_BUDGET_S = 60.0
POLL_INTERVAL_S = 0.4


# ── 结果记录 ────────────────────────────────────────────────────────────────


class Report:
    """验收结果累加器：任何一条 FAIL 都让整轮非零退出。"""

    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def section(self, title: str) -> None:
        print(f"\n### {title}")

    def note(self, text: str) -> None:
        print(f"    · {text}")

    def check(self, label: str, condition, detail: str = "") -> bool:
        ok = bool(condition)
        self.results.append((label, ok, str(detail)))
        mark = "PASS" if ok else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"  [{mark}] {label}{suffix}")
        return ok

    def eq(self, label: str, actual, expected) -> bool:
        return self.check(label, actual == expected, f"actual={actual!r} expected={expected!r}")

    def expect_error(self, label: str, response: Response, expected: str) -> bool:
        return self.check(
            label,
            response.status >= 400 and response.reason == expected,
            f"status={response.status} reason={response.reason!r} expected={expected!r}",
        )

    @property
    def failures(self) -> list[tuple[str, bool, str]]:
        return [item for item in self.results if not item[1]]

    def finish(self) -> int:
        total, failed = len(self.results), len(self.failures)
        print(f"\n=== 热部署验收：{total - failed}/{total} 通过 ===")
        for label, _ok, detail in self.failures:
            print(f"  FAIL {label} — {detail}")
        return 0 if not failed else 1


# ── HTTP ───────────────────────────────────────────────────────────────────


@dataclass
class Response:
    status: int
    payload: dict
    # 非 JSON 响应（例如 Prometheus 文本格式的指标）保留原始正文，JSON 响应也一并留下，
    # 便于断言"响应里没有敏感内容"。
    raw: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def reason(self) -> str:
        return str(self.payload.get("reason_code") or self.payload.get("detail") or "")


def http_request(
    url: str, *, method: str = "GET", body=None, headers=None, cookie_jar=None, timeout=180.0
) -> Response:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    opener = (
        urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
        if cookie_jar is not None
        else urllib.request.build_opener()
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            return Response(response.status, _json_object(raw), raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as error:
        raw = error.read()
        payload = _json_object(raw)
        text = raw.decode("utf-8", "replace")
        if not payload:
            payload = {"detail": text[:400]}
        return Response(error.code, payload, text)


def _json_object(raw: bytes) -> dict:
    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def parse_prometheus(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """极简 Prometheus 文本解析：只取 `<name>{labels} value`，注释与空行忽略。

    这里刻意不引入 Prometheus 客户端库：验收要证明的是"API 真的产出了可被抓取的 exposition
    格式，且数值等于台账"，用一个不到 20 行的解析器就能独立验证格式本身，避免"用同一个库
    解析自己写的东西"这种自证。
    """
    series: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition("{")
        if rest:
            labels_text, _, value_text = rest.partition("}")
            labels = tuple(
                (key, value)
                for key, value in (
                    (part.partition("=")[0].strip(), part.partition("=")[2].strip().strip('"'))
                    for part in labels_text.split(",")
                    if part.strip()
                )
            )
        else:
            name, _, value_text = line.partition(" ")
            labels = ()
        series[(name.strip(), labels)] = float(value_text.strip() or 0)
    return series


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    """.env 的极简解析：只取验收需要的键，不引入 shell 语义。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class AdminApi:
    """管理会话：cookie 会话 + CSRF；所有写操作都走 `plugins:manage`。"""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.jar = http.cookiejar.CookieJar()
        self.csrf = ""
        self.principal = ""

    def login(self) -> Response:
        response = self.call(
            "POST",
            "/auth/v1/session",
            {"username": self.username, "password": self.password},
            csrf=False,
        )
        if response.ok:
            self.csrf = str(response.payload.get("csrf_token", ""))
            self.principal = str(response.payload.get("principal", ""))
        return response

    def call(self, method: str, path: str, body=None, *, csrf: bool = True) -> Response:
        headers = {}
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        return http_request(
            self.base_url + path, method=method, body=body, headers=headers, cookie_jar=self.jar
        )

    def get(self, path: str) -> Response:
        return self.call("GET", path)

    def post(self, path: str, body=None) -> Response:
        return self.call("POST", path, {} if body is None else body)


class AgentApi:
    """节点侧会话：Bearer token。只做注册、心跳、回报与受认证制品下载。"""

    def __init__(self, base_url: str, node_id: str, token: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.node_id = node_id
        self.token = token
        # 心跳会把所有 pending 意图一次性翻成 dispatched，所以没轮到处理的意图先缓冲起来，
        # 否则"顺手心跳一次"就等于把别人的意图丢了。
        self.buffered: list[dict] = []
        self.reconciliation_required: list[str] = []
        self.observed: list[dict] = []

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def bootstrap_local(
        self, display_name: str, capabilities: dict, api_token: str = ""
    ) -> Response:
        response = http_request(
            self.base_url + "/v1/agent/bootstrap-local",
            method="POST",
            body={
                "enrollment_token": "local-bootstrap",
                "node_id": self.node_id,
                "display_name": display_name,
                "is_co_located": True,
                "capabilities": capabilities,
            },
            headers={"Authorization": f"Bearer {api_token}"} if api_token else {},
        )
        if response.ok:
            self.token = str(response.payload.get("session_token", ""))
        return response

    def register_candidate(
        self, display_name: str, is_co_located: bool, capabilities: dict
    ) -> dict:
        """子节点零配置自报到：进入 `candidate`，等待管理员在网页批准（ADR-026）。"""
        response = http_request(
            self.base_url + "/v1/agent/candidate-register",
            method="POST",
            body={
                "enrollment_token": "candidate",
                "node_id": self.node_id,
                "display_name": display_name,
                "is_co_located": is_co_located,
                "capabilities": capabilities,
            },
        )
        if response.ok:
            self.token = str(response.payload.get("session_token", ""))
        return response.payload

    def heartbeat(self, observations: list[dict] | None = None) -> dict:
        """心跳兼两件事：上报平台服务观测、领一次性交付的部署意图。"""
        body = {
            "node_id": self.node_id,
            "session_token": self.token,
            "timestamp_unix_ms": int(time.time() * 1000),
            "available_memory_bytes": 0,
            "current_concurrency": 0,
            "running_instance_ids": [],
        }
        observations = self.observed if observations is None else observations
        if observations:
            body["runtime_observations"] = observations
        payload = http_request(
            self.base_url + "/v1/agent/heartbeat",
            method="POST",
            body=body,
            headers=self.auth_headers(),
        ).payload
        self.reconciliation_required = list(payload.get("reconciliation_required") or [])
        for intent in payload.get("pending_intents") or []:
            self.buffered.append(intent)
        return payload

    def pending_intents(self) -> list[dict]:
        return list(self.heartbeat().get("pending_intents") or [])

    def wait_for_intent(
        self, action: str, *, runtime_instance_id: str = "", timeout_s: float = 60.0
    ) -> dict | None:
        """意图是**一次性投递**（心跳把它从 pending 翻成 dispatched），所以这里必须轮询心跳。"""
        deadline = time.monotonic() + timeout_s
        while True:
            for index, intent in enumerate(self.buffered):
                if intent.get("action") != action:
                    continue
                if runtime_instance_id and intent.get("runtime_instance_id") != runtime_instance_id:
                    continue
                return self.buffered.pop(index)
            if time.monotonic() >= deadline:
                return None
            self.pending_intents()
            time.sleep(POLL_INTERVAL_S)

    def report_deployment(self, **fields) -> Response:
        payload = {"node_id": self.node_id}
        payload.update({key: value for key, value in fields.items() if value not in ("", None)})
        return http_request(
            self.base_url + "/v1/agent/report",
            method="POST",
            body=payload,
            headers=self.auth_headers(),
        )

    def download_release_bundle(self, release_id: str, target: pathlib.Path) -> pathlib.Path:
        """与 `tools/node_agent.py` 同口径：先落 `.part` 再 rename，只信落盘字节。"""
        target = pathlib.Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        request = urllib.request.Request(
            f"{self.base_url}/v1/agent/releases/{release_id}/bundle",
            headers={
                **self.auth_headers(),
                "User-Agent": f"SensoryPlex-NodeAgent/{self.node_id}",
            },
            method="GET",
        )
        temporary = target.with_name(target.name + ".part")
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                with temporary.open("wb") as handle:
                    shutil.copyfileobj(response, handle, length=1 << 20)
        except urllib.error.HTTPError as error:
            raw = error.read()
            temporary.unlink(missing_ok=True)
            code = str(_json_object(raw).get("reason_code", "")) or raw[:120].decode(
                "utf-8", "replace"
            )
            raise BundleDownloadError(f"HTTP {error.code}: {code}") from error
        except (urllib.error.URLError, OSError) as error:
            temporary.unlink(missing_ok=True)
            raise BundleDownloadError(f"bundle_download_failed: {error}") from error
        temporary.replace(target)
        return target


class BundleDownloadError(RuntimeError):
    """制品下载失败；执行器会把它转成稳定错误码 `release_bundle_unavailable`。"""


# ── 控制面契约验收（容器内） ───────────────────────────────────────────────


class ApiScenarios:
    """控制面契约验收：用**虚拟 Agent 回报**驱动状态机，不起真实插件进程。

    这里验的是"控制面是否按契约推进/拒绝"。真实进程、平台服务与 endpoint 契约由
    `NativeScenarios` 在宿主上验；两层分开跑是因为平台服务适配器只能托管**本机**进程。
    """

    def __init__(
        self,
        report: Report,
        admin: AdminApi,
        base_url: str,
        *,
        platform: str,
        arch: str,
        api_token: str,
        canary_config: dict,
    ) -> None:
        self.report = report
        self.admin = admin
        self.base_url = base_url
        self.platform = platform
        self.arch = arch
        self.api_token = api_token
        self.canary_config = canary_config
        self.agent = AgentApi(base_url, VERIFY_NODE)
        self.squeezed = AgentApi(base_url, SQUEEZED_NODE)
        self._releases: dict[str, dict] = {}

    # ── 通用 ──────────────────────────────────────────────────────────────

    def release(self, plugin_id: str) -> dict:
        if plugin_id not in self._releases:
            response = self.admin.get(f"/admin/v1/plugin-releases?plugin_id={plugin_id}")
            items = [
                item
                for item in response.payload.get("items", [])
                if item["platform"] == self.platform and item["arch"] == self.arch
            ]
            if not items:
                raise SystemExit(
                    f"制品仓里没有 {plugin_id} 的 {self.platform}-{self.arch} release："
                    "先跑 tools/plugin_release.py build 并同步"
                )
            self._releases[plugin_id] = items[0]
        return self._releases[plugin_id]

    def operation(self, operation_id: str) -> dict:
        response = self.admin.get(f"/admin/v1/plugin-deployments/{operation_id}")
        if not response.ok:
            raise SystemExit(f"读部署操作失败：{response.status} {response.reason}")
        return response.payload

    def wait_stage(self, operation_id: str, expected: str, timeout_s: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout_s
        payload: dict = {}
        while time.monotonic() < deadline:
            payload = self.operation(operation_id)
            if payload.get("stage") == expected:
                return payload
            time.sleep(POLL_INTERVAL_S)
        return payload

    def create(
        self,
        kind: str,
        plugin_id: str,
        release_id: str,
        config: dict | None = None,
        node_id: str = VERIFY_NODE,
    ):
        return self.admin.post(
            f"/admin/v1/nodes/{node_id}/plugins/{plugin_id}:{kind}",
            {"release_id": release_id, "config": config or {}},
        )

    def node(self, node_id: str) -> dict:
        return self.admin.get(f"/admin/v1/nodes/{node_id}").payload

    def bump_heartbeat(self, agent: AgentApi | None = None) -> None:
        """预检会拒绝心跳超过 60 秒的节点，所以每次创建部署之前先刷一次心跳。"""
        (agent or self.agent).heartbeat()

    def report_intent(self, intent: dict, agent: AgentApi | None = None, **fields) -> Response:
        payload = {
            "intent_id": intent["intent_id"],
            "instance_id": intent["instance_id"],
            "action": intent["action"],
            "operation_id": intent["operation_id"],
            "generation": int(intent["generation"]),
            "release_id": intent["release_id"],
            "runtime_instance_id": intent["runtime_instance_id"],
            "success": True,
            "actual_state": "PLUGIN_INSTANCE_STATE_READY",
        }
        payload.update({key: value for key, value in fields.items() if value not in ("", None)})
        return (agent or self.agent).report_deployment(**payload)

    def drive_candidate_to_active(
        self, operation: dict, agent: AgentApi, release: dict, *, config: dict | None = None
    ) -> dict:
        """按真实执行器的回报顺序把候选推到切换完成：staging → starting → candidate_ready。

        与 `node_agent_hot_deploy.stage_release` 同一契约：**只有** `candidate_ready` 请求切换。
        """
        candidate = operation["candidate_runtime_instance_id"]
        intent = agent.wait_for_intent(
            "DEPLOYMENT_ACTION_STAGE_RELEASE", runtime_instance_id=candidate
        )
        if not intent:
            return {}
        self.report_intent(
            intent,
            agent,
            stage="PLUGIN_OPERATION_STAGE_STAGING",
            staging_ms=1200,
            actual_state="PLUGIN_INSTANCE_STATE_INSTALLING",
        )
        self.report_intent(
            intent,
            agent,
            stage="PLUGIN_OPERATION_STAGE_STARTING",
            staging_ms=1200,
            starting_ms=900,
            actual_state="PLUGIN_INSTANCE_STATE_INSTALLING",
        )
        self.report_intent(
            intent,
            agent,
            stage="PLUGIN_OPERATION_STAGE_CANDIDATE_READY",
            staging_ms=1200,
            starting_ms=900,
            validating_ms=3000,
            endpoint="127.0.0.1:9",
            supervisor_id="org.sensoryplex.plugin.verify",
            verified_plugin_id=release["plugin_id"],
            verified_artifact_digest=release["artifact_digest"],
        )
        return self.wait_stage(operation["operation_id"], "PLUGIN_OPERATION_STAGE_SUCCEEDED")

    # ── 场景 1：受控制品仓同步与拒收 ───────────────────────────────────────

    def scenario_release_sync(self) -> None:
        self.report.section("控制面 · 受控制品仓同步与拒收")

        first = self.admin.post("/admin/v1/plugin-releases:sync")
        self.report.check("同步返回 2xx", first.ok, f"status={first.status}")
        second = self.admin.post("/admin/v1/plugin-releases:sync")
        payload = second.payload
        self.report.check(
            "第二轮同步无新增", not payload.get("imported"), str(payload.get("imported"))
        )
        self.report.check(
            "第二轮同步无拒收且 total 与 unchanged 一致",
            not payload.get("rejected")
            and payload.get("total") == len(payload.get("unchanged", [])),
            json.dumps(payload, ensure_ascii=False)[:200],
        )
        self.report.check(
            "所有 release 都是 first_party + authenticated",
            all(
                item["trust"] == "first_party" and item["authenticated"]
                for item in self.admin.get("/admin/v1/plugin-releases").payload["items"]
            ),
        )

        cases = {
            "bytes": lambda d: d.update(bundle_bytes=int(d["bundle_bytes"]) + 1),
            "digest": lambda d: d.update(bundle_digest="sha256:" + "0" * 64),
            "thirdparty": lambda d: d.update(trust="third_party", authenticated=False),
            "container": lambda d: d.update(form="container"),
            "escape": lambda d: d.update(bundle_path="../escape.tar.gz"),
        }
        expected = {
            "bytes": "bundle_bytes_mismatch",
            "digest": "bundle_digest_mismatch",
            "thirdparty": "release_not_first_party_authenticated",
            "container": "unsupported_form",
            "escape": "bundle_path_outside_repository",
        }
        self._write_release_fixtures(cases)
        try:
            response = self.admin.post("/admin/v1/plugin-releases:sync")
            rejected = {
                item.split(":", 1)[0]: item.split(":", 1)[1]
                for item in response.payload.get("rejected", [])
            }
            for case, reason in expected.items():
                label = f"_verify/{case}/{self.platform}-{self.arch}/release.json"
                self.report.check(
                    f"拒收 {case}",
                    rejected.get(label) == reason,
                    f"actual={rejected.get(label)!r} expected={reason!r}",
                )
            self.report.check(
                "拒收后仍未导入任何非首方/篡改制品的 release",
                not response.payload.get("imported"),
                str(response.payload.get("imported")),
            )
        finally:
            shutil.rmtree(ROOT / VERIFY_FIXTURE_DIR, ignore_errors=True)

    def _write_release_fixtures(self, cases: dict) -> None:
        source_dir = (
            ROOT / ".data/releases" / CANARY_PLUGIN / "0.1.0" / f"{self.platform}-{self.arch}"
        )
        source_descriptor = json.loads((source_dir / "release.json").read_text())
        source_bundle = source_dir / pathlib.Path(source_descriptor["bundle_path"]).name
        for case, mutate in cases.items():
            target = ROOT / VERIFY_FIXTURE_DIR / case / f"{self.platform}-{self.arch}"
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_bundle, target / "bundle.tar.gz")
            descriptor = json.loads(json.dumps(source_descriptor))
            descriptor["release_id"] = f"rel_verify_{case}"
            descriptor["plugin_id"] = f"org.sensoryplex.verify-{case}"
            descriptor["bundle_path"] = f"_verify/{case}/{self.platform}-{self.arch}/bundle.tar.gz"
            mutate(descriptor)
            (target / "release.json").write_text(json.dumps(descriptor, indent=2) + "\n")

    # ── 场景 2：部署意图形状、fencing 与蓝绿切换 ───────────────────────────

    def scenario_deployment_lifecycle(self) -> dict:
        self.report.section("控制面 · 部署创建 → 意图形状 → fencing → 蓝绿切换")
        self.bump_heartbeat()
        release = self.release(CANARY_PLUGIN)
        created = self.create("provision", CANARY_PLUGIN, release["release_id"], self.canary_config)
        self.report.check(
            "创建 provision 返回 201",
            created.status == 201,
            f"status={created.status} reason={created.reason}",
        )
        operation = created.payload
        operation_id = operation.get("operation_id", "")
        self.report.eq(
            "初始阶段 accepted", operation.get("stage"), "PLUGIN_OPERATION_STAGE_ACCEPTED"
        )
        self.report.eq("generation 从 1 开始", int(operation.get("generation", 0)), 1)
        self.report.eq(
            "候选运行实例初始状态 planned",
            (operation.get("candidate") or {}).get("state"),
            "PLUGIN_RUNTIME_STATE_PLANNED",
        )
        self.report.check(
            "provision 没有 from 实例（槽位此前为空）",
            not operation.get("from_runtime_instance_id"),
        )
        instance_id = operation.get("instance_id", "")
        candidate_id = operation.get("candidate_runtime_instance_id", "")

        intent = self.agent.wait_for_intent(
            "DEPLOYMENT_ACTION_STAGE_RELEASE", runtime_instance_id=candidate_id
        )
        if not intent:
            self.report.check("拿到 stage_release 意图", False, "心跳未下发意图")
            return {"operation_id": operation_id, "instance_id": instance_id}
        self._assert_intent_shape(intent, release)

        self.report_intent(intent, stage="PLUGIN_OPERATION_STAGE_STAGING", staging_ms=1200)
        staged = self.wait_stage(operation_id, "PLUGIN_OPERATION_STAGE_STAGING")
        self.report.eq(
            "staging 回报推进操作阶段", staged.get("stage"), "PLUGIN_OPERATION_STAGE_STAGING"
        )
        self.report.eq(
            "staging 后候选实例状态为 staged",
            (staged.get("candidate") or {}).get("state"),
            "PLUGIN_RUNTIME_STATE_STAGED",
        )

        # fencing：操作仍在飞行中时，旧 generation / 错 release 的回报必须被拒。
        stale_generation = self.agent.report_deployment(
            intent_id=intent["intent_id"],
            instance_id=intent["instance_id"],
            action=intent["action"],
            success=True,
            actual_state="PLUGIN_INSTANCE_STATE_READY",
            operation_id=operation_id,
            generation=int(intent["generation"]) + 1,
            release_id=intent["release_id"],
            runtime_instance_id=candidate_id,
            stage="PLUGIN_OPERATION_STAGE_VALIDATING",
        )
        self.report.expect_error(
            "旧 generation 回报被拒", stale_generation, "fencing_token_mismatch"
        )

        wrong_release = self.agent.report_deployment(
            intent_id=intent["intent_id"],
            instance_id=intent["instance_id"],
            action=intent["action"],
            success=True,
            actual_state="PLUGIN_INSTANCE_STATE_READY",
            operation_id=operation_id,
            generation=int(intent["generation"]),
            release_id="rel_not_this_candidate",
            runtime_instance_id=candidate_id,
            stage="PLUGIN_OPERATION_STAGE_VALIDATING",
        )
        self.report.expect_error("错 release 回报被拒", wrong_release, "stale_deployment_report")

        wrong_runtime = self.agent.report_deployment(
            intent_id=intent["intent_id"],
            instance_id=intent["instance_id"],
            action=intent["action"],
            success=True,
            actual_state="PLUGIN_INSTANCE_STATE_READY",
            operation_id=operation_id,
            generation=int(intent["generation"]),
            release_id=intent["release_id"],
            runtime_instance_id="rti_not_the_candidate",
            stage="PLUGIN_OPERATION_STAGE_VALIDATING",
        )
        self.report.expect_error("错运行实例回报被拒", wrong_runtime, "stale_deployment_report")

        self.report_intent(
            intent,
            stage="PLUGIN_OPERATION_STAGE_STARTING",
            staging_ms=1200,
            starting_ms=900,
            actual_state="PLUGIN_INSTANCE_STATE_INSTALLING",
        )
        starting = self.wait_stage(operation_id, "PLUGIN_OPERATION_STAGE_STARTING")
        self.report.eq(
            "starting 回报推进操作阶段",
            starting.get("stage"),
            "PLUGIN_OPERATION_STAGE_STARTING",
        )
        # 真实执行器在验证阶段只回报 candidate_ready；这里额外插一次 validating 回报，
        # 确认"验证进行中"不会提前切换 active（切换只由 candidate_ready 触发）。
        self.report_intent(
            intent,
            stage="PLUGIN_OPERATION_STAGE_VALIDATING",
            staging_ms=1200,
            starting_ms=900,
            validating_ms=3000,
        )
        validating = self.wait_stage(operation_id, "PLUGIN_OPERATION_STAGE_VALIDATING")
        self.report.eq(
            "validating 只推进阶段与实例状态，不切换 active",
            (validating.get("stage"), (validating.get("candidate") or {}).get("state")),
            ("PLUGIN_OPERATION_STAGE_VALIDATING", "PLUGIN_RUNTIME_STATE_VALIDATING"),
        )
        self.report_intent(
            intent,
            stage="PLUGIN_OPERATION_STAGE_CANDIDATE_READY",
            staging_ms=1200,
            starting_ms=900,
            validating_ms=3000,
            supervisor_id="org.sensoryplex.plugin.verify",
            verified_plugin_id=CANARY_PLUGIN,
            verified_artifact_digest=release["artifact_digest"],
            endpoint="127.0.0.1:9",
        )
        verified = self.wait_stage(operation_id, "PLUGIN_OPERATION_STAGE_SUCCEEDED")
        self.report.eq(
            "无前置实例时切换后直接 succeeded",
            verified.get("stage"),
            "PLUGIN_OPERATION_STAGE_SUCCEEDED",
        )
        slot = self.node_slot(instance_id)
        self.report.eq(
            "槽位 active 指针指向候选实例",
            slot.get("active_runtime_instance_id"),
            candidate_id,
        )
        self.report.eq(
            "槽位已经存下 active release", slot.get("active_release_id"), release["release_id"]
        )
        self.report.eq("槽位 generation = 操作 generation", int(slot.get("generation", 0)), 1)
        self.report.eq(
            "verified 身份被如实落库",
            (verified.get("candidate") or {}).get("verified_plugin_id"),
            CANARY_PLUGIN,
        )

        duplicate = self.agent.report_deployment(
            intent_id=intent["intent_id"],
            instance_id=intent["instance_id"],
            action=intent["action"],
            success=True,
            actual_state="PLUGIN_INSTANCE_STATE_READY",
            operation_id=operation_id,
            generation=int(intent["generation"]),
            release_id=intent["release_id"],
            runtime_instance_id=candidate_id,
            stage="PLUGIN_OPERATION_STAGE_CANDIDATE_READY",
        )
        self.report.expect_error("重复投递同一意图被拒", duplicate, "duplicate_deployment_report")
        return {"operation_id": operation_id, "instance_id": instance_id, "release": release}

    def _assert_intent_shape(self, intent: dict, release: dict) -> None:
        allowed = {
            "intent_id",
            "instance_id",
            "node_id",
            "plugin_id",
            "plugin_version",
            "action",
            "artifact_digest",
            "rollback_digest",
            "config",
            "created_at",
            "deadline_unix_ms",
            "operation_id",
            "generation",
            "release_id",
            "bundle_digest",
            "runtime_instance_id",
            "grace_period_ms",
            "config_hash",
        }
        self.report.check(
            "意图字段不超出契约白名单（无 URL / 命令 / 宿主路径 / 密钥）",
            set(intent) <= allowed,
            f"unexpected={sorted(set(intent) - allowed)}",
        )
        forbidden = ("url", "command", "argv", "path", "token", "secret", "credential", "env")
        self.report.check(
            "意图字段名里没有下载地址/命令/路径/凭据语义",
            not [key for key in intent if any(word in key.lower() for word in forbidden)],
            str(sorted(intent)),
        )
        blob = json.dumps(intent, ensure_ascii=False)
        self.report.check(
            "意图正文里没有本机文件系统路径",
            "/Users/" not in blob and "/tmp/" not in blob and "http" not in blob,
            blob[:200],
        )
        self.report.eq("意图带 release_id", intent.get("release_id"), release["release_id"])
        self.report.eq(
            "意图带 bundle_digest", intent.get("bundle_digest"), release["bundle_digest"]
        )
        self.report.eq(
            "意图带 artifact_digest", intent.get("artifact_digest"), release["artifact_digest"]
        )
        self.report.check(
            "意图带 operation/generation/runtime 身份",
            bool(intent.get("operation_id")) and int(intent.get("generation", 0)) >= 1,
            f"operation_id={intent.get('operation_id')!r} generation={intent.get('generation')!r}",
        )
        self.report.check(
            "排空 grace 不短于插件声明的最大请求 deadline",
            int(intent.get("grace_period_ms", 0)) >= int(release["default_deadline_ms"]),
            f"grace={intent.get('grace_period_ms')} declared={release['default_deadline_ms']}",
        )
        self.report.check(
            "意图带未来截止时间",
            int(intent.get("deadline_unix_ms", 0)) > int(time.time() * 1000),
            str(intent.get("deadline_unix_ms")),
        )

    def node_slot(self, instance_id: str, node_id: str = VERIFY_NODE) -> dict:
        response = self.admin.get(f"/admin/v1/nodes/{node_id}")
        for slot in response.payload.get("instances", []) or []:
            if slot.get("instance_id") == instance_id:
                return slot
        return {}

    # ── 场景 3：升级、排空与取消窗口 ───────────────────────────────────────

    def scenario_upgrade_drain_and_cancel_window(self, state: dict) -> dict:
        self.report.section("控制面 · 升级（旧 active 与候选并存）→ 排空 → 取消窗口关闭")
        release = state["release"]
        self.bump_heartbeat()
        created = self.create("upgrade", CANARY_PLUGIN, release["release_id"], self.canary_config)
        self.report.check(
            "升级创建返回 201", created.status == 201, f"status={created.status} {created.reason}"
        )
        operation = created.payload
        operation_id = operation["operation_id"]
        old_runtime_id = operation.get("from_runtime_instance_id", "")
        self.report.eq("升级的 from 是旧 active 实例", old_runtime_id, state["active_runtime_id"])
        self.report.eq("升级 generation 递增", int(operation.get("generation", 0)), 2)

        intent = self.agent.wait_for_intent(
            "DEPLOYMENT_ACTION_STAGE_RELEASE",
            runtime_instance_id=operation["candidate_runtime_instance_id"],
        )
        if not intent:
            self.report.check("拿到升级意图", False, "心跳未下发意图")
            return state
        self.report_intent(
            intent,
            stage="PLUGIN_OPERATION_STAGE_CANDIDATE_READY",
            staging_ms=800,
            starting_ms=700,
            validating_ms=2500,
            supervisor_id="org.sensoryplex.plugin.verify",
            verified_plugin_id=CANARY_PLUGIN,
            verified_artifact_digest=release["artifact_digest"],
            endpoint="127.0.0.1:9",
        )
        draining = self.wait_stage(operation_id, "PLUGIN_OPERATION_STAGE_DRAINING_OLD")
        self.report.eq(
            "切换后进入 draining_old（旧实例仍在排空）",
            draining.get("stage"),
            "PLUGIN_OPERATION_STAGE_DRAINING_OLD",
        )
        self.report.eq(
            "旧实例被标记为 previous 并处于 draining",
            (
                (draining.get("previous") or {}).get("role"),
                (draining.get("previous") or {}).get("state"),
            ),
            ("PLUGIN_RUNTIME_ROLE_PREVIOUS", "PLUGIN_RUNTIME_STATE_DRAINING"),
        )
        self.report.eq(
            "切换后槽位 active 指针已经是候选实例（不是被替换的旧实例）",
            (draining.get("active") or {}).get("runtime_instance_id"),
            operation["candidate_runtime_instance_id"],
        )
        self.report.eq(
            "切换后 active / previous 角色分明（旧实例留作 previous）",
            (
                (draining.get("active") or {}).get("role"),
                (draining.get("previous") or {}).get("role"),
            ),
            ("PLUGIN_RUNTIME_ROLE_ACTIVE", "PLUGIN_RUNTIME_ROLE_PREVIOUS"),
        )
        self.report.check(
            "被替换实例仍由 from_runtime_instance_id 保留（蓝绿并存可追溯）",
            draining.get("from_runtime_instance_id") == old_runtime_id,
            f"from={draining.get('from_runtime_instance_id')} old={old_runtime_id}",
        )
        self.report.expect_error(
            "切换后取消窗口已关闭",
            self.admin.post(f"/admin/v1/plugin-deployments/{operation_id}:cancel"),
            "plugin_deployment_cancel_window_closed",
        )

        drain_intent = self.agent.wait_for_intent(
            "DEPLOYMENT_ACTION_DRAIN", runtime_instance_id=old_runtime_id
        )
        if not drain_intent:
            self.report.check("拿到排空意图", False, "心跳未下发排空意图")
            return state
        self.report.eq(
            "排空意图的 release 是旧实例的 release",
            drain_intent["release_id"],
            release["release_id"],
        )
        self.report.check(
            "排空 grace 不短于旧 release 声明的 deadline",
            int(drain_intent.get("grace_period_ms", 0)) >= int(release["default_deadline_ms"]),
            f"grace={drain_intent.get('grace_period_ms')} "
            f"declared={release['default_deadline_ms']}",
        )
        drained = self.report_intent(
            drain_intent,
            draining_ms=4200,
            verified_plugin_id=CANARY_PLUGIN,
            verified_artifact_digest=release["artifact_digest"],
            actual_state="PLUGIN_INSTANCE_STATE_UNINSTALLED",
        )
        self.report.check("排空回报被接受", drained.ok, f"status={drained.status} {drained.reason}")
        settled = self.wait_stage(operation_id, "PLUGIN_OPERATION_STAGE_SUCCEEDED")
        self.report.eq(
            "排空完成后操作 succeeded", settled.get("stage"), "PLUGIN_OPERATION_STAGE_SUCCEEDED"
        )
        self.report.eq("排空耗时被记录", int(settled.get("draining_ms", 0)), 4200)
        self.report.eq(
            "旧实例已 stopped",
            (settled.get("previous") or {}).get("state"),
            "PLUGIN_RUNTIME_STATE_STOPPED",
        )
        return {
            **state,
            "upgrade_operation_id": operation_id,
            "active_runtime_id": operation["candidate_runtime_instance_id"],
            "previous_runtime_id": old_runtime_id,
        }

    # ── 场景 4：显式回滚 = 反向部署操作 ────────────────────────────────────

    def scenario_rollback(self, state: dict) -> dict:
        self.report.section("控制面 · 显式回滚（反向操作，不改写历史）")
        upgrade_id = state["upgrade_operation_id"]
        before = self.operation(upgrade_id)
        self.bump_heartbeat()
        response = self.admin.post(f"/admin/v1/plugin-deployments/{upgrade_id}:rollback")
        self.report.check(
            "回滚创建返回 201",
            response.status == 201,
            f"status={response.status} {response.reason}",
        )
        rollback = response.payload
        self.report.eq("回滚操作 kind=rollback", rollback.get("kind"), "rollback")
        self.report.eq("回滚指回原操作", rollback.get("rollback_of_operation_id"), upgrade_id)
        self.report.eq(
            "回滚的是槽位记下的上一版本 release",
            rollback.get("release_id"),
            state["release"]["release_id"],
        )
        self.report.eq(
            "回滚 generation = 槽位 generation + 1",
            int(rollback.get("generation", 0)),
            int(before.get("generation", 0)) + 1,
        )
        history = self.operation(upgrade_id)
        self.report.check(
            "历史操作行未被改写",
            (history["kind"], history["stage"], history["error_code"])
            == (before["kind"], before["stage"], before["error_code"]),
            f"before={before['stage']} after={history['stage']}",
        )

        operation_id = rollback["operation_id"]
        intent = self.agent.wait_for_intent(
            "DEPLOYMENT_ACTION_STAGE_RELEASE",
            runtime_instance_id=rollback["candidate_runtime_instance_id"],
        )
        if not intent:
            self.report.check("拿到回滚意图", False, "心跳未下发意图")
            return state
        self.report_intent(
            intent,
            stage="PLUGIN_OPERATION_STAGE_CANDIDATE_READY",
            staging_ms=700,
            starting_ms=650,
            validating_ms=2200,
            supervisor_id="org.sensoryplex.plugin.verify",
            verified_plugin_id=CANARY_PLUGIN,
            verified_artifact_digest=state["release"]["artifact_digest"],
            endpoint="127.0.0.1:9",
        )
        draining = self.wait_stage(operation_id, "PLUGIN_OPERATION_STAGE_DRAINING_OLD")
        self.report.check(
            "回滚切换完成并开始排空被替换的实例",
            draining.get("stage") == "PLUGIN_OPERATION_STAGE_DRAINING_OLD",
            f"stage={draining.get('stage')}",
        )
        drain_intent = self.agent.wait_for_intent(
            "DEPLOYMENT_ACTION_DRAIN",
            runtime_instance_id=state["active_runtime_id"],
        )
        if drain_intent:
            self.report_intent(
                drain_intent, draining_ms=1500, actual_state="PLUGIN_INSTANCE_STATE_UNINSTALLED"
            )
        settled = self.wait_stage(operation_id, "PLUGIN_OPERATION_STAGE_SUCCEEDED")
        self.report.eq(
            "回滚操作最终 succeeded", settled.get("stage"), "PLUGIN_OPERATION_STAGE_SUCCEEDED"
        )
        slot = self.node_slot(rollback["instance_id"])
        self.report.eq(
            "槽位 active 指针指回上一个运行实例",
            slot.get("active_runtime_instance_id"),
            rollback["candidate_runtime_instance_id"],
        )
        return {
            **state,
            "rollback_operation_id": operation_id,
            "active_runtime_id": rollback["candidate_runtime_instance_id"],
        }

    # ── 场景 5：切换前取消（旧 active 不受影响） ────────────────────────────

    def scenario_cancel_pre_cutover(self, state: dict) -> None:
        self.report.section("控制面 · 切换前取消：候选被停，旧 active 指针不变")
        slot_before = self.node_slot(state["instance_id"])
        self.bump_heartbeat()
        created = self.create(
            "upgrade", CANARY_PLUGIN, state["release"]["release_id"], self.canary_config
        )
        if created.status != 201:
            self.report.check(
                "创建待取消的操作", False, f"status={created.status} {created.reason}"
            )
            return
        operation = created.payload
        operation_id = operation["operation_id"]
        intent = self.agent.wait_for_intent(
            "DEPLOYMENT_ACTION_STAGE_RELEASE",
            runtime_instance_id=operation["candidate_runtime_instance_id"],
        )
        if intent:
            self.report_intent(intent, stage="PLUGIN_OPERATION_STAGE_STAGING", staging_ms=400)
        cancelled = self.admin.post(f"/admin/v1/plugin-deployments/{operation_id}:cancel")
        self.report.check(
            "切换前取消被接受", cancelled.ok, f"status={cancelled.status} {cancelled.reason}"
        )
        self.report.eq(
            "操作阶段 cancelled", cancelled.payload.get("stage"), "PLUGIN_OPERATION_STAGE_CANCELLED"
        )
        after = self.operation(operation_id)
        self.report.eq(
            "候选实例被标记 failed 并落错误码",
            (
                (after.get("candidate") or {}).get("state"),
                (after.get("candidate") or {}).get("error_code"),
            ),
            ("PLUGIN_RUNTIME_STATE_FAILED", "cancelled_by_administrator"),
        )
        slot_after = self.node_slot(state["instance_id"])
        self.report.eq(
            "旧 active 指针保持不变",
            slot_after.get("active_runtime_instance_id"),
            slot_before.get("active_runtime_instance_id"),
        )
        stop_intent = self.agent.wait_for_intent(
            "DEPLOYMENT_ACTION_STOP",
            runtime_instance_id=operation["candidate_runtime_instance_id"],
        )
        if not stop_intent:
            self.report.check("取消下发了候选清理意图", False, "未收到 action=stop 意图")
            return
        self.report.check("取消下发了候选清理意图 (action=stop)", True)
        stopped = self.report_intent(stop_intent, actual_state="PLUGIN_INSTANCE_STATE_UNINSTALLED")
        self.report.check(
            "取消后的清理回报可以被收尾（否则操作与事实永远不一致）",
            stopped.ok,
            f"status={stopped.status} reason={stopped.reason}",
        )
        self.report.expect_error(
            "已取消的操作不能再取消",
            self.admin.post(f"/admin/v1/plugin-deployments/{operation_id}:cancel"),
            "plugin_deployment_operation_already_closed",
        )

    # ── 场景 6：制品下载授权 ───────────────────────────────────────────────

    def scenario_bundle_entitlement(self) -> None:
        self.report.section("控制面 · release 下载只向持有匹配意图的 Agent 会话开放")
        release = self.release(CANARY_PLUGIN)
        entitled = self.admin.post("/admin/v1/plugin-releases:sync")
        self.report.check("同步幂等（下载授权用例前置）", entitled.ok, f"status={entitled.status}")

        self.bump_heartbeat()
        created = self.create("upgrade", CANARY_PLUGIN, release["release_id"], self.canary_config)
        if created.status == 201:
            operation = created.payload
            intent = self.agent.wait_for_intent(
                "DEPLOYMENT_ACTION_STAGE_RELEASE",
                runtime_instance_id=operation["candidate_runtime_instance_id"],
            )
            target = pathlib.Path("/tmp/sensoryplex-hot-deploy-check/entitled.tar.gz")
            try:
                self.agent.download_release_bundle(release["release_id"], target)
                self.report.check(
                    "持有未完成意图的 Agent 可以下载 bundle",
                    target.is_file() and target.stat().st_size == int(release["bundle_bytes"]),
                    f"bytes={target.stat().st_size if target.is_file() else 0}",
                )
            except BundleDownloadError as error:
                self.report.check("持有未完成意图的 Agent 可以下载 bundle", False, str(error))
            # 收尾：取消这个操作，避免给下一轮留下悬挂意图。
            if intent:
                self.report_intent(intent, stage="PLUGIN_OPERATION_STAGE_STAGING", staging_ms=1)
            self.admin.post(f"/admin/v1/plugin-deployments/{operation['operation_id']}:cancel")
            stop_intent = self.agent.wait_for_intent(
                "DEPLOYMENT_ACTION_STOP",
                runtime_instance_id=operation["candidate_runtime_instance_id"],
            )
            if stop_intent:
                self.report_intent(stop_intent, actual_state="PLUGIN_INSTANCE_STATE_UNINSTALLED")
        else:
            self.report.check(
                "创建下载授权用例的操作", False, f"status={created.status} {created.reason}"
            )

        # 无意图的 release：授权必须被拒。
        unentitled = self.release(REAL_PLUGIN)
        try:
            self.agent.download_release_bundle(
                unentitled["release_id"], pathlib.Path("/tmp/x.tar.gz")
            )
            self.report.check("无匹配意图时下载被拒", False, "下载居然成功了")
        except BundleDownloadError as error:
            self.report.check(
                "无匹配意图时下载被拒 (reason_code=release_not_entitled_for_this_agent)",
                "release_not_entitled_for_this_agent" in str(error),
                str(error),
            )
        anonymous = http_request(
            f"{self.base_url}/v1/agent/releases/{release['release_id']}/bundle"
        )
        self.report.expect_error(
            "不带节点会话令牌下载被拒", anonymous, "missing_node_session_token"
        )
        bad_token = http_request(
            f"{self.base_url}/v1/agent/releases/{release['release_id']}/bundle",
            headers={"Authorization": "Bearer sp_node_not_a_real_token"},
        )
        self.report.expect_error("伪造节点令牌下载被拒", bad_token, "invalid_node_credentials")

    # ── 场景 7：升级余量预检 ───────────────────────────────────────────────

    def scenario_headroom(self) -> None:
        self.report.section("控制面 · 升级余量不足时拒绝，不降级为停机更新")
        # 受限节点：内存刚好满足插件 preflight 的最低要求（embed 声明 2Gi），但放不下
        # "旧实例声明 + 候选声明"两份。先让 canary（512MiB）在受限节点上成为 active，
        # 再部署 embed：余量只剩 1.5GiB，必须显式拒绝，而不是停掉旧实例硬上。
        squeezed = self.squeezed
        squeezed.bootstrap_local(
            "热部署验收节点（内存受限，契约层）",
            {
                "platform": self.platform,
                "arch": self.arch,
                "cpu_cores": 2,
                "memory_bytes": 2 << 30,
                "unified_memory_bytes": 0,
                "accelerators": [],
                "supported_artifacts": ["local_native"],
                "labels": {"verify": "plugin_hot_deploy", "constrained": "true"},
            },
            self.api_token,
        )
        squeezed.heartbeat()
        canary = self.release(CANARY_PLUGIN)
        placeholder = self.create(
            "provision", CANARY_PLUGIN, canary["release_id"], self.canary_config, SQUEEZED_NODE
        )
        self.report.check(
            "受限节点先放得下轻量占位实例（canary 512MiB）",
            placeholder.status == 201,
            f"status={placeholder.status} reason={placeholder.reason}",
        )
        if placeholder.status == 201:
            adopted = self.drive_candidate_to_active(placeholder.payload, squeezed, canary)
            self.report.eq(
                "占位实例成为 active（此后只剩 1.5GiB 余量）",
                adopted.get("stage"),
                "PLUGIN_OPERATION_STAGE_SUCCEEDED",
            )
            self.report.expect_error(
                "同一槽位再次 provision 被拒（不是靠余量兜底）",
                self.create(
                    "provision",
                    CANARY_PLUGIN,
                    canary["release_id"],
                    self.canary_config,
                    SQUEEZED_NODE,
                ),
                "plugin_already_active",
            )
        release = self.release(REAL_PLUGIN)
        response = self.admin.post(
            f"/admin/v1/nodes/{SQUEEZED_NODE}/plugins/{REAL_PLUGIN}:provision",
            {"release_id": release["release_id"], "config": {}},
        )
        self.report.expect_error(
            "余量不足返回 upgrade_headroom_insufficient", response, "upgrade_headroom_insufficient"
        )
        detail = self.admin.get(f"/admin/v1/nodes/{SQUEEZED_NODE}").payload
        self.report.check(
            "余量不足时没有为 embed 建槽位/运行实例",
            all(item.get("plugin_id") != REAL_PLUGIN for item in (detail.get("instances") or [])),
            json.dumps(detail.get("instances"))[:200],
        )
        canary_slot = next(
            (
                item
                for item in (detail.get("instances") or [])
                if item.get("plugin_id") == CANARY_PLUGIN
            ),
            {},
        )
        self.report.check(
            "余量不足不降级为停机更新：旧实例仍是 active",
            bool(canary_slot.get("active_runtime_instance_id"))
            and canary_slot.get("active_release_id") == canary["release_id"],
            json.dumps(canary_slot)[:200],
        )

    # ── 场景 8：节点准入 ───────────────────────────────────────────────────

    def scenario_node_admission(self) -> None:
        self.report.section("控制面 · 候选节点未被准入时不得部署")
        # 走子节点自己的零配置自报到通路（`--candidate` / candidate-register），
        # 不是"管理员签发令牌直接准入"的 enroll：只有前者才停在 candidate 等批准。
        candidate = AgentApi(self.base_url, CANDIDATE_NODE)
        registration = candidate.register_candidate(
            "热部署验收节点（未准入）",
            False,
            {
                "platform": self.platform,
                "arch": self.arch,
                "cpu_cores": 4,
                "memory_bytes": 8 << 30,
                "unified_memory_bytes": 0,
                "accelerators": [],
                "supported_artifacts": ["local_native"],
                "labels": {"verify": "plugin_hot_deploy"},
            },
        )
        self.report.eq("自报到得到候选节点", registration.get("status"), "NODE_STATUS_CANDIDATE")
        if not candidate.token:
            self.report.check("候选节点拿到会话令牌", False, json.dumps(registration)[:160])
        release = self.release(CANARY_PLUGIN)
        response = self.admin.post(
            f"/admin/v1/nodes/{CANDIDATE_NODE}/plugins/{CANARY_PLUGIN}:provision",
            {"release_id": release["release_id"], "config": self.canary_config},
        )
        self.report.expect_error("候选节点被挡在部署之前", response, "candidate_node_not_admitted")
        # 未准入的节点也不该拿到任何部署意图。
        delivered = candidate.pending_intents()
        self.report.check(
            "候选节点领不到部署意图",
            not delivered,
            json.dumps(delivered, ensure_ascii=False)[:160],
        )

    def scenario_metrics_aggregation(self) -> None:
        """可观测性：指标按 node/plugin/release/stage/reason 聚合，且不记录敏感内容。"""
        self.report.section("控制面 · 可观测性：指标聚合与敏感内容排除")
        # 阶段词表只有一处定义（API 模块），验收脚本直接引用，避免"两边各写一份必然漂移"。
        from sensoryplex_api.interfaces.plugin_deploy import METRIC_PHASES  # noqa: PLC0415

        path = "/admin/v1/plugin-deployments/metrics"
        anonymous = http_request(self.base_url + path)
        self.report.expect_error("未认证抓取指标被拒", anonymous, "authentication_required")
        if self.api_token:
            business = http_request(
                self.base_url + path, headers={"Authorization": f"Bearer {self.api_token}"}
            )
            self.report.expect_error(
                "业务凭据（无 plugins:manage）抓取指标被拒", business, "permission_denied"
            )
        else:
            self.report.note("环境未提供业务凭据，跳过「无 plugins:manage 被拒」这一项")

        response = self.admin.get(f"{path}?window_hours=720&node_id={VERIFY_NODE}")
        self.report.check(
            "管理会话可以抓取指标，且是 Prometheus exposition 文本",
            response.ok
            and "# TYPE sensoryplex_plugin_deployment_operations gauge" in response.raw
            and "sensoryplex_plugin_deployment_metrics_info{" in response.raw,
            f"status={response.status} bytes={len(response.raw)}",
        )
        series = parse_prometheus(response.raw)

        # 与台账逐桶对账：计数与每个阶段的 sum/max 都必须等于真实操作行算出来的值。
        items = self.admin.get(
            f"/admin/v1/plugin-deployments?node_id={VERIFY_NODE}&limit=100"
        ).payload.get("items", [])
        zeros = {f"{phase}_{kind}": 0 for phase in METRIC_PHASES for kind in ("sum", "max")}
        # 桶键 = (kind, stage, reason)：与指标的 label 维度逐字对齐，少一维就会把两类操作
        # 合并成一个桶而对账失败——这正是"不能两边各写一份词表"的原因。
        expected: dict[tuple[str, str, str], dict[str, int]] = {}
        for item in items:
            bucket = expected.setdefault(
                (item.get("kind") or "", item["stage"], item.get("error_code") or ""),
                {"operations": 0, **zeros},
            )
            bucket["operations"] += 1
            for phase in METRIC_PHASES:
                value = int(item.get(f"{phase}_ms") or 0)
                bucket[f"{phase}_sum"] += value
                bucket[f"{phase}_max"] = max(bucket[f"{phase}_max"], value)
        actual: dict[tuple[str, str, str], dict[str, int]] = {}
        for (name, labels), value in series.items():
            fields = dict(labels)
            key = (fields.get("kind", ""), fields.get("stage", ""), fields.get("reason", ""))
            if name == "sensoryplex_plugin_deployment_operations":
                actual.setdefault(key, {"operations": 0, **zeros})["operations"] = int(value)
            elif name.startswith("sensoryplex_plugin_deployment_phase_milliseconds_"):
                suffix = name.rsplit("_", 1)[1]
                actual.setdefault(key, {"operations": 0, **zeros})[
                    f"{fields.get('phase', '')}_{suffix}"
                ] = int(value)
        mismatches = [key for key, value in expected.items() if actual.get(key) != value]
        self.report.check(
            "指标桶（操作计数 + 各阶段 sum/max）与台账逐项一致",
            not mismatches and not [key for key in actual if key not in expected],
            f"buckets={len(expected)} mismatch={mismatches[:2]} "
            f"extra={[key for key in actual if key not in expected][:2]}",
        )
        kinds = sorted(
            {
                dict(labels).get("kind", "")
                for name, labels in series
                if name == "sensoryplex_plugin_deployment_operations"
            }
        )
        self.report.check(
            "操作类别（kind）进 label：升级 / 回滚与 provision 不混桶",
            "" not in kinds and len(kinds) >= 2,
            f"kinds={kinds}",
        )
        self.report.check(
            "operation_id 不进指标 label（低基数规则，逐操作耗时走操作资源）",
            "op_" not in response.raw,
            f"bytes={len(response.raw)}",
        )
        forbidden = (
            "sha256:",
            "wheelhouse",
            "batch_size",
            "venv/bin",
            "/Users/",
            "/tmp/",
            "password",
            "secret",
            "PRIVATE KEY",
        )
        leaked = [needle for needle in forbidden if needle in response.raw]
        self.report.check(
            "指标响应不含 bundle 内容 / 配置值 / 宿主路径 / 密钥",
            not leaked,
            f"leaked={leaked}",
        )
        self.report.eq(
            "info 指标如实回报本次抓取的窗口",
            series.get(("sensoryplex_plugin_deployment_metrics_info", (("window_hours", "720"),))),
            1.0,
        )
        recent = parse_prometheus(self.admin.get(f"{path}?window_hours=1").raw)
        self.report.check(
            "窗口过滤生效：1 小时窗口内本节点仍有 series",
            any(
                name == "sensoryplex_plugin_deployment_operations"
                and dict(labels).get("node_id") == VERIFY_NODE
                for name, labels in recent
            ),
            f"series={len(recent)}",
        )
        unknown = parse_prometheus(self.admin.get(f"{path}?node_id=no-such-node-{VERIFY_NODE}").raw)
        self.report.check(
            "node 过滤生效：未知节点没有任何操作 series",
            not any(name == "sensoryplex_plugin_deployment_operations" for name, _ in unknown),
            f"series={len(unknown)}",
        )

    # ── 准备与收尾 ─────────────────────────────────────────────────────────

    def prepare(self) -> None:
        # 幂等前置：验收节点上一轮可能留下槽位/运行实例/部署操作台账（例如进程被杀没走到
        # teardown），断言会踩在旧数据上。先把三个验收节点整体复位成"不存在"再自注册。
        for node_id in (VERIFY_NODE, SQUEEZED_NODE, CANDIDATE_NODE):
            self.reset_node(node_id)
        response = self.agent.bootstrap_local(
            "热部署验收节点（契约层，非物理节点）",
            {
                "platform": self.platform,
                "arch": self.arch,
                "cpu_cores": 8,
                "memory_bytes": 64 << 30,
                "unified_memory_bytes": 0,
                "accelerators": [],
                "supported_artifacts": ["local_native"],
                "labels": {"verify": "plugin_hot_deploy"},
            },
            self.api_token,
        )
        if not response.ok:
            raise SystemExit(
                f"验收节点自注册失败：{response.status} {response.reason}"
                "（容器内需要回环来源，或提供 --api-token）"
            )
        self.agent.heartbeat()

    def teardown(self) -> None:
        # 验收节点收尾：先吊销（留下审计痕迹），再真正删掉节点的热部署台账与槽位。
        for node_id in (VERIFY_NODE, SQUEEZED_NODE):
            self.admin.post(
                f"/admin/v1/nodes/{node_id}:revoke", {"reason": "hot_deploy_verify_done"}
            )
        for node_id in (VERIFY_NODE, SQUEEZED_NODE, CANDIDATE_NODE):
            deleted = self.admin.call("DELETE", f"/admin/v1/nodes/{node_id}")
            if deleted.status not in (200, 404):
                print(f"    · 验收节点 {node_id} 清理返回 {deleted.status} {deleted.reason}")
        shutil.rmtree(ROOT / VERIFY_FIXTURE_DIR, ignore_errors=True)
        shutil.rmtree(pathlib.Path("/tmp/sensoryplex-hot-deploy-check"), ignore_errors=True)

    def reset_node(self, node_id: str) -> None:
        """把一个验收节点复位到"不存在"：吊销 → 删除（连带它的热部署台账）。"""
        if not self.admin.get(f"/admin/v1/nodes/{node_id}").ok:
            return
        self.admin.post(f"/admin/v1/nodes/{node_id}:revoke", {"reason": "hot_deploy_verify_reset"})
        deleted = self.admin.call("DELETE", f"/admin/v1/nodes/{node_id}")
        if deleted.status not in (200, 404):
            raise SystemExit(f"验收节点 {node_id} 复位失败：{deleted.status} {deleted.reason}")

    # ── 入口 ───────────────────────────────────────────────────────────────

    def run(self) -> None:
        self.prepare()
        self.scenario_release_sync()
        state = self.scenario_deployment_lifecycle()
        if not state.get("operation_id"):
            return
        state["active_runtime_id"] = self.node_slot(state["instance_id"]).get(
            "active_runtime_instance_id", ""
        )
        state = self.scenario_upgrade_drain_and_cancel_window(state)
        state = self.scenario_rollback(state)
        self.scenario_cancel_pre_cutover(state)
        self.scenario_bundle_entitlement()
        self.scenario_headroom()
        self.scenario_node_admission()
        self.scenario_metrics_aggregation()


# ── 宿主执行器验收（原生） ─────────────────────────────────────────────────


class NativeScenarios:
    """真实执行器验收：真下载、真解包、真平台服务、真进程、真蓝绿。

    平台服务适配器（macOS 用户级 LaunchAgent / Linux systemd user unit）只能托管**本机**进程，
    所以这一层必须在宿主跑，而且必须用本机节点。验收用 `--startup-budget-s` 把"候选启动总时限"
    收紧（ADR-030 默认 300 秒），这样超时路径不用等满 5 分钟就能显式失败；代码路径完全一致。
    """

    def __init__(
        self,
        report: Report,
        admin: AdminApi,
        base_url: str,
        *,
        node_id: str,
        state_file: pathlib.Path,
        startup_budget_s: float,
        model_config: dict,
        fixture_dir: pathlib.Path,
        api_token: str = "",
        keep_fixtures: bool = False,
    ) -> None:
        self.report = report
        self.admin = admin
        self.base_url = base_url
        self.node_id = node_id
        self.state_file = pathlib.Path(state_file)
        self.fixture_dir = pathlib.Path(fixture_dir)
        self.model_config = model_config
        self.api_token = api_token
        self.keep_fixtures = keep_fixtures
        self.canary_config = {"batch_size": 1}
        state = json.loads(self.state_file.read_text())
        self.agent = AgentApi(base_url, node_id, state.get("session_token", ""))

        sys.path.insert(0, str(ROOT))
        from tools.node_agent_hot_deploy import HotDeployExecutor
        from tools.node_agent_platform import (  # noqa: PLC0415
            safe_extract_bundle,
            verify_bundle_bytes,
            verify_bundle_tree,
        )

        self._safe_extract_bundle = safe_extract_bundle
        self._verify_bundle_bytes = verify_bundle_bytes
        self._verify_bundle_tree = verify_bundle_tree
        self.base_dir = self.state_file.resolve().parent / "plugins"
        self.executor = HotDeployExecutor(
            self.agent, base_dir=self.base_dir, startup_budget_s=startup_budget_s
        )
        self.report.note(
            f"执行器：platform={self.executor.platform} arch={self.executor.arch} "
            f"supervisor={self.executor.supervisor.name} startup_budget_s={startup_budget_s}"
        )

    # ── 前置：节点必须是**本机**且平台与宿主真实能力一致 ───────────────────

    def prepare(self) -> None:
        """原生验收前置：节点在线、心跳新鲜，且平台/架构与本机真实能力一致。

        同机节点常被容器侧以 `platform=linux` 自纳管（容器里跑的底座认的是容器能力），
        而宿主首方 release 是 `macos-aarch64`。这里按**宿主**能力重新注册一次同机节点，
        走的就是 Agent 自己的 `bootstrap-local` 通路；顺带把会话令牌写回状态文件。
        """
        detail = self.admin.get(f"/admin/v1/nodes/{self.node_id}")
        if not detail.ok:
            raise SystemExit(
                f"节点 {self.node_id} 不存在：{detail.status} {detail.reason}；先跑 "
                f"`tools/node_agent.py enroll --local --node-id {self.node_id} "
                f"--state-file {self.state_file}`"
            )
        capabilities = detail.payload.get("capabilities") or {}
        current = (capabilities.get("platform", ""), capabilities.get("arch", ""))
        expected = (self.executor.platform, self.executor.arch)
        if current != expected:
            from tools.node_agent import probe_host_capabilities  # noqa: PLC0415

            self.report.note(
                f"节点 {self.node_id} 当前平台 {current[0]}-{current[1]} 与宿主 "
                f"{expected[0]}-{expected[1]} 不一致：按宿主真实能力重新注册"
            )
            response = self.agent.bootstrap_local(
                str(detail.payload.get("display_name") or "本机数据面"),
                probe_host_capabilities(),
                self.api_token,
            )
            if not response.ok:
                raise SystemExit(
                    f"按宿主能力重新注册 {self.node_id} 失败：{response.status} {response.reason}"
                    "（需要回环来源或 --api-token / SENSORYPLEX_API_TOKEN）"
                )
            self.agent.token = str(response.payload.get("session_token", ""))
            state = json.loads(self.state_file.read_text())
            state["session_token"] = self.agent.token
            self.state_file.write_text(json.dumps(state, indent=2) + "\n")
        else:
            self.report.note(f"节点 {self.node_id} 平台与宿主一致：{expected[0]}-{expected[1]}")
        # 部署预检会拒绝心跳超过 60 秒的节点，正式开始前先把心跳刷到新鲜。
        self.agent.heartbeat()

    # ── 通用 ──────────────────────────────────────────────────────────────

    def release(self, plugin_id: str) -> dict:
        response = self.admin.get(f"/admin/v1/plugin-releases?plugin_id={plugin_id}")
        node = self.admin.get(f"/admin/v1/nodes/{self.node_id}").payload
        capabilities = node.get("capabilities", {})
        items = [
            item
            for item in response.payload.get("items", [])
            if item["platform"] == capabilities.get("platform")
            and item["arch"] == capabilities.get("arch")
        ]
        if not items:
            raise SystemExit(
                f"节点 {self.node_id} 的平台是 "
                f"{capabilities.get('platform')}-{capabilities.get('arch')}，"
                f"但制品仓里没有 {plugin_id} 的对应 release"
            )
        return items[0]

    def refresh_observations(self) -> list[dict]:
        """真实 Agent 每次心跳都带平台服务实际状态；验收也必须走同一条路径。"""
        observed = self.executor.observations()
        self.agent.observed = observed
        return observed

    def operation(self, operation_id: str) -> dict:
        return self.admin.get(f"/admin/v1/plugin-deployments/{operation_id}").payload

    def wait_settled(self, operation_id: str, timeout_s: float = 180.0) -> dict:
        deadline = time.monotonic() + timeout_s
        payload: dict = {}
        while time.monotonic() < deadline:
            payload = self.operation(operation_id)
            if payload.get("stage") in (
                "PLUGIN_OPERATION_STAGE_SUCCEEDED",
                "PLUGIN_OPERATION_STAGE_FAILED",
                "PLUGIN_OPERATION_STAGE_CANCELLED",
            ):
                return payload
            time.sleep(POLL_INTERVAL_S)
        return payload

    def slot(self, plugin_id: str) -> dict:
        detail = self.admin.get(f"/admin/v1/nodes/{self.node_id}").payload
        for instance in detail.get("instances", []) or []:
            if instance.get("plugin_id") == plugin_id:
                return instance
        return {}

    def write_mode(self, plugin_id: str, runtime_instance_id: str, mode: str) -> None:
        run_dir = self.executor.runtime_dir(plugin_id, runtime_instance_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "mode.json").write_text(json.dumps({"mode": mode}) + "\n")

    def deploy(
        self,
        kind: str,
        plugin_id: str,
        release: dict,
        config: dict,
        *,
        mode: str = "",
        timeout_s: float = 180.0,
    ) -> dict:
        """真实走一遍：创建操作 → 领意图 → 执行器真装真起 → 排空旧实例 → 等终态。"""
        # 先刷观测再送心跳：部署预检会拒绝心跳超过 60 秒的节点，而观测必须与心跳同批上报。
        self.refresh_observations()
        self.agent.heartbeat()
        created = self.admin.post(
            f"/admin/v1/nodes/{self.node_id}/plugins/{plugin_id}:{kind}",
            {"release_id": release["release_id"], "config": config},
        )
        if created.status != 201:
            return {"created": created, "operation": {}, "final": {}, "reason": created.reason}
        operation = created.payload
        candidate = operation["candidate_runtime_instance_id"]
        if mode:
            self.write_mode(plugin_id, candidate, mode)
        intent = self.agent.wait_for_intent(
            "DEPLOYMENT_ACTION_STAGE_RELEASE", runtime_instance_id=candidate, timeout_s=30.0
        )
        if intent is None:
            return {
                "created": created,
                "operation": operation,
                "final": self.operation(operation["operation_id"]),
                "reason": "intent_not_delivered",
            }
        staged = self.executor.stage_release(intent)
        previous = operation.get("from_runtime_instance_id") or ""
        # 候选失败时不会有排空意图（旧 active 保持不动），别白等满轮询超时。
        if previous and staged:
            drain = self.agent.wait_for_intent(
                "DEPLOYMENT_ACTION_DRAIN", runtime_instance_id=previous, timeout_s=30.0
            )
            if drain:
                self.executor.drain(drain)
        return {
            "created": created,
            "operation": operation,
            "final": self.wait_settled(operation["operation_id"], timeout_s=timeout_s),
            "reason": "",
        }

    def entry_kind(self, plugin_id: str) -> str:
        """首次部署走哪条操作：槽位为空 → `provision`，槽位已有 active → 蓝绿 `upgrade`。"""
        slot = self.slot(plugin_id)
        return "upgrade" if slot.get("active_runtime_instance_id") else "provision"

    def deploy_entry(
        self, plugin_id: str, release: dict, config: dict, *, mode: str = ""
    ) -> tuple[str, dict]:
        """可重复的"首次部署"：按槽位现状选 `provision` / `upgrade`，并如实注明选了哪条。"""
        kind = self.entry_kind(plugin_id)
        if kind == "upgrade":
            self.report.note(f"{plugin_id} 槽位已有 active：首次部署改走蓝绿升级，而不是 provision")
        return kind, self.deploy(kind, plugin_id, release, config, mode=mode)

    # ── 场景 1：受控 bundle 拒收 ───────────────────────────────────────────

    def scenario_bundle_rejections(self) -> None:
        source = self._canary_bundle()
        digest = self._canary_release()["bundle_digest"]
        # 每一条都要打在不同的一层上：`tampered_bytes` 只动字节、必须按**原**摘要被拒；
        # 其余几条是"重新打包成结构上有问题的包"，字节摘要按新落盘内容自证，
        # 否则全部会在第一层就退化成 `release_bundle_digest_mismatch`，把深层检查盖掉。
        checks = (
            ("tampered_bytes", self._mutate_bytes, "release_bundle_digest_mismatch", False),
            ("content_flip", self._mutate_flip, "release_bundle_content_digest_mismatch", True),
            ("content_append", self._mutate_append, "release_bundle_size_mismatch", True),
            ("extra_member", self._mutate_extra, "release_bundle_member_mismatch", True),
            ("symlink_member", self._mutate_symlink, "release_bundle_unsafe_member", True),
            ("dotdot_member", self._mutate_dotdot, "release_bundle_path_escape", True),
            ("absolute_member", self._mutate_absolute, "release_bundle_path_escape", True),
            ("sbom_rewrite", self._mutate_sbom, "release_bundle_sbom_digest_mismatch", True),
        )
        for name, mutate, expected, resign in checks:
            bundle = self.fixture_dir / f"{name}.tar.gz"
            mutate(source, bundle)
            actual = self._bundle_error(bundle, self._digest(bundle) if resign else digest)
            self.report.check(
                f"拒收 {name}", actual == expected, f"actual={actual!r} expected={expected!r}"
            )
        self.report.check(
            "合法 bundle 通过复算（对照组）",
            self._bundle_error(source, digest) == "",
            self._bundle_error(source, digest),
        )

    @staticmethod
    def _digest(path: pathlib.Path) -> str:
        """与执行器同口径的落盘字节摘要（`sha256:<hex>`）。"""
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def _canary_release(self) -> dict:
        if not getattr(self, "_canary_release_cache", None):
            self._canary_release_cache = self.release(CANARY_PLUGIN)
        return self._canary_release_cache

    def _canary_bundle(self) -> pathlib.Path:
        descriptor = self._canary_release()
        path = (
            ROOT
            / ".data/releases"
            / CANARY_PLUGIN
            / descriptor["plugin_version"]
            / f"{descriptor['platform']}-{descriptor['arch']}"
            / pathlib.Path(descriptor.get("bundle_path", "bundle.tar.gz")).name
        )
        return path

    def _bundle_error(self, bundle: pathlib.Path, digest: str) -> str:
        """复算一族"落盘字节 → 成员形状 → 逐文件摘要"，返回稳定错误码（无错返回空串）。"""
        try:
            self._verify_bundle_bytes(bundle, digest)
        except Exception as error:  # noqa: BLE001
            return getattr(error, "code", "unexpected")
        target = self.fixture_dir / f"extract-{bundle.stem}"
        shutil.rmtree(target, ignore_errors=True)
        try:
            manifest = self._safe_extract_bundle(bundle, target)
            self._verify_bundle_tree(target, manifest)
        except Exception as error:  # noqa: BLE001
            return getattr(error, "code", "unexpected")
        return ""

    def _expand(self, source: pathlib.Path, destination: pathlib.Path) -> None:
        shutil.rmtree(destination, ignore_errors=True)
        destination.mkdir(parents=True, exist_ok=True)
        with tarfile.open(source, "r:gz") as archive:
            archive.extractall(destination, filter="data")

    def _repack(self, source: pathlib.Path, destination: pathlib.Path, extra=None) -> None:
        members: list[tuple[tarfile.TarInfo, pathlib.Path | None]] = []
        with tarfile.open(source, "r:gz") as archive:
            for member in archive.getmembers():
                path = None
                if member.isfile():
                    handle = archive.extractfile(member)
                    path = self.fixture_dir / "staged" / member.name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(handle.read() if handle else b"")
                members.append((member, path))
        pending = list(members) + list(extra or [])
        with tarfile.open(destination, "w:gz") as archive:
            for member, path in pending:
                if path is None:
                    archive.addfile(member)
                else:
                    archive.addfile(member, path.open("rb"))

    def _mutate_bytes(self, source: pathlib.Path, destination: pathlib.Path) -> None:
        data = bytearray(source.read_bytes())
        data[len(data) // 2] ^= 0xFF
        destination.write_bytes(bytes(data))

    def _mutate_flip(self, source: pathlib.Path, destination: pathlib.Path) -> None:
        self._stage_and_repack(
            source, destination, lambda staged: self._flip(staged / "payload/plugin.yaml")
        )

    def _mutate_append(self, source: pathlib.Path, destination: pathlib.Path) -> None:
        def mutate(staged: pathlib.Path) -> None:
            target = staged / "payload/plugin.yaml"
            target.write_bytes(target.read_bytes() + b"# tampered\n")

        self._stage_and_repack(source, destination, mutate)

    def _mutate_extra(self, source: pathlib.Path, destination: pathlib.Path) -> None:
        def mutate(staged: pathlib.Path) -> None:
            (staged / "payload/extra.txt").write_text("extra\n")

        self._stage_and_repack(source, destination, mutate)

    def _mutate_sbom(self, source: pathlib.Path, destination: pathlib.Path) -> None:
        """改 SBOM 内容并同步更新 manifest.files 里的条目，但不更新 manifest_digest/sbom_digest。"""

        def mutate(staged: pathlib.Path) -> None:
            sbom = staged / "payload/sbom.cdx.json"
            document = json.loads(sbom.read_text())
            document["metadata"] = {"tampered": True}
            sbom.write_text(json.dumps(document, indent=2) + "\n")
            raw = sbom.read_bytes()
            manifest_path = staged / "bundle.manifest.json"
            manifest = json.loads(manifest_path.read_text())
            for entry in manifest["files"]:
                if entry["path"] == "payload/sbom.cdx.json":
                    entry["bytes"] = len(raw)
                    entry["sha256"] = hashlib.sha256(raw).hexdigest()
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

        self._stage_and_repack(source, destination, mutate)

    def _mutate_symlink(self, source: pathlib.Path, destination: pathlib.Path) -> None:
        info = tarfile.TarInfo("payload/link.txt")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        info.mode = 0o777
        self._repack(source, destination, [(info, None)])

    def _mutate_dotdot(self, source: pathlib.Path, destination: pathlib.Path) -> None:
        payload = b"escape\n"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(payload)
        path = self.fixture_dir / "staged" / "escape.txt"
        path.write_bytes(payload)
        self._repack(source, destination, [(info, path)])

    def _mutate_absolute(self, source: pathlib.Path, destination: pathlib.Path) -> None:
        payload = b"escape\n"
        info = tarfile.TarInfo("/tmp/sensoryplex-escape.txt")
        info.size = len(payload)
        path = self.fixture_dir / "staged" / "escape-abs.txt"
        path.write_bytes(payload)
        self._repack(source, destination, [(info, path)])

    def _flip(self, path: pathlib.Path) -> None:
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0x01
        path.write_bytes(bytes(data))

    def _stage_and_repack(self, source: pathlib.Path, destination: pathlib.Path, mutate) -> None:
        staged = self.fixture_dir / "staged"
        self._expand(source, staged)
        mutate(staged)
        staged_members: list[tuple[tarfile.TarInfo, pathlib.Path | None]] = []
        for path in sorted(staged.rglob("*")):
            relative = path.relative_to(staged).as_posix()
            info = tarfile.TarInfo(relative)
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                staged_members.append((info, None))
            else:
                info.size = path.stat().st_size
                info.mode = 0o755 if path.stat().st_mode & 0o100 else 0o644
                staged_members.append((info, path))
        with tarfile.open(destination, "w:gz") as archive:
            for info, path in staged_members:
                if path is None:
                    archive.addfile(info)
                else:
                    with path.open("rb") as handle:
                        archive.addfile(info, handle)

    # ── 场景 2：deploy-canary 状态机矩阵 ───────────────────────────────────

    def scenario_canary_matrix(self) -> dict:
        release = self._canary_release()
        kind, first = self.deploy_entry(CANARY_PLUGIN, release, self.canary_config)
        final = first.get("final") or {}
        self.report.check(
            f"canary 首次部署（{kind}）成功并切到 succeeded",
            final.get("stage") == "PLUGIN_OPERATION_STAGE_SUCCEEDED",
            f"stage={final.get('stage')} error={final.get('error_code')} "
            f"reason={first.get('reason')}",
        )
        slot = self.slot(CANARY_PLUGIN)
        active_before = slot.get("active_runtime_instance_id", "")
        candidate = final.get("candidate") or {}
        self.report.check(
            "候选进程真的起了：endpoint 是 loopback 且非 0 端口",
            _is_loopback_endpoint(candidate.get("endpoint", "")),
            str(candidate.get("endpoint")),
        )
        self.report.eq(
            "候选自证身份被如实落库",
            (candidate.get("verified_plugin_id"), candidate.get("verified_artifact_digest")),
            (CANARY_PLUGIN, release["artifact_digest"]),
        )
        self.report.check(
            "staging/starting/validating 耗时都被记录",
            int(final.get("staging_ms", 0)) > 0
            and int(final.get("starting_ms", 0)) > 0
            and int(final.get("validating_ms", 0)) > 0,
            f"staging={final.get('staging_ms')} starting={final.get('starting_ms')} "
            f"validating={final.get('validating_ms')}",
        )
        observed = self.refresh_observations()
        matched = [item for item in observed if item["reconciliation"] == "matched"]
        self.report.check(
            "Agent 观测与平台服务一致（reconciliation=matched）",
            bool(matched),
            json.dumps(observed, ensure_ascii=False)[:200],
        )
        if not active_before:
            self.report.check("canary 成为槽位 active", False, "槽位没有 active 指针")
            return {}

        failures = (
            ("wrong_digest", "candidate_artifact_digest_mismatch"),
            ("wrong_identity", "candidate_plugin_identity_mismatch"),
            ("invalid_config", "candidate_config_invalid"),
            ("start_fail", "candidate_start_failed"),
            ("health_not_ready", "candidate_start_timeout"),
            ("no_endpoint", "plugin_endpoint_file_timeout"),
            ("endpoint_garbage", "plugin_endpoint_file_invalid"),
        )
        for mode, expected_code in failures:
            result = self.deploy("upgrade", CANARY_PLUGIN, release, self.canary_config, mode=mode)
            final = result.get("final") or {}
            self.report.check(
                f"{mode} → 操作 failed 且错误码 {expected_code}",
                final.get("stage") == "PLUGIN_OPERATION_STAGE_FAILED"
                and final.get("error_code") == expected_code,
                f"stage={final.get('stage')} error_code={final.get('error_code')!r} "
                f"reason={result.get('reason')}",
            )
            slot_after = self.slot(CANARY_PLUGIN)
            self.report.check(
                f"{mode} → 切换前失败，旧 active 指针不变",
                slot_after.get("active_runtime_instance_id") == active_before,
                f"active={slot_after.get('active_runtime_instance_id')} expected={active_before}",
            )
            unit = (final.get("candidate") or {}).get("unit_name") or ""
            if unit:
                self.report.check(
                    f"{mode} → 失败候选已被卸载（不留悬挂进程）",
                    not self.executor.supervisor.state(unit).running,
                    f"unit={unit}",
                )

        hung = self.deploy("upgrade", CANARY_PLUGIN, release, self.canary_config, mode="hung_drain")
        final = hung.get("final") or {}
        self.report.check(
            "hung_drain → 排空 RPC 挂死时仍然在受限 grace 后强制卸载并存下排空耗时",
            final.get("stage") == "PLUGIN_OPERATION_STAGE_SUCCEEDED"
            and int(final.get("draining_ms", 0)) >= int(release["default_deadline_ms"]),
            f"stage={final.get('stage')} draining_ms={final.get('draining_ms')} "
            f"declared={release['default_deadline_ms']}",
        )
        return {"active_runtime_id": self.slot(CANARY_PLUGIN).get("active_runtime_instance_id", "")}

    # ── 场景 3：真实首方插件蓝绿 ───────────────────────────────────────────

    def scenario_real_plugin(self) -> dict:
        release = self.release(REAL_PLUGIN)
        self.report.note(
            f"真实插件 {REAL_PLUGIN}@{release['plugin_version']} release={release['release_id']} "
            f"declared_memory={release['declared_memory_bytes']}B"
        )
        kind, first = self.deploy_entry(REAL_PLUGIN, release, self.model_config)
        final = first.get("final") or {}
        self.report.check(
            f"真实插件首次部署（{kind}）→ Describe/ValidateConfig/Start/Health 全绿并切换",
            final.get("stage") == "PLUGIN_OPERATION_STAGE_SUCCEEDED",
            f"stage={final.get('stage')} error={final.get('error_code')} "
            f"detail={final.get('error_detail')!r} reason={first.get('reason')}",
        )
        candidate = final.get("candidate") or {}
        endpoint = candidate.get("endpoint", "")
        self.report.check(
            "真实插件候选 endpoint 是 loopback 且非 0 端口",
            _is_loopback_endpoint(endpoint),
            str(endpoint),
        )
        self.report.eq(
            "真实插件自证身份与摘要一致",
            (candidate.get("verified_plugin_id"), candidate.get("verified_artifact_digest")),
            (REAL_PLUGIN, release["artifact_digest"]),
        )
        self.report.check(
            "真实插件的 staging/starting/validating 耗时都被记录",
            int(final.get("staging_ms", 0)) > 0
            and int(final.get("starting_ms", 0)) > 0
            and int(final.get("validating_ms", 0)) > 0,
            f"staging={final.get('staging_ms')} starting={final.get('starting_ms')} "
            f"validating={final.get('validating_ms')}",
        )
        self.report.note(
            f"真实插件耗时：staging={final.get('staging_ms')}ms "
            f"starting={final.get('starting_ms')}ms validating={final.get('validating_ms')}ms"
        )
        self.assert_live_endpoint(endpoint, REAL_PLUGIN)
        active_first = candidate.get("runtime_instance_id", "")

        upgraded = self.deploy("upgrade", REAL_PLUGIN, release, self.model_config)
        up_final = upgraded.get("final") or {}
        self.report.check(
            "同插件升级（新 generation）→ 蓝绿切换并排空旧实例",
            up_final.get("stage") == "PLUGIN_OPERATION_STAGE_SUCCEEDED",
            f"stage={up_final.get('stage')} error={up_final.get('error_code')}",
        )
        self.report.check(
            "排空耗时 > 0（真的等过 grace 再卸载）",
            int(up_final.get("draining_ms", 0)) > 0,
            str(up_final.get("draining_ms")),
        )
        previous = up_final.get("previous") or {}
        self.report.eq(
            "被替换的实例记为 previous 且已 stopped",
            previous.get("state"),
            "PLUGIN_RUNTIME_STATE_STOPPED",
        )
        self.report.eq(
            "previous 就是切换前的 active", previous.get("runtime_instance_id"), active_first
        )
        self.report.check(
            "旧实例的制品仍留在本机（可回滚，不删制品）",
            bool(previous.get("runtime_instance_id"))
            and (self.base_dir / REAL_PLUGIN / "releases").is_dir(),
            str(self.base_dir / REAL_PLUGIN / "releases"),
        )
        active_second = (up_final.get("candidate") or {}).get("runtime_instance_id", "")

        rollback = self.admin.post(
            f"/admin/v1/plugin-deployments/{up_final['operation_id']}:rollback"
        )
        self.report.check(
            "回滚创建返回 201", rollback.status == 201, f"{rollback.status} {rollback.reason}"
        )
        if rollback.status == 201:
            rollback_operation = rollback.payload
            self.refresh_observations()
            intent = self.agent.wait_for_intent(
                "DEPLOYMENT_ACTION_STAGE_RELEASE",
                runtime_instance_id=rollback_operation["candidate_runtime_instance_id"],
                timeout_s=30.0,
            )
            if intent:
                self.executor.stage_release(intent)
                drain = self.agent.wait_for_intent(
                    "DEPLOYMENT_ACTION_DRAIN", runtime_instance_id=active_second, timeout_s=30.0
                )
                if drain:
                    self.executor.drain(drain)
            rb_final = self.wait_settled(rollback_operation["operation_id"])
            self.report.check(
                "显式回滚走反向操作并成功",
                rb_final.get("stage") == "PLUGIN_OPERATION_STAGE_SUCCEEDED"
                and rb_final.get("kind") == "rollback",
                f"stage={rb_final.get('stage')} kind={rb_final.get('kind')} "
                f"error={rb_final.get('error_code')}",
            )
            self.report.eq(
                "回滚后 active 指向回滚候选",
                self.slot(REAL_PLUGIN).get("active_runtime_instance_id"),
                rollback_operation["candidate_runtime_instance_id"],
            )
            self.assert_live_endpoint(
                (rb_final.get("candidate") or {}).get("endpoint", ""), REAL_PLUGIN
            )
        return {"active_runtime_id": self.slot(REAL_PLUGIN).get("active_runtime_instance_id", "")}

    def assert_live_endpoint(self, endpoint: str, plugin_id: str) -> None:
        """直接对**真在跑的进程**再打一次 Describe/Health：候选验收不是控制面的自说自话。"""
        if not _is_loopback_endpoint(endpoint):
            self.report.check("直连真实进程确认真身", False, f"endpoint={endpoint!r}")
            return
        from tools.node_agent_hot_deploy import PluginChannel

        try:
            with PluginChannel(endpoint, timeout_s=10.0) as channel:
                description = channel.describe()
                health = channel.health()
            self.report.check(
                "直连真实进程：Describe 身份一致、Health=ready",
                description.name == plugin_id and health.state == "ready",
                f"name={description.name} state={health.state}",
            )
        except Exception as error:  # noqa: BLE001
            self.report.check("直连真实进程：Describe/Health", False, repr(error)[:160])

    # ── 场景 4：Agent 重启对账与安全卸载 ───────────────────────────────────

    def scenario_reconciliation_and_unload(self) -> None:
        active = self.slot(CANARY_PLUGIN)
        unit = ""
        for runtime_instance_id, entry in sorted(self.executor.registry().items()):
            if entry.get("plugin_id") == CANARY_PLUGIN and runtime_instance_id == active.get(
                "active_runtime_instance_id"
            ):
                unit = entry["unit_name"]
        if not unit:
            self.report.check("找到 canary active 的 unit", False, str(active))
            return

        # 模拟"Agent 重启后发现托管单元被外部干掉了"：实例行还是 active，平台服务已经没了。
        self.executor.supervisor.stop(unit)
        observed = self.refresh_observations()
        mismatched = [item for item in observed if item["reconciliation"] != "matched"]
        self.report.check(
            "平台服务消失时观测如实报 unknown（不猜成功）",
            bool(mismatched),
            json.dumps(mismatched, ensure_ascii=False)[:200],
        )
        self.agent.heartbeat()
        self.report.check(
            "控制面回传 reconciliation_required",
            active.get("active_runtime_instance_id") in self.agent.reconciliation_required,
            f"required={self.agent.reconciliation_required}",
        )
        untrusted = self.operation(active.get("active_runtime_instance_id", ""))
        _ = untrusted  # active_runtime_instance_id 不是操作号；下面用槽位状态判断
        slot = self.slot(CANARY_PLUGIN)
        self.report.check(
            "对账只记录事实：active 指针与角色都不被改写",
            slot.get("active_runtime_instance_id") == active.get("active_runtime_instance_id"),
            f"active={slot.get('active_runtime_instance_id')}",
        )

        releases_root = self.base_dir / CANARY_PLUGIN / "releases"
        installed = (
            sorted(path.name for path in releases_root.iterdir()) if releases_root.is_dir() else []
        )
        unloaded = self.executor.unload_all()
        self.report.check(
            "安全卸载停掉全部托管单元",
            bool(unloaded["stopped"]) and not unloaded["failed"],
            json.dumps(unloaded),
        )
        remaining = (
            [path.name for path in releases_root.iterdir()] if releases_root.is_dir() else []
        )
        self.report.check(
            "安全卸载不删 active/previous 制品",
            installed and remaining and set(installed) <= set(remaining),
            f"installed={installed} remaining={remaining}",
        )
        after = self.refresh_observations()
        self.report.check(
            "卸载后观测全部变为 stopped/unknown（不再宣称 matched）",
            all(
                item["reconciliation"] != "matched" or item["observed_state"] == "stopped"
                for item in after
            ),
            json.dumps(after, ensure_ascii=False)[:200],
        )

    # ── 入口 ───────────────────────────────────────────────────────────────

    def run(self) -> None:
        self.prepare()
        shutil.rmtree(self.fixture_dir, ignore_errors=True)
        self.fixture_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.report.section("宿主执行器 · 受控 bundle 拒收（篡改 / 摘要 / 路径逃逸）")
            self.scenario_bundle_rejections()
            self.report.section("宿主执行器 · deploy-canary 状态机矩阵（真实平台服务）")
            self.scenario_canary_matrix()
            self.report.section("宿主执行器 · 真实首方插件蓝绿验收")
            self.scenario_real_plugin()
            self.report.section("宿主执行器 · Agent 重启对账与安全卸载")
            self.scenario_reconciliation_and_unload()
        finally:
            if self.keep_fixtures:
                self.report.note(f"保留 fixture 现场：{self.fixture_dir}")
            else:
                shutil.rmtree(self.fixture_dir, ignore_errors=True)


def _is_loopback_endpoint(endpoint: str) -> bool:
    host, _, port = str(endpoint).rpartition(":")
    return host in {"127.0.0.1", "localhost"} and port.isdigit() and int(port) != 0


# ── 命令行入口 ─────────────────────────────────────────────────────────────

# 验收默认把"候选启动总时限"收紧到 60 秒（ADR-030 的生产默认是 300 秒），
# 这样 `health_not_ready` / `no_endpoint` 这类超时路径不必等满 5 分钟才显式失败；
# 执行器的超时判定代码路径完全一致，只是同一个参数取不同的值。
DEFAULT_STARTUP_BUDGET_S = 60.0


def _host_platform_arch() -> tuple[str, str]:
    """宿主真实平台/架构，与 `tools/node_agent.py` 的能力探测保持同一映射口径。"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    target_platform = {"darwin": "macos"}.get(system, system)
    target_arch = {"arm64": "aarch64"}.get(machine, machine)
    return target_platform, target_arch


def _repository_platform_arch() -> tuple[str, str] | None:
    """制品仓里已经构建好的平台/架构族（只有一个时作为默认口径）。

    API 契约层大多在容器里跑，容器自己的平台（linux）与"在宿主机上构建的首方 release"
    （macos-aarch64）常常不是同一族；默认取制品仓可以避免"在容器里必须手写 --platform"。
    有多个族时不做猜测，退回宿主探测并要求显式 `--platform/--arch`。
    """
    root = ROOT / ".data/releases" / CANARY_PLUGIN
    found: set[tuple[str, str]] = set()
    if root.is_dir():
        for version_dir in sorted(root.iterdir()):
            if not version_dir.is_dir():
                continue
            for target in sorted(version_dir.iterdir()):
                if target.is_dir() and (target / "release.json").is_file():
                    target_platform, _, target_arch = target.name.rpartition("-")
                    if target_platform and target_arch:
                        found.add((target_platform, target_arch))
    return found.pop() if len(found) == 1 else None


def _resolve_credentials(args, env_file: dict[str, str]) -> tuple[str, str]:
    """演示账号：显式参数 > `.env` > `.data/demo-password`（`make demo-seed` 生成的）。"""
    username = args.user or env_file.get("SENSORYPLEX_DEMO_USERNAME", "") or "demo"
    password = args.password or env_file.get("SENSORYPLEX_DEMO_PASSWORD", "")
    if not password or password.startswith("REPLACE_WITH"):
        secret = ROOT / ".data/demo-password"
        if secret.is_file():
            password = secret.read_text().strip()
    return username, password


def _resolve_model_config(raw: str) -> dict:
    """真实首方插件（embed-bge-onnx）的候选配置；默认指向仓库内已固化的权重。"""
    if raw:
        return json.loads(raw)
    return {
        "model_dir": str(ROOT / ".data/models/bge-small-zh-v1.5"),
        "model_file": "onnx/model_quantized.onnx",
        "provider": "cpu",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ADR-030 插件热部署执行器验收（api=控制面契约层，native=宿主真实执行器）"
    )
    parser.add_argument(
        "--scope",
        choices=("api", "native", "all"),
        default="api",
        help="api 在容器内跑契约层；native 在宿主跑真实执行器；all 串起来（须在宿主）",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8091")
    parser.add_argument("--user", default="", help="管理账号；默认取 demo 账号")
    parser.add_argument("--password", default="", help="管理账号密码；默认取 .data/demo-password")
    parser.add_argument(
        "--api-token", default="", help="本机自注册凭据；默认取 SENSORYPLEX_API_TOKEN"
    )
    parser.add_argument("--node-id", default="local-host", help="native 验收使用的本机节点")
    parser.add_argument("--state-file", default=".data/agent/local-host.json")
    parser.add_argument("--platform", default="", help="制品族平台；默认取制品仓/宿主探测")
    parser.add_argument("--arch", default="", help="制品族架构；默认取制品仓/宿主探测")
    parser.add_argument(
        "--startup-budget-s",
        type=float,
        default=DEFAULT_STARTUP_BUDGET_S,
        help="候选启动总时限（秒）；生产默认 300，验收默认收紧以便超时路径可验",
    )
    parser.add_argument("--fixture-dir", default="/tmp/sensoryplex-hot-deploy-verify")
    parser.add_argument(
        "--model-config", default="", help="真实插件候选配置 JSON；默认指向仓库权重"
    )
    parser.add_argument(
        "--keep-fixtures", action="store_true", help="保留篡改 fixture 现场便于排查"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    env_file = read_env_file(ROOT / ".env")
    api_token = (
        args.api_token
        or os.environ.get("SENSORYPLEX_API_TOKEN", "")
        or env_file.get("SENSORYPLEX_API_TOKEN", "")
    )
    host_platform, host_arch = _host_platform_arch()
    repository_target = _repository_platform_arch()
    default_platform, default_arch = repository_target or (host_platform, host_arch)
    target_platform = args.platform or default_platform
    target_arch = args.arch or default_arch

    print("=" * 78)
    print("ADR-030 插件热部署执行器验收")
    print(
        f"  scope={args.scope} base_url={args.base_url} "
        f"制品族={target_platform}-{target_arch} 宿主={host_platform}-{host_arch} "
        f"startup_budget_s={args.startup_budget_s}"
    )
    print("=" * 78)

    report = Report()
    if repository_target:
        report.note(
            f"制品族默认取制品仓：{repository_target[0]}-{repository_target[1]}"
            "（可用 --platform/--arch 覆盖）"
        )

    username, password = _resolve_credentials(args, env_file)
    if not password:
        print("没有可用的管理账号密码：先跑 `make demo-seed`，或用 --user/--password 指定。")
        return 1
    admin = AdminApi(args.base_url, username, password)
    login = admin.login()
    if not login.ok:
        print(f"登录失败：{login.status} {login.reason}")
        return 1
    report.note(f"管理会话已建立：{admin.principal or username}")

    if args.scope in ("api", "all"):
        scenarios = ApiScenarios(
            report,
            admin,
            args.base_url,
            platform=target_platform,
            arch=target_arch,
            api_token=api_token,
            canary_config={"batch_size": 1},
        )
        try:
            scenarios.run()
        finally:
            scenarios.teardown()

    if args.scope in ("native", "all"):
        if (host_platform, host_arch) != (target_platform, target_arch):
            print(
                f"原生执行器只能托管**本机同平台**制品：宿主是 {host_platform}-{host_arch}，"
                f"制品族是 {target_platform}-{target_arch}。"
                "只跑契约层请用 --scope api；要用 --scope native 请在对应平台节点上执行。"
            )
            return 1
        state_file = pathlib.Path(args.state_file)
        if not state_file.is_absolute():
            state_file = ROOT / state_file
        if not state_file.is_file():
            raise SystemExit(
                f"缺少节点状态文件 {state_file}：先跑 `tools/node_agent.py enroll --local "
                f"--node-id {args.node_id} --state-file {args.state_file}`"
            )
        NativeScenarios(
            report,
            admin,
            args.base_url,
            node_id=args.node_id,
            state_file=state_file,
            startup_budget_s=args.startup_budget_s,
            model_config=_resolve_model_config(args.model_config),
            fixture_dir=pathlib.Path(args.fixture_dir),
            api_token=api_token,
            keep_fixtures=args.keep_fixtures,
        ).run()

    return report.finish()


if __name__ == "__main__":
    raise SystemExit(main())
