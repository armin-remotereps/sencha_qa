from django.db import models

from vision_worker.protocol import WorkerState


class VisionWorker(models.Model):
    """One vision worker (one GPU process), identified by its configured name.

    The row is reused across reconnects. ``channel_name`` names the consumer
    that owns the live connection; a late disconnect of an older socket only
    releases the row if it still owns it, as with ``Project.agent_channel_name``.
    """

    name = models.CharField(max_length=100, unique=True)
    connected = models.BooleanField(default=False, db_index=True)
    channel_name = models.CharField(max_length=255, blank=True, default="")
    # Empty until the worker's first status message.
    state = models.CharField(
        max_length=16,
        choices=[(state.value, state.value) for state in WorkerState],
        blank=True,
        default="",
    )
    status_message = models.CharField(max_length=500, blank=True, default="")
    device = models.CharField(max_length=64, blank=True, default="")
    client_version = models.CharField(max_length=32, blank=True, default="")
    capabilities = models.JSONField(default=list, blank=True)
    system_info = models.JSONField(default=dict, blank=True)
    load_seconds = models.FloatField(null=True, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    # Bumped on every status/heartbeat; a worker silent for
    # VISION_WORKER_STALE_SECONDS is treated as gone (its consumer died).
    last_seen_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_disconnected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return self.name
