import argparse
import base64
from dataclasses import dataclass, field
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from typing import List, Optional
from urllib import request as urlrequest
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

try:
    from google.cloud import compute_v1
    from google.cloud import resourcemanager_v3
except ImportError:
    compute_v1 = None
    resourcemanager_v3 = None

GITHUB_REPO = "fatekey/gcp_free"
GITHUB_BRANCH = "master"
GITHUB_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"
GITHUB_RAW_SCRIPTS_BASE = f"{GITHUB_RAW_BASE}/scripts"
REMOTE_SCRIPT_URLS = {
    "apt": f"{GITHUB_RAW_SCRIPTS_BASE}/apt.sh",
    "dae": f"{GITHUB_RAW_SCRIPTS_BASE}/dae.sh",
    "net_iptables": f"{GITHUB_RAW_SCRIPTS_BASE}/net_iptables.sh",
    "net_shutdown": f"{GITHUB_RAW_SCRIPTS_BASE}/net_shutdown.sh",
}
FIREWALL_RULES_TO_CLEAN = [
    "allow-all-ingress-custom",
    "deny-cdn-egress-custom",
]

REGION_OPTIONS = [
    {"name": "俄勒冈 (Oregon) [推荐]", "region": "us-west1", "default_zone": "us-west1-b"},
    {"name": "爱荷华 (Iowa)", "region": "us-central1", "default_zone": "us-central1-f"},
    {"name": "南卡罗来纳 (South Carolina)", "region": "us-east1", "default_zone": "us-east1-b"},
]

OS_IMAGE_OPTIONS = [
    {"name": "Debian 12 (Bookworm)", "project": "debian-cloud", "family": "debian-12"},
    {"name": "Ubuntu 22.04 LTS", "project": "ubuntu-os-cloud", "family": "ubuntu-2204-lts"},
]

DEFAULT_PROFILES_FILE = "profiles.json"
PAID_CONFIRM_TEXT = "CREATE PAID VM"
FREE_INSTANCE_NAME = "free-tier-vm"
FREE_MACHINE_TYPE = "e2-micro"
FREE_DISK_SIZE_GB = 30
FREE_DISK_TYPE = "pd-standard"
FREE_REGIONS = {"us-west1", "us-central1", "us-east1"}
CUSTOM_MACHINE_SERIES = {"e2", "n1", "n2", "n2d", "n4"}
DISK_TYPE_OPTIONS = ["pd-standard", "pd-balanced", "pd-ssd", "pd-extreme"]
NETWORK_TIER_OPTIONS = ["STANDARD", "PREMIUM"]
EXTERNAL_IP_MODES = ["ephemeral", "static", "none"]
COMPUTE_ENGINE_SERVICE_ID = "6F81-5844-456A"
HOURS_PER_MONTH = 730
COMMON_MACHINE_SPECS = {
    "e2-micro": (2, 1024),
    "e2-small": (2, 2048),
    "e2-medium": (2, 4096),
    "f1-micro": (1, 614),
    "g1-small": (1, 1740),
}
MACHINE_FAMILY_RATIOS = {
    "standard": 4096,
    "highmem": 8192,
    "highcpu": 1024,
    "megmem": 14336,
}


@dataclass
class OsImageConfig:
    project: str = "debian-cloud"
    family: str = "debian-12"
    name: str = "Debian 12 (Bookworm)"


@dataclass
class DiskConfig:
    size_gb: int = FREE_DISK_SIZE_GB
    type: str = FREE_DISK_TYPE


@dataclass
class NetworkConfig:
    network: str = "global/networks/default"
    subnetwork: str = ""
    external_ip_mode: str = "ephemeral"
    static_nat_ip: str = ""
    network_tier: str = "STANDARD"
    tags: List[str] = field(default_factory=lambda: ["http-server", "https-server"])


@dataclass
class CustomMachineConfig:
    series: str
    cpu: int
    memory_mb: int
    extended_memory: bool = False


@dataclass
class InstanceConfig:
    instance_name: str = FREE_INSTANCE_NAME
    zone: str = "us-west1-b"
    os_image: OsImageConfig = field(default_factory=OsImageConfig)
    machine_type: str = FREE_MACHINE_TYPE
    custom_machine: Optional[CustomMachineConfig] = None
    disk: DiskConfig = field(default_factory=DiskConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)

    @property
    def region(self):
        return zone_to_region(self.zone)


def print_info(msg):
    print(f"[信息] {msg}")
    sys.stdout.flush()


def print_success(msg):
    print(f"\033[92m[成功] {msg}\033[0m")
    sys.stdout.flush()


def print_warning(msg):
    print(f"\033[93m[警告] {msg}\033[0m")
    sys.stdout.flush()


def require_gcp_libraries():
    if compute_v1 is None or resourcemanager_v3 is None:
        print("【错误】缺少必要的 Python 库。")
        print("请先在终端运行以下命令安装：")
        print("pip install google-cloud-compute google-cloud-resource-manager")
        sys.exit(1)


def zone_to_region(zone):
    parts = zone.split("-")
    if len(parts) < 3:
        return zone
    return "-".join(parts[:-1])


def normalize_partial_path(value, resource_prefix):
    value = (value or "").strip()
    if not value:
        return value
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if "/" in value:
        return value
    return f"{resource_prefix}/{value}"


