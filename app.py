import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from flask import Flask, jsonify, render_template_string, request

app = Flask(__name__)

# Auto-detect database and configuration paths (with environment variable overrides)
CONTAINER_NAME = os.environ.get("HEADSCALE_CONTAINER", "headscale").strip() or "headscale"

_env_db = os.environ.get("HEADSCALE_DB_PATH", "").strip()
DB_CANDIDATES = (
    ([_env_db] if _env_db else [])
    + [
        "/data/db.sqlite",
        "/data/headscale/lib/db.sqlite",
        "/var/lib/headscale/db.sqlite",
        "/etc/headscale/db.sqlite",
    ]
)

_env_cfg = os.environ.get("HEADSCALE_CONFIG_PATH", "").strip()
CONFIG_CANDIDATES = (
    ([_env_cfg] if _env_cfg else [])
    + [
        "/etc/headscale/config.yaml",
        "/data/headscale/config/config.yaml",
    ]
)

# Global mutex to serialize IP modification operations
CHANGE_LOCK = threading.Lock()


def get_db_path() -> str:
    for p in DB_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return DB_CANDIDATES[0]


def get_headscale_api_urls() -> list[str]:
    """Build ordered list of candidate Headscale API base URLs"""
    urls = []
    env_url = os.environ.get("HEADSCALE_URL", "").strip().rstrip("/")
    if env_url:
        urls.append(env_url)

    # Auto-detect port from config.yaml listen_addr (e.g. 0.0.0.0:8080 or 0.0.0.0:8085)
    detected_ports = []
    for cfg_path in CONFIG_CANDIDATES:
        if cfg_path and os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    content = f.read()
                m_port = re.search(r"^\s*listen_addr:\s*['\"]?(?:[^:'\"\s]*):(\d+)['\"]?", content, re.MULTILINE)
                if m_port:
                    detected_ports.append(m_port.group(1))
            except Exception:
                pass
            break

    for port in detected_ports + ["8085", "8080"]:
        for host in (CONTAINER_NAME, "headscale", "127.0.0.1", "host.docker.internal"):
            candidate = f"http://{host}:{port}"
            if candidate not in urls:
                urls.append(candidate)
    return urls


def get_headscale_prefixes() -> dict:
    """Read IPv4 prefix from Headscale config.yaml"""
    v4_prefix = "100.64.0.0/10"
    for cfg_path in CONFIG_CANDIDATES:
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    content = f.read()
                m_v4 = re.search(r"^\s*v4:\s*([0-9./]+)", content, re.MULTILINE)
                if m_v4:
                    v4_prefix = m_v4.group(1).strip()
            except Exception:
                pass
            break
    return {"v4": v4_prefix}


def extract_api_key_prefix(raw_key: str) -> str:
    """Extract prefix from hskey-api-<prefix>-<secret> or <prefix>.<secret>"""
    raw_key = raw_key.strip()
    if raw_key.startswith("hskey-api-"):
        rest = raw_key[len("hskey-api-"):]
        if len(rest) > 13 and rest[12] == "-":
            return rest[:12]
        parts = rest.split("-", 1)
        return parts[0] if parts else ""
    if "." in raw_key:
        return raw_key.split(".", 1)[0]
    return ""


def verify_token_via_sqlite(raw_key: str) -> bool:
    """Fallback token validation via SQLite when Headscale is reloading"""
    prefix = extract_api_key_prefix(raw_key)
    if not prefix:
        return False
    db_path = get_db_path()
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT 1 FROM api_keys "
                "WHERE prefix = ? AND (expiration IS NULL OR expiration > CURRENT_TIMESTAMP) "
                "LIMIT 1;",
                (prefix,),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False


def fetch_headscale_nodes(raw_key: str, timeout: float = 4.0) -> tuple[str, list | None]:
    """Fetch nodes from Headscale REST API and validate API key"""
    if not raw_key or not raw_key.strip():
        return "unauthorized", None

    clean_key = raw_key.strip()
    headers = {"Authorization": f"Bearer {clean_key}"}

    for base_url in get_headscale_api_urls():
        url = f"{base_url}/api/v1/node"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    payload = json.loads(resp.read().decode("utf-8"))
                    return "ok", payload.get("nodes", [])
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return "unauthorized", None
        except Exception:
            continue

    return "unreachable", None


def verify_token(raw_key: str) -> bool:
    if not raw_key or not raw_key.strip():
        return False
    status, _ = fetch_headscale_nodes(raw_key, timeout=3.0)
    if status == "ok":
        return True
    if status == "unauthorized":
        return False
    return verify_token_via_sqlite(raw_key)


def get_sqlite_node_enrichment() -> dict[int, dict]:
    """Read node metadata from SQLite"""
    enrichment = {}
    db_path = get_db_path()
    if not os.path.exists(db_path):
        return enrichment
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, ipv4, ipv6, hostname, given_name "
                "FROM nodes WHERE deleted_at IS NULL;"
            )
            for row in cur.fetchall():
                nid, ipv4, ipv6, hostname, given_name = row
                enrichment[int(nid)] = {
                    "ipv4": ipv4 or "",
                    "ipv6": ipv6 or "",
                    "hostname": hostname or "",
                    "given_name": given_name or "",
                }
        finally:
            conn.close()
    except Exception:
        pass
    return enrichment


