def test_help_page_renders(auth_client):
    resp = auth_client.get("/settings/help")
    assert resp.status_code == 200
    html = resp.data.decode()
    # Header + a few key sections
    assert "DocIndex Help" in html
    assert 'id="help-drives"' in html
    assert 'id="help-ai-chat"' in html
    assert 'id="help-sync"' in html
    assert 'id="help-troubleshooting"' in html


def test_help_page_requires_login(client):
    resp = client.get("/settings/help")
    assert resp.status_code in (302, 401)


def test_help_link_in_profile_dropdown(auth_client):
    resp = auth_client.get("/settings/profile")
    assert b"/settings/help" in resp.data
