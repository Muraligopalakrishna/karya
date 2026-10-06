import email
import json
import subprocess
import zipfile
from email.message import EmailMessage

import requests

from karya.config import settings
from karya.registry import run_tool
from karya.tools import email_tools, pc, website


def call(_tool, **args):
    return run_tool(_tool, args)


def test_file_tools_roundtrip(monkeypatch):
    assert "Wrote" in call("write_file", path="notes/todo.txt", content="buy milk\nfix pc")
    assert call("read_file", path="notes/todo.txt") == "buy milk\nfix pc"
    call("write_file", path="notes/todo.txt", content="\ncall mom", append=True)
    assert call("read_file", path="notes/todo.txt").endswith("call mom")
    listing = json.loads(call("list_files", path="notes"))
    assert listing["entries"][0]["name"] == "todo.txt"
    found = json.loads(call("find_files", pattern="todo*", root=str(settings.workspace)))
    assert any(f.endswith("todo.txt") for f in found)
    assert "Copied" in call("copy_path", source="notes/todo.txt", destination="backup/todo.txt")
    assert "Moved" in call("move_path", source="backup/todo.txt", destination="backup/renamed.txt")
    trashed = []
    monkeypatch.setattr(pc, "send2trash", lambda p: (trashed.append(p), __import__("os").remove(p)))
    assert "Recycle Bin" in call("delete_path", path="backup/renamed.txt")
    assert trashed and not (settings.workspace / "backup" / "renamed.txt").exists()
    assert "ready" in call("make_folder", path="projects/new")


def test_extract_text_pdf_and_docx():
    if settings.resume_path.exists():
        assert len(call("read_resume")) > 50
    docx = settings.workspace / "letter.docx"
    with zipfile.ZipFile(docx, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p>"
                                        "<w:p><w:r><w:t>World</w:t></w:r></w:p></w:body></w:document>")
    assert call("read_file", path="letter.docx").split() == ["Hello", "World"]
    (settings.workspace / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    assert "binary" in call("read_file", path="blob.bin")


def test_memory_tools():
    assert "#1" in call("remember", text="Prefers remote roles")
    call("update_profile", field="city", value="Hyderabad")
    data = json.loads(call("recall", query="remote"))
    assert data["notes"][0]["text"] == "Prefers remote roles" and data["profile"]["city"] == "Hyderabad"
    assert call("forget", note_id=1) == "Forgotten."


def test_application_tracker():
    assert "#1" in call("track_application", company="Acme", role="Frontend Dev", url="https://x", method="linkedin")
    call("track_application", company="Beta", role="UI Designer", status="saved")
    assert "interview" in call("update_application", app_id=1, status="interview", notes="call on Monday")
    apps = json.loads(call("list_applications", status="interview"))
    assert apps[0]["company"] == "Acme" and "Monday" in apps[0]["notes"]
    assert call("delete_application", app_id=2) == "Deleted."
    assert call("update_application", app_id=99).startswith("ERROR")


class FakeSMTP:
    sent = []

    def __init__(self, host, port, context=None, timeout=None):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        assert user == "me@gmail.com" and password == "abcdabcdabcdabcd"

    def send_message(self, msg, to_addrs=None):
        FakeSMTP.sent.append((msg, to_addrs))


def _raw_email():
    m = EmailMessage()
    m["From"] = "HR <hr@acme.com>"
    m["To"] = "me@gmail.com"
    m["Subject"] = "Interview invite"
    m["Date"] = "Wed, 30 Sep 2026 10:00:00 +0530"
    m["Message-ID"] = "<abc@acme.com>"
    m.set_content("Hi Asha, can you join an interview on Friday?")
    m.add_attachment(b"%PDF-1.4 test", maintype="application", subtype="pdf", filename="details.pdf")
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, host, timeout=None):
        pass

    def login(self, user, password):
        return "OK", []

    def select(self, folder, readonly=True):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"7"]
        return "OK", [(b"7 (UID 7 FLAGS () RFC822.SIZE 500 BODY[] {500}", _raw_email()), b")"]

    def logout(self):
        pass


