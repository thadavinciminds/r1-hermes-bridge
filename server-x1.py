#!/usr/bin/env python3
"""
R1 ↔ Hermes Bridge (x1 / Windows edition)
==========================================
WebSocket server that speaks OpenClaw Gateway protocol on the Rabbit R1 side
and routes chat to Hermes Agent via Tailscale.

This runs on the x1 laptop (same WiFi as the R1). It forwards everything to
Hermes at 100.116.144.9:8642 over Tailscale.

Usage:
    python server.py

QR payload the R1 scans:
    {"type":"clawdbot-gateway","version":1,"ips":["<X1_WIFI_IP>"],"port":18790,
     "token":"<token>","protocol":"ws"}
"""

import asyncio
import json
import os
import secrets
import sys
import time
from datetime import datetime, timezone

import httpx
import websockets
from websockets.asyncio.server import serve

# ── Configuration ───────────────────────────────────────────────────────────
BRIDGE_PORT = int(os.getenv("R1_BRIDGE_PORT", "18790"))
BRIDGE_HOST = os.getenv("R1_BRIDGE_HOST", "0.0.0.0")

# Hermes LXC via Tailscale (the x1 can reach this)
HERMES_API_URL = os.getenv(
    "HERMES_API_URL", "http://100.116.144.9:8642/v1"
)
HERMES_API_KEY = os.getenv(
    "HERMES_API_KEY",
    "hermes-Fk25u-U_v5DXSpF7N24EDTF4HkJJDPlW",
)

# Load auth token (same as LXC bridge so R1 pairing persists)
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".r1-auth-token")
if os.path.exists(TOKEN_FILE):
    with open(TOKEN_FILE) as f:
        AUTH_TOKEN = f.read().strip()
else:
    AUTH_TOKEN = os.getenv("R1_AUTH_TOKEN", secrets.token_hex(32))
    with open(TOKEN_FILE, "w") as f:
        f.write(AUTH_TOKEN)

# ── State ───────────────────────────────────────────────────────────────────
devices: dict = {}
pending_requests: dict = {}

# ── Helpers ─────────────────────────────────────────────────────────────────

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ── Hermes API ──────────────────────────────────────────────────────────────

