from dataclasses import asdict, dataclass


@dataclass
class SyncResult:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    unmapped: int = 0
    failed_batches: int = 0

    def record(self, result: str, count: int = 1) -> None:
        field = {"sent_mapped": "sent", "skipped": "skipped", "sent_unmapped": "unmapped"}.get(result, "failed")
        setattr(self, field, getattr(self, field) + count)

    def snapshot(self, *, error: str = "", stopped: bool = False) -> dict:
        if stopped:
            status = "stopped"
        elif error or self.failed or self.failed_batches or self.unmapped:
            status = "partial_failed" if self.sent or self.unmapped else "failed"
        else:
            status = "completed"
        label = {"completed": "同步完成", "partial_failed": "未全部成功", "failed": "同步失败", "stopped": "已中断"}[status]
        return {**asdict(self), "status": status, "label": label, "error": error}