def validate_instance_name(name):
    if not re.fullmatch(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?", name or ""):
        raise ValueError("实例名必须以小写字母开头，只能包含小写字母、数字和连字符，长度 1-63。")


def validate_custom_machine(series, cpu, memory_mb):
    series = (series or "").lower()
    if series not in CUSTOM_MACHINE_SERIES:
        raise ValueError(f"暂不支持的自定义机器系列: {series}")
    if cpu != 1 and (cpu < 2 or cpu % 2 != 0):
        raise ValueError("自定义 CPU 数量必须为 1 或大于等于 2 的偶数。")
    if memory_mb <= 0 or memory_mb % 256 != 0:
        raise ValueError("自定义内存必须为正数，且是 256MB 的倍数。")


def build_custom_machine_type(series, cpu, memory_mb, extended_memory=False):
    validate_custom_machine(series, cpu, memory_mb)
    machine_type = f"{series.lower()}-custom-{cpu}-{memory_mb}"
    if extended_memory:
        machine_type += "-ext"
    return machine_type


def machine_type_to_path(zone, machine_type):
    return normalize_partial_path(machine_type, f"zones/{zone}/machineTypes")


def disk_type_to_path(zone, disk_type):
    return normalize_partial_path(disk_type, f"zones/{zone}/diskTypes")


def os_config_to_image_config(os_config):
    return OsImageConfig(
        project=os_config["project"],
        family=os_config["family"],
        name=os_config.get("name", os_config["family"]),
    )


def default_free_instance_config(zone, os_config):
    return InstanceConfig(
        instance_name=FREE_INSTANCE_NAME,
        zone=zone,
        os_image=os_config_to_image_config(os_config),
        machine_type=FREE_MACHINE_TYPE,
        custom_machine=None,
        disk=DiskConfig(size_gb=FREE_DISK_SIZE_GB, type=FREE_DISK_TYPE),
        network=NetworkConfig(),
    )


def instance_config_machine_type(config):
    if config.custom_machine:
        return build_custom_machine_type(
            config.custom_machine.series,
            config.custom_machine.cpu,
            config.custom_machine.memory_mb,
            config.custom_machine.extended_memory,
        )
    return config.machine_type


def is_free_tier_config(config):
    return (
        config.region in FREE_REGIONS
        and not config.custom_machine
        and instance_config_machine_type(config) == FREE_MACHINE_TYPE
        and config.disk.size_gb <= FREE_DISK_SIZE_GB
        and config.disk.type == FREE_DISK_TYPE
        and config.network.external_ip_mode in ("ephemeral", "none")
        and config.network.network_tier == "STANDARD"
    )


def validate_instance_config(config):
    validate_instance_name(config.instance_name)
    if config.disk.size_gb < 10:
        raise ValueError("启动盘大小必须至少为 10GB。")
    if config.disk.type not in DISK_TYPE_OPTIONS:
        raise ValueError(f"不支持的磁盘类型: {config.disk.type}")
    if config.network.external_ip_mode not in EXTERNAL_IP_MODES:
        raise ValueError(f"不支持的外网 IP 模式: {config.network.external_ip_mode}")
    if config.network.network_tier not in NETWORK_TIER_OPTIONS:
        raise ValueError(f"不支持的网络层级: {config.network.network_tier}")
    if config.network.external_ip_mode == "static" and not config.network.static_nat_ip:
        raise ValueError("静态外部 IP 模式必须指定 static_nat_ip。")
    if config.network.static_nat_ip and config.network.external_ip_mode != "static":
        raise ValueError("仅 static 外部 IP 模式可以指定 static_nat_ip。")
    if config.custom_machine:
        validate_custom_machine(
            config.custom_machine.series,
            config.custom_machine.cpu,
            config.custom_machine.memory_mb,
        )
    elif not config.machine_type:
        raise ValueError("必须指定 machine_type 或 custom_machine。")


def config_to_profile(config):
    profile = {
        "instance_name": config.instance_name,
        "zone": config.zone,
        "os_image": {
            "project": config.os_image.project,
            "family": config.os_image.family,
        },
        "disk": {
            "size_gb": config.disk.size_gb,
            "type": config.disk.type,
        },
        "network": {
            "network": config.network.network,
            "subnetwork": config.network.subnetwork,
            "external_ip_mode": config.network.external_ip_mode,
            "static_nat_ip": config.network.static_nat_ip,
            "network_tier": config.network.network_tier,
            "tags": config.network.tags,
        },
    }
    if config.custom_machine:
        profile["custom_machine"] = {
            "series": config.custom_machine.series,
            "cpu": config.custom_machine.cpu,
            "memory_mb": config.custom_machine.memory_mb,
            "extended_memory": config.custom_machine.extended_memory,
        }
    else:
        profile["machine_type"] = config.machine_type
    return profile


def profile_to_config(profile):
    os_image = profile.get("os_image") or {}
    disk = profile.get("disk") or {}
    network = profile.get("network") or {}
    custom_machine = profile.get("custom_machine")
    config = InstanceConfig(
        instance_name=profile.get("instance_name", FREE_INSTANCE_NAME),
        zone=profile.get("zone", "us-west1-b"),
        os_image=OsImageConfig(
            project=os_image.get("project", "debian-cloud"),
            family=os_image.get("family", "debian-12"),
            name=os_image.get("name", os_image.get("family", "debian-12")),
        ),
        machine_type=profile.get("machine_type", ""),
        custom_machine=CustomMachineConfig(
            series=custom_machine.get("series", ""),
            cpu=int(custom_machine.get("cpu", 0)),
            memory_mb=int(custom_machine.get("memory_mb", 0)),
            extended_memory=bool(custom_machine.get("extended_memory", False)),
        )
        if custom_machine
        else None,
        disk=DiskConfig(
            size_gb=int(disk.get("size_gb", FREE_DISK_SIZE_GB)),
            type=disk.get("type", FREE_DISK_TYPE),
        ),
        network=NetworkConfig(
            network=network.get("network", "global/networks/default"),
            subnetwork=network.get("subnetwork", ""),
            external_ip_mode=network.get("external_ip_mode", "ephemeral"),
            static_nat_ip=network.get("static_nat_ip", ""),
            network_tier=network.get("network_tier", "STANDARD").upper(),
            tags=list(network.get("tags", ["http-server", "https-server"])),
        ),
    )
    validate_instance_config(config)
    return config


def empty_profiles_document():
    return {"profiles": {}, "webdav_url": ""}


def load_profiles(path=DEFAULT_PROFILES_FILE):
    if not os.path.exists(path):
        return empty_profiles_document()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("profiles.json 必须是 JSON 对象。")
    data.setdefault("profiles", {})
    data.setdefault("webdav_url", "")
    return data


def save_profiles(data, path=DEFAULT_PROFILES_FILE):
    data.setdefault("profiles", {})
    data.setdefault("webdav_url", "")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_profile_config(profile_name, profiles_path=DEFAULT_PROFILES_FILE):
    data = load_profiles(profiles_path)
    profile = data.get("profiles", {}).get(profile_name)
    if not profile:
        raise ValueError(f"未找到 profile: {profile_name}")
    return profile_to_config(profile)


def merge_imported_profiles(imported_data, profiles_path=DEFAULT_PROFILES_FILE):
    if not isinstance(imported_data, dict) or not isinstance(imported_data.get("profiles"), dict):
        raise ValueError("导入内容必须包含 profiles 对象。")
    data = load_profiles(profiles_path)
    data.setdefault("profiles", {}).update(imported_data["profiles"])
    if imported_data.get("webdav_url"):
        data["webdav_url"] = imported_data["webdav_url"]
    save_profiles(data, profiles_path)
    return len(imported_data["profiles"])


def fetch_json_url(url, username=None, password=None):
    headers = {"Accept": "application/json"}
    if username is not None and password is not None:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    req = urlrequest.Request(url, headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(f"读取 JSON 失败: {e}") from e


def fetch_json_with_query(url, query):
    separator = "&" if "?" in url else "?"
    req = urlrequest.Request(f"{url}{separator}{urlencode(query)}", headers={"Accept": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        raise RuntimeError(f"读取价格 API 失败: {e}") from e


def put_json_url(url, data, username=None, password=None):
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if username is not None and password is not None:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    req = urlrequest.Request(url, data=body, headers=headers, method="PUT")
    try:
        with urlrequest.urlopen(req, timeout=30) as response:
            response.read()
    except (HTTPError, URLError, TimeoutError) as e:
        raise RuntimeError(f"写入 JSON 失败: {e}") from e


def resolve_webdav_url(args=None, profiles_path=DEFAULT_PROFILES_FILE):
    cli_url = getattr(args, "url", None) if args else None
    if cli_url:
        return cli_url
    env_url = os.environ.get("WEBDAV_URL")
    if env_url:
        return env_url
    return load_profiles(profiles_path).get("webdav_url", "")


def get_webdav_credentials():
    username = os.environ.get("WEBDAV_USERNAME") or input("WebDAV 用户名: ").strip()
    password = os.environ.get("WEBDAV_PASSWORD") or getpass.getpass("WebDAV 密码: ")
    return username, password


def print_instance_config_summary(config):
    print("\n--- 实例配置摘要 ---")
    print(f"实例名: {config.instance_name}")
    print(f"可用区: {config.zone}")
    print(f"系统镜像: {config.os_image.project}/{config.os_image.family}")
    print(f"机器类型: {instance_config_machine_type(config)}")
    print(f"启动盘: {config.disk.size_gb}GB {config.disk.type}")
    print(f"网络: {config.network.network}")
    print(f"子网: {config.network.subnetwork or '(自动/默认)'}")
    print(f"外网 IP: {config.network.external_ip_mode}")
    if config.network.static_nat_ip:
        print(f"静态外部 IP: {config.network.static_nat_ip}")
    print(f"网络层级: {config.network.network_tier}")
    print(f"网络标签: {', '.join(config.network.tags) if config.network.tags else '(无)'}")
    print(f"免费规格判断: {'是' if is_free_tier_config(config) else '否'}")


def money_to_float(money):
    units = int(money.get("units", 0) or 0)
    nanos = int(money.get("nanos", 0) or 0)
    return units + nanos / 1_000_000_000


def first_unit_price(sku):
    pricing = (sku.get("pricingInfo") or [{}])[-1]
    expression = pricing.get("pricingExpression", {})
    tiered_rates = expression.get("tieredRates") or []
    if not tiered_rates:
        return None
    return money_to_float(tiered_rates[0].get("unitPrice", {}))


def fetch_compute_skus(api_key, currency="USD"):
    if not api_key:
        raise ValueError("缺少 Cloud Billing Catalog API key。")

    skus = []
    page_token = ""
    base_url = f"https://cloudbilling.googleapis.com/v1/services/{COMPUTE_ENGINE_SERVICE_ID}/skus"
    while True:
        query = {"key": api_key, "currencyCode": currency, "pageSize": 5000}
        if page_token:
            query["pageToken"] = page_token
        data = fetch_json_with_query(base_url, query)
        skus.extend(data.get("skus", []))
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break
    return skus


def region_matches_sku(sku, region):
    regions = sku.get("serviceRegions") or []
    geo_regions = (sku.get("geoTaxonomy") or {}).get("regions") or []
    return region in regions or region in geo_regions or not regions


def machine_series(machine_type):
    return (machine_type or "").split("-")[0].lower()


def machine_spec_from_type(machine_type):
    machine_type = machine_type.split("/")[-1]
    if machine_type in COMMON_MACHINE_SPECS:
        return COMMON_MACHINE_SPECS[machine_type]

    custom_match = re.fullmatch(r"([a-z0-9]+)-custom-(\d+)-(\d+)(?:-ext)?", machine_type)
    if custom_match:
        return int(custom_match.group(2)), int(custom_match.group(3))

    match = re.fullmatch(r"([a-z0-9]+)-([a-z]+)-(\d+)", machine_type)
    if match and match.group(2) in MACHINE_FAMILY_RATIOS:
        vcpu = int(match.group(3))
        return vcpu, vcpu * MACHINE_FAMILY_RATIOS[match.group(2)]

    return None


def disk_price_keywords(disk_type):
    return {
        "pd-standard": ["standard", "pd capacity"],
        "pd-balanced": ["balanced", "pd capacity"],
        "pd-ssd": ["ssd", "pd capacity"],
        "pd-extreme": ["extreme", "pd capacity"],
    }.get(disk_type, [disk_type.replace("pd-", ""), "pd capacity"])


def find_price_sku(skus, region, keywords, reject_keywords=None):
    reject_keywords = reject_keywords or []
    matches = []
    for sku in skus:
        if not region_matches_sku(sku, region):
            continue
        description = (sku.get("description") or "").lower()
        category = sku.get("category") or {}
        category_text = " ".join(str(v).lower() for v in category.values())
        haystack = f"{description} {category_text}"
        if all(keyword.lower() in haystack for keyword in keywords) and not any(
            keyword.lower() in haystack for keyword in reject_keywords
        ):
            price = first_unit_price(sku)
            if price is not None:
                matches.append((sku, price))
    return matches[0] if matches else (None, None)


def estimate_instance_price(config, project_id=None, api_key=None, currency="USD"):
    api_key = api_key or os.environ.get("CLOUD_BILLING_API_KEY", "")
    if not api_key:
        return {
            "available": False,
            "reason": "未设置 CLOUD_BILLING_API_KEY 或 --pricing-api-key，无法调用 Cloud Billing Catalog API。",
        }

    machine_type = instance_config_machine_type(config)
    spec = machine_spec_from_type(machine_type)
    if spec is None and project_id and compute_v1 is not None:
        try:
            machine_client = compute_v1.MachineTypesClient()
            machine = machine_client.get(
                project=project_id,
                zone=config.zone,
                machine_type=machine_type.split("/")[-1],
            )
            spec = (int(machine.guest_cpus), int(machine.memory_mb))
        except Exception:
            spec = None
    if spec is None:
        return {
            "available": False,
            "reason": f"无法推断机器类型 {machine_type} 的 vCPU/RAM，无法估算价格。",
        }

    skus = fetch_compute_skus(api_key, currency)
    region = config.region
    series = machine_series(machine_type)
    vcpu, memory_mb = spec
    memory_gb = memory_mb / 1024
    line_items = []
    missing = []

    cpu_sku, cpu_price = find_price_sku(
        skus,
        region,
        [series, "core"],
        ["commitment", "preemptible", "spot", "sole tenancy", "license"],
    )
    if cpu_sku:
        monthly = cpu_price * vcpu * HOURS_PER_MONTH
        line_items.append(
            {
                "name": "vCPU",
                "description": cpu_sku.get("description", ""),
                "quantity": vcpu * HOURS_PER_MONTH,
                "unit": "vCPU-hour",
                "unit_price": cpu_price,
                "monthly": monthly,
            }
        )
    else:
        missing.append("vCPU")

    ram_sku, ram_price = find_price_sku(
        skus,
        region,
        [series, "ram"],
        ["commitment", "preemptible", "spot", "sole tenancy", "license"],
    )
    if ram_sku:
        monthly = ram_price * memory_gb * HOURS_PER_MONTH
        line_items.append(
            {
                "name": "RAM",
                "description": ram_sku.get("description", ""),
                "quantity": memory_gb * HOURS_PER_MONTH,
                "unit": "GB-hour",
                "unit_price": ram_price,
                "monthly": monthly,
            }
        )
    else:
        missing.append("RAM")

    disk_sku, disk_price = find_price_sku(
        skus,
        region,
        disk_price_keywords(config.disk.type),
        ["snapshot", "image", "regional", "replica", "commitment"],
    )
    if disk_sku:
        monthly = disk_price * config.disk.size_gb
        line_items.append(
            {
                "name": "Boot disk",
                "description": disk_sku.get("description", ""),
                "quantity": config.disk.size_gb,
                "unit": "GB-month",
                "unit_price": disk_price,
                "monthly": monthly,
            }
        )
    else:
        missing.append("Boot disk")

    return {
        "available": bool(line_items),
        "currency": currency,
        "region": region,
        "machine_spec": {"vcpu": vcpu, "memory_gb": memory_gb},
        "line_items": line_items,
        "missing": missing,
        "total_monthly": sum(item["monthly"] for item in line_items),
        "notes": [
            "估算按 730 小时/月计算。",
            "未包含网络流量、快照、镜像、空闲静态 IP、税费、折扣、承诺使用折扣或账号合同价。",
        ],
    }


def print_price_estimate(estimate):
    print("\n--- 费用估算 ---")
    if not estimate.get("available"):
        print_warning(estimate.get("reason", "无法估算价格。"))
        return
    currency = estimate["currency"]
    spec = estimate["machine_spec"]
    print(f"区域: {estimate['region']}")
    print(f"规格: {spec['vcpu']} vCPU / {spec['memory_gb']:.2f} GB RAM")
    for item in estimate["line_items"]:
        print(
            f"- {item['name']}: {item['monthly']:.2f} {currency}/月 "
            f"({item['quantity']:.2f} {item['unit']} x {item['unit_price']:.6f})"
        )
        print(f"  SKU: {item['description']}")
    if estimate.get("missing"):
        print_warning(f"未能估算: {', '.join(estimate['missing'])}")
    print(f"合计估算: {estimate['total_monthly']:.2f} {currency}/月")
    for note in estimate.get("notes", []):
        print(f"注: {note}")


def confirm_paid_creation_if_needed(config, confirm_text=None, force_prompt=True):
    if is_free_tier_config(config):
        return True
    print_warning("该配置不属于脚本内置的 GCP 免费实例规格，可能产生费用。")
    if confirm_text == PAID_CONFIRM_TEXT:
        return True
    if not force_prompt:
        return False
    typed = input(f"如确认创建收费实例，请输入 {PAID_CONFIRM_TEXT}: ").strip()
    return typed == PAID_CONFIRM_TEXT


def build_network_interface(config):
    network_interface = compute_v1.NetworkInterface()
    if config.network.subnetwork:
        network_interface.subnetwork = config.network.subnetwork
    else:
        network_interface.network = config.network.network

    if config.network.external_ip_mode != "none":
        access_config = compute_v1.AccessConfig()
        access_config.name = "External NAT"
        access_config.type_ = compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name
        access_config.network_tier = config.network.network_tier
        if config.network.static_nat_ip:
            access_config.nat_i_p = config.network.static_nat_ip
        network_interface.access_configs = [access_config]
    return network_interface


def build_instance_resource(config, source_disk_image):
    disk = compute_v1.AttachedDisk()
    disk.boot = True
    disk.auto_delete = True
    initialize_params = compute_v1.AttachedDiskInitializeParams()
    initialize_params.source_image = source_disk_image
    initialize_params.disk_size_gb = config.disk.size_gb
    initialize_params.disk_type = disk_type_to_path(config.zone, config.disk.type)
    disk.initialize_params = initialize_params

    instance = compute_v1.Instance()
    instance.name = config.instance_name
    instance.machine_type = machine_type_to_path(config.zone, instance_config_machine_type(config))
    instance.disks = [disk]
    instance.network_interfaces = [build_network_interface(config)]

    if config.network.tags:
        tags = compute_v1.Tags()
        tags.items = config.network.tags
        instance.tags = tags
    return instance


def select_from_list(items, prompt_text, label_fn):
    print(f"\n--- {prompt_text} ---")
    for i, item in enumerate(items):
        print(f"[{i+1}] {label_fn(item)}")
    while True:
        choice = input(f"请输入数字选择 (1-{len(items)}): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(items):
                return items[idx]
        print("输入无效，请重试。")


def prompt_manual_project_id():
    while True:
        project_id = input("请输入项目 ID: ").strip()
        if project_id:
            return project_id
        print("输入不能为空，请重试。")


def select_gcp_project():
    require_gcp_libraries()
    print_info("正在扫描您的项目列表...")
    try:
        client = resourcemanager_v3.ProjectsClient()
        request = resourcemanager_v3.SearchProjectsRequest(query="")
        page_result = client.search_projects(request=request)

        active_projects = []
        for project in page_result:
            if project.state == resourcemanager_v3.Project.State.ACTIVE:
                active_projects.append(project)

        if not active_projects:
            print_warning("未找到活跃的项目。请手动输入项目 ID。")
            return prompt_manual_project_id()

        print("\n--- 请选择目标项目 ---")
        for i, p in enumerate(active_projects):
            print(f"[{i+1}] {p.project_id} ({p.display_name})")

        while True:
            choice = input(f"请输入数字选择 (1-{len(active_projects)}): ").strip()
            if choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(active_projects):
                    selected = active_projects[idx]
                    print_info(f"已选择项目: {selected.project_id} ({selected.display_name})")
                    return selected.project_id
            print("输入无效，请重试。")
    except Exception as e:
        print_warning(f"无法列出项目: {e}。请手动输入项目 ID。")
        return prompt_manual_project_id()


def list_zones_for_region(project_id, region):
    require_gcp_libraries()
    zones_client = compute_v1.ZonesClient()
    zones = []
    for zone in zones_client.list(project=project_id):
        if zone.status != "UP":
            continue
        zone_region = zone.region.split("/")[-1] if zone.region else ""
        if zone_region == region:
            zones.append(zone.name)
    return sorted(zones)


def select_zone(project_id):
    region_config = select_from_list(REGION_OPTIONS, "请选择部署区域", lambda r: r["name"])
    region = region_config["region"]
    default_zone = region_config["default_zone"]

    print_info(f"正在获取 {region} 的可用区列表...")
    try:
        zones = list_zones_for_region(project_id, region)
    except Exception as e:
        print_warning(f"获取可用区失败: {e}。将使用默认可用区 {default_zone}。")
        return default_zone

    if not zones:
        print_warning(f"未获取到可用区列表，使用默认可用区 {default_zone}。")
        return default_zone

    return select_from_list(zones, f"请选择可用区 ({region})", lambda z: z)


def select_os_image():
    return select_from_list(OS_IMAGE_OPTIONS, "请选择操作系统", lambda o: o["name"])


def prompt_text(label, default=""):
    suffix = f" (默认 {default})" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def prompt_int(label, default):
    while True:
        value = prompt_text(label, str(default))
        try:
            return int(value)
        except ValueError:
            print("请输入有效整数。")


def prompt_yes_no(label, default=True):
    hint = "Y/n" if default else "y/N"
    value = input(f"{label} ({hint}): ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def list_machine_types_for_zone(project_id, zone):
    require_gcp_libraries()
    client = compute_v1.MachineTypesClient()
    return sorted(mt.name for mt in client.list(project=project_id, zone=zone))


def select_machine_type(project_id, zone):
    print("\n--- 请选择机器类型 ---")
    print("[1] 输入预定义机器类型（如 e2-small、n2-standard-2）")
    print("[2] 从当前可用区 API 列表选择")
    print("[3] 创建自定义机器类型")
    while True:
        choice = input("请输入数字选择: ").strip()
        if choice == "1":
            return prompt_text("请输入 machine type", FREE_MACHINE_TYPE), None
        if choice == "2":
            try:
                machine_types = list_machine_types_for_zone(project_id, zone)
                selected = select_from_list(machine_types, f"请选择机器类型 ({zone})", lambda m: m)
                return selected, None
            except Exception as e:
                print_warning(f"读取机器类型失败: {e}")
        if choice == "3":
            series = select_from_list(sorted(CUSTOM_MACHINE_SERIES), "请选择自定义机器系列", lambda s: s)
            cpu = prompt_int("请输入 vCPU 数量（1 或偶数）", 2)
            memory_mb = prompt_int("请输入内存 MB（256 的倍数）", 2048)
            extended_memory = prompt_yes_no("是否使用 extended memory", False)
            custom_machine = CustomMachineConfig(series, cpu, memory_mb, extended_memory)
            validate_custom_machine(series, cpu, memory_mb)
            return "", custom_machine
        print("输入无效，请重试。")


def prompt_network_config():
    print("\n--- 网络配置 ---")
    network = prompt_text("VPC 网络", "global/networks/default")
    subnetwork = prompt_text("子网（留空自动/默认）", "")

    print("\n--- 外网 IP ---")
    print("[1] 临时外部 IP")
    print("[2] 已保留静态外部 IP")
    print("[3] 不分配外部 IP")
    while True:
        choice = input("请输入数字选择 (默认 1): ").strip() or "1"
        if choice == "1":
            external_ip_mode = "ephemeral"
            static_nat_ip = ""
            break
        if choice == "2":
            external_ip_mode = "static"
            static_nat_ip = prompt_text("请输入已保留静态外部 IP")
            break
        if choice == "3":
            external_ip_mode = "none"
            static_nat_ip = ""
            break
        print("输入无效，请重试。")

    network_tier = select_from_list(NETWORK_TIER_OPTIONS, "请选择网络层级", lambda t: t)
    tags_text = prompt_text("网络标签（逗号分隔，留空表示无）", "http-server,https-server")
    tags = [tag.strip() for tag in tags_text.split(",") if tag.strip()]

    return NetworkConfig(
        network=network,
        subnetwork=subnetwork,
        external_ip_mode=external_ip_mode,
        static_nat_ip=static_nat_ip,
        network_tier=network_tier,
        tags=tags,
    )


def prompt_custom_instance_config(project_id):
    zone = select_zone(project_id)
    os_config = select_os_image()
    instance_name = prompt_text("实例名", "custom-vm")
    machine_type, custom_machine = select_machine_type(project_id, zone)
    disk_size_gb = prompt_int("启动盘大小 GB", FREE_DISK_SIZE_GB)
    disk_type = select_from_list(DISK_TYPE_OPTIONS, "请选择磁盘类型", lambda d: d)
    network_config = prompt_network_config()

    config = InstanceConfig(
        instance_name=instance_name,
        zone=zone,
        os_image=os_config_to_image_config(os_config),
        machine_type=machine_type,
        custom_machine=custom_machine,
        disk=DiskConfig(size_gb=disk_size_gb, type=disk_type),
        network=network_config,
    )
    validate_instance_config(config)
    return config


def save_profile_interactively(config, profiles_path=DEFAULT_PROFILES_FILE):
    if not prompt_yes_no("是否将该配置保存为 profile", False):
        return
    profile_name = prompt_text("profile 名称", config.instance_name)
    data = load_profiles(profiles_path)
    data.setdefault("profiles", {})[profile_name] = config_to_profile(config)
    save_profiles(data, profiles_path)
    print_success(f"已保存 profile: {profile_name}")


def create_instance_from_config(
    project_id,
    config,
    dry_run=False,
    confirm_text=None,
    force_paid_prompt=True,
    pricing_api_key=None,
    pricing_currency="USD",
    estimate_price=True,
):
    validate_instance_config(config)
    print_instance_config_summary(config)

    if estimate_price and (pricing_api_key or os.environ.get("CLOUD_BILLING_API_KEY") or not is_free_tier_config(config)):
        try:
            estimate = estimate_instance_price(config, project_id, pricing_api_key, pricing_currency)
            print_price_estimate(estimate)
        except Exception as e:
            print_warning(f"价格估算失败: {e}")

    if dry_run:
        print_success("dry-run 完成：配置有效，未发送创建请求。")
        return True

    if not confirm_paid_creation_if_needed(config, confirm_text, force_paid_prompt):
        print_warning("未确认收费风险，已取消创建。")
        return False

    require_gcp_libraries()
    instance_client = compute_v1.InstancesClient()
    images_client = compute_v1.ImagesClient()

    print(f"\n[开始] 正在 {project_id} 项目中准备资源...")
    print(f"可用区: {config.zone}")
    print(f"系统: {config.os_image.name}")

    try:
        image_response = images_client.get_from_family(
            project=config.os_image.project,
            family=config.os_image.family,
        )
        source_disk_image = image_response.self_link
        instance = build_instance_resource(config, source_disk_image)

        print("配置组装完成，正在向 Google Cloud 发送创建请求...")
        operation = instance_client.insert(
            project=project_id,
            zone=config.zone,
            instance_resource=instance,
        )

        print("请求已发送，正在等待操作完成... (约 30-60 秒)")
        operation_client = compute_v1.ZoneOperationsClient()
        operation = operation_client.wait(
            project=project_id,
            zone=config.zone,
            operation=operation.name,
        )

        if operation.error:
            print("创建失败:", operation.error)
            return False
        else:
            print_success(f"实例 '{config.instance_name}' 已创建！")
            try:
                inst_info = instance_client.get(
                    project=project_id,
                    zone=config.zone,
                    instance=config.instance_name,
                )
                access_configs = inst_info.network_interfaces[0].access_configs
                if access_configs:
                    ip = access_configs[0].nat_i_p
                    print(f"外部 IP 地址: {ip}")
            except Exception:
                pass
            print("请前往 GCP 控制台查看详情。")
            return True

    except Exception as e:
        print(f"\n[失败] 操作中止: {e}")
        traceback.print_exc()
        return False


def create_instance(project_id, zone, os_config, instance_name=FREE_INSTANCE_NAME):
    config = default_free_instance_config(zone, os_config)
    config.instance_name = instance_name
    return create_instance_from_config(project_id, config)


def list_instances(project_id):
    require_gcp_libraries()
    instance_client = compute_v1.InstancesClient()
    request = compute_v1.AggregatedListInstancesRequest(project=project_id)

    print_info(f"正在扫描项目 {project_id} 中的实例...")

    instances = []
    for zone_path, response in instance_client.aggregated_list(request=request):
        if not response.instances:
            continue
        zone_short = zone_path.split("/")[-1]
        for instance in response.instances:
            network = None
            internal_ip = "-"
            external_ip = "-"
            if instance.network_interfaces:
                network = instance.network_interfaces[0].network
                internal_ip = instance.network_interfaces[0].network_i_p
                access_configs = instance.network_interfaces[0].access_configs
                if access_configs:
                    external_ip = access_configs[0].nat_i_p or "-"
            instances.append(
                {
                    "name": instance.name,
                    "zone": zone_short,
                    "status": instance.status,
                    "cpu_platform": instance.cpu_platform or "Unknown CPU Platform",
                    "network": network or "global/networks/default",
                    "internal_ip": internal_ip,
                    "external_ip": external_ip,
                }
            )
    return instances


def select_instance(project_id):
    instances = list_instances(project_id)
    if not instances:
        print_warning("该项目中没有任何实例！")
        return None

    print("\n--- 请选择目标服务器 ---")
    for i, inst in enumerate(instances):
        status_color = "\033[92m" if inst["status"] == "RUNNING" else "\033[91m"
        network_short = inst["network"].split("/")[-1] if inst["network"] else "-"
        print(
            f"[{i+1}] {inst['name']:<20} | 区域: {inst['zone']:<15} | 状态: "
            f"{status_color}{inst['status']}\033[0m | 网络: {network_short} | 内网IP: "
            f"{inst['internal_ip']} | 外网IP: {inst['external_ip']} | CPU: {inst['cpu_platform']}"
        )

    while True:
        choice = input(f"请输入数字选择 (1-{len(instances)}): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(instances):
                return instances[idx]
        print("输入无效，请重试。")


def wait_for_operation(project_id, zone, operation_name):
    operation_client = compute_v1.ZoneOperationsClient()
    return operation_client.wait(project=project_id, zone=zone, operation=operation_name)


def reroll_cpu_loop(project_id, instance_info):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]

    instance_client = compute_v1.InstancesClient()
    attempt_counter = 1

    print_info(f"目标实例: {instance_name} ({zone})")
    print_info("目标: 只要 CPU 包含 'AMD' 即停止。")

    while True:
        print("\n" + "=" * 50)
        print_info(f"第 {attempt_counter} 次尝试...")

        current_inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
        if current_inst.status != "RUNNING":
            print_info(f"正在启动虚拟机 {instance_name}...")
            op = instance_client.start(project=project_id, zone=zone, instance=instance_name)
            wait_for_operation(project_id, zone, op.name)
            print_info("虚拟机已通电，正在等待系统初始化...")

        current_platform = "Unknown CPU Platform"
        max_retries = 60

        for i in range(max_retries):
            current_inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)

            if current_inst.status != "RUNNING":
                print_warning(f"检测到虚拟机状态异常变为: {current_inst.status}。跳过本次检测。")
                current_platform = "Instability Detected"
                break

            current_platform = current_inst.cpu_platform
            if current_platform and current_platform != "Unknown CPU Platform":
                break

            if (i + 1) % 5 == 0:
                print_info(f"正在等待 CPU 元数据同步... ({i+1}/{max_retries}) - 机器正在启动中")
            time.sleep(2)

        if current_platform == "Unknown CPU Platform":
            print_warning("超时：等待 2 分钟后仍无法获取 CPU 信息。")
        else:
            print_info(f"检测到 CPU: {current_platform}")

        if "AMD" in str(current_platform).upper():
            print_success(f"恭喜！已成功刷到目标 CPU: {current_platform}")
            print_info("脚本执行完毕。")
            break

        print_warning(f"结果不满意 ({current_platform})。准备重置...")
        print_info(f"正在关停虚拟机 {instance_name}...")
        op = instance_client.stop(project=project_id, zone=zone, instance=instance_name)
        wait_for_operation(project_id, zone, op.name)
        attempt_counter += 1
        time.sleep(2)


def read_cdn_ips(filename="cdnip.txt"):
    if not os.path.exists(filename):
        print(f"【错误】找不到文件: {filename}")
        print("请在脚本同目录下创建该文件，并填入IP段。")
        return []

    ip_list = []
    with open(filename, "r", encoding="utf-8") as f:
        for line in f:
            clean_line = line.strip()
            if clean_line:
                ip = clean_line.split()[0]
                ip_list.append(ip)

    print(f"已从 {filename} 读取到 {len(ip_list)} 个 IP 段。")
    return ip_list


def set_protocol_field(config_object, value):
    try:
        config_object.ip_protocol = value
    except AttributeError:
        try:
            config_object.I_p_protocol = value
        except AttributeError:
            print(f"\n【调试信息】无法设置协议字段。对象 '{type(config_object).__name__}' 的有效属性如下:")
            print([d for d in dir(config_object) if not d.startswith("_")])
            raise


def add_allow_all_ingress(project_id, network):
    firewall_client = compute_v1.FirewallsClient()
    rule_name = "allow-all-ingress-custom"

    print(f"\n正在创建入站规则: {rule_name} ...")

    firewall_rule = compute_v1.Firewall()
    firewall_rule.name = rule_name
    firewall_rule.direction = "INGRESS"
    firewall_rule.network = network
    firewall_rule.priority = 1000
    firewall_rule.source_ranges = ["0.0.0.0/0"]

    allow_config = compute_v1.Allowed()
    set_protocol_field(allow_config, "all")
    firewall_rule.allowed = [allow_config]

    try:
        operation = firewall_client.insert(project=project_id, firewall_resource=firewall_rule)
        print("正在应用规则...")
        operation_client = compute_v1.GlobalOperationsClient()
        operation_client.wait(project=project_id, operation=operation.name)
        print_success("已添加允许所有入站连接的规则。")
    except Exception as e:
        if "already exists" in str(e):
            print_warning(f"规则 {rule_name} 已存在。")
        else:
            print(f"【失败】{e}")
            traceback.print_exc()


def add_deny_cdn_egress(project_id, ip_ranges, network):
    if not ip_ranges:
        print("IP 列表为空，跳过创建拒绝规则。")
        return

    firewall_client = compute_v1.FirewallsClient()
    rule_name = "deny-cdn-egress-custom"

    print(f"\n正在创建出站拒绝规则: {rule_name} ...")

    firewall_rule = compute_v1.Firewall()
    firewall_rule.name = rule_name
    firewall_rule.direction = "EGRESS"
    firewall_rule.network = network
    firewall_rule.priority = 900
    firewall_rule.destination_ranges = ip_ranges

    deny_config = compute_v1.Denied()
    set_protocol_field(deny_config, "all")
    firewall_rule.denied = [deny_config]

    try:
        operation = firewall_client.insert(project=project_id, firewall_resource=firewall_rule)
        print("正在应用规则...")
        operation_client = compute_v1.GlobalOperationsClient()
        operation_client.wait(project=project_id, operation=operation.name)
        print_success(f"已添加拒绝规则，共拦截 {len(ip_ranges)} 个 IP 段。")
    except Exception as e:
        if "already exists" in str(e):
            print_warning(f"规则 {rule_name} 已存在。")
        else:
            print(f"【失败】{e}")
            traceback.print_exc()


def configure_firewall(project_id, network):
    print("\n------------------------------------------------")
    print("防火墙规则管理菜单")
    print("------------------------------------------------")
    print(f"目标网络: {network}")

    choice_in = input("\n[1/2] 是否添加【允许所有入站连接 (0.0.0.0/0)】规则? (y/n): ").strip().lower()
    if choice_in == "y":
        add_allow_all_ingress(project_id, network)
    else:
        print("已跳过入站规则配置。")

    choice_out = input("\n[2/2] 是否添加【拒绝对 cdnip.txt 中 IP 的出站连接】规则? (y/n): ").strip().lower()
    if choice_out == "y":
        ips = read_cdn_ips()
        if ips:
            if len(ips) > 256:
                print(f"【警告】IP 数量 ({len(ips)}) 超过 GCP 单条规则上限 (256)。")
                print("脚本将只取前 256 个 IP。")
                ips = ips[:256]

            add_deny_cdn_egress(project_id, ips, network)
    else:
        print("已跳过出站规则配置。")

    print("\n所有操作完成。")


def is_not_found_error(exc):
    msg = str(exc).lower()
    return "notfound" in msg or "not found" in msg or "404" in msg


def delete_firewall_rule(project_id, rule_name):
    firewall_client = compute_v1.FirewallsClient()
    try:
        operation = firewall_client.delete(project=project_id, firewall=rule_name)
        operation_client = compute_v1.GlobalOperationsClient()
        operation_client.wait(project=project_id, operation=operation.name)
        print_success(f"已删除防火墙规则: {rule_name}")
        return True
    except Exception as e:
        if is_not_found_error(e):
            print_info(f"防火墙规则不存在，已跳过: {rule_name}")
            return True
        print_warning(f"删除防火墙规则失败: {rule_name} ({e})")
        return False


def delete_disks_if_needed(project_id, zone, disk_names):
    if not disk_names:
        return True
    disk_client = compute_v1.DisksClient()
    all_ok = True
    for disk_name in disk_names:
        try:
            operation = disk_client.delete(project=project_id, zone=zone, disk=disk_name)
            wait_for_operation(project_id, zone, operation.name)
            print_success(f"已删除磁盘: {disk_name}")
        except Exception as e:
            if is_not_found_error(e):
                print_info(f"磁盘不存在，已跳过: {disk_name}")
            else:
                print_warning(f"删除磁盘失败: {disk_name} ({e})")
                all_ok = False
    return all_ok


def delete_free_resources(project_id, instance_info):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]

    print("\n------------------------------------------------")
    print("即将删除以下资源（可以重新创建免费资源）：")
    print(f"- 实例: {instance_name} ({zone})")
    print(f"- 相关磁盘（如仍存在）")
    print(f"- 防火墙规则: {', '.join(FIREWALL_RULES_TO_CLEAN)}")
    confirm = input("请输入 DELETE 确认删除: ").strip()
    if confirm != "DELETE":
        print("已取消删除操作。")
        return False

    instance_client = compute_v1.InstancesClient()
    disk_names = []
    try:
        inst = instance_client.get(project=project_id, zone=zone, instance=instance_name)
        for disk in inst.disks:
            if disk.source:
                disk_names.append(disk.source.split("/")[-1])
    except Exception as e:
        print_warning(f"读取实例信息失败，磁盘清理可能不完整: {e}")

    print_info("正在删除实例...")
    try:
        operation = instance_client.delete(project=project_id, zone=zone, instance=instance_name)
        wait_for_operation(project_id, zone, operation.name)
        print_success("实例已删除。")
    except Exception as e:
        if is_not_found_error(e):
            print_info("实例不存在，已跳过删除。")
        else:
            print_warning(f"实例删除失败: {e}")
            return False

    delete_disks_if_needed(project_id, zone, disk_names)

    print_info("正在清理防火墙规则...")
    for rule_name in FIREWALL_RULES_TO_CLEAN:
        delete_firewall_rule(project_id, rule_name)

    print_success("清理完成。建议到控制台确认无残留资源。")
    return True


def pick_remote_method():
    has_gcloud = shutil.which("gcloud") is not None
    has_ssh = shutil.which("ssh") is not None

    if not has_gcloud and not has_ssh:
        print_warning("本机未发现 gcloud 或 ssh，无法执行远程脚本。")
        return None

    if has_gcloud:
        choice = input("是否使用 gcloud compute ssh 远程执行? (Y/n): ").strip().lower()
        if choice in ("", "y", "yes"):
            return {"method": "gcloud"}

    if not has_ssh:
        print_warning("未找到 ssh 命令，无法继续。")
        return None

    default_user = getpass.getuser()
    ssh_user = input(f"请输入 SSH 用户名 (默认 {default_user}): ").strip() or default_user
    ssh_port = input("请输入 SSH 端口 (默认 22): ").strip() or "22"
    ssh_key = input("请输入 SSH 私钥路径 (留空表示使用默认密钥): ").strip()
    return {"method": "ssh", "user": ssh_user, "port": ssh_port, "key": ssh_key}


def build_remote_download_command(script_url):
    return (
        "set -e;"
        "if command -v curl >/dev/null 2>&1; then DL=\"curl -fsSL\";"
        "elif command -v wget >/dev/null 2>&1; then DL=\"wget -qO-\";"
        "else echo \"error: curl or wget not found\"; exit 1; fi;"
        "tmp=$(mktemp /tmp/gcp_free.XXXXXX.sh);"
        f"$DL \"{script_url}\" > \"$tmp\";"
        "sudo bash \"$tmp\";"
        "rm -f \"$tmp\""
    )


def build_remote_exec_command(project_id, instance_info, remote_config, remote_command):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]
    method = remote_config.get("method")

    if method == "gcloud":
        return [
            "gcloud",
            "compute",
            "ssh",
            instance_name,
            "--project",
            project_id,
            "--zone",
            zone,
            "--command",
            remote_command,
        ]
    if method == "ssh":
        host = instance_info.get("external_ip")
        if not host or host == "-":
            print_warning("该实例没有外网 IP，无法使用 SSH 直连。")
            return None
        cmd = ["ssh"]
        port = remote_config.get("port")
        if port:
            cmd += ["-p", str(port)]
        key_path = remote_config.get("key")
        if key_path:
            cmd += ["-i", key_path]
        cmd += [f"{remote_config.get('user')}@{host}", remote_command]
        return cmd

    print_warning("远程执行方式未设置。")
    return None


def build_remote_upload_command(project_id, instance_info, remote_config, local_path, remote_path):
    instance_name = instance_info["name"]
    zone = instance_info["zone"]
    method = remote_config.get("method")

    if method == "gcloud":
        return [
            "gcloud",
            "compute",
            "scp",
            local_path,
            f"{instance_name}:{remote_path}",
            "--project",
            project_id,
            "--zone",
            zone,
        ]
    if method == "ssh":
        if shutil.which("scp") is None:
            print_warning("未找到 scp 命令，无法上传文件。")
            return None
        host = instance_info.get("external_ip")
        if not host or host == "-":
            print_warning("该实例没有外网 IP，无法使用 SSH 直连。")
            return None
        cmd = ["scp"]
        port = remote_config.get("port")
        if port:
            cmd += ["-P", str(port)]
        key_path = remote_config.get("key")
        if key_path:
            cmd += ["-i", key_path]
        cmd += [local_path, f"{remote_config.get('user')}@{host}:{remote_path}"]
        return cmd

    print_warning("远程执行方式未设置。")
    return None


def run_remote_script(project_id, instance_info, script_key, remote_config):
    script_url = REMOTE_SCRIPT_URLS.get(script_key)
    if not script_url:
        print_warning("未知的脚本类型，无法执行。")
        return False
    remote_command = build_remote_download_command(script_url)
    cmd = build_remote_exec_command(project_id, instance_info, remote_config, remote_command)
    if not cmd:
        return False

    print_info(f"正在远程执行脚本: {script_url}")
    try:
        result = subprocess.run(cmd)
        if result.returncode == 0:
            print_success("远程脚本执行完成。")
            return True
        print_warning(f"远程脚本执行失败，退出码: {result.returncode}")
        return False
    except Exception as e:
        print_warning(f"远程执行失败: {e}")
        return False


def select_traffic_monitor_script():
    print("\n--- 请选择流量监控脚本 ---")
    print("[1] 安装 超额关闭 ssh 之外其他入站 (net_iptables.sh)")
    print("[2] 安装 超额自动关机 (net_shutdown.sh)")
    print("[0] 返回")
    while True:
        choice = input("请输入数字选择: ").strip()
        if choice == "1":
            return "net_iptables"
        if choice == "2":
            return "net_shutdown"
        if choice == "0":
            return None
        print("输入无效，请重试。")


def deploy_dae_config(project_id, instance_info, remote_config):
    local_config = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.dae")
    if not os.path.isfile(local_config):
        print_warning(f"找不到本地配置文件: {local_config}")
        return False

    remote_tmp = "/tmp/config.dae"
    upload_cmd = build_remote_upload_command(
        project_id,
        instance_info,
        remote_config,
        local_config,
        remote_tmp,
    )
    if not upload_cmd:
        return False

    print_info("正在上传 config.dae ...")
    try:
        result = subprocess.run(upload_cmd)
        if result.returncode != 0:
            print_warning(f"上传失败，退出码: {result.returncode}")
            return False
    except Exception as e:
        print_warning(f"上传失败: {e}")
        return False

    remote_command = (
        "set -e;"
        "sudo mkdir -p /usr/local/etc/dae;"
        "sudo cp /tmp/config.dae /usr/local/etc/dae/config.dae;"
        "sudo chmod 600 /usr/local/etc/dae/config.dae;"
        "sudo systemctl enable dae;"
        "sudo systemctl restart dae;"
        "rm -f /tmp/config.dae"
    )
    exec_cmd = build_remote_exec_command(project_id, instance_info, remote_config, remote_command)
    if not exec_cmd:
        return False

    print_info("正在应用配置并重启 dae ...")
    try:
        result = subprocess.run(exec_cmd)
        if result.returncode == 0:
            print_success("配置已更新并重启 dae。")
            return True
        print_warning(f"配置应用失败，退出码: {result.returncode}")
        return False
    except Exception as e:
        print_warning(f"配置应用失败: {e}")
        return False


def parse_tags(value):
    if value is None:
        return ["http-server", "https-server"]
    if value.strip() == "":
        return []
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def config_from_cli_args(args):
    custom_machine = None
    machine_type = getattr(args, "machine_type", "") or ""
    if getattr(args, "custom_series", None):
        custom_machine = CustomMachineConfig(
            series=args.custom_series,
            cpu=args.custom_cpu,
            memory_mb=args.custom_memory_mb,
            extended_memory=getattr(args, "custom_extended_memory", False),
        )
        machine_type = ""

    config = InstanceConfig(
        instance_name=args.name,
        zone=args.zone,
        os_image=OsImageConfig(
            project=getattr(args, "os_project", "debian-cloud"),
            family=getattr(args, "os_family", "debian-12"),
            name=getattr(args, "os_family", "debian-12"),
        ),
        machine_type=machine_type,
        custom_machine=custom_machine,
        disk=DiskConfig(
            size_gb=getattr(args, "disk_size_gb", FREE_DISK_SIZE_GB),
            type=getattr(args, "disk_type", FREE_DISK_TYPE),
        ),
        network=NetworkConfig(
            network=getattr(args, "network", "global/networks/default"),
            subnetwork=getattr(args, "subnetwork", ""),
            external_ip_mode=getattr(args, "external_ip_mode", "ephemeral"),
            static_nat_ip=getattr(args, "static_nat_ip", ""),
            network_tier=getattr(args, "network_tier", "STANDARD"),
            tags=parse_tags(getattr(args, "tags", None)),
        ),
    )
    validate_instance_config(config)
    return config


def resolve_project_for_command(args):
    if getattr(args, "project", None):
        return args.project
    if getattr(args, "dry_run", False):
        return "dry-run-project"
    return select_gcp_project()


def handle_create_free(args):
    os_config = {
        "name": getattr(args, "os_family", "debian-12"),
        "project": args.os_project,
        "family": args.os_family,
    }
    config = default_free_instance_config(args.zone, os_config)
    config.instance_name = args.name
    project_id = resolve_project_for_command(args)
    return create_instance_from_config(
        project_id,
        config,
        dry_run=args.dry_run,
        confirm_text=getattr(args, "confirm_paid", None),
        force_paid_prompt=not args.dry_run,
        pricing_api_key=getattr(args, "pricing_api_key", None),
        pricing_currency=getattr(args, "pricing_currency", "USD"),
        estimate_price=not getattr(args, "no_price_estimate", False),
    )


def handle_create_custom(args):
    config = config_from_cli_args(args)
    if getattr(args, "save_profile", ""):
        data = load_profiles(args.profiles_file)
        data.setdefault("profiles", {})[args.save_profile] = config_to_profile(config)
        save_profiles(data, args.profiles_file)
        print_success(f"已保存 profile: {args.save_profile}")
    project_id = resolve_project_for_command(args)
    return create_instance_from_config(
        project_id,
        config,
        dry_run=args.dry_run,
        confirm_text=args.confirm_paid,
        force_paid_prompt=not args.dry_run,
        pricing_api_key=getattr(args, "pricing_api_key", None),
        pricing_currency=getattr(args, "pricing_currency", "USD"),
        estimate_price=not getattr(args, "no_price_estimate", False),
    )


def handle_create_from_profile(args):
    config = get_profile_config(args.profile, args.profiles_file)
    project_id = resolve_project_for_command(args)
    return create_instance_from_config(
        project_id,
        config,
        dry_run=args.dry_run,
        confirm_text=args.confirm_paid,
        force_paid_prompt=not args.dry_run,
        pricing_api_key=getattr(args, "pricing_api_key", None),
        pricing_currency=getattr(args, "pricing_currency", "USD"),
        estimate_price=not getattr(args, "no_price_estimate", False),
    )


def handle_profiles(args):
    if args.profile_command == "list":
        data = load_profiles(args.profiles_file)
        profiles = data.get("profiles", {})
        if not profiles:
            print_info("当前没有保存任何 profile。")
            return True
        print("\n--- Profiles ---")
        for name, profile in sorted(profiles.items()):
            zone = profile.get("zone", "-")
            machine_type = profile.get("machine_type")
            if not machine_type and profile.get("custom_machine"):
                cm = profile["custom_machine"]
                machine_type = build_custom_machine_type(
                    cm.get("series", ""),
                    int(cm.get("cpu", 0)),
                    int(cm.get("memory_mb", 0)),
                    bool(cm.get("extended_memory", False)),
                )
            print(f"- {name}: {profile.get('instance_name', name)} | {zone} | {machine_type or '-'}")
        return True

    if args.profile_command == "save":
        config = config_from_cli_args(args)
        data = load_profiles(args.profiles_file)
        data.setdefault("profiles", {})[args.profile] = config_to_profile(config)
        save_profiles(data, args.profiles_file)
        print_success(f"已保存 profile: {args.profile}")
        return True

    if args.profile_command == "import-url":
        imported = fetch_json_url(args.url)
        count = merge_imported_profiles(imported, args.profiles_file)
        print_success(f"已导入 {count} 个 profile。")
        return True

    if args.profile_command == "pull-webdav":
        url = resolve_webdav_url(args, args.profiles_file)
        if not url:
            print_warning("未提供 WebDAV URL。请使用 --url 或设置 WEBDAV_URL。")
            return False
        username, password = get_webdav_credentials()
        imported = fetch_json_url(url, username, password)
        count = merge_imported_profiles(imported, args.profiles_file)
        print_success(f"已从 WebDAV 拉取并导入 {count} 个 profile。")
        return True

    if args.profile_command == "push-webdav":
        url = resolve_webdav_url(args, args.profiles_file)
        if not url:
            print_warning("未提供 WebDAV URL。请使用 --url 或设置 WEBDAV_URL。")
            return False
        username, password = get_webdav_credentials()
        data = load_profiles(args.profiles_file)
        put_json_url(url, data, username, password)
        print_success("已推送 profiles.json 到 WebDAV。")
        return True

    print_warning("未知 profiles 子命令。")
    return False


def add_instance_config_arguments(parser, require_name=True):
    parser.add_argument("--name", required=require_name, default=None if require_name else "custom-vm", help="实例名")
    parser.add_argument("--zone", required=True, help="可用区，例如 us-west1-b")
    parser.add_argument("--os-project", default="debian-cloud", help="镜像项目")
    parser.add_argument("--os-family", default="debian-12", help="镜像 family")
    machine_group = parser.add_mutually_exclusive_group()
    machine_group.add_argument("--machine-type", default=FREE_MACHINE_TYPE, help="预定义 machine type")
    machine_group.add_argument("--custom-series", choices=sorted(CUSTOM_MACHINE_SERIES), help="自定义机器系列")
    parser.add_argument("--custom-cpu", type=int, default=2, help="自定义 vCPU 数量")
    parser.add_argument("--custom-memory-mb", type=int, default=2048, help="自定义内存 MB")
    parser.add_argument("--custom-extended-memory", action="store_true", help="自定义机器类型启用 extended memory")
    parser.add_argument("--disk-size-gb", type=int, default=FREE_DISK_SIZE_GB, help="启动盘大小 GB")
    parser.add_argument("--disk-type", choices=DISK_TYPE_OPTIONS, default=FREE_DISK_TYPE, help="启动盘类型")
    parser.add_argument("--network", default="global/networks/default", help="VPC 网络")
    parser.add_argument("--subnetwork", default="", help="子网，留空则使用网络默认")
    parser.add_argument("--external-ip-mode", choices=EXTERNAL_IP_MODES, default="ephemeral", help="外部 IP 模式")
    parser.add_argument("--static-nat-ip", default="", help="已保留静态外部 IP")
    parser.add_argument("--network-tier", choices=NETWORK_TIER_OPTIONS, default="STANDARD", help="网络层级")
    parser.add_argument("--tags", default="http-server,https-server", help="网络标签，逗号分隔；空字符串表示无标签")


def add_pricing_arguments(parser):
    parser.add_argument("--pricing-api-key", default="", help="Cloud Billing Catalog API key；也可用 CLOUD_BILLING_API_KEY")
    parser.add_argument("--pricing-currency", default="USD", help="价格币种，默认 USD")
    parser.add_argument("--no-price-estimate", action="store_true", help="跳过价格估算")


def build_arg_parser():
    parser = argparse.ArgumentParser(description="GCP 免费/自定义服务器多功能管理工具")
    parser.add_argument("--project", help="GCP 项目 ID")
    parser.add_argument("--profiles-file", default=DEFAULT_PROFILES_FILE, help="本地 profiles JSON 路径")

    subparsers = parser.add_subparsers(dest="command")

    create_free = subparsers.add_parser("create-free", help="创建免费实例")
    create_free.add_argument("--name", default=FREE_INSTANCE_NAME, help="实例名")
    create_free.add_argument("--zone", default="us-west1-b", help="免费区域内的可用区")
    create_free.add_argument("--os-project", default="debian-cloud", help="镜像项目")
    create_free.add_argument("--os-family", default="debian-12", help="镜像 family")
    create_free.add_argument("--dry-run", action="store_true", help="只验证配置，不创建实例")
    create_free.add_argument("--confirm-paid", default="", help=f"收费配置确认文本，必须为 {PAID_CONFIRM_TEXT}")
    add_pricing_arguments(create_free)

    create_custom = subparsers.add_parser("create-custom", help="创建自定义实例")
    add_instance_config_arguments(create_custom)
    create_custom.add_argument("--dry-run", action="store_true", help="只验证配置，不创建实例")
    create_custom.add_argument("--confirm-paid", default="", help=f"收费配置确认文本，必须为 {PAID_CONFIRM_TEXT}")
    create_custom.add_argument("--save-profile", default="", help="创建前保存为指定 profile 名称")
    add_pricing_arguments(create_custom)

    create_from_profile = subparsers.add_parser("create-from-profile", help="从 profile 创建实例")
    create_from_profile.add_argument("--profile", required=True, help="profile 名称")
    create_from_profile.add_argument("--dry-run", action="store_true", help="只验证配置，不创建实例")
    create_from_profile.add_argument("--confirm-paid", default="", help=f"收费配置确认文本，必须为 {PAID_CONFIRM_TEXT}")
    add_pricing_arguments(create_from_profile)

    profiles = subparsers.add_parser("profiles", help="管理本地/远端 profiles")
    profile_subparsers = profiles.add_subparsers(dest="profile_command", required=True)

    profile_subparsers.add_parser("list", help="列出本地 profiles")

    profile_save = profile_subparsers.add_parser("save", help="从 CLI 参数保存 profile")
    profile_save.add_argument("--profile", required=True, help="profile 名称")
    add_instance_config_arguments(profile_save)

    profile_import = profile_subparsers.add_parser("import-url", help="从 HTTP(S) URL 导入 profiles JSON")
    profile_import.add_argument("--url", required=True, help="profiles JSON URL")

    profile_pull = profile_subparsers.add_parser("pull-webdav", help="从 WebDAV 拉取 profiles JSON")
    profile_pull.add_argument("--url", default="", help="WebDAV 文件 URL")

    profile_push = profile_subparsers.add_parser("push-webdav", help="推送本地 profiles JSON 到 WebDAV")
    profile_push.add_argument("--url", default="", help="WebDAV 文件 URL")

    return parser


def run_cli(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.command:
        return main()
    if args.command == "create-free":
        return handle_create_free(args)
    if args.command == "create-custom":
        return handle_create_custom(args)
    if args.command == "create-from-profile":
        return handle_create_from_profile(args)
    if args.command == "profiles":
        return handle_profiles(args)
    parser.print_help()
    return False


def main():
    print("GCP 免费服务器多功能管理工具")
    project_id = select_gcp_project()
    current_instance = None
    remote_config = None

    while True:
        print("\n================================================")
        print(f"当前项目: {project_id}")
        if current_instance:
            print(f"当前服务器: {current_instance['name']} ({current_instance['zone']})")
        else:
            print("当前服务器: 未选择")
        print("------------------------------------------------")
        print("[1] 新建免费实例")
        print("[2] 新建自定义实例")
        print("[3] 从 profile 创建实例")
        print("[4] 选择服务器")
        print("[5] 刷 AMD CPU")
        print("[6] 配置防火墙规则")
        print("[7] Debian换源")
        print("[8] 安装 dae")
        print("[9] 上传 config.dae 并启用 dae")
        print("[10] 安装流量监控脚本（仅适配 Debian）")
        print("[11] 删除当前免费资源")
        print("[0] 退出")
        choice = input("请输入数字选择: ").strip()

        if choice == "1":
            zone = select_zone(project_id)
            os_config = select_os_image()
            create_instance(project_id, zone, os_config)
        elif choice == "2":
            config = prompt_custom_instance_config(project_id)
            save_profile_interactively(config)
            create_instance_from_config(project_id, config)
        elif choice == "3":
            data = load_profiles(DEFAULT_PROFILES_FILE)
            profiles = sorted(data.get("profiles", {}).keys())
            if not profiles:
                print_warning("当前没有保存任何 profile。")
            else:
                profile_name = select_from_list(profiles, "请选择 profile", lambda p: p)
                config = profile_to_config(data["profiles"][profile_name])
                create_instance_from_config(project_id, config)
        elif choice == "4":
            current_instance = select_instance(project_id)
        elif choice == "5":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                reroll_cpu_loop(project_id, current_instance)
        elif choice == "6":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                network = current_instance.get("network") or "global/networks/default"
                configure_firewall(project_id, network)
        elif choice == "7":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    run_remote_script(project_id, current_instance, "apt", remote_config)
        elif choice == "8":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    run_remote_script(project_id, current_instance, "dae", remote_config)
        elif choice == "9":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if not remote_config:
                    remote_config = pick_remote_method()
                if remote_config:
                    deploy_dae_config(project_id, current_instance, remote_config)
        elif choice == "10":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                script_key = select_traffic_monitor_script()
                if script_key:
                    if not remote_config:
                        remote_config = pick_remote_method()
                    if remote_config:
                        run_remote_script(project_id, current_instance, script_key, remote_config)
        elif choice == "11":
            if not current_instance:
                current_instance = select_instance(project_id)
            if current_instance:
                if delete_free_resources(project_id, current_instance):
                    current_instance = None
        elif choice == "0":
            print("已退出。")
            break
        else:
            print("输入无效，请重试。")


if __name__ == "__main__":
    try:
        ok = run_cli()
        if ok is False:
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n[用户终止] 脚本已停止。")
    except Exception as e:
        print(f"\n[错误] 发生异常: {e}")
        traceback.print_exc()
