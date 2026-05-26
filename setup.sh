#!/bin/bash
# R1 ↔ Hermes Bridge — Setup & QR Code Generator
#
# This script:
#   1. Detects your LAN IP addresses
#   2. Generates/generates an auth token
#   3. Creates a QR code the Rabbit R1 can scan
#   4. Optionally starts the bridge server
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh              # Interactive
#   ./setup.sh --no-start   # Generate QR only, don't start bridge

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_PORT="${R1_BRIDGE_PORT:-18790}"

# Parse arguments
NO_START=false
OUTPUT_DIR="$SCRIPT_DIR"
for arg in "$@"; do
    case "$arg" in
        --no-start) NO_START=true ;;
        -h|--help) echo "Usage: $0 [--no-start] [output-dir]" ; exit 0 ;;
        *) OUTPUT_DIR="$arg" ;;
    esac
done

OUTPUT_FILE="$OUTPUT_DIR/r1-hermes-qr.png"

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

banner() {
    echo -e "${CYAN}"
    echo "  ╔══════════════════════════════════════════╗"
    echo "  ║     🐇  R1 → Hermes Bridge Setup        ║"
    echo "  ╚══════════════════════════════════════════╝"
    echo -e "${NC}"
}

# ── Auth Token ──────────────────────────────────────────────────────────────

get_or_create_token() {
    local TOKEN_FILE="$SCRIPT_DIR/.r1-auth-token"

    # Check env var first
    if [ -n "$R1_AUTH_TOKEN" ]; then
        echo "$R1_AUTH_TOKEN"
        return
    fi

    # Check file
    if [ -f "$TOKEN_FILE" ]; then
        cat "$TOKEN_FILE"
        return
    fi

    # Generate new
    local NEW_TOKEN
    if command -v openssl &>/dev/null; then
        NEW_TOKEN=$(openssl rand -hex 32)
    else
        NEW_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    fi

    echo "$NEW_TOKEN" > "$TOKEN_FILE"
    chmod 600 "$TOKEN_FILE"
    echo "$NEW_TOKEN"
}

# ── LAN IP Detection ────────────────────────────────────────────────────────

get_lan_ips() {
    # Get all non-loopback, non-link-local, non-docker IPv4 addresses
    ip -4 addr show 2>/dev/null | \
        grep -oP '(?<=inet\s)\d+(\.\d+){3}' | \
        grep -v '^127\.' | \
        grep -v '^169\.254\.' | \
        grep -v '^172\.17\.'
}

# ── QR Code Generation ──────────────────────────────────────────────────────

generate_qr() {
    local TOKEN="$1"
    local PORT="$2"
    shift 2
    local IPS=("$@")

    # Build JSON payload
    local IPS_JSON=""
    for ip in "${IPS[@]}"; do
        if [ -n "$IPS_JSON" ]; then
            IPS_JSON+=","
        fi
        IPS_JSON+="\"$ip\""
    done

    local PAYLOAD
    PAYLOAD=$(cat <<EOF
{"type":"clawdbot-gateway","version":1,"ips":[${IPS_JSON}],"port":${PORT},"token":"${TOKEN}","protocol":"ws"}
EOF
)

    echo -e "${YELLOW}QR Code Payload:${NC}"
    echo "  $PAYLOAD"
    echo ""

    # Generate QR code to terminal
    echo -e "${YELLOW}QR Code (scan with your R1):${NC}"
    echo ""

    # Try multiple QR generators
    if python3 -c "import qrcode" 2>/dev/null; then
        # Generate with qrcode lib
        python3 -c "
import qrcode
qr = qrcode.QRCode(box_size=2, border=2)
qr.add_data('''$PAYLOAD''')
qr.make(fit=True)
qr.print_ascii()
"
    elif command -v qrencode &>/dev/null; then
        qrencode -t ANSIUTF8 "$PAYLOAD"
    elif command -v npx &>/dev/null; then
        npx --yes qrcode "$PAYLOAD"
    else
        echo -e "${YELLOW}  (Install qrcode: pip3 install qrcode[pil])${NC}"
    fi

    # Save QR code as PNG
    echo ""
    echo -e "${YELLOW}Saving QR code to:${NC} $OUTPUT_FILE"

    if python3 -c "import qrcode" 2>/dev/null; then
        python3 -c "
import qrcode
img = qrcode.make('''$PAYLOAD''')
img.save('$OUTPUT_FILE')
print('  ✓ QR code saved as PNG')
"
    elif command -v qrencode &>/dev/null; then
        qrencode -o "$OUTPUT_FILE" "$PAYLOAD"
        echo "  ✓ QR code saved as PNG"
    elif command -v npx &>/dev/null; then
        npx --yes qrcode "$PAYLOAD" -o "$OUTPUT_FILE"
        echo "  ✓ QR code saved as PNG"
    else
        echo -e "${RED}  ✗ Cannot save PNG (install qrcode or qrencode)${NC}"
    fi
}

# ── Main ────────────────────────────────────────────────────────────────────

main() {
    banner

    # Detect LAN IPs
    echo -e "${BLUE}Detecting LAN IP addresses...${NC}"
    mapfile -t IPS < <(get_lan_ips)

    if [ ${#IPS[@]} -eq 0 ]; then
        echo -e "${RED}Error: No LAN IP addresses detected.${NC}"
        echo "Are you connected to a network?"
        exit 1
    fi

    for ip in "${IPS[@]}"; do
        echo -e "  ${GREEN}✓${NC} $ip"
    done
    echo ""

    # Get/create token
    TOKEN=$(get_or_create_token)
    echo -e "${BLUE}Auth token:${NC} ${TOKEN:0:16}..."
    echo ""

    # If Hermes API key needed, check
    if [ -z "$HERMES_API_KEY" ] && [ -z "$API_SERVER_KEY" ]; then
        echo -e "${YELLOW}⚠ No HERMES_API_KEY set. Using default from .env${NC}"
    fi

    # Generate QR code
    generate_qr "$TOKEN" "$BRIDGE_PORT" "${IPS[@]}"

    # Connection info
    echo ""
    echo -e "${GREEN}══════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Connection Info${NC}"
    echo -e "${GREEN}══════════════════════════════════════════${NC}"
    echo ""
    for ip in "${IPS[@]}"; do
        echo -e "  ${CYAN}ws://${ip}:${BRIDGE_PORT}${NC}"
    done
    echo ""
    echo -e "  Token: ${TOKEN:0:16}..."
    echo ""
    echo -e "${GREEN}══════════════════════════════════════════${NC}"
    echo ""
    echo -e "To start the bridge:"
    echo -e "  ${CYAN}python3 $SCRIPT_DIR/server.py${NC}"
    echo ""
    echo -e "Or as a systemd service:"
    echo -e "  ${CYAN}systemctl --user start r1-hermes-bridge${NC}"
    echo ""

    # Prompt to start
    if [ "$NO_START" = false ]; then
        read -p "Start the bridge server now? [y/N] " start_confirm
        if [[ "$start_confirm" =~ ^[Yy]$ ]]; then
            echo ""
            echo -e "${GREEN}Starting bridge server...${NC}"
            exec python3 "$SCRIPT_DIR/server.py"
        fi
    fi
}

main "$@"
