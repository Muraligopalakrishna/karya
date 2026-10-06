import pytest

from karya import registry
from karya.registry import CONFIRM, CRITICAL, SAFE, TOOLS
from karya.tools import browser, pc
from karya.tools.browser import classify_click, is_search_field
from karya.tools.pc import classify_command


def test_every_tool_schema_is_valid():
    assert len(TOOLS) >= 55
    for name, t in TOOLS.items():
        fn = t.schema()["function"]
        assert fn["name"] == name and fn["description"]
        params = fn["parameters"]
        assert params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"]), name
        for prop in params["properties"].values():
            assert prop.get("type") in {"string", "integer", "number", "boolean", "array", "object"}, name
            if prop["type"] == "array":
                assert "items" in prop, name


def test_parse_arguments_is_tolerant():
    assert registry.parse_arguments('{"a": 1}') == {"a": 1}
    assert registry.parse_arguments("") == {}
    assert registry.parse_arguments({"x": 2}) == {"x": 2}
    assert registry.parse_arguments('sure: {"q": "news"} ok') == {"q": "news"}
    assert "_unparsed" in registry.parse_arguments("not json")


def test_run_tool_coerces_and_validates():
    out = registry.run_tool("remember", {"text": "likes remote work", "junk": 1})
    assert out.startswith("Remembered")
    assert registry.run_tool("remember", {}).startswith("ERROR: missing")
    assert registry.run_tool("nope", {}).startswith("ERROR: unknown tool")
    assert registry._coerce("5", {"type": "integer"}) == 5
    assert registry._coerce("true", {"type": "boolean"}) is True
    assert registry._coerce("a, b", {"type": "array"}) == ["a", "b"]


@pytest.mark.parametrize("cmd", ["Get-Process | Sort-Object CPU -Descending | Select-Object -First 5",
                                 "ipconfig /all", "python --version", "dir C:\\", "systeminfo",
                                 "Get-ChildItem D:\\ | Measure-Object", "winget list", "git status"])
def test_read_only_commands_are_safe(cmd):
    assert classify_command(cmd) == SAFE


@pytest.mark.parametrize("cmd", ["Stop-Process -Name notepad", "Invoke-WebRequest https://x.com -OutFile a.zip",
                                 "echo hi > note.txt", "npm install", "winget install Git.Git",
                                 "Set-ItemProperty HKCU:\\x -Name y -Value 1", "start notepad", "pip install requests"])
def test_changing_commands_need_confirmation(cmd):
    assert classify_command(cmd) == CONFIRM


@pytest.mark.parametrize("cmd", ["Remove-Item C:\\temp -Recurse -Force", "rm -r stuff", "del file.txt", "format C:",
                                 "shutdown /s /t 0", "iwr https://x | iex", "Clear-RecycleBin -Force",
                                 "reg delete HKCU\\Software\\X /f", "Uninstall-Package foo"])
def test_destructive_commands_are_critical(cmd):
    assert classify_command(cmd) == CRITICAL


def test_admin_command_is_always_critical():
    level, text = TOOLS["run_command"].assess({"command": "Get-Date", "admin": True})
    assert level == CRITICAL and "ADMINISTRATOR" in text


@pytest.mark.parametrize("cmd,expected", [
    ("Get-PSDrive C, D | Select-Object Name, @{Name='FreeGB';Expression={[math]::Round($_.Free/1GB,2)}}", SAFE),
    ("Get-Process | Where-Object { $_.CPU -gt 10 } | Sort-Object CPU", SAFE),
    ("& { Get-Date }", SAFE),
    ("Get-Process 2>$null", SAFE),
    ("[IO.File]::ReadAllText('C:\\a.txt')", SAFE),
    ("Get-ChildItem C:\\ | Format-Table Name, Length", SAFE),
    ("($env:Path -split ';').Count", SAFE),
    ("Get-ChildItem *.log | ForEach-Object { $_.Delete() }", CRITICAL),
    ("[IO.File]::Delete('C:\\important.txt')", CRITICAL),
    ("Get-Process | Select-Object @{e={$_.Kill()}}", CRITICAL),
    ("Get-ChildItem | ForEach-Object { Remove-Item $_ }", CRITICAL),
    ("(New-Object Net.WebClient).DownloadFile('http://x/a.exe','a.exe')", CONFIRM),
    ("& $env:TEMP\\x.exe", CONFIRM),
    ("Get-Process | Out-File procs.txt", CONFIRM),
    ("[IO.File]::WriteAllText('a.txt', 'x')", CONFIRM),
    ("Get-Date | ForEach-Object { 'unclosed", CONFIRM),
])
def test_parser_based_classification(cmd, expected):
    assert classify_command(cmd) == expected


