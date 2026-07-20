"""DiscordAdapter 계약 테스트(§5.2 — 디스코드 특화 단위).

이벤트루프 실구동(Gateway 접속)은 라이브 검증(0e) 몫이라 여기선 제외하고, 루프 없이 단위 검증
가능한 것만 다룬다: render_view(custom_id·스타일), fetch_file 보안(§2.4 CDN·확장자·크기·트래버설),
_message_event/_on_message 정규화·필터, _on_interaction defer 선행·custom_id 파싱·비허용 드롭,
send/edit 청킹·마스킹·버튼 말미(코루틴 경계는 _run 스텁), ack 멱등·맵 소비, close 안전성.

discord.py 미설치 환경(예: CI 최소셋)에서는 importorskip 으로 전체 스킵 → 236 코어 그린 불변.
"""

from __future__ import annotations

import asyncio
import urllib.error
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")  # 미설치면 이 파일 전체 스킵(코어 236 은 무영향)

import discord_adapter  # noqa: E402  (importorskip 뒤에 와야 함)
from adapter import Button, Event  # noqa: E402
from discord_adapter import DiscordAdapter, render_view  # noqa: E402

_ALLOWED = frozenset({777})


def _adapter(secrets=None, limit=discord_adapter.DISCORD_LIMIT):
    """접속하지 않는 어댑터(생성만) — poll() 을 부르지 않으면 Gateway 로 안 나간다."""
    return DiscordAdapter("tok", secrets if secrets is not None else [], _ALLOWED, limit=limit)


# ---------------------------------------------------------------------------
# render_view: Button → discord.ui.View (custom_id=encode_callback, 스타일 매핑, ≤100자)
# ---------------------------------------------------------------------------
def test_render_view_custom_id_and_style():
    view = render_view(
        [Button("✅ Push", "push", style="primary"), Button("❌ 취소", "x", style="danger")]
    )
    items = view.children
    assert [it.custom_id for it in items] == ["push", "x"]
    assert items[0].style == discord.ButtonStyle.primary
    assert items[1].style == discord.ButtonStyle.danger


def test_render_view_default_style_is_secondary():
    view = render_view([Button("데모", "p", "trading_info")])
    it = view.children[0]
    assert it.custom_id == "p:trading_info"  # encode_callback 직렬화
    assert it.style == discord.ButtonStyle.secondary
    assert it.label == "데모"


def test_render_view_choice_and_notify_custom_ids():
    from bridge import choice_buttons, notify_buttons

    v1 = render_view(notify_buttons("ti-open"))
    assert [c.custom_id for c in v1.children] == ["nb:ok:ti-open", "nb:later:ti-open"]
    v2 = render_view(choice_buttons(55, [("유지", "keep"), ("교체", "swap")]))
    assert [c.custom_id for c in v2.children] == ["c:55:0", "c:55:1", "c:55:other"]


def test_render_view_custom_id_within_discord_100_char_limit():
    # §1.3: DC custom_id ≤100자. id·name≤64 라 인코드 결과가 한도 안(캡은 오작동 없이 무시로 안전).
    from telegram_adapter import encode_callback

    for action, arg in (
        ("push", ""),
        ("x", ""),
        ("p", "x" * 64),
        ("nb:ok", "y" * 64),
        ("nb:later", "z" * 64),
        ("c", "999999:12"),
    ):
        assert len(encode_callback(action, arg)) <= discord_adapter._CUSTOM_ID_LIMIT


# ---------------------------------------------------------------------------
# fetch_file: §2.4 보안 계승(CDN 도메인·확장자·크기·트래버설)
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, data=b"", headers=None):
        self._data = data
        self.headers = headers or {}

    def read(self, n=-1):
        return self._data if n is None or n < 0 else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def _patch_urlopen(monkeypatch, resp):
    # fetch_file 은 리다이렉트 차단 opener(_NOREDIRECT_OPENER.open)를 쓴다(M-3) — 그걸 패치.
    monkeypatch.setattr(discord_adapter._NOREDIRECT_OPENER, "open", lambda *_a, **_k: resp)


