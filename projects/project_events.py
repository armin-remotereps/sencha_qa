"""The per-project events group name (a leaf: no imports).

``ProjectEventsConsumer`` (``ws/projects/<id>/events/``) joins it and
``projects.tasks`` sends the refined Application context to it; both import
the name from here so the tasks module never imports the consumers.
"""

from __future__ import annotations


def project_events_group(project_id: int) -> str:
    return f"project_events_{project_id}"
