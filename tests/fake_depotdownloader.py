#!/usr/bin/env python3
"""Mimics DepotDownloader's console behaviour closely enough for the queue tests."""
import sys
import time
from pathlib import Path

args = sys.argv[1:]
opt = lambda k: args[args.index(k) + 1] if k in args else None  # noqa: E731
out = Path(opt("-dir"))
if opt("-username"):
    sys.stdout.write(f'Enter account password for "{opt("-username")}": ')
    sys.stdout.flush()
    if sys.stdin.readline().strip() != "hunter2":
        print("\nFailed to authenticate with Steam: InvalidPassword")
        sys.exit(1)
    print()
elif "-qr" in args:
    print("Logging in with QR code...")
    print("Use the Steam Mobile App to sign in with this QR code:")
    print("█▀▀▀▀▀█ ▄▀▄ █▀▀▀▀▀█")
sys.stderr.write("STEAM GUARD! Please enter your 2-factor auth code from your authenticator app: ")
sys.stderr.flush()
if sys.stdin.readline().strip() != "12345":
    print("Failed to authenticate with Steam: InvalidLoginAuthCode")
    sys.exit(1)
if opt("-manifest") == "999":
    print("Error: Unable to download manifest 999 for depot " + opt("-depot"))
    sys.exit(1)
print("Got depot key for " + opt("-depot"))
(out / "bin").mkdir(parents=True, exist_ok=True)
for i, name in enumerate(["bin/game.exe", "data.pak"]):
    time.sleep(0.05)
    (out / name).write_bytes(b"x" * (i + 1))
    print(f"{(i + 1) * 50:6.2f}% {out / name}")
(out / ".DepotDownloader").mkdir(exist_ok=True)
print("Total downloaded: 3 bytes (3 bytes uncompressed) from 1 depots")