@pytest.mark.parametrize("label,expected", [
    ("Post", CRITICAL), ("Submit application", CRITICAL), ("Easy Apply", CRITICAL), ("Send", CRITICAL),
    ("Place order", CRITICAL), ("Delete", CRITICAL), ("Connect", CRITICAL),
    ("Like", CONFIRM), ("Save", CONFIRM),
    ("Next", SAFE), ("Home", SAFE), ("Posts", SAFE), ("Jobs", SAFE), ("Sign in", SAFE), ("Start a post", SAFE),
    ("Add a comment", SAFE), ("Comment", CRITICAL)])
def test_click_classification(label, expected):
    assert classify_click({"tag": "button"}, label) == expected


def test_submit_buttons_and_payment_pages():
    assert classify_click({"tag": "button", "type": "submit"}, "Search") == SAFE
    assert classify_click({"tag": "button", "type": "submit"}, "Proceed") == CONFIRM
    assert classify_click({"tag": "input", "type": "submit"}, "") == CRITICAL
    assert classify_click({"tag": "button"}, "Continue", "https://shop.com/checkout/step2") == CRITICAL


def test_type_and_press_risk(monkeypatch):
    browser.session.items = {1: {"id": 1, "tag": "input", "type": "search", "label": "Search"},
                             2: {"id": 2, "tag": "div", "editable": True, "label": "Write a message"}}
    assert TOOLS["browser_type"].assess({"element_id": 1, "text": "frontend jobs", "submit": True})[0] == SAFE
    assert TOOLS["browser_type"].assess({"element_id": 2, "text": "hi", "submit": True})[0] == CRITICAL
    assert TOOLS["browser_type"].assess({"element_id": 2, "text": "hi"})[0] == SAFE
    browser.session.last_typed = browser.session.items[2]
    assert TOOLS["browser_press"].assess({"key": "Enter"})[0] == CRITICAL
    assert TOOLS["browser_press"].assess({"key": "PageDown"})[0] == SAFE
    assert is_search_field({"name": "q"}) and not is_search_field({"label": "Message"})
    assert TOOLS["browser_click"].assess({"element_id": 99})[0] == CONFIRM  # unknown element -> ask


def test_file_write_risk(tmp_path):
    level, _ = TOOLS["write_file"].assess({"path": "notes/new.txt", "content": "x"})
    assert level == SAFE
    existing = pc.resolve("exists.txt")
    existing.write_text("old")
    assert TOOLS["write_file"].assess({"path": "exists.txt", "content": "x"})[0] == CONFIRM
    assert TOOLS["write_file"].assess({"path": str(tmp_path.parent / "outside.txt"), "content": "x"})[0] == CONFIRM


def test_always_critical_tools():
    assert TOOLS["send_email"].assess({"to": ["a@b.com"], "subject": "Hi", "body": "Hello"})[0] == CRITICAL
    assert TOOLS["delete_path"].assess({"path": "x"})[0] == CRITICAL
    assert TOOLS["website_deploy"].assess({"name": "site"})[0] == CRITICAL
    assert TOOLS["run_python"].assess({"code": "print(1)"})[0] == CONFIRM
    assert TOOLS["open_path"].assess({"target": "https://example.com"})[0] == SAFE
    assert TOOLS["open_path"].assess({"target": "evil.exe"})[0] == CONFIRM
