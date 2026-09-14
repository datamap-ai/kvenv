# kvenv

Load an application's secrets from an **Azure Key Vault** into its environment **at runtime**,
the same way on a laptop (`az login`), in CI (service principal), and deployed (managed
identity). No secrets in `.env`, no "pull to .env" step, one RBAC role everywhere.

Three ports, one contract: Python, Node (incl. Vite), PowerShell.

## How it works

1. Read the committed `.env` (and a gitignored `.env.local` for per-machine overrides).
2. Sign in with `DefaultAzureCredential`.
3. List the vault, take every secret named `<system>-<KEY>`, and set `KEY` (with `-` → `_`) in
   the process environment **if it is not already set**.

Precedence, highest first: process environment → `.env.local` → `.env` → vault. Existing
variables always win, so CI variables and deployed app settings override the vault.

## Configuration

| Variable | Meaning |
|---|---|
| `KVENV_VAULT` | the vault name (`https://<name>.vault.azure.net`). Required unless the next two are set |
| `KVENV_VAULT_PATTERN` | optional; a pattern containing `{env}`, e.g. `kv-myorg-{env}` |
| `KVENV_ENV` | optional environment label substituted into the pattern, e.g. `test`, `prod` |
| `KVENV_SYSTEM` | required; one or more comma-separated system names, or `*` to map every secret 1:1 |
| `KVENV_OPTIONAL=1` | warn instead of fail when the vault is unreadable (offline work) |

These live in the committed `.env`, next to public configuration:

```dotenv
KVENV_VAULT=kv-example-test
KVENV_SYSTEM=myapp
API_URL=https://api.example.com
```

## Secret naming

```
<system>-<KEY>
```

- `system` — lowercase; the application or credential owner (`myapp`, `graph-reporting`).
  **Never a prefix of another system name** — loading is prefix-based, so `app` would swallow
  `app-worker-*`. `push` refuses such a collision.
- `KEY` — the environment variable name with `_` → `-` (Key Vault forbids underscores).
  `myapp-CLIENT-SECRET` ↔ `CLIENT_SECRET`.

The environment is **not** in the name; use one vault per environment.
`KVENV_SYSTEM=*` maps a whole vault 1:1 (`STYTCH-SECRET` → `STYTCH_SECRET`) for vaults that
do not use the prefix.

`push` tags every secret with `system`, `var`, `repo`, `owner`, `rotated`, `source`, so the vault
can be filtered without parsing names.

## What goes where

| | Location |
|---|---|
| tokens, client secrets, passwords, connection strings, webhook URLs, keys | **vault** |
| kvenv pointers, public client IDs, tenant IDs, URLs, ports, flags | **committed `.env`** |
| per-machine overrides | **`.env.local`**, gitignored |
| every variable the app reads, with example values and vault secret names | **committed `.env.example`** |

A fresh clone needs nothing but `az login`.

## Install and use

### Python

```bash
pip install "kvenv @ https://github.com/datamap-ai/kvenv/archive/<commit>.tar.gz#subdirectory=python"
```
```python
import kvenv
kvenv.load()          # first line of the entry module
```

### Node / Vite

```bash
npm i https://github.com/datamap-ai/kvenv/tarball/<commit>
```
```js
import { loadKvEnv } from '@datamap-ai/kvenv';
await loadKvEnv();    // before anything reads process.env
```
For a Vite app put the same two lines at the top of `vite.config.ts`. Vite forwards every
`VITE_*` present in `process.env` into `import.meta.env`; the browser never touches the vault.
For a CommonJS entry point, a 3-line `start.mjs` (import, `await loadKvEnv()`,
`await import('./index.js')`) does the job.

Pin a commit in the URL rather than a branch so installs are reproducible. Tarball URLs need
neither `git` nor SSH inside a Docker build.

### PowerShell / docker compose

```powershell
Import-Module ./pwsh/KvEnv.psm1
Import-KvEnv
docker compose up      # compose sees the loaded variables via ${VAR}
```

## Failures are loud and name their cause

| Cause | Fix |
|---|---|
| not signed in | `az login` (or SP variables in CI) |
| signed in but 403 | grant this identity **Key Vault Secrets User** on the vault; `az login` will not help |
| vault unreachable | wrong `KVENV_VAULT` / pattern |
| `no secrets named '<system>-*'` | wrong system name, or nothing pushed yet |

## CLI

```bash
python -m kvenv push --system myapp --vault kv-example --from .env --repo myapp --only CLIENT_SECRET,API_TOKEN
python -m kvenv push --system myapp --vault kv-example --from-json local.settings.json --json-path Values --only WEBHOOK_URL
python -m kvenv push --system myapp --vault kv-example --file PRIVATE_PEM=private.pem --content-type application/x-pem-file
python -m kvenv list --system myapp --vault kv-example
python -m kvenv check        # what would load() use from this directory
```

`push` and `list` never print values.
