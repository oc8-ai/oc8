# Start a safe local oc8 evaluation instance on Windows.
# It creates only missing local secrets and never resets containers or volumes.
#
# Interactive by default: asks which operating mode to run (Community, Demo,
# or Dev) and, for Community/Demo, whether you already run your own reverse
# proxy (binds oc8 to loopback, OC8_QUICKSTART_OWN_PROXY/_PROXY_PORT/
# _EXTERNAL_URL) or want oc8's own Caddy to handle it (an optional custom
# domain for automatic HTTPS, OC8_QUICKSTART_DOMAIN). All can be preset for
# scripted/non-interactive runs via $env:OC8_QUICKSTART_MODE
# (community|demo|dev) and the vars above -- when stdin isn't a terminal and
# none are set, it falls back to the previous non-interactive default:
# Community mode, no proxy, no domain.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Fail([string]$Message) {
  Write-Error "Quickstart stopped: $Message"
  exit 1
}

function New-HexSecret([int]$ByteCount) {
  $bytes = New-Object byte[] $ByteCount
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  return ([Convert]::ToHexString($bytes)).ToLowerInvariant()
}

function New-Base64Secret([int]$ByteCount) {
  $bytes = New-Object byte[] $ByteCount
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  return [Convert]::ToBase64String($bytes)
}

function Get-EnvValue([string]$Key) {
  if (-not (Test-Path ".env")) { return "" }
  $line = Get-Content ".env" | Where-Object { $_ -match "^$([regex]::Escape($Key))=" } | Select-Object -First 1
  if ($null -eq $line) { return "" }
  return $line.Substring($Key.Length + 1)
}

# Unconditional overwrite (or append if the key is missing entirely). Only
# used below for values the user just typed at a prompt -- everything else
# in this script goes through Set-EnvValueIfMissing so a rerun never
# clobbers what's already in .env.
function Set-EnvValue([string]$Key, [string]$Value) {
  $lines = @(Get-Content ".env")
  $pattern = "^$([regex]::Escape($Key))="
  $found = $false
  $updated = foreach ($line in $lines) {
    if ($line -match $pattern) {
      $found = $true
      "$Key=$Value"
    } else {
      $line
    }
  }
  if (-not $found) { $updated += "$Key=$Value" }
  Set-Content -Path ".env" -Value $updated -Encoding utf8
}

function Set-EnvValueIfMissing([string]$Key, [string]$Value) {
  if (-not [string]::IsNullOrWhiteSpace((Get-EnvValue $Key))) { return }
  Set-EnvValue $Key $Value
  Write-Host "Generated $Key in .env."
}

function Test-Interactive {
  return -not [System.Console]::IsInputRedirected
}

function Resolve-ContainerRuntime {
  if ($env:OC8_CONTAINER_RUNTIME) { return $env:OC8_CONTAINER_RUNTIME }
  $fromEnv = Get-EnvValue "OC8_CONTAINER_RUNTIME"
  if (-not [string]::IsNullOrWhiteSpace($fromEnv)) { return $fromEnv }
  if (Get-Command docker -ErrorAction SilentlyContinue) { return "docker" }
  if (Get-Command podman -ErrorAction SilentlyContinue) { return "podman" }
  Fail "Neither Docker nor Podman was found. Install one of them first."
}

$ContainerRuntime = Resolve-ContainerRuntime
if ($ContainerRuntime -notin @("docker", "podman")) {
  Fail "OC8_CONTAINER_RUNTIME must be 'docker' or 'podman', got '$ContainerRuntime'."
}

if ($ContainerRuntime -eq "docker") {
  if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker Desktop is required. Install it, enable WSL2 integration if applicable, then start Docker Desktop."
  }
  try { docker compose version | Out-Null } catch { Fail "Docker Compose v2 is required." }
  try { docker info | Out-Null } catch { Fail "Docker Desktop is installed but not running." }
  $ComposeCmd = @("docker", "compose")
} else {
  if (-not (Get-Command podman -ErrorAction SilentlyContinue)) {
    Fail "Podman is required (OC8_CONTAINER_RUNTIME=podman). Install Podman Desktop first."
  }
  try {
    podman compose version | Out-Null
    $ComposeCmd = @("podman", "compose")
  } catch {
    Fail "Podman Compose is required: install the 'podman compose' plugin (Podman v4+)."
  }
  try { podman info | Out-Null } catch { Fail "Podman is installed but not ready. Run 'podman machine start' first." }
}

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created .env from .env.example."
} else {
  Write-Host "Using existing .env; non-empty values will not be changed."
}

