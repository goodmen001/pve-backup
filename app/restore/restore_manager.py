import os
import re
import time
import logging
from pathlib import Path
from typing import Tuple, Optional

import paramiko

from ..pve.client import clean_pve_tmp_files
from ..config import BACKUP_DIR, DATA_DIR

logger = logging.getLogger("pve-backup")


class RestoreManager:
    def __init__(self, app_ctx):
        self.ctx = app_ctx

    def perform_restore_once(
        self, filename: str, source: str,
        restore_vmid: str = "", restore_force: bool = False,
        restore_skip_existing: bool = True
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        host = getattr(self.ctx, "_pve_host", "")
        port = getattr(self.ctx, "_ssh_port", 22)
        user = getattr(self.ctx, "_ssh_username", "root")
        pwd = getattr(self.ctx, "_ssh_password", "")
        key = getattr(self.ctx, "_ssh_key_file", "")

        if not host:
            return False, "未配置PVE主机", None

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        sftp = None

        try:
            if key:
                pk = paramiko.RSAKey.from_private_key_file(key)
                ssh.connect(host, port=port, username=user, pkey=pk)
            else:
                ssh.connect(host, port=port, username=user, password=pwd)

            # Get backup file
            backup_path = None
            if source == "本地备份":
                for sub in ("containers", "virtualmachines"):
                    p = Path(getattr(self.ctx, "_backup_path", str(BACKUP_DIR))) / sub / filename
                    if p.exists():
                        backup_path = str(p)
                        break
                if not backup_path:
                    return False, f"本地文件不存在: {filename}", None
            elif source == "WebDAV备份":
                tmp = DATA_DIR / "temp"
                tmp.mkdir(parents=True, exist_ok=True)
                backup_path = str(tmp / filename)
                ok, err = self.ctx.backup_manager.download_from_webdav(filename, backup_path)
                if not ok:
                    return False, f"WebDAV下载失败: {err}", None
            else:
                return False, f"不支持的来源: {source}", None

            # Upload to PVE
            sftp = ssh.open_sftp()
            remote = f"/tmp/{filename}"
            logger.info(f"上传 {backup_path} -> {remote}")
            sftp.put(backup_path, remote)

            original_vmid = self._extract_vmid(filename)
            target = restore_vmid or original_vmid or ""
            if not target:
                return False, "无法提取VMID，请手动指定", None

            exists = self._vm_exists(ssh, target)
            if exists:
                if restore_skip_existing:
                    return False, f"VM {target} 已存在，跳过", target
                if not restore_force:
                    return False, f"VM {target} 已存在，启用强制覆盖", target
                is_lxc = "lxc" in filename.lower()
                ok, err = self._delete_vm(ssh, target, is_lxc)
                if not ok:
                    return False, f"删除现有VM失败: {err}", target

            is_lxc = "lxc" in filename.lower()
            if is_lxc:
                cmd = f"pct restore {target} {remote}"
            else:
                cmd = f"qmrestore {remote} {target}"
            storage = getattr(self.ctx, "_restore_storage", "local")
            if storage:
                cmd += f" --storage {storage}"

            logger.info(f"执行恢复: {cmd}")
            stdin, stdout, stderr = ssh.exec_command(cmd)
            while stdout.readline():
                pass
            rc = stdout.channel.recv_exit_status()
            if rc != 0:
                err = stderr.read().decode().strip()
                return False, f"恢复失败: {err}", target

            # Cleanup remote temp
            try:
                sftp.remove(remote)
            except Exception:
                pass
            if source == "WebDAV备份":
                try:
                    os.remove(backup_path)
                except Exception:
                    pass

            return True, None, target

        except Exception as e:
            return False, f"恢复异常: {e}", None
        finally:
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    pass
            ssh.close()
            if getattr(self.ctx, "_auto_cleanup_tmp", False):
                try:
                    clean_pve_tmp_files(host, port, user, pwd, key)
                except Exception:
                    pass

    def _extract_vmid(self, filename: str) -> Optional[str]:
        m = re.search(r"vzdump-(?:qemu|lxc)-(\d+)-", filename)
        return m.group(1) if m else None

    def _vm_exists(self, ssh: paramiko.SSHClient, vmid: str) -> bool:
        for c in (f"qm list | grep -q '^{vmid}\\s'", f"pct list | grep -q '^{vmid}\\s'"):
            _, stdout, _ = ssh.exec_command(c)
            if stdout.channel.recv_exit_status() == 0:
                return True
        return False

    def _delete_vm(self, ssh: paramiko.SSHClient, vmid: str, is_lxc: bool) -> Tuple[bool, Optional[str]]:
        pref = "pct" if is_lxc else "qm"
        ssh.exec_command(f"{pref} stop {vmid}")
        time.sleep(5)
        _, stdout, stderr = ssh.exec_command(f"{pref} destroy {vmid}")
        rc = stdout.channel.recv_exit_status()
        if rc != 0:
            return False, stderr.read().decode().strip()
        return True, None