_CDN = "https://cdn.discordapp.com/attachments/1/2/photo.png?ex=abc&is=def&hm=deadbeef"


def test_fetch_file_happy_writes_basename(monkeypatch, tmp_path):
    _patch_urlopen(monkeypatch, _FakeResp(b"\x89PNGdata"))
    dest = _adapter().fetch_file(_CDN, tmp_path)
    assert dest.name == "photo.png"  # 쿼리스트링 제거된 basename
    assert dest.parent == tmp_path
    assert dest.read_bytes() == b"\x89PNGdata"


def test_fetch_file_rejects_non_cdn_domain(monkeypatch, tmp_path):
    _patch_urlopen(monkeypatch, _FakeResp(b"x"))
    with pytest.raises(ValueError, match="도메인"):
        _adapter().fetch_file("https://evil.example.com/attachments/1/2/photo.png", tmp_path)


def test_fetch_file_rejects_http_scheme(monkeypatch, tmp_path):
    _patch_urlopen(monkeypatch, _FakeResp(b"x"))
    with pytest.raises(ValueError, match="도메인"):
        _adapter().fetch_file("http://cdn.discordapp.com/a/b/photo.png", tmp_path)


def test_fetch_file_rejects_bad_extension(monkeypatch, tmp_path):
    _patch_urlopen(monkeypatch, _FakeResp(b"x"))
    with pytest.raises(ValueError, match="확장자"):
        _adapter().fetch_file("https://cdn.discordapp.com/a/b/evil.gif", tmp_path)


def test_fetch_file_traversal_stays_basename(monkeypatch, tmp_path):
    _patch_urlopen(monkeypatch, _FakeResp(b"x"))
    # 경로에 ../ 가 있어도 basename 만 저장 → dest 밖으로 못 나감.
    dest = _adapter().fetch_file("https://media.discordapp.net/a/../../etc/evil.jpg", tmp_path)
    assert dest.name == "evil.jpg"
    assert dest.parent == tmp_path


def test_fetch_file_rejects_oversize_body(monkeypatch, tmp_path):
    monkeypatch.setattr(discord_adapter, "MAX_PHOTO_BYTES", 4)
    _patch_urlopen(monkeypatch, _FakeResp(b"toolongbody"))
    with pytest.raises(ValueError, match=r"10MB|상한"):
        _adapter().fetch_file(_CDN, tmp_path)


def test_fetch_file_rejects_oversize_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(discord_adapter, "MAX_PHOTO_BYTES", 4)
    _patch_urlopen(monkeypatch, _FakeResp(b"ok", headers={"Content-Length": "999"}))
    with pytest.raises(ValueError, match=r"10MB|상한"):
        _adapter().fetch_file(_CDN, tmp_path)


def test_fetch_file_rejects_redirect(monkeypatch, tmp_path):
    # M-3: CDN 이 3xx 로 내부주소(169.254.169.254)를 가리켜도 opener 가 추종 대신 HTTPError → 거부.
    def raise_302(*_a, **_k):
        raise urllib.error.HTTPError(_CDN, 302, "redirect blocked", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(discord_adapter._NOREDIRECT_OPENER, "open", raise_302)
    with pytest.raises(urllib.error.HTTPError):
        _adapter().fetch_file(_CDN, tmp_path)


# ---------------------------------------------------------------------------
# _message_event / _on_message: 수신 정규화(§1.4) + 자기·비허용 필터
# ---------------------------------------------------------------------------
def _msg(user_id, content="", *, channel_id=100, channel_name="trading_info", msg_id=5, atts=None):
    channel = SimpleNamespace(id=channel_id, name=channel_name)
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id),
        channel=channel,
        content=content,
        id=msg_id,
        attachments=atts or [],
    )


def test_message_event_text_normalization():
    ev = _adapter()._message_event(_msg(777, "etf_info 확인해줘"))
    assert ev.kind == "text"
    assert ev.channel_id == 100 and ev.user_id == 777
    assert ev.text == "etf_info 확인해줘" and ev.message_id == 5
    assert ev.project == "trading_info"  # 채널명 = 프로젝트 후보(0단계 매핑)


