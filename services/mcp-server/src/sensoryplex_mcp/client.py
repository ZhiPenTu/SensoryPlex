"""SensoryPlex REST API Client for MCP Server.

Provides typed, authenticated access to SensoryPlex core API endpoints:
- Multimodal material search and observation detail (OCR, ASR, VLM)
- Timeline coverage and video playback stream mapping
- Video asset inventory and processing job dispatch
- Cluster node topology, hardware accelerators, and plugin catalog
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

LOGGER = logging.getLogger("sensoryplex.mcp.client")


class SensoryPlexAPIError(Exception):
    """Base exception for SensoryPlex API errors."""

    def __init__(self, status_code: int, reason_code: str, detail: str = ""):
        self.status_code = status_code
        self.reason_code = reason_code
        self.detail = detail or reason_code
        super().__init__(f"[{status_code}] {self.reason_code}: {self.detail}")


class SensoryPlexAuthError(SensoryPlexAPIError):
    """Raised when authentication or authorization fails."""


class SensoryPlexNotFoundError(SensoryPlexAPIError):
    """Raised when requested resource does not exist."""


class SensoryPlexClient:
    """Asynchronous HTTP client for the SensoryPlex API."""

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("SENSORYPLEX_BASE_URL", "http://127.0.0.1:8091")
        ).rstrip("/")
        self.api_token = api_token or os.getenv("SENSORYPLEX_API_TOKEN")
        self.username = username or os.getenv("SENSORYPLEX_USERNAME")
        self.password = password or os.getenv("SENSORYPLEX_PASSWORD")
        self.timeout = timeout
        self._session_cookies: dict[str, str] = {}
        self._csrf_token: str = ""
        self._demo_attempted: bool = False
        self._http_client: httpx.AsyncClient | None = None

    async def get_client(self) -> httpx.AsyncClient:
        """Returns or creates the underlying httpx.AsyncClient."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._http_client

    async def close(self) -> None:
        """Close the underlying HTTP client session."""
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    def _build_headers(self, custom_headers: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        if self._csrf_token:
            headers["x-csrf-token"] = self._csrf_token
        if custom_headers:
            headers.update(custom_headers)
        return headers

    async def _try_demo_login(self, client: httpx.AsyncClient) -> bool:
        """Try discovering and logging in via the local demo account if available."""
        if self._demo_attempted:
            return False
        self._demo_attempted = True
        try:
            resp = await client.get("/auth/v1/demo-account")
            if resp.status_code == 200:
                data = resp.json()
                if data.get("enabled") and data.get("username") and data.get("password"):
                    login_resp = await client.post(
                        "/auth/v1/session",
                        json={
                            "username": data["username"],
                            "password": data["password"],
                        },
                    )
                    if login_resp.status_code == 200:
                        session_data = login_resp.json()
                        self._csrf_token = session_data.get("csrf_token", "")
                        for cookie_name, cookie_val in login_resp.cookies.items():
                            self._session_cookies[cookie_name] = cookie_val
                        LOGGER.info(
                            "Successfully authenticated using demo account: %s", data["username"]
                        )
                        return True
        except Exception as exc:
            LOGGER.debug("Demo account auto-login failed: %s", exc)
        return False

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Execute an HTTP request with error handling and optional demo login retry."""
        client = await self.get_client()

        # Attempt initial demo login if no token is configured
        if not self.api_token and not self._session_cookies and not self._demo_attempted:
            await self._try_demo_login(client)

        req_headers = self._build_headers(headers)
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}

        try:
            resp = await client.request(
                method=method,
                url=path,
                params=clean_params,
                json=json_data,
                headers=req_headers,
                cookies=self._session_cookies,
            )
        except httpx.ConnectError as exc:
            err_msg = (
                f"Cannot connect to SensoryPlex API at {self.base_url}. "
                f"Ensure the container stack is running (./deploy/up.sh). Error: {exc}"
            )
            raise SensoryPlexAPIError(503, "service_unavailable", err_msg) from exc
        except httpx.TimeoutException as exc:
            raise SensoryPlexAPIError(
                504,
                "request_timeout",
                f"Request to {path} timed out after {self.timeout}s.",
            ) from exc

        # Handle 401 Unauthorized with one-time demo retry
        if resp.status_code == 401 and not self.api_token and not self._demo_attempted:
            if await self._try_demo_login(client):
                req_headers = self._build_headers(headers)
                resp = await client.request(
                    method=method,
                    url=path,
                    params=clean_params,
                    json=json_data,
                    headers=req_headers,
                    cookies=self._session_cookies,
                )

        if resp.status_code == 401:
            err_msg = (
                "SensoryPlex API authentication failed. Set SENSORYPLEX_API_TOKEN "
                "or configure a demo account via 'make demo-seed'."
            )
            raise SensoryPlexAuthError(401, "authentication_required", err_msg)
        if resp.status_code == 403:
            raise SensoryPlexAuthError(403, "permission_denied", resp.text)
        if resp.status_code == 404:
            raise SensoryPlexNotFoundError(404, "resource_not_found", resp.text)
        if resp.status_code >= 400:
            reason = "api_error"
            try:
                err_data = resp.json()
                reason = err_data.get("reason_code") or err_data.get("detail") or str(err_data)
            except Exception:
                reason = resp.text
            raise SensoryPlexAPIError(resp.status_code, reason, resp.text)

        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.text

    # ─────────────────────────────────────────────────────────────
    # System & Health
    # ─────────────────────────────────────────────────────────────
    async def get_health(self) -> dict[str, Any]:
        """Fetch platform health and capability readiness."""
        return await self.request("GET", "/v1/health")

    async def get_capabilities(self) -> dict[str, Any]:
        """Fetch complete capability status with reason codes."""
        return await self.request("GET", "/v1/capabilities")

    async def get_audit_events(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Fetch platform security audit events."""
        return await self.request(
            "GET",
            "/admin/v1/audit-events",
            params={"limit": limit, "offset": offset},
        )

    # ─────────────────────────────────────────────────────────────
    # Multimodal Material Search & Inspection
    # ─────────────────────────────────────────────────────────────
    async def search_materials(
        self,
        query: str,
        mode: str = "keyword",
        limit: int = 20,
        start_ms: int | None = None,
        end_ms: int | None = None,
        min_confidence: float | None = None,
        modalities: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search multimodal materials by keyword or vector semantic search."""
        body: dict[str, Any] = {
            "query": query,
            "mode": mode,
            "limit": limit,
        }
        if start_ms is not None:
            body["start_ms"] = start_ms
        if end_ms is not None:
            body["end_ms"] = end_ms
        if min_confidence is not None:
            body["min_confidence"] = min_confidence
        if modalities:
            body["modalities"] = modalities
        if tags:
            body["tags"] = tags
        return await self.request("POST", "/v1/materials:search", json_data=body)

    async def get_material(
        self,
        key: str,
        revision: int | None = None,
        execution_id: str = "",
    ) -> dict[str, Any]:
        """Retrieve full details of a MaterialUnit including observations and time range."""
        params: dict[str, Any] = {}
        if revision is not None:
            params["revision"] = revision
        if execution_id:
            params["execution_id"] = execution_id
        return await self.request("GET", f"/v1/materials/{key}", params=params)

    async def get_execution_timeline(self, execution_id: str) -> dict[str, Any]:
        """Fetch 1-second continuous slice coverage for an execution."""
        return await self.request("GET", f"/v1/executions/{execution_id}/timeline")

    async def get_execution_materials(
        self,
        execution_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Retrieve paginated materials belonging to an execution."""
        return await self.request(
            "GET",
            f"/v1/executions/{execution_id}/materials",
            params={"offset": offset, "limit": limit},
        )

    # ─────────────────────────────────────────────────────────────
    # Media Assets & Playback
    # ─────────────────────────────────────────────────────────────
    async def list_uploads(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List uploaded media assets and their admission status."""
        return await self.request("GET", "/v1/uploads", params={"limit": limit, "offset": offset})

    async def get_upload(self, upload_id: str) -> dict[str, Any]:
        """Get status and metadata of a specific upload."""
        return await self.request("GET", f"/v1/uploads/{upload_id}")

    def get_playback_stream_url(self, upload_id: str) -> str:
        """Construct the direct HTTP Range streaming URL for video playback."""
        clean_id = upload_id.removeprefix("upload://").removeprefix("asset_")
        if not clean_id.startswith("upload_") and len(clean_id) == 32:
            clean_id = f"upload_{clean_id}"
        return f"{self.base_url}/v1/uploads/{clean_id}/stream"

    # ─────────────────────────────────────────────────────────────
    # Pipelines & Jobs
    # ─────────────────────────────────────────────────────────────
    async def list_pipelines(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List available processing pipelines (draft and published)."""
        return await self.request(
            "GET",
            "/v1/pipelines",
            params={"limit": limit, "offset": offset},
        )

    async def create_and_dispatch_job(
        self,
        asset_id: str,
        pipeline_id: str,
        name: str = "",
        node_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a job draft and immediately dispatch it."""
        job_name = name or f"Job-{asset_id[:8]}"
        body: dict[str, Any] = {
            "name": job_name,
            "asset_id": asset_id,
            "pipeline_id": pipeline_id,
        }
        if node_id:
            body["node_id"] = node_id
        return await self.request("POST", "/v1/jobs", json_data=body)

    async def get_execution(self, execution_id: str) -> dict[str, Any]:
        """Get execution details including state, tasks, and receipts."""
        return await self.request("GET", f"/v1/executions/{execution_id}")

    async def get_run(self, run_id: str) -> dict[str, Any]:
        """Get pipeline run details."""
        return await self.request("GET", f"/v1/runs/{run_id}")

    async def get_run_tasks(self, run_id: str) -> dict[str, Any]:
        """Get individual task details for a pipeline run."""
        return await self.request("GET", f"/v1/runs/{run_id}/tasks")

    async def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Cancel an ongoing pipeline run."""
        return await self.request("POST", f"/v1/runs/{run_id}:cancel")

    # ─────────────────────────────────────────────────────────────
    # Cluster Nodes & Catalog (Admin)
    # ─────────────────────────────────────────────────────────────
    async def list_nodes(self, limit: int = 50) -> dict[str, Any]:
        """List all worker nodes in the cluster with status and accelerators."""
        return await self.request("GET", "/v1/nodes", params={"limit": limit})

    async def get_node(self, node_id: str) -> dict[str, Any]:
        """Get detailed node info including hardware accelerators and instances."""
        return await self.request("GET", f"/v1/nodes/{node_id}")

    async def approve_node(self, node_id: str) -> dict[str, Any]:
        """Approve an enrolling candidate node to join the cluster."""
        return await self.request("POST", f"/v1/nodes/{node_id}:approve")

    async def get_catalog(self) -> dict[str, Any]:
        """List registered plugin catalog entries."""
        return await self.request("GET", "/admin/v1/catalog")
