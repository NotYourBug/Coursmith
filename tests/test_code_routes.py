"""Actual owner/CSRF, one-time response and DOM-only CSV contracts."""

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from threading import Event, Thread

import httpx
import pytest

from course_platform.admin.routes.codes import router  # noqa: F401
from course_platform.database import transaction
from test_product_routes import login, post


def values(product, **extra):
    return {"product_id": str(product.id), "count": "1", "purpose": "sale", "activation_days": "30", "note": "",
            "idempotency_key": "http-batch-key", **extra}


def test_code_response_and_history_do_not_leak_secrets(code_client, active_product, db_path):
    login(code_client)
    form = code_client.get("/admin/code-batches/new")
    assert form.status_code == 200 and 'name="idempotency_key"' in form.text
    response = post(code_client, "/admin/code-batches/new", values(active_product, note='<script>alert("中文")</script>'))
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    raw = re.search(r"CS-[A-Za-z0-9_-]{32}", response.text).group()
    assert '<script>alert' not in response.text and '&lt;script&gt;' in response.text
    assert '<script src="/static/admin/admin.js" defer></script>' in response.text
    replay = post(code_client, "/admin/code-batches/new", values(active_product, note='<script>alert("中文")</script>'))
    assert replay.status_code == 200 and raw not in replay.text and "明文" in replay.text
    for path in ("/admin/code-batches", "/admin/code-batches/1", "/admin/code-batches/1/csv", "/admin/code-batches/1/download"):
        page = code_client.get(path)
        assert raw not in page.text
    with transaction(db_path) as connection:
        hashed = connection.execute("SELECT code_hash FROM access_codes").fetchone()[0]
    assert hashed not in code_client.get("/admin/code-batches/1").text
    assert raw.encode() not in db_path.read_bytes()


@pytest.mark.parametrize("path,action", [("/admin/code-batches/new", "code.issue"),
    ("/admin/codes/1/revoke", "code.void"), ("/admin/codes/1/replace", "code.reissue"),
    ("/admin/code-batches/1/revoke-unused", "code.void"), ("/admin/code-batches/1/replace-unused", "code.reissue")])
@pytest.mark.parametrize("fault,status", [("auth", 401), ("origin", 403), ("csrf", 403), ("body", 413), ("revision", 409)])
def test_every_domain_post_denial_has_fixed_audit(code_client, active_product, code_service, actor, db_path, path, action, fault, status):
    from course_platform.operations.codes import BatchInput

    code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="sale"), "service-key")
    if fault != "auth":
        login(code_client)
    data = values(active_product) | {"revision": "bad" if fault == "revision" else "1", "reason": "作废"}
    if fault == "revision" and path.endswith("new"):
        # Issuance has no previous batch revision; its invalid input is 400.
        data["count"] = "true"
        status = 400
    data["csrf_token"] = "bad" if fault == "csrf" else code_client.cookies.get("coursmith_admin_csrf", "")
    headers = {"Origin": "https://evil.example" if fault == "origin" else "http://testserver"}
    response = code_client.post(path, content=b"x" * 65537, headers={**headers, "Content-Type": "application/x-www-form-urlencoded"}) if fault == "body" else code_client.post(path, data=data, headers=headers)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    with transaction(db_path) as connection:
        events = connection.execute("SELECT action, request_id, actor_admin_id FROM admin_events WHERE outcome='denied' AND action=?", (action,)).fetchall()
        assert len(events) == 1 and events[0]["request_id"] == response.headers["x-request-id"]
        assert connection.execute("SELECT voided_at FROM access_codes WHERE id=1").fetchone()[0] is None


