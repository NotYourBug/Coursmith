"""Security boundaries use real Starlette streams and migrated SQLite files."""

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.requests import Request

from course_platform.database import transaction
from course_platform.domain import BusinessError
from course_platform.admin.security import RateLimiter
from course_platform.security import (
    CsrfService,
    check_origin,
    parse_unique_form,
    read_limited_body,
    source_key,
)


def request(headers=(), peer="198.51.100.7", receive=None, method="POST"):
    return Request(
        {"type": "http", "method": method, "path": "/", "headers": list(headers),
         "client": (peer, 1234) if peer else None, "scheme": "https",
         "server": ("courses.example", 443), "query_string": b""},
        receive=receive,
    )


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.mark.parametrize("media,limit", [
    (b"application/x-www-form-urlencoded", 65536), (b"application/json", 16384),
])
def test_stream_stops_before_parsing_oversize_body(media, limit):
    chunks = iter([b"x" * limit, b"x", b"must-not-read"])
    consumed = []

    async def receive():
        chunk = next(chunks)
        consumed.append(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    with pytest.raises(BusinessError) as err:
        asyncio.run(read_limited_body(request([(b"content-type", media)], receive=receive), limit))
    assert err.value.status_code == 413
    assert len(consumed) == 2


@pytest.mark.parametrize("media,limit", [
    (b"application/x-www-form-urlencoded; charset=UTF-8", 65536),
    (b"application/json", 16384),
])
def test_exact_body_boundary_is_accepted(media, limit):
    async def receive():
        return {"type": "http.request", "body": b"a" * limit, "more_body": False}

    assert asyncio.run(read_limited_body(request([(b"content-type", media)], receive=receive), limit)) == b"a" * limit


@pytest.mark.parametrize("headers,status", [
    ([], 415), ([(b"content-type", b"text/plain")], 415),
    ([(b"content-type", b"multipart/form-data")], 415),
    ([(b"content-type", b"application/json; charset=latin-1")], 415),
    ([(b"content-type", b"application/json"), (b"content-type", b"application/x-www-form-urlencoded")], 400),
    ([(b"content-type", b"application/json"), (b"content-length", b"16385")], 413),
    ([(b"content-type", b"application/json"), (b"content-length", b"-1")], 400),
    ([(b"content-type", b"application/json"), (b"content-length", b"1"), (b"content-length", b"2")], 400),
])
def test_bad_body_headers_fail_before_reading(headers, status):
    async def receive():
        pytest.fail("Invalid headers must be rejected before consuming the body")

    with pytest.raises(BusinessError) as err:
        asyncio.run(read_limited_body(request(headers, receive=receive), 65536))
    assert err.value.status_code == status


@pytest.mark.parametrize("body", [
    b"product_id=1&product_id=2", b"code=first&code=second",
    b"csrf_token=a&csrf_token=b", b"csrf_token=a&csrf%5ftoken=b", b"title=a&title=b",
])
def test_duplicate_security_fields_are_rejected(body):
    with pytest.raises(BusinessError) as err:
        parse_unique_form(body)
    assert err.value.status_code == 400


@pytest.mark.parametrize("body", [
    b'{"csrf_token":"a"}', b"code", b"=value", b"a=1&&b=2",
    b"a=%", b"a=%0g", b"a=%FF", b"a=\xff", b"a=x%00y",
])
def test_malformed_form_cannot_be_coerced(body):
    with pytest.raises(BusinessError) as err:
        parse_unique_form(body)
    assert err.value.status_code == 400


def test_form_preserves_blanks_and_decodes_utf8_once():
    assert parse_unique_form(b"code=CS-ab%2Bcd&title=%E8%AF%BE+one&notes=&literal=%2526") == {
        "code": "CS-ab+cd", "title": "课 one", "notes": "", "literal": "%26",
    }
    assert parse_unique_form(b"") == {}


@pytest.mark.parametrize("headers", [
    [], [(b"origin", b"null")], [(b"origin", b"https://evil.example")],
    [(b"origin", b"https://courses.example.evil")],
    [(b"origin", b"https://courses.example/path")],
    [(b"origin", b"https://owner@courses.example")],
    [(b"origin", b"https://courses.example:444")],
    [(b"origin", b"https://courses.example, https://evil.example")],
    [(b"origin", b"https://courses.example"), (b"origin", b"https://courses.example")],
    [(b"referer", b"https://courses.example/form"), (b"host", b"courses.example")],
])
def test_origin_requires_one_exact_site_origin(headers):
    with pytest.raises(BusinessError) as err:
        check_origin(request(headers), "https://courses.example")
    assert err.value.status_code == 403


def test_origin_canonicalizes_default_port_and_hostname():
    check_origin(request([(b"origin", b"https://COURSES.example:443")]), "https://courses.example")
    check_origin(request([(b"origin", b"http://127.0.0.1:8000")]), "http://127.0.0.1:8000")


def test_forwarded_headers_are_ignored_unless_peer_is_trusted():
    headers = [(b"x-forwarded-for", b"203.0.113.1"), (b"forwarded", b"for=203.0.113.2"),
               (b"x-real-ip", b"203.0.113.3")]
    assert source_key(request(headers), ()) == digest("198.51.100.7")
    assert source_key(request(headers), ("10.0.0.0/8",)) == digest("198.51.100.7")
    assert source_key(request(headers, peer="10.0.0.1"), ("10.0.0.0/8",)) == digest("203.0.113.1")


def test_proxy_chain_stops_at_first_untrusted_hop():
    headers = [(b"x-forwarded-for", b"192.0.2.99, 198.51.100.9, 10.0.0.2")]
    assert source_key(request(headers, peer="10.0.0.1"), ("10.0.0.0/8",)) == digest("198.51.100.9")
    assert source_key(request(peer="2001:0db8::1"), ()) == digest("2001:db8::1")


@pytest.mark.parametrize("headers", [
    [(b"x-forwarded-for", b"unknown")], [(b"x-forwarded-for", b"198.51.100.1,,10.0.0.2")],
    [(b"x-forwarded-for", b"198.51.100.1:80")],
    [(b"x-forwarded-for", b"198.51.100.1"), (b"x-forwarded-for", b"198.51.100.2")],
])
def test_trusted_proxy_rejects_ambiguous_source(headers):
    with pytest.raises(BusinessError) as err:
        source_key(request(headers, peer="10.0.0.1"), ("10.0.0.0/8",))
    assert err.value.status_code == 400


def test_missing_peer_fails_closed():
    with pytest.raises(BusinessError):
        source_key(request(peer=None), ())


def test_public_limit_survives_restart(db_path, clock):
    limiter = RateLimiter(db_path, clock=clock.now)
    for _ in range(10):
        limiter.check_public("source-hash")
    with pytest.raises(BusinessError) as err:
        RateLimiter(db_path, clock=clock.now).check_public("source-hash")
    assert err.value.status_code == 429
    assert err.value.headers == {"Retry-After": "60"}


def test_limits_survive_service_restart(db_path, clock):
    for _ in range(5):
        limiter = RateLimiter(db_path, clock=clock.now)
        limiter.check_login("owner", "source")
        limiter.record_login_failure("owner", "source")
    restarted = RateLimiter(db_path, clock=clock.now)
    with pytest.raises(BusinessError) as err:
        restarted.check_login("owner", "source")
    assert err.value.status_code == 429
    assert err.value.headers == {"Retry-After": "900"}
    clock.advance(minutes=15)
    restarted.check_login("owner", "source")


def test_account_and_source_limits_are_independent(rate_limiter):
    for i in range(5):
        rate_limiter.record_login_failure("account-a", f"source-{i}")
    with pytest.raises(BusinessError):
        rate_limiter.check_login("account-a", "fresh-source")
    rate_limiter.check_login("account-b", "source-0")
    for i in range(5):
        rate_limiter.record_login_failure(f"account-{i}", "source-a")
    with pytest.raises(BusinessError):
        rate_limiter.check_login("fresh-account", "source-a")
    rate_limiter.check_login("fresh-account", "fresh-source")


def test_login_checks_do_not_count_successes(rate_limiter):
    for _ in range(20):
        rate_limiter.check_login("owner", "source")
    for _ in range(4):
        rate_limiter.record_login_failure("owner", "source")
    rate_limiter.check_login("owner", "source")
    rate_limiter.record_login_failure("owner", "source")
    with pytest.raises(BusinessError):
        rate_limiter.check_login("owner", "source")


def test_login_block_lasts_fifteen_minutes_from_fifth_failure(rate_limiter, clock):
    for _ in range(4):
        rate_limiter.record_login_failure("owner", "source")
    clock.advance(minutes=14)
    rate_limiter.record_login_failure("owner", "source")
    clock.advance(minutes=1)
    with pytest.raises(BusinessError) as err:
        rate_limiter.check_login("owner", "source")
    assert err.value.headers == {"Retry-After": "840"}
    clock.advance(minutes=14)
    rate_limiter.check_login("owner", "source")


def test_login_failure_window_expires_before_fifth_failure(rate_limiter, clock):
    for _ in range(4):
        rate_limiter.record_login_failure("owner", "source")
    clock.advance(minutes=15)
    rate_limiter.record_login_failure("owner", "source")
    rate_limiter.check_login("owner", "source")


def test_public_window_boundary_and_sources_are_independent(rate_limiter, clock):
    for _ in range(10):
        rate_limiter.check_public("source")
    rate_limiter.check_public("other-source")
    clock.advance(seconds=59, microseconds=500000)
    with pytest.raises(BusinessError) as err:
        rate_limiter.check_public("source")
    assert err.value.headers == {"Retry-After": "1"}
    clock.advance(microseconds=500000)
    rate_limiter.check_public("source")


def test_public_counter_is_atomic_across_services(db_path, clock):
    def attempt(_):
        try:
            RateLimiter(db_path, clock=clock.now).check_public("shared-source")
            return 200
        except BusinessError as err:
            return err.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(20)))
    assert results.count(200) == 10
    assert results.count(429) == 10


