import threading
import time
import logging

logger = logging.getLogger("pve-backup")


class RestoreExecutor:
    def __init__(self, app_ctx):
        self.ctx = app_ctx

    def run_restore_job(
        self, filename: str, source: str = "本地备份",
        restore_vmid: str = "", restore_force: bool = False,
        restore_skip_existing: bool = True
    ):
        if not getattr(self.ctx, "_enable_restore", False):
            logger.error("恢复功能未启用")
            return
        if not self.ctx._restore_lock:
            self.ctx._restore_lock = threading.Lock()
        if not self.ctx._global_task_lock:
            self.ctx._global_task_lock = threading.Lock()
        if not self.ctx._global_task_lock.acquire(blocking=False):
            logger.debug("其他任务执行中，恢复跳过")
            return
        if not self.ctx._restore_lock.acquire(blocking=False):
            logger.debug("已有恢复任务执行中，跳过")
            self.ctx._global_task_lock.release()
            return

        entry = {"timestamp": time.time(), "success": False, "filename": filename, "target_vmid": restore_vmid or "自动", "message": "开始"}
        self.ctx._restore_activity = "开始"

        try:
            ok, err, target = self.ctx.restore_manager.perform_restore_once(
                filename, source, restore_vmid, restore_force, restore_skip_existing
            )
            entry["success"] = ok
            entry["target_vmid"] = target or restore_vmid or "自动"
            entry["message"] = "成功" if ok else f"失败: {err}"
            self.ctx.notification_handler.send_restore_notification(
                success=ok, message=entry["message"], filename=filename, target_vmid=target
            )
        except Exception as e:
            logger.error(f"恢复异常: {e}")
            entry["message"] = f"异常: {e}"
        finally:
            self.ctx._restore_activity = "空闲"
            self.ctx.history_handler.save_restore_history_entry(entry)
            for lock in (self.ctx._restore_lock, self.ctx._global_task_lock):
                if lock and lock.locked():
                    try:
                        lock.release()
                    except RuntimeError:
                        pass
