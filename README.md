# Headscale IP WebUI (`headscale-ip-webui`)

轻量级、安全的 **Headscale 节点 IPv4 可视化管理与原子互换工具**（同时提供 Web 界面与 CLI 命令行脚本）。

> 解决了 Headscale 官方及现有第三方 UI（如 `headscale-ui`、`headplane`）不支持直接修改或互换已注册节点 IPv4 地址的痛点。

---

## ✨ 核心特性

- **可视化行内编辑**：仿 Tailscale / Headplane 极简风格界面，点击节点 IP 即可直接修改。
- **实时合法性与冲突检测**：输入时实时校验 CGNAT 网段（`100.64.0.0/10` 或自定义网段），自动检测目标 IP 是否已被其他节点占用。
- **一键原子互换 IP（Swap IP）**：当目标 IP 已被其他节点占用时，支持一键交换两个节点的 IPv4 地址，无需借用第三个中转 IP。
- **安全的事务与容器生命周期控制**：
  1. 修改前通过 Docker Socket 平滑停止 Headscale 容器（确保内存状态与 SQLite WAL 完整落盘释放锁）；
  2. 自动创建 `db.sqlite.bak` 备份；
  3. 执行 SQLite `IMMEDIATE` 事务更新并执行 `wal_checkpoint(TRUNCATE)`；
  4. 自动重启 Headscale 容器并轮询等待服务恢复就绪。
- **原生 API Key 鉴权**：直接复用 Headscale 的 Admin API Key，无需额外配置账号密码体系。
- **自动环境探测**：自动识别宿主机上正在运行的 Headscale 容器、数据库路径、`config.yaml` 及监听端口。

---

## 🚀 一行命令安装与自动配置（推荐）

在运行着 Headscale 容器的宿主机上，直接执行以下一行命令即可自动探测 Headscale 数据目录、完成构建并启动服务：

```bash
curl -fsSL https://raw.githubusercontent.com/whyicn/headscale-ip-webui/main/install.sh | bash
```

自定义端口（例如改为 `5080` 端口）一键安装：

```bash
curl -fsSL https://raw.githubusercontent.com/whyicn/headscale-ip-webui/main/install.sh | PORT=5080 bash
```

安装完成后，打开浏览器访问 `http://<服务器IP>:5000`，输入你的 Headscale API Key 即可管理节点 IP。

> 💡 **如何获取 Headscale API Key？**
> 在服务器终端执行以下命令即可生成一个有效期 90 天的 Key：
> ```bash
> docker exec headscale headscale apikeys create --expiration 90d
> ```

---

## 🐳 其他部署方式

### 方式一：使用 Docker Compose

```bash
git clone https://github.com/whyicn/headscale-ip-webui.git
cd headscale-ip-webui

# 按需修改数据目录环境变量后启动
HEADSCALE_DATA_DIR=/data/headscale/lib \
HEADSCALE_CONFIG_DIR=/data/headscale/config \
docker compose up -d --build
```

### 方式二：命令行脚本独立使用（CLI）

除了 Web 界面外，仓库内还提供了独立的 [`change-node-ip.sh`](./change-node-ip.sh) 脚本，支持在宿主机或容器内直接通过命令行修改或互换节点 IP：

```bash
# 将节点 ID=3 的 IP 修改为 100.64.0.10
./change-node-ip.sh 3 100.64.0.10

# 若 100.64.0.10 已被其他节点占用，添加 --swap 参数直接与对方互换 IP
./change-node-ip.sh 3 100.64.0.10 --swap
```

---

## ⚙️ 环境变量说明

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `PORT` | `5000` | WebUI 监听端口 |
| `HEADSCALE_CONTAINER` | `headscale` | Headscale 容器名称（用于修改前后平滑启停容器） |
| `HEADSCALE_DB_PATH` | 自动探测 | 容器内或宿主机 `db.sqlite` 文件路径 |
| `HEADSCALE_CONFIG_PATH` | 自动探测 | `config.yaml` 路径（用于自动读取 `listen_addr` 端口及 `prefixes.v4` 网段） |
| `HEADSCALE_URL` | 自动探测 | Headscale API 地址（如 `http://127.0.0.1:8080` 或 `http://headscale:8085`） |

---

## 📄 许可协议 (License)

本项目采用 **[免费自用、禁止二次分发源码可用许可协议 (Free-to-Use, Non-Redistribution Source-Available License)](./LICENSE)**：
- ✅ **允许**：个人或组织从本官方仓库免费下载、部署、自用及内部修改。
- ❌ **禁止**：未经作者书面授权，禁止将本项目源码、修改版或容器镜像二次分发至第三方平台，禁止用于任何商业售卖或付费代部署服务。