def test_message_event_photo_picks_image_attachment():
    att = SimpleNamespace(filename="toss.PNG", url=_CDN)
    ev = _adapter()._message_event(_msg(777, "MU", atts=[att]))
    assert ev.kind == "photo"
    assert ev.photo_ref == _CDN
    assert ev.text == "MU"


def test_message_event_dm_channel_project_none():
    channel = SimpleNamespace(id=9)  # name 속성 없음(DM)
    m = SimpleNamespace(
        author=SimpleNamespace(id=777), channel=channel, content="hi", id=1, attachments=[]
    )
    assert _adapter()._message_event(m).project is None


def test_on_message_drops_disallowed_enqueues_allowed():
    a = _adapter()  # 미접속 → client.user is None(자기 메시지 가드는 통과)
    asyncio.run(a._on_message(_msg(999, "hax")))
    assert a._queue.qsize() == 0  # 비허용 유저 드롭
    asyncio.run(a._on_message(_msg(777, "trading_info go")))
    assert a._queue.qsize() == 1
    ev = a._queue.get_nowait()
    assert ev.kind == "text" and ev.user_id == 777


# ---------------------------------------------------------------------------
# _on_interaction: defer 선행(§2.3) + custom_id 파싱 + 비허용 드롭
# ---------------------------------------------------------------------------
def _interaction(user_id, custom_id, *, msg_id=42, channel_id=100, order=None):
    async def defer():
        if order is not None:
            order.append("defer")

    return SimpleNamespace(
        type=discord.InteractionType.component,
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(defer=defer),
        data={"custom_id": custom_id},
        id=9001,
        message=SimpleNamespace(id=msg_id),
        channel_id=channel_id,
    )


def test_on_interaction_defers_before_enqueue():
    a = _adapter()
    order = []
    inter = _interaction(777, "push", order=order)

    class _RecordQueue:
        def put(self, ev):
            order.append(("put", ev))

    a._queue = _RecordQueue()
    asyncio.run(a._on_interaction(inter))
    # defer 가 큐 적재보다 반드시 먼저(§2.3 3초 규약)
    assert order[0] == "defer"
    assert order[1][0] == "put"
    ev = order[1][1]
    assert ev.kind == "button" and ev.action == "push" and ev.callback_id == "9001"
    assert ev.channel_id == 100 and ev.message_id == 42 and ev.user_id == 777
    # interaction 이 ack 용으로 맵에 등록됨
    assert a._interactions["9001"] is inter


def test_on_interaction_parses_choice_custom_id():
    a = _adapter()
    asyncio.run(a._on_interaction(_interaction(777, "c:42:1")))
    ev = a._queue.get_nowait()
    assert ev.action == "c" and ev.action_arg == "42:1"


def test_on_interaction_unknown_custom_id_becomes_empty_action():
    a = _adapter()
    asyncio.run(a._on_interaction(_interaction(777, "bogus")))
    ev = a._queue.get_nowait()
    assert ev.action == "" and ev.action_arg == ""  # 코어가 ack 후 무시


def test_on_interaction_disallowed_user_dropped_no_defer():
    a = _adapter()
    order = []
    inter = _interaction(999, "push", order=order)
    asyncio.run(a._on_interaction(inter))
    assert order == []  # defer 조차 안 함
    assert a._queue.qsize() == 0
    assert a._interactions == {}


def test_on_interaction_ignores_non_component():
    a = _adapter()
    inter = _interaction(777, "push")
    inter.type = discord.InteractionType.application_command
    asyncio.run(a._on_interaction(inter))
    assert a._queue.qsize() == 0


# ---------------------------------------------------------------------------
# send / edit: 청킹·마스킹·버튼 말미 (_run·coro 스텁으로 루프 없이 검증)
# ---------------------------------------------------------------------------
def _stub_calls(adapter, ids):
    """_send_coro/_edit_coro 를 튜플로, _run 을 레코더로 대체(코루틴 미생성 → 경고 없음)."""
    calls = []
    adapter._send_coro = lambda cid, body, view: ("send", cid, body, view)  # type: ignore[assignment]
    adapter._edit_coro = lambda cid, mid, body, view: ("edit", cid, mid, body, view)  # type: ignore[assignment]
    it = iter(ids)

    def fake_run(coro):
        calls.append(coro)
        return next(it, None)

    adapter._run = fake_run  # type: ignore[assignment]
    return calls


