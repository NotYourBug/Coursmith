"""Real SQLite reservation history and per-send execution gates."""

from dataclasses import replace
from datetime import date
from decimal import Decimal
import hashlib
import json

import pytest

from course_platform.database import transaction
from course_platform.domain import BusinessError
from course_platform.production.models import BriefInput, ReferenceInput
from tests.jobs.fakes import production_settings


def execution(j, **changes):
    from course_platform.jobs.budget import TokenBudget

    settings = production_settings(j.db_path.parent, **changes)
    budget = TokenBudget(j.db_path,clock=j.clock.now)
    j.service.configure_execution(settings,budget)
    j.config = settings.public
    return settings,budget


def connection_call(j, *, settings_changes=None):
    from course_platform.production.prompts import build_connection_request

    settings,budget = execution(j, **(settings_changes or {}))
    job = j.submit(kind="connection_test")
    claim = j.repository.claim_next("budget-worker")
    request = build_connection_request(settings.public)
    with transaction(j.db_path,immediate=True) as conn:
        call_id = budget.begin_call_in_tx(conn,claim,request)
    return settings,budget,job,claim,request,call_id


def test_utf8_fallback_is_payload_bytes_plus_2048(jobs_context):
    from course_platform.production.llm import ModelRequest, serialize_model_payload

    j = jobs_context()
    settings,budget = execution(j)
    request = ModelRequest(messages=({"role":"user","content":"中文😀"},),max_output_tokens=32,config=settings.public)
    assert budget.estimate(request) == len(serialize_model_payload(request)) + 2048 + 32


def test_valid_60_chapters_can_exceed_200000_budget_without_truncation(jobs_context,read_rows):
    j = jobs_context()
    execution(j)
    p = j.projects.create(j.actor,BriefInput(title="合法课程",chapter_count=60),mode="quick")
    j.projects.save_reference(j.actor,p.id,p.revision,None,ReferenceInput(text="材"*20000))
    p = j.projects.get(p.id)
    with pytest.raises(BusinessError) as denied:
        j.submit(p,reserved_tokens=0)
    assert denied.value.code == "token_budget_exceeded"
    assert j.drafts.freeze_input(p.id).references[0].data.text == "材"*20000
    assert read_rows("SELECT * FROM production_jobs") == []
    assert read_rows("SELECT * FROM token_reservations") == []


def test_submission_ignores_forged_reservation_and_reserves_logical_requests(jobs_context,read_rows):
    from course_platform.production.prompts import build_execution_plan

    j = jobs_context()
    settings,budget = execution(j)
    p = j.project()
    frozen = j.drafts.freeze_input(p.id)
    plan = build_execution_plan(frozen,kind="course_draft_generate",chapter_ids=(),config=settings.public)
    expected = sum(budget.estimate(request) for request in plan)
    job = j.submit(p,reserved_tokens=0)
    row = read_rows("SELECT * FROM token_reservations WHERE job_id=?",(job.id,))[0]
    assert row["reserved_tokens"] == expected
    assert row["reserved_tokens"] < expected*3
    again = j.submit(p,reserved_tokens=999999999)
    assert again.id == job.id
    assert len(read_rows("SELECT * FROM token_reservations")) == 1


def test_unconfigured_submission_has_no_caller_amount_fallback(jobs_context,read_rows):
    from course_platform.jobs.service import JobService

    j = jobs_context()
    service = JobService(j.db_path,j.drafts,clock=j.clock.now)
    p = j.project()
    with pytest.raises(BusinessError) as denied:
        service.submit(j.actor,j.request(kind="outline_generate",project_id=p.id,project_revision=p.revision),
            j.config,idempotency_key="unconfigured",reserved_tokens=0)
    assert denied.value.code == "model_not_configured"
    assert read_rows("SELECT * FROM production_jobs") == []


def test_proven_rejection_releases_before_fresh_retry_reservation(jobs_context,read_rows):
    from course_platform.production.llm import Usage

    j = jobs_context()
    settings,budget,job,claim,request,first = connection_call(j)
    total = read_rows("SELECT * FROM token_reservations WHERE job_id=? AND call_id IS NULL",(job.id,))[0]
    assert total["released_tokens"] == total["reserved_tokens"]
    with transaction(j.db_path,immediate=True) as conn:
        budget.settle_in_tx(conn,call_id=first,usage=Usage(0,0),status="rejected")
        second = budget.begin_call_in_tx(conn,claim,request)
    rows = read_rows("SELECT * FROM token_reservations WHERE call_id IS NOT NULL ORDER BY id")
    assert first != second
    assert rows[0]["state"] == "released"
    assert rows[0]["released_tokens"] == rows[0]["reserved_tokens"]
    assert rows[1]["state"] == "reserved"
    assert rows[1]["released_tokens"] == 0
    assert len(read_rows("SELECT * FROM model_calls")) == 2
    assert settings.task_token_limit == 200000


