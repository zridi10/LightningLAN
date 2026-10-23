"""The web UI: static assets, security headers, and the UI's API flow end to end on two peers."""

import hashlib
import re
from pathlib import Path

import pytest

from lightninglan.server import STATIC_DIR

from .harness import common_files, start_together, wait_download, wait_for

APP_JS = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
PHONE_JS = (STATIC_DIR / "phone.js").read_text(encoding="utf-8")
PHONE_HTML = (STATIC_DIR / "phone.html").read_text(encoding="utf-8")
PAGES = [pytest.param(APP_JS, INDEX_HTML, id="computer"), pytest.param(PHONE_JS, PHONE_HTML, id="phone")]


# ----- static checks on the pages themselves (the computer's, and the phone's) ----------------

@pytest.mark.parametrize("script, page", PAGES)
def test_every_element_the_script_uses_exists_in_the_page(script, page):
    used = set(re.findall(r"\$\('([\w-]+)'\)", script))
    defined = set(re.findall(r'id="([\w-]+)"', page))
    assert used, "expected $('id') lookups in the script"
    assert used <= defined, f"missing ids in the page: {sorted(used - defined)}"


@pytest.mark.parametrize("script, page", PAGES)
@pytest.mark.parametrize("pattern", [r"\.innerHTML\b", r"\.outerHTML\b", r"insertAdjacentHTML", r"document\.write",
                                     r"\beval\(", r"new Function\("])
def test_script_never_builds_html_from_strings(pattern, script, page):
    assert not re.search(pattern, script), f"the script uses {pattern}; file names must only go in via textContent"


@pytest.mark.parametrize("script, page", PAGES)
def test_page_has_no_inline_script_or_handlers(script, page):
    # Required for the strict Content-Security-Policy to hold.
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", page)
    assert not re.search(r"\son[a-z]+\s*=", page)
    assert "style=" not in page


def test_the_computer_page_has_the_phone_dialog():
    for element_id in ["phone-open", "phone-dialog", "phone-qr", "phone-url", "phone-new-key"]:
        assert f'id="{element_id}"' in INDEX_HTML


def test_page_contains_the_required_parts():
    for element_id in ["self-name", "peer-count", "drop-zone", "file-input", "files", "file-rows", "filter",
                       "downloads", "peers", "theme-toggle", "toasts"]:
        assert f'id="{element_id}"' in INDEX_HTML
    for column in ["name", "size", "owners", "mtime"]:
        assert f'data-sort="{column}"' in INDEX_HTML


