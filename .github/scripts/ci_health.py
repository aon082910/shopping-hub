"""Triage a selftest run into 'a site changed' vs 'our IP is blocked'.

selftest cannot tell those apart -- a blocked request and a rewritten page both
produce '0 listings parsed'. But the *pattern* across adapters is informative:
these are independent companies with independent markup, so several breaking on
the same day is far more likely to be us than them.

Exit 0 = healthy or inconclusive, 1 = at least one adapter looks genuinely broken.
"""
from __future__ import annotations
import re, subprocess, sys, os

SITES = sys.argv[1] if len(sys.argv) > 1 else ""
cmd = [sys.executable, "-m", "sourcehub.cli", "selftest"]
if SITES:
    cmd += ["--site", SITES]

proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
out = (proc.stdout or "") + (proc.stderr or "")
print(out)

ok, bad = [], []
for line in out.splitlines():
    m = re.match(r"\s+(\S+)\s+(ok|FAIL|ERROR)\b", line)
    if m:
        (ok if m.group(2) == "ok" else bad).append(m.group(1))

total = len(ok) + len(bad)
if not total:
    print("::warning::selftest produced no adapter lines; treating as inconclusive")
    sys.exit(0)

ratio = len(bad) / total
verdict = "healthy"
if bad:
    verdict = "network" if ratio >= 0.75 else "broken"

summary = [
    "## Adapter health",
    "",
    f"- healthy: **{len(ok)}** ({', '.join(ok) or 'none'})",
    f"- failing: **{len(bad)}** ({', '.join(bad) or 'none'})",
    "",
]
if verdict == "network":
    summary += [
        f"{len(bad)} of {total} adapters failed. These are unrelated companies, so a "
        "simultaneous break is far more likely to be **this runner's IP being blocked** "
        "than every site changing at once. No issue opened.",
        "",
        "Datacenter IPs get blocked routinely. For a trustworthy result, run this "
        "workflow on a self-hosted runner from a residential connection.",
    ]
elif verdict == "broken":
    summary += [
        f"{len(bad)} of {total} adapters failed while the rest worked from the same IP, "
        "so this looks like **site markup actually changed** rather than a block.",
        "",
        "Reproduce with: `python -m sourcehub.cli selftest --site " + ",".join(bad) + "`",
    ]
else:
    summary += ["All adapters parsed listings."]

text = "\n".join(summary)
print(text)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
        fh.write(text + "\n")
if os.environ.get("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
        fh.write(f"verdict={verdict}\n")
        fh.write(f"failing={','.join(bad)}\n")

sys.exit(1 if verdict == "broken" else 0)