# --- Operating mode: Community (empty, real password setup), Demo (seeded
# ACME showcase behind real password login), or Dev (seeded ACME + instant
# unauthenticated login, localhost only). Each maps to a docker-compose.yml
# + override combination -- see docker-compose.demo.yml/docker-compose.dev.yml
# for exactly what each one changes.
$Mode = $env:OC8_QUICKSTART_MODE
if ([string]::IsNullOrWhiteSpace($Mode)) {
  if (Test-Interactive) {
    Write-Host "`nWhich operating mode do you want to run?"
    Write-Host "  1) Prod - empty instance, real password setup (recommended)"
    Write-Host "  2) Demo      - seeded bilingual ACME showcase data behind a real password login"
    Write-Host "  3) Dev       - seeded ACME data + instant unauthenticated login (localhost only, never expose)"
    $modeChoice = Read-Host "Choice [1]"
    switch ($modeChoice) {
      { $_ -in @("1", "") } { $Mode = "community" }
      "2" { $Mode = "demo" }
      "3" { $Mode = "dev" }
      default { Fail "Invalid choice: $modeChoice" }
    }
  } else {
    $Mode = "community"
  }
}
if ($Mode -notin @("community", "demo", "dev")) {
  Fail "OC8_QUICKSTART_MODE must be 'community', 'demo', or 'dev', got '$Mode'."
}

$ComposeFiles = @("-f", "docker-compose.yml")
switch ($Mode) {
  "demo" {
    $ComposeFiles += @("-f", "docker-compose.demo.yml")
    Write-Host "Demo mode: seeded ACME showcase data behind a real password login."
  }
  "dev" {
    $ComposeFiles += @("-f", "docker-compose.dev.yml")
    Write-Host "Dev mode: instant unauthenticated admin login. LOCALHOST ONLY -- never expose this to a network."
  }
}

# --- Optional custom domain for automatic HTTPS. Skipped in Dev mode: that
# mode's login has no password, so it must never be reachable off localhost.
# Always asks interactively, with no default -- a checkout reused for a
# different host/mode must not silently inherit or suggest an old domain.
if ($Mode -ne "dev") {
  $ownProxy = $env:OC8_QUICKSTART_OWN_PROXY
  if ([string]::IsNullOrWhiteSpace($ownProxy) -and (Test-Interactive)) {
    $ownProxy = Read-Host "Already running your own reverse proxy on this host (nginx, Traefik, another Caddy)? [y/N]"
  }
  if ($ownProxy -match "^[Yy]") {
    # Your proxy owns the domain and the TLS certificate; oc8's own Caddy
    # must not also try to grab :80/:443 or provision one. Bind it to
    # loopback on a plain port instead, and hand back what to point at.
    $proxyPort = $env:OC8_QUICKSTART_PROXY_PORT
    if ([string]::IsNullOrWhiteSpace($proxyPort) -and (Test-Interactive)) {
      $proxyPort = Read-Host "Localhost port for your proxy to reach oc8 on (leave empty for 8080)"
    }
    if ([string]::IsNullOrWhiteSpace($proxyPort)) { $proxyPort = "8080" }
    Set-EnvValue "OC8_HTTP_PORT" "127.0.0.1:$proxyPort"
    Set-EnvValue "OC8_DOMAIN" ""
    $externalUrl = $env:OC8_QUICKSTART_EXTERNAL_URL
    if ([string]::IsNullOrWhiteSpace($externalUrl) -and (Test-Interactive)) {
      $externalUrl = Read-Host "Externally visible URL your proxy serves this under (e.g. https://oc8.example.com)"
    }
    if (-not [string]::IsNullOrWhiteSpace($externalUrl)) {
      Set-EnvValue "OC8_FRONTEND_BASE_URL" $externalUrl
    }
    Write-Host "Point your reverse proxy at 127.0.0.1:$proxyPort (plain HTTP) -- oc8's own Caddy will not attempt to obtain a certificate."
  } else {
    $existingDomain = Get-EnvValue "OC8_DOMAIN"
    $domain = $env:OC8_QUICKSTART_DOMAIN
    if ([string]::IsNullOrWhiteSpace($domain) -and (Test-Interactive)) {
      $domain = Read-Host "Custom domain for automatic HTTPS (leave empty for plain HTTP)"
    } elseif ([string]::IsNullOrWhiteSpace($domain) -and -not (Test-Interactive)) {
      $domain = $existingDomain
    }
    if (-not [string]::IsNullOrWhiteSpace($domain)) {
      if ($domain -ne $existingDomain) {
        Write-Host "Point DNS for $domain at this host before continuing, or certificate issuance will fail."
      }
      Set-EnvValue "OC8_DOMAIN" $domain
      $currentBaseUrl = Get-EnvValue "OC8_FRONTEND_BASE_URL"
      if ([string]::IsNullOrWhiteSpace($currentBaseUrl) -or $currentBaseUrl -eq "http://localhost") {
        Set-EnvValue "OC8_FRONTEND_BASE_URL" "https://$domain"
      } else {
        Write-Host "Note: OC8_FRONTEND_BASE_URL is already set to $currentBaseUrl -- leaving it, but it should probably be https://$domain."
      }
    } elseif (-not [string]::IsNullOrWhiteSpace($existingDomain)) {
      Set-EnvValue "OC8_DOMAIN" ""
      Write-Host "Cleared OC8_DOMAIN -- plain HTTP."
    }
  }
}

