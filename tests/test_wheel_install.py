"""The installed artifact must deliver every consumer without source fallback."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest


@pytest.fixture(scope="module")
def installed_release(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    outside = tmp_path_factory.mktemp("installed-release")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONUTF8"] = "1"
    built = subprocess.run([sys.executable, "-X", "utf8", "-m", "build", "--wheel", "--outdir", str(outside / "dist")],
                           cwd=root, env=env, capture_output=True, text=True, timeout=180)
    assert built.returncode == 0, "Wheel build environment failed: " + built.stderr[-2000:]
    wheel = next((outside / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        shipped = set(archive.namelist())
    required = {path.relative_to(root).as_posix() for directory in ("templates", "static", "migrations")
                for path in (root / "course_platform" / directory).rglob("*") if path.is_file()
                and "__pycache__" not in path.parts}
    assert required <= shipped, "Installed wheel omits delivery resources: " + repr(sorted(required - shipped))
    assert not any(name.startswith("tests/") or "frozen_original_access" in name for name in shipped)
    subprocess.run([sys.executable, "-m", "venv", str(outside / "venv")], check=True, timeout=60)
    python = outside / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    install_args = [str(python), "-m", "pip", "install", "--timeout", "10", "--retries", "1"]
    if env.get("COURSMITH_WHEELHOUSE"):
        install_args += ["--no-index", "--find-links", env["COURSMITH_WHEELHOUSE"]]
    installed = subprocess.run(install_args + ["-c", str(root / "docs/operations/task12-validated-requirements.txt"),
                                               str(wheel), "httpx2==2.13.1"],
                               cwd=outside, env=env, capture_output=True, text=True, timeout=240)
    assert installed.returncode == 0, "Fresh installation environment failed: " + installed.stderr[-2000:]
    return outside, python, wheel, env


@pytest.mark.packaging
def test_wheel_installed_site_has_all_pages_and_assets(installed_release, server_factory, release_content):
    outside, python, wheel, env = installed_release
    root = Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(wheel) as archive:
        shipped = set(archive.namelist())
    required = {path.relative_to(root).as_posix() for directory in ("templates", "static", "migrations")
                for path in (root / "course_platform" / directory).rglob("*") if path.is_file()
                and "__pycache__" not in path.parts}
    assert required <= shipped, "Installed wheel omits delivery resources: " + repr(sorted(required - shipped))
    content = outside / "content"
    shutil.copytree(release_content, content / "fixture-course")
    shutil.copyfile(root / "tests/frozen_original_access.py", outside / "frozen_original_access.py")
    result = subprocess.run([str(python), "-X", "utf8", "-c", INSTALLED_CONSUMERS],
                            cwd=outside, env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr[-4000:]
    assert "installed modern + resolved short + standalone migrated consumers passed" in result.stdout
    print(result.stdout)
    # Actual installed factory on a socket, from outside source cwd.
    import httpx
    with server_factory(content, outside / "modern.db", python=python, cwd=outside, cli=False, tls=True) as site:
        for path in ("/", "/help", "/admin/login", "/access", "/access/restore", "/courses/fixture-course",
                     "/courses/fixture-course/chapters/1", "/courses/fixture-course/chapters/intro.html",
                     "/courses/fixture-course/lessons/unit/intro.html", "/courses/fixture-course/assets/theme.css",
                     "/static/admin/admin.css", "/static/admin/admin.js", "/static/site.css"):
            response = httpx.get(site.url + path, verify=False, trust_env=False, timeout=10)
            assert response.status_code == 200, path


INSTALLED_CONSUMERS = r'''
import hashlib, importlib.metadata as metadata, json, re, sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from contextlib import closing
from fastapi.testclient import TestClient
import course_platform
from course_platform.app import create_app
from course_platform.settings import Settings
from course_platform.database import initialize_database, migrate_database, sync_course, open_readonly, check_database
from course_platform.content import CourseManifest
from course_platform.admin.auth import AdminService
from course_platform.domain import Actor
from course_platform.operations.products import ProductService, AccessPolicy, SalesChannel, SalesChecklist
from course_platform.operations.orders import OrderService, OrderInput
from frozen_original_access import AccessService as Original
from zoneinfo import ZoneInfo
assert 'site-packages' in str(Path(course_platform.__file__).resolve())
assert Path(sys.prefix).resolve() in Path(course_platform.__file__).resolve().parents
assert not (Path.cwd()/'course_platform').exists()
assert not any('worktrees' in p or p.endswith('智课工坊') for p in sys.path)
assert ZoneInfo('Asia/Shanghai').key == 'Asia/Shanghai'
package = Path('content/fixture-course').resolve()
manifest = CourseManifest.model_validate_json((package/'manifest.json').read_bytes())
actor = Actor(1, 'installed-owner')
checklist = SalesChecklist(quality=True, sources=True, ai=True, mobile=True, downloads=True)
def setup(db):
    app = create_app(Settings('', '', package.parent, db.resolve(), 72, 'test', 'https://testserver'))
    client = TestClient(app, base_url='https://testserver', client=('198.51.100.8',50001), follow_redirects=False)
    client.__enter__()
    AdminService(db).initialize_owner('owner', 'example-pass-123')
    products = app.state.product_service
    with closing(open_readonly(db)) as c:
        pid = c.execute('SELECT id FROM products').fetchone()[0]
    draft = products.get_product(pid)
    cat = products.save_category(actor,None,None,'installed-technical','技术',0,True)
    data = draft.data.model_copy(update=dict(category_id=cat, synopsis='Course',audience='Learner',prerequisites='None',
        outcomes=['Practice'],ai_disclosure='AI assisted',support_text='Support',channels=[SalesChannel(name='Shop',url='https://shop.example/course')],
        policy=AccessPolicy(access_mode='days',access_days=30,online=True,pdf=False,zip=False,update_policy='current_version')))
    revised = products.update(actor,pid,draft.revision,data)
    product = products.activate(actor,pid,revised.revision,checklist)
    assert check_database(db)['version'] == 5
    return app,client,product
def public(client,path,**data):
    client.get('/access/restore' if 'restore' in path else '/access')
    return client.post(path,data=dict(csrf_token=client.cookies.get('coursmith_public_csrf'),**data),headers={'Origin':'https://testserver'})
def owner(client):
    page=client.get('/admin/login')
    token=re.search(r'name="csrf_token" value="([^"]+)"',page.text).group(1)
    assert client.post('/admin/login',data=dict(username='owner',password='example-pass-123',csrf_token=token),headers={'Origin':'https://testserver'}).status_code==303
def post(client,path,**data):
    return client.post(path,data=dict(csrf_token=client.cookies.get('coursmith_admin_csrf'),**data),headers={'Origin':'https://testserver'})
db=Path('modern.db')
app,buyer,product=setup(db)
admin=TestClient(app,base_url='https://testserver',client=('198.51.100.7',50000),follow_redirects=False)
owner(admin)
order=post(admin,'/admin/orders/new',channel='shop',shop_id='shop',external_order_id='installed-proof',product_id=str(product.id),
    paid_cents='100',paid_at=datetime.now(timezone.utc).isoformat(),note='Checked payment',idempotency_key='record',confirm='on')
assert order.status_code==303
issued=post(admin,order.headers['location']+'/issue',revision='1',idempotency_key='issue')
assert issued.status_code==200
code=re.search(r'CS-[A-Za-z0-9_-]{32}',issued.text).group()
redeemed=public(buyer,'/access/redeem',code=code,course_slug='fixture-course')
assert redeemed.status_code==200
old_key=re.search(r'LK-[A-Za-z0-9_-]{43}',redeemed.text).group()
assert buyer.get('/learn/fixture-course/chapters/intro.html').status_code==200
assert buyer.get('/learn/fixture-course/lessons/unit/intro.html').status_code==200
assert buyer.post('/learn/fixture-course/chapters/1/progress',data=dict(completed='true',csrf_token=buyer.cookies.get('course_csrf_fixture-course')),
    headers={'Origin':'https://testserver'}).status_code==303
with closing(open_readonly(db)) as c:
    before=tuple(c.execute('SELECT expires_at,issued_policy_json,verified_at,verified_by,verified_reason FROM entitlements').fetchone())
reset=post(admin,'/admin/entitlements/1/reset-credential',revision='1',reason='Verified actual order holder',idempotency_key='reset',confirm='on')
assert reset.status_code==200
key=re.search(r'LK-[A-Za-z0-9_-]{43}',reset.text).group()
assert buyer.get('/learn/fixture-course').status_code==403
assert public(buyer,'/access/restore',credential=old_key).status_code==403
assert public(buyer,'/access/restore',credential=key).status_code==200
assert '已完成' in buyer.get('/learn/fixture-course').text
with closing(open_readonly(db)) as c:
    assert tuple(c.execute('SELECT expires_at,issued_policy_json,verified_at,verified_by,verified_reason FROM entitlements').fetchone())==before
for path in ('/admin','/admin/audit','/admin/categories','/admin/products','/admin/products/new','/admin/products/1',
    '/admin/orders','/admin/orders/new','/admin/orders/1','/admin/code-batches','/admin/code-batches/new','/admin/code-batches/1',
    '/admin/entitlements/1','/admin/legacy-codes','/admin/account/password'):
    assert admin.get(path).status_code==200, path
buyer.__exit__(None,None,None); admin.close()
# Independent original producer creates genuinely short code and unassociated session.
legacy=Path('legacy.db')
initialize_database(legacy); sync_course(manifest,package,legacy)
old=Original(legacy)
short=old.create_access_code(manifest.course_id,datetime.now(timezone.utc)+timedelta(days=10))
session=old.redeem_access_code(old.create_access_code(manifest.course_id),manifest.course_id)
old.record_progress(session.session_id,manifest.course_id,1,True)
migrate_database(legacy,backup_path=Path('before-legacy.db'))
app,client,product=setup(legacy)
owner(client)
with closing(open_readonly(legacy)) as c:
    code_id=c.execute('SELECT id FROM access_codes WHERE code_hash=?',(hashlib.sha256(short.encode()).hexdigest(),)).fetchone()[0]
    assert c.execute('SELECT source_code_id FROM entitlements WHERE id=1').fetchone()[0] is None
resolved=post(client,f'/admin/legacy-codes/{code_id}/resolve',revision='1',purpose='gift',verification_reason='Original recipient checked',
    confirm='on',access_mode='days',access_days='30',online='on',update_policy='current_version')
assert resolved.status_code==303
assert public(client,'/access/redeem',code=short,course_slug='fixture-course').status_code==200
assert client.get('/learn/fixture-course/chapters/1').status_code==200
verified=post(client,'/admin/entitlements/1/verify-legacy',revision='1',purpose='gift',verification_reason='Independent original holder checked',
    idempotency_key='verify',confirm='on',access_mode='days',access_days='7',online='on',update_policy='current_version',
    expires_at=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat())
assert verified.status_code==303 and 'LK-' not in verified.text
reset=post(client,'/admin/entitlements/1/reset-credential',revision='2',reason='Verified independent holder',idempotency_key='legacy-reset',confirm='on')
assert reset.status_code==200
key=re.search(r'LK-[A-Za-z0-9_-]{43}',reset.text).group()
assert public(client,'/access/restore',credential=key).status_code==200
assert '已完成' in client.get('/learn/fixture-course').text
client.__exit__(None,None,None)
print('installed modern + resolved short + standalone migrated consumers passed')
print('Installed runtime: '+sys.version.split()[0])
print(json.dumps(sorted((d.name,d.version) for d in metadata.distributions())))
'''
