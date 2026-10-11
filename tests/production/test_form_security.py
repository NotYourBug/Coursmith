"""Raw/decoded bounds and every native POST security gate."""
import asyncio

import httpx
import pytest

from tests.production.test_routes import csrf, post, textarea

POST_PATHS = [
    "/admin/production/new",
    *[f"/admin/production/1/{suffix}" for suffix in (
        "brief", "mode", "archive", "references/save", "references/1/remove", "outline/save", "outline/confirm", "outline/adopt",
        "chapters/00000000-0000-0000-0000-000000000001/save", "chapters/00000000-0000-0000-0000-000000000001/regenerate",
        "candidates/1/adopt", "candidates/1/reject", "acknowledge", "generate")],
    "/admin/jobs/1/cancel", "/admin/jobs/1/resume", "/admin/jobs/1/calls/1/reconcile",
    "/admin/jobs/check-interrupted", "/admin/jobs/model/test-connection",
]


@pytest.mark.parametrize("path", POST_PATHS, ids=lambda p: p.replace("/admin/", ""))
@pytest.mark.parametrize("fault,status", [("anonymous", 401), ("origin", 403), ("csrf", 403)])
def test_every_post_requires_current_owner_origin_csrf(production_client, path, fault, status):
    c = production_client
    data = {"csrf_token": csrf(c), "title": "private-input-sentinel", "markdown": "private-input-sentinel", "revision": "1"}
    headers = {"Origin": "http://evil.test" if fault == "origin" else "http://testserver"}
    if fault == "anonymous":
        c.cookies.clear()
    elif fault == "csrf":
        data["csrf_token"] = "b" * 43
    response = c.post(path, data=data, headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert "private-input-sentinel" not in response.text


def test_2mib_chunked_body_rejected_before_parse(production_client, monkeypatch):
    import course_platform.admin.routes.auth as auth
    def forbidden(*args):
        raise AssertionError("overflow must fail before parse")
    monkeypatch.setattr(auth, "parse_unique_form", forbidden)
    async def run():
        async def chunks():
            for _ in range(33):
                yield b"x" * 65536
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=production_client.app), base_url="http://testserver",
            cookies=dict(production_client.cookies.items())) as c:
            return await c.post("/admin/production/new", content=chunks(), headers={"Origin": "http://testserver", "Content-Type": "application/x-www-form-urlencoded"})
    response = asyncio.run(run())
    assert response.status_code == 413 and response.headers["cache-control"] == "no-store"