# --- Demo mode's password login (compose.demo.yml requires OC8_DEMO_PASSWORD).
$DemoEmail = "demo@oc8.ai"
if ($Mode -eq "demo") {
  $existingDemoPassword = Get-EnvValue "OC8_DEMO_PASSWORD"
  if ([string]::IsNullOrWhiteSpace($existingDemoPassword)) {
    $demoPassword = $env:OC8_QUICKSTART_DEMO_PASSWORD
    if ([string]::IsNullOrWhiteSpace($demoPassword) -and (Test-Interactive)) {
      $secure = Read-Host "Demo login password (min 8 chars, leave empty to auto-generate)" -AsSecureString
      $demoPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    }
    if ([string]::IsNullOrWhiteSpace($demoPassword)) {
      $demoPassword = New-Base64Secret 18
      Write-Host "Generated demo password: $demoPassword"
      Write-Host "(save this now -- it is only shown this once)"
    } elseif ($demoPassword.Length -lt 8) {
      Fail "Demo password must be at least 8 characters."
    }
    Set-EnvValue "OC8_DEMO_PASSWORD" $demoPassword
  }
  $envDemoEmail = Get-EnvValue "OC8_DEMO_EMAIL"
  if (-not [string]::IsNullOrWhiteSpace($envDemoEmail)) { $DemoEmail = $envDemoEmail }
  Write-Host "Demo sign-in address: $DemoEmail"
}

Set-EnvValueIfMissing "OC8_JWT_SECRET" (New-HexSecret 32)
Set-EnvValueIfMissing "OC8_SECRET_KEK" (New-Base64Secret 32)
Set-EnvValueIfMissing "POSTGRES_PASSWORD" (New-HexSecret 24)
Set-EnvValueIfMissing "OC8_SANDBOX_PROVISIONER_TOKEN" (New-HexSecret 32)
Set-EnvValueIfMissing "OC8_CONTAINER_RUNTIME" $ContainerRuntime

if ($ContainerRuntime -eq "podman") {
  $PodmanSocket = (podman info --format '{{.Host.RemoteSocket.Path}}' 2>$null)
  if ([string]::IsNullOrWhiteSpace($PodmanSocket)) {
    Fail "Could not determine the Podman API socket path (podman info --format failed)."
  }
  Set-EnvValueIfMissing "OC8_CONTAINER_SOCKET" $PodmanSocket
}

Write-Host "`nBuilding and starting oc8 ($Mode mode)…"
if ($ContainerRuntime -eq "docker") {
  docker compose @ComposeFiles up -d --build
} else {
  & $ComposeCmd[0] $ComposeCmd[1] @ComposeFiles up -d --build
}

$domainInEnv = Get-EnvValue "OC8_DOMAIN"
if (-not [string]::IsNullOrWhiteSpace($domainInEnv)) {
  $url = "https://$domainInEnv"
} else {
  $portMapping = Get-EnvValue "OC8_HTTP_PORT"
  if ([string]::IsNullOrWhiteSpace($portMapping)) { $portMapping = "80" }
  if ($portMapping.Contains(":")) {
    $urlHost, $port = $portMapping -split ":", 2
    if ($urlHost -eq "0.0.0.0" -or $urlHost -eq "::") { $urlHost = "127.0.0.1" }
    $url = "http://${urlHost}:$port"
  } else {
    $url = "http://localhost"
    if ($portMapping -ne "80") { $url = "$url`:$portMapping" }
  }
}

Write-Host "`nWaiting for $url/health …"
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "$url/health"
    if ($response.StatusCode -eq 200) { $ready = $true; break }
  } catch {}
  Start-Sleep -Seconds 2
}
if (-not $ready) {
  & $ComposeCmd[0] $ComposeCmd[1] @ComposeFiles ps
  Fail "oc8 did not become healthy in time. Inspect: $($ComposeCmd -join ' ') $($ComposeFiles -join ' ') logs -f backend"
}

Write-Host "`n✓ oc8 ($Mode mode) is running at $url"
switch ($Mode) {
  "community" { Write-Host "Next: open the URL and create the local administrator account." }
  "demo" { Write-Host "Next: sign in as $DemoEmail with the password shown above (or already in .env)." }
  "dev" { Write-Host "Next: open the URL -- dev-login signs you in as org_admin with no password." }
}
Write-Host "Logs: $($ComposeCmd -join ' ') logs -f backend"
Write-Host "Stop later (keeps data): $($ComposeCmd -join ' ') stop"
if ($Mode -ne "community") {
  Write-Host "Restart later in the same mode: $($ComposeCmd -join ' ') $($ComposeFiles -join ' ') up -d"
}
