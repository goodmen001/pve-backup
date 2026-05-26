import os
import sys
import time
import threading
import tempfile
import logging
from pathlib import Path
from typing import Optional

import paramiko

from ..pve.client import get_pve_status, get_container_status, get_qemu_status, clean_pve_tmp_files, clean_pve_logs, list_template_images
from ..config import BACKUP_DIR

logger = logging.getLogger("pve-backup")


def _ssh_connect(ctx):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    host = getattr(ctx, "_pve_host", "")
    port = getattr(ctx, "_ssh_port", 22)
    user = getattr(ctx, "_ssh_username", "root")
    pwd = getattr(ctx, "_ssh_password", "")
    key = getattr(ctx, "_ssh_key_file", "")
    if key:
        pk = paramiko.RSAKey.from_private_key_file(key)
        ssh.connect(host, port=port, username=user, pkey=pk)
    else:
        ssh.connect(host, port=port, username=user, password=pwd)
    return ssh


class APIHandler:
    def __init__(self, ctx):
        self.ctx = ctx

    # ── Config ──
    def get_config(self):
        return self.ctx.get_config()

    def save_config(self, data: dict):
        # Merge into current config and save
        cfg = self.ctx.get_config()
        cfg.update(data)
        self.ctx.save_config(cfg)
        # Re-init app context
        for k, v in data.items():
            setattr(self.ctx, f"_{k}", v)
        self.ctx.scheduler_manager.setup_scheduler()
        return {"success": True, "message": "配置已保存"}

    # ── Status ──
    def get_status(self):
        cfg = self.ctx.get_config()
        next_run = None
        if self.ctx._scheduler:
            job = self.ctx._scheduler.get_job("pve_backup_cron")
            if job and job.next_run_time:
                next_run = job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
        return {
            "enabled": self.ctx._enabled if hasattr(self.ctx, "_enabled") else False,
            "backup_activity": self.ctx._backup_activity,
            "restore_activity": self.ctx._restore_activity,
            "enable_restore": getattr(self.ctx, "_enable_restore", False),
            "cron": getattr(self.ctx, "_cron", "0 3 * * *"),
            "next_run_time": next_run,
            "enable_log_cleanup": getattr(self.ctx, "_enable_log_cleanup", False),
            "auto_cleanup_tmp": cfg.get("auto_cleanup_tmp", True),
            "status_poll_interval": cfg.get("status_poll_interval", 30000),
            "container_poll_interval": cfg.get("container_poll_interval", 30000),
        }

    # ── Dashboard ──
    def get_dashboard_data(self):
        bh = self.ctx.history_handler.load_backup_history()
        rh = self.ctx.history_handler.load_restore_history()
        ab = self.ctx.backup_manager.get_available_backups()
        return {
            "backup_stats": {
                "total": len(bh),
                "successful": sum(1 for x in bh if x.get("success")),
                "failed": sum(1 for x in bh if not x.get("success")),
            },
            "restore_stats": {
                "total": len(rh),
                "successful": sum(1 for x in rh if x.get("success")),
                "failed": sum(1 for x in rh if not x.get("success")),
            },
            "available_backups": {
                "local": sum(1 for x in ab if x["source"] == "本地备份"),
                "webdav": sum(1 for x in ab if x["source"] == "WebDAV备份"),
                "total": len(ab),
            },
            "status": {
                "backup_activity": self.ctx._backup_activity,
                "restore_activity": self.ctx._restore_activity,
                "running": self.ctx._running,
            },
        }

    # ── History ──
    def get_backup_history(self):
        return self.ctx.history_handler.load_backup_history() or []

    def get_restore_history(self):
        return self.ctx.history_handler.load_restore_history() or []

    def clear_history(self):
        self.ctx.history_handler.clear_all_history()
        return {"success": True, "message": "历史已清理"}

    # ── Backup actions ──
    def run_backup(self):
        if not getattr(self.ctx, "_pve_host", "") or not getattr(self.ctx, "_ssh_username", ""):
            return {"success": False, "message": "PVE配置不完整，请先在配置页填写主机和SSH信息"}
        if not getattr(self.ctx, "_ssh_password", "") and not getattr(self.ctx, "_ssh_key_file", ""):
            return {"success": False, "message": "SSH认证未配置，请填写密码或SSH密钥"}
        lock = getattr(self.ctx, "_lock", None)
        if lock and lock.locked():
            return {"success": False, "message": "备份任务正在执行中，请等待完成"}
        g_lock = getattr(self.ctx, "_global_task_lock", None)
        if g_lock and g_lock.locked():
            return {"success": False, "message": "其他任务正在执行中，请等待完成"}
        threading.Thread(target=self.ctx.backup_executor.run_backup_job, daemon=True).start()
        return {"success": True, "message": "备份任务已启动"}

    def get_available_backups(self):
        return self.ctx.backup_manager.get_available_backups() or []

    def _find_local_file(self, filename: str) -> Optional[str]:
        bp = Path(getattr(self.ctx, "_backup_path", str(BACKUP_DIR)))
        for sub in ["containers", "virtualmachines"]:
            fp = bp / sub / filename
            if fp.is_file() and str(fp.resolve()).startswith(str(bp.resolve())):
                return str(fp)
        return None

    def delete_backup(self, data: dict):
        fn = data.get("filename", "")
        src = data.get("source", "本地备份")
        if not fn:
            return {"success": False, "message": "缺少文件名"}
        if src == "本地备份":
            fp = self._find_local_file(fn)
            if not fp:
                return {"success": False, "message": "文件不存在"}
            os.remove(fp)
            return {"success": True, "message": f"已删除: {fn}"}
        elif src == "WebDAV备份":
            from ..storage.webdav import WebDAVClient
            client = WebDAVClient(
                url=self.ctx._webdav_url, username=self.ctx._webdav_username,
                password=self.ctx._webdav_password, path=self.ctx._webdav_path,
                skip_dir_check=True, logger=logger, plugin_name="PVE-Backup",
            )
            ok, err = client.delete_file(fn)
            client.close()
            if ok:
                return {"success": True, "message": f"已删除WebDAV: {fn}"}
            return {"success": False, "message": f"删除失败: {err}"}
        return {"success": False, "message": "不支持的来源"}

    def restore_backup(self, data: dict):
        fn = data.get("filename", "")
        src = data.get("source", "本地备份")
        vmid = data.get("restore_vmid", "")
        force = data.get("restore_force", False)
        skip = data.get("restore_skip_existing", True)
        if not fn:
            return {"success": False, "message": "缺少文件名"}
        threading.Thread(
            target=self.ctx.restore_executor.run_restore_job,
            args=(fn, src, vmid, force, skip), daemon=True
        ).start()
        return {"success": True, "message": f"已启动恢复: {fn}"}

    # ── PVE status ──
    def get_pve_status(self):
        return get_pve_status(
            self.ctx._pve_host, self.ctx._ssh_port,
            self.ctx._ssh_username, self.ctx._ssh_password,
            self.ctx._ssh_key_file,
        )

    def get_container_status(self):
        qemu = get_qemu_status(
            self.ctx._pve_host, self.ctx._ssh_port,
            self.ctx._ssh_username, self.ctx._ssh_password,
            self.ctx._ssh_key_file,
        )
        lxc = get_container_status(
            self.ctx._pve_host, self.ctx._ssh_port,
            self.ctx._ssh_username, self.ctx._ssh_password,
            self.ctx._ssh_key_file,
        )
        return qemu + lxc

    # ── VM actions ──
    def container_action(self, data: dict):
        vmid = str(data.get("vmid", "")).strip()
        action = str(data.get("action", "")).strip()
        vmtype = str(data.get("type", "")).strip().lower()
        if not all([vmid, action, vmtype]):
            return {"success": False, "message": "缺少参数"}
        if action not in ("start", "stop", "reboot"):
            return {"success": False, "message": "不支持的操作"}
        if vmtype not in ("qemu", "lxc"):
            return {"success": False, "message": "类型必须为qemu或lxc"}
        pref = "qm" if vmtype == "qemu" else "pct"
        try:
            ssh = _ssh_connect(self.ctx)
            _, stdout, stderr = ssh.exec_command(f"{pref} {action} {vmid}")
            rc = stdout.channel.recv_exit_status()
            ssh.close()
            if rc == 0:
                return {"success": True, "message": f"{vmtype.upper()} {vmid} {action} 成功"}
            return {"success": False, "message": stderr.read().decode().strip() or "未知错误"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def container_snapshot(self, data: dict):
        vmid = str(data.get("vmid", "")).strip()
        vmtype = str(data.get("type", "")).strip().lower()
        snapname = str(data.get("name", "")).strip() or f"auto-{int(time.time())}"
        if not vmid or vmtype not in ("qemu", "lxc"):
            return {"success": False, "message": "参数错误"}
        pref = "qm" if vmtype == "qemu" else "pct"
        try:
            ssh = _ssh_connect(self.ctx)
            _, stdout, stderr = ssh.exec_command(f"{pref} snapshot {vmid} {snapname}")
            rc = stdout.channel.recv_exit_status()
            ssh.close()
            if rc == 0:
                return {"success": True, "message": f"快照创建成功: {snapname}"}
            return {"success": False, "message": stderr.read().decode().strip() or "未知错误"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def host_action(self, data: dict):
        action = data.get("action", "")
        if action not in ("reboot", "shutdown"):
            return {"success": False, "message": "action必须为reboot或shutdown"}
        try:
            ssh = _ssh_connect(self.ctx)
            ssh.exec_command("reboot" if action == "reboot" else "poweroff")
            ssh.close()
            return {"success": True, "message": f"主机{action}命令已发送"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ── Cleanup ──
    def cleanup_tmp(self):
        c, err = clean_pve_tmp_files(
            self.ctx._pve_host, self.ctx._ssh_port,
            self.ctx._ssh_username, self.ctx._ssh_password,
            self.ctx._ssh_key_file,
        )
        if err:
            return {"success": False, "message": f"清理失败: {err}"}
        return {"success": True, "message": f"已清理 {c} 个临时文件"}

    def cleanup_logs(self):
        if not getattr(self.ctx, "_enable_log_cleanup", False):
            return {"success": False, "message": "未启用日志清理"}
        res = clean_pve_logs(
            self.ctx._pve_host, self.ctx._ssh_port,
            self.ctx._ssh_username, self.ctx._ssh_password,
            self.ctx._ssh_key_file,
            journal_days=getattr(self.ctx, "_log_journal_days", 0),
            log_dirs={
                "/var/log/vzdump": getattr(self.ctx, "_log_vzdump_keep", 0),
                "/var/log/pve": getattr(self.ctx, "_log_pve_keep", 0),
                "/var/log/dpkg.log": getattr(self.ctx, "_log_dpkg_keep", 0),
            },
        )
        return {"success": True, "message": "日志清理完成", "result": res}

    def template_images(self):
        try:
            return list_template_images(
                self.ctx._pve_host, self.ctx._ssh_port,
                self.ctx._ssh_username, self.ctx._ssh_password,
                self.ctx._ssh_key_file,
            )
        except Exception:
            return []

    def stop_all_tasks(self):
        stopped = []
        for lock_name in ("_lock", "_restore_lock", "_global_task_lock"):
            lock = getattr(self.ctx, lock_name, None)
            if lock and lock.locked():
                try:
                    lock.release()
                    stopped.append(lock_name)
                except RuntimeError:
                    pass
        self.ctx._running = False
        self.ctx._backup_activity = "空闲"
        self.ctx._restore_activity = "空闲"
        msg = f"已停止: {', '.join(stopped)}" if stopped else "无运行中的任务"
        return {"success": True, "message": msg}

    def download_backup(self, filename: str, source: str = "本地备份"):
        if source == "本地备份":
            fp = self._find_local_file(filename)
            if fp:
                return fp
            return None
        elif source == "WebDAV备份":
            tmp = Path(tempfile.gettempdir()) / "pve_backup_download"
            tmp.mkdir(parents=True, exist_ok=True)
            dest = str(tmp / filename)
            ok, err = self.ctx.backup_manager.download_from_webdav(filename, dest)
            if ok:
                return dest
            return None
        return None

    def test_notification(self):
        from ..notification.notifications import CHANNEL_DESCRIPTIONS
        channels = getattr(self.ctx, "_notify_channels", {}) or {}
        results = {}
        for ch_name, ch_conf in channels.items():
            if not ch_conf.get("enabled"):
                results[ch_name] = {"sent": False, "error": "未启用"}
                continue
            desc = CHANNEL_DESCRIPTIONS.get(ch_name, {})
            label = desc.get("label", ch_name)
            title = f"{label} 测试通知"
            text = f"这是一条来自 PVE Backup 的测试通知\n如果你收到这条消息，说明通知配置正确 ✅\n\n⏱️ {__import__('time').time()}"
            try:
                method = getattr(self.ctx.notification_handler, f"_send_{ch_name}", None)
                if method:
                    method(title, text, ch_conf)
                    results[ch_name] = {"sent": True, "error": None}
                else:
                    results[ch_name] = {"sent": False, "error": "未知渠道"}
            except Exception as e:
                results[ch_name] = {"sent": False, "error": str(e)}
        return {"success": True, "results": results}

    def get_token(self):
        return {"api_token": os.environ.get("PVE_API_TOKEN", "pve-backup-token")}
