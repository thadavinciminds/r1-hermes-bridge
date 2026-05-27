#!/usr/bin/env python3
"""
R1 ↔ Hermes Bridge — WebSocket server that speaks OpenClaw Gateway protocol
on the Rabbit R1 side and routes chat to Hermes Agent on the backend.

Protocol: Implements the subset of OpenClaw Gateway WS protocol that the
Rabbit R1 expects for device pairing and chat.

Usage:
    python3 server.py [--port 18790] [--hermes-url http://127.0.0.1:8642/v1]

The QR code payload the R1 scans:
    {"type":"clawdbot-gateway","version":1,"ips":["<LAN_IP>"],"port":18790,
     "token":"<token>","protocol":"ws"}
"""

import asyncio
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx
import websockets
from websockets.asyncio.server import serve

# ── Configuration ───────────────────────────────────────────────────────────

BRIDGE_PORT = int(os.getenv("R1_BRIDGE_PORT", "18790"))
BRIDGE_HOST = os.getenv("R1_BRIDGE_HOST", "0.0.0.0")
HERMES_API_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642/v1")
HERMES_API_KEY = os.getenv(
    "HERMES_API_KEY",
    os.getenv("API_SERVER_KEY", "hermes-Fk25u-U_v5DXSpF7N24EDTF4HkJJDPlW"),
)
# Auth token: env var first, then file, then generate + persist
_AUTH_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".r1-auth-token")
AUTH_TOKEN = os.getenv("R1_AUTH_TOKEN")
if not AUTH_TOKEN:
    try:
        with open(_AUTH_TOKEN_FILE) as f:
            AUTH_TOKEN = f.read().strip()
    except FileNotFoundError:
        AUTH_TOKEN = secrets.token_hex(32)
        with open(_AUTH_TOKEN_FILE, "w") as f:
            f.write(AUTH_TOKEN)

# ── State ───────────────────────────────────────────────────────────────────

# Track connected R1 devices: {device_id: {"ws": websocket, "approved": bool, "display_name": str}}
devices: dict = {}
# Track pending pairing requests: {request_id: device_id}
pending_requests: dict = {}

# ── Device Token Persistence ────────────────────────────────────────────────

DEVICE_TOKENS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".r1-device-tokens.json"
)


def load_device_tokens() -> set:
    """Load persisted device tokens from disk."""
    if not os.path.exists(DEVICE_TOKENS_FILE):
        return set()
    try:
        with open(DEVICE_TOKENS_FILE) as f:
            data = json.load(f)
        return set(data.get("tokens", []))
    except (json.JSONDecodeError, IOError):
        return set()


def save_device_token(token: str):
    """Persist a device token to disk."""
    tokens = load_device_tokens()
    tokens.add(token)
    os.makedirs(os.path.dirname(DEVICE_TOKENS_FILE), exist_ok=True)
    with open(DEVICE_TOKENS_FILE, "w") as f:
        json.dump({"tokens": list(tokens)}, f)

# ── Helpers ─────────────────────────────────────────────────────────────────

def generate_request_id() -> str:
    return secrets.token_hex(8)


def generate_device_token() -> str:
    return f"hdt_{secrets.token_hex(32)}"


# ── Hermes API ──────────────────────────────────────────────────────────────