def test_unknown_usage_keeps_reservation_and_blocks_new_spending(jobs_context,read_rows):
    j = jobs_context()
    _,budget,job,claim,request,call = connection_call(j)
    with transaction(j.db_path,immediate=True) as conn:
        budget.settle_in_tx(conn,call_id=call,usage=None,status="unknown")
    row = read_rows("SELECT * FROM token_reservations WHERE call_id=?",(call,))[0]
    assert row["state"] == "unknown"
    assert row["used_tokens"] is None
    assert row["released_tokens"] == 0
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path,immediate=True) as conn:
            budget.begin_call_in_tx(conn,claim,request)
    assert denied.value.code == "model_usage_unknown"
    assert len(read_rows("SELECT * FROM model_calls WHERE job_id=?",(job.id,))) == 1


def test_usage_settlement_is_idempotent_and_rejects_conflicting_release(jobs_context,read_rows):
    from course_platform.production.llm import Usage

    j = jobs_context()
    _,budget,_,_,_,call = connection_call(j)
    with transaction(j.db_path,immediate=True) as conn:
        budget.settle_in_tx(conn,call_id=call,usage=Usage(12,4),status="successful")
    before = read_rows("SELECT * FROM token_reservations WHERE call_id=?",(call,))
    with transaction(j.db_path,immediate=True) as conn:
        budget.settle_in_tx(conn,call_id=call,usage=Usage(12,4),status="successful")
    assert read_rows("SELECT * FROM token_reservations WHERE call_id=?",(call,)) == before
    with pytest.raises(BusinessError):
        with transaction(j.db_path,immediate=True) as conn:
            budget.settle_in_tx(conn,call_id=call,usage=Usage(0,0),status="rejected")
    assert before[0]["used_tokens"] == 16
    assert before[0]["released_tokens"] == before[0]["reserved_tokens"]-16


@pytest.mark.parametrize("usage,status", [(None,"rejected"),(None,"successful"),((1,0),"rejected"),((0,0),"unknown")])
def test_settlement_cannot_fabricate_known_zero(jobs_context,usage,status):
    from course_platform.production.llm import Usage

    j = jobs_context()
    _,budget,_,_,_,call = connection_call(j)
    value = None if usage is None else Usage(*usage)
    with pytest.raises(BusinessError):
        with transaction(j.db_path,immediate=True) as conn:
            budget.settle_in_tx(conn,call_id=call,usage=value,status=status)


def test_price_snapshot_remains_historical_after_rotation(jobs_context,read_rows):
    from course_platform.production.config import PriceSnapshot

    j = jobs_context()
    price = PriceSnapshot(Decimal("0.10"),Decimal("0.20"),"USD",date(2026,10,9))
    _,budget,_,_,_,call = connection_call(j,settings_changes={"price":price})
    expected = '{"as_of":"2026-10-09","currency":"USD","input_per_million":"0.10","output_per_million":"0.20"}'
    assert read_rows("SELECT price_json FROM model_calls WHERE id=?",(call,))[0]["price_json"] == expected
    execution(j,price=PriceSnapshot(Decimal("99"),Decimal("99"),"CNY",date(2026,10,10)))
    assert read_rows("SELECT price_json FROM model_calls WHERE id=?",(call,))[0]["price_json"] == expected
    assert budget is not None


@pytest.mark.parametrize("gate", ["cancel","expiry","drift","context","budget"])
def test_every_call_rechecks_fence_config_context_and_remaining_budget(jobs_context,read_rows,gate):
    from course_platform.production.prompts import build_connection_request
    from course_platform.production.llm import ModelRequest, Usage

    j = jobs_context()
    settings,budget = execution(j)
    job = j.submit(kind="connection_test")
    claim = j.repository.claim_next("guard-worker")
    request = build_connection_request(settings.public)
    if gate == "cancel":
        with transaction(j.db_path,immediate=True) as conn:
            conn.execute("UPDATE production_jobs SET cancel_requested=1 WHERE id=?",(job.id,))
    if gate == "expiry":
        j.clock.advance(seconds=60)
    if gate == "drift":
        j.service.configure_execution(replace(settings,public=settings.public.model_copy(update={"model":"different"})),budget)
    if gate == "context":
        request = ModelRequest(messages=({"role":"user","content":"x"*1000000},),max_output_tokens=32,config=settings.public)
    if gate == "budget":
        with transaction(j.db_path,immediate=True) as conn:
            first = budget.begin_call_in_tx(conn,claim,request)
            budget.settle_in_tx(conn,call_id=first,usage=Usage(199999,0),status="successful")
    before = read_rows("SELECT * FROM model_calls")
    with pytest.raises(BusinessError):
        with transaction(j.db_path,immediate=True) as conn:
            budget.begin_call_in_tx(conn,claim,request)
    assert read_rows("SELECT * FROM model_calls") == before


