"""Login of the chat web app: credentials from a git-ignored .env file, one user."""
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_chat_web import make_agent  # noqa: E402

import kg_telemetry as telemetry  # noqa: E402
from kg_chat import auth, cli  # noqa: E402
from kg_chat.auth import AuthConfigError, Credentials, LoginThrottle, parse_env  # noqa: E402
from kg_chat.web import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PW = "correct horse battery staple"


def creds(**kw):
    return Credentials(username=kw.pop("username", "alice"), password=kw.pop("password", PW), **kw)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.setattr("kg_chat.web.time.sleep", lambda s: None)
    clock = Clock()
    a = create_app(make_agent(), title="host.test", credentials=creds(), throttle=LoginThrottle(clock=clock))
    a.config["TESTING"] = True
    a.clock = clock
    return a


def csrf_of(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def login(client, user="alice", pw=PW, csrf=None):
    return client.post("/login", data={"username": user, "password": pw, "csrf": csrf if csrf is not None else csrf_of(client)})


# ---------------------------------------------------------------------------- the .env file

def test_env_parser():
    env = parse_env('# c\nCHAT_USERNAME=alice\nexport CHAT_PASSWORD = "pass # word"\nA=\'x y\'\nB=plain # trailing\nbad line\n1X=no\nCHAT_USERNAME=bob\n\n')
    assert env["CHAT_USERNAME"] == "bob" and env["CHAT_PASSWORD"] == "pass # word" and env["A"] == "x y" and env["B"] == "plain" and "1X" not in env and "bad line" not in env


def test_credentials_file_checks(tmp_path):
    f = tmp_path / ".env"
    with pytest.raises(AuthConfigError) as e:
        Credentials.from_env_file(f)
    assert ".env.example" in str(e.value)
    f.write_text("CHAT_PASSWORD=abcdefghijklmnop\n")
    with pytest.raises(AuthConfigError) as e:
        Credentials.from_env_file(f)
    assert "CHAT_USERNAME" in str(e.value) and "abcdefghijklmnop" not in str(e.value)
    f.write_text("CHAT_USERNAME=alice\n")
    with pytest.raises(AuthConfigError) as e:
        Credentials.from_env_file(f)
    assert "CHAT_PASSWORD" in str(e.value)
    f.write_text("CHAT_USERNAME=alice\nCHAT_PASSWORD_HASH=md5:nope\n")
    with pytest.raises(AuthConfigError):
        Credentials.from_env_file(f)
    f.write_text("CHAT_USERNAME=alice\nCHAT_PASSWORD=hunter22x\nFLASK_SECRET_KEY=k\n")
    f.chmod(0o644)
    c = Credentials.from_env_file(f)
    assert c.secret_key == "k" and any("shorter than" in w for w in c.warnings) and any("chmod 600" in w for w in c.warnings)
    assert "hunter22x" not in repr(c) and "hunter22x" not in str(c.warnings)                       # the password is in no repr or warning
    f.chmod(0o600)
    f.write_text("CHAT_USERNAME=alice\nCHAT_PASSWORD=a-passphrase-that-is-long\n")
    assert Credentials.from_env_file(f).warnings == []


def test_password_check_plain_and_hashed():
    plain = creds()
    assert plain.verify("alice", PW) and not plain.verify("alice", "x") and not plain.verify("bob", PW) and not plain.verify("", "")
    hashed = Credentials(username="alice", password_hash=auth.hash_password(PW))
    assert hashed.password_hash.startswith("scrypt:") and hashed.verify("alice", PW) and not hashed.verify("alice", PW + "x") and not hashed.verify("bob", PW)


def test_hash_password_command(monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PW)
    cli.main(["hash-password"])
    line = capsys.readouterr().out.strip()
    assert line.startswith("CHAT_PASSWORD_HASH=scrypt:") and Credentials(username="alice", password_hash=line.split("=", 1)[1]).verify("alice", PW)
    answers = iter(["a", "b"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    with pytest.raises(SystemExit):
        cli.main(["hash-password"])


def test_env_files_are_git_ignored_and_the_example_is_tracked():
    assert subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT).returncode == 0
    assert subprocess.run(["git", "check-ignore", "-q", ".env.example"], cwd=ROOT).returncode != 0
    ex = (ROOT / ".env.example").read_text()
    assert "CHAT_USERNAME" in ex and "CHAT_PASSWORD" in ex and "chmod 600" in ex


# ---------------------------------------------------------------------------- the login flow

def test_everything_needs_a_login_except_health(app):
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    for path in ("/api/status",):
        assert c.get(path).status_code == 401
    assert c.post("/api/chat", json={"message": "hi"}).status_code == 401 and c.post("/api/reset", json={}).status_code == 401
    assert c.get("/healthz").status_code == 200 and c.get("/login").status_code == 200
    assert c.get("/metrics").status_code == 404                                       # not enabled here; it is never behind the login


def test_login_success_session_and_logout(app):
    c = app.test_client()
    r = login(c)
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Secure" not in cookie
    assert c.get("/").status_code == 200 and b"Sign out" in c.get("/").data and c.get("/login").status_code == 302
    assert c.post("/api/chat", json={"message": "damping welded steel"}).status_code == 200
    token = re.search(r'name="csrf" value="([0-9a-f]+)"', c.get("/").get_data(as_text=True)).group(1)
    assert c.post("/logout", data={"csrf": "wrong"}).status_code == 400 and c.get("/").status_code == 200       # a forged logout does nothing
    out = c.post("/logout", data={"csrf": token})
    assert out.status_code == 302 and out.headers["Location"].endswith("/login")
    assert c.get("/").status_code == 302 and c.post("/api/chat", json={"message": "hi"}).status_code == 401


def test_wrong_credentials_say_nothing_about_which_part_was_wrong(app):
    c = app.test_client()
    a = login(c, pw="nope")
    b = login(c, user="mallory")
    assert a.status_code == b.status_code == 401
    assert "Invalid username or password." in a.get_data(as_text=True) and "Invalid username or password." in b.get_data(as_text=True)
    assert c.get("/").status_code == 302


def test_login_needs_the_csrf_token_and_ignores_next_redirects(app):
    c = app.test_client()
    assert login(c, csrf="").status_code == 400 and login(c, csrf="deadbeef").status_code == 400
    assert c.post("/login", data={"username": "alice", "password": PW}).status_code == 400
    r = c.post("/login?next=//evil.example/", data={"username": "alice", "password": PW, "csrf": csrf_of(c)})
    assert r.status_code == 302 and r.headers["Location"] == "/"                         # always the home page, never a caller-chosen address


def test_repeated_failures_lock_the_address_out_and_it_expires(app, caplog):
    c = app.test_client()
    before = {o: telemetry.metrics().value("cogent_chat_logins_total", outcome=o) for o in ("ok", "failed", "locked")}
    for _ in range(4):
        assert login(c, pw="bad").status_code == 401                                     # the free attempts
    assert login(c, pw="bad").status_code == 401                                         # fifth failure starts the lock-out
    r = login(c)                                                                         # even the right password is refused now
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0 and "Too many failed attempts" in r.get_data(as_text=True)
    app.clock.t += 31
    assert login(c).status_code == 302                                                   # served its time
    m = telemetry.metrics()
    assert m.value("cogent_chat_logins_total", outcome="failed") == before["failed"] + 5
    assert m.value("cogent_chat_logins_total", outcome="locked") == before["locked"] + 1
    assert m.value("cogent_chat_logins_total", outcome="ok") == before["ok"] + 1
    t = LoginThrottle(clock=app.clock)
    waits = [t.failed("x") for _ in range(8)]
    assert waits[:4] == [0, 0, 0, 0] and waits[4] == 30 and waits[5] == 60 and waits[6] == 120
    assert LoginThrottle(clock=app.clock, cap=100).failed("y") == 0
    t.succeeded("x")
    assert t.wait("x") == 0


def test_the_password_is_never_logged_or_sent_back(app, caplog):
    c = app.test_client()
    with caplog.at_level(logging.INFO):
        bad = login(c, pw="my-secret-guess")
        good = login(c)
    assert "my-secret-guess" not in caplog.text and PW not in caplog.text
    assert "my-secret-guess" not in bad.get_data(as_text=True) and PW not in good.get_data(as_text=True)
    assert "login failed" in caplog.text and "login ok" in caplog.text


def test_secure_cookie_security_headers_and_secret_key(monkeypatch):
    monkeypatch.setattr("kg_chat.web.time.sleep", lambda s: None)
    a = create_app(make_agent(), credentials=creds(secret_key="from-env-file"), secure_cookie=True, session_hours=1)
    assert a.config["SECRET_KEY"] == "from-env-file" and a.config["SESSION_COOKIE_SECURE"] and a.config["PERMANENT_SESSION_LIFETIME"].total_seconds() == 3600
    r = a.test_client().get("/login")
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Cache-Control"] == "no-store"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_without_credentials_there_is_no_login_page():
    a = create_app(make_agent())
    c = a.test_client()
    assert c.get("/").status_code == 200 and c.get("/login").status_code == 404


def test_metrics_stays_on_its_own_token_not_the_login(monkeypatch):
    a = create_app(make_agent(), credentials=creds(), metrics=True, metrics_token="mtok")
    c = a.test_client()
    assert c.get("/metrics").status_code == 401                                          # no bearer token
    assert c.get("/metrics", headers={"Authorization": "Bearer mtok"}).status_code == 200      # no browser login needed


# ---------------------------------------------------------------------------- the command line

def test_web_command_needs_credentials_or_no_login(monkeypatch, tmp_path):
    monkeypatch.setenv("COGENT_MCP_TOKEN", "x")
    with pytest.raises(SystemExit) as e:
        cli.main(["web", "--mcp-url", "https://h/mcp", "--env-file", str(tmp_path / "missing.env")])
    assert ".env.example" in str(e.value)
    env = tmp_path / ".env"
    env.write_text("CHAT_USERNAME=u\nCHAT_PASSWORD=a-long-enough-passphrase\n")
    with pytest.raises(SystemExit) as e2:
        cli.main(["web", "--mcp-url", "https://h/mcp", "--host", "0.0.0.0", "--allow-remote", "--no-login", "--env-file", str(env)])
    assert "--no-login is only allowed on 127.0.0.1" in str(e2.value)


# ---------------------------------------------------------------------------- in a browser

def test_login_page_in_a_real_browser(monkeypatch):
    pytest.importorskip("playwright")
    import os
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server
    monkeypatch.setattr("kg_chat.web.time.sleep", lambda s: None)
    srv = make_server("127.0.0.1", 0, create_app(make_agent(), title="host.test", credentials=creds()), threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with sync_playwright() as pw:
            exe = os.environ.get("KG_BROWSER_PATH") or ("/opt/pw-browsers/chromium" if os.path.exists("/opt/pw-browsers/chromium") else None)
            try:
                browser = pw.chromium.launch(args=["--no-sandbox"], executable_path=exe)
            except Exception as exc:
                pytest.skip(f"no browser: {str(exc)[:80]}")
            page = browser.new_page()
            page.goto(base + "/")
            assert page.url.endswith("/login") and page.is_visible("#username")
            page.fill("#username", "alice")
            page.fill("#password", "wrong")
            page.click("button[type=submit]")
            assert "Invalid username or password." in page.inner_text(".err") and page.url.endswith("/login")
            page.fill("#username", "alice")
            page.fill("#password", PW)
            page.click("button[type=submit]")
            page.wait_for_selector("#q")
            assert page.url == base + "/" and "Sign out" in page.inner_text("header")
            page.fill("#q", "damping for welded steel")
            page.click("#send")
            page.wait_for_selector(".msg.bot table")
            page.click("text=Sign out")
            page.wait_for_selector("#username")
            assert page.url.endswith("/login")
            page.goto(base + "/")
            assert page.url.endswith("/login")                                          # signed out for real
            browser.close()
    finally:
        srv.shutdown()