def test_login_failure_counters_are_atomic(db_path, clock):
    def fail(_):
        RateLimiter(db_path, clock=clock.now).record_login_failure("owner", "source")

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(fail, range(5)))
    with pytest.raises(BusinessError):
        RateLimiter(db_path, clock=clock.now).check_login("owner", "fresh")
    with pytest.raises(BusinessError):
        RateLimiter(db_path, clock=clock.now).check_login("fresh", "source")


def test_rate_denial_audit_never_contains_credentials(rate_limiter, db_path):
    for _ in range(5):
        rate_limiter.record_login_failure("owner-sensitive", "LK-sensitive")
    with pytest.raises(BusinessError) as err:
        rate_limiter.check_login("owner-sensitive", "LK-sensitive")
    for _ in range(10):
        rate_limiter.check_public("CS-sensitive")
    with pytest.raises(BusinessError):
        rate_limiter.check_public("CS-sensitive")
    with transaction(db_path) as connection:
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events")]
        limits = [dict(row) for row in connection.execute("SELECT * FROM request_limits")]
    assert len(events) == 2
    assert all(row["outcome"] == "denied" and row["actor_admin_id"] is None for row in events)
    stored = str(events) + str(limits) + str(err.value)
    assert "owner-sensitive" not in stored
    assert "LK-sensitive" not in stored
    assert "CS-sensitive" not in stored


