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


def test_help_lists_enabled_modules(app, auth_client):
    from app.extensions import db
    from app.models import ModuleState

    resp = auth_client.get("/settings/help")
    assert b'id="help-modules"' not in resp.data

    with app.app_context():
        db.session.add(ModuleState(name="hello", enabled=True))
        db.session.commit()

    resp = auth_client.get("/settings/help")
    html = resp.data.decode()
    assert 'id="help-modules"' in html
    assert "Hello" in html
