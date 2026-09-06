"""Translation falls back to the free, keyless provider rather than giving up.

The gap this closes: TRANSLATE_PROVIDER=claude with no ANTHROPIC_API_KEY (or a
rate-limited/down API) used to mean every Chinese listing went untranslated for
that whole run -- silently, since the batch loop already catches the exception and
substitutes the raw source text. But `google_free` (deep-translator's unofficial
Google Translate client) needs no key at all and was sitting right there, unused as
a fallback. An explicit TRANSLATE_PROVIDER=none is different: that's the user
deliberately turning translation off, and must NOT get overridden.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_trfallback_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sqlalchemy import select  # noqa: E402

from sourcehub.db.models import Translation  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline.translate import Translator  # noqa: E402

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def main() -> None:
    init_db()

    print("configured provider unreachable -> falls back to google_free")

    def _broken_claude(self, texts, src_lang):
        raise RuntimeError("ANTHROPIC_API_KEY is unset")

    def _fake_google_free(self, texts, src_lang):
        return [{"无线蓝牙耳机": "Wireless Bluetooth Earbuds"}.get(t, t) for t in texts]

    Translator._call_provider = _broken_claude
    Translator._google_free = _fake_google_free

    with session_scope() as s:
        tr = Translator(s, provider="claude")
        out = tr.translate_many(["无线蓝牙耳机"])
        check("fallback translation used, not raw passthrough", out, ["Wireless Bluetooth Earbuds"])

        row = s.scalar(select(Translation))
        check("cache row attributes the translation to the provider that "
              "actually produced it, not the one that failed", row.provider, "google_free")

    print("\na fresh Translator for the same text hits the fallback's cache entry")
    with session_scope() as s:
        tr2 = Translator(s, provider="google_free")
        out2 = tr2.translate_many(["无线蓝牙耳机"])
        check("google_free-configured run reuses the fallback's cache entry",
              out2, ["Wireless Bluetooth Earbuds"])

    print("\nexplicit TRANSLATE_PROVIDER=none is never overridden")

    def _should_not_be_called(self, texts, src_lang):
        raise AssertionError("google_free must not run when the provider is none")

    Translator._google_free = _should_not_be_called
    with session_scope() as s:
        tr3 = Translator(s, provider="none")
        out3 = tr3.translate_many(["一个从未见过的新短语"])
        check("none stays untranslated (raw text passed through)",
              out3, ["一个从未见过的新短语"])

    print("\ngoogle_free itself failing does not recurse into itself")

    def _broken_google_free(self, texts, src_lang):
        raise RuntimeError("rate limited")

    Translator._call_provider = lambda self, texts, src_lang: self._google_free(texts, src_lang)
    Translator._google_free = _broken_google_free
    with session_scope() as s:
        tr4 = Translator(s, provider="google_free")
        out4 = tr4.translate_many(["另一个从未见过的短语"])
        check("falls back to raw text, does not crash", out4, ["另一个从未见过的短语"])

    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("translation fallback OK")


if __name__ == "__main__":
    main()
