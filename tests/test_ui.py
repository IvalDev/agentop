"""Development web console: served at ``/ui`` only in development environments.

The console is static HTML + JavaScript over the public API. Besides the
mount itself, these tests guard its security rules on the shipped files: the
token never reaches web storage or cookies, server text is never parsed as
HTML, and the page loads nothing external or inline.
"""

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app

STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "ui" / "static"


async def _get(monkeypatch: pytest.MonkeyPatch, environment: str, path: str):
    monkeypatch.setenv("AA_ENVIRONMENT", environment)
    monkeypatch.setenv("AA_JWT_SECRET", "test-secret-0123456789-abcdefghijklmnop")
    monkeypatch.setenv("AA_CREDENTIALS_KEY", "cqsp933navSzGBmyUEnO6EKloifkQMdohIhgjgTQ3iE=")
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.mark.parametrize("environment", ["dev", "development", "local", "test"])
async def test_console_page_is_served_in_development(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    response = await _get(monkeypatch, environment, "/ui/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<script src="app.js"></script>' in response.text


async def test_console_script_is_served_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    response = await _get(monkeypatch, "dev", "/ui/app.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


@pytest.mark.parametrize("environment", ["production", "staging", "PRODUCTION", "Dev"])
@pytest.mark.parametrize("path", ["/ui/", "/ui/app.js"])
async def test_console_is_absent_outside_development(
    monkeypatch: pytest.MonkeyPatch, environment: str, path: str
) -> None:
    response = await _get(monkeypatch, environment, path)
    assert response.status_code == 404


def test_console_script_keeps_the_token_in_memory_and_renders_text_only() -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
    ):
        assert forbidden not in script, forbidden


def test_console_page_has_no_inline_handlers_scripts_or_external_resources() -> None:
    page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", page, re.IGNORECASE)
    assert re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.DOTALL) == [""]
    assert not re.search(r"(src|href)\s*=\s*\"(https?:)?//", page, re.IGNORECASE)
