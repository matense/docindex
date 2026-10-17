"""Tools + auth hardening coverage: persona converter parsing, POST-only logout."""

from tools.convert_agency_personas import _parse_frontmatter


def test_parse_frontmatter_valid():
    meta, body = _parse_frontmatter(
        "---\nname: Scout\ndescription: Finds things\n---\nHello body")
    assert meta == {"name": "Scout", "description": "Finds things"}
    assert body == "Hello body"


def test_parse_frontmatter_missing():
    meta, body = _parse_frontmatter("no frontmatter here")
    assert meta is None
    assert body == "no frontmatter here"


def test_parse_frontmatter_skips_list_lines():
    meta, _ = _parse_frontmatter(
        "---\nname: X\ndescription: Y\n - item\n  indented: no\n---\nbody")
    assert meta == {"name": "X", "description": "Y"}


def test_logout_requires_post(auth_client):
    assert auth_client.get("/logout").status_code == 405
    assert auth_client.post("/logout").status_code in (302, 303)