async def hermes_chat(messages: list, system_prompt: str = None) -> str:
    """Send chat to Hermes Agent API server and get response."""
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
                        # Send streaming event
                        await ws.send(json.dumps({
                            "type": "event",
                            "event": "agent.message",
                            "seq": chunk_count,
                            "payload": {"text": content, "stream": True},
                        }))
                        chunk_count += 1
                except (json.JSONDecodeError, KeyError):
                    continue

    # Send final complete event
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
    client_info = params.get("client", {})
    device_info = params.get("device", {})
    auth = params.get("auth", {})
    role = params.get("role", "node")

    # Verify auth token — accept pairing token, persisted device tokens, or in-memory tokens
    token = auth.get("token", "")
    valid_tokens = {AUTH_TOKEN}
    valid_tokens.update(load_device_tokens())  # persisted
    for d in devices.values():                 # in-memory (belt-and-suspenders)
        dt = d.get("device_token")
        if dt:
            valid_tokens.add(dt)

    if token not in valid_tokens:
        # Bootstrap: if no tokens ever persisted and no devices active, accept any token
        if len(devices) == 0 and len(load_device_tokens()) == 0:
            print(f"  🔓 Bootstrap: accepting unseen device token (first-ever connection)")
            save_device_token(token)
        else:
            return {
                "type": "res",
                "id": request_id,
                "ok": False,
                "error": {"code": "UNAUTHORIZED", "message": "Invalid auth token"},
            }

    device_id = device_info.get("id", f"r1-{secrets.token_hex(8)}")
    display_name = f"Rabbit R1 ({client_info.get('platform', 'unknown')})"

    # Check if we already know this device
    is_new = device_id not in devices

    # Generate pairing request ID for new devices
    pairing_request_id = None
    if is_new:
        pairing_request_id = f"pr_{secrets.token_hex(8)}"
        pending_requests[pairing_request_id] = device_id

    # Generate device token
    device_token = generate_device_token()
    save_device_token(device_token)  # persist for reconnects

    devices[device_id] = {
        "ws": ws,
        "approved": not is_new,  # Auto-approve if device reconnects
        "display_name": display_name,
        "device_token": device_token,
        "role": role,
        "client": client_info,
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }

    conn_id = f"conn_{secrets.token_hex(8)}"

    # Build hello-ok response
    hello_payload = {
        "type": "hello-ok",
        "protocol": 4,
        "server": {
            "version": "Hermes-Bridge/1.0",
            "connId": conn_id,
        },
        "features": {
            "methods": [
                "chat.send",
                "chat.history",
                "node.pair.approve",
                "node.pair.reject",
                "node.list",
                "system-presence",
                "health",
                "status",
                "talk.catalog",
                "talk.config",
                "talk.session.create",
                "talk.session.appendAudio",
                "talk.session.startTurn",
                "talk.session.endTurn",
                "talk.session.cancelTurn",
                "talk.session.close",
            ],
            "events": [
                "agent",
                "chat",
                "presence",
                "tick",
                "heartbeat",
                "node.presence",
            ],
        },
        "snapshot": {
            "pendingDevices": [
                {
                    "requestId": pairing_request_id,
                    "displayName": display_name,
                    "deviceId": device_id,
                    "role": role,
                    "caps": params.get("caps", []),
                }
            ]
            if pairing_request_id and not devices[device_id]["approved"]
            else [],
            "devices": [
                {
                    "deviceId": did,
                    "displayName": d["display_name"],
                    "approved": d["approved"],
                }
                for did, d in devices.items()
                if d["approved"]
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

    # Auto-approve if new device
    if is_new and pairing_request_id:
        devices[device_id]["approved"] = True
        print(f"  ✓ Auto-approved device: {display_name} ({device_id})")

    return {
        "type": "res",
        "id": request_id,
        "ok": True,
        "payload": hello_payload,
    }


async def handle_chat_send(ws, request_id: str, params: dict, device_id: str) -> dict:
    """Handle chat.send — route message to Hermes."""
    text = params.get("text", "")
    messages = params.get("messages", None)

    if not text and not messages:
        return {
            "type": "res",
            "id": request_id,
            "ok": False,
            "error": {"code": "INVALID_PARAMS", "message": "text or messages required"},
        }

    print(f"  📩 Chat from {device_id[:12]}...: {text[:100]}")

    try:
        if messages:
            # Use the full message array from R1
            reply = await hermes_chat(messages)
        else:
            # Single message
            reply = await hermes_chat([{"role": "user", "content": text}])

        print(f"  📤 Response: {reply[:100]}...")

        return {
            "type": "res",
            "id": request_id,
            "ok": True,
            "payload": {
                "text": reply,
                "messages": [{"role": "assistant", "content": reply}],
            },
        }
    except Exception as e:
        print(f"  ❌ Hermes API error: {e}")
        return {
            "type": "res",
            "id": request_id,
            "ok": False,
            "error": {"code": "AGENT_ERROR", "message": str(e)},
        }


async def handle_chat_send_stream(ws, request_id: str, params: dict, device_id: str, next_seq_fn):
    """Handle chat.send — full OpenClaw protocol: ACK → thinking → reply → lifecycle → chat.final."""
    text = params.get("message") or params.get("text", "")
    messages = params.get("messages", None)
    session_key = params.get("sessionKey", "main")
    if not text and not messages:
        try:
            await ws.send(json.dumps({
                "type": "res", "id": request_id, "ok": False,
                "error": {"code": "INVALID_PARAMS", "message": "text or messages required"},
            }))
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
            pass
        return None

    print(f"  📩 Chat from {device_id[:12]}...: {text[:120]}")

    run_id = params.get("idempotencyKey") or f"run_{secrets.token_hex(8)}"
    started_at = int(time.time() * 1000)

    try:
        # ── ACK with runId + status: "started" ───────────────────────────
        await ws.send(json.dumps({
            "type": "res", "id": request_id, "ok": True,
            "payload": {"runId": run_id, "status": "started", "sessionKey": session_key},
        }))

        # ── agent.thinking (active=true) ─────────────────────────────────
        await ws.send(json.dumps({
            "type": "event",
            "event": "agent",
            "seq": next_seq_fn(),
            "payload": {
                "runId": run_id, "seq": 1,
                "stream": "thinking",
                "ts": started_at,
                "data": {"text": "", "delta": "", "active": True},
                "sessionKey": session_key,
            },
        }))

        # ── Call Hermes API ──────────────────────────────────────────────
        if messages:
            reply = await hermes_chat(messages)
        else:
            reply = await hermes_chat([{"role": "user", "content": text}])

        now_ts = int(time.time() * 1000)
        end_ts = int(time.time() * 1000)

        # ── agent.assistant ──────────────────────────────────────────
        await ws.send(json.dumps({
            "type": "event",
            "event": "agent",
            "seq": next_seq_fn(),
            "payload": {
                "runId": run_id, "seq": 2,
                "stream": "assistant",
                "ts": now_ts,
                "data": {"text": reply, "delta": reply},
                "sessionKey": session_key,
            },
        }))
        print(f"  📤 Response: {reply[:100]}...")

        # ── agent.thinking (active=false) ────────────────────────────
        await ws.send(json.dumps({
            "type": "event",
            "event": "agent",
            "seq": next_seq_fn(),
            "payload": {
                "runId": run_id, "seq": 3,
                "stream": "thinking",
                "ts": end_ts,
                "data": {"text": "", "delta": "", "active": False},
                "sessionKey": session_key,
            },
        }))

        # ── agent.lifecycle (phase=end) ──────────────────────────────
        await ws.send(json.dumps({
            "type": "event",
            "event": "agent",
            "seq": next_seq_fn(),
            "payload": {
                "runId": run_id, "seq": 4,
                "stream": "lifecycle",
                "ts": end_ts,
                "data": {"phase": "end", "endedAt": end_ts},
                "sessionKey": session_key,
            },
        }))

        # ── chat.final event (matches OpenClaw protocol — this is the
        #    event that tells the R1 the turn is complete and input is
        #    unlocked; no separate "idle" event exists in the protocol).
        chat_final_seq = next_seq_fn()
        await ws.send(json.dumps({
            "type": "event",
            "event": "chat",
            "seq": chat_final_seq,
            "payload": {
                "runId": run_id,
                "sessionKey": session_key,
                "seq": chat_final_seq,
                "state": "final",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": reply}],
                    "timestamp": end_ts,
                },
            },
        }))

        # Send a presence event to signal the agent is now idle — the R1
        # may be waiting for this before unlocking the input for the next message
        await ws.send(json.dumps({
            "type": "event",
            "event": "presence",
            "seq": next_seq_fn(),
            "payload": {
                "status": "idle",
                "deviceId": device_id,
                "ts": end_ts,
            },
        }))

        print("  → Sent: ACK + thinking(start) + assistant + thinking(end) + lifecycle(end) + chat.final + presence(idle)")
        return run_id

    except websockets.exceptions.ConnectionClosed:
        print(f"  🔌 R1 disconnected mid-response for {device_id[:12]}")
    except asyncio.CancelledError:
        print(f"  ⚡ Chat handler cancelled for {device_id[:12]}")
    except Exception as e:
        print(f"  ❌ Chat error for {device_id[:12]}: {type(e).__name__}: {e}")
        # Try to send error event, but don't crash if R1 is already gone
        try:
            now_ts = int(time.time() * 1000)
            err_seq = next_seq_fn()
            await ws.send(json.dumps({
                "type": "event",
                "event": "chat",
                "seq": err_seq,
                "payload": {
                    "runId": run_id,
                    "sessionKey": session_key,
                    "seq": err_seq,
                    "state": "error",
                    "errorMessage": str(e),
                },
            }))
        except Exception:
            pass


# ── WebSocket Connection Handler ────────────────────────────────────────────

async def r1_handler(websocket):
    """Handle a single R1 WebSocket connection."""
    peer = websocket.remote_address
    device_id = None
    print(f"\n🔌 New connection from {peer}")

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
        print(f"  → Sent connect.challenge")

        # Step 2: Wait for connect request
        raw = await asyncio.wait_for(websocket.recv(), timeout=30)
        msg = json.loads(raw)
        print(f"  ← Received: {msg.get('type')}/{msg.get('method', '?')} id={msg.get('id', '?')}")

        if msg.get("type") != "req" or msg.get("method") != "connect":
            error_resp = {
                "type": "res",
                "id": msg.get("id", "unknown"),
                "ok": False,
                "error": {"code": "BAD_REQUEST", "message": "Expected connect request"},
            }
            await websocket.send(json.dumps(error_resp))
            return

        # Step 3: Handle connect
        connect_params = msg.get("params", {})
        connect_resp = await handle_connect(websocket, msg["id"], connect_params)
        await websocket.send(json.dumps(connect_resp))

        if not connect_resp.get("ok"):
            print(f"  ❌ Connect failed: {connect_resp.get('error')}")
            return

        # Extract device info for tracking
        device_info = connect_params.get("device", {})
        device_id = device_info.get("id", f"r1-{secrets.token_hex(8)}")
        print(f"  ✓ R1 connected: {device_id[:16]}...")

        # Per-connection monotonically increasing seq for events
        _seq_counter = [0]
        def next_seq():
            _seq_counter[0] += 1
            return _seq_counter[0]

        # Send presence event to wake up the R1 mascot
        now_ms = int(time.time() * 1000)
        await websocket.send(json.dumps({
            "type": "event",
            "event": "presence",
            "seq": next_seq(),
            "payload": {
                "deviceId": device_id,
                "status": "online",
                "ts": now_ms,
            },
        }))
        print(f"  → Sent presence event")

        # Start tick heartbeat (every 8 seconds — R1 timeout is ~8s despite hello-ok saying 15s)
        async def tick_loop():
            try:
                while True:
                    await asyncio.sleep(8)
                    await websocket.send(json.dumps({
                        "type": "event",
                        "event": "tick",
                        "seq": next_seq(),
                        "payload": {"ts": int(time.time() * 1000)},
                    }))
            except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
                pass

        tick_task = asyncio.create_task(tick_loop())
        try:
            # Step 4: Message loop — handle subsequent messages
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                    # Debug: log every incoming message
                    msg_type = msg.get("type", "")
                    msg_id = msg.get("id", generate_request_id())
                    method = msg.get("method", "")
                    params = msg.get("params", {})
                    # Truncate long messages for log readability
                    raw_preview = raw[:300] + "..." if len(raw) > 300 else raw
                    print(f"  ← RAW [{len(raw)}B]: {raw_preview}")
                except json.JSONDecodeError:
                    print(f"  ⚠ Invalid JSON from {device_id[:12] if device_id else peer}")
                    continue

                if msg_type == "req":
                    print(f"  ← RPC: {method} id={msg_id}")

                    if method == "chat.send":
                        run_id = await handle_chat_send_stream(websocket, msg_id, params, device_id, next_seq)
                        if run_id:
                            print(f"  ✓ Chat turn complete (runId={run_id})")

                    elif method == "chat.history":
                        await websocket.send(json.dumps({
                            "type": "res", "id": msg_id, "ok": True,
                            "payload": {"messages": []},
                        }))

                    elif method == "health":
                        await websocket.send(json.dumps({
                            "type": "res", "id": msg_id, "ok": True,
                            "payload": {"status": "ok", "uptime": int(time.time()), "version": "Hermes-Bridge/1.0"},
                        }))

                    elif method == "status":
                        await websocket.send(json.dumps({
                            "type": "res", "id": msg_id, "ok": True,
                            "payload": {
                                "gateway": {"status": "running", "version": "Hermes-Bridge/1.0"},
                                "devices": len(devices),
                                "agent": "Hermes Agent",
                            },
                        }))

                    elif method == "system-presence":
                        presence_entries = {}
                        for did, d in devices.items():
                            presence_entries[did] = {
                                "deviceId": did,
                                "displayName": d["display_name"],
                                "roles": [d["role"]],
                                "scopes": [],
                                "connected": True,
                            }
                        await websocket.send(json.dumps({
                            "type": "res", "id": msg_id, "ok": True,
                            "payload": {"entries": presence_entries},
                        }))

                    elif method == "node.list":
                        node_list = []
                        for did, d in devices.items():
                            node_list.append({
                                "deviceId": did,
                                "displayName": d["display_name"],
                                "role": d["role"],
                                "approved": d["approved"],
                                "connected": True,
                                "lastSeenAtMs": int(time.time() * 1000),
                                "lastSeenReason": "connect",
                                "caps": d.get("client", {}).get("caps", []),
                            })
                        await websocket.send(json.dumps({
                            "type": "res", "id": msg_id, "ok": True,
                            "payload": {"nodes": node_list},
                        }))

                    elif method == "node.pair.approve":
                        req_id = params.get("requestId", "")
                        if req_id in pending_requests:
                            did = pending_requests[req_id]
                            if did in devices:
                                devices[did]["approved"] = True
                                del pending_requests[req_id]
                        await websocket.send(json.dumps({
                            "type": "res", "id": msg_id, "ok": True,
                            "payload": {"approved": True},
                        }))

                    elif method == "node.event":
                        # Handle node events (like presence.alive)
                        event_name = msg.get("event", params.get("event", ""))
                        await websocket.send(json.dumps({
                            "type": "res", "id": msg_id, "ok": True,
                            "event": event_name, "handled": True, "reason": "ack",
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
                        print(f"  ⚠ Unknown method: {method}")
                        await websocket.send(json.dumps({
                            "type": "res", "id": msg_id, "ok": True,
                            "payload": {"note": f"Method '{method}' acknowledged"},
                        }))

                elif msg_type == "event":
                    # Handle events from R1 (e.g., typing indicators, status)
                    event_name = msg.get("event", "?")
                    print(f"  ← EVENT: {event_name} payload={json.dumps(msg.get('payload', {}))[:200]}")
                    pass

                else:
                    print(f"  ⚠ Unknown message type: {msg_type}")
        finally:
            tick_task.cancel()
            try:
                await tick_task
            except asyncio.CancelledError:
                pass

    except asyncio.TimeoutError:
        print(f"  ⏱ Timeout waiting for connect from {peer}")
    except websockets.exceptions.ConnectionClosed:
        print(f"  🔌 Connection closed: {peer}")
    except Exception as e:
        print(f"  ❌ Error handling {peer}: {e}")
    finally:
        if device_id and device_id in devices:
            del devices[device_id]
            print(f"  🧹 Cleaned up device: {device_id[:12]}...")


# ── Main ────────────────────────────────────────────────────────────────────

async def health_endpoint(reader, writer):
    """HTTP health check + QR code serving."""
    try:
        # Read request line to check path
        raw_request = b""
        while b"\r\n\r\n" not in raw_request:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=5)
            if not chunk:
                break
            raw_request += chunk
            if len(raw_request) > 8192:
                break

        request_line = raw_request.split(b"\r\n")[0].decode("utf-8", errors="replace")
        path = request_line.split(" ")[1] if " " in request_line else "/"

        if path == "/qr-final.png" or path == "/qr":
            qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr-final.png")
            if os.path.exists(qr_path):
                with open(qr_path, "rb") as f:
                    qr_data = f.read()
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\n"
                    b"Content-Length: " + str(len(qr_data)).encode() + b"\r\n"
                    b"Access-Control-Allow-Origin: *\r\n\r\n" + qr_data
                )
            else:
                body = b'{"error":"QR not found"}'
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Access-Control-Allow-Origin: *\r\n\r\n" + body
                )
        else:
            body = json.dumps({"status": "ok", "devices": len(devices), "bridge": "r1-hermes"}).encode()
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                b"Access-Control-Allow-Origin: *\r\n\r\n" + body
            )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def main():
    print("=" * 60)
    print("  🐇 R1 ↔ Hermes Bridge")
    print("=" * 60)
    print(f"  Bridge port:    {BRIDGE_PORT}")
    print(f"  Hermes API:     {HERMES_API_URL}")
    print(f"  Auth token:     {AUTH_TOKEN}")
    print(f"  API key prefix: {HERMES_API_KEY[:12]}...")
    print("=" * 60)
    print()
    print("  Waiting for Rabbit R1 connections...")
    print("  (Generate a QR code with setup.sh)")
    print()

    # Health check HTTP server on port 18788
    health_port = BRIDGE_PORT - 2
    health_server = await asyncio.start_server(health_endpoint, "0.0.0.0", health_port)
    print(f"  ✓ Health check: http://0.0.0.0:{health_port}")

    async with serve(r1_handler, BRIDGE_HOST, BRIDGE_PORT, ping_interval=20, ping_timeout=10):
        print(f"  ✓ Bridge listening on ws://{BRIDGE_HOST}:{BRIDGE_PORT}")
        await asyncio.get_running_loop().create_future()  # Run forever


if __name__ == "__main__":
    asyncio.run(main())
