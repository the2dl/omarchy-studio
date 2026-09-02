#!/usr/bin/env python3
"""Minimal gpu-screen-recorder IPC client.

Usage: gsr-ipc.py <socket_path> <json_request>
Prints the raw reply line verbatim to stdout. Exits 1 on timeout.
"""
import socket
import sys
import time

sock_path = sys.argv[1]
request = sys.argv[2]
timeout = float(sys.argv[3]) if len(sys.argv) > 3 else 15.0

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(timeout)
s.connect(sock_path)
t0 = time.monotonic()
s.sendall(request.encode() + b"\n")

buf = b""
while b"\n" not in buf:
    try:
        chunk = s.recv(4096)
    except socket.timeout:
        print("TIMEOUT after %.2fs, partial=%r" % (time.monotonic() - t0, buf), file=sys.stderr)
        sys.exit(1)
    if not chunk:
        print("EOF, partial=%r" % buf, file=sys.stderr)
        sys.exit(1)
    buf += chunk

elapsed = time.monotonic() - t0
line = buf.split(b"\n")[0].decode()
print(line)
print("  (reply took %.3fs)" % elapsed, file=sys.stderr)
s.close()
