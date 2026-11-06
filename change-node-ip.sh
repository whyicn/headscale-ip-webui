#!/usr/bin/env bash
set -euo pipefail

NODE_ID="${1:-}"
NEW_IPV4="${2:-}"
MODE="${3:-}"
CONTAINER_NAME="${HEADSCALE_CONTAINER:-headscale}"

if [[ -n "${HEADSCALE_DB_PATH:-}" && -f "${HEADSCALE_DB_PATH}" ]]; then
  DB_PATH="${HEADSCALE_DB_PATH}"
elif [[ -f "/data/db.sqlite" ]]; then
  DB_PATH="/data/db.sqlite"
elif [[ -f "/data/headscale/lib/db.sqlite" ]]; then
  DB_PATH="/data/headscale/lib/db.sqlite"
elif [[ -f "/var/lib/headscale/db.sqlite" ]]; then
  DB_PATH="/var/lib/headscale/db.sqlite"
elif [[ -f "/etc/headscale/db.sqlite" ]]; then
  DB_PATH="/etc/headscale/db.sqlite"
else
  echo "Error: Database file not found (set HEADSCALE_DB_PATH to specify db.sqlite path) / 错误: 数据库文件不存在" >&2
  exit 1
fi

if [[ -z "${NODE_ID}" || -z "${NEW_IPV4}" ]]; then
  echo "Usage / 用法: $0 <node_id> <new_ipv4> [--swap]" >&2
  exit 1
fi

exec python3 - "${NODE_ID}" "${NEW_IPV4}" "${MODE}" "${DB_PATH}" "${CONTAINER_NAME}" << 'PYEOF'
import ipaddress
import os
import shutil
import socket
import sqlite3
import sys
import time
import urllib.request

node_id_raw, new_ip_raw, mode, db_path, container_name = sys.argv[1:6]
allow_swap = (mode == "--swap")

# 1. Validate parameters and IPv4 format
if not node_id_raw.isdigit():
    print(f"Error: Node ID must be numeric (got: {node_id_raw}) / 错误: 节点 ID 必须为数字", file=sys.stderr)
    sys.exit(1)
node_id = int(node_id_raw)

try:
    ip_obj = ipaddress.IPv4Address(new_ip_raw.strip())
except ValueError:
    print(f"Error: Invalid IPv4 address format: {new_ip_raw} / 错误: 无效的 IPv4 地址格式", file=sys.stderr)
    sys.exit(1)

subnet = ipaddress.IPv4Network("100.64.0.0/10")
if ip_obj not in subnet or ip_obj in (subnet.network_address, subnet.broadcast_address):
    print(f"Error: IP {ip_obj} is outside CGNAT subnet (100.64.0.0/10) / 错误: IP 不在专用网段内", file=sys.stderr)
    sys.exit(1)

new_ip = str(ip_obj)


def docker_api(method: str, path: str, timeout: float = 15.0) -> int:
    sock_path = "/var/run/docker.sock"
    if not os.path.exists(sock_path):
        return 0
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        req = f"{method} {path} HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        s.sendall(req.encode("utf-8"))
        chunks = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
        resp = b"".join(chunks).decode("utf-8", errors="ignore")
        first_line = resp.splitlines()[0] if resp else ""
        parts = first_line.split()
        return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    finally:
        s.close()


def wait_headscale_ready(max_wait: float = 12.0) -> bool:
    deadline = time.time() + max_wait
    urls = [
        f"http://{container_name}:8085/health",
        "http://127.0.0.1:8085/health",
        f"http://{container_name}:8080/health",
        "http://127.0.0.1:8080/health",
    ]
    while time.time() < deadline:
        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=1.5) as resp:
                    if resp.status == 200:
                        return True
            except Exception:
                pass
        time.sleep(0.25)
    return False


stopped = False
try:
    # 2. Gracefully stop Headscale container so in-memory state and WAL flush cleanly
    status = docker_api("POST", f"/containers/{container_name}/stop?t=8", timeout=12.0)
    if status in (204, 304):
        stopped = True
    time.sleep(0.15)

    # Backup SQLite database
    try:
        shutil.copy2(db_path, f"{db_path}.bak")
    except Exception:
        pass

    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA busy_timeout = 5000;")
        cur.execute(
            "SELECT ipv4, COALESCE(NULLIF(given_name, ''), hostname) "
            "FROM nodes WHERE id = ? AND deleted_at IS NULL LIMIT 1;",
            (node_id,),
        )
        row = cur.fetchone()
        if not row:
            print(f"Error: Machine not found (ID: {node_id}) / 错误: 未找到该节点", file=sys.stderr)
            sys.exit(1)

        old_ip, node_name = row[0] or "", row[1] or f"node-{node_id}"
        if old_ip == new_ip:
            print(f"Info: Machine [{node_name}] already has IP [{new_ip}] / 提示: 当前 IP 未变化。")
            sys.exit(0)

        cur.execute(
            "SELECT id, COALESCE(NULLIF(given_name, ''), hostname) "
            "FROM nodes WHERE ipv4 = ? AND id != ? AND deleted_at IS NULL LIMIT 1;",
            (new_ip, node_id),
        )
        conflict = cur.fetchone()

        if conflict and not allow_swap:
            c_id, c_name = conflict
            print(
                f"Error: IP {new_ip} is already in use by {c_name} [ID: {c_id}] (use --swap to exchange) / "
                f"错误: IP 已被占用 ({c_name} [ID: {c_id}])",
                file=sys.stderr,
            )
            sys.exit(1)

        cur.execute("BEGIN IMMEDIATE;")
        if conflict and allow_swap:
            c_id, c_name = conflict
            cur.execute(
                "UPDATE nodes SET ipv4 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (old_ip, c_id),
            )
            cur.execute(
                "UPDATE nodes SET ipv4 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (new_ip, node_id),
            )
            msg = (
                f"Success! Swapped IPs: [{node_name} (#{node_id})] {old_ip} -> {new_ip}, "
                f"[{c_name} (#{c_id})] {new_ip} -> {old_ip}."
            )
        else:
            cur.execute(
                "UPDATE nodes SET ipv4 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (new_ip, node_id),
            )
            msg = f"Success! Updated [{node_name} (#{node_id})] IPv4 from [{old_ip}] to [{new_ip}]."

        conn.commit()
        try:
            cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
    finally:
        conn.close()
finally:
    if stopped or os.path.exists("/var/run/docker.sock"):
        docker_api("POST", f"/containers/{container_name}/start", timeout=12.0)

wait_headscale_ready(12.0)
print(msg + " Headscale service reloaded.")
PYEOF
