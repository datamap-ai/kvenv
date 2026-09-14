/**
 * kvenv — load a repo's secrets from the environment's Azure Key Vault at runtime.
 *
 * Same contract as the Python and PowerShell ports:
 *   KVENV_SYSTEM   required; one or more comma-separated system names
 *   KVENV_ENV      "test" (default) or "prod" — picks the vault
 *   KVENV_VAULT    optional override; otherwise kv-datamap-ops-<env>
 *   KVENV_OPTIONAL "1" downgrades vault failures to a console warning
 *
 * Secret names are `<system>-<KEY>` where KEY is the env var name with `_` → `-`.
 * `loadKvEnv()` applies .env.local then .env from the repo root (never overwriting a
 * variable already set), then fetches every secret whose name starts with `<system>-`
 * and sets the variable if still unset. Process env always wins.
 *
 * Auth: DefaultAzureCredential — `az login` locally, managed identity deployed,
 * service-principal env vars in CI. Values are never logged.
 *
 * Vite: `await loadKvEnv()` at the top of vite.config.ts. Vite forwards any VITE_*
 * present in process.env into import.meta.env, so the browser never touches the vault.
 */
import { readFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';

export const VAULT_PATTERN = 'kv-datamap-ops-{env}';
export const ENVS = ['test', 'prod'];
export const DEFAULT_ENV = 'test';

export class KvEnvError extends Error {}

export function vaultName(env, override) {
  override = override ?? process.env.KVENV_VAULT;
  if (override) return override;
  env = (env ?? process.env.KVENV_ENV ?? DEFAULT_ENV).toLowerCase();
  if (!ENVS.includes(env)) throw new KvEnvError(`KVENV_ENV=${env} is not one of ${ENVS.join(', ')}`);
  return VAULT_PATTERN.replace('{env}', env);
}

export function secretName(system, varName) {
  return `${system}-${varName.replace(/_/g, '-')}`;
}

export function varName(system, name) {
  const prefix = `${system}-`;
  if (!name.toLowerCase().startsWith(prefix.toLowerCase())) return null;
  return name.slice(prefix.length).replace(/-/g, '_');
}

export function repoRoot(start = process.cwd()) {
  let p = resolve(start);
  for (;;) {
    if (existsSync(join(p, '.git')) || existsSync(join(p, '.env'))) return p;
    const parent = dirname(p);
    if (parent === p) return resolve(start);
    p = parent;
  }
}

function parseDotenv(path) {
  const out = {};
  if (!existsSync(path)) return out;
  const text = readFileSync(path, 'utf8').replace(/^﻿/, '');
  for (const raw of text.split(/\r?\n/)) {
    let line = raw.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    if (line.startsWith('export ')) line = line.slice(7);
    const i = line.indexOf('=');
    const k = line.slice(0, i).trim();
    let v = line.slice(i + 1).trim();
    if (v.length >= 2 && v[0] === v[v.length - 1] && (v[0] === '"' || v[0] === "'")) v = v.slice(1, -1);
    if (k) out[k] = v;
  }
  return out;
}

function applyDotenv(root) {
  // .env.local (per-machine, gitignored) beats .env (committed); both lose to process.env.
  for (const f of ['.env.local', '.env']) {
    for (const [k, v] of Object.entries(parseDotenv(join(root, f)))) {
      if (process.env[k] === undefined) process.env[k] = v;
    }
  }
}

function classify(err, vault) {
  const name = err?.name ?? '';
  const status = err?.statusCode ?? err?.status;
  if (name === 'CredentialUnavailableError' || name === 'AuthenticationRequiredError' || name === 'AggregateAuthenticationError') {
    return new KvEnvError(`kvenv: not signed in to Azure, cannot read ${vault}. Run \`az login\` (or set AZURE_CLIENT_ID/AZURE_TENANT_ID/AZURE_CLIENT_SECRET in CI).`);
  }
  if (status === 403) {
    return new KvEnvError(`kvenv: signed in, but this identity is denied on ${vault}. \`az login\` will not help. Ask for the 'Key Vault Secrets User' role on that vault for this identity.`);
  }
  if (err?.code === 'ENOTFOUND' || err?.code === 'ECONNREFUSED' || name === 'RestError' && !status) {
    return new KvEnvError(`kvenv: vault ${vault} is unreachable (DNS/network). Check KVENV_ENV / KVENV_VAULT.`);
  }
  return new KvEnvError(`kvenv: failed reading ${vault}: ${name}: ${err?.message ?? err}`);
}

async function fetchSecrets(systems, vault) {
  const { DefaultAzureCredential } = await import('@azure/identity');
  const { SecretClient } = await import('@azure/keyvault-secrets');
  const client = new SecretClient(`https://${vault}.vault.azure.net/`, new DefaultAzureCredential());
  const counts = Object.fromEntries(systems.map((s) => [s, 0]));
  try {
    const names = [];
    for await (const p of client.listPropertiesOfSecrets()) {
      if (p.enabled !== false) names.push(p.name);
    }
    for (const system of systems) {
      for (const name of names) {
        const v = varName(system, name);
        if (v === null) continue;
        counts[system] += 1;
        if (process.env[v] !== undefined) continue;
        const s = await client.getSecret(name);
        process.env[v] = s.value ?? '';
      }
    }
  } catch (err) {
    throw classify(err, vault);
  }
  return counts;
}

/**
 * @param {{system?: string, env?: string, vault?: string, dotenv?: boolean, optional?: boolean, start?: string}} [opts]
 * @returns {Promise<Record<string, number>>} secrets found per system
 */
export async function loadKvEnv(opts = {}) {
  const { system, env, vault, dotenv = true, start } = opts;
  if (dotenv) applyDotenv(repoRoot(start));
  const optional = opts.optional ?? ['1', 'true', 'yes'].includes((process.env.KVENV_OPTIONAL ?? '').toLowerCase());
  const raw = system ?? process.env.KVENV_SYSTEM ?? '';
  const systems = raw.split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);
  try {
    if (systems.length === 0) {
      throw new KvEnvError('kvenv: KVENV_SYSTEM is not set. Add `KVENV_SYSTEM=<app name>` and `KVENV_ENV=test` to the committed .env (run the general-kvenv-setup skill).');
    }
    const v = vaultName(env, vault);
    const counts = await fetchSecrets(systems, v);
    const missing = Object.entries(counts).filter(([, n]) => n === 0).map(([s]) => s);
    if (missing.length) {
      throw new KvEnvError(`kvenv: no secrets named '${missing[0]}-*' in ${v}. Either the app name is wrong or nothing has been pushed yet (python -m kvenv push --system ${missing[0]} --env ${env ?? process.env.KVENV_ENV ?? DEFAULT_ENV} --from .env).`);
    }
    return counts;
  } catch (err) {
    if (optional && err instanceof KvEnvError) {
      console.warn(err.message);
      return {};
    }
    throw err;
  }
}

export default loadKvEnv;
