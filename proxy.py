#!/usr/bin/env python3
"""R1 Bridge Proxy — runs on the laptop, forwards WebSocket to the real bridge.

Usage:
  pip install websockets
  python proxy.py

Listens on 0.0.0.0:18790 and forwards everything to the bridge via Tailscale.
"""
import asyncio
import websockets
import sys

BRIDGE_HOST = "100.116.144.9"
BRIDGE_PORT = 18790
LOCAL_PORT = 18790

async def proxy(local_ws, path):
    """Forward a WebSocket connection to the bridge."""
    bridge_url = f"ws://{BRIDGE_HOST}:{BRIDGE_PORT}{path}"
    print(f"  ← R1 connected, forwarding to {bridge_url}")
    try:
        async with websockets.connect(bridge_url) as remote_ws:
            async def forward(src, dst, name):
                try:
                    async for msg in src:
                        await dst.send(msg)
                except websockets.ConnectionClosed:
                    pass
                except Exception as e:
                    print(f"  [{name}] {e}")

            await asyncio.gather(
                forward(local_ws, remote_ws, "R1→bridge"),
                forward(remote_ws, local_ws, "bridge→R1"),
            )
    except Exception as e:
        print(f"  ✗ Bridge unreachable: {e}")

async def main():
    print(f"🐇 R1 Proxy — listening on ws://0.0.0.0:{LOCAL_PORT}")
    print(f"   Forwarding to ws://{BRIDGE_HOST}:{BRIDGE_PORT}")
    print(f"   Press Ctrl+C to stop")
    async with websockets.serve(proxy, "0.0.0.0", LOCAL_PORT):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDone.")
