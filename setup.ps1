# R1 -> Hermes Bridge -- x1 Windows Setup
# =========================================
# Run this on your x1 laptop. It will:
#   1. Install Python dependencies (websockets, httpx, qrcode)
#   2. Detect your WiFi IP address
#   3. Generate a QR code the R1 can scan
#   4. Start the bridge server
#
# Usage (in PowerShell as Administrator):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\setup.ps1

param(
    [switch]$NoStart,
    [switch]$Help
)

if ($Help) {
    Write-Host ""
    Write-Host "R1 -> Hermes Bridge -- x1 Setup"
    Write-Host "================================"
    Write-Host "Connects your Rabbit R1 to Hermes Agent via this laptop."
    Write-Host ""
    Write-Host "Options:"
    Write-Host "  -NoStart    Generate QR code only, don't start the bridge"
    Write-Host "  -Help       Show this help"
    Write-Host ""
    exit 0
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgePort = 18790
$HealthPort = 18788

Write-Host ""
Write-Host "=== R1 -> Hermes Bridge (x1 Setup) ===" -ForegroundColor Cyan
Write-Host ""

# ---- Check Python (no Stop on error yet -- we handle stderr manually) ----
$oldEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"

try {
    $pyVer = & python --version 2>&1
    Write-Host "[OK] Python: $pyVer" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Python not found! Install from https://python.org" -ForegroundColor Red
    Write-Host "       Make sure to check 'Add Python to PATH' during install."
    pause
    exit 1
}

# ---- Install Dependencies ----
Write-Host "[..] Checking Python dependencies..." -ForegroundColor Yellow
$deps = @("websockets", "httpx", "qrcode", "PIL")
$missing = @()
foreach ($dep in $deps) {
    $ok = $false
    try {
        $result = & python -c "import $dep; print('ok')" 2>&1 | Out-String
        if ($result.Trim() -eq "ok") { $ok = $true }
    } catch {}
    if (-not $ok) { $missing += $dep }
}
if ($missing.Count -gt 0) {
    Write-Host "[..] Installing: $($missing -join ', ')..." -ForegroundColor Yellow
    foreach ($dep in $missing) {
        $pipName = $dep
        if ($dep -eq "PIL") { $pipName = "pillow" }
        & python -m pip install $pipName --quiet 2>&1 | Out-Null
        Write-Host "     [OK] $dep" -ForegroundColor Green
    }
} else {
    Write-Host "[OK] All dependencies found" -ForegroundColor Green
}
Write-Host ""

# Restore strict error handling for the rest of the script
$ErrorActionPreference = $oldEAP

# ---- Auth Token ----
$tokenFile = Join-Path $ScriptDir ".r1-auth-token"
if (Test-Path $tokenFile) {
    $token = (Get-Content $tokenFile -Raw).Trim()
}
if (-not $token -or $token.Length -eq 0) {
    $rng = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
    $bytes = New-Object byte[] 32
    $rng.GetBytes($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    $rng.Dispose()
    Set-Content -Path $tokenFile -Value $token -NoNewline
}
Write-Host "[OK] Auth token: $($token.Substring(0, [Math]::Min(16, $token.Length)))..." -ForegroundColor Blue
Write-Host ""

# ---- Detect WiFi IP (parse ipconfig -- works on all Windows versions) ----
$wifiIP = $null
try {
    $ipconfig = & ipconfig 2>$null | Out-String
    # Find all "IPv4 Address" lines and pick the first non-APIPA address
    $lines = $ipconfig -split "`r`n"
    $candidates = @()
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "IPv4 Address.*:\s*(\d+\.\d+\.\d+\.\d+)") {
            $ip = $matches[1]
            if ($ip -notmatch "^169\.254\." -and $ip -notmatch "^127\.") {
                $candidates += $ip
            }
        }
        # Also try the older "IP Address" format
        if ($lines[$i] -match "IP Address[^:]*:\s*(\d+\.\d+\.\d+\.\d+)") {
            $ip = $matches[1]
            if ($ip -notmatch "^169\.254\." -and $ip -notmatch "^127\.") {
                $candidates += $ip
            }
        }
    }
    # Prefer 192.168.x.x or 10.x.x.x (typical WiFi LAN)
    $lan = $candidates | Where-Object { $_ -match "^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)" }
    if ($lan) { $candidates = $lan }
    if ($candidates.Count -gt 0) {
        $wifiIP = $candidates[0]
    }
} catch {}