def format_nodes_list(raw_nodes: list) -> list[dict]:
    """Format nodes list for frontend"""
    enrichment = get_sqlite_node_enrichment()
    nodes = []

    for n in raw_nodes:
        raw_id = n.get("id")
        try:
            nid = int(raw_id)
        except (ValueError, TypeError):
            continue

        ipv4 = ""
        ipv6 = ""
        for ip in n.get("ipAddresses", []) or []:
            if ":" in ip and not ipv6:
                ipv6 = ip
            elif ":" not in ip and not ipv4:
                ipv4 = ip

        extra = enrichment.get(nid, {})
        if not ipv4 and extra.get("ipv4"):
            ipv4 = extra["ipv4"]
        if not ipv6 and extra.get("ipv6"):
            ipv6 = extra["ipv6"]

        given_name = n.get("givenName") or extra.get("given_name") or ""
        hostname = n.get("name") or extra.get("hostname") or ""
        display_name = given_name or hostname or f"node-{nid}"

        user_obj = n.get("user") or {}
        user_name = user_obj.get("name") or user_obj.get("displayName") or ""

        tags = n.get("tags") or []
        if not tags and n.get("preAuthKey") and isinstance(n["preAuthKey"], dict):
            tags = n["preAuthKey"].get("aclTags") or []

        expiry = n.get("expiry")
        if expiry and str(expiry).startswith("0001-"):
            expiry = None

        nodes.append({
            "id": nid,
            "displayName": display_name,
            "givenName": given_name,
            "hostname": hostname,
            "ipv4": ipv4,
            "ipv6": ipv6,
            "online": bool(n.get("online", False)),
            "lastSeen": n.get("lastSeen") or "",
            "expiry": expiry,
            "user": user_name,
            "tags": tags,
        })

    nodes.sort(key=lambda x: x["displayName"].lower())
    return nodes


def docker_api_call(method: str, path: str, timeout: float = 15.0) -> tuple[int, str]:
    """Call Docker Engine API via /var/run/docker.sock"""
    sock_path = "/var/run/docker.sock"
    if not os.path.exists(sock_path):
        return 0, "Docker socket not found"

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        req = f"{method} {path} HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        s.sendall(req.encode("utf-8"))
        chunks = []
        while True:
            buf = s.recv(4096)
            if not buf:
                break
            chunks.append(buf)
        raw_resp = b"".join(chunks).decode("utf-8", errors="ignore")
        lines = raw_resp.splitlines()
        first_line = lines[0] if lines else ""
        parts = first_line.split()
        code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        return code, raw_resp
    except Exception as e:
        return -1, str(e)
    finally:
        s.close()


