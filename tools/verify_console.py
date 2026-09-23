"""以真实浏览器验证应用准备流程；不发起 Runtime 任务或构造模型产物。"""

import argparse
import hashlib
import json
import secrets
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--media", required=True, type=Path, help="有权限使用的真实 WebM 或 MP4")
    parser.add_argument("--url", default="http://127.0.0.1:8091")
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--password-file", type=Path, default=ROOT / ".data/console-preview/admin-password"
    )
    args = parser.parse_args()
    if urlparse(args.url).hostname not in {"127.0.0.1", "localhost"}:
        parser.error("verification is restricted to a local preview")
    if not args.media.is_file() or args.media.suffix.lower() not in {".mp4", ".webm"}:
        parser.error("an existing authorized MP4 or WebM is required")
    if args.password_file.stat().st_mode & 0o077:
        parser.error("password file must only be accessible to its owner")
    marker = "ui-" + uuid.uuid4().hex[:8]
    output = ROOT / ".data/console-preview" / marker
    output.mkdir(parents=True, mode=0o700)
    command = ["npx", "--yes", "agent-browser@0.38.1", "--session", marker]

    def browser(*arguments):
        try:
            result = subprocess.run(
                command + list(arguments), text=True, capture_output=True, timeout=50
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"browser_{arguments[0]}_timed_out") from None
        if result.returncode:
            # 不拼接 argv 或完整快照，避免密码与仅显示一次的凭据进入日志。
            raise RuntimeError(f"browser_{arguments[0]}_failed")
        return result.stdout

    def verify(expression):
        value = browser("eval", f"Boolean({expression})").strip()
        if value != "true":
            raise AssertionError("browser_assertion_failed: " + expression)

    def button(name):
        browser("find", "role", "button", "click", "--name", name, "--exact")

    def visit(path):
        browser("open", args.url + path)
        browser("wait", "--load", "networkidle")

    def login(name, password):
        browser("fill", 'input[name="username"]', name)
        browser("fill", 'input[name="password"]', password)
        browser("click", "form button.primary")
        browser("wait", "--url", "**/assets")
        browser("wait", "--load", "networkidle")

    passed = []
    try:
        visit("/")
        login(args.username, args.password_file.read_text().strip())
        print("Browser step passed", flush=True)
        passed.append("form_login")
        visit("/plugins")
        browser("click", '[data-plugin-id="org.sensoryplex.vlm-moondream"] button.primary')
        browser("fill", 'input[name="name"]', marker)
        browser("click", "form button.primary")
        browser("wait", "--load", "networkidle")
        verify(f"document.body.innerText.includes({json.dumps(marker)})")
        visit("/pipelines")
        button("新建方案")
        browser("fill", 'input[name="name"]', marker)
        browser("select", 'select[name="config_id"]', marker + " · v1")
        browser("click", "form button.primary")
        browser("wait", "--load", "networkidle")
        verify(f"document.body.innerText.includes({json.dumps(marker)})")
        print("Browser step passed", flush=True)
        passed.append("config_and_pipeline_persisted")
        visit("/assets")
        browser("upload", 'input[type="file"]', str(args.media.resolve()))
        browser("wait", "--load", "networkidle")
        # 首行是本轮新上传记录；后端没有按文件名复用所有者引用。
        browser("click", "tbody tr:first-child button")
        browser("wait", "video")
        browser(
            "eval",
            "document.querySelector('video').muted=true; document.querySelector('video').play()",
        )
        browser("wait", "--fn", "document.querySelector('video').currentTime > 0")
        with args.media.open("rb") as media:
            digest = hashlib.file_digest(media, "sha256").hexdigest()
        verify(f"document.body.innerText.includes({json.dumps('sha256:' + digest)})")
        verify(
            "document.querySelector('video').videoWidth > 0 && "
            "!document.querySelector('video').error"
        )
        browser(
            "eval",
            "document.querySelector('video').pause(); "
            "document.querySelector('video').currentTime=5",
        )
        browser("screenshot", str(output / "playback.png"))
        button("关闭")
        print("Browser step passed", flush=True)
        passed.append("real_upload_digest_playback")
        visit("/jobs")
        button("新建草稿")
        browser("fill", 'input[name="name"]', marker)
        browser(
            "eval",
            "const s=document.querySelector('select[name=asset_id]');s.value=s.options[1].value;"
            "s.dispatchEvent(new Event('change',{bubbles:true}))",
        )
        browser("select", 'select[name="pipeline_id"]', marker + " · v1")
        browser("click", "form button.primary")
        browser("wait", "--load", "networkidle")
        browser("reload")
        browser("wait", "--load", "networkidle")
        verify(f"document.body.innerText.includes({json.dumps(marker)})")
        verify(
            "[...document.querySelectorAll('button')].find(x=>x.textContent==='开始处理').disabled"
        )
        browser("screenshot", str(output / "jobs.png"))
        print("Browser step passed", flush=True)
        passed.append("draft_refresh_and_execution_disabled")
        visit("/access")
        button("创建凭据")
        browser("fill", 'input[name="name"]', marker)
        browser("click", "form button.primary")
        browser("wait", "--load", "networkidle")
        verify("document.querySelector('.token-secret code').textContent.startsWith('spx_')")
        button("我已保存")
        browser("click", "tbody tr:first-child button")
        browser("wait", "--load", "networkidle")
        verify("document.querySelector('tbody tr').innerText.includes('已撤销')")
        print("Browser step passed", flush=True)
        passed.append("token_create_hide_revoke")
        visit("/users")
        button("创建用户")
        viewer_password = secrets.token_urlsafe(24)
        browser("fill", 'input[name="display_name"]', marker)
        browser("fill", 'input[name="username"]', marker)
        browser("fill", 'input[name="password"]', viewer_password)
        browser("click", "form button.primary")
        browser("wait", "--load", "networkidle")
        verify(f"document.body.innerText.includes({json.dumps(marker)})")
        button("退出登录")
        browser("wait", 'input[name="username"]')
        login(marker, viewer_password)
        verify("!document.querySelector('a[href=\"/plugins\"]')")
        visit("/plugins")
        verify("document.body.innerText.includes('无权访问此页面')")
        visit("/assets")
        verify(f"!document.body.innerText.includes({json.dumps(args.media.name)})")
        browser("set", "viewport", "390", "844")
        verify("document.documentElement.scrollWidth <= window.innerWidth")
        button("切换导航")
        verify("document.querySelector('.sidebar').classList.contains('mobile-open')")
        browser("screenshot", str(output / "mobile.png"))
        button("切换导航")
        print("Browser step passed", flush=True)
        passed.append("viewer_menu_route_owner_mobile")
        errors = browser("errors").strip()
        if errors:
            raise AssertionError("browser_reported_runtime_errors")
        (output / "result.json").write_text(
            json.dumps({"checks": passed, "runtime_execution_verified": False}, indent=2) + "\n"
        )
        print(f"Console browser verification passed: {len(passed)} checks; artifacts: {output}")
    finally:
        browser("close")


if __name__ == "__main__":
    main()
