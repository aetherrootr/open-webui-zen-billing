# Open WebUI Zen Billing

A separately versioned component for Open WebUI **v0.12.5**, recording each
authenticated user's OpenCode Zen costs while keeping one shared upstream API
key. No additional proxy, S3, Redis or database server is required.

This repository owns the Python source, tests, dependency lock and container
release. The DevOps repository owns only the image reference, Kubernetes Job and
existing Secret/PVC references.

## How it runs

The release image contains two native Open WebUI functions and their installer.
It is an **installation Job**, not a long-running billing API or proxy. An Argo CD
PostSync hook runs the image once and installs/updates the functions through Open
WebUI's native administration API. The functions then run inside Open WebUI:

```text
Users -> Open WebUI -> shared OpenCode Zen API
              |
              +-> Zen billing filter -> existing PostgreSQL ledger
              +-> Zen reporting model -> per-user/month report
```

- `zen_cost_tracker`: enabled globally; only processes supported Zen connections.
- `zen_cost_report`: select **Zen 费用报表** and send `查看本月费用` or `2026-11`.
  Admins see all users; regular users see only their own records.

Reported `usage.cost` takes precedence over catalog estimates. Explicitly marked
provider estimates remain estimates. The report separates reported costs,
estimated costs, and requests requiring reconciliation. Estimates use exact model
IDs from the Zen `/models` catalog, with separate input/output/cache rates and
supported context tiers; unknown models and missing usage are not treated as free.
Prices and totals use decimal arithmetic in USD.

## Develop and test

Install [uv](https://docs.astral.sh/uv/) and run:

```sh
uv sync --python 3.12 --locked
make check
make test
```

Tests use a private in-memory SQLite database, synthetic identities and a small
public catalog fixture captured on 2026-11-09. No test needs a production API key
or calls a paid model. The CI image test also runs the tests inside the exact
Open WebUI base image, catching incompatibilities with internal imports.

The prior preview was verified in the deployed application: Haiku returned
11 input / 4 output tokens and `usage.cost = 0.000031`; that amount appeared once
in the user's report. The release image itself must still be built by CI before
image-based deployment is verified.

## Push and publish

Create the remote GitHub repository, then push this local repository:

```sh
git remote add origin git@github.com:aetherrootr/open-webui-zen-billing.git
git push -u origin main
git tag v0.1.0
git push origin v0.1.0
```

GitHub Actions runs lint, unit/installer tests and the base-image test, then
publishes **linux/amd64** images to `ghcr.io/<owner>/<repository>`. Branches publish
their branch name and `sha-...`; a `v0.1.0` tag publishes the `v0.1.0` image tag.
Pull requests test/build without publishing. Publishing uses `GITHUB_TOKEN` with
`packages: write`; no Zen key, database credential or separate registry password
belongs in Actions. Deployment does not run from this workflow.

If the repository/package is private, configure the cluster's standard GHCR pull
credential (`imagePullSecrets`). Match the image path to your actual remote name.
Prefer an immutable image digest in production; do not deploy branch tags.

## Deploy from the DevOps repository

`deploy/job.example.yaml` documents the integration contract. The equivalent
Jsonnet is prepared in `os-environment` on `codex/zen-user-cost-tracking`.
Publish the image first, review the DevOps PR, then merge it yourself. Argo CD
follows `master` and runs the PostSync installation after Open WebUI is healthy.

The installer uses the existing Open WebUI PostgreSQL Secret and persisted
signing-key file to obtain a five-minute administrator token. It creates no
permanent admin credential and needs no Kubernetes API access. The shared Zen
credential remains in Open WebUI; the filter reuses it only for Zen's own HTTPS
catalog endpoint. For an external installation, supply an existing admin token
via `WEBUI_ADMIN_TOKEN_FILE` instead of the database/signing-key bootstrap.

Source replacement is idempotent; existing function valves and cost records are
preserved. The function IDs and `zen_user_cost_entry` table remain compatible with
the deployed preview. No manual function copy/paste is needed after a release.
The image base version must match the running Open WebUI version; upgrades need
tests against the new base before deployment.

Required runtime settings:

| Variable | Purpose |
|---|---|
| `WEBUI_INTERNAL_URL` | Internal Open WebUI service URL |
| `WEBUI_SECRET_KEY_FILE` | Existing signing-key file (read-only mount) |
| `DATABASE_URL` | Existing Open WebUI DB, with password injected from Secret |
| `WEBUI_ADMIN_TOKEN_FILE` | Optional existing admin token; replaces bootstrap above |
| `BILLING_PLUGIN_DIR` | Image-provided source path; normally leave unchanged |

## Data and accounting scope

Records are in `zen_user_cost_entry` in the existing Open WebUI database, covered
by its database backup. They include user ID/name, model, timestamps, chat/message
IDs, usage, cost source and price snapshots. No prompts, responses, authentication tokens or
API keys are stored in the ledger. Chat deletion does not remove cost records.

Tracking starts when the filter is enabled. Saved-chat completion flows are
supported. This is usage attribution, **not an invoice, payment system or complete
upstream billing reconciliation**: background title/tag generation, calls that
bypass filters, bare API requests without completion context, personal Direct
Connections and interrupted requests without a completion callback may be absent.
Merged tool-round estimates can differ from per-request context tiers. A one-hour
cache write with no matching catalog rate is marked for reconciliation. The
OpenCode statement remains authoritative. No historical backfill is performed.

To roll back, deploy a previously tested image and rerun the Job. To stop tracking,
disable the filter in Admin > Functions and remove/disable its GitOps installer;
otherwise the next installation enables it again. The ledger remains intact.
