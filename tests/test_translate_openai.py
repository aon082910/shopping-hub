"""TRANSLATE_PROVIDER=openai talks to an OpenAI-compatible /chat/completions endpoint.

No network: httpx.post is replaced with a fake that records the request and returns
a canned chat-completions response, so this checks the request shape (URL, bearer
header, model) and that the reply is parsed back into one string per input.
"""

from __future__ import annotations

import json
import json as _json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_tropenai_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "openai"
os.environ["LLM_BASE_URL"] = "http://gateway.test:20128/v1/"
os.environ["LLM_API_KEY"] = "test-key"
os.environ.pop("LLM_MODEL", None)

import httpx  # noqa: E402

from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline.translate import Translator  # noqa: E402

FAILS: list[str] = []
CALLS: list[dict] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def _fake_post(url, headers=None, json=None, timeout=None, **_):  # noqa: A002
    CALLS.append({"url": url, "headers": headers or {}, "json": json})
    texts = _json.loads(json["messages"][-1]["content"])
    out = [{"无线蓝牙耳机": "Wireless Bluetooth Earbuds"}.get(t, t) for t in texts]
    body = {"choices": [{"message": {"content": _json.dumps(out)}}]}
    return httpx.Response(200, json=body, request=httpx.Request("POST", url))


def main() -> None:
    init_db()
    httpx.post = _fake_post

    print("openai provider")
    with session_scope() as s:
        tr = Translator(s)
        check("provider comes from TRANSLATE_PROVIDER", tr.provider, "openai")
        out = tr.translate_many(["无线蓝牙耳机"])
        check("reply parsed into translations", out, ["Wireless Bluetooth Earbuds"])

    check("one request sent", len(CALLS), 1)
    if CALLS:
        c = CALLS[0]
        check("posts to <base>/chat/completions without a double slash",
              c["url"], "http://gateway.test:20128/v1/chat/completions")
        check("sends the key as a bearer token", c["headers"].get("Authorization"), "Bearer test-key")
        check("default model is apps-default", c["json"]["model"], "apps-default")
        check("user message is the JSON array of source texts",
              json.loads(c["json"]["messages"][-1]["content"]), ["无线蓝牙耳机"])

    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("openai translation OK")


if __name__ == "__main__":
    main()