def test_prompts_send_complete_references_and_only_production_fields(jobs_context):
    from course_platform.production.prompts import build_outline_request, build_chapter_request
    from course_platform.production.llm import serialize_model_payload

    j = jobs_context()
    settings,_ = execution(j)
    p = j.project(outline=True)
    j.projects.save_reference(j.actor,p.id,p.revision,None,ReferenceInput(text="资料-e\u0301\r\n😀",source_note="待核查"))
    frozen = j.drafts.freeze_input(p.id)
    for request in (build_outline_request(frozen,settings.public),
            build_chapter_request(frozen,frozen.chapters[0].id,settings.public)):
        body = json.loads(serialize_model_payload(request))
        assert set(body) == {"messages","model","max_tokens","stream","thinking"}
        assert body["model"] == "deepseek-flash"
        assert body["thinking"] == {"type":"disabled"}
        source = json.loads(body["messages"][1]["content"])
        assert source["references"][0]["data"]["text"] == "资料-e\u0301\r\n😀"
        assert source["brief"]["title"] == "private-title-中文😀"
        assert not {"orders","buyers","admin","tools","api_key"} & set(source)
        assert "fake-private-key" not in str(body)


def test_request_hash_and_original_input_hash_are_retained(jobs_context,read_rows):
    from course_platform.production.llm import serialize_model_payload

    j = jobs_context()
    _,_,job,claim,request,call = connection_call(j)
    row = read_rows("SELECT * FROM model_calls WHERE id=?",(call,))[0]
    assert row["request_hash"] == hashlib.sha256(serialize_model_payload(request)).hexdigest()
    assert row["input_hash"] == read_rows("SELECT input_hash FROM production_jobs WHERE id=?",(job.id,))[0]["input_hash"]
    assert row["attempt_id"] == claim.attempt_id

def test_default_ten_quick_job_is_reserved_below_default_cap(jobs_context,read_rows):
    j = jobs_context()
    execution(j)
    p = j.projects.create(j.actor,BriefInput(title="Agent开发"),mode="quick")
    job = j.submit(p,reserved_tokens=0)
    reservation = read_rows("SELECT * FROM token_reservations WHERE job_id=?",(job.id,))[0]
    assert 0<reservation["reserved_tokens"]<=200000
    assert len(j.drafts.freeze_input(p.id).chapters)==0


