"""kvenv — load an application's secrets from an Azure Key Vault into the environment at runtime.

Contract (identical in the Node and PowerShell ports):

  * ``KVENV_VAULT``   the Key Vault name (``https://<name>.vault.azure.net``). Required unless
                     ``KVENV_VAULT_PATTERN`` (containing ``{env}``) and ``KVENV_ENV`` are both set.
  * ``KVENV_SYSTEM``  required; one or more comma-separated system names, or ``*`` to map
                     every secret in the vault 1:1 (vaults that do not use the ``<system>-`` prefix).
  * ``KVENV_ENV``     an environment label (e.g. ``test``, ``prod``); only used with the pattern.
  * ``KVENV_OPTIONAL`` set to ``1`` to downgrade vault failures to a warning.

Secret names are ``<system>-<KEY>`` where KEY is the environment variable name with
``_`` replaced by ``-``. ``myapp-CLIENT-SECRET`` becomes ``CLIENT_SECRET``.

``load()`` first applies ``.env.local`` then ``.env`` from the repo root (never
overwriting a variable that is already set), then fetches every vault secret whose
name starts with ``<system>-`` and sets the variable if it is still unset. Existing
process variables always win, so CI and deployed app settings can override.

Auth is ``DefaultAzureCredential``: ``az login`` on a laptop, managed identity when
deployed, service-principal variables in CI. Same RBAC role in every case.

Values are never logged or printed by this module.
"""
from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path
from typing import Iterable

__all__ = ["load", "KvEnvError", "vault_name", "secret_name", "var_name", "repo_root"]
__version__ = "0.1.0"



class KvEnvError(RuntimeError):
    """Raised when the vault cannot be read. Message names the cause and the remedy."""


# --------------------------------------------------------------------------- naming

def vault_name(env: str | None = None, override: str | None = None) -> str:
    """Resolve the vault name: KVENV_VAULT, else KVENV_VAULT_PATTERN with {env} substituted."""
    override = override or os.environ.get("KVENV_VAULT")
    if override:
        return override
    pattern = os.environ.get("KVENV_VAULT_PATTERN")
    env = env or os.environ.get("KVENV_ENV")
    if pattern and env:
        return pattern.replace("{env}", env)
    raise KvEnvError(
        "kvenv: no vault configured. Set KVENV_VAULT=<vault name> in the committed .env "
        "(or KVENV_VAULT_PATTERN containing {env} together with KVENV_ENV)."
    )


WILDCARD = "*"  # KVENV_SYSTEM=* : every secret in the vault, secret name == variable name
                # (for vaults that do not use the <system>- prefix).


def secret_name(system: str, var: str) -> str:
    """Environment variable name -> Key Vault secret name."""
    key = var.replace("_", "-")
    return key if system == WILDCARD else f"{system}-{key}"


def var_name(system: str, name: str) -> str | None:
    """Key Vault secret name -> environment variable name, or None if not for this system."""
    if system == WILDCARD:
        return name.replace("-", "_")
    prefix = f"{system}-"
    if not name.lower().startswith(prefix.lower()):
        return None
    return name[len(prefix):].replace("-", "_")


# --------------------------------------------------------------------------- .env

def repo_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (cwd) to the first directory holding ``.git`` or ``.env``."""
    p = (start or Path.cwd()).resolve()
    for cand in (p, *p.parents):
        if (cand / ".git").exists() or (cand / ".env").exists():
            return cand
    return p


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def _apply_dotenv(root: Path) -> None:
    # .env.local (per-machine, gitignored) wins over .env (committed); both lose to the process env.
    for fname in (".env.local", ".env"):
        for k, v in _parse_dotenv(root / fname).items():
            os.environ.setdefault(k, v)


# --------------------------------------------------------------------------- vault

def _classify(exc: Exception, vault: str) -> KvEnvError:
    from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ServiceRequestError

    if isinstance(exc, ClientAuthenticationError):
        return KvEnvError(
            f"kvenv: not signed in to Azure, cannot read {vault}. Run `az login` "
            "(or set AZURE_CLIENT_ID/AZURE_TENANT_ID/AZURE_CLIENT_SECRET in CI)."
        )
    if isinstance(exc, HttpResponseError) and getattr(exc, "status_code", None) == 403:
        return KvEnvError(
            f"kvenv: signed in, but this identity is denied on {vault}. `az login` will not "
            "help. Ask for the 'Key Vault Secrets User' role on that vault for this identity."
        )
    if isinstance(exc, ServiceRequestError):
        return KvEnvError(
            f"kvenv: vault {vault} is unreachable (DNS/network). Check KVENV_ENV / KVENV_VAULT."
        )
    return KvEnvError(f"kvenv: failed reading {vault}: {type(exc).__name__}: {exc}")


def _fetch(systems: Iterable[str], vault: str) -> dict[str, int]:
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    client = SecretClient(vault_url=f"https://{vault}.vault.azure.net/", credential=cred)
    counts = {s: 0 for s in systems}
    try:
        names = [p.name for p in client.list_properties_of_secrets() if p.enabled is not False]
        for system in systems:
            for name in names:
                var = var_name(system, name)
                if var is None:
                    continue
                counts[system] += 1
                if var in os.environ:
                    continue  # process env wins; skip the network round trip
                os.environ[var] = client.get_secret(name).value or ""
    except Exception as exc:  # noqa: BLE001 - classified below
        raise _classify(exc, vault) from None
    return counts


def load(
    system: str | None = None,
    env: str | None = None,
    vault: str | None = None,
    *,
    dotenv: bool = True,
    optional: bool | None = None,
    start: Path | None = None,
) -> dict[str, int]:
    """Populate ``os.environ`` from ``.env`` files and the Key Vault.

    Returns ``{system: number_of_secrets_found}``. Raises :class:`KvEnvError` when the
    vault is unreadable or a system has no secrets, unless ``optional`` (or
    ``KVENV_OPTIONAL=1``), in which case a warning is emitted instead.
    """
    if dotenv:
        _apply_dotenv(repo_root(start))
    if optional is None:
        optional = os.environ.get("KVENV_OPTIONAL", "").lower() in ("1", "true", "yes")
    raw = system or os.environ.get("KVENV_SYSTEM", "")
    systems = [s.strip() if s.strip() == WILDCARD else s.strip().lower() for s in raw.split(",") if s.strip()]
    try:
        if not systems:
            raise KvEnvError(
                "kvenv: KVENV_SYSTEM is not set. Add `KVENV_SYSTEM=<app name>` and "
                "`KVENV_VAULT=<vault name>` to the committed .env."
            )
        v = vault_name(env, vault)
        counts = _fetch(systems, v)
        missing = [s for s, n in counts.items() if n == 0]
        if missing:
            raise KvEnvError(
                f"kvenv: no secrets named '{missing[0]}-*' in {v}. Either the app name is wrong "
                f"or nothing has been pushed yet (`python -m kvenv push --system {missing[0]} --vault {v} --from .env`)."
            )
        return counts
    except KvEnvError as e:
        if optional:
            warnings.warn(str(e), stacklevel=2)
            return {}
        raise


def _print_status(counts: dict[str, int], v: str) -> None:
    for s, n in counts.items():
        print(f"kvenv: {s}: {n} secret(s) from {v}", file=sys.stderr)