def test_challenge_persists_only_hash_and_cannot_replay(csrf_service, db_path, clock):
    token = csrf_service.issue_challenge("login")
    with transaction(db_path) as connection:
        row = dict(connection.execute("SELECT * FROM csrf_challenges").fetchone())
    assert row["nonce_hash"] == digest(token)
    assert token not in str(row)
    assert row["expires_at"] == "2026-10-02T00:10:00+00:00"
    restarted = CsrfService(db_path, clock=clock.now)
    restarted.consume_challenge("login", token, token)
    with pytest.raises(BusinessError) as err:
        restarted.consume_challenge("login", token, token)
    assert err.value.status_code == 403


def test_csrf_expiry_and_cross_session_reuse_fail(csrf_service, clock):
    token_a = csrf_service.issue_challenge("redeem")
    token_b = csrf_service.issue_challenge("redeem")
    with pytest.raises(BusinessError):
        csrf_service.consume_challenge("redeem", token_a, token_b)
    with pytest.raises(BusinessError):
        csrf_service.consume_challenge("recover", token_a, token_a)
    # Invalid attempts must not burn the valid original challenge.
    csrf_service.consume_challenge("redeem", token_a, token_a)
    clock.advance(minutes=10)
    with pytest.raises(BusinessError):
        csrf_service.consume_challenge("redeem", token_b, token_b)