def test_send_single_chunk_returns_first_id():
    a = _adapter()
    calls = _stub_calls(a, [111])
    mid = a.send(100, "짧은 응답")
    assert mid == 111
    assert len(calls) == 1
    assert calls[0] == ("send", 100, "짧은 응답", None)


def test_send_masks_secrets():
    a = _adapter(secrets=["SECRET"])
    calls = _stub_calls(a, [1])
    a.send(100, "token=SECRET 노출")
    assert calls[0][2] == "token=*** 노출"


def test_send_chunks_buttons_on_last_only():
    a = _adapter(limit=5)
    calls = _stub_calls(a, [10, 20, 30])
    mid = a.send(100, "abcdefghijkl", [Button("Push", "push", style="primary")])  # 12자 → 3청크
    assert mid == 10  # 첫 청크 id
    assert len(calls) == 3
    # 버튼(view)은 마지막 청크에만
    assert calls[0][3] is None and calls[1][3] is None
    assert calls[2][3] is not None  # render_view 결과(View)


def test_edit_overflow_edits_head_then_sends_rest():
    a = _adapter(limit=5)
    calls = _stub_calls(a, [None, None, None])
    a.edit(100, 42, "abcdefghijkl", [Button("Push", "push")])  # 3청크
    # edit 튜플=(edit,cid,mid,body,view)→view=[4]. send 튜플=(send,cid,body,view)→view=[3].
    assert calls[0][0] == "edit" and calls[0][1] == 100 and calls[0][2] == 42
    assert calls[0][4] is None  # head 는 다청크라 버튼 없음
    assert calls[1][0] == "send" and calls[2][0] == "send"
    assert calls[2][3] is not None  # 마지막 후속 발행에 버튼


def test_edit_single_chunk_keeps_buttons_on_head():
    a = _adapter()
    calls = _stub_calls(a, [None])
    a.edit(100, 42, "짧음", [Button("Push", "push")])
    assert len(calls) == 1
    assert calls[0][0] == "edit"
    assert calls[0][4] is not None  # 단일 청크 → head 에 버튼


# ---------------------------------------------------------------------------
# notify(H-1): 허용 user_id → DM 발송(send=채널 전용과 분리)
# ---------------------------------------------------------------------------
def test_notify_routes_to_dm_send_coro_not_channel():
    a = _adapter()
    calls = []
    a._dm_send_coro = lambda uid, body, view: ("dm", uid, body, view)  # type: ignore[assignment]
    a._send_coro = lambda cid, body, view: ("send", cid, body, view)  # type: ignore[assignment]

    def fake_run(coro):
        calls.append(coro)
        return 55

    a._run = fake_run  # type: ignore[assignment]
    mid = a.notify(777, "⏰ 알림", [Button("✅", "nb:ok", "a")])
    assert mid == 55
    assert calls[0][0] == "dm" and calls[0][1] == 777  # 채널(send)이 아니라 DM 경로
    assert calls[0][3] is not None  # 단일 청크 → 버튼 부착


def test_notify_masks_secrets():
    a = _adapter(secrets=["SECRET"])
    calls = []
    a._dm_send_coro = lambda uid, body, view: ("dm", uid, body, view)  # type: ignore[assignment]
    a._run = lambda coro: calls.append(coro)  # type: ignore[assignment]
    a.notify(777, "token=SECRET 노출")
    assert calls[0][2] == "token=*** 노출"


def test_dm_send_coro_resolves_user_to_dm_channel():
    # H-1: user_id → get_user(캐시) → create_dm → send. 채널 해석을 DM 으로.
    a = _adapter()
    sent = {}

    class _DM:
        async def send(self, body, view=None):
            sent["body"], sent["view"] = body, view
            return SimpleNamespace(id=999)

    class _User:
        dm_channel = None

        async def create_dm(self):
            return _DM()

    a._client.get_user = lambda uid: _User() if uid == 777 else None  # type: ignore[assignment]
    mid = asyncio.run(a._dm_send_coro(777, "본문", None))
    assert mid == 999 and sent["body"] == "본문"


