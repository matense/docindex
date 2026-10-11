def test_releases_page_renders(auth_client):
    resp = auth_client.get("/settings/releases")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "What's new" in html
    # Every release from app/data/releases.json is rendered, newest first
    assert ">v0.1.2<" in html
    assert html.index(">v0.1.2<") < html.index(">v0.1.1<") < html.index(">v0.1<")
    # The Automation early-stage disclaimer ships with v0.1.2
    assert "very early stage" in html


def test_releases_page_requires_login(client):
    resp = client.get("/settings/releases")
    assert resp.status_code in (302, 401)


def test_releases_link_in_profile_dropdown(auth_client):
    resp = auth_client.get("/settings/profile")
    assert b"/settings/releases" in resp.data


def test_app_version_matches_latest_release(auth_client):
    from app import APP_VERSION
    resp = auth_client.get("/settings/releases")
    assert f"v{APP_VERSION}".encode() in resp.data