CSS = (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def test_every_theme_in_the_cycle_has_styles_and_its_scene_exists():
    themes = re.search(r"var THEMES = \[([^\]]*)\]", APP_JS).group(1)
    names = re.findall(r"'([\w-]+)'", themes)
    assert names[:3] == ["auto", "light", "dark"] and len(names) == 6
    for theme in names[2:]:  # light is the default colours
        assert f':root[data-theme="{theme}"]' in CSS, f"no colours for the {theme} theme"
        assert f"{theme}:" in APP_JS.split("THEME_NAMES", 1)[1].split("};", 1)[0], f"no display name for {theme}"
    for theme in names[3:]:
        assert f'class="scene scene-{theme}"' in INDEX_HTML
        assert f".scene-{theme} {{" in CSS


def test_theme_artwork_exists_and_is_safe_as_an_image():
    import xml.etree.ElementTree as ET

    referenced = set(re.findall(r"url\((themes/[^)]+)\)", CSS))
    assert len(referenced) >= 10
    for rel in referenced:
        path = STATIC_DIR / rel
        assert path.is_file(), f"{rel} is missing"
        ET.parse(path)
        text = path.read_text(encoding="utf-8")
        # The strict Content-Security-Policy applies to these too: attributes only, no styles or scripts.
        assert "<style" not in text and "style=" not in text and "<script" not in text, rel


def test_page_has_the_name_editor_and_thunder_cloud():
    for element_id in ["name-edit", "name-form", "name-input", "name-dice", "name-save", "name-cancel"]:
        assert f'id="{element_id}"' in INDEX_HTML
    assert "ThunderCLOUD" in INDEX_HTML and "Common Space" not in INDEX_HTML


def test_random_name_route_suggests_a_different_funny_name(peer):
    from lightninglan.names import FUNNY_NAMES

    peer.settings.name = FUNNY_NAMES[0]
    names = {peer.get("/api/random-name").json()["name"] for _ in range(20)}
    assert names <= set(FUNNY_NAMES) and FUNNY_NAMES[0] not in names
    from .conftest import LAN_CLIENT

    assert peer.get("/api/random-name", client=LAN_CLIENT).status_code == 403  # the local UI only


def test_styles_support_light_and_dark():
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in css
    assert ':root[data-theme="dark"]' in css and ':root:not([data-theme="light"])' in css


# ----- served by the app ----------------------------------------------------------------------

def test_index_and_assets_are_served_with_security_headers(peer):
    r = peer.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-cache"
    for asset, ctype in [("app.js", "javascript"), ("style.css", "text/css"), ("favicon.svg", "image/svg+xml")]:
        a = peer.get(f"/static/{asset}")
        assert a.status_code == 200, asset
        assert ctype in a.headers["content-type"]
        assert a.headers["cache-control"] == "no-cache"


def test_static_route_does_not_escape_its_folder(peer):
    for path in ["/static/../server.py", "/static/..%2fserver.py", "/static/%2e%2e/config.py"]:
        r = peer.get(path)
        assert r.status_code == 404, path


def test_open_endpoint_validates_its_input(peer):
    ui = {"X-LightningLAN-Client": "ui"}
    assert peer.post("/api/open", json={"target": "C:\\Windows"}, headers=ui).status_code == 422
    assert peer.post("/api/open", json={"target": "download", "download_id": "nope"}, headers=ui).status_code == 404


def test_unknown_downloads_are_404(peer):
    ui = {"X-LightningLAN-Client": "ui"}
    assert peer.request("DELETE", "/api/downloads/nope", headers=ui).status_code == 404
    assert peer.post("/api/downloads/nope/resume", headers=ui).status_code == 404
    assert peer.post("/api/downloads", json={"file_id": "x"}, headers=ui).status_code == 400


def test_api_responses_carry_hardening_headers_even_when_rejected(peer):
    from .conftest import LAN_CLIENT

    r = peer.get("/api/status", client=LAN_CLIENT)
    assert r.status_code == 403
    assert r.headers["x-content-type-options"] == "nosniff"


# ----- two real peers, driven through the same endpoints the page uses --------------------------

@pytest.mark.integration
def test_ui_flow_end_to_end_on_two_peers(spawn):
    a, b = spawn("Alice PC", start=False), spawn("Bob PC", start=False)
    (b.share / "holiday.jpg").write_bytes(b"\xff\xd8 pretend jpeg" * 1000)
    start_together(a, b)

    # Page loads on both peers.
    for p in (a, b):
        page = p.get("/")
        assert page.status_code == 200 and 'id="drop-zone"' in page.text
        assert p.get("/static/app.js").status_code == 200

    # Header: own name and peer count.
    status = a.get("/api/status").json()
    assert status["name"] == "Alice PC"
    wait_for(lambda: a.get("/api/status").json()["peers_online"] == 1, timeout=30, message="A to count B")
    peers = a.get("/api/peers").json()
    assert [p["name"] for p in peers["peers"]] == ["Bob PC"]
    assert peers["peers"][0]["files"] == 1

    # Drop zone: Alice adds a file (raw body upload, as the page's XHR does).
    payload = b"meeting notes " * 2000
    up = a.post("/api/upload", params={"name": "notes.txt", "mtime": 1_700_000_000_000}, content=payload,
                headers={"Content-Type": "application/octet-stream", "Origin": f"http://localhost:{a.port}"})
    assert up.status_code == 201, up.text
    assert up.json() == {"name": "notes.txt", "size": len(payload), "renamed": False}
    again = a.post("/api/upload", params={"name": "notes.txt"}, content=b"second")
    assert again.json()["name"] == "notes (1).txt"

    # ThunderCLOUD on Bob shows Alice's files; Alice sees Bob's photo; ETag polling works.
    files_b = wait_for(lambda: (lambda f: f if {"notes.txt", "notes (1).txt"} <= set(f) else None)(common_files(b)),
                       timeout=30, message="Bob to see Alice's uploads")
    assert files_b["notes.txt"]["sources"][0]["name"] == "Alice PC"
    assert files_b["notes.txt"]["local"] is False
    assert files_b["holiday.jpg"]["local"] is True
    first = b.get("/api/common")
    assert b.get("/api/common", headers={"If-None-Match": first.headers["etag"]}).status_code == 304
    files_a = wait_for(lambda: common_files(a).get("holiday.jpg"), timeout=30, message="Alice to see the photo")
    assert files_a["sources"][0]["name"] == "Bob PC"

    # Download button on Bob, then the downloads panel.
    started = b.post("/api/downloads", json={"file_id": files_b["notes.txt"]["id"]})
    assert started.status_code == 200, started.text
    d = wait_download(b, started.json()["id"], timeout=60)
    assert d["status"] == "completed", d
    assert d["progress"] == 1.0 and d["received"] == len(payload)
    assert d["source"]["name"] == "Alice PC"
    saved = Path(d["path"])
    assert saved.parent == b.downloads and saved.read_bytes() == payload
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == files_b["notes.txt"]["id"]

    # Starting the same download again while one is listed creates a new entry with a new name.
    second = b.post("/api/downloads", json={"file_id": files_b["notes.txt"]["id"]}).json()
    assert wait_download(b, second["id"])["path"].endswith("notes (1).txt")

    # Downloads panel controls: pause/resume on a finished item are harmless; remove and clear work.
    assert b.post(f"/api/downloads/{d['id']}/pause").json()["status"] == "completed"
    assert b.post(f"/api/downloads/{d['id']}/bogus").status_code == 404
    assert b.delete(f"/api/downloads/{d['id']}").status_code == 200
    assert all(x["id"] != d["id"] for x in b.get("/api/downloads").json()["downloads"])
    b.post("/api/downloads/clear")
    assert b.get("/api/downloads").json()["downloads"] == []

    # A file Bob already has is offered as "in your share", not as a download.
    assert common_files(b)["holiday.jpg"]["local"] is True

    # "Quit completely" button (shown while stopped), like the tray's Quit.
    assert a.post("/api/shutdown").json() == {"ok": True}
    a.proc.wait(timeout=15)
    assert a.proc.returncode == 0
    wait_for(lambda: a.peer_id not in b.online_peer_ids(), timeout=15, message="Bob to see Alice leave")
