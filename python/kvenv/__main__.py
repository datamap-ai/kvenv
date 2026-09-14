"""kvenv CLI — push secrets into a vault, list what is there. Never prints values.

  python -m kvenv push --system kyriba --env test --from .env --repo kyriba
  python -m kvenv push --system netsuite --env prod --file PRIVATE_PEM=private.pem \
        --content-type application/x-pem-file
  python -m kvenv push --system slack --env test --from-json local.settings.json --json-path Values
  python -m kvenv list --system kyriba --env test
  python -m kvenv check            # what would load() do here, without loading

`push` writes one secret per variable, named ``<system>-<KEY>``, tagged
``system``, ``var``, ``repo``, ``owner``, ``rotated``, ``source`` (+ ``public=true`` when
``--public`` is given). ``--only A,B`` restricts which keys are pushed; ``--skip`` excludes.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

from . import DEFAULT_ENV, WILDCARD, KvEnvError, _parse_dotenv, repo_root, secret_name, var_name, vault_name

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def _client(vault: str):
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    cred = DefaultAzureCredential(exclude_interactive_browser_credential=True)
    return SecretClient(vault_url=f"https://{vault}.vault.azure.net/", credential=cred)


def _owner() -> str:
    env_owner = os.environ.get("KVENV_OWNER")
    if env_owner:
        return env_owner
    try:
        out = subprocess.run(
            ["az", "account", "show", "--query", "user.name", "-o", "tsv"],
            capture_output=True, text=True, timeout=10, check=False, shell=(os.name == "nt"),
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return getpass.getuser()


def _collect(args: argparse.Namespace) -> dict[str, tuple[str, str]]:
    """Return {VAR: (value, source)}. Values are held only in memory."""
    items: dict[str, tuple[str, str]] = {}
    if args.from_env:
        p = Path(args.from_env)
        for k, v in _parse_dotenv(p).items():
            items[k] = (v, str(p))
    if args.from_json:
        p = Path(args.from_json)
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        for seg in (args.json_path.split(".") if args.json_path else []):
            data = data[seg]
        for k, v in data.items():
            if isinstance(v, (str, int, float, bool)):
                items[str(k)] = (str(v), f"{p}#{args.json_path or ''}")
    for spec in args.file or []:
        if "=" not in spec:
            sys.exit(f"--file expects VAR=path, got {spec!r}")
        k, path = spec.split("=", 1)
        items[k] = (Path(path).read_text(encoding="utf-8"), path)
    only = {s.strip() for s in (args.only or "").split(",") if s.strip()}
    skip = {s.strip() for s in (args.skip or "").split(",") if s.strip()}
    if only:
        items = {k: v for k, v in items.items() if k in only}
        missing = only - set(items)
        if missing:
            sys.exit(f"--only names not found in source: {sorted(missing)}")
    if skip:
        items = {k: v for k, v in items.items() if k not in skip}
    # Never push the pointer variables themselves.
    for k in ("KVENV_SYSTEM", "KVENV_ENV", "KVENV_VAULT", "KVENV_OPTIONAL"):
        items.pop(k, None)
    return items


def cmd_push(args: argparse.Namespace) -> int:
    vault = vault_name(args.env, args.vault)
    items = _collect(args)
    if not items:
        sys.exit("nothing to push (no variables after filtering)")
    client = _client(vault)
    # Loading is prefix-based (`<system>-*`), so one system name must never be a prefix of another
    # ("boomi" would swallow "boomi-embedkit-*"). Refuse the push when that would happen.
    existing = {(p.tags or {}).get("system") for p in client.list_properties_of_secrets()} - {None, args.system}
    clash = [] if args.system == WILDCARD else [s for s in existing if s.startswith(args.system + "-") or args.system.startswith(s + "-")]
    if clash and not args.force:
        sys.exit(f"system {args.system!r} collides by prefix with existing system(s) {sorted(clash)} in {vault}; "
                 "pick a name that is not a prefix of / prefixed by another system, or pass --force")
    today = _dt.date.today().isoformat()
    owner = _owner()
    for var, (value, source) in items.items():
        name = secret_name(args.system, var)
        tags = {
            **({} if args.system == WILDCARD else {"system": args.system}),
            "var": var,
            "owner": owner,
            "rotated": today,
            "source": source.replace("\\", "/"),
        }
        if args.repo:
            tags["repo"] = args.repo
        if args.public:
            tags["public"] = "true"
        if args.dry_run:
            print(f"would set {vault}/{name}  (var={var}, {len(value)} chars)")
            continue
        client.set_secret(name, value, content_type=args.content_type, tags=tags)
        print(f"set {vault}/{name}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    vault = vault_name(args.env, args.vault)
    client = _client(vault)
    rows = []
    for p in client.list_properties_of_secrets():
        if args.system and var_name(args.system, p.name) is None:
            continue
        t = p.tags or {}
        rows.append((p.name, t.get("system", ""), t.get("repo", ""), t.get("rotated", ""), "public" if t.get("public") == "true" else ""))
    if not rows:
        print(f"no secrets{' for ' + args.system if args.system else ''} in {vault}")
        return 1
    w = max(len(r[0]) for r in rows)
    print(f"{vault}:")
    for name, system, repo, rotated, pub in sorted(rows):
        print(f"  {name.ljust(w)}  system={system}  repo={repo}  rotated={rotated} {pub}".rstrip())
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    root = repo_root()
    merged: dict[str, str] = {}
    for f in (".env", ".env.local"):
        merged.update(_parse_dotenv(root / f))
    system = os.environ.get("KVENV_SYSTEM") or merged.get("KVENV_SYSTEM")
    env = os.environ.get("KVENV_ENV") or merged.get("KVENV_ENV") or DEFAULT_ENV
    override = os.environ.get("KVENV_VAULT") or merged.get("KVENV_VAULT")
    print(f"repo root : {root}")
    print(f"system    : {system or '(missing)'}")
    print(f"env       : {env}")
    try:
        print(f"vault     : {vault_name(env, override)}")
    except KvEnvError as e:
        print(f"vault     : {e}")
        return 2
    if not system:
        print("KVENV_SYSTEM is missing — run the general-kvenv-setup skill.")
        return 2
    args.system, args.env, args.vault = system.split(",")[0].strip(), env, override
    return cmd_list(args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kvenv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("push", help="write variables into the vault as <system>-<KEY>")
    p.add_argument("--system", required=True)
    p.add_argument("--env", default=None, help="test (default) or prod")
    p.add_argument("--vault", default=None, help="override vault name")
    p.add_argument("--from", dest="from_env", help=".env-style file to read")
    p.add_argument("--from-json", help="JSON file to read (e.g. local.settings.json)")
    p.add_argument("--json-path", default="", help="dotted path inside the JSON, e.g. Values")
    p.add_argument("--file", action="append", help="VAR=path : push a whole file as one secret")
    p.add_argument("--only", help="comma-separated variable names to include")
    p.add_argument("--skip", help="comma-separated variable names to exclude")
    p.add_argument("--repo", help="consuming repo name(s) for the repo tag")
    p.add_argument("--public", action="store_true", help="tag public=true (non-secret config)")
    p.add_argument("--content-type", default="text/plain")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="push even if the system name collides by prefix")
    p.set_defaults(fn=cmd_push)

    l = sub.add_parser("list", help="list secret names (never values)")
    l.add_argument("--system")
    l.add_argument("--env", default=None)
    l.add_argument("--vault", default=None)
    l.set_defaults(fn=cmd_list)

    c = sub.add_parser("check", help="show what load() would use from this directory")
    c.set_defaults(fn=cmd_check)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KvEnvError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
