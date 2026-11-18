"""Create .env with fresh random secrets for self-hosted Langfuse.

    python scripts/init_env.py        (or: make langfuse-env)

.env is read by BOTH docker compose (to configure the Langfuse containers) and
the Python app (LANGFUSE_* keys tell the SDK where to send traces). It is
git-ignored. Running this again does nothing if .env already exists - delete it
first (and `make langfuse-reset`) to start over.
"""

import secrets
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"


def main() -> None:
    if ENV.exists():
        print(f"{ENV} already exists - leaving it alone.")
        return
    values = {
        # --- read by the Python SDK -------------------------------------------
        "LANGFUSE_BASE_URL": "http://localhost:3000",
        "LANGFUSE_PUBLIC_KEY": f"pk-lf-{uuid.uuid4()}",
        "LANGFUSE_SECRET_KEY": f"sk-lf-{uuid.uuid4()}",
        # --- login for the Langfuse UI (local only) -----------------------------
        "LANGFUSE_INIT_USER_EMAIL": "admin@example.com",
        "LANGFUSE_INIT_USER_PASSWORD": secrets.token_urlsafe(12),
        # --- internal secrets for the Langfuse containers ---------------------
        "LANGFUSE_SALT": secrets.token_hex(16),
        "LANGFUSE_ENCRYPTION_KEY": secrets.token_hex(32),  # must be 64 hex chars
        "NEXTAUTH_SECRET": secrets.token_hex(32),
        "POSTGRES_PASSWORD": secrets.token_hex(16),
        "CLICKHOUSE_PASSWORD": secrets.token_hex(16),
        "MINIO_ROOT_PASSWORD": secrets.token_hex(16),
        "REDIS_AUTH": secrets.token_hex(16),
    }
    ENV.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    ENV.chmod(0o600)
    print(f"Wrote {ENV}")
    print(f"Langfuse login: {values['LANGFUSE_INIT_USER_EMAIL']} / {values['LANGFUSE_INIT_USER_PASSWORD']}")


if __name__ == "__main__":
    main()
