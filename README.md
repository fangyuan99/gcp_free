# GCP Free 工具集

这是一个用于管理 GCP 免费实例的脚本集合，提供创建实例、刷 AMD CPU、配置防火墙、换源、安装 dae，以及远程安装流量监控脚本等功能。

创建免费实例需要绑定结算账号，也就是说目前应该处于试用赠金或者付费账号状态。

## 功能概览

- 创建/选择 GCP 免费实例
- 创建自定义实例（机器类型、磁盘、网络、静态外部 IP）
- 使用 CLI 参数或 JSON profiles 创建实例
- 从 HTTP(S) 外链导入 profiles，支持 WebDAV 拉取/保存 profiles
- 刷 AMD CPU
- 配置防火墙规则
- 换源、安装 dae、上传 `config.dae`
- 远程安装流量监控脚本（iptables 监控 / 超额自动关机）
## 快速开始（推荐）

打开 https://console.cloud.google.com/
在右上角点击 Cloud Shell 
在 Cloud Shell 服务器运行
```bash
# 初次运行
git clone https://github.com/fatekey/gcp_free && cd gcp_free && bash start.sh
# 再次运行
cd ~/gcp_free && bash start.sh
```

## 环境要求

- 已安装 Google Cloud SDK（`gcloud`）
- 已登录并具备对应项目权限（建议先 `gcloud auth login`）
- Python 3

## 本地运行

### 环境要求

- 已安装 Google Cloud SDK（`gcloud`）
- 已登录并具备对应项目权限（建议先 `gcloud auth application-default login`）
- Python 3
### 运行脚本

使用 `start.sh` 自动初始化环境：

```bash
bash start.sh
```

首次运行会：

1. 启用所需 GCP API
2. 创建并进入 venv
3. 安装依赖
4. 执行 `gcp.py`

再次运行只会进入 venv 并执行 `gcp.py`。

## 手动运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install google-cloud-compute google-cloud-resource-manager
python gcp.py
```

## 自定义实例与 CLI

默认运行 `python gcp.py` 仍会进入交互菜单。也可以使用 CLI：

```bash
# 创建免费实例
python gcp.py create-free --project your-project-id --zone us-west1-b

# 自定义实例 dry-run（只校验，不创建）
python gcp.py create-custom \
  --project your-project-id \
  --name custom-vm \
  --zone us-west1-b \
  --machine-type e2-small \
  --disk-size-gb 30 \
  --disk-type pd-standard \
  --network global/networks/default \
  --external-ip-mode ephemeral \
  --pricing-api-key "$CLOUD_BILLING_API_KEY" \
  --dry-run

# 创建自定义机器类型实例
python gcp.py create-custom \
  --project your-project-id \
  --name custom-vm \
  --zone us-west1-b \
  --custom-series e2 \
  --custom-cpu 2 \
  --custom-memory-mb 2048 \
  --confirm-paid "CREATE PAID VM"
```

自定义实例可能产生费用。非免费规格创建前会要求输入固定确认文本：

```text
CREATE PAID VM
```

价格估算使用 Cloud Billing Catalog API 的公开价，默认读取 `CLOUD_BILLING_API_KEY`，也可以用 `--pricing-api-key` 传入。估算按 730 小时/月计算 VM vCPU/RAM 和启动盘容量，不包含网络流量、快照、镜像、空闲静态 IP、税费、折扣、承诺使用折扣或账号合同价。如果没有 API key 或 SKU 匹配失败，脚本会保留收费强确认流程。

```bash
export CLOUD_BILLING_API_KEY="your-api-key"
python gcp.py create-custom --name custom-vm --zone us-west1-b --machine-type e2-small --dry-run

# 不需要估算时可跳过
python gcp.py create-custom --name custom-vm --zone us-west1-b --machine-type e2-small --no-price-estimate
```

## Profiles

本地配置默认保存到 `profiles.json`（已加入 `.gitignore`）。格式示例：

```json
{
  "profiles": {
    "small-vm": {
      "instance_name": "small-vm",
      "zone": "us-west1-b",
      "os_image": {
        "project": "debian-cloud",
        "family": "debian-12"
      },
      "machine_type": "e2-small",
      "disk": {
        "size_gb": 30,
        "type": "pd-standard"
      },
      "network": {
        "network": "global/networks/default",
        "subnetwork": "",
        "external_ip_mode": "ephemeral",
        "static_nat_ip": "",
        "network_tier": "STANDARD",
        "tags": ["http-server", "https-server"]
      }
    }
  },
  "webdav_url": ""
}
```

常用命令：

```bash
python gcp.py profiles list
python gcp.py profiles save --profile small-vm --name small-vm --zone us-west1-b --machine-type e2-small
python gcp.py create-from-profile --project your-project-id --profile small-vm --confirm-paid "CREATE PAID VM"
python gcp.py profiles import-url --url https://example.com/profiles.json
```

WebDAV 同步使用环境变量读取凭据，不会把密码写入配置文件：

```bash
export WEBDAV_URL="https://example.com/remote/profiles.json"
export WEBDAV_USERNAME="username"
export WEBDAV_PASSWORD="password"
python gcp.py profiles pull-webdav
python gcp.py profiles push-webdav
```

## 脚本说明

- `gcp.py`: 主控制脚本
- `config.dae`: dae 配置模板
- `scripts/apt.sh`: 换源脚本
- `scripts/dae.sh`: 安装 dae
- `scripts/net_iptables.sh`: 流量监控（iptables）
- `scripts/net_shutdown.sh`: 超额自动关机

## 常见问题

- 如果 `start.sh` 报错提示未找到 venv，可删除 `.gcp_free_initialized` 后重新初始化。
