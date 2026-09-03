# Workflows

| Workflow | When | What it does |
|---|---|---|
| `tests.yml` | push, PR | Runs the 13 test suites and builds the image without pushing |
| `health.yml` | Mondays, manual | Runs `selftest` against live sites and opens an issue when an adapter looks genuinely broken |
| `release.yml` | tag `v*`, manual | Builds and pushes both images (`latest` and `slim`) to Docker Hub, then cuts a GitHub release |

## One-time setup for releases

`release.yml` needs exactly one repository secret, added under
**Settings -> Secrets and variables -> Actions -> New repository secret**:

| Secret | Value |
|---|---|
| `DOCKERHUB_TOKEN` | A Docker Hub **access token** with Read & Write, from https://app.docker.com/settings/personal-access-tokens |

The name must be exactly `DOCKERHUB_TOKEN` -- the workflow reads that string, and any
other name reads as empty. The *value* is the token; the secret's name is not where
the username goes.

The Docker Hub account name is not a secret (it is already public in the image tag
`allornothing/shopping-hub`), so it is hardcoded. Publishing under a different
account only needs a repository **variable** named `DOCKERHUB_USERNAME`.

Use an access token, not your password: a token is scoped to the registry and can be
revoked on its own.

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