@pytest.mark.parametrize("characters", ["😀","\x01","\\","\"","\x01😀\\\"","\u2028e\u0301"],ids=["emoji","control","slash","quote","mixed","combining"])
def test_unknown_item_bound_dominates_exact_nested_wire(jobs_context,characters):
    from dataclasses import replace
    from course_platform.production.models import OutlineView,OutlineItem,ChapterView
    from course_platform.production.prompts import build_execution_plan,build_chapter_request
    from course_platform.production.llm import serialize_model_payload

    j = jobs_context()
    settings,budget = execution(j)
    p = j.projects.create(j.actor,BriefInput(title="Agent开发",chapter_count=1),mode="quick")
    frozen = j.drafts.freeze_input(p.id)
    planned = build_execution_plan(frozen,kind="course_draft_generate",chapter_ids=(),config=settings.public)[1]
    def text(n):
        return (characters*(n//len(characters)+1))[:n]
    chapter_id="00000000-0000-4000-8000-000000000000"
    outlined = replace(frozen,outline=OutlineView(1,1,p.id,(OutlineItem(chapter_id=chapter_id,title=text(200),goal=text(2000)),),False),
        chapters=(ChapterView(chapter_id,None,""),))
    actual = build_chapter_request(outlined,chapter_id,settings.public)
    assert len(serialize_model_payload(planned)) >= len(serialize_model_payload(actual))
    assert budget.estimate(planned) >= budget.estimate(actual)
    source = json.loads(actual.messages[1]["content"])
    for name,want in (("title",text(200)),("goal",text(2000))):
        value = source["chapter"][name]
        if isinstance(value,dict):
            import base64
            assert value["encoding"] == "base64-utf8"
            value = base64.b64decode(value["value"],validate=True).decode()
        assert value == want
    assert set(source) == {"brief","references","chapter","body"}
    assert outlined.input_hash == frozen.input_hash


def test_estimate_only_request_cannot_be_recorded_or_sent(jobs_context,read_rows):
    import asyncio
    import httpx
    from course_platform.production.llm import OpenAIModel
    from course_platform.production.prompts import build_execution_plan
    from tests.jobs.fakes import ModelTransport

    j = jobs_context()
    settings,budget = execution(j)
    p = j.project()
    j.submit(p)
    claim = j.repository.claim_next("plan-worker")
    planned = build_execution_plan(claim.input,kind="course_draft_generate",chapter_ids=(),config=settings.public)[1]
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path,immediate=True) as conn:
            budget.begin_call_in_tx(conn,claim,planned)
    assert denied.value.code == "model_plan_not_executable"
    assert read_rows("SELECT * FROM model_calls") == []
    transport = ModelTransport([])
    async def run():
        async with httpx.AsyncClient(transport=transport) as client:
            return await OpenAIModel(settings,http_client=client).generate(planned)
    with pytest.raises(BusinessError) as denied:
        asyncio.run(run())
    assert denied.value.code == "model_plan_not_executable"
    assert transport.calls == []

def test_effective_pool_is_transferred_without_double_counting(jobs_context,read_rows):
    from course_platform.production.prompts import build_connection_request
    from course_platform.production.llm import Usage

    j=jobs_context()
    settings,budget=execution(j)
    job=j.submit(kind="connection_test")
    claim=j.repository.claim_next("transfer-worker")
    request=build_connection_request(settings.public)
    amount=read_rows("SELECT reserved_tokens FROM token_reservations WHERE job_id=?",(job.id,))[0]["reserved_tokens"]
    with transaction(j.db_path,immediate=True) as conn:
        assert budget.effective_tokens_in_tx(conn,job_id=job.id)==amount
        call=budget.begin_call_in_tx(conn,claim,request)
        assert budget.effective_tokens_in_tx(conn,job_id=job.id)==amount
        budget.settle_in_tx(conn,call_id=call,usage=Usage(12,4),status="successful")
        assert budget.effective_tokens_in_tx(conn,job_id=job.id)==16


def test_late_known_usage_is_idempotent_and_preserves_unknown_history(jobs_context,read_rows):
    from course_platform.production.llm import Usage

    j=jobs_context()
    _,budget,_,claim,request,call=connection_call(j)
    with transaction(j.db_path,immediate=True) as conn:
        budget.settle_in_tx(conn,call_id=call,usage=None,status="unknown")
    with transaction(j.db_path,immediate=True) as conn:
        budget.settle_in_tx(conn,call_id=call,usage=Usage(12,4),status="successful")
    before=read_rows("SELECT * FROM model_calls WHERE id=?",(call,))
    assert before[0]["status"]=="unknown"
    with transaction(j.db_path,immediate=True) as conn:
        budget.settle_in_tx(conn,call_id=call,usage=Usage(12,4),status="successful")
    assert read_rows("SELECT * FROM model_calls WHERE id=?",(call,))==before
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path,immediate=True) as conn:
            budget.begin_call_in_tx(conn,claim,request)
    assert denied.value.code=="model_usage_unknown"


def test_configured_output_limit_cannot_be_bypassed_by_request(jobs_context):
    from course_platform.production.llm import ModelRequest
    from course_platform.production.prompts import build_chapter_request

    j=jobs_context()
    base=production_settings(j.db_path.parent)
    settings,budget=execution(j,public=base.public.model_copy(update={"chapter_output_tokens":32}))
    p=j.project(outline=True)
    j.service.submit(j.actor,j.request(kind="chapters_generate",project_id=p.id,project_revision=p.revision),
        settings.public,idempotency_key="output-key",reserved_tokens=0)
    claim=j.repository.claim_next("output-worker")
    valid=build_chapter_request(claim.input,claim.input.chapters[0].id,settings.public)
    forged=ModelRequest(messages=valid.messages,max_output_tokens=4000,config=settings.public)
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path,immediate=True) as conn:
            budget.begin_call_in_tx(conn,claim,forged)
    assert denied.value.code=="invalid_model_request"


