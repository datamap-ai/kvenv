export declare const VAULT_PATTERN: string;
export declare const ENVS: string[];
export declare const DEFAULT_ENV: string;
export declare const WILDCARD: string;

export declare class KvEnvError extends Error {}

export declare function vaultName(env?: string, override?: string): string;
export declare function secretName(system: string, varName: string): string;
export declare function varName(system: string, name: string): string | null;
export declare function repoRoot(start?: string): string;

export interface LoadKvEnvOptions {
  /** Comma-separated system names. Default: process.env.KVENV_SYSTEM */
  system?: string;
  /** "test" | "prod". Default: process.env.KVENV_ENV ?? "test" */
  env?: string;
  /** Vault name override. Default: process.env.KVENV_VAULT ?? kv-datamap-ops-<env> */
  vault?: string;
  /** Apply .env.local and .env from the repo root first. Default true */
  dotenv?: boolean;
  /** Warn instead of throwing on vault failure. Default: KVENV_OPTIONAL=1 */
  optional?: boolean;
  /** Directory to start the repo-root search from. Default: process.cwd() */
  start?: string;
}

/** Populates process.env; resolves to secrets-found-per-system. */
export declare function loadKvEnv(opts?: LoadKvEnvOptions): Promise<Record<string, number>>;
export default loadKvEnv;