def test_challenge_is_valid_until_exact_expiry(csrf_service, clock):
    token = csrf_service.issue_challenge("login")
    clock.advance(minutes=9, seconds=59)
    csrf_service.consume_challenge("login", token, token)


def test_unknown_challenge_is_rejected(csrf_service):
    with pytest.raises(BusinessError):
        csrf_service.consume_challenge("login", "a" * 43, "a" * 43)


def test_consumed_challenge_can_be_replaced_after_business_failure(csrf_service):
    original = csrf_service.issue_challenge("recover")
    csrf_service.consume_challenge("recover", original, original)
    # A future error response issues a fresh technical challenge for its cookie/form.
    replacement = csrf_service.issue_challenge("recover")
    assert replacement != original
    csrf_service.consume_challenge("recover", replacement, replacement)


def test_challenge_consumption_is_atomic(db_path, clock, csrf_service):
    token = csrf_service.issue_challenge("login")

    def attempt(_):
        try:
            CsrfService(db_path, clock=clock.now).consume_challenge("login", token, token)
            return 200
        except BusinessError as err:
            return err.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count(200) == 1
    assert results.count(403) == 7


def test_bound_csrf_requires_cookie_form_and_stored_hash(csrf_service):
    token = "a" * 43
    other = "b" * 43
    csrf_service.verify_bound_csrf(token, token, digest(token))
    for form, cookie, stored in [(token, other, digest(token)), (other, other, digest(token)),
                                 (token, token, digest(other)), ("", "", digest("")),
                                 ("课" * 43, "课" * 43, digest("课" * 43))]:
        with pytest.raises(BusinessError) as err:
            csrf_service.verify_bound_csrf(form, cookie, stored)
        assert err.value.status_code == 403


def test_csrf_denial_audit_contains_no_tokens(csrf_service, db_path):
    token = csrf_service.issue_challenge("login")
    with pytest.raises(BusinessError):
        csrf_service.consume_challenge("login", token, "b" * 43)
    with transaction(db_path) as connection:
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events")]
    assert len(events) == 1 and events[0]["outcome"] == "denied"
    assert token not in str(events)
    assert digest(token) not in str(events)


@pytest.mark.parametrize("body", [b'{"code":"a=b"}', b'"code=a"', b"code=a\nb"])
def test_non_form_wire_syntax_cannot_be_parsed_as_form(body):
    with pytest.raises(BusinessError) as err:
        parse_unique_form(body)
    assert err.value.status_code == 400


def test_malformed_ipv6_origin_suffix_is_not_normalized_away():
    with pytest.raises(BusinessError) as err:
        check_origin(request([(b"origin", b"https://[2001:db8::1]evil")]), "https://[2001:db8::1]")
    assert err.value.status_code == 403


def test_malformed_csrf_unicode_is_a_safe_denial(csrf_service):
    status = None
    try:
        csrf_service.consume_challenge("login", "\ud800" * 43, "\ud800" * 43)
    except BusinessError as err:
        status = err.status_code
    except UnicodeError:
        status = 500
    assert status == 403