def test_duplicate_decoded_field_rejected(production_client):
    c = production_client
    response = c.post("/admin/production/new", content=f"csrf_token={csrf(c)}&%63srf_token={csrf(c)}&title=private-title&mode=quick",
        headers={"Origin": "http://testserver", "Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 400 and response.headers["cache-control"] == "no-store"
    assert "private-title" not in response.text


def test_m1_forms_still_limited_to_65536(production_client):
    response = post(production_client, "/admin/account/password", revision="1", current_password="example-pass-123", new_password="x" * 65536)
    assert response.status_code == 413
    response = post(production_client, "/admin/production/new", title="课程", mode="quick", audience="x" * 65536)
    assert response.status_code == 400  # production-specific raw gate passed


def test_unauthorized_or_bad_csrf_never_echoes_markdown(production_data):
    d, c = production_data, production_data.client
    path = f"/admin/production/{d.project_id}/chapters/{d.chapter_id}/save"
    response = c.post(path, data={"csrf_token": "b" * 43, "markdown": "私密正文不得回显"}, headers={"Origin": "http://testserver"})
    assert response.status_code == 403 and "私密正文不得回显" not in response.text
    c.cookies.clear()
    response = c.post(path, data={"markdown": "私密正文不得回显"}, headers={"Origin": "http://testserver"})
    assert response.status_code == 401 and "私密正文不得回显" not in response.text


@pytest.mark.parametrize("method,path", [("put", "/admin/production/new"), ("get", "/admin/jobs/check-interrupted"), ("delete", "/admin/jobs/model"), ("post", "/admin/production/1/preview")])
def test_method_rejections_are_private(production_client, method, path):
    response = getattr(production_client, method)(path)
    assert response.status_code == 405 and response.headers["cache-control"] == "no-store"


def test_safe_conflict_keeps_code_but_omits_known_credentials(production_data):
    d, c = production_data, production_data.client
    path = f"/admin/production/{d.project_id}/chapters/{d.chapter_id}/save"
    assert post(c, path, body_revision="", markdown="saved").status_code == 303
    code = "```\n" + "AbCdef1234567890" * 10000 + "\n```"
    response = post(c, path, body_revision="", markdown=code)
    assert response.status_code == 409 and textarea(response, "markdown") == code
    for secret in ("task9-fake-api-key", c.cookies.get("coursmith_admin"), csrf(c), "CS-secretfragment", "LK-secretfragment", "named-secret"):
        response = post(c, path, body_revision="", markdown="prefix " + secret, api_key="named-secret")
        assert response.status_code == 409
        assert textarea(response, "markdown") == "" and secret not in response.text


@pytest.mark.parametrize("fault", ["disabled", "expired", "revoked", "buyer"])
def test_current_session_faults_never_echo_input(production_data, fault):
    from course_platform.database import transaction
    d, c = production_data, production_data.client
    if fault == "buyer":
        c.cookies.clear()
        c.cookies.set("coursmith_session", "buyer-cookie", path="/")
    else:
        with transaction(d.db_path, immediate=True) as conn:
            if fault == "disabled":
                conn.execute("UPDATE admins SET enabled=0")
            elif fault == "expired":
                conn.execute("UPDATE admin_sessions SET expires_at='2000-01-01T00:00:00+00:00'")
            else:
                conn.execute("UPDATE admin_sessions SET revoked_at='2026-10-02T00:00:00+00:00'")
    response = post(c, f"/admin/production/{d.project_id}/chapters/{d.chapter_id}/save", body_revision="", markdown="current-session-private-source")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store" and "current-session-private-source" not in response.text


def test_concurrent_session_revocation_prevents_safe_conflict_echo(production_data, monkeypatch):
    from course_platform.database import transaction
    from course_platform.domain import BusinessError
    d, c = production_data, production_data.client
    target = c.app.state.draft_service
    real_save = target.save_chapter
    assert post(c, f"/admin/production/{d.project_id}/chapters/{d.chapter_id}/save", body_revision="", markdown="saved").status_code == 303
    def revoke(*args, **kwargs):
        try:
            return real_save(*args, **kwargs)
        except BusinessError:
            with transaction(d.db_path, immediate=True) as conn:
                conn.execute("DELETE FROM admin_sessions")
            raise
    monkeypatch.setattr(target, "save_chapter", revoke)
    response = post(c, f"/admin/production/{d.project_id}/chapters/{d.chapter_id}/save", body_revision="", markdown="revoked-private-copy")
    assert response.status_code == 401 and "revoked-private-copy" not in response.text


def test_decoded_200kib_body_is_separate_from_raw_limit(production_data):
    d, c = production_data, production_data.client
    path = f"/admin/production/{d.project_id}/chapters/{d.chapter_id}/save"
    body = "中文😀" * 20480
    assert len(body.encode()) == 204800
    assert post(c, path, body_revision="", markdown=body).status_code == 303
    response = post(c, path, body_revision="1", markdown=body + "x")
    assert response.status_code == 400 and "chapter_too_large" in response.text
    assert textarea(response, "markdown") == ""
    assert d.drafts.freeze_input(d.project_id).chapters[0].markdown == body


def test_known_session_fragment_is_excluded_without_long_code_heuristic(production_data):
    d, c = production_data, production_data.client
    path = f"/admin/production/{d.project_id}/chapters/{d.chapter_id}/save"
    assert post(c, path, body_revision="", markdown="saved").status_code == 303
    fragment = c.cookies.get("coursmith_admin")[:20]
    response = post(c, path, body_revision="", markdown="prefix " + fragment)
    assert response.status_code == 409 and textarea(response, "markdown") == ""
    assert fragment not in response.text


def test_form_selected_foreign_chapter_and_mapping_never_echo(production_data):
    from course_platform.production.models import BriefInput, OutlineInput, OutlineItem
    d, c = production_data, production_data.client
    other = d.projects.create(d.actor, BriefInput(title="其他"), mode="quick")
    outline = d.drafts.save_outline(d.actor, other.id, other.revision, OutlineInput(items=[OutlineItem(chapter_id=None, title="其他章", goal="")]))
    foreign = outline.items[0].chapter_id
    for suffix in ("generate", "acknowledge"):
        response = post(c, f"/admin/production/{d.project_id}/" + suffix, revision="2", kind="chapters_generate", chapter_ids=foreign, markdown="forbidden-copy")
        assert response.status_code == 404 and "forbidden-copy" not in response.text


def test_denial_action_is_server_fixed_and_private(production_data, caplog):
    import json
    import logging
    from course_platform.database import transaction
    d, c = production_data, production_data.client
    caplog.set_level(logging.DEBUG)
    with transaction(d.db_path) as conn:
        prior = conn.execute("SELECT max(id) FROM admin_events").fetchone()[0]
    response = post(c, f"/admin/production/{d.project_id}/brief", revision="1", title="private-audit-title", action="auth.login")
    assert response.status_code == 409
    with transaction(d.db_path) as conn:
        events = [dict(r) for r in conn.execute("SELECT * FROM admin_events WHERE id>?", (prior,))]
    assert len(events) == 1 and events[0]["action"] == "production.update"
    assert json.loads(events[0]["changes_json"]) == {"error_code": "stale_revision"}
    assert "private-audit-title" not in str(events) + caplog.text
    assert textarea(response, "title") == "private-audit-title"


def test_conflict_rechecks_current_csrf_binding_before_echo(production_data, monkeypatch):
    from course_platform.database import transaction
    from course_platform.domain import BusinessError
    d, c = production_data, production_data.client
    path = f"/admin/production/{d.project_id}/chapters/{d.chapter_id}/save"
    assert post(c, path, body_revision="", markdown="saved").status_code == 303
    target = c.app.state.draft_service
    real_save = target.save_chapter
    def rotate_binding(*args, **kwargs):
        try:
            return real_save(*args, **kwargs)
        except BusinessError:
            with transaction(d.db_path, immediate=True) as conn:
                conn.execute("UPDATE admin_sessions SET csrf_hash=?", ("a" * 64,))
            raise
    monkeypatch.setattr(target, "save_chapter", rotate_binding)
    response = post(c, path, body_revision="", markdown="rotated-csrf-private-source")
    assert response.status_code == 403
    assert "rotated-csrf-private-source" not in response.text


@pytest.mark.parametrize("path", ["/admin/production", "/admin/production/new", "/admin/production/1", "/admin/production/1/preview",
    "/admin/production/1/chapters/00000000-0000-0000-0000-000000000001", "/admin/production/1/candidates/1", "/admin/jobs", "/admin/jobs/1", "/admin/jobs/model"])
def test_all_private_gets_require_current_owner(production_client, path):
    production_client.cookies.clear()
    response = production_client.get(path)
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"
    assert response.headers["cache-control"] == "no-store"


def test_cross_job_call_guard_and_job_revision_is_distinct(production_data):
    from course_platform.database import transaction
    from course_platform.production.llm import ModelFailure
    from tests.jobs.fakes import ScriptedModel
    from tests.production.test_routes import run_controlled_worker
    d, c = production_data, production_data.client
    jobs = []
    for index in range(2):
        queued = post(c, f"/admin/production/{d.project_id}/generate", revision=str(d.projects.get(d.project_id).revision),
            kind="chapters_generate", idempotency_key=f"call-owner-{index}")
        assert queued.status_code == 303
        assert asyncio.run(run_controlled_worker(d, ScriptedModel([ModelFailure("response_unknown", None)])).run_once())
        job_id = int(queued.headers["location"].split("/")[-1])
        with transaction(d.db_path) as conn:
            row = dict(conn.execute("SELECT * FROM production_jobs WHERE id=?", (job_id,)).fetchone())
            call_id = conn.execute("SELECT id FROM model_calls WHERE job_id=?", (job_id,)).fetchone()[0]
        jobs.append((queued.headers["location"], row["revision"], call_id))
    before = c.app.state.job_service.get(int(jobs[0][0].split("/")[-1])).revision
    response = post(c, jobs[0][0] + f"/calls/{jobs[1][2]}/reconcile", revision=str(jobs[0][1]), input_tokens="1", output_tokens="2", markdown="foreign-call-private-copy")
    assert response.status_code == 404 and "foreign-call-private-copy" not in response.text
    assert c.app.state.job_service.get(int(jobs[0][0].split("/")[-1])).revision == before
    assert jobs[0][1] != d.projects.get(d.project_id).revision
    response = post(c, jobs[0][0] + f"/calls/{jobs[0][2]}/reconcile", revision=str(d.projects.get(d.project_id).revision), input_tokens="1", output_tokens="2")
    assert response.status_code == 409


def large_outline_payload(goal, *, title_suffix=""):
    import json
    # Each individual goal is legal; their aggregate exceeds one chapter.
    return json.dumps([{"chapter_id": None, "title": f"第{index}章" + title_suffix, "goal": goal}
        for index in range(1, 61)], ensure_ascii=False, separators=(",", ":"))


def test_valid_large_outline_stale_conflict_keeps_exact_submitted_copy(production_data):
    from urllib.parse import urlencode
    d, c = production_data, production_data.client
    created = post(c, "/admin/production/new", title="大纲复制测试", mode="precise", chapter_count="60")
    assert created.status_code == 303
    base = created.headers["location"]
    project_id = int(base.rsplit("/", 1)[1])
    saved_text = large_outline_payload("学" * 2000)
    stale_text = large_outline_payload("练" * 2000, title_suffix="</textarea><script>example()</script>")
    assert 204800 < len(saved_text.encode()) < len(stale_text.encode())
    assert len(urlencode({"csrf_token": csrf(c), "revision": "1", "outline_json": stale_text}).encode()) < 2097152
    assert post(c, base + "/outline/save", revision="1", outline_json=saved_text).status_code == 303
    before = d.drafts.freeze_input(project_id)
    assert len(before.outline.items) == 60 and all(item.goal == "学" * 2000 for item in before.outline.items)
    response = post(c, base + "/outline/save", revision="1", outline_json=stale_text)
    assert response.status_code == 409 and response.headers["cache-control"] == "no-store"
    assert 'name="revision" value="1"' in response.text
    assert 'name="revision" value="2"' not in response.text
    assert 'href="' + base + '"' in response.text
    assert d.drafts.freeze_input(project_id) == before
    copied = textarea(response, "outline_json")
    assert len(copied.encode()) == len(stale_text.encode())
    assert copied == stale_text
    assert "</textarea><script>example()</script>" not in response.text


@pytest.mark.parametrize("credential", ["configured_key", "session_fragment", "delivery_code"])
def test_large_outline_copy_still_suppresses_known_credentials(production_data, credential):
    d, c = production_data, production_data.client
    created = post(c, "/admin/production/new", title="大纲凭证过滤", mode="precise", chapter_count="60")
    assert created.status_code == 303
    base = created.headers["location"]
    project_id = int(base.rsplit("/", 1)[1])
    saved_text = large_outline_payload("学" * 2000)
    assert post(c, base + "/outline/save", revision="1", outline_json=saved_text).status_code == 303
    before = d.drafts.freeze_input(project_id)
    secret = {"configured_key": "task9-fake-api-key", "session_fragment": c.cookies.get("coursmith_admin")[:20],
        "delivery_code": "CS-secretfragment"}[credential]
    stale_text = large_outline_payload("练" * 2000, title_suffix=secret)
    assert len(stale_text.encode()) > 204800
    response = post(c, base + "/outline/save", revision="1", outline_json=stale_text)
    assert response.status_code == 409 and response.headers["cache-control"] == "no-store"
    assert textarea(response, "outline_json") == "" and secret not in response.text
    assert d.drafts.freeze_input(project_id) == before
