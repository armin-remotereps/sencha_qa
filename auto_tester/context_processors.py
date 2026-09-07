from __future__ import annotations

from django.http import HttpRequest


def branding(request: HttpRequest) -> dict[str, str]:
    """Expose the product's name and tagline to every template."""
    return {
        "brand_name": "Punk Hazard",
        "brand_tagline": "AI testing on real applications.",
    }