async def hermes_chat(messages: list, system_prompt: str = None) -> str:
    """Send chat to Hermes Agent via Tailscale and get response."""
    api_messages = []
    if system_prompt:
        api_messages.append({"role": "system", "content": system_prompt})
    api_messages.extend(messages)

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{HERMES_API_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {HERMES_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "hermes-agent",
                "messages": api_messages,
                "stream": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        choice = data.get("choices", [{}])[0]
        return choice.get("message", {}).get("content", "[No response]")


async def hermes_chat_stream(messages: list, ws, request_id: str, system_prompt: str = None):
    """Stream chat response from Hermes to R1 via WebSocket events."""
    api_messages = []
    if system_prompt:
        api_messages.append({"role": "system", "content": system_prompt})
    api_messages.extend(messages)

    full_text = []

    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream(
            "POST",
            f"{HERMES_API_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {HERMES_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "hermes-agent",
                "messages": api_messages,
                "stream": True,
            },
        ) as resp:
            resp.raise_for_status()
            chunk_count = 0
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        full_text.append(content)
                        await ws.send(json.dumps({
                            "type": "event",
                            "event": "agent.message",
                            "seq": chunk_count,
                            "payload": {"text": content, "stream": True},
                        }))
                        chunk_count += 1
                except (json.JSONDecodeError, KeyError):
                    continue

    complete_text = "".join(full_text)
    await ws.send(json.dumps({
        "type": "event",
        "event": "agent.message",
        "seq": chunk_count + 1,
        "payload": {"text": complete_text, "stream": False, "complete": True},
    }))
    return complete_text


# ── OpenClaw Gateway Protocol Handlers ──────────────────────────────────────

async def handle_connect(ws, request_id: str, params: dict) -> dict:
    """Handle the OpenClaw Gateway `connect` method."""
    auth = params.get("auth", {})
    device_info = params.get("device", {})
    client_info = params.get("client", {})
    role = params.get("role", "node")

    token = auth.get("token", "")

    # Accept either the original pairing token OR any previously issued device token
    valid_tokens = {AUTH_TOKEN}
    for d in devices.values():
        dt = d.get("device_token")
        if dt:
            valid_tokens.add(dt)

    if token not in valid_tokens:
        log(f"  ← Auth token mismatch (got {token[:12]}..., expected one of {len(valid_tokens)} tokens)")
        return {
            "type": "res", "id": request_id, "ok": False,
            "error": {"code": "UNAUTHORIZED", "message": "Invalid auth token"},
        }

    device_id = device_info.get("id", f"r1-{secrets.token_hex(8)}")
    display_name = f"Rabbit R1 ({client_info.get('platform', 'unknown')})"

    is_new = device_id not in devices
    pairing_request_id = None
    if is_new:
        pairing_request_id = f"pr_{secrets.token_hex(8)}"
        pending_requests[pairing_request_id] = device_id

    device_token = f"hdt_{secrets.token_hex(32)}"

    devices[device_id] = {
        "ws": ws,
        "approved": True,  # Auto-approve on x1 bridge
        "display_name": display_name,
        "device_token": device_token,
        "role": role,
        "client": client_info,
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }

    log(f"✓ R1 connected: {display_name} ({device_id[:16]}...)")

    hello_payload = {
        "type": "hello-ok",
        "protocol": 4,
        "server": {
            "version": "Hermes-X1-Bridge/1.0",
            "connId": f"conn_{secrets.token_hex(8)}",
        },
        "features": {
            "methods": [
                "chat.send", "chat.history", "node.pair.approve",
                "node.pair.reject", "node.list", "system-presence",
                "health", "status", "talk.catalog", "talk.config",
                "talk.session.create", "talk.session.appendAudio",
                "talk.session.startTurn", "talk.session.endTurn",
                "talk.session.cancelTurn", "talk.session.close",
            ],
            "events": [
                "agent.message", "agent.thinking", "agent.error",
                "chat.message", "presence", "tick", "heartbeat",
                "node.presence",
            ],
        },
        "snapshot": {
            "pendingDevices": [],
            "devices": [
                {"deviceId": did, "displayName": d["display_name"],
                 "approved": d["approved"]}
                for did, d in devices.items() if d["approved"]
            ],
        },
        "auth": {
            "role": role,
            "scopes": params.get("scopes", []),
            "deviceToken": device_token,
        },
        "policy": {
            "maxPayload": 26214400,
            "maxBufferedBytes": 52428800,
            "tickIntervalMs": 15000,
        },
        "pluginSurfaceUrls": {},
    }

    return {"type": "res", "id": request_id, "ok": True, "payload": hello_payload}


async def handle_chat_send_stream(ws, request_id: str, params: dict, device_id: str):
    """Handle chat.send — stream Hermes response to R1."""
    text = params.get("text", "")
    messages = params.get("messages", None)

    if not text and not messages:
        await ws.send(json.dumps({
            "type": "res", "id": request_id, "ok": False,
            "error": {"code": "INVALID_PARAMS", "message": "text or messages required"},
        }))
        return

    log(f"📩 Chat from R1: {text[:120]}")

    try:
        if messages:
            reply = await hermes_chat_stream(messages, ws, request_id)
        else:
            reply = await hermes_chat_stream(
                [{"role": "user", "content": text}], ws, request_id
            )

        await ws.send(json.dumps({
            "type": "res", "id": request_id, "ok": True,
            "payload": {
                "text": reply,
                "messages": [{"role": "assistant", "content": reply}],
            },
        }))
        log(f"📤 Response: {reply[:100]}...")
    except Exception as e:
        log(f"❌ Hermes API error: {e}")
        await ws.send(json.dumps({
            "type": "res", "id": request_id, "ok": False,
            "error": {"code": "AGENT_ERROR", "message": str(e)},
        }))


# ── WebSocket Connection Handler ────────────────────────────────────────────

async def r1_handler(websocket):
    """Handle a single R1 WebSocket connection."""
    peer = websocket.remote_address
    device_id = None
    log(f"🔌 New connection from {peer}")

    try:
        # Step 1: Send pre-connect challenge
        nonce = secrets.token_hex(16)
        challenge = {
            "type": "event",
            "event": "connect.challenge",
            "payload": {
                "nonce": nonce,
                "ts": int(time.time() * 1000),
            },
        }
        await websocket.send(json.dumps(challenge))

        # Step 2: Wait for connect request
        raw = await asyncio.wait_for(websocket.recv(), timeout=30)
        msg = json.loads(raw)
        log(f"  ← Received: {msg.get('type')}/{msg.get('method', '?')}")

        if msg.get("type") != "req" or msg.get("method") != "connect":
            await websocket.send(json.dumps({
                "type": "res", "id": msg.get("id", "unknown"), "ok": False,
                "error": {"code": "BAD_REQUEST", "message": "Expected connect request"},
            }))
            return

        # Step 3: Handle connect
        connect_params = msg.get("params", {})
        connect_resp = await handle_connect(websocket, msg["id"], connect_params)
        await websocket.send(json.dumps(connect_resp))

        if not connect_resp.get("ok"):
            log(f"❌ Connect failed: {connect_resp.get('error')}")
            return

        device_info = connect_params.get("device", {})
        device_id = device_info.get("id", f"r1-{secrets.token_hex(8)}")

        # Step 4: Message loop
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log(f"⚠ Invalid JSON from {device_id[:12]}")
                continue

            msg_type = msg.get("type", "")
            msg_id = msg.get("id", secrets.token_hex(8))
            method = msg.get("method", "")
            params = msg.get("params", {})

            if msg_type == "req":
                if method == "chat.send":
                    await handle_chat_send_stream(websocket, msg_id, params, device_id)

                elif method == "chat.history":
                    await websocket.send(json.dumps({
                        "type": "res", "id": msg_id, "ok": True,
                        "payload": {"messages": []},
                    }))

                elif method == "health":
                    await websocket.send(json.dumps({
                        "type": "res", "id": msg_id, "ok": True,
                        "payload": {"status": "ok", "uptime": int(time.time()),
                                     "version": "Hermes-X1-Bridge/1.0"},
                    }))

                elif method == "status":
                    await websocket.send(json.dumps({
                        "type": "res", "id": msg_id, "ok": True,
                        "payload": {
                            "gateway": {"status": "running", "version": "Hermes-X1-Bridge/1.0"},
                            "devices": len(devices),
                            "agent": "Hermes Agent",
                        },
                    }))

                elif method == "node.list":
                    await websocket.send(json.dumps({
                        "type": "res", "id": msg_id, "ok": True,
                        "payload": {
                            "nodes": [
                                {"deviceId": did, "displayName": d["display_name"],
                                 "role": d["role"], "approved": d["approved"],
                                 "connected": True, "lastSeenAtMs": int(time.time() * 1000),
                                 "lastSeenReason": "connect",
                                 "caps": d.get("client", {}).get("caps", [])}
                                for did, d in devices.items()
                            ]
                        },
                    }))

                elif method == "system-presence":
                    await websocket.send(json.dumps({
                        "type": "res", "id": msg_id, "ok": True,
                        "payload": {"entries": {}},
                    }))

                elif method in ("node.pair.approve", "node.pair.reject"):
                    await websocket.send(json.dumps({
                        "type": "res", "id": msg_id, "ok": True,
                        "payload": {"approved": True},
                    }))

                elif method == "talk.catalog":
                    await websocket.send(json.dumps({
                        "type": "res", "id": msg_id, "ok": True,
                        "payload": {"providers": []},
                    }))

                elif method == "talk.config":
                    await websocket.send(json.dumps({
                        "type": "res", "id": msg_id, "ok": True,
                        "payload": {"configured": False},
                    }))

                else:
                    await websocket.send(json.dumps({
                        "type": "res", "id": msg_id, "ok": True,
                        "payload": {"note": f"Method '{method}' acknowledged"},
                    }))

            elif msg_type == "event":
                pass  # R1 events (typing indicators, etc.)

    except asyncio.TimeoutError:
        log(f"⏱ Timeout waiting for connect from {peer}")
    except websockets.exceptions.ConnectionClosed:
        log(f"🔌 Connection closed: {peer}")
    except Exception as e:
        log(f"❌ Error handling {peer}: {e}")
    finally:
        if device_id and device_id in devices:
            del devices[device_id]
            log(f"🧹 Cleaned up: {device_id[:12]}...")


# ── HTTP Health Check ───────────────────────────────────────────────────────

async def health_endpoint(reader, writer):
    try:
        body = json.dumps({"status": "ok", "devices": len(devices),
                           "bridge": "r1-hermes-x1", "hermes_api": HERMES_API_URL}).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Access-Control-Allow-Origin: *\r\n\r\n" + body
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


# ── Main ────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 60)
    print("  🐇 R1 ↔ Hermes Bridge (x1 / Windows)")
    print("=" * 60)
    print(f"  Bridge port:    {BRIDGE_PORT}")
    print(f"  Hermes API:     {HERMES_API_URL}")
    print(f"  Auth token:     {AUTH_TOKEN[:16]}...")
    print("=" * 60)
    print()

    # Health check on port 18788
    health_port = BRIDGE_PORT - 2
    health_server = await asyncio.start_server(health_endpoint, "0.0.0.0", health_port)
    print(f"  ✓ Health check: http://0.0.0.0:{health_port}")

    async with serve(r1_handler, BRIDGE_HOST, BRIDGE_PORT):
        print(f"  ✓ Bridge listening on ws://{BRIDGE_HOST}:{BRIDGE_PORT}")
        print()
        print("  Waiting for Rabbit R1 connections...")
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
