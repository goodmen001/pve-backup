import json
import os
from pathlib import Path
from typing import Any, Dict, Optional
from .logger import logger
from .notification.notifications import get_default_notify_channels

DATA_DIR = Path(os.environ.get("PVE_BACKUP_DATA_DIR", "/data"))
CONFIG_FILE = DATA_DIR / "config.json"
BACKUP_DIR = Path(os.environ.get("PVE_BACKUP_DIR", "/backups"))


def get_default_config() -> dict:
    return {
        "login_username": "admin",
        "login_password": "admin123",
        "enabled": False,
        "cron": "0 3 * * *",
        "onlyonce": False,
        "notify": False,
        "notify_channels": get_default_notify_channels(),
        "retry_count": 0,
        "retry_interval": 60,
        # SSH
        "pve_host": "",
        "ssh_port": 22,
        "ssh_username": "root",
        "ssh_password": "",
        "ssh_key_file": "",
        # Backup
        "storage_name": "local",
        "enable_local_backup": True,
        "backup_path": str(BACKUP_DIR),
        "backup_mode": "snapshot",
        "compress_mode": "zstd",
        "backup_vmid": "",
        "keep_backup_num": 5,
        "download_all_backups": True,
        # WebDAV
        "enable_webdav": False,
        "webdav_url": "",
        "webdav_username": "",
        "webdav_password": "",
        "webdav_path": "",
        "webdav_keep_backup_num": 7,
        "clear_history": False,
        # Restore
        "enable_restore": False,
        "restore_storage": "local",
        "restore_vmid": "",
        "restore_force": False,
        "restore_skip_existing": True,
        "restore_file": "",
        "restore_now": False,
        # Misc
        "auto_cleanup_tmp": True,
        "enable_log_cleanup": False,
        "log_journal_days": 0,
        "log_vzdump_keep": 0,
        "log_pve_keep": 0,
        "log_dpkg_keep": 0,
        "cleanup_template_images": False,
    }


def load_config() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cfg = None
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
    if cfg is None:
        return get_default_config()
    # Ensure notify_channels exists (for upgrades from old config)
    if "notify_channels" not in cfg:
        cfg["notify_channels"] = get_default_notify_channels()
    return cfg


def save_config(config: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")


def get_config_value(key: str, default=None):
    config = load_config()
    return config.get(key, default)


def set_config_value(key: str, value: Any):
    config = load_config()
    config[key] = value
    save_config(config)
