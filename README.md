# 🐇 R1 ↔ Hermes Bridge

Connect your **Rabbit R1** device to **Hermes Agent** — talk to Hermes through your R1 using voice, and get spoken responses back.

## How It Works

```
┌──────────┐     WebSocket      ┌──────────────┐     HTTP/SSE      ┌────────────┐
│ Rabbit R1│ ◄────────────────► │ R1 Bridge     │ ◄──────────────► │ Hermes     │
│          │   OpenClaw GW      │ (Python WS)   │   OpenAI API     │ Agent API  │
│          │   Protocol          │ port 18790    │                  │ port 8642  │
└──────────┘                    └──────────────┘                  └────────────┘
```

1. **R1 scans a QR code** that contains the bridge's WebSocket URL + auth token
2. **R1 connects** via WebSocket using the OpenClaw Gateway protocol
3. **Bridge auto-approves** the R1 device
4. **Chat messages** from R1 are routed to Hermes Agent's API server
5. **Hermes responses** are streamed back to the R1 as text (R1 reads them aloud)

The R1 thinks it's talking to an OpenClaw gateway — we just speak enough of the protocol to make it happy, and route everything to Hermes on the backend.

## Architecture

The bridge implements the **minimum subset** of the OpenClaw Gateway WebSocket protocol needed for the Rabbit R1:

| Protocol Part | What the R1 Expects | What We Do |
|---|---|---|
| `connect.challenge` | Server sends nonce+timestamp | ✅ Sent on connection |
| `connect` request | R1 sends device identity + auth token | ✅ Verified against our token |
| Device pairing | R1 shows as "pending" → needs approval | ✅ Auto-approved immediately |
| `chat.send` | R1 sends user text | ✅ Routed to Hermes API |
| Response streaming | Server streams agent reply | ✅ SSE from Hermes → WS events to R1 |
| `health`, `status`, `node.list` | R1 queries gateway state | ✅ Stubbed with valid responses |

### QR Code Payload Format

The R1 scans a QR code with this JSON structure:

```json
{
  "type": "clawdbot-gateway",
  "version": 1,
  "ips": ["192.168.1.100"],
  "port": 18790,
  "token": "<hex-auth-token>",
  "protocol": "ws"
}
```

This is the same format OpenClaw uses — the R1 doesn't know the difference. The `setup.sh` script generates this automatically.

## Setup

### Prerequisites

- Hermes Agent installed and running (`hermes gateway status`)
- Hermes API server enabled (already on port 8642 if you see `API_SERVER_ENABLED=true` in `.env`)
- Rabbit R1 device on the same network
- Python 3.9+ with `websockets`, `httpx`, `qrcode` (installed automatically)

### Quick Start

```bash
# 1. Install Python deps
cd ~/r1-hermes-bridge
pip install websockets httpx qrcode[pil]

# 2. Run setup (generates QR code)
./setup.sh

# 3. Scan the QR code with your R1
#    On the R1: go to Settings → Connect Device → Scan QR

# 4. Start chatting!
#    Press the R1 button and talk — Hermes will respond
```

### Running as a Service

```bash
# Install the systemd user service
mkdir -p ~/.config/systemd/user
cp ~/r1-hermes-bridge/r1-hermes-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now r1-hermes-bridge

# Check status
systemctl --user status r1-hermes-bridge

# View logs
journalctl --user -u r1-hermes-bridge -f
```

### Environment Variables

Set these in `~/.hermes/.env` or `~/r1-hermes-bridge/.r1-bridge-env`:

| Variable | Default | Description |
|---|---|---|
| `R1_BRIDGE_PORT` | `18790` | WebSocket port for R1 connections |
| `HERMES_API_KEY` | (from `API_SERVER_KEY` in .env) | Auth key for Hermes API |
| `HERMES_API_URL` | `http://127.0.0.1:8642/v1` | Hermes API endpoint |
| `R1_AUTH_TOKEN` | (auto-generated) | Token the R1 uses to authenticate |

## Troubleshooting

### R1 won't connect (can't scan QR / no connection)

1. **Are you on the same network?** The R1 must be on the same LAN as the bridge.
2. **Is the bridge running?** Check: `systemctl --user status r1-hermes-bridge`
3. **Is the port open?** Check: `ss -tlnp | grep 18790`
4. **Firewall?** If you have ufw: `sudo ufw allow 18790/tcp`
5. **Tailscale?** The R1 uses LAN IPs, not Tailscale. Make sure the LAN IP in the QR is reachable.

### R1 connects but no responses

1. **Check Hermes API:** `curl -H "Authorization: Bearer <key>" http://127.0.0.1:8642/v1/models`
2. **Check bridge logs:** `journalctl --user -u r1-hermes-bridge -n 50`
3. **Wrong API key?** Verify `HERMES_API_KEY` matches `API_SERVER_KEY` from `~/.hermes/.env`

### "No LAN IPs detected" during setup

This means the machine isn't on a local network (or is on a VPS). The R1 needs LAN connectivity. If your Hermes host is remote, you'll need to:
- Use Tailscale and expose the port on the Tailscale interface
- Update the QR code IP to the Tailscale IP

### Port already in use

If port 18790 is taken, change it:
```bash
export R1_BRIDGE_PORT=18791
./setup.sh
```

## How It Differs from OpenClaw

| Aspect | OpenClaw | Hermes Bridge |
|---|---|---|
| Gateway WS protocol | Full implementation | Minimal subset for R1 |
| Device pairing | Manual approval via CLI | Auto-approve |
| Agent backend | Built-in agent | Routes to Hermes API |
| Talk/Voice mode | Built-in TTS/STT pipeline | R1 handles its own voice |
| Multi-device | Full node management | R1-focused (but supports multiple) |

The R1 handles voice capture and TTS playback natively — the bridge only needs to relay text.

## Files

```
~/r1-hermes-bridge/
├── server.py          # WebSocket bridge (main server)
├── setup.sh           # QR code generator + setup wizard
├── .r1-auth-token     # Auto-generated auth token (created by setup.sh)
├── .r1-bridge-env     # Optional env overrides
├── r1-hermes-bridge.service  # systemd unit file
└── README.md          # This file
```
