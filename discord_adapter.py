#!/usr/bin/env python3
"""discord_adapter.py — 디스코드 플랫폼 어댑터(Adapter 구현).

spike/discord_bridge_spike.py 로 실증한 패턴을 정식화한다: discord.py(asyncio) 봇을 전용
스레드의 이벤트루프에서 구동하고, on_message/on_interaction 이 정규화 `Event` 를 queue.Queue 에
적재하면 poll() 이 `.get()` 으로 직렬 소비한다(§2.4/§3.2). send/edit/ack 는 워커(동기) 스레드에서
`run_coroutine_threadsafe(coro, loop).result(timeout)` 로 코루틴 완료까지 블록해 동기 값을 반환한다.

의존성 격리: discord.py import 는 **이 파일에만** 있다. 텔레그램 실행 경로(bridge.main)는 이
모듈을 지연 import 하므로 discord.py 미설치 노트북에서도 죽지 않는다(계약: 본체 stdlib 전용).

보안 경계(§2.4 계승·비완화):
- fetch_file 은 디스코드 CDN 도메인 화이트리스트(cdn.discordapp.com/media.discordapp.net)·확장자·
  10MB·경로 트래버설 차단(basename 만)을 텔레그램 download_file 과 동형으로 적용.
- custom_id 는 신뢰 경계 밖 — parse_callback 정확 매칭만(임의 실행 금지, 텔레그램과 같은 코덱).
- 봇 토큰은 어댑터 인스턴스에만 보관, 전송 직전 mask_secrets 로 방어심층 마스킹.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import discord  # 유일한 discord.py import 지점 — 텔레그램 경로는 이 모듈을 import 하지 않는다.
from adapter import Button, Event, mask_secrets

# 콜백 코덱(parse_callback/encode_callback)·청킹·다운로드 상수는 플랫폼 무관 정본(§1.3·§2.4)이라
# telegram_adapter 에서 재사용한다 — telegram_adapter 는 stdlib 전용이므로 여기서 import 해도
# 텔레그램 런타임 의존이 생기지 않는다(순수 문자열/상수 재사용, 보안 로직 단일 소스 유지).
from telegram_adapter import (
    _NOREDIRECT_OPENER,
    MAX_PHOTO_BYTES,
    PHOTO_EXTS,
    chunk_text,
    encode_callback,
    parse_callback,
)

log = logging.getLogger("bridge")

DISCORD_LIMIT = 2000  # 디스코드 메시지 한도(§2.1) — 초과 장문은 청킹.
_CUSTOM_ID_LIMIT = 100  # 디스코드 custom_id 한도(§1.3). 우리 콜백 문자열은 항상 이 안(id·name≤64).
_CALL_TIMEOUT = 30  # run_coroutine_threadsafe().result() 타임아웃(§3.2 — tg_call 30s 와 정합).
# fetch_file 다운로드 도메인 고정(§2.4 — 임의 URL 다운로드=SSRF 차단).
_DISCORD_CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})


def _style(style: str) -> Any:
    """Button.style → discord.ButtonStyle(§4 초안: default→회색·primary→블루·danger→레드)."""
    return {
        "default": discord.ButtonStyle.secondary,
        "primary": discord.ButtonStyle.primary,
        "danger": discord.ButtonStyle.danger,
    }.get(style, discord.ButtonStyle.secondary)


def render_view(buttons: list[Button]) -> Any:
    """list[Button] → discord.ui.View. custom_id=encode_callback(§1.3), 스타일 매핑, ≤100자.

    클릭은 클라이언트 레벨 on_interaction 이 custom_id 로 라우팅한다(뷰 자체 콜백 미사용) —
    비영속 custom_id(메시지 id·프로젝트명 포함)라 persistent view 등록 없이 전역 이벤트로 받는다.
    timeout=None: 뷰가 만료돼도 on_interaction 은 계속 발화하므로 렌더 목적상 무기한 유지해도 무해.
    """
    view = discord.ui.View(timeout=None)
    for b in buttons:
        # custom_id 는 char 기준 100자 캡(우리 값은 항상 그 안). 초과 시 잘리면 parse_callback 이
        # 거르므로(오작동 대신 무시) 안전. 텔레그램은 64바이트 캡 — 한도만 다르고 코덱은 동일.
        cid = encode_callback(b.action, b.arg)[:_CUSTOM_ID_LIMIT]
        view.add_item(discord.ui.Button(label=b.label, style=_style(b.style), custom_id=cid))
    return view


class DiscordAdapter:
    """디스코드 Gateway/REST 를 Adapter 계약(poll·send·edit·ack·fetch_file·close)으로 감싼다.

    생성 시 봇토큰 + secrets(마스킹 대상) + allowed(선-필터용)를 주입받는다. 봇 스레드는 최초
    poll() 호출 때 기동한다(생성만으로는 접속하지 않음 — 단위 테스트에서 순수 메서드 검증 가능).
    """

    def __init__(
        self,
        token: str,
        secrets: list[str],
        allowed: frozenset[int],
        *,
        limit: int = DISCORD_LIMIT,
    ) -> None:
        self.token = token
        self.secrets = secrets
        # 어댑터 선-필터(방어심층·스팸/맵누증 차단). 권위 인가 게이트는 코어(handle_event, §3.1).
        self._allowed = allowed
        self.limit = limit
        self._queue: queue.Queue[Event | None] = queue.Queue()
        # callback_id -> live Interaction. ack(defer 이후 followup)로 잇는다. ack 에서 pop(정리).
        # ponytail: ack 가 항상 소비 + 비허용은 선-필터로 미적재라 맵 유계(토큰 15분 만료가 상한).
        self._interactions: dict[str, Any] = {}
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        intents = discord.Intents.default()
        intents.message_content = True  # on_message 본문 수신 필수(Developer Portal 도 켜야 함).
        self._client = discord.Client(intents=intents)
        self._register_events()

    # ── 이벤트 등록(전용 스레드 이벤트루프에서 발화) ──────────────────────────
    def _register_events(self) -> None:
        @self._client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

        @self._client.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            await self._on_interaction(interaction)

    async def _on_message(self, message: discord.Message) -> None:
        """텍스트/사진 메시지 → 정규화 Event 큐 적재. 자기 메시지·비허용은 드롭."""
        me = self._client.user
        if me is not None and message.author.id == me.id:
            return  # 자기 메시지 무시(에코 루프 방지)
        if message.author.id not in self._allowed:
            return  # 선-필터(코어도 재검증) — 스팸 유입 차단
        self._queue.put(self._message_event(message))

    def _message_event(self, message: discord.Message) -> Event:
        """discord.Message → Event(§1.4). 이미지 첨부가 있으면 photo, 아니면 text."""
        channel = message.channel
        # 0단계 채널→프로젝트 매핑(최소): 길드 채널명만 후보로 채운다(DM 은 name 없음 → None).
        # 코어가 채워진 project 를 검증·소비하는 배선·자동생성은 1단계(여기선 매핑만).
        project = getattr(channel, "name", None)
        image = next(
            (a for a in message.attachments if Path(a.filename or "").suffix.lower() in PHOTO_EXTS),
            None,
        )
        if image is not None:
            return Event(
                kind="photo",
                channel_id=channel.id,
                user_id=message.author.id,
                text=message.content or "",
                message_id=message.id,
                photo_ref=image.url,
                project=project,
            )
        return Event(
            kind="text",
            channel_id=channel.id,
            user_id=message.author.id,
            text=message.content or "",
            message_id=message.id,
            project=project,
        )

    async def _on_interaction(self, interaction: discord.Interaction) -> None:
        """버튼(component) 탭 → 즉시 defer(3초 규약, §2.3) 후 정규화 Event 큐 적재.

        비허용 유저는 defer·적재 없이 드롭(불필요 API·맵 누증 차단 — 코어도 재검증). defer 를
        **큐 적재 전** 이벤트루프 스레드에서 먼저 호출해, 워커 지연과 무관하게 3초 규약 유지.
        """
        if interaction.type != discord.InteractionType.component:
            return  # 컴포넌트 외(슬래시 명령 등)는 0단계 미사용
        if interaction.user.id not in self._allowed:
            return  # 선-필터: defer 도 하지 않음(코어 게이트가 최종 무회신 처리)
        try:
            await interaction.response.defer()  # §2.3 3초 규약 — 큐 적재보다 반드시 선행
        except discord.HTTPException as e:
            log.warning("interaction defer 실패: %s", type(e).__name__)
        data: dict[str, Any] = interaction.data if isinstance(interaction.data, dict) else {}
        custom_id = data.get("custom_id")
        parsed = parse_callback(custom_id) if isinstance(custom_id, str) else None
        action, arg = parsed if parsed is not None else ("", "")
        callback_id = str(interaction.id)
        self._interactions[callback_id] = interaction  # ack(followup)로 잇기
        msg = interaction.message
        self._queue.put(
            Event(
                kind="button",
                channel_id=interaction.channel_id or 0,
                user_id=interaction.user.id,
                message_id=msg.id if msg is not None else None,
                action=action,
                action_arg=arg,
                callback_id=callback_id,
            )
        )

    # ── 봇 스레드(전용 이벤트루프) ─────────────────────────────────────────────
    def _run_bot(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._client.start(self.token))
        except (asyncio.CancelledError, RuntimeError):
            pass
        except discord.DiscordException as e:
            # L-1: 로그인 실패(잘못된 토큰=LoginFailure)·게이트웨이 예외가 미포착이면 봇 스레드가
            # 조용히 죽고 poll 이 queue.get() 에서 영구 블록된다. 명시 로그(토큰 미노출) 후 finally
            # 의 종료 센티넬로 poll·main 을 깨워 깨끗이 종료시킨다.
            log.error("디스코드 봇 스레드 종료(%s) — 토큰·권한을 확인하세요", type(e).__name__)
        finally:
            with contextlib.suppress(RuntimeError, discord.DiscordException):
                loop.run_until_complete(self._client.close())
            loop.close()
            # L-1: 봇 스레드가 어떤 이유로든 끝나면 poll 해제(센티넬) — 봇 사망 시 무한 블록 방지.
            self._closed = True
            self._queue.put(None)
            log.info("디스코드 이벤트루프 종료")

    def _start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run_bot, name="discord-bot", daemon=True)
            self._thread.start()

    # ── 수신: 큐 직렬 소비 제너레이터(§2.5) ────────────────────────────────────
    def poll(self) -> Iterator[Event]:
        """봇 스레드 기동 후 큐를 직렬 소비하는 블로킹 제너레이터. close() 시 센티넬로 종료."""
        self._start()
        while not self._closed:
            item = self._queue.get()
            if item is None:  # close() 가 넣은 종료 센티넬
                break
            yield item

    # ── 송신(§2.1) ────────────────────────────────────────────────────────────
    def _emit(
        self,
        text: str,
        buttons: list[Button] | None,
        coro: Callable[[str, Any], Coroutine[Any, Any, Any]],
    ) -> int | None:
        """청킹·마스킹·버튼(마지막 청크만) 공통 발송 루프. coro(body, view)→message_id. 첫 id 반환.

        send(채널)·notify(user DM)가 대상 해석 코루틴만 달리해 공유한다(§2.1 규칙 단일 소스).
        """
        chunks = chunk_text(mask_secrets(text, self.secrets), self.limit)
        last = len(chunks) - 1
        first_id: int | None = None
        for i, chunk in enumerate(chunks):
            view = render_view(buttons) if buttons is not None and i == last else None
            mid = self._run(coro(chunk or "(빈 응답)", view))
            if i == 0:
                first_id = mid if isinstance(mid, int) else None
        return first_id

    def send(self, channel_id: int, text: str, buttons: list[Button] | None = None) -> int | None:
        """마스킹 후 청크 분할 전송. 버튼은 마지막 청크에만. 첫 청크 message_id 반환(실패 None)."""
        return self._emit(text, buttons, lambda body, view: self._send_coro(channel_id, body, view))

    def notify(self, user_id: int, text: str, buttons: list[Button] | None = None) -> int | None:
        """H-1: 허용 user_id 의 DM 으로 발송(알림 브로드캐스트 타겟). send 는 채널 전용이라 분리.

        user_id → get_user/fetch_user → create_dm 채널로 해석해 발송(§2.1 청킹·마스킹·버튼 동형).
        run_coroutine_threadsafe 경계·예외 삼킴은 _run 이 흡수(실패는 로그·None).
        """
        return self._emit(text, buttons, lambda body, view: self._dm_send_coro(user_id, body, view))

    def edit(
        self,
        channel_id: int,
        message_id: int,
        text: str,
        buttons: list[Button] | None = None,
    ) -> None:
        """진행 메시지 in-place 갱신. 오버플로(§2.2): 첫 청크 편집 + 나머지 후속 발행, 버튼 말미."""
        chunks = chunk_text(mask_secrets(text, self.secrets), self.limit)
        last = len(chunks) - 1
        head_view = render_view(buttons) if buttons is not None and last == 0 else None
        self._run(self._edit_coro(channel_id, message_id, chunks[0] or "(빈 응답)", head_view))
        for j, extra in enumerate(chunks[1:], start=1):
            view = render_view(buttons) if buttons is not None and j == last else None
            self._run(self._send_coro(channel_id, extra or "(빈 응답)", view))

    def ack(self, callback_id: str | None, note: str | None = None) -> None:
        """이미 defer 됨(§2.3) → note 있으면 followup.send, 없으면 no-op. callback_id 소비(정리)."""
        if not callback_id:
            return
        interaction = self._interactions.pop(callback_id, None)
        if interaction is None:
            return  # 이미 소비/미등록 — no-op(멱등)
        if note:
            self._run(self._followup_coro(interaction, note))

    def fetch_file(self, photo_ref: str, dest_dir: Path) -> Path:
        """attachment.url 다운로드 — 디스코드 CDN 도메인·확장자·크기·트래버설 잠금(§2.4 계승).

        저장명은 URL 경로의 basename 만(쿼리·경로 성분 제거 → 트래버설 차단). 위반은 ValueError.
        """
        parsed = urllib.parse.urlparse(photo_ref)
        if parsed.scheme != "https" or parsed.hostname not in _DISCORD_CDN_HOSTS:
            raise ValueError(f"허용되지 않은 다운로드 도메인: {parsed.hostname!r}")
        name = Path(urllib.parse.unquote(parsed.path)).name  # basename 만 — 경로/쿼리 제거
        if not name or name in (".", ".."):
            raise ValueError("잘못된 파일명")
        ext = Path(name).suffix.lower()
        if ext not in PHOTO_EXTS:
            raise ValueError(f"허용되지 않은 확장자: {ext!r}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        req = urllib.request.Request(photo_ref)  # https + CDN 화이트리스트 통과분만(SSRF 차단)
        # M-3: 리다이렉트 차단 opener — CDN 이 3xx 로 내부주소를 가리켜도 추종 안 함(SSRF 차단).
        with _NOREDIRECT_OPENER.open(req, timeout=30) as resp:  # 스킴·호스트 검증됨
            clen = resp.headers.get("Content-Length")
            if clen is not None and clen.isdigit() and int(clen) > MAX_PHOTO_BYTES:
                raise ValueError("사진이 크기 상한(10MB)을 초과합니다.")
            payload = resp.read(MAX_PHOTO_BYTES + 1)  # 상한+1 만 읽어 초과 즉시 판정(메모리 보호)
        if len(payload) > MAX_PHOTO_BYTES:
            raise ValueError("사진이 크기 상한(10MB)을 초과합니다.")
        dest.write_bytes(payload)
        return dest

    def close(self) -> None:
        """Gateway·이벤트루프·워커 정리. 중복 호출 무해."""
        self._closed = True
        self._queue.put(None)  # poll() 제너레이터 해제(센티넬)
        loop = self._loop
        if loop is not None and loop.is_running():
            # 다른 스레드에서 안전하게 종료 요청(봇 루프가 client.close() 수행).
            asyncio.run_coroutine_threadsafe(self._client.close(), loop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    # ── asyncio↔동기 경계 헬퍼(§3.2) ──────────────────────────────────────────
    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """코루틴을 봇 이벤트루프에 밀어넣고 완료까지 동기 대기. 실패는 로그+None(§3.3, 루프 보호).

        플랫폼 오류(rate-limit·네트워크·루프 사망)는 어댑터가 삼키고 로그만 남긴다(코어 직렬 루프
        보호). 그래서 광범위 except — send/edit/ack 계약이 "실패는 로그·None"이기 때문(§3.3).
        """
        loop = self._loop
        if loop is None or not loop.is_running():
            log.warning("디스코드 이벤트루프 미준비 — 호출 스킵")
            coro.close()
            return None
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut.result(timeout=_CALL_TIMEOUT)
        except Exception as e:  # §3.3: 모든 플랫폼 오류를 삼키고 로그(코어 직렬 루프 보호)
            log.warning("디스코드 호출 실패: %s", type(e).__name__)
            return None

    async def _send_coro(self, channel_id: int, body: str, view: Any) -> int | None:
        channel = self._client.get_channel(channel_id) or await self._client.fetch_channel(
            channel_id
        )
        msg = await channel.send(body, view=view) if view is not None else await channel.send(body)
        return int(msg.id)

    async def _dm_send_coro(self, user_id: int, body: str, view: Any) -> int | None:
        """H-1: user_id → DM 채널 해석 후 발송. get_user 캐시 우선, 없으면 fetch_user."""
        user = self._client.get_user(user_id) or await self._client.fetch_user(user_id)
        channel = user.dm_channel or await user.create_dm()
        msg = await channel.send(body, view=view) if view is not None else await channel.send(body)
        return int(msg.id)

    async def _edit_coro(self, channel_id: int, message_id: int, body: str, view: Any) -> None:
        channel = self._client.get_channel(channel_id) or await self._client.fetch_channel(
            channel_id
        )
        # get_partial_message: 캐시·fetch 없이 message_id 로 편집(view=None 이면 컴포넌트 제거).
        await channel.get_partial_message(message_id).edit(content=body, view=view)

    async def _followup_coro(self, interaction: Any, note: str) -> None:
        await interaction.followup.send(note)