def test_routes_replace_and_revoke_with_real_revisions(code_client, active_product):
    login(code_client)
    assert post(code_client, "/admin/code-batches/new", values(active_product, count="2")).status_code == 200
    page = code_client.get("/admin/code-batches/1")
    assert '/admin/codes/1/revoke' in page.text and '/admin/code-batches/1/replace-unused' in page.text
    assert post(code_client, "/admin/codes/1/replace", {"revision": "1", "reason": "丢失", "idempotency_key": "single"}).status_code == 200
    assert post(code_client, "/admin/code-batches/1/replace-unused", {"revision": "1", "reason": "丢失", "idempotency_key": "batch"}).status_code == 409
    assert post(code_client, "/admin/code-batches/1/replace-unused", {"revision": "2", "reason": "丢失", "idempotency_key": "batch"}).status_code == 200
    assert post(code_client, "/admin/code-batches/1/replace-unused", {"revision": "2", "reason": "丢失", "idempotency_key": "batch"}).status_code == 200
    assert post(code_client, "/admin/codes/3/revoke", {"revision": "1", "reason": "作废"}).status_code == 303
    assert post(code_client, "/admin/code-batches/3/revoke-unused", {"revision": "1", "reason": "作废剩余"}).status_code == 303


def test_lists_paginate_and_fetch_only_safe_fields(code_client, active_product, db_path, monkeypatch):
    from course_platform.operations import codes

    login(code_client)
    for index in range(21):
        assert post(code_client, "/admin/code-batches/new", values(active_product, idempotency_key=f"key-{index}")).status_code == 200
    queries = []
    original = codes.open_readonly

    def traced(path):
        connection = original(path)
        connection.set_trace_callback(queries.append)
        return connection

    monkeypatch.setattr(codes, "open_readonly", traced)
    first = code_client.get("/admin/code-batches")
    second = code_client.get("/admin/code-batches?page=2")
    detail = code_client.get("/admin/code-batches/1")
    assert first.status_code == second.status_code == detail.status_code == 200
    assert len(re.findall(r'href="/admin/code-batches/[0-9]+"', first.text)) == 20
    assert len(re.findall(r'href="/admin/code-batches/[0-9]+"', second.text)) == 1
    assert "下一页" in first.text and "上一页" in second.text
    assert all("select *" not in q.lower() and "code_hash" not in q.lower()
               and "issued_policy_json" not in q.lower() for q in queries)
    assert code_client.get("/admin/code-batches?page=999999999999999999").status_code == 400


def test_csv_formula_prefix_is_neutralized():
    # Execute the shipped browser script's DOM/Blob behavior in Node, with
    # only browser primitives adapted; no duplicate CSV algorithm in the test.
    node = shutil.which("node")
    assert node, "Node is required to execute the own external browser script"
    script = Path(__file__).parents[1] / "course_platform/static/admin/admin.js"
    harness = r'''
const fs = require('fs'), vm = require('vm');
let listeners={}, captured, revoked, clicked=false, copied;
const rows=[['编号','码','备注'], ['CODE-1','CS-example','  =1+1'], ['CODE-2','中文','\t+2'],
 ['CODE-3','x','\n-3'], ['CODE-4','x',' @SUM(1)'], ['CODE-5','x','中文,"引用"\n下一行']];
const button=id=>({addEventListener:(name, fn)=>listeners[id]=fn});
global.document={querySelector:s=>s==='#issued-code-table'?{querySelectorAll:()=>rows.map(r=>({querySelectorAll:()=>r.map(textContent=>({textContent}))}))}:null,
 getElementById:button, createElement:()=>({click:()=>clicked=true,remove:()=>{}}),body:{appendChild:()=>{}}};
Object.defineProperty(global,'navigator',{value:{clipboard:{writeText:async value=>copied=value}},configurable:true});
global.URL={createObjectURL:blob=>(captured=blob,'blob:one'),revokeObjectURL:url=>revoked=url};
vm.runInThisContext(fs.readFileSync(process.argv[1],'utf8'));
(async()=>{await listeners['download-csv']();await listeners['copy-codes']();
console.log(JSON.stringify({csv:await captured.text(),revoked,clicked,copied}));})();
'''
    result = subprocess.run([node, "-e", harness, str(script)], capture_output=True, text=True, encoding="utf8", check=True)
    payload = json.loads(result.stdout)
    assert payload["clicked"] and payload["revoked"] == "blob:one"
    for text in ("  =1+1", "\t+2", "\n-3", " @SUM(1)"):
        assert '"\'' + text + '"' in payload["csv"]
    assert '"中文,""引用""\n下一行"' in payload["csv"]
    assert "CS-example" in payload["copied"]