# ---------------------------------------------------------------------------
# ack: 멱등·맵 소비 / close: 안전성
# ---------------------------------------------------------------------------
def test_ack_none_callback_is_noop():
    a = _adapter()
    ran = []
    a._run = lambda coro: ran.append(coro)  # type: ignore[assignment]
    a.ack(None)
    a.ack("")
    assert ran == []


def test_ack_unknown_callback_is_noop():
    a = _adapter()
    ran = []
    a._run = lambda coro: ran.append(coro)  # type: ignore[assignment]
    a.ack("nope", "note")
    assert ran == []  # 맵에 없으면 followup 도 안 함


def test_ack_with_note_sends_followup_and_consumes_map():
    a = _adapter()
    inter = SimpleNamespace(name="i")
    a._interactions["9001"] = inter
    a._followup_coro = lambda interaction, note: ("followup", interaction, note)  # type: ignore[assignment]
    ran = []
    a._run = lambda coro: ran.append(coro)  # type: ignore[assignment]
    a.ack("9001", "확인")
    assert ran == [("followup", inter, "확인")]
    assert "9001" not in a._interactions  # 소비(맵 정리)


def test_ack_without_note_consumes_map_no_followup():
    a = _adapter()
    a._interactions["9001"] = SimpleNamespace()
    ran = []
    a._run = lambda coro: ran.append(coro)  # type: ignore[assignment]
    a.ack("9001")  # note 없음 → 이미 defer 됨, followup 안 함
    assert ran == []
    assert "9001" not in a._interactions


def test_close_before_start_is_safe_and_sets_sentinel():
    a = _adapter()
    a.close()  # 스레드·루프 미기동 상태에서도 예외 없이
    assert a._closed is True
    assert a._queue.get_nowait() is None  # poll 해제용 종료 센티넬


def test_run_bot_login_failure_signals_poll_sentinel():
    # L-1: 잘못된 토큰(LoginFailure)으로 봇 스레드가 죽으면 poll 이 queue.get() 에서 영구 블록된다 —
    # _run_bot 이 예외를 포착하고 종료 센티넬을 큐에 넣어 poll·main 이 깨끗이 끝나게 한다.
    a = _adapter()

    async def _boom():
        raise discord.LoginFailure("bad token")

    async def _aclose():
        return None

    a._client.start = lambda *_a, **_k: _boom()  # type: ignore[assignment]
    a._client.close = lambda *_a, **_k: _aclose()  # type: ignore[assignment]
    a._run_bot()  # 동기 실행 — 예외를 삼키고 센티넬을 넣어야 함(무한 블록 방지)
    assert a._closed is True
    assert a._queue.get_nowait() is None  # poll 해제 센티넬(봇 사망 시)


def test_poll_terminates_when_bot_thread_dies(monkeypatch):
    # L-1 통합: 봇 스레드가 죽어 센티넬만 들어오면 poll 은 정상 종료(무한 블록 X).
    a = _adapter()
    monkeypatch.setattr(a, "_start", lambda: None)  # 실제 Gateway 접속 방지
    a._queue.put(None)  # 봇 사망 시 _run_bot 이 넣는 센티넬을 모사
    assert list(a.poll()) == []  # 블록 없이 즉시 종료


def test_poll_drains_queue_until_sentinel(monkeypatch):
    a = _adapter()
    monkeypatch.setattr(a, "_start", lambda: None)  # Gateway 접속 방지
    ev = Event(kind="text", channel_id=1, user_id=777, text="hi")
    a._queue.put(ev)
    a._queue.put(None)  # 센티넬
    got = list(a.poll())
    assert got == [ev]


def test_run_without_loop_returns_none_and_closes_coro():
    a = _adapter()  # _loop 은 None(미기동)

    async def coro():
        return 1

    c = coro()
    assert a._run(c) is None  # 루프 미준비 → None
    # 코루틴이 close 돼 "never awaited" 경고가 안 남(파괴 시점 검증은 생략, 호출만으로 close 됨)
