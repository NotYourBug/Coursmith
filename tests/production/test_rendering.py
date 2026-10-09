"""Untrusted Markdown must remain inert and never fetch resources."""

from html.parser import HTMLParser
import importlib
from pathlib import Path
import socket
import subprocess
import sys

import pytest


class Fragment(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, text):
        self.text.append(text)


def render(markdown):
    return importlib.import_module("course_platform.production.rendering").render_markdown(markdown)


def test_raw_html_and_code_examples_never_execute():
    raw = '<script>alert(1)</script>\n<iframe src="https://example.test"></iframe>\n'
    raw += '<svg onload="run()"></svg><form><input autofocus onfocus="run()"></form>\n'
    raw += '<style>body{display:none}</style><img src=x onerror=run()>\n\n'
    result = render(raw + '```html\n<script>example()</script>\n```\n\n`<button onclick="go()">`')
    fragment = Fragment(result.html)
    assert not {tag for tag, _ in fragment.elements} & {
        "script", "iframe", "svg", "form", "input", "style", "img", "button"
    }
    assert all(not name.startswith("on") and name != "style"
               for _, attrs in fragment.elements for name in attrs)
    assert '<script>example()</script>' in ''.join(fragment.text)
    assert '<button onclick="go()">' in ''.join(fragment.text)
    assert "&lt;script&gt;" in result.html


@pytest.mark.parametrize("image", [
    "![图片](https://remote.example.test/image.png)",
    "![图片](data:image/png;base64,AA==)",
    "![图片](/private/image.png)",
    "![图片][pic]\n\n[pic]: https://remote.example.test/image.png",
], ids=["remote", "data", "relative", "reference"])
def test_remote_images_are_placeholders(image):
    result = render(image)
    assert all(tag != "img" for tag, _ in Fragment(result.html).elements)
    assert "待补素材" in ''.join(Fragment(result.html).text)
    assert [(f.code, f.chapter_id, f.blocking) for f in result.findings] == [
        ("image_placeholder", None, False)
    ]


@pytest.mark.parametrize("url", [
    "javascript:alert%281%29", "data:text/html,evil", "file:///private",
    "mailto:owner@example.test", "//example.test/path", "/private", "../escape",
    "https://alice:secret@example.test/path", "https://alice@example.test/path",
    "https://example.test/%0Asecret", "https://example.test/%00",
    "https://example.test/%7F", "https://example.test/%C2%85",
    "https://example.test/%E2%80%AE", "https://example.test/%5Cevil",
    "https://example.test:99999/path", "https:///missing-host",
], ids=["javascript", "data", "file", "mail", "protocol-relative", "root", "parent",
        "password", "username", "newline", "null", "del", "c1", "bidi", "backslash",
        "bad-port", "missing-host"])
def test_unsafe_links_and_credentials_in_url_rejected(url):
    result = render(f"[链接](<{url}>)")
    assert all("href" not in attrs for _, attrs in Fragment(result.html).elements)
    assert any(f.code == "unsafe_link" and f.blocking for f in result.findings)
    assert url not in repr(result.findings)
    assert "secret" not in repr(result.findings)


@pytest.mark.parametrize("markdown", [
    '[资料](https://example.test/中文?q=1&x=2 "标题")',
    '<https://example.test/path>',
    '[资料][ref]\n\n[ref]: http://example.test/path',
], ids=["inline", "autolink", "reference"])
def test_safe_links_are_external_with_safe_relationship(markdown):
    result = render(markdown)
    links = [attrs for tag, attrs in Fragment(result.html).elements if tag == "a"]
    assert len(links) == 1
    assert links[0]["href"].startswith(("https://example.test/", "http://example.test/"))
    assert set(links[0]["rel"].split()) == {"noopener", "noreferrer"}
    assert "外部资料" in ''.join(Fragment(result.html).text)
    assert result.findings == ()


