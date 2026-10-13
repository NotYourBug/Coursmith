"""The real installed wheel must operate M2A without source or model traffic."""

import json
import os
from pathlib import Path
import subprocess
import time
import zipfile

import pytest

from course_platform.database import open_readonly
from contextlib import closing
from tests.test_wheel_install import installed_release  # explicit shared fixture, no conftest widening
from tests.production.test_privacy import KEY_SENTINEL, SOURCE_SENTINEL


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.packaging
def test_fresh_wheel_runs_workbench_outside_source_tree(installed_release, server_factory):
    outside, python, _, env = installed_release
    env = env.copy()
    env.update(COURSE_DATABASE=str(outside / "workbench.db"), COURSE_CONTENT_ROOT=str(outside / "private-content"),
               COURSE_ENVIRONMENT="test", COURSE_SITE_ORIGIN="http://testserver", COURSE_TRUSTED_PROXY_CIDRS="",
               COURSE_BASE_URL="https://api.deepseek.com/", DEEPSEEK_API_KEY="", COURSE_MODEL="deepseek-flash",
               COURSE_MODEL_CONTEXT_TOKENS="", COURSE_TASK_TOKEN_LIMIT="200000", COURSE_MODEL_PRICE_JSON="")
    result = subprocess.run([str(python), "-X", "utf8", "-c", INSTALLED_WORKBENCH], cwd=outside,
                            env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    proof = json.loads(result.stdout.splitlines()[-1])
    module = Path(proof["module_path"]).resolve()
    assert module.is_relative_to(outside / ".venv") and not module.is_relative_to(ROOT)
    assert proof["web_status"] == 200 and proof["human_review_required"] is True
    assert proof["snapshot_count"] == 2 and proof["model_calls"] == 0
    for args in (["worker", "--help"], ["migrate", "--check-only"]):
        cli = subprocess.run([str(python), "-X", "utf8", "-m", "course_platform.cli", *args], cwd=outside,
                             env=env, capture_output=True, text=True, timeout=30)
        assert cli.returncode == 0, cli.stdout + cli.stderr
        if args[0] == "worker":
            assert "--api-key" not in cli.stdout and "--shell" not in cli.stdout
    # Execute the installed fixed CLI, not a source Worker or test fake flag.
    log = outside / "installed-worker.log"
    with log.open("wb") as output:
        process = subprocess.Popen([str(python), "-X", "utf8", "-m", "course_platform.cli", "worker"],
            cwd=outside, env=env, stdout=output, stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with closing(open_readonly(outside / "workbench.db")) as connection:
                    state = connection.execute("SELECT state FROM production_jobs WHERE id=?", (proof["job_id"],)).fetchone()[0]
                if state == "succeeded":
                    break
                assert process.poll() is None, log.read_text(encoding="utf8")
                time.sleep(.05)
            else:
                pytest.fail("Installed fixed CLI did not complete local validation: " + log.read_text(encoding="utf8"))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    assert '"status": "local_only"' in log.read_text(encoding="utf8")
    with closing(open_readonly(outside / "workbench.db")) as connection:
        assert connection.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM token_reservations WHERE call_id IS NOT NULL").fetchone()[0] == 0
    # A real installed factory also serves the workbench across a socket.
    import httpx
    import re
    with server_factory(outside / "private-content", outside / "workbench.db", python=python, cwd=outside, cli=False) as site:
        with httpx.Client(base_url=site.url, trust_env=False, follow_redirects=False) as client:
            login = client.get("/admin/login")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1)
            assert client.post("/admin/login", data=dict(username="owner", password="example-pass-123", csrf_token=csrf),
                               headers={"Origin": site.url}).status_code == 303
            for path in ("/admin/production", "/admin/jobs", "/admin/jobs/model", "/static/admin/production.js", "/static/admin/production.css"):
                assert client.get(path).status_code == 200, path
    print("Installed M2A native dual modes, immutable snapshot, fixed local-only CLI and module origin passed: " + result.stdout)
    assert (ROOT / "docs/operations/m2a-runbook.md").is_file(), "Installed M2A operator handoff is missing"


@pytest.mark.packaging
def test_model_configuration_and_fake_credentials_not_in_wheel(installed_release):
    _, _, wheel, _ = installed_release
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            assert not name.startswith(("tests/", ".superpowers/", ".env", "data/", "content/")), name
            assert not name.endswith((".db", ".sqlite", ".sqlite3", "config.json")), name
            raw = archive.read(name)
            for secret in (KEY_SENTINEL, SOURCE_SENTINEL, "task9-fake-api-key", "task10-fake-key", "fake-private-key"):
                assert secret.encode() not in raw, name


@pytest.mark.packaging
def test_complete_dependency_handoff_matches_actual_installed_runtime(installed_release):
    outside, python, _, env = installed_release
    result = subprocess.run([str(python), "-X", "utf8", "-c",
        "import importlib.metadata as m,json;print(json.dumps({d.name.lower().replace('_','-'):d.version for d in m.distributions()}))"],
        cwd=outside, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    pins = {}
    for line in (ROOT / "docs/operations/m2a-validated-requirements.txt").read_text(encoding="utf8").splitlines():
        if line.strip() and not line.startswith("#"):
            name, version = line.split("==")
            pins[name.lower().replace("_", "-")] = version
    missing = {name: version for name, version in actual.items() if pins.get(name) != version}
    assert not missing, "Complete actual installed dependency handoff is missing: " + repr(missing)
    checked = subprocess.run([str(python), "-m", "pip", "check"], cwd=outside, env=env,
                             capture_output=True, text=True, timeout=30)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    print("Fresh installed dependency inventory: " + json.dumps(actual, sort_keys=True))


@pytest.mark.packaging
def test_installed_sdk_adapter_keeps_flash_wire_and_private_diagnostics(installed_release):
    outside, python, _, env = installed_release
    result = subprocess.run([str(python), "-X", "utf8", "-c", INSTALLED_SDK], cwd=outside,
                            env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "installed SDK Flash/thinking/output/usage/privacy passed" in result.stdout
    assert KEY_SENTINEL not in result.stdout + result.stderr and SOURCE_SENTINEL not in result.stdout + result.stderr
    print(result.stdout)


INSTALLED_SDK = r'''
import asyncio,io,json,logging
from pathlib import Path
import httpx
from course_platform.settings import Settings
from course_platform.production.config import load_production_settings
from course_platform.production.llm import OpenAIModel,serialize_model_payload
from course_platform.production.prompts import build_connection_request
key='sk-task13-private-credential-781f782f'
config=load_production_settings(Settings('https://api.deepseek.com/',key,Path('sdk-content'),Path('sdk.db'),72,'test'),env={'COURSE_MODEL':'deepseek-flash','COURSE_MODEL_CONTEXT_TOKENS':'1000000','COURSE_TASK_TOKEN_LIMIT':'200000','COURSE_MODEL_PRICE_JSON':''})
request=build_connection_request(config.public)
output=io.StringIO();handler=logging.StreamHandler(output)
for name in ('openai._base_client','httpx','httpcore.connection'):
    logger=logging.getLogger(name);logger.setLevel(logging.DEBUG);logger.addHandler(handler)
calls=[]
def respond(wire):
    calls.append(wire.content)
    assert wire.content==serialize_model_payload(request)
    payload=json.loads(wire.content)
    assert payload['model']=='deepseek-flash' and payload['thinking']=={'type':'disabled'}
    assert payload['max_tokens']==32 and payload['stream'] is False and 'tools' not in payload
    return httpx.Response(200,json={'id':'installed-sdk','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'连接正常'}}],'usage':{'prompt_tokens':12,'completion_tokens':4,'total_tokens':16}})
async def run():
    client=httpx.AsyncClient(transport=httpx.MockTransport(respond),trust_env=False)
    result=await OpenAIModel(config,http_client=client).generate(request)
    assert result.text=='连接正常' and result.usage.input_tokens==12 and result.usage.output_tokens==4
asyncio.run(run())
assert len(calls)==1 and key not in output.getvalue() and '连接正常' not in output.getvalue()
print('installed SDK Flash/thinking/output/usage/privacy passed')
'''


INSTALLED_WORKBENCH = r'''
import hashlib,json,re,socket,sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
from contextlib import closing
from fastapi.testclient import TestClient
import course_platform
from course_platform.app import create_app
from course_platform.settings import load_settings
from course_platform.database import open_readonly,backup_database,check_database
from course_platform.domain import Actor
from course_platform.jobs.models import JobRequest
from course_platform.jobs.repository import JobRepository
from course_platform.jobs.worker import maintain
from course_platform.production.snapshots import SnapshotService
original_connect,original_dns=socket.socket.connect,socket.getaddrinfo
def connect(sock,address):
    if isinstance(address,tuple) and address[0] not in ('127.0.0.1','::1','localhost'):
        raise AssertionError('Installed acceptance may only use loopback')
    return original_connect(sock,address)
def dns(host,*args,**kwargs):
    if host not in ('127.0.0.1','::1','localhost',None): raise AssertionError('No external resolution')
    return original_dns(host,*args,**kwargs)
socket.socket.connect,socket.getaddrinfo=connect,dns
assert Path(course_platform.__file__).resolve().is_relative_to(Path(sys.prefix))
assert not (Path.cwd()/'course_platform').exists()
settings=load_settings()
app=create_app(settings)
body='## 学习目标\n理解\n\n## 讲解\n中文😀\n\n## 案例\n案例\n\n## 练习\n思考\n\n## 总结\n回顾\n'
with TestClient(app,client=('198.51.100.7',50000),follow_redirects=False) as client:
    app.state.admin_service.initialize_owner('owner','example-pass-123')
    page=client.get('/admin/login')
    csrf=re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
    assert client.post('/admin/login',data=dict(username='owner',password='example-pass-123',csrf_token=csrf),headers={'Origin':'http://testserver'}).status_code==303
    def post(path,**data):
        response=client.post(path,data=dict(csrf_token=client.cookies.get('coursmith_admin_csrf'),**data),headers={'Origin':'http://testserver'})
        assert response.status_code==303, (path,response.status_code,response.text)
        return response
    listing=client.get('/admin/production')
    assert listing.status_code==200 and '制作项目' in listing.text and listing.headers['cache-control']=='no-store'
    actor=Actor(1,'installed-proof');retained=[]
    for mode in ('quick','precise'):
        created=post('/admin/production/new',title='安装课程😀',mode=mode,chapter_count='1')
        base=created.headers['location'];pid=int(base.rsplit('/',1)[1])
        post(base+'/references/save',revision=str(app.state.production_service.get(pid).revision),text='私有资料\r\n😀',source_note='自有笔记',source_status='owner_noted')
        post(base+'/outline/save',revision=str(app.state.production_service.get(pid).revision),outline_json=json.dumps([dict(chapter_id=None,title='同名章',goal='学习')]))
        frozen=app.state.draft_service.freeze_input(pid);chapter=frozen.chapters[0]
        post(base+'/chapters/'+chapter.id+'/save',body_revision='',markdown=body)
        if mode=='precise': post(base+'/outline/confirm',revision=str(app.state.production_service.get(pid).revision))
        frozen=app.state.draft_service.freeze_input(pid)
        location=post(base+'/snapshots',revision=str(frozen.project_revision)).headers['location']
        view=client.get(location); assert view.status_code==200 and view.headers['cache-control']=='no-store'
        snapshots=SnapshotService(settings.database_path,app.state.draft_service)
        snapshot=snapshots.get(actor,int(location.rsplit('/',1)[1]))
        assert snapshots.read_snapshot_payload(snapshot.id)==frozen
        assert hashlib.sha256(snapshot.payload_json.encode()).hexdigest()==snapshot.hash
        assert snapshot.check.human_review_required and not snapshot.check.blocking
        assert frozen.outline.confirmed is (mode=='precise')
        retained.append((snapshot,frozen))
        for path in (base,base+'/chapters/'+chapter.id,base+'/preview','/admin/jobs','/admin/jobs/model','/static/admin/production.js','/static/admin/production.css'):
            assert client.get(path).status_code==200,path
    local_request=JobRequest(kind='draft_validate',project_id=pid,project_revision=frozen.project_revision)
    diagnostic=app.state.job_service.submit(actor,local_request,app.state.production_settings.public,idempotency_key='installed-readonly-diagnostic',reserved_tokens=0)
    clock=[datetime.now(timezone.utc)];base_clock=clock[0]
    repository=JobRepository(settings.database_path,clock=lambda:clock[0])
    claim=repository.claim_next('installed-diagnostic');assert claim.job_id==diagnostic.id
    app.state.recovery_job_service.repository.clock=lambda:clock[0]
    def durable_history():
        with closing(open_readonly(settings.database_path)) as connection:
            names=[row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name") if row[0] not in ('admin_sessions','request_limits','csrf_challenges')]
            return {name:[tuple(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY 1')] for name in names}
    history=durable_history()
    for age,status in ((59,'fresh'),(60,'lease_expired'),(119,'lease_expired'),(120,'heartbeat_overdue')):
        clock[0]=base_clock+timedelta(seconds=age)
        for path in ('/admin/jobs',f'/admin/jobs/{diagnostic.id}'):
            response=client.get(path)
            assert response.status_code==200 and response.headers['cache-control']=='no-store'
            assert f'data-worker-status="{status}"' in response.text
            if age>=60: assert '任务记录仍为 running' in response.text and '正在执行；' not in response.text
        assert durable_history()==history
    assert app.state.job_service.get(diagnostic.id).state=='running'
    assert maintain(repository,app.state.token_budget)==1
    idle=client.get('/admin/jobs')
    assert '不能据此确认 worker 此刻在线或失联' in idle.text and 'data-worker-status="heartbeat_overdue"' not in idle.text
    job=app.state.job_service.submit(actor,local_request,app.state.production_settings.public,idempotency_key='installed-local-validation',reserved_tokens=0)
with closing(open_readonly(settings.database_path)) as connection:
    assert connection.execute('SELECT count(*) FROM outline_confirmations').fetchone()[0]==1
    assert connection.execute('SELECT count(*) FROM courses').fetchone()[0]==0
    assert connection.execute('SELECT count(*) FROM model_calls').fetchone()[0]==0
    count=connection.execute('SELECT count(*) FROM production_snapshots').fetchone()[0]
def graph(path):
    with closing(open_readonly(path)) as connection:
        names=[row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name:[tuple(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY 1')] for name in names}
before=graph(settings.database_path)
backup_database(settings.database_path,Path('installed-backup.db'))
backup_database(Path('installed-backup.db'),Path('installed-restored.db'))
assert graph(Path('installed-restored.db'))==before and check_database(Path('installed-restored.db'))['foreign_keys']=='ok'
from course_platform.production.drafts import DraftService
recovered=SnapshotService(Path('installed-restored.db'),DraftService(Path('installed-restored.db')))
for snapshot,frozen in retained:
    assert recovered.get(actor,snapshot.id)==snapshot and recovered.read_snapshot_payload(snapshot.id)==frozen
assert graph(Path('installed-restored.db'))==before
print(json.dumps(dict(module_path=course_platform.__file__,web_status=listing.status_code,snapshot_count=count,human_review_required=snapshot.check.human_review_required,model_calls=0,job_id=job.id),ensure_ascii=False))
'''