@pytest.mark.parametrize("operation", ["issue_batch", "replace", "replace_unused", "revoke", "revoke_unused", "record_denial"])
def test_sync_code_work_offloaded(code_app, code_client, active_product, code_service, actor, monkeypatch, operation):
    from course_platform.operations.codes import BatchInput

    code_service.issue_batch(actor, BatchInput(product_id=active_product.id, purpose="sale"), "initial")
    login(code_client)
    gate, release = Event(), Event()
    original = getattr(code_service, operation)

    def delayed(*args, **kwargs):
        gate.set()
        assert release.wait(5), "SQLite work blocked the event loop"
        return original(*args, **kwargs)

    monkeypatch.setattr(code_service, operation, delayed)
    route, data = {
        "issue_batch": ("/admin/code-batches/new", values(active_product)),
        "replace": ("/admin/codes/1/replace", {"revision": "1", "reason": "丢失", "idempotency_key": "replacement"}),
        "replace_unused": ("/admin/code-batches/1/replace-unused", {"revision": "1", "reason": "丢失", "idempotency_key": "replacement"}),
        "revoke": ("/admin/codes/1/revoke", {"revision": "1", "reason": "作废"}),
        "revoke_unused": ("/admin/code-batches/1/revoke-unused", {"revision": "1", "reason": "作废"}),
        "record_denial": ("/admin/codes/1/revoke", {"revision": "bad", "reason": "作废"}),
    }[operation]

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=code_app), base_url="http://testserver", cookies=dict(code_client.cookies.items())) as client:
            pending = asyncio.create_task(client.post(route, data={"csrf_token": code_client.cookies.get("coursmith_admin_csrf"), **data}, headers={"Origin": "http://testserver"}))
            assert await asyncio.to_thread(gate.wait, 3)
            try:
                assert (await asyncio.wait_for(client.get("/static/admin/admin.js"), 1)).status_code == 200
                assert not release.is_set()
            finally:
                release.set()
            assert (await pending).status_code == (409 if operation == "record_denial" else 303 if operation.startswith("revoke") else 200)

    watchdog = Thread(target=lambda: (release.wait(4), release.set()), daemon=True)
    watchdog.start()
    try:
        asyncio.run(scenario())
    finally:
        release.set()
        watchdog.join()


def test_stream_stops_at_body_limit_and_duplicate_form_is_audited(code_app, code_client, active_product, db_path):
    login(code_client)
    chunks = []

    async def stream():
        for index in range(3):
            chunks.append(index)
            yield b"x" * 40000

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=code_app), base_url="http://testserver", cookies=dict(code_client.cookies.items())) as client:
            return await client.post("/admin/code-batches/new", content=stream(), headers={
                "Origin": "http://testserver", "Content-Type": "application/x-www-form-urlencoded"})

    response = asyncio.run(scenario())
    assert response.status_code == 413 and chunks == [0, 1]
    duplicate = code_client.post("/admin/code-batches/new", content="count=1&count=2", headers={
        "Origin": "http://testserver", "Content-Type": "application/x-www-form-urlencoded"})
    assert duplicate.status_code == 400
    with transaction(db_path) as connection:
        assert connection.execute("SELECT count(*) FROM code_batches").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM admin_events WHERE action='code.issue' AND outcome='denied'").fetchone()[0] == 2


def test_owner_required_for_batch_pages_and_invalid_objects_are_safe(code_client, active_product, db_path):
    code_client.cookies.set("course_session_fixture-course", "buyer-only")
    for path in ("/admin/code-batches", "/admin/code-batches/new", "/admin/code-batches/1"):
        response = code_client.get(path)
        assert response.status_code == 303 and response.headers["location"] == "/admin/login"
        assert response.headers["cache-control"] == "no-store"
    login(code_client)
    response = post(code_client, "/admin/codes/CS-untrusted/revoke", {"revision": "1", "reason": "作废"})
    assert response.status_code == 400 and "CS-untrusted" not in response.text
    with transaction(db_path) as connection:
        row = connection.execute("SELECT object_type, object_id, action FROM admin_events WHERE action='code.void'").fetchone()
        assert tuple(row) == ("code", "invalid", "code.void")
    assert code_client.get("/admin/code-batches/999").status_code == 404
    assert code_client.get("/admin/codes/1/revoke").status_code == 405