def test_render_preserves_chinese_tables_and_long_code():
    code = '示例 <script>literal()</script> 😀 ' + "x" * 12000
    result = render('| 项目 | 说明 |\n| --- | --- |\n| 中文😀 | 百分号% |\n\n```text\n' + code + '\n```')
    fragment = Fragment(result.html)
    assert {"table", "thead", "tbody", "th", "td", "pre", "code"} <= {
        tag for tag, _ in fragment.elements
    }
    assert "中文😀" in ''.join(fragment.text)
    assert code in ''.join(fragment.text)


def test_rendering_does_not_fetch_images_or_links(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Renderer attempted network access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    result = render('![图](https://invalid.example.test/a.png)\n\n[资料](https://invalid.example.test)')
    assert "待补素材" in result.html
    assert "外部资料" in result.html


def test_import_has_no_database_network_worker_or_file_side_effects(tmp_path):
    # Fresh process exercises import, rather than a previously cached module.
    source_root = Path(__file__).resolve().parents[2]
    script = '''
import pathlib, socket, sqlite3, sys, threading
sys.path.insert(0, sys.argv[1])
def denied(*a, **kw):
    raise AssertionError("import side effect")
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
sqlite3.connect = denied
threading.Thread.start = denied
pathlib.Path.write_text = denied
pathlib.Path.write_bytes = denied
import course_platform.production.rendering
import course_platform.production.checks
assert not any(name.startswith("course_platform.jobs") for name in sys.modules)
assert list(pathlib.Path.cwd().iterdir()) == []
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(source_root)],
                            cwd=tmp_path, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize("url", ["https://exa\u200bmple.test/path", "https://exa\u200dmple.test/path"],
                         ids=["zero-width-space-host", "zero-width-joiner-host"])
def test_url_normalization_cannot_hide_original_host_controls(url):
    result = render(f"[资料](<{url}>)")
    assert all("href" not in attrs for _, attrs in Fragment(result.html).elements)
    assert any(f.code == "unsafe_link" and f.blocking for f in result.findings)


@pytest.mark.parametrize("encoded", ["%2f", "%2F", "%3f", "%3F", "%23", "%252f", "%253f", "%2523"],
                         ids=["slash-lower", "slash-upper", "query-lower", "query-upper", "fragment",
                              "double-slash", "double-query", "double-fragment"])
@pytest.mark.parametrize("authority", ["{encoded}@evil.test", "{encoded}:PRIVATE_CREDENTIAL@evil.test",
                                      "{encoded}%40evil.test", "{encoded}.evil.test"],
                         ids=["username", "username-password", "encoded-at", "ambiguous-host"])
def test_original_authority_credentials_and_encoded_delimiters_are_rejected(encoded, authority):
    url = "https://example.test" + authority.format(encoded=encoded) + "/path"
    result = render(f"[资料](<{url}>)")
    assert all("href" not in attrs for _, attrs in Fragment(result.html).elements)
    assert [(finding.code, finding.chapter_id, finding.blocking) for finding in result.findings] == [
        ("unsafe_link", None, True)
    ]
    assert "PRIVATE_CREDENTIAL" not in repr(result.findings)
    assert url not in repr(result.findings)


@pytest.mark.parametrize("url", [
    "HTTP://example.test/资料?item=1&next=2", "https://例子.test/中文",
    "https://example.test/path%2Fpart?q=what%3Fvalue#topic%23part",
], ids=["http-chinese", "chinese-host", "encoded-path-query-fragment"])
def test_unambiguous_authorities_preserve_normal_http_links(url):
    result = render(f"[资料](<{url}>)")
    links = [attrs for tag, attrs in Fragment(result.html).elements if tag == "a"]
    assert len(links) == 1
    assert set(links[0]["rel"].split()) == {"noopener", "noreferrer"}
    assert "外部资料" in ''.join(Fragment(result.html).text)
    assert result.findings == ()