@pytest.mark.parametrize("number", [True,-1,2**63,"1"])
def test_reservation_limits_revalidate_before_sql(jobs_context,number):
    j=jobs_context()
    _,budget=execution(j)
    job=j.submit(kind="connection_test")
    with pytest.raises(BusinessError):
        with transaction(j.db_path,immediate=True) as conn:
            budget.reserve_in_tx(conn,job_id=job.id,planned_tokens=number,limit=200000)

def test_claim_config_must_match_persisted_frozen_job_config(jobs_context,read_rows):
    from course_platform.production.prompts import build_connection_request

    j=jobs_context()
    settings,budget=execution(j)
    j.submit(kind="connection_test")
    claim=j.repository.claim_next("frozen-config-worker")
    new_public=settings.public.model_copy(update={"context_tokens":64000})
    j.service.configure_execution(replace(settings,public=new_public),budget)
    forged=replace(claim,config=new_public)
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path,immediate=True) as conn:
            budget.begin_call_in_tx(conn,forged,build_connection_request(new_public))
    assert denied.value.code=="model_config_changed"
    assert read_rows("SELECT * FROM model_calls")==[]


def test_aggregate_arithmetic_never_uses_overflowing_sql_sum(jobs_context):
    from course_platform.production.llm import Usage

    j=jobs_context()
    _,budget,job,claim,request,first=connection_call(j)
    with transaction(j.db_path,immediate=True) as conn:
        budget.settle_in_tx(conn,call_id=first,usage=Usage(12,4),status="successful")
        second=budget.begin_call_in_tx(conn,claim,request)
        budget.settle_in_tx(conn,call_id=second,usage=Usage(12,4),status="successful")
        # Existing historical rows may each have a valid signed SQLite integer;
        # adding them with SQL SUM would overflow before applying the next gate.
        conn.execute("UPDATE model_calls SET input_tokens=?,output_tokens=0",(2**63-1,))
        conn.execute("UPDATE token_reservations SET used_tokens=?,released_tokens=0 WHERE call_id IS NOT NULL",(2**63-1,))
        assert budget.effective_tokens_in_tx(conn,job_id=job.id)==2*(2**63-1)
        with pytest.raises(BusinessError) as denied:
            budget.begin_call_in_tx(conn,claim,request)
        assert denied.value.code=="token_budget_exceeded"


def test_flash_estimator_counts_controlled_thinking_wire_bytes(jobs_context):
    from course_platform.production.llm import ModelRequest

    j=jobs_context()
    settings,budget=execution(j)
    request=ModelRequest(messages=({"role":"user","content":"中文😀"},),max_output_tokens=32,config=settings.public)
    expected=b'{"messages":[{"role":"user","content":"'+"中文😀".encode()+b'"}],"model":"deepseek-flash","max_tokens":32,"stream":false,"thinking":{"type":"disabled"}}'
    assert budget.estimate(request)==len(expected)+2048+32


def test_legacy_frozen_job_is_not_silently_aliased_to_flash(jobs_context,read_rows):
    from course_platform.database import to_db_time
    from course_platform.jobs.models import canonical_json
    from course_platform.production.prompts import build_connection_request

    j=jobs_context()
    settings,budget=execution(j)
    old=settings.public.model_copy(update={"model":"deepseek-chat"})
    old_json=canonical_json(old.model_dump(warnings=False))
    with transaction(j.db_path,immediate=True) as conn:
        job_id=conn.execute("""INSERT INTO production_jobs
            (actor_admin_id,kind,input_hash,input_json,config_json,request_digest,created_at)
            VALUES (?,'connection_test',?,'{}',?,?,?)""",
            (j.actor.admin_id,hashlib.sha256(b'{}').hexdigest(),old_json,"a"*64,to_db_time(j.clock.now()))).lastrowid
        budget.reserve_in_tx(conn,job_id=job_id,planned_tokens=10000,limit=200000)
    claim=j.repository.claim_next("legacy-profile-worker")
    before=read_rows("SELECT * FROM token_reservations WHERE job_id=?",(job_id,))
    with pytest.raises(BusinessError) as denied:
        with transaction(j.db_path,immediate=True) as conn:
            budget.begin_call_in_tx(conn,claim,build_connection_request(old))
    assert denied.value.code=="model_config_changed"
    assert read_rows("SELECT config_json FROM production_jobs WHERE id=?",(job_id,))[0]["config_json"]==old_json
    assert read_rows("SELECT * FROM token_reservations WHERE job_id=?",(job_id,))==before
    assert read_rows("SELECT * FROM model_calls")==[]
