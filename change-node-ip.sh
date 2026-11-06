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
  echo "错误: 数据库文件不存在 (请通过 HEADSCALE_DB_PATH 指定 db.sqlite 路径)" >&2
  exit 1
fi

if [[ -z "${NODE_ID}" || -z "${NEW_IPV4}" ]]; then
  echo "用法: $0 <节点ID> <新IPv4地址> [--swap]" >&2
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

# 1. 参数与 IP 合法性校验
if not node_id_raw.isdigit():
    print(f"错误: 节点 ID 必须为数字 (当前输入: {node_id_raw})", file=sys.stderr)
    sys.exit(1)
node_id = int(node_id_raw)

try:
    ip_obj = ipaddress.IPv4Address(new_ip_raw.strip())
except ValueError:
    print(f"错误: 无效的 IPv4 地址格式: {new_ip_raw}", file=sys.stderr)
    sys.exit(1)

subnet = ipaddress.IPv4Network("100.64.0.0/10")
if ip_obj not in subnet or ip_obj in (subnet.network_address, subnet.broadcast_address):
    print(f"错误: IP {ip_obj} 不在 Tailscale/Headscale 专用 CGNAT 网段 (100.64.0.0/10) 内", file=sys.stderr)
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
    # 2. 先平滑停止 Headscale 容器，确保内存状态与 WAL 完整落盘并释放数据库锁
    status = docker_api("POST", f"/containers/{container_name}/stop?t=8", timeout=12.0)
    if status in (204, 304):
        stopped = True
    time.sleep(0.15)

    # 备份数据库
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
            print(f"错误: 未找到该节点 (ID: {node_id})", file=sys.stderr)
            sys.exit(1)

        old_ip, node_name = row[0] or "", row[1] or f"node-{node_id}"
        if old_ip == new_ip:
            print(f"提示: 节点 [{node_name}] 当前 IP 已是 [{new_ip}]，无需重复修改。")
            sys.exit(0)

        cur.execute(
            "SELECT id, COALESCE(NULLIF(given_name, ''), hostname) "
            "FROM nodes WHERE ipv4 = ? AND id != ? AND deleted_at IS NULL LIMIT 1;",
            (new_ip, node_id),
        )
        conflict = cur.fetchone()

        if conflict and not allow_swap:
            c_id, c_name = conflict
            print(f"错误: IP {new_ip} 已被其他节点占用 ({c_name} [ID: {c_id}])", file=sys.stderr)
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
                f"修改成功！已互换节点 IP：[{node_name} (#{node_id})] {old_ip} -> {new_ip}，"
                f"[{c_name} (#{c_id})] {new_ip} -> {old_ip}。"
            )
        else:
            cur.execute(
                "UPDATE nodes SET ipv4 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (new_ip, node_id),
            )
            msg = f"修改成功！节点 [{node_name} (#{node_id})] 的 IPv4 已从 [{old_ip}] 变更为 [{new_ip}]。"

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
print(msg + " Headscale 服务已完成重载。")
PYEOF
