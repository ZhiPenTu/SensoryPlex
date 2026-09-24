"""生成本地凭据，不会覆盖已有设置，也不会把密钥写入日志。"""

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
path = ROOT / ".env"
if path.exists():
    print("Existing .env preserved")
else:
    password, token = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    template = (ROOT / ".env.example").read_text()
    content = template.replace("REPLACE_WITH_RANDOM_URL_SAFE_SECRET", password).replace(
        "REPLACE_WITH_RANDOM_API_TOKEN", token
    )
    content = content.replace("REPLACE_WITH_RANDOM_MINIO_SECRET", secrets.token_urlsafe(32))
    # 检索面的令牌在 .env 里有两个名字（检索面读 AUTH_TOKEN，api 读 SEARCH_TOKEN）：
    # 一次 replace 把同一个随机值写进这两处，避免出现"两个名字两把锁"。
    content = content.replace("REPLACE_WITH_RANDOM_INDEX_TOKEN", secrets.token_urlsafe(48))
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(content)
    print("Created .env with private permissions and random local credentials")
