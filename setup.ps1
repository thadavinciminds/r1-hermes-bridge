# R1 ↔ Hermes Bridge — x1 Windows Setup
# ========================================
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
    [switch]$NoStart,       # Generate QR only, don't start bridge
    [switch]$Help
)

if ($Help) {
    Write-Host @"
R1 ↔ Hermes Bridge — x1 Setup
==============================
Run this on your x1 laptop to create a bridge that connects your R1 to Hermes.

Options:
  -NoStart    Generate QR code only, don't start the bridge
  -Help       Show this help

The bridge:
  - Accepts WebSocket connections from your R1 on port 18790
  - Forwards all chat to Hermes at 100.116.144.9:8642 via Tailscale
  - Streams responses back to your R1 in real-time
"@
    exit 0
}

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BridgePort = 18790
$HealthPort = $BridgePort - 2  # 18788

# ── Colors ──────────────────────────────────────────────────────────────────
function Write-Banner {
    Write-Host ""
    Write-Host "  ╔══════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "  ║     🐇  R1 → Hermes Bridge (x1 Setup)    ║" -ForegroundColor Cyan
    Write-Host "  ╚══════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
}

# ── Python Check ────────────────────────────────────────────────────────────
function Test-PythonInstalled {
    try {
        $v = python --version 2>&1
        Write-Host "  ✓ Python: $v" -ForegroundColor Green
        return $true
    } catch {
        Write-Host "  ✗ Python not found! Install from https://python.org" -ForegroundColor Red
        Write-Host "    Make sure to check 'Add Python to PATH' during install."
        return $false
    }
}

# ── Dependencies ────────────────────────────────────────────────────────────
function Install-Dependencies {
    Write-Host "  Checking dependencies..." -ForegroundColor Yellow
    
    $packages = @("websockets", "httpx", "qrcode[pil]")
    $missing = @()
    
    foreach ($pkg in $packages) {
        $name = $pkg -replace '\[.*\]', ''
        $result = python -c "import $name; print('ok')" 2>&1
        if ($result -ne "ok") {
            $missing += $pkg
        }
    }
    
    if ($missing.Count -gt 0) {
        Write-Host "  Installing: $($missing -join ', ')..." -ForegroundColor Yellow
        foreach ($pkg in $missing) {
            python -m pip install $pkg --quiet
            Write-Host "    ✓ $pkg" -ForegroundColor Green
        }
    } else {
        Write-Host "  ✓ All dependencies found" -ForegroundColor Green
    }
}

# ── Auth Token ──────────────────────────────────────────────────────────────
function Get-AuthToken {
    $TokenFile = Join-Path $ScriptDir ".r1-auth-token"
    
    if (Test-Path $TokenFile) {
        $token = Get-Content $TokenFile -Raw
        $token = $token.Trim()
        if ($token.Length -gt 0) {
            return $token
        }
    }
    
    # Generate new token
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    
    Set-Content -Path $TokenFile -Value $token -NoNewline
    return $token
}

# ── WiFi IP Detection ───────────────────────────────────────────────────────
function Get-WiFiIP {
    try {
        # Get the IP of the interface that has a default route to 192.168.x.x
        $ips = Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
            $_.IPAddress -like "192.168.*" -or $_.IPAddress -like "10.*" -or $_.IPAddress -like "172.16.*"
        } | Sort-Object -Property InterfaceMetric
        
        if ($ips.Count -eq 0) {
            # Fallback: any non-loopback, non-APIPA
            $ips = Get-NetIPAddress -AddressFamily IPv4 | Where-Object {
                $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*"
            } | Sort-Object -Property InterfaceMetric
        }
        
        # Prefer WiFi interfaces
        $wifi = $ips | Where-Object { $_.InterfaceAlias -match "wi-?fi|wlan|wireless" }
        if ($wifi) { $ips = $wifi }
        
        return ($ips | Select-Object -First 1).IPAddress
    } catch {
        Write-Host "  ⚠ Could not auto-detect IP. Using fallback..." -ForegroundColor Yellow
        return "192.168.1.100"  # User will need to override
    }
}

