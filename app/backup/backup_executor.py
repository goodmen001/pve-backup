import os
import re
import time
import threading
import logging
from pathlib import Path
from typing import Tuple, Optional, Dict, Any

import paramiko

from ..pve.client import clean_pve_tmp_files
from ..config import BACKUP_DIR

logger = logging.getLogger("pve-backup")


class BackupExecutor:
    def __init__(self, app_ctx):
        self.ctx = app_ctx

    def run_backup_job(self):
        if not self.ctx._lock:
            self.ctx._lock = threading.Lock()
        if not self.ctx._global_task_lock:
            self.ctx._global_task_lock = threading.Lock()

        if self.ctx._restore_lock and self.ctx._restore_lock.locked():
            logger.info("恢复任务正在执行，备份跳过")
            return
        if not self.ctx._global_task_lock.acquire(blocking=False):
            logger.debug("其他任务执行中，备份跳过")
            return
        if not self.ctx._lock.acquire(blocking=False):
            logger.debug("已有备份执行中，跳过")
            self.ctx._global_task_lock.release()
            return

        entry = {"timestamp": time.time(), "success": False, "filename": None, "message": "开始"}
        self.ctx._backup_activity = "开始"

        try:
            self.ctx._running = True
            logger.info("开始备份任务...")

            host = getattr(self.ctx, "_pve_host", "")
            user = getattr(self.ctx, "_ssh_username", "")
            pwd = getattr(self.ctx, "_ssh_password", "")
            key = getattr(self.ctx, "_ssh_key_file", "")
            if not host or not user or (not pwd and not key):
                err = "PVE配置不完整"
                logger.error(err)
                self.ctx.notification_handler.send_backup_notification(success=False, message=err, backup_details={})
                entry["message"] = err
                self.ctx.history_handler.save_backup_history_entry(entry)
                return

            bpath = getattr(self.ctx, "_backup_path", str(BACKUP_DIR))
            Path(bpath).mkdir(parents=True, exist_ok=True)

            for i in range(getattr(self.ctx, "_retry_count", 0) + 1):
                ok, err_msg, fname, details = self._perform_once()
                if ok:
                    self.ctx.notification_handler.send_backup_notification(
                        success=True, message="备份成功", filename=fname, backup_details=details
                    )
                    return
                else:
                    logger.warning(f"第{i+1}次备份失败: {err_msg}")
                    if i < getattr(self.ctx, "_retry_count", 0):
                        time.sleep(getattr(self.ctx, "_retry_interval", 60))

            self.ctx.notification_handler.send_backup_notification(
                success=False, message=f"备份失败: {err_msg}", backup_details={}
            )

        except Exception as e:
            logger.error(f"备份主流程异常: {e}")
        finally:
            self.ctx._running = False
            self.ctx._backup_activity = "空闲"
            for lock in (self.ctx._lock, self.ctx._global_task_lock):
                if lock and lock.locked():
                    try:
                        lock.release()
                    except RuntimeError:
                        pass

    def _perform_once(self) -> Tuple[bool, Optional[str], Optional[str], Dict[str, Any]]:
        host = getattr(self.ctx, "_pve_host", "")
        port = getattr(self.ctx, "_ssh_port", 22)
        user = getattr(self.ctx, "_ssh_username", "root")
        pwd = getattr(self.ctx, "_ssh_password", "")
        key = getattr(self.ctx, "_ssh_key_file", "")

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        sftp = None

        try:
            if key:
                pk = paramiko.RSAKey.from_private_key_file(key)
                ssh.connect(host, port=port, username=user, pkey=pk)
            else:
                ssh.connect(host, port=port, username=user, password=pwd)

            # Check running vzdump
            stdin, stdout, _ = ssh.exec_command("pgrep -x vzdump || ps -C vzdump --no-headers")
            if stdout.read().decode().strip():
                return False, "PVE端已有备份任务运行", None, {}

            # Determine VMIDs
            vmid_str = getattr(self.ctx, "_backup_vmid", "")
            if not vmid_str.strip():
                qemu_ids = self._get_vmids(ssh, "qm")
                lxc_ids = self._get_vmids(ssh, "pct")
                all_ids = sorted(set(qemu_ids + lxc_ids), key=lambda x: int(x))
                if not all_ids:
                    return False, "未找到任何VM/CT", None, {}
                vmid_list = all_ids
            else:
                vmid_list = [v.strip() for v in vmid_str.split(",") if v.strip()]

            sftp = ssh.open_sftp()
            all_ok = True
            downloaded = []
            successful_vmids = []

            for vmid in vmid_list:
                logger.info(f"备份 VMID: {vmid}")
                compress = getattr(self.ctx, "_compress_mode", "zstd")
                mode = getattr(self.ctx, "_backup_mode", "snapshot")
                storage = getattr(self.ctx, "_storage_name", "local")
                cmd = f"vzdump {vmid} --compress {compress} --mode {mode} --storage {storage}"

                stdin, stdout, stderr = ssh.exec_command(cmd)
                created = None
                while True:
                    line = stdout.readline()
                    if not line:
                        break
                    m = re.search(r"creating vzdump archive '(.+)'", line)
                    if m:
                        created = m.group(1)

                rc = stdout.channel.recv_exit_status()
                if rc != 0 or not created:
                    logger.error(f"VMID {vmid} 备份失败")
                    all_ok = False
                    continue

                ok, err, fn, det = self.ctx.backup_manager.download_single_backup_file(ssh, sftp, created, os.path.basename(created))
                if ok:
                    downloaded.append({"filename": fn, "details": det})
                    successful_vmids.append(vmid)
                    logger.info(f"VMID {vmid} 备份完成: {fn}")
                else:
                    logger.error(f"VMID {vmid} 处理失败: {err}")
                    all_ok = False

            # Cleanup
            if getattr(self.ctx, "_enable_local_backup", True):
                self.ctx.backup_manager.cleanup_old_backups()
            if getattr(self.ctx, "_enable_webdav", False) and getattr(self.ctx, "_webdav_url", ""):
                self.ctx.backup_manager.cleanup_webdav_backups()

            if downloaded:
                fnames = [d["filename"] for d in downloaded]
                if all_ok:
                    self.ctx.history_handler.save_backup_history_entry({
                        "timestamp": time.time(), "success": True,
                        "filename": ", ".join(fnames), "message": f"备份成功 [VMIDs: {', '.join(successful_vmids)}]",
                    })
                    all_fnames = ", ".join(fnames)
                    return True, None, all_fnames, {"downloaded_files": downloaded}
                else:
                    failed_vmids = [v for v in vmid_list if v not in successful_vmids]
                    self.ctx.history_handler.save_backup_history_entry({
                        "timestamp": time.time(), "success": False,
                        "filename": ", ".join(fnames),
                        "message": f"部分VMID备份失败 [成功: {', '.join(successful_vmids)}] [失败: {', '.join(failed_vmids)}]",
                    })
                    return False, f"部分失败: {', '.join(failed_vmids)}", ", ".join(fnames), {"downloaded_files": downloaded}
            else:
                self.ctx.history_handler.save_backup_history_entry({
                    "timestamp": time.time(), "success": False,
                    "filename": None, "message": "所有容器备份失败",
                })
                return False, "所有备份失败", None, {}

        except Exception as e:
            return False, f"备份异常: {e}", None, {}
        finally:
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    pass
            ssh.close()
            # auto cleanup tmp
            if getattr(self.ctx, "_auto_cleanup_tmp", False):
                try:
                    clean_pve_tmp_files(host, port, user, pwd, key)
                except Exception:
                    pass

    @staticmethod
    def _get_vmids(ssh: paramiko.SSHClient, cmd: str) -> list:
        try:
            stdin, stdout, _ = ssh.exec_command(f"{cmd} list 2>&1 | tail -n +2 | awk '{{print $1}}' | grep -E '^[0-9]+$'")
            return [x.strip() for x in stdout.read().decode().splitlines() if x.strip()]
        except Exception:
            return []