if (-not $wifiIP) {
    $wifiIP = "192.168.1.100"
    Write-Host "[WARN] Could not detect WiFi IP. Using $wifiIP" -ForegroundColor Yellow
    Write-Host "       If wrong, find your IP with: ipconfig" -ForegroundColor Yellow
    Write-Host "       Then edit .qr-payload.json and re-run: python qrgen.py r1-hermes-qr.png .qr-payload.json" -ForegroundColor Yellow
} else {
    Write-Host "[OK] WiFi IP: $wifiIP" -ForegroundColor Green
}
Write-Host ""

# ---- Firewall Rule ----
Write-Host "[..] Checking firewall..." -ForegroundColor Yellow
$ruleName = "R1 Hermes Bridge (Port $BridgePort)"
$existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
if (-not $existing) {
    try {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $BridgePort,$HealthPort -Action Allow -Profile Private,Public | Out-Null
        Write-Host "[OK] Firewall rule added for ports $BridgePort,$HealthPort" -ForegroundColor Green
    } catch {
        Write-Host "[WARN] Could not add firewall rule (run as Admin?). You may need to allow port $BridgePort manually." -ForegroundColor Yellow
    }
} else {
    Write-Host "[OK] Firewall rule already exists" -ForegroundColor Green
}
Write-Host ""

# ---- Generate QR Code ----
$payload = @{
    type     = "clawdbot-gateway"
    version  = 1
    ips      = @($wifiIP)
    port     = $BridgePort
    token    = $token
    protocol = "ws"
} | ConvertTo-Json -Compress

Write-Host "[..] QR payload: $payload" -ForegroundColor DarkGray
Write-Host ""

$qrPath = Join-Path $ScriptDir "r1-hermes-qr.png"
$payloadFile = Join-Path $ScriptDir ".qr-payload.json"
Set-Content -Path $payloadFile -Value $payload -NoNewline

$qrgen = Join-Path $ScriptDir "qrgen.py"
python $qrgen $qrPath $payloadFile

Remove-Item $payloadFile -ErrorAction SilentlyContinue

Write-Host "[OK] QR code saved: $qrPath" -ForegroundColor Green
Write-Host ""

# Open the image
Start-Process $qrPath

# ---- Connection Info ----
Write-Host "=====================================" -ForegroundColor Green
Write-Host " CONNECTION INFO" -ForegroundColor Green
Write-Host "=====================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Bridge:    ws://${wifiIP}:${BridgePort}" -ForegroundColor Cyan
Write-Host "  Health:    http://${wifiIP}:${HealthPort}" -ForegroundColor Cyan
Write-Host "  Hermes:    http://100.116.144.9:8642/v1 (via Tailscale)" -ForegroundColor DarkGray
Write-Host ""

# ---- Start Bridge ----
if (-not $NoStart) {
    Write-Host "[..] Starting bridge server..." -ForegroundColor Green
    Write-Host "     (Press Ctrl+C to stop)" -ForegroundColor DarkGray
    Write-Host ""

    $env:HERMES_API_URL = "http://100.116.144.9:8642/v1"
    $env:R1_AUTH_TOKEN = $token
    $env:R1_BRIDGE_PORT = $BridgePort

    python (Join-Path $ScriptDir "server-x1.py")
} else {
    Write-Host "To start the bridge:" -ForegroundColor Yellow
    Write-Host "  python server-x1.py" -ForegroundColor Cyan
    Write-Host ""
}
