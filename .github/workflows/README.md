# Workflows

| Workflow | When | What it does |
|---|---|---|
| `tests.yml` | push, PR | Runs the 13 test suites and builds the image without pushing |
| `health.yml` | Mondays, manual | Runs `selftest` against live sites and opens an issue when an adapter looks genuinely broken |
| `release.yml` | tag `v*`, manual | Builds and pushes both images (`latest` and `slim`) to Docker Hub, then cuts a GitHub release |

## One-time setup for releases

`release.yml` needs two repository secrets. Add them under
**Settings -> Secrets and variables -> Actions**:

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | `allornothing` |
| `DOCKERHUB_TOKEN` | A Docker Hub **access token** with Read/Write, from Account Settings -> Personal access tokens |

Use an access token, not your password: a token can be revoked on its own and is
scoped to the registry.

Then publishing is:

```bash
git tag v1.0.1 && git push origin v1.0.1
```

The workflow checks the token exists before starting the build, so a missing
secret fails in seconds instead of after a ten minute image build.

## On the health check

`selftest` cannot tell a blocked request from rewritten markup -- both come back as
zero listings. `.github/scripts/ci_health.py` uses the failure *pattern* instead:
these are unrelated companies, so several breaking at once is much more likely to be
our IP than all of them changing on the same day. It only opens an issue when a
minority fail while the rest succeed from the same address.

GitHub's runners are datacenter IPs and get blocked routinely, so a scheduled run
will often report "network". For results you can trust, run it on a self-hosted
runner from a residential connection -- the Unraid box itself works well:

Actions -> adapter health -> Run workflow -> set **runner** to your runner's label.