def wait_for_headscale_ready(api_key: str, expected_ips: dict[int, str] | None = None, max_wait: float = 14.0) -> list[dict] | None:
    """Wait for Headscale container to come back online and serve updated node IPs"""
    deadline = time.time() + max_wait
    last_nodes = None
    while time.time() < deadline:
        status, raw_nodes = fetch_headscale_nodes(api_key, timeout=2.0)
        if status == "ok" and raw_nodes is not None:
            formatted = format_nodes_list(raw_nodes)
            last_nodes = formatted
            if not expected_ips:
                return formatted
            ip_map = {n["id"]: n["ipv4"] for n in formatted}
            if all(ip_map.get(nid) == target_ip for nid, target_ip in expected_ips.items()):
                return formatted
        time.sleep(0.25)
    return last_nodes


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Machines - Headscale</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    :root {
      --bg: #ffffff;
      --hover-bg: #f1f3f3;
      --mist-100: #f1f3f3;
      --mist-200: #e2e5e9;
      --mist-300: #cbd1d8;
      --mist-500: #6b7280;
      --mist-600: #4b5563;
      --mist-700: #374151;
      --mist-800: #1f2937;
      --mist-900: #111827;
      --indigo-500: #6366f1;
      --indigo-600: #4f46e5;
      --blue-600: #2563eb;
      --green-600: #16a34a;
      --amber-600: #d97706;
      --red-600: #dc2626;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background-color: var(--bg);
      color: var(--mist-900);
      -webkit-font-smoothing: antialiased;
      line-height: 1.5;
      font-size: 14px;
    }

    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }

    main.container {
      max-width: 1200px;
      margin: 16px auto 96px;
      padding: 0 24px;
      overscroll-behavior: contain;
    }

    /* Authentication box (only shown before API key is verified) */
    .auth-box {
      max-width: 420px;
      margin: 88px auto 0;
      padding: 24px;
      border: 1px solid var(--mist-200);
      border-radius: 8px;
      background: #ffffff;
    }
    .auth-title {
      font-size: 16px;
      font-weight: 600;
      color: var(--mist-900);
      margin-bottom: 6px;
    }
    .auth-desc {
      font-size: 13px;
      color: var(--mist-500);
      margin-bottom: 16px;
    }

    /* Search & Filter Toolbar */
    .toolbar {
      margin-bottom: 16px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px;
    }

    .search-wrap {
      position: relative;
      width: 256px;
    }

    .input-base {
      width: 100%;
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 14px;
      line-height: 20px;
      background-color: #ffffff;
      border: 1px solid var(--mist-200);
      color: var(--mist-900);
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    .input-base::placeholder {
      color: #9ca3af;
    }
    .input-base:focus {
      border-color: var(--indigo-500);
      box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.25);
    }

    .pill-btn {
      padding: 6px 12px;
      border-radius: 9999px;
      font-size: 14px;
      line-height: 20px;
      font-weight: 500;
      border: 1px solid var(--mist-200);
      background-color: #ffffff;
      color: var(--mist-700);
      cursor: pointer;
      transition: border-color 0.15s, background-color 0.15s;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      user-select: none;
      text-decoration: none;
    }
    .pill-btn:hover {
      background-color: var(--hover-bg);
      border-color: var(--mist-300);
    }
    .pill-btn.active {
      border-color: var(--indigo-500);
      color: var(--indigo-600);
      background-color: rgba(99, 102, 241, 0.06);
    }

    .btn-primary {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 6px;
      background-color: var(--indigo-500);
      padding: 8px 14px;
      font-size: 14px;
      font-weight: 600;
      color: #ffffff;
      border: none;
      cursor: pointer;
      transition: background-color 0.15s;
      white-space: nowrap;
    }
    .btn-primary:hover:not(:disabled) {
      background-color: rgba(99, 102, 241, 0.9);
    }
    .btn-primary:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .btn-sm {
      padding: 4px 10px;
      font-size: 12.5px;
      border-radius: 6px;
      height: 28px;
    }

    .btn-ghost {
      background-color: #ffffff;
      border: 1px solid var(--mist-200);
      color: var(--mist-700);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 500;
      transition: background-color 0.15s;
    }
    .btn-ghost:hover:not(:disabled) {
      background-color: var(--hover-bg);
    }

    .btn-swap {
      background-color: var(--amber-600);
      color: #ffffff;
      border: none;
      cursor: pointer;
      font-weight: 600;
    }
    .btn-swap:hover:not(:disabled) {
      background-color: #b45309;
    }

    .machine-count {
      margin-left: auto;
      font-size: 14px;
      white-space: nowrap;
      color: var(--mist-500);
    }

    /* Alert Banner */
    .alert-bar {
      display: none;
      margin-bottom: 12px;
      padding: 10px 14px;
      border-radius: 6px;
      font-size: 13px;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    .alert-bar.success {
      display: flex;
      background-color: #f0fdf4;
      border: 1px solid #bbf7d0;
      color: #166534;
    }
    .alert-bar.error {
      display: flex;
      background-color: #fef2f2;
      border: 1px solid #fecaca;
      color: #991b1b;
    }

    /* Table Styles */
    .table-wrap {
      overflow-x: auto;
    }
    table.machines-table {
      width: 100%;
      min-width: 640px;
      table-layout: auto;
      border-collapse: collapse;
    }
    table.machines-table thead {
      color: var(--mist-600);
    }
    table.machines-table th {
      padding: 0 0 8px 0;
      text-align: left;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      user-select: none;
    }
    table.machines-table th:first-child {
      padding-left: 8px;
    }
    .th-btn {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      cursor: pointer;
      background: none;
      border: none;
      color: inherit;
      font: inherit;
      text-transform: uppercase;
    }
    .th-btn:hover {
      color: var(--mist-900);
    }

    table.machines-table tbody {
      vertical-align: top;
      border-top: 1px solid var(--mist-100);
    }
    table.machines-table tbody tr {
      border-bottom: 1px solid var(--mist-100);
      background-color: #ffffff;
      transition: background-color 0.12s;
      cursor: pointer;
    }
    table.machines-table tbody tr:hover,
    table.machines-table tbody tr.editing-row {
      background-color: var(--hover-bg);
    }

    table.machines-table td {
      padding: 10px 8px 10px 0;
    }
    table.machines-table td:first-child {
      padding-left: 8px;
    }

    .node-name {
      font-weight: 600;
      line-height: 1.375;
      color: var(--mist-900);
      transition: color 0.12s;
    }
    tr:hover .node-name {
      color: var(--blue-600);
    }
    .node-sub {
      font-size: 14px;
      color: var(--mist-900);
      opacity: 0.5;
    }
    .badge-row {
      margin-top: 6px;
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }
    .badge-expiry {
      height: 20px;
      font-size: 12px;
      padding: 2px 6px;
      border-radius: 6px;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      background-color: var(--mist-200);
      color: var(--mist-800);
    }
    .badge-tag {
      height: 20px;
      font-size: 12px;
      padding: 2px 6px;
      border-radius: 6px;
      white-space: nowrap;
      display: inline-flex;
      align-items: center;
      background-color: var(--mist-100);
      color: var(--mist-700);
    }
    tr:hover .badge-tag {
      background-color: #e5e7eb;
    }

    /* Addresses column & inline editor */
    .addr-display {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      color: var(--mist-900);
    }
    .addr-chevron {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border-radius: 9999px;
      padding: 4px;
      color: var(--mist-600);
      border: none;
      background: transparent;
      cursor: pointer;
    }
    .addr-chevron:hover {
      background-color: var(--mist-200);
    }

    .inline-editor {
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-width: 270px;
    }
    .inline-controls {
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .inline-input {
      width: 142px;
      height: 28px;
      padding: 0 8px;
      font-size: 13.5px;
      border-radius: 6px;
      background: #ffffff;
      border: 1px solid var(--indigo-500);
      box-shadow: 0 0 0 2px rgba(99, 102, 241, 0.2);
      color: var(--mist-900);
      outline: none;
    }
    .inline-hint {
      font-size: 12px;
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }
    .inline-hint.ok { color: var(--green-600); }
    .inline-hint.conflict { color: var(--amber-600); }
    .inline-hint.invalid { color: var(--red-600); }

    /* Last Seen column */
    .last-seen-wrap {
      display: flex;
      align-items: flex-start;
      gap: 6px;
    }
    .status-icon {
      margin-top: 2px;
      width: 16px;
      height: 16px;
      flex-shrink: 0;
    }
    .status-online { color: var(--green-600); }
    .status-offline { color: var(--mist-200); }
    .last-seen-main {
      font-size: 14px;
      color: var(--mist-600);
    }
    .last-seen-rel {
      font-size: 12px;
      color: var(--mist-900);
      opacity: 0.5;
    }

    .toast {
      position: fixed;
      bottom: 20px;
      right: 20px;
      background: var(--mist-900);
      color: #ffffff;
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 13px;
      box-shadow: 0 6px 16px rgba(0, 0, 0, 0.12);
      z-index: 100;
      display: none;
    }
  </style>
</head>
<body>
  <main class="container">
    <!-- 1. Authentication View -->
    <div id="authView" class="auth-box" style="display: none;">
      <div class="auth-title">Headscale Authentication</div>
      <div class="auth-desc">Enter your Admin API Key to view and manage machine IPv4 addresses.</div>
      <div style="display: flex; gap: 8px;">
        <input type="password" id="apiKey" class="input-base mono" placeholder="hskey-api-..." autocomplete="off">
        <button type="button" id="authBtn" class="btn-primary" onclick="loadNodes(true)">Connect</button>
      </div>
      <div id="authError" style="display:none; margin-top: 10px; font-size: 12.5px; color: var(--red-600);"></div>
    </div>

    <!-- 2. Machines List View -->
    <div id="mainView" style="display: none;">
      <div class="toolbar">
        <div class="search-wrap">
          <input id="searchInput" maxlength="100" placeholder="Search by name or IP address..." class="input-base" value="" oninput="renderTable()">
        </div>

        <button type="button" id="statusFilterBtn" class="pill-btn" onclick="cycleStatusFilter()">
          <span id="statusFilterLabel">Status</span>
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"></path></svg>
        </button>

        <button type="button" class="pill-btn" onclick="loadNodes(true)">
          Refresh
        </button>

        <button type="button" class="pill-btn" onclick="switchApiKey()">
          API Key
        </button>

        <span id="machineCount" class="machine-count">0 machines</span>
      </div>

      <div id="resultBanner" class="alert-bar">
        <span id="resultText"></span>
        <button type="button" class="btn-ghost btn-sm" onclick="hideAlert()" style="height:22px; padding:0 8px; font-size:11.5px;">Dismiss</button>
      </div>

      <div class="table-wrap">
        <table class="machines-table">
          <thead>
            <tr>
              <th>
                <button type="button" class="th-btn" onclick="toggleSort('name')">
                  Name
                  <svg id="sortIcon-name" xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m18 15-6-6-6 6"></path></svg>
                </button>
              </th>
              <th style="width: 34%;">
                <button type="button" class="th-btn" onclick="toggleSort('ip')">
                  Addresses
                </button>
              </th>
              <th>
                <button type="button" class="th-btn" onclick="toggleSort('lastSeen')">
                  Last Seen
                </button>
              </th>
            </tr>
          </thead>
          <tbody id="nodesTableBody"></tbody>
        </table>
      </div>
    </div>
  </main>

  <div id="toast" class="toast"></div>

  <script>
    const STORAGE_KEY = 'hs_ip_manager_api_key';
    let nodesList = [];
    let editingNodeId = null;
    let isSubmitting = false;
    let statusFilter = 'all'; // 'all' | 'online' | 'offline'
    let sortField = 'name';
    let sortAsc = true;

    window.addEventListener('DOMContentLoaded', () => {
      const savedKey = localStorage.getItem(STORAGE_KEY);
      if (savedKey) {
        document.getElementById('apiKey').value = savedKey;
        loadNodes(false);
      } else {
        showAuthView();
      }

      document.getElementById('apiKey').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') loadNodes(true);
      });
    });

    function getCleanKey() {
      return document.getElementById('apiKey').value.trim();
    }

    function showAuthView(errorMsg = '') {
      document.getElementById('mainView').style.display = 'none';
      document.getElementById('authView').style.display = 'block';
      const errEl = document.getElementById('authError');
      if (errorMsg) {
        errEl.textContent = errorMsg;
        errEl.style.display = 'block';
      } else {
        errEl.style.display = 'none';
      }
      setTimeout(() => document.getElementById('apiKey').focus(), 50);
    }

    function switchApiKey() {
      localStorage.removeItem(STORAGE_KEY);
      document.getElementById('apiKey').value = '';
      editingNodeId = null;
      showAuthView();
    }

    function cycleStatusFilter() {
      const order = ['all', 'online', 'offline'];
      statusFilter = order[(order.indexOf(statusFilter) + 1) % order.length];
      const btn = document.getElementById('statusFilterBtn');
      const label = document.getElementById('statusFilterLabel');
      if (statusFilter === 'all') {
        label.textContent = 'Status';
        btn.classList.remove('active');
      } else if (statusFilter === 'online') {
        label.textContent = 'Status: Online';
        btn.classList.add('active');
      } else {
        label.textContent = 'Status: Offline';
        btn.classList.add('active');
      }
      renderTable();
    }

    function toggleSort(field) {
      if (sortField === field) {
        sortAsc = !sortAsc;
      } else {
        sortField = field;
        sortAsc = true;
      }
      renderTable();
    }

    function showToast(text) {
      const el = document.getElementById('toast');
      el.textContent = text;
      el.style.display = 'block';
      clearTimeout(window.__toastTimer);
      window.__toastTimer = setTimeout(() => {
        el.style.display = 'none';
      }, 2200);
    }

    async function loadNodes(fromUserAction = false) {
      const key = getCleanKey();
      const authBtn = document.getElementById('authBtn');
      if (!key) {
        showAuthView('Please enter a valid Admin API Key.');
        return;
      }

      if (authBtn) {
        authBtn.disabled = true;
        authBtn.textContent = 'Connecting...';
      }

      try {
        const res = await fetch('api/nodes', {
          headers: { 'Authorization': 'Bearer ' + key }
        });
        const data = await res.json();

        if (!res.ok) {
          if (res.status === 401 || res.status === 403) {
            localStorage.removeItem(STORAGE_KEY);
            showAuthView(data.error || 'Invalid or expired API Key.');
          } else {
            setTimeout(() => loadNodes(false), 1500);
          }
          return;
        }

        nodesList = Array.isArray(data) ? data : (data.nodes || []);
        localStorage.setItem(STORAGE_KEY, key);

        document.getElementById('authView').style.display = 'none';
        document.getElementById('mainView').style.display = 'block';

        renderTable();
        if (fromUserAction) {
          showToast(`Loaded ${nodesList.length} machines`);
        }
      } catch (e) {
        showAuthView('Network error: ' + e.message);
      } finally {
        if (authBtn) {
          authBtn.disabled = false;
          authBtn.textContent = 'Connect';
        }
      }
    }

    function formatRelativeTime(isoStr) {
      if (!isoStr) return '';
      const d = new Date(isoStr);
      if (isNaN(d.getTime())) return '';
      const diffSec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
      if (diffSec < 60) return `${diffSec} seconds ago`;
      const diffMin = Math.floor(diffSec / 60);
      if (diffMin < 60) return `${diffMin} minute${diffMin === 1 ? '' : 's'} ago`;
      const diffHr = Math.floor(diffMin / 60);
      if (diffHr < 24) return `${diffHr} hour${diffHr === 1 ? '' : 's'} ago`;
      const days = Math.floor(diffHr / 24);
      const remHr = diffHr % 24;
      if (remHr > 0) {
        return `${days} day${days === 1 ? '' : 's'}, ${remHr} hour${remHr === 1 ? '' : 's'} ago`;
      }
      return `${days} day${days === 1 ? '' : 's'} ago`;
    }

    function formatExpiryBadge(expiryStr) {
      if (!expiryStr) return 'No expiry';
      const d = new Date(expiryStr);
      if (isNaN(d.getTime())) return 'No expiry';
      const opts = { month: 'short', day: 'numeric', year: 'numeric' };
      const formatted = d.toLocaleDateString('en-US', opts);
      return d.getTime() < Date.now() ? `Expired ${formatted}` : `Expires ${formatted}`;
    }

    function isValidTailscaleIpv4(ipStr) {
      const parts = ipStr.split('.');
      if (parts.length !== 4) return { valid: false, reason: 'Invalid IPv4 format' };
      const nums = [];
      for (const p of parts) {
        if (!/^\\d+$/.test(p)) return { valid: false, reason: 'Octets must be numeric' };
        const n = Number(p);
        if (n < 0 || n > 255) return { valid: false, reason: 'Octets must be 0-255' };
        nums.push(n);
      }
      if (nums[0] !== 100 || nums[1] < 64 || nums[1] > 127) {
        return { valid: false, reason: 'Must be within 100.64.0.0/10' };
      }
      if (nums[1] === 64 && nums[2] === 0 && nums[3] === 0) {
        return { valid: false, reason: 'Cannot use network address 100.64.0.0' };
      }
      if (nums[1] === 127 && nums[2] === 255 && nums[3] === 255) {
        return { valid: false, reason: 'Cannot use broadcast address' };
      }
      return { valid: true };
    }

    function openRowEdit(nodeId, event) {
      if (event) event.stopPropagation();
      if (isSubmitting) return;
      if (editingNodeId === nodeId) return;
      editingNodeId = nodeId;
      renderTable();
      const input = document.getElementById(`inline-ip-${nodeId}`);
      if (input) {
        input.focus();
        input.select();
        updateInlineValidation(nodeId);
      }
    }

    function cancelRowEdit(event) {
      if (event) event.stopPropagation();
      if (isSubmitting) return;
      editingNodeId = null;
      renderTable();
    }

    function updateInlineValidation(nodeId) {
      const input = document.getElementById(`inline-ip-${nodeId}`);
      const hint = document.getElementById(`inline-hint-${nodeId}`);
      if (!input || !hint) return;

      const val = input.value.trim();
      const currentNode = nodesList.find(n => n.id === nodeId);
      if (!val || (currentNode && currentNode.ipv4 === val)) {
        hint.className = 'inline-hint';
        hint.innerHTML = '<span style="color:var(--mist-500);">Press Enter to save, Esc to cancel</span>';
        return;
      }

      const check = isValidTailscaleIpv4(val);
      if (!check.valid) {
        hint.className = 'inline-hint invalid';
        hint.innerHTML = `<span>✕ ${check.reason}</span>`;
        return;
      }

      const conflict = nodesList.find(n => n.ipv4 === val && n.id !== nodeId);
      if (conflict) {
        hint.className = 'inline-hint conflict';
        hint.innerHTML = `
          <span>In use by <strong>${conflict.displayName}</strong></span>
          <button type="button" class="btn-swap btn-sm" style="height:22px; padding:0 8px; font-size:11.5px;"
            onclick="submitRowIp(${nodeId}, true, event)">
            Swap IP
          </button>
        `;
        return;
      }

      hint.className = 'inline-hint ok';
      hint.innerHTML = `<span>✓ ${val} available</span>`;
    }

    async function submitRowIp(nodeId, allowSwap = false, event = null) {
      if (event) event.stopPropagation();
      if (isSubmitting) return;

      const input = document.getElementById(`inline-ip-${nodeId}`);
      const saveBtn = document.getElementById(`inline-save-${nodeId}`);
      if (!input) return;

      const newIp = input.value.trim();
      const key = getCleanKey();

      const check = isValidTailscaleIpv4(newIp);
      if (!check.valid) {
        showAlert(check.reason, false);
        return;
      }

      isSubmitting = true;
      input.disabled = true;
      if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = 'Saving...';
      }
      hideAlert();

      try {
        const resp = await fetch('api/change-ip', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + key
          },
          body: JSON.stringify({
            node: nodeId,
            new_ip: newIp,
            swap: allowSwap
          })
        });
        const data = await resp.json();
        showAlert(data.message, data.success);

        if (data.success) {
          editingNodeId = null;
          if (Array.isArray(data.nodes)) {
            nodesList = data.nodes;
            renderTable();
          } else {
            await loadNodes(false);
          }
          showToast('IP updated');
        } else {
          input.disabled = false;
          if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = 'Save';
          }
        }
      } catch (err) {
        showAlert('Request failed: ' + err.message, false);
        input.disabled = false;
        if (saveBtn) {
          saveBtn.disabled = false;
          saveBtn.textContent = 'Save';
        }
      } finally {
        isSubmitting = false;
      }
    }

    function ipToNum(ip) {
      if (!ip) return 0;
      return ip.split('.').reduce((acc, oct) => (acc << 8) + (parseInt(oct, 10) || 0), 0) >>> 0;
    }

    function renderTable() {
      const tbody = document.getElementById('nodesTableBody');
      const query = (document.getElementById('searchInput').value || '').trim().toLowerCase();

      const filtered = nodesList.filter(n => {
        if (statusFilter === 'online' && !n.online) return false;
        if (statusFilter === 'offline' && n.online) return false;
        if (!query) return true;
        const hay = [
          n.id, n.displayName, n.givenName, n.hostname,
          n.ipv4, n.ipv6, n.user,
          ...(n.tags || [])
        ].join(' ').toLowerCase();
        return hay.includes(query);
      });

      filtered.sort((a, b) => {
        let cmp = 0;
        if (sortField === 'name') {
          cmp = String(a.displayName).localeCompare(String(b.displayName));
        } else if (sortField === 'ip') {
          cmp = ipToNum(a.ipv4) - ipToNum(b.ipv4);
        } else if (sortField === 'lastSeen') {
          if (a.online !== b.online) {
            cmp = a.online ? -1 : 1;
          } else {
            cmp = new Date(b.lastSeen || 0) - new Date(a.lastSeen || 0);
          }
        }
        return sortAsc ? cmp : -cmp;
      });

      document.getElementById('machineCount').textContent = `${filtered.length} machine${filtered.length === 1 ? '' : 's'}`;

      if (filtered.length === 0) {
        tbody.innerHTML = `
          <tr>
            <td colspan="3" style="text-align: center; color: var(--mist-500); padding: 36px 0;">
              No machines found
            </td>
          </tr>
        `;
        return;
      }

      tbody.innerHTML = '';
      filtered.forEach(n => {
        const isEditing = (editingNodeId === n.id);
        const tr = document.createElement('tr');
        if (isEditing) tr.className = 'editing-row';
        tr.onclick = (e) => openRowEdit(n.id, e);

        const isTagOwned = (n.tags && n.tags.length > 0) || !n.user || n.user.toLowerCase().includes('tagged');
        const ownerText = isTagOwned ? 'Tag-owned' : n.user;
        const expiryText = formatExpiryBadge(n.expiry);
        const tagsHtml = (n.tags || []).map(t => `<span class="badge-tag">${t}</span>`).join('');

        // ADDRESSES column
        let addrHtml = '';
        if (isEditing) {
          addrHtml = `
            <div class="inline-editor" onclick="event.stopPropagation()">
              <div class="inline-controls">
                <input id="inline-ip-${n.id}" type="text" class="inline-input mono" value="${n.ipv4 || ''}"
                  oninput="updateInlineValidation(${n.id})"
                  onkeydown="if(event.key==='Enter') submitRowIp(${n.id}, false, event); if(event.key==='Escape') cancelRowEdit(event);">
                <button type="button" id="inline-save-${n.id}" class="btn-primary btn-sm" onclick="submitRowIp(${n.id}, false, event)">Save</button>
                <button type="button" class="btn-ghost btn-sm" onclick="cancelRowEdit(event)">Cancel</button>
              </div>
              <div id="inline-hint-${n.id}" class="inline-hint"></div>
            </div>
          `;
        } else {
          addrHtml = `
            <div class="addr-display">
              <span>${n.ipv4 || '-'}</span>
              <button type="button" class="addr-chevron" title="Edit IP" onclick="openRowEdit(${n.id}, event)">
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"></path></svg>
              </button>
            </div>
          `;
        }

        // LAST SEEN column
        let lastSeenHtml = '';
        if (n.online) {
          lastSeenHtml = `
            <div class="last-seen-wrap">
              <svg class="status-icon status-online" viewBox="0 0 24 24" fill="currentColor">
                <title>Online</title>
                <circle cx="12" cy="12" r="8"></circle>
              </svg>
              <div>
                <p class="last-seen-main">Connected</p>
              </div>
            </div>
          `;
        } else {
          const dt = n.lastSeen ? new Date(n.lastSeen).toLocaleString('en-US') : '-';
          const rel = formatRelativeTime(n.lastSeen);
          lastSeenHtml = `
            <div class="last-seen-wrap">
              <svg class="status-icon status-offline" viewBox="0 0 24 24" fill="currentColor">
                <title>Offline</title>
                <circle cx="12" cy="12" r="8"></circle>
              </svg>
              <div>
                <p class="last-seen-main">${dt}</p>
                ${rel ? `<p class="last-seen-rel">${rel}</p>` : ''}
              </div>
            </div>
          `;
        }

        tr.innerHTML = `
          <td>
            <p class="node-name">${n.displayName}</p>
            <p class="node-sub">${ownerText}</p>
            <div class="badge-row">
              <span class="badge-expiry">${expiryText}</span>
              ${tagsHtml}
            </div>
          </td>
          <td>${addrHtml}</td>
          <td>${lastSeenHtml}</td>
        `;
        tbody.appendChild(tr);
      });
    }

    function showAlert(msg, isSuccess) {
      const banner = document.getElementById('resultBanner');
      const text = document.getElementById('resultText');
      banner.className = 'alert-bar ' + (isSuccess ? 'success' : 'error');
      text.textContent = msg;
    }

    function hideAlert() {
      const banner = document.getElementById('resultBanner');
      banner.className = 'alert-bar';
      document.getElementById('resultText').textContent = '';
    }
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/nodes", methods=["GET"])
def get_nodes():
    auth_header = request.headers.get("Authorization", "")
    raw_key = auth_header.replace("Bearer ", "").strip()

    if not raw_key:
        return jsonify({"error": "Unauthorized: Missing API Key."}), 401

    status, raw_nodes = fetch_headscale_nodes(raw_key, timeout=4.0)
    if status == "unauthorized":
        return jsonify({"error": "Unauthorized: Invalid or expired API Key."}), 401

    if status == "unreachable" or raw_nodes is None:
        if not verify_token_via_sqlite(raw_key):
            return jsonify({"error": "Unauthorized: Invalid or expired API Key."}), 401
        return jsonify({"error": "Headscale is reloading, please wait..."}), 502

    nodes = format_nodes_list(raw_nodes)
    return jsonify(nodes)


