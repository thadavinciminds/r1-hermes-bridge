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

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgePort = 18790
$HealthPort = 18788

Write-Host ""
Write-Host "=== R1 -> Hermes Bridge (x1 Setup) ===" -ForegroundColor Cyan
Write-Host ""

# ---- Check Python ----
try {
    $pyVer = python --version 2>&1
    Write-Host "[OK] Python: $pyVer" -ForegroundColor Green
} catch {
    Write-Host "[FAIL] Python not found! Install from https://python.org" -ForegroundColor Red
    Write-Host "       Make sure to check 'Add Python to PATH' during install."
    pause
    exit 1
}

# ---- Install Dependencies ----
Write-Host "[..] Checking Python dependencies..." -ForegroundColor Yellow
$deps = @("websockets", "httpx", "qrcode")
$missing = @()
foreach ($dep in $deps) {
    $result = python -c "import $dep; print('ok')" 2>&1
    if ($result -ne "ok") {
        $missing += $dep
    }
}
if ($missing.Count -gt 0) {
    Write-Host "[..] Installing: $($missing -join ', ')..." -ForegroundColor Yellow
    foreach ($dep in $missing) {
        python -m pip install $dep --quiet
        Write-Host "     [OK] $dep" -ForegroundColor Green
    }
} else {
    Write-Host "[OK] All dependencies found" -ForegroundColor Green
}
Write-Host ""

# ---- Auth Token ----
$tokenFile = Join-Path $ScriptDir ".r1-auth-token"
if (Test-Path $tokenFile) {
    $token = (Get-Content $tokenFile -Raw).Trim()
}
if (-not $token -or $token.Length -eq 0) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    Set-Content -Path $tokenFile -Value $token -NoNewline
}
Write-Host "[OK] Auth token: $($token.Substring(0, [Math]::Min(16, $token.Length)))..." -ForegroundColor Blue
Write-Host ""

# ---- Detect WiFi IP ----
$wifiIP = $null
try {
    $allIPs = Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
        $_.IPAddress -like "192.168.*" -or
        $_.IPAddress -like "10.*" -or
        $_.IPAddress -like "172.16.*"
    } | Sort-Object -Property InterfaceMetric

    if ($allIPs.Count -gt 0) {
        $wifi = $allIPs | Where-Object { $_.InterfaceAlias -match "wi-?fi|wlan|wireless" }
        if ($wifi) { $allIPs = $wifi }
        $wifiIP = ($allIPs | Select-Object -First 1).IPAddress
    }
} catch {}

if (-not $wifiIP) {
    $wifiIP = "192.168.1.100"
    Write-Host "[WARN] Could not detect WiFi IP. Using $wifiIP" -ForegroundColor Yellow
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
