# kvenv

Load a repo's secrets from the environment's Azure Key Vault **at runtime**, the same way
on a laptop (`az login`), in CI (service principal), and deployed (managed identity).
No secrets in `.env`, no "pull to .env" step, one RBAC role everywhere.

This README is the source of truth for the naming scheme.

## Vaults

| Environment | Vault | Who uses it |
|---|---|---|
| `test` (default) | `kv-datamap-ops-test` | laptops, CI, test deployments |
| `prod` | `kv-datamap-ops-prod` | production deployments (`KVENV_ENV=prod` in app settings) |

Both live in the **DataMap Operations** subscription, resource group
`datamap-operations-keyvault`, RBAC mode, purge protection on, audit logs to Log Analytics.
Humans get **Key Vault Administrator**; workloads get **Key Vault Secrets User** via managed identity.

## Secret naming

```
<system>-<KEY>
```

| Part | Rule | Examples |
|---|---|---|
| `system` | lowercase; the owning external system or app registration; suffix when one system has several credential sets. **Never a prefix of another system name** (loading is prefix-based: `boomi` would swallow `boomi-embedkit-*`, so that one is `embedkit`). `push` refuses such a collision. | `boomi`, `embedkit`, `kyriba`, `rippling`, `breezy`, `graph-opsmetrics`, `slack`, `orchestration`, `verabricks-erp` |
| `KEY` | the environment variable name with `_` → `-` (Key Vault forbids underscores). Keep any prefix the variable already has so the round trip is mechanical | `BOOMI-TOKEN` ↔ `BOOMI_TOKEN`, `AZURE-CLIENT-SECRET` ↔ `AZURE_CLIENT_SECRET` |

The environment is **not** in the name; it is the vault. The same name exists in both
vaults with different values when a system has both a test and a prod credential. A
single-tenant system used from laptops (Boomi, Kyriba, Rippling) lives in **test**, and in
**prod** only when a deployed prod workload needs it.

**Tags** on every secret: `system`, `var` (original variable name), `repo` (consumers),
`owner`, `rotated` (ISO date), `source` (where it came from), `public=true` for non-secret
config stored for convenience. Filter with them:

```bash
az keyvault secret list --vault-name kv-datamap-ops-test --query "[?tags.system=='kyriba'].name" -o tsv
```

## What goes where

| | Location |
|---|---|
| Tokens, client secrets, passwords, connection strings, webhook URLs, encryption keys | **vault** |
| kvenv pointers, `VITE_*` client IDs, tenant IDs, redirect URIs, API URLs, ports, log levels, feature flags | **committed `.env`** |
| per-machine overrides (e.g. `KVENV_ENV=prod` when a laptop must deliberately hit prod) | **`.env.local`**, gitignored |

A fresh clone therefore needs nothing but `az login`.

**`.env.example` is committed too and lists every variable the app reads**, including the vault-backed
ones, each with an example value and, for secrets, the vault secret name. It is the one place a new
developer can see the whole configuration surface without touching the vault:

```dotenv
# [committed] kvenv app name
KVENV_SYSTEM=kyriba
# [committed]
coupa-url=https://<instance>.coupahost.com
# [vault] kv-datamap-ops-test/kyriba-clientsecret
clientsecret=<coupa-oauth-client-secret>
```

```dotenv
# .env — committed
KVENV_SYSTEM=kyriba
KVENV_ENV=test
```

Precedence, highest first: process environment → `.env.local` → `.env` → vault. The vault
never overwrites a variable that is already set, so CI and deployed app settings win.

## Install and use

### Python

```bash
pip install "git+https://github.com/datamap-ai/kvenv.git#subdirectory=python"
```
```python
import kvenv
kvenv.load()          # first line of the entry module
```

### Node / Vite

```bash
npm i github:datamap-ai/kvenv
```
```js
import { loadKvEnv } from '@datamap-ai/kvenv';
await loadKvEnv();    // before anything reads process.env
```
For a Vite app put the same two lines at the top of `vite.config.ts`. Vite forwards every
`VITE_*` present in `process.env` into `import.meta.env`; the browser never touches the vault.

### PowerShell / docker compose

```powershell
Import-Module (Join-Path $PSScriptRoot 'KvEnv.psm1')   # or from the clone: ./pwsh/KvEnv.psm1
Import-KvEnv
docker compose up
```

### Options

| Variable | Meaning |
|---|---|
| `KVENV_SYSTEM` | required; comma-separated system names, or `*` to map every secret in the vault 1:1 (`STYTCH-SECRET` → `STYTCH_SECRET`) for vaults that predate the `<system>-` convention, e.g. `KVENV_VAULT=kv-dev-datamap-ai` |
| `KVENV_ENV` | `test` (default) or `prod` |
| `KVENV_VAULT` | override the derived vault name |
| `KVENV_OPTIONAL=1` | warn instead of fail when the vault is unreadable (offline work) |

Failures are loud and name their cause, because the three causes need three fixes:
**not signed in** → `az login`; **RBAC denied** → ask for `Key Vault Secrets User` on that
vault for this identity (`az login` will not help); **unreachable** → wrong `KVENV_ENV`/`KVENV_VAULT`.

## Pushing secrets

```bash
python -m kvenv push --system kyriba --env test --from .env --repo kyriba --only clientid,clientsecret
python -m kvenv push --system slack  --env prod --from-json local.settings.json --json-path Values --only SLACK_WEBHOOK_URL
python -m kvenv push --system netsuite --env test --file PRIVATE_PEM=private.pem --content-type application/x-pem-file
python -m kvenv list --system kyriba --env test
python -m kvenv check        # what would load() use from this directory
```

`push` never prints values. Set `KVENV_OWNER` to override the `owner` tag (defaults to the
`az` signed-in user).

## Claude Code

The datamap-library ships `general-nudge-kvenv-setup` (SessionStart hook) and the
`general-kvenv-setup` skill. A session in a repo missing `KVENV_SYSTEM`/`KVENV_ENV` is told
exactly what is missing and asked to collect vault, environment, and app name before doing
anything else.
