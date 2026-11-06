# Headscale IP WebUI (`headscale-ip-webui`)

[**English**](#english) | [**简体中文**](#简体中文)

---

<a id="english"></a>

## English

A lightweight, safe **Web UI & CLI tool to modify, assign, and atomically swap machine IPv4 addresses in Headscale**.

> Solves a common pain point in the Headscale ecosystem: neither Headscale's official CLI nor popular web panels (`headscale-ui`, `headplane`) natively support modifying or swapping IPv4 addresses of registered nodes.

### ✨ Key Features

- **Bilingual Web UI (Default English + Chinese Toggle)**: Clean Tailscale / Headplane-inspired interface with instant `EN / 中文` language switching.
- **Inline IPv4 Editing & Real-Time Validation**: Click any machine's IP to edit inline, with instant CGNAT subnet (`100.64.0.0/10` or custom prefix) and conflict validation.
- **One-Click Atomic IP Swap (`Swap IP`)**: If the target IPv4 is already assigned to another machine, swap the two machines' IP addresses atomically with a single click—no temporary third IP needed.
- **Safe Container & SQLite Transaction Lifecycle**:
  1. Gracefully stops the Headscale container via Docker Socket so in-memory state and SQLite WAL flush cleanly;
  2. Automatically creates a `db.sqlite.bak` backup;
  3. Updates node IPv4(s) inside an `IMMEDIATE` transaction and runs `PRAGMA wal_checkpoint(TRUNCATE)`;
  4. Restarts the Headscale container and waits until the API is healthy and serving the updated IP(s).
- **Native Headscale API Key Auth**: Authenticates directly against your Headscale Admin API Key—no extra user database required.
- **Zero-Config Auto-Detection**: Automatically detects your running Headscale container, `db.sqlite` path, `config.yaml`, and listen port.

---

### 🚀 One-Line Installation & Auto-Configuration (Recommended)

Run the following single command on your Headscale host to auto-detect paths, build, and start the service:

```bash
curl -fsSL https://raw.githubusercontent.com/whyicn/headscale-ip-webui/main/install.sh | bash
```

Custom port (e.g. port `5080`):

```bash
curl -fsSL https://raw.githubusercontent.com/whyicn/headscale-ip-webui/main/install.sh | PORT=5080 bash
```

Once started, open `http://<server-ip>:5000` in your browser and enter your Headscale Admin API Key.

> 💡 **Need an Admin API Key?** Run:
> ```bash
> docker exec headscale headscale apikeys create --expiration 90d
> ```

---

### 🐳 Alternative Deployment Methods

#### Option 1: Docker Compose

```bash
git clone https://github.com/whyicn/headscale-ip-webui.git
cd headscale-ip-webui

HEADSCALE_DATA_DIR=/data/headscale/lib \
HEADSCALE_CONFIG_DIR=/data/headscale/config \
docker compose up -d --build
```

#### Option 2: Standalone CLI Script

You can also use [`change-node-ip.sh`](./change-node-ip.sh) directly from the terminal without the Web UI:

```bash
# Change machine ID=3 IPv4 to 100.64.0.10
./change-node-ip.sh 3 100.64.0.10

# Swap IPv4 with whichever machine currently holds 100.64.0.10
./change-node-ip.sh 3 100.64.0.10 --swap
```

---

### ⚙️ Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PORT` | `5000` | Web UI listening port |
| `HEADSCALE_CONTAINER` | `headscale` | Target Headscale Docker container name |
| `HEADSCALE_DB_PATH` | Auto-detected | Path to `db.sqlite` |
| `HEADSCALE_CONFIG_PATH` | Auto-detected | Path to `config.yaml` (auto-reads `listen_addr` & `prefixes.v4`) |
| `HEADSCALE_URL` | Auto-detected | Headscale API base URL (e.g. `http://127.0.0.1:8080` or `http://headscale:8085`) |

---
---

<a id="简体中文"></a>

## 简体中文

轻量级、安全的 **Headscale 节点 IPv4 可视化管理与原子互换工具**（同时提供中英双语 Web 界面与 CLI 命令行脚本）。

> 解决了 Headscale 官方及现有第三方 UI（如 `headscale-ui`、`headplane`）不支持直接修改或互换已注册节点 IPv4 地址的痛点。

### ✨ 核心特性

- **中英双语界面（默认英文，一键切换中文）**：极简 Tailscale / Headplane 风格，右上角一键切换 `EN / 中文`。
- **可视化行内编辑与实时校验**：点击节点 IP 即可修改，实时校验 CGNAT 网段（`100.64.0.0/10` 或自定义网段）并检测冲突。
- **一键原子互换 IP（Swap IP）**：当目标 IP 已被其他节点占用时，支持一键直接交换两个节点的 IPv4 地址，无需借用第三个中转 IP。
- **安全的事务与容器生命周期控制**：
  1. 修改前通过 Docker Socket 平滑停止 Headscale 容器（确保内存状态与 SQLite WAL 完整落盘释放锁）；
  2. 自动创建 `db.sqlite.bak` 备份；
  3. 执行 SQLite `IMMEDIATE` 事务更新并执行 `wal_checkpoint(TRUNCATE)`；
  4. 自动重启 Headscale 容器并轮询等待服务恢复就绪。
- **原生 API Key 鉴权**：直接复用 Headscale 的 Admin API Key，无需额外配置账号密码。
- **自动环境探测**：自动识别宿主机上正在运行的 Headscale 容器、数据库路径、`config.yaml` 及监听端口。

---

### 🚀 一行命令安装与自动配置（推荐）

在运行着 Headscale 容器的宿主机上，直接执行以下一行命令即可自动探测 Headscale 数据目录、完成构建并启动服务：

```bash
curl -fsSL https://raw.githubusercontent.com/whyicn/headscale-ip-webui/main/install.sh | bash
```

自定义端口（例如改为 `5080` 端口）一键安装：

```bash
curl -fsSL https://raw.githubusercontent.com/whyicn/headscale-ip-webui/main/install.sh | PORT=5080 bash
```

安装完成后，打开浏览器访问 `http://<服务器IP>:5000`，输入你的 Headscale API Key 即可管理节点 IP（点击右上角 `中文` 即可切换为中文界面）。

> 💡 **如何获取 Headscale API Key？**
> ```bash
> docker exec headscale headscale apikeys create --expiration 90d
> ```

---

## 📄 License / 许可协议

This project is licensed under the **[Free-to-Use, Non-Redistribution Source-Available License](./LICENSE)** (本项目采用**免费自用、禁止二次分发源码可用许可协议**):
- ✅ **Permitted / 允许**: Free to download, install, run, and modify for personal or internal organizational use from this official repository.
- ❌ **Prohibited / 禁止**: Redistribution, mirroring, republishing, or commercial resale/paid hosting without prior written permission from the author.