def test_ipv6_zone_ids_are_not_accepted_as_forwarded_sources():
    with pytest.raises(BusinessError) as err:
        source_key(request([(b"x-forwarded-for", b"2001:db8::1%attacker")], peer="10.0.0.1"), ("10.0.0.0/8",))
    assert err.value.status_code == 400


def test_rejected_login_retries_do_not_extend_block(rate_limiter, clock):
    for _ in range(5):
        rate_limiter.record_login_failure("owner", "source")
    clock.advance(minutes=14)
    rate_limiter.record_login_failure("owner", "source")
    with pytest.raises(BusinessError) as err:
        rate_limiter.check_login("owner", "source")
    assert err.value.headers == {"Retry-After": "60"}
    clock.advance(minutes=1)
    rate_limiter.check_login("owner", "source")


def test_rejected_public_retries_do_not_extend_window(rate_limiter, clock):
    for _ in range(10):
        rate_limiter.check_public("source")
    clock.advance(seconds=30)
    for _ in range(3):
        with pytest.raises(BusinessError) as err:
            rate_limiter.check_public("source")
        assert err.value.headers == {"Retry-After": "30"}
    clock.advance(seconds=30)
    rate_limiter.check_public("source")


def test_bound_csrf_verification_does_not_rotate_session_nonce(csrf_service, db_path):
    token = csrf_service.issue_challenge("login")
    csrf_service.verify_bound_csrf(token, token, digest(token))
    csrf_service.verify_bound_csrf(token, token, digest(token))
    # Pure verification leaves prechallenge lifecycle and session rotation to their owners.
    csrf_service.consume_challenge("login", token, token)
    with transaction(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_composed_login_limits_use_the_callers_transaction(rate_limiter, db_path):
    # Transaction helpers must see uncommitted counters and roll back with
    # their owner, while standalone APIs retain independent denial auditing.
    for _ in range(4):
        rate_limiter.record_login_failure("owner", "source")
    with pytest.raises(BusinessError) as error:
        with transaction(db_path, immediate=True) as connection:
            rate_limiter.check_login_in_tx(connection, "owner", "source")
            rate_limiter.record_login_failure_in_tx(connection, "owner", "source")
            rate_limiter.check_login_in_tx(connection, "owner", "source")
    assert error.value.status_code == 429
    with transaction(db_path) as connection:
        rows = connection.execute("SELECT count, blocked_until FROM request_limits").fetchall()
        assert len(rows) == 2 and all(tuple(row) == (4, None) for row in rows)
        assert connection.execute("SELECT count(*) FROM admin_events").fetchone()[0] == 0
    rate_limiter.check_login("owner", "source")
    rate_limiter.record_login_failure("owner", "source")
    with pytest.raises(BusinessError) as error:
        rate_limiter.check_login("owner", "source")
    assert error.value.headers == {"Retry-After": "900"}
    with transaction(db_path) as connection:
        events = [dict(row) for row in connection.execute("SELECT * FROM admin_events")]
    assert len(events) == 1 and events[0]["action"] == "security.rate_limit"


def test_csrf_boundary_can_own_denial_without_consuming_valid_challenge(csrf_service, db_path):
    token = csrf_service.issue_challenge("admin.login")
    with pytest.raises(BusinessError) as error:
        csrf_service.consume_challenge("admin.login", token, "b" * 43, audit_denial=False)
    assert error.value.status_code == 403
    with transaction(db_path) as connection:
        assert connection.execute("SELECT consumed_at FROM csrf_challenges").fetchone()[0] is None
        assert connection.execute("SELECT count(*) FROM admin_events").fetchone()[0] == 0
    csrf_service.consume_challenge("admin.login", token, token)
    with pytest.raises(BusinessError):
        csrf_service.consume_challenge("admin.login", token, token)
    with transaction(db_path) as connection:
        event = connection.execute("SELECT * FROM admin_events").fetchone()
    assert event["action"] == "security.csrf" and event["outcome"] == "denied"