# ── QR Code Generation ──────────────────────────────────────────────────────
function New-QRCode {
    param([string]$Token, [string]$IP)
    
    $payload = @{
        type     = "clawdbot-gateway"
        version  = 1
        ips      = @($IP)
        port     = $BridgePort
        token    = $Token
        protocol = "ws"
    } | ConvertTo-Json -Compress
    
    Write-Host ""
    Write-Host "  QR Payload:" -ForegroundColor Yellow
    Write-Host "  $payload" -ForegroundColor DarkGray
    Write-Host ""
    
    $qrPath = Join-Path $ScriptDir "r1-hermes-qr.png"
    
    # Generate QR as PNG
    python -c @"
import qrcode, json
payload = json.loads('''$payload''' if isinstance('''$payload''', str) else '''$payload''')
payload = '''$payload'''
img = qrcode.make(payload)
img.save(r'$qrPath')
print(f'Saved: $qrPath ({img.size[0]}x{img.size[1]})')
"@
    
    Write-Host "  ✓ QR code saved: $qrPath" -ForegroundColor Green
    Write-Host ""
    
    # Try to display the QR in the terminal
    python -c @"
import qrcode, json
payload = '''$payload'''
qr = qrcode.QRCode(box_size=2, border=2)
qr.add_data(payload)
qr.make(fit=True)
qr.print_ascii()
"@ 2>$null
    
    # Open the image
    Start-Process $qrPath
}

# ── Firewall Rule ───────────────────────────────────────────────────────────
function Add-FirewallRule {
    Write-Host "  Checking firewall..." -ForegroundColor Yellow
    
    $ruleName = "R1 Hermes Bridge (Port $BridgePort)"
    $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    
    if (-not $existing) {
        try {
            New-NetFirewallRule -DisplayName $ruleName `
                -Direction Inbound -Protocol TCP -LocalPort $BridgePort,$HealthPort `
                -Action Allow -Profile Private,Public | Out-Null
            Write-Host "  ✓ Firewall rule added for ports $BridgePort,$HealthPort" -ForegroundColor Green
        } catch {
            Write-Host "  ⚠ Could not add firewall rule (run as Admin?). You may need to allow port $BridgePort manually." -ForegroundColor Yellow
        }
    } else {
        Write-Host "  ✓ Firewall rule already exists" -ForegroundColor Green
    }
}

# ── Main ────────────────────────────────────────────────────────────────────

Write-Banner

# Check Python
if (-not (Test-PythonInstalled)) {
    Write-Host ""
    Write-Host "  Install Python from https://python.org then re-run this script."
    pause
    exit 1
}

# Install deps
Install-Dependencies
Write-Host ""

# Get auth token
$Token = Get-AuthToken
Write-Host "  Auth token: $($Token.Substring(0,16))..." -ForegroundColor Blue
Write-Host ""

# Detect WiFi IP
$WiFiIP = Get-WiFiIP
Write-Host "  WiFi IP: $WiFiIP" -ForegroundColor Green
Write-Host "  (If this is wrong, edit the QR or re-run with correct network)"

# Setup firewall
Add-FirewallRule
Write-Host ""

# Generate QR
New-QRCode -Token $Token -IP $WiFiIP

# Connection info
Write-Host "  ══════════════════════════════════════════" -ForegroundColor Green
Write-Host "  Connection Info" -ForegroundColor Green
Write-Host "  ══════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  Bridge:    ws://${WiFiIP}:${BridgePort}" -ForegroundColor Cyan
Write-Host "  Health:    http://${WiFiIP}:${HealthPort}" -ForegroundColor Cyan
Write-Host "  Hermes:    http://100.116.144.9:8642/v1 (via Tailscale)" -ForegroundColor DarkGray
Write-Host ""

if (-not $NoStart) {
    Write-Host "  Starting bridge server..." -ForegroundColor Green
    Write-Host "  (Press Ctrl+C to stop)" -ForegroundColor DarkGray
    Write-Host ""
    
    # Start the bridge
    $env:HERMES_API_URL = "http://100.116.144.9:8642/v1"
    $env:R1_AUTH_TOKEN = $Token
    $env:R1_BRIDGE_PORT = $BridgePort
    
    python (Join-Path $ScriptDir "server-x1.py")
} else {
    Write-Host "  To start the bridge:" -ForegroundColor Yellow
    Write-Host "    python server-x1.py" -ForegroundColor Cyan
    Write-Host ""
}
