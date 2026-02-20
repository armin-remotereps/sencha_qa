from __future__ import annotations

from agents.services.dmr_client import send_chat_completion
from agents.services.dmr_config import build_refiner_config
from agents.types import ChatMessage

_REFINER_SYSTEM_PROMPT = (
    "You are a technical editor. The user will provide messy notes about a software project "
    "that an AI QA agent needs to test. Restructure these notes into a clear, concise "
    "project brief using these sections (omit empty sections):\n\n"
    "## Application\n"
    "URL, type of application, tech stack if mentioned.\n\n"
    "## Credentials\n"
    "Login credentials, API keys, tokens.\n\n"
    "## Environment\n"
    "Installed software, OS details, dependencies, file paths.\n\n"
    "## Navigation\n"
    "How to reach key areas of the application, menu structure.\n\n"
    "## Known Issues\n"
    "Quirks, workarounds, things that take time, flaky areas.\n\n"
    "Rules:\n"
    "- Preserve ALL information, especially credentials and URLs exactly as written\n"
    "- Do not add information that wasn't in the original notes\n"
    "- Keep it concise — no filler prose\n"
    "- Use bullet points within sections\n"
    "- Output only the restructured text, no preamble"
)


def refine_project_prompt(raw_prompt: str) -> str:
    config = build_refiner_config()
    messages = (
        ChatMessage(role="system", content=_REFINER_SYSTEM_PROMPT),
        ChatMessage(role="user", content=raw_prompt),
    )
    response = send_chat_completion(config, messages, tools=())
    content = response.message.content
    if not isinstance(content, str) or not content:
        raise ValueError(
            f"Refiner model returned unexpected content type: {type(content)}"
        )
    return content