def test_email_send_and_read(monkeypatch):
    assert call("send_email", to=["a@b.com"], subject="x", body="y").startswith("ERROR: email is not set up")
    monkeypatch.setattr(settings, "email_address", "me@gmail.com")
    monkeypatch.setattr(settings, "email_password", "abcdabcdabcdabcd")
    monkeypatch.setattr(email_tools.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(email_tools.imaplib, "IMAP4_SSL", FakeIMAP)
    resume = settings.workspace / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    out = call("send_email", to="hr@acme.com, boss@acme.com", subject="Application", body="Hello!",
               cc=["friend@x.com"], attachments=[str(resume)])
    assert out.startswith("Email sent")
    msg, rcpts = FakeSMTP.sent[-1]
    assert rcpts == ["hr@acme.com", "boss@acme.com", "friend@x.com"]
    parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
    assert parsed["Subject"] == "Application" and [a.get_filename() for a in parsed.iter_attachments()] == ["resume.pdf"]
    assert call("send_email", to=["a@b.com"], subject="x", body="y", attachments=["missing.pdf"]).startswith("ERROR")
    rows = json.loads(call("read_emails", limit=5, unread_only=True))
    assert rows[0]["subject"] == "Interview invite" and rows[0]["unread"] is True and "Friday" in rows[0]["snippet"]
    full = json.loads(call("get_email", uid="7", save_attachments=True))
    assert full["message_id"] == "<abc@acme.com>" and "Friday" in full["body"]
    assert (settings.workspace / "email_attachments" / "details.pdf").exists()


def test_website_create_preview_deploy(monkeypatch):
    out = json.loads(call("website_create", name="My Portfolio!", files={
        "index.html": "<!doctype html><h1>Asha</h1>", "css/style.css": "h1{color:red}"}))
    assert out["site"] == "my-portfolio"
    assert sorted(p.replace("\\", "/") for p in out["files_written"]) == ["css/style.css", "index.html"]
    assert call("website_create", name="x", files={"../evil.txt": "x"}).startswith("ERROR")
    prev = json.loads(call("website_preview", name="my-portfolio", open_in_browser=False))
    assert "Asha" in requests.get(prev["url"], timeout=5).text
    assert requests.get(prev["url"] + "css/style.css", timeout=5).text == "h1{color:red}"
    website.stop_previews()

    def fake_run(cmd, cwd=None, capture_output=True, timeout=None, creationflags=0):
        assert "deploy" in cmd and "--prod" in cmd
        return subprocess.CompletedProcess(cmd, 0, b"Production: https://my-portfolio-abc.vercel.app [2s]\n", b"")
    monkeypatch.setattr(website.subprocess, "run", fake_run)
    deployed = json.loads(call("website_deploy", name="my-portfolio"))
    assert deployed["live_urls"] == ["https://my-portfolio-abc.vercel.app"]

    def fake_fail(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, b"", b"Error: No existing credentials found. Please run `vercel login`")
    monkeypatch.setattr(website.subprocess, "run", fake_fail)
    assert "VERCEL_TOKEN" in call("website_deploy", name="my-portfolio")


def test_pc_tools():
    info = json.loads(call("system_info"))
    assert info["ram_total_gb"] > 1 and info["disks"] and info["top_memory"]
    assert json.loads(call("list_processes", limit=3))
    out = json.loads(call("run_command", command="Get-Date -Format yyyy"))
    assert out["exit_code"] == 0 and out["output"].strip().isdigit()
    py = json.loads(call("run_python", code="print(6*7)"))
    assert py["output"].strip() == "42"
    assert "timed out" in call("run_command", command="Start-Sleep -Seconds 10", timeout=5)


def test_clipboard_roundtrip():
    original = call("clipboard", action="get")
    try:
        call("clipboard", action="set", text="karya-test-123")
        assert call("clipboard", action="get").strip() == "karya-test-123"
    finally:
        if original and original != "(clipboard is empty)":
            call("clipboard", action="set", text=original)



def test_commands_cannot_see_karya_secrets(monkeypatch):
    monkeypatch.setenv("KIRO_API_KEY", "ksk_should_not_leak")
    monkeypatch.setenv("EMAIL_APP_PASSWORD", "pw_should_not_leak")
    monkeypatch.setenv("KARYA_HARMLESS_VALUE", "visible")
    out = call("run_command", command="Get-ChildItem env: | Select-Object -ExpandProperty Value")
    assert "ksk_should_not_leak" not in out and "pw_should_not_leak" not in out and "visible" in out
    py = call("run_python", code="import os; print(sorted(k for k in os.environ if 'KEY' in k or 'PASSWORD' in k))")
    assert "KIRO_API_KEY" not in py and "EMAIL_APP_PASSWORD" not in py
