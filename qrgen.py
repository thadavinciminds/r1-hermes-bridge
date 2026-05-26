#!/usr/bin/env python3
"""QR code generator for R1 → Hermes bridge. Called from setup.ps1."""
import sys, json, os
import qrcode

def main():
    if len(sys.argv) < 2:
        print("Usage: python qrgen.py <qr_path> [payload_file]", file=sys.stderr)
        sys.exit(1)

    qr_path = sys.argv[1]

    # Read payload from file or stdin
    if len(sys.argv) >= 3:
        with open(sys.argv[2], "r") as f:
            payload = f.read().strip()
    else:
        payload = sys.stdin.read().strip()

    # Generate and save PNG
    img = qrcode.make(payload)
    img.save(qr_path)
    print(f"Saved: {qr_path} ({img.size[0]}x{img.size[1]})")

    # Print ASCII QR to terminal
    qr = qrcode.QRCode(box_size=2, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    qr.print_ascii()

if __name__ == "__main__":
    main()
