"""No Brakes dialer internals."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> None:
    """Populate os.environ from .env without clobbering real env vars.

    Strips surrounding quotes. The AIOS .env quotes some values, and a parser
    that does not strip them hands Twilio a key wrapped in literal double quotes
    and fails with an opaque 401.
    """
    import os

    p = path or (ROOT / ".env")
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)
