import os
import re
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

import paramiko

from ..config import BACKUP_DIR

logger = logging.getLogger("pve-backup")


class BackupManager:
    def __init__(self, app_ctx):
        self.ctx = app_ctx

    def _get_backup_path(self):
        return Path(getattr(self.ctx, "_backup_path", str(BACKUP_DIR)))

    def cleanup_old_backups(self):
        backup_path = self._get_backup_path()
        keep = getattr(self.ctx, "_keep_backup_num", 5)
        if keep <= 0:
            return
        for sub in ["containers", "virtualmachines"]:
            d = backup_path / sub
            if not d.is_dir():
                continue
            files = []
            for f in d.iterdir():
                if f.is_file() and f.name.endswith((".tar.gz", ".tar.lzo", ".tar.zst", ".vma.gz", ".vma.lzo", ".vma.zst")):
                    mt = f.stat().st_mtime
                    files.append((mt, f))
            files.sort(key=lambda x: x[0], reverse=True)
            if len(files) > keep:
                for _, f in files[keep:]:
                    try:
                        f.unlink()
                        logger.info(f"已删除旧备份: {f.name}")
                    except Exception as e:
                        logger.error(f"删除旧备份 {f.name} 失败: {e}")

    def upload_to_webdav(self, local_file_path: str, filename: str) -> Tuple[bool, Optional[str]]:
        if not getattr(self.ctx, "_enable_webdav", False) or not getattr(self.ctx, "_webdav_url", ""):
            return False, "WebDAV未启用"
        try:
            from ..storage.webdav import WebDAVClient
            client = WebDAVClient(
                url=self.ctx._webdav_url,
                username=self.ctx._webdav_username,
                password=self.ctx._webdav_password,
                path=self.ctx._webdav_path,
                skip_dir_check=True,
                logger=logger,
                plugin_name="PVE-Backup",
            )
            success, error = client.upload(local_file_path, filename)
            client.close()
            return success, error
        except Exception as e:
            return False, f"WebDAV上传失败: {e}"

    def cleanup_webdav_backups(self):
        if not getattr(self.ctx, "_enable_webdav", False) or not getattr(self.ctx, "_webdav_url", ""):
            return
        keep = getattr(self.ctx, "_webdav_keep_backup_num", 7)
        if keep <= 0:
            return
        try:
            from ..storage.webdav import WebDAVClient
            client = WebDAVClient(
                url=self.ctx._webdav_url,
                username=self.ctx._webdav_username,
                password=self.ctx._webdav_password,
                path=self.ctx._webdav_path,
                skip_dir_check=True,
                logger=logger,
                plugin_name="PVE-Backup",
            )
            deleted, error = client.cleanup_old_files(keep_count=keep)
            if error:
                logger.error(f"WebDAV清理失败: {error}")
            else:
                logger.info(f"WebDAV清理完成，已删除 {deleted} 个旧文件")
            client.close()
        except Exception as e:
            logger.error(f"WebDAV清理异常: {e}")

    def get_available_backups(self) -> List[Dict[str, Any]]:
        backups = []
        backup_path = self._get_backup_path()
        if backup_path.is_dir():
            for sub in ["containers", "virtualmachines"]:
                d = backup_path / sub
                if d.is_dir():
                    for f in d.iterdir():
                        if f.is_file() and f.name.endswith((".tar.gz", ".tar.lzo", ".tar.zst", ".vma.gz", ".vma.lzo", ".vma.zst")):
                            try:
                                stat = f.stat()
                                backups.append({
                                    "filename": f.name,
                                    "path": str(f),
                                    "size_mb": stat.st_size / (1024 * 1024),
                                    "time_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                                    "source": "本地备份",
                                })
                            except Exception:
                                pass
        if getattr(self.ctx, "_enable_webdav", False) and getattr(self.ctx, "_webdav_url", ""):
            try:
                from ..storage.webdav import WebDAVClient
                client = WebDAVClient(
                    url=self.ctx._webdav_url,
                    username=self.ctx._webdav_username,
                    password=self.ctx._webdav_password,
                    path=self.ctx._webdav_path,
                    skip_dir_check=True,
                    logger=logger,
                    plugin_name="PVE-Backup",
                )
                files, error = client.list_files()
                if not error:
                    for fi in files:
                        fn = fi.get("filename", "")
                        if any(fn.lower().endswith(e) for e in (".tar.gz", ".tar.lzo", ".tar.zst", ".vma.gz", ".vma.lzo", ".vma.zst")):
                            ft = fi.get("time")
                            backups.append({
                                "filename": fn,
                                "path": fi.get("href", ""),
                                "size_mb": fi.get("size_mb", 0),
                                "time_str": datetime.fromtimestamp(ft).strftime("%Y-%m-%d %H:%M:%S") if ft else "未知",
                                "source": "WebDAV备份",
                            })
                client.close()
            except Exception:
                pass
        backups.sort(key=lambda x: x.get("time_str", ""), reverse=True)
        return backups

    def get_webdav_backups(self) -> List[Dict[str, Any]]:
        return [b for b in self.get_available_backups() if b["source"] == "WebDAV备份"]

    def download_single_backup_file(
        self, ssh: paramiko.SSHClient, sftp: paramiko.SFTPClient,
        remote_file: str, backup_filename: str
    ) -> Tuple[bool, Optional[str], Optional[str], Dict[str, Any]]:
        try:
            is_container = any(backup_filename.lower().endswith(e) for e in (".tar.gz", ".tar.lzo", ".tar.zst"))
            sub_dir = "containers" if is_container else "virtualmachines"
            details = {"local_backup": {"enabled": False, "success": False}, "webdav_backup": {"enabled": False, "success": False}}

            local_enabled = getattr(self.ctx, "_enable_local_backup", True)
            webdav_enabled = getattr(self.ctx, "_enable_webdav", False)
            webdav_url = getattr(self.ctx, "_webdav_url", "")

            local_path = None
            if local_enabled:
                backup_dir = self._get_backup_path() / sub_dir
                backup_dir.mkdir(parents=True, exist_ok=True)
                local_path = str(backup_dir / backup_filename)
                try:
                    sftp.get(remote_file, local_path)
                    details["local_backup"] = {"enabled": True, "success": True, "path": local_path, "filename": backup_filename}
                    logger.info(f"本地备份成功: {backup_filename}")
                except Exception as e:
                    logger.error(f"本地下载失败: {e}")
                    details["local_backup"]["error"] = str(e)
                    if not (webdav_enabled and webdav_url):
                        return False, f"本地下载失败: {e}", None, {}
                    tmp_dir = Path(tempfile.gettempdir()) / "pve_backup_temp"
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    local_path = str(tmp_dir / backup_filename)
                    sftp.get(remote_file, local_path)

            if webdav_enabled and webdav_url:
                if not local_path:
                    tmp_dir = Path(tempfile.gettempdir()) / "pve_backup_temp"
                    tmp_dir.mkdir(parents=True, exist_ok=True)
                    local_path = str(tmp_dir / backup_filename)
                    sftp.get(remote_file, local_path)
                success, error = self.upload_to_webdav(local_path, backup_filename)
                details["webdav_backup"] = {"enabled": True, "success": success, "filename": backup_filename, "error": error}
                if success:
                    logger.info(f"WebDAV备份成功: {backup_filename}")
                else:
                    logger.error(f"WebDAV备份失败: {error}")
                if local_path and "pve_backup_temp" in local_path:
                    try:
                        os.remove(local_path)
                    except Exception:
                        pass

            # 备份已下载到本地/WebDAV，删除PVE上的远程备份文件
            try:
                sftp.remove(remote_file)
                logger.info(f"已删除PVE远程备份: {remote_file}")
            except Exception as e:
                logger.warning(f"删除PVE远程备份失败(可能已被删除): {e}")

            return True, None, backup_filename, details
        except Exception as e:
            return False, f"下载备份文件失败: {e}", None, {}

    def download_from_webdav(self, filename: str, local_path: str) -> Tuple[bool, Optional[str]]:
        if not getattr(self.ctx, "_enable_webdav", False) or not getattr(self.ctx, "_webdav_url", ""):
            return False, "WebDAV未启用"
        try:
            from ..storage.webdav import WebDAVClient
            client = WebDAVClient(
                url=self.ctx._webdav_url,
                username=self.ctx._webdav_username,
                password=self.ctx._webdav_password,
                path=self.ctx._webdav_path,
                skip_dir_check=True,
                logger=logger,
                plugin_name="PVE-Backup",
            )
            success, error = client.download(filename, local_path)
            client.close()
            return success, error
        except Exception as e:
            return False, f"WebDAV下载失败: {e}"
