"""The built web app (``web/dist``) is served at ``/`` with a client-side route fallback.

Every test builds a throwaway dist directory so the suite never depends on
``npm run build``. The fallback must never shadow the API, the dev console,
health, or the API docs, and must not turn missing files into HTML.
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response

from app.main import create_app

INDEX_HTML = '<!doctype html><html lang="es-ES"><body><div id="root"></div></body></html>'
ASSET_JS = "console.log('built');"


@pytest.fixture
def web_dist(tmp_path: Path) -> Path:
    """A minimal Vite build output: index.html, one hashed asset, one public file."""
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text(ASSET_JS, encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the dist", encoding="utf-8")
    return dist


async def _request(
    monkeypatch: pytest.MonkeyPatch,
    dist: Path,
    method: str,
    path: str,
    environment: str = "dev",
) -> Response:
    monkeypatch.setenv("AA_ENVIRONMENT", environment)
    monkeypatch.setenv("AA_JWT_SECRET", "test-secret-0123456789-abcdefghijklmnop")
    monkeypatch.setenv("AA_WEB_DIST_DIR", str(dist))
    application = create_app()
    application.state.db = None
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path)


def _is_index(response: Response) -> bool:
    return response.status_code == 200 and response.text == INDEX_HTML


@pytest.mark.parametrize("path", ["/", "/chat", "/conocimiento", "/configuracion/equipo", "/login"])
async def test_client_routes_fall_back_to_the_index(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path, path: str
) -> None:
    response = await _request(monkeypatch, web_dist, "GET", path)
    assert _is_index(response)
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"


async def test_head_is_answered_like_get(monkeypatch: pytest.MonkeyPatch, web_dist: Path) -> None:
    response = await _request(monkeypatch, web_dist, "HEAD", "/chat")
    assert response.status_code == 200


async def test_built_assets_are_served_with_a_long_cache(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path
) -> None:
    response = await _request(monkeypatch, web_dist, "GET", "/assets/index-abc123.js")
    assert response.status_code == 200
    assert response.text == ASSET_JS
    assert "javascript" in response.headers["content-type"]
    assert "immutable" in response.headers["cache-control"]


async def test_public_files_are_served(monkeypatch: pytest.MonkeyPatch, web_dist: Path) -> None:
    response = await _request(monkeypatch, web_dist, "GET", "/favicon.svg")
    assert response.status_code == 200
    assert response.text == "<svg/>"


@pytest.mark.parametrize("path", ["/assets/missing.js", "/assets/no-extension", "/missing.png"])
async def test_missing_files_are_404_not_the_index(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path, path: str
) -> None:
    response = await _request(monkeypatch, web_dist, "GET", path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("path", ["/..%2fsecret.txt", "/assets/..%2f..%2fsecret.txt"])
async def test_paths_cannot_escape_the_dist_directory(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path, path: str
) -> None:
    response = await _request(monkeypatch, web_dist, "GET", path)
    assert "outside the dist" not in response.text
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/%00", "/a%00b", "/assets/%00.js", "/chat/%00"])
async def test_paths_the_filesystem_rejects_are_404(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path, path: str
) -> None:
    response = await _request(monkeypatch, web_dist, "GET", path)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def _csp_directives(response: Response) -> dict[str, list[str]]:
    policy = response.headers["content-security-policy"]
    directives = [part.split() for part in policy.split(";") if part.strip()]
    return {name: sources for name, *sources in directives}


@pytest.mark.parametrize("path", ["/", "/chat", "/assets/index-abc123.js", "/missing.png", "/%00"])
async def test_web_app_responses_carry_security_headers(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path, path: str
) -> None:
    response = await _request(monkeypatch, web_dist, "GET", path)
    directives = _csp_directives(response)
    assert directives["default-src"] == ["'self'"]
    assert directives["script-src"] == ["'self'"]
    assert directives["style-src"] == ["'self'"]
    assert directives["object-src"] == ["'none'"]
    assert directives["base-uri"] == ["'self'"]
    assert directives["frame-ancestors"] == ["'none'"]
    policy = response.headers["content-security-policy"]
    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", ["/api/v1/auth/me", "/healthz", "/ui/", "/docs"])
async def test_security_headers_are_only_for_the_web_app(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path, path: str
) -> None:
    # The dev console and the Swagger UI keep their own (inline-script) pages working.
    response = await _request(monkeypatch, web_dist, "GET", path)
    assert "content-security-policy" not in response.headers
    assert "x-frame-options" not in response.headers


async def test_api_errors_stay_json(monkeypatch: pytest.MonkeyPatch, web_dist: Path) -> None:
    response = await _request(monkeypatch, web_dist, "GET", "/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json() == {"detail": "not authenticated"}


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_unknown_api_paths_are_still_404(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path, method: str
) -> None:
    response = await _request(monkeypatch, web_dist, method, "/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


async def test_api_trailing_slash_redirect_is_kept(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path
) -> None:
    response = await _request(monkeypatch, web_dist, "GET", "/api/v1/documents")
    assert response.status_code == 307
    assert response.headers["location"].endswith("/api/v1/documents/")


async def test_non_get_requests_to_client_routes_are_404(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path
) -> None:
    response = await _request(monkeypatch, web_dist, "POST", "/chat")
    assert response.status_code == 404


async def test_health_and_api_docs_are_not_shadowed(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path
) -> None:
    health = await _request(monkeypatch, web_dist, "GET", "/healthz")
    assert health.json()["status"] == "ok"
    docs = await _request(monkeypatch, web_dist, "GET", "/docs")
    assert docs.status_code == 200
    assert "swagger" in docs.text.lower()
    schema = await _request(monkeypatch, web_dist, "GET", "/openapi.json")
    assert "/api/v1/auth/login" in schema.json()["paths"]
    assert not any("{path}" in route for route in schema.json()["paths"])


async def test_dev_console_is_not_shadowed(monkeypatch: pytest.MonkeyPatch, web_dist: Path) -> None:
    console = await _request(monkeypatch, web_dist, "GET", "/ui/")
    assert console.status_code == 200
    assert '<script src="app.js"></script>' in console.text
    redirect = await _request(monkeypatch, web_dist, "GET", "/ui")
    assert redirect.status_code == 307
    assert redirect.headers["location"].endswith("/ui/")


@pytest.mark.parametrize("path", ["/ui", "/ui/", "/ui/app.js"])
async def test_console_paths_stay_404_outside_development(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path, path: str
) -> None:
    response = await _request(monkeypatch, web_dist, "GET", path, environment="production")
    assert response.status_code == 404
    assert not _is_index(response)


async def test_the_web_app_is_also_served_in_production(
    monkeypatch: pytest.MonkeyPatch, web_dist: Path
) -> None:
    response = await _request(monkeypatch, web_dist, "GET", "/chat", environment="production")
    assert _is_index(response)


@pytest.mark.parametrize("make_dir", [False, True])
async def test_nothing_is_mounted_without_a_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, make_dir: bool
) -> None:
    dist = tmp_path / "dist"
    if make_dir:  # an empty or half-written build without index.html
        dist.mkdir()
    for path in ("/", "/chat"):
        response = await _request(monkeypatch, dist, "GET", path)
        assert response.status_code == 404
