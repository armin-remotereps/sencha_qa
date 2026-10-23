import logging

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class VisualEngineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "visual_engine"

    def ready(self) -> None:
        if not settings.VISION_WORKER_TOKEN:
            logger.warning(
                "VISION_WORKER_TOKEN is empty: every vision worker handshake "
                "will be rejected and no test run can start."
            )
