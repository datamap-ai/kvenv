<#
.SYNOPSIS
  kvenv for PowerShell — load a repo's secrets from the environment's Azure Key Vault
  into the current process, using the Azure CLI (`az login`) for auth.

.DESCRIPTION
  Same contract as the Python and Node ports:
    KVENV_SYSTEM   required; comma-separated system names
    KVENV_ENV      test (default) | prod   — picks the vault
    KVENV_VAULT    optional override; otherwise kv-datamap-ops-<env>
    KVENV_OPTIONAL 1 → warn instead of throw

  Secret names are <system>-<KEY>; KEY is the env var name with _ → -.
  Import-KvEnv applies .env.local then .env from the repo root (never overwriting a
  variable already set), then sets every <system>-* secret still unset. Values are
  never written to the console.

.EXAMPLE
  Import-Module ./pwsh/KvEnv.psm1
  Import-KvEnv                                  # reads KVENV_* from .env
  Import-KvEnv -System verabricks-erp -Env test # explicit
  docker compose up
#>

$script:VaultPattern = 'kv-datamap-ops-{0}'
$script:Envs = @('test', 'prod')

function Get-KvEnvRepoRoot {
  param([string]$Start = (Get-Location).Path)
  $p = (Resolve-Path $Start).Path
  while ($true) {
    if ((Test-Path (Join-Path $p '.git')) -or (Test-Path (Join-Path $p '.env'))) { return $p }
    $parent = Split-Path $p -Parent
    if (-not $parent -or $parent -eq $p) { return (Resolve-Path $Start).Path }
    $p = $parent
  }
}

function Read-KvEnvDotenv {
  param([string]$Path)
  $out = @{}
  if (-not (Test-Path -LiteralPath $Path)) { return $out }
  foreach ($raw in Get-Content -LiteralPath $Path -Encoding UTF8) {
    $line = $raw.Trim()
    if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
    if ($line.StartsWith('export ')) { $line = $line.Substring(7) }
    $i = $line.IndexOf('=')
    $k = $line.Substring(0, $i).Trim()
    $v = $line.Substring($i + 1).Trim()
    if ($v.Length -ge 2 -and $v[0] -eq $v[-1] -and ($v[0] -eq '"' -or $v[0] -eq "'")) { $v = $v.Substring(1, $v.Length - 2) }
    if ($k) { $out[$k] = $v }
  }
  return $out
}

function Get-KvEnvVaultName {
  param([string]$Env, [string]$Override)
  if (-not $Override) { $Override = $env:KVENV_VAULT }
  if ($Override) { return $Override }
  if (-not $Env) { $Env = $env:KVENV_ENV }
  if (-not $Env) { $Env = 'test' }
  $Env = $Env.ToLower()
  if ($script:Envs -notcontains $Env) { throw "KVENV_ENV=$Env is not one of $($script:Envs -join ', ')" }
  return ($script:VaultPattern -f $Env)
}

function Import-KvEnv {
  [CmdletBinding()]
  param(
    [string]$System,
    [string]$Env,
    [string]$Vault,
    [switch]$NoDotenv,
    [switch]$Optional,
    [string]$Start = (Get-Location).Path
  )
  $root = Get-KvEnvRepoRoot -Start $Start
  if (-not $NoDotenv) {
    foreach ($f in '.env.local', '.env') {
      $kv = Read-KvEnvDotenv -Path (Join-Path $root $f)
      foreach ($k in $kv.Keys) {
        if (-not [Environment]::GetEnvironmentVariable($k)) { [Environment]::SetEnvironmentVariable($k, $kv[$k]) }
      }
    }
  }
  if (-not $Optional) { $Optional = ($env:KVENV_OPTIONAL -in @('1', 'true', 'yes')) }
  if (-not $System) { $System = $env:KVENV_SYSTEM }
  $systems = @(($System -split ',') | ForEach-Object { if ($_.Trim() -eq '*') { '*' } else { $_.Trim().ToLower() } } | Where-Object { $_ })
  try {
    if ($systems.Count -eq 0) {
      throw 'kvenv: KVENV_SYSTEM is not set. Add KVENV_SYSTEM=<app name> and KVENV_ENV=test to the committed .env (run the general-kvenv-setup skill).'
    }
    $vaultName = Get-KvEnvVaultName -Env $Env -Override $Vault
    $acct = az account show 2>$null
    if (-not $acct) { throw "kvenv: not signed in to Azure, cannot read $vaultName. Run ``az login``." }
    $listJson = az keyvault secret list --vault-name $vaultName --query '[?attributes.enabled].name' -o json 2>&1
    if ($LASTEXITCODE -ne 0) {
      if ("$listJson" -match 'Forbidden|403') { throw "kvenv: signed in, but this identity is denied on $vaultName. Ask for 'Key Vault Secrets User' on that vault." }
      throw "kvenv: could not list $vaultName : $listJson"
    }
    $names = @($listJson | ConvertFrom-Json)
    $result = @{}
    foreach ($s in $systems) {
      $prefix = if ($s -eq '*') { '' } else { "$s-" }
      $count = 0
      foreach ($n in $names) {
        if ($prefix -and -not $n.ToLower().StartsWith($prefix)) { continue }
        $count++
        $var = $n.Substring($prefix.Length).Replace('-', '_')
        if ([Environment]::GetEnvironmentVariable($var)) { continue }
        $val = az keyvault secret show --vault-name $vaultName --name $n --query value -o tsv 2>$null
        if ($LASTEXITCODE -eq 0) { [Environment]::SetEnvironmentVariable($var, $val) }
      }
      $result[$s] = $count
      if ($count -eq 0) { throw "kvenv: no secrets named '$s-*' in $vaultName. Wrong app name, or nothing pushed yet." }
    }
    return $result
  } catch {
    if ($Optional) { Write-Warning $_.Exception.Message; return @{} }
    throw
  }
}

Export-ModuleMember -Function Import-KvEnv, Get-KvEnvVaultName, Get-KvEnvRepoRoot