@app.route("/api/change-ip", methods=["POST"])
def change_ip():
    auth_header = request.headers.get("Authorization", "")
    raw_key = auth_header.replace("Bearer ", "").strip()

    if not verify_token(raw_key):
        return jsonify({"success": False, "message": "Unauthorized: Invalid or expired API Key."}), 403

    body = request.get_json(silent=True) or {}
    node_raw = str(body.get("node", "")).strip()
    new_ip_raw = str(body.get("new_ip", "")).strip()
    allow_swap = bool(body.get("swap", False))

    if not node_raw or not new_ip_raw:
        return jsonify({"success": False, "message": "Missing target node ID or new IPv4 address."}), 400

    if not node_raw.isdigit():
        return jsonify({"success": False, "message": f"Invalid node ID: {node_raw}"}), 400
    node_id = int(node_raw)

    prefixes = get_headscale_prefixes()
    try:
        ip_obj = ipaddress.IPv4Address(new_ip_raw)
    except ValueError:
        return jsonify({"success": False, "message": f"Invalid IPv4 address format: {new_ip_raw}"}), 400

    try:
        subnet = ipaddress.IPv4Network(prefixes["v4"], strict=False)
    except ValueError:
        subnet = ipaddress.IPv4Network("100.64.0.0/10")

    if ip_obj not in subnet or ip_obj in (subnet.network_address, subnet.broadcast_address):
        return jsonify({
            "success": False,
            "message": f"IP {ip_obj} is outside the configured Headscale subnet ({subnet})."
        }), 400

    new_ip = str(ip_obj)
    db_path = get_db_path()
    if not os.path.exists(db_path):
        return jsonify({"success": False, "message": f"Database file not found: {db_path}"}), 500

    if not CHANGE_LOCK.acquire(timeout=20.0):
        return jsonify({"success": False, "message": "Another IP change operation is in progress. Please try again."}), 429

    try:
        status, live_raw_nodes = fetch_headscale_nodes(raw_key, timeout=3.0)
        if status == "ok" and live_raw_nodes is not None:
            live_nodes = format_nodes_list(live_raw_nodes)
            target_live = next((n for n in live_nodes if n["id"] == node_id), None)
            if not target_live:
                return jsonify({"success": False, "message": f"Machine #{node_id} not found."}), 404
            if target_live["ipv4"] == new_ip:
                return jsonify({
                    "success": True,
                    "message": f"Machine {target_live['displayName']} already has IPv4 {new_ip}.",
                    "nodes": live_nodes,
                })
            conflict_live = next((n for n in live_nodes if n["ipv4"] == new_ip and n["id"] != node_id), None)
            if conflict_live and not allow_swap:
                return jsonify({
                    "success": False,
                    "message": (
                        f"IP {new_ip} is already assigned to {conflict_live['displayName']} (#{conflict_live['id']}). "
                        f"Click 'Swap IP' to exchange addresses between the two machines."
                    ),
                    "conflict_node": conflict_live,
                }), 400

        stopped = False
        expected_ips = {node_id: new_ip}
        try:
            stop_code, _ = docker_api_call("POST", f"/containers/{CONTAINER_NAME}/stop?t=8", timeout=12.0)
            if stop_code in (204, 304):
                stopped = True
            time.sleep(0.15)

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
                    return jsonify({"success": False, "message": f"Machine #{node_id} not found in database."}), 404

                old_ip = row[0] or ""
                node_name = row[1] or f"node-{node_id}"

                cur.execute(
                    "SELECT id, COALESCE(NULLIF(given_name, ''), hostname) "
                    "FROM nodes WHERE ipv4 = ? AND id != ? AND deleted_at IS NULL LIMIT 1;",
                    (new_ip, node_id),
                )
                conflict_row = cur.fetchone()

                if conflict_row and not allow_swap:
                    c_id, c_name = conflict_row
                    return jsonify({
                        "success": False,
                        "message": f"IP {new_ip} is already in use by {c_name} (#{c_id})."
                    }), 400

                cur.execute("BEGIN IMMEDIATE;")

                if conflict_row and allow_swap:
                    c_id, c_name = conflict_row
                    c_name = c_name or f"node-{c_id}"
                    cur.execute(
                        "UPDATE nodes SET ipv4 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                        (old_ip, c_id),
                    )
                    cur.execute(
                        "UPDATE nodes SET ipv4 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                        (new_ip, node_id),
                    )
                    expected_ips[int(c_id)] = old_ip
                    action_msg = (
                        f"Swapped IPs: {node_name} ({old_ip} → {new_ip}) and {c_name} ({new_ip} → {old_ip})."
                    )
                else:
                    cur.execute(
                        "UPDATE nodes SET ipv4 = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;",
                        (new_ip, node_id),
                    )
                    action_msg = f"Updated {node_name} IPv4 from {old_ip} to {new_ip}."

                conn.commit()
                try:
                    cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                except Exception:
                    pass
            finally:
                conn.close()
        finally:
            if stopped or os.path.exists("/var/run/docker.sock"):
                docker_api_call("POST", f"/containers/{CONTAINER_NAME}/start", timeout=12.0)

        updated_nodes = wait_for_headscale_ready(raw_key, expected_ips=expected_ips, max_wait=14.0)

        return jsonify({
            "success": True,
            "message": action_msg,
            "nodes": updated_nodes,
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Error: {str(e)}"}), 500
    finally:
        CHANGE_LOCK.release()


if __name__ == "__main__":
    listen_port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=listen_port, threaded=True)
