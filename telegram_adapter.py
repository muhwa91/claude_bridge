#!/usr/bin/env python3
"""telegram_adapter.py — 텔레그램 플랫폼 어댑터(Adapter 구현).

기존 bridge.py 의 텔레그램 표면부(롱폴링 수신·REST 송신·inline_keyboard 렌더·파일 다운로드·
콜백/사진 파싱)를 **로직 무변경으로** 이 파일로 이동한다. 코어는 정규화 `Event`/`Button` 과
`Adapter` 6메서드만 안다. Python 표준 라이브러리만 사용(외부 패키지 0).

보안 경계(계승·비완화):
- 파일 다운로드 도메인 고정(api.telegram.org)·확장자 화이트리스트·크기 상한 10MB·경로 트래버설 차단.
- callback_data 는 신뢰 경계 밖 — parse_callback 정확 매칭만(임의 실행 금지).
- 봇 토큰은 어댑터 인스턴스에만 보관, 회신 전송 직전 mask_secrets 로 방어심층 마스킹.
"""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from adapter import Button, Event, _valid_id, mask_secrets

log = logging.getLogger("bridge")

# D5: Telegram 한도는 UTF-16 코드유닛 4096 기준이나 여기선 코드포인트로 분할하므로,
# 비-BMP 이모지 다량 시 초과 방지용 안전마진으로 4000 으로 낮춘다.
TELEGRAM_LIMIT = 4000
POLL_TIMEOUT = 25  # 텔레그램 롱폴링 대기(초)

# 사진 대조용 상수. 다운로드 표면(크기·확장자)을 고정으로 잠근다.
MAX_PHOTO_BYTES = 10 * 1024 * 1024  # 10MB 상한
PHOTO_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})


# M-3: 리다이렉트 차단 opener. 화이트리스트 CDN 이 3xx 로 내부주소(오라클 메타데이터
# 169.254.169.254 등)를 가리켜도 추종하지 않는다 — redirect_request→None 이면 urllib 이 그 3xx 를
# HTTPError 로 승격해(추종 안 함) 내부주소 재요청을 원천 차단한다. 사진 다운로드(download_file·
# 디스코드 fetch_file)만 이 opener 를 쓴다 — tg_call·REST(fetch_stock)는 고정호스트라 무관.
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        # 3xx 를 추종하지 않음 → urllib 이 HTTPError 로 승격(내부주소 재요청 원천 차단).
        return None


_NOREDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


# ══════════════════════════════════════════════════════════════════════════
# 순수 표면 함수 (bridge.py 에서 이동 — 로직·시그니처 무변경)
# ══════════════════════════════════════════════════════════════════════════
def chunk_text(text: str, limit: int) -> list[str]:
    """플랫폼 한도(호출측 명시: TG=TELEGRAM_LIMIT / DC=DISCORD_LIMIT)로 분할. 빈 문자열이면 [""]."""
    if text == "":
        return [""]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def parse_callback(data: str) -> tuple[str, str] | None:
    """callback_data(신뢰 경계 밖) → (action, arg). 화이트리스트 밖은 None.

    `push`/`x` → (그대로, ""), `p:<name>` → ("p", name),
    `nb:ok:<id>`/`nb:later:<id>` → ("nb:ok"/"nb:later", id). 임의 실행 없이 정확 매칭만.
    """
    if data in ("push", "x"):
        return (data, "")
    if data.startswith("p:") and len(data) > 2:
        return ("p", data[2:])
    for prefix in ("nb:ok:", "nb:later:"):
        if data.startswith(prefix):
            item_id = data[len(prefix) :]
            # id 는 우리가 발행하지만 callback_data 는 신뢰 경계 밖 — 방출측과 같은 문(_valid_id).
            if _valid_id(item_id):
                return (prefix[:-1], item_id)
            return None
    if data.startswith("c:"):
        # c:<msg_id>:<idx|other> — msg_id 정수, 선택은 정수 인덱스 또는 'other'.
        # L-3: isascii() 병행으로 전각·위첨자 등 유니코드 숫자(int() 통과)를 차단.
        parts = data.split(":")
        mid_ok = len(parts) == 3 and parts[1].isascii() and parts[1].isdigit()
        sel_ok = mid_ok and (parts[2] == "other" or (parts[2].isascii() and parts[2].isdigit()))
        if sel_ok:
            return ("c", f"{parts[1]}:{parts[2]}")
        return None
    return None


def extract_photo(update: dict[str, Any]) -> str | None:
    """update.message.photo 배열에서 **최대 해상도**의 file_id 추출. 순수.

    텔레그램은 같은 사진을 여러 해상도(PhotoSize)로 보낸다 — width*height 가 가장 큰 것을
    고른다(오름차순 가정에 의존하지 않음). photo 없음·형식 불일치는 None.
    """
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    photos = msg.get("photo")
    if not isinstance(photos, list) or not photos:
        return None
    sizes = [p for p in photos if isinstance(p, dict)]
    if not sizes:
        return None
    best = max(sizes, key=lambda p: int(p.get("width", 0) or 0) * int(p.get("height", 0) or 0))
    fid = best.get("file_id")
    return fid if isinstance(fid, str) and fid else None


# ── Button ↔ 텔레그램 inline_keyboard 렌더(범용 렌더러 1개, 특화 키보드 4종 대체) ──
def encode_callback(action: str, arg: str) -> str:
    """정규화 (action, arg) → TG callback_data 문자열(parse_callback 의 역함수).

    코어 Button.action/arg 를 텔레그램 전송 문자열로 직렬화. 디코드(parse_callback)의 역.
    """
    if action in ("push", "x"):
        return action
    if action in ("p", "nb:ok", "nb:later", "c"):
        return f"{action}:{arg}"
    return action  # 방출측이 유효 액션만 넘기므로 폴백은 그대로


def render_buttons(buttons: list[Button]) -> dict[str, Any]:
    """list[Button] → TG inline_keyboard(dict). 한 줄 2개씩, callback_data 64바이트 캡.

    (구 project_keyboard·push_keyboard·notify_keyboard·choice_keyboard 를 단일화한 범용 렌더러.)
    text 는 Button.label, callback_data 는 encode_callback 결과를 64바이트로 절단(부분 멀티바이트
    ignore). style 은 텔레그램에 색 개념이 없어 무시(디스코드 어댑터가 사용).
    """
    rendered: list[dict[str, str]] = []
    for b in buttons:
        raw = encode_callback(b.action, b.arg).encode("utf-8")
        if len(raw) > 64:
            # 방어심층: 64B 초과 = callback_data 절단 → 왕복 매칭 실패(탭해도 무반응).
            # _valid_id(≤54)가 방출을 막지만, 우회 방출 시 조용한 실패 대신 경고를 남긴다.
            log.warning("callback_data 64B 초과 절단(action=%s) — 왕복 매칭 실패 가능", b.action)
        data = raw[:64].decode("utf-8", "ignore")
        rendered.append({"text": b.label, "callback_data": data})
    rows = [rendered[i : i + 2] for i in range(0, len(rendered), 2)]
    return {"inline_keyboard": rows}


# ══════════════════════════════════════════════════════════════════════════
# 텔레그램 REST + 파일 (bridge.py 에서 이동 — 로직 무변경, 어댑터 내부 소비)
# ══════════════════════════════════════════════════════════════════════════
def tg_call(token: str, method: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # 고정 https(api.telegram.org)
        payload: dict[str, Any] = json.load(resp)
    return payload


def tg_get_file(token: str, file_id: str) -> str:
    """getFile → result.file_path 반환(다운로드 경로). 응답에 file_path 없으면 ValueError."""
    resp = tg_call(token, "getFile", {"file_id": file_id}, timeout=30)
    result = resp.get("result")
    fp = result.get("file_path") if isinstance(result, dict) else None
    if not isinstance(fp, str) or not fp:
        raise ValueError("getFile 응답에 file_path 없음")
    return fp


def download_file(token: str, file_path: str, dest_dir: Path) -> Path:
    """텔레그램 파일 서버에서 사진 다운로드. 저장 파일명은 서버가 정한 basename(사용자 입력 아님).

    보안: 다운로드 URL 도메인 고정, 확장자 화이트리스트(jpg/jpeg/png/webp), 크기 상한 10MB.
    저장명은 텔레그램 file_path 의 basename 만 사용(경로 성분 제거 → 트래버설 차단). 규칙 위반은
    ValueError(호출측이 graceful 회신).
    """
    ext = Path(file_path).suffix.lower()
    if ext not in PHOTO_EXTS:
        raise ValueError(f"허용되지 않은 확장자: {ext!r}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(file_path).name  # basename 만 — 경로 트래버설 차단
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    req = urllib.request.Request(url)
    # M-3: 리다이렉트 차단 opener — 고정 https 호스트에서 내부주소로의 3xx 추종(SSRF) 차단.
    with _NOREDIRECT_OPENER.open(req, timeout=30) as resp:  # 고정 https(api.telegram.org)
        clen = resp.headers.get("Content-Length")
        if clen is not None and clen.isdigit() and int(clen) > MAX_PHOTO_BYTES:
            raise ValueError("사진이 크기 상한(10MB)을 초과합니다.")
        data = resp.read(MAX_PHOTO_BYTES + 1)  # 상한+1 만 읽어 초과 즉시 판정(메모리 보호)
    if len(data) > MAX_PHOTO_BYTES:
        raise ValueError("사진이 크기 상한(10MB)을 초과합니다.")
    dest.write_bytes(data)
    return dest


# ── offset 영속(포이즌 메시지 재처리 방지) — 어댑터 내부(§2.5) ──
def load_offset(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def save_offset(path: Path, offset: int) -> None:
    # D2: 임시파일에 쓴 뒤 원자적 교체 — 쓰기 중 크래시로 offset 이 손상돼 0 으로 읽혀
    # 미확정 배치가 재수신되는 것을 막는다.
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(offset), encoding="utf-8")
    tmp.replace(path)


_NET_ERRORS = (
    urllib.error.URLError,
    OSError,
    json.JSONDecodeError,
    http.client.HTTPException,
)


# ══════════════════════════════════════════════════════════════════════════
# TelegramAdapter — Adapter 6메서드 구현
# ══════════════════════════════════════════════════════════════════════════
class TelegramAdapter:
    """텔레그램 롱폴링/REST 를 Adapter 계약(poll·send·edit·ack·fetch_file·close)으로 감싼다.

    생성 시 봇토큰 + 마스킹 대상 secrets 를 주입받아 보관 — send/edit 시그니처에 secrets 가 없다
    (마스킹은 어댑터 내부 책임, §2.1).
    """

    def __init__(
        self,
        token: str,
        secrets: list[str],
        offset_file: Path,
        poll_timeout: int = POLL_TIMEOUT,
        limit: int = TELEGRAM_LIMIT,
    ) -> None:
        self.token = token
        self.secrets = secrets
        self.offset_file = offset_file
        self.poll_timeout = poll_timeout
        self.limit = limit
        self._closed = False

    # ── 수신: getUpdates 롱폴링 → 정규화 Event 제너레이터(§2.5) ──
    def poll(self) -> Iterator[Event]:
        offset = load_offset(self.offset_file)
        while not self._closed:
            try:
                resp = tg_call(
                    self.token,
                    "getUpdates",
                    {"timeout": self.poll_timeout, "offset": offset},
                    timeout=self.poll_timeout + 10,
                )
            except _NET_ERRORS as e:
                log.warning("getUpdates 실패(%s) — 5초 후 재시도", type(e).__name__)
                time.sleep(5)
                continue
            if not resp.get("ok"):
                log.warning("getUpdates ok=false — 5초 후 재시도")
                time.sleep(5)
                continue
            for upd in resp.get("result", []):
                # D4: update_id 를 먼저 추출해 offset 을 선진행(포이즌 메시지 재수신 핫루프 방지).
                uid = upd.get("update_id") if isinstance(upd, dict) else None
                if isinstance(uid, int):
                    offset = uid + 1
                yield from self._to_events(upd)
                try:
                    save_offset(self.offset_file, offset)  # 처리 후 영속(§2.5)
                except OSError as e:
                    log.error("offset 저장 실패: %s", type(e).__name__)

    def _to_events(self, upd: Any) -> Iterator[Event]:
        """텔레그램 update dict → 정규화 Event(§1.4). callback/photo/text 만 방출."""
        if not isinstance(upd, dict):
            return
        cq = upd.get("callback_query")
        if isinstance(cq, dict):
            ev = self._callback_event(cq)
            if ev is not None:
                yield ev
            return
        # D6: edited_message 는 무시(신규 message 만 트리거 — 처리한 메시지 편집 재실행 방지).
        msg = upd.get("message")
        if not isinstance(msg, dict):
            return
        chat = msg.get("chat")
        chat_id = chat.get("id") if isinstance(chat, dict) else None
        frm = msg.get("from")
        user_id = frm.get("id") if isinstance(frm, dict) else chat_id  # TG 1:1: from.id==chat.id
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            return
        mid = _int_or_none(msg.get("message_id"))
        photo = msg.get("photo")
        if isinstance(photo, list) and photo:
            caption = msg.get("caption")
            yield Event(
                kind="photo",
                channel_id=chat_id,
                user_id=user_id,
                text=caption if isinstance(caption, str) else "",
                message_id=mid,
                photo_ref=extract_photo(upd),
            )
            return
        # 텍스트 없는 비지원 메시지(스티커 등)는 text="" 로 정규화 → 코어가 "텍스트만 처리" 회신.
        text = msg.get("text")
        yield Event(
            kind="text",
            channel_id=chat_id,
            user_id=user_id,
            text=text if isinstance(text, str) else "",
            message_id=mid,
        )

    def _callback_event(self, cq: dict[str, Any]) -> Event | None:
        frm = cq.get("from")
        from_id = frm.get("id") if isinstance(frm, dict) else None
        message = cq.get("message")
        chat = message.get("chat") if isinstance(message, dict) else None
        chat_id = chat.get("id") if isinstance(chat, dict) else from_id
        if not isinstance(chat_id, int) or not isinstance(from_id, int):
            return None
        data = cq.get("data")
        parsed = parse_callback(data) if isinstance(data, str) else None
        # 미해석 callback_data 는 action="" → 코어가 ack 후 무시(구 parse_callback None 경로 보존).
        action, arg = parsed if parsed is not None else ("", "")
        cq_id = cq.get("id")
        message_id = _int_or_none(message.get("message_id")) if isinstance(message, dict) else None
        return Event(
            kind="button",
            channel_id=chat_id,
            user_id=from_id,
            message_id=message_id,
            action=action,
            action_arg=arg,
            callback_id=cq_id if isinstance(cq_id, str) else None,
        )

    # ── 송신 ──
    def send(self, channel_id: int, text: str, buttons: list[Button] | None = None) -> int | None:
        """마스킹 후 청크 분할 전송. 버튼은 마지막 청크에만. 첫 청크 message_id 반환(실패 None)."""
        chunks = chunk_text(mask_secrets(text, self.secrets), self.limit)
        last = len(chunks) - 1
        first_id: int | None = None
        for i, chunk in enumerate(chunks):
            markup = render_buttons(buttons) if buttons is not None and i == last else None
            mid = self._send_one(channel_id, chunk or "(빈 응답)", markup)
            if i == 0:
                first_id = mid
        return first_id

    def notify(self, user_id: int, text: str, buttons: list[Button] | None = None) -> int | None:
        """H-1: 텔레그램 1:1 은 chat_id==user_id 라 본인 chat 으로 발송 = send 위임(DM 등가)."""
        return self.send(user_id, text, buttons)

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
        head_markup = render_buttons(buttons) if buttons is not None and last == 0 else None
        self._edit_one(channel_id, message_id, chunks[0] or "(빈 응답)", head_markup)
        for j, extra in enumerate(chunks[1:], start=1):
            markup = render_buttons(buttons) if buttons is not None and j == last else None
            self._send_one(channel_id, extra or "(빈 응답)", markup)

    def ack(self, callback_id: str | None, note: str | None = None) -> None:
        """answerCallbackQuery — 로딩 스피너 종료. callback_id=None 이면 no-op. 실패는 로그만."""
        if not callback_id:
            return
        params: dict[str, Any] = {"callback_query_id": callback_id}
        if note:
            params["text"] = note
        try:
            tg_call(self.token, "answerCallbackQuery", params, timeout=30)
        except _NET_ERRORS as e:
            log.warning("answerCallbackQuery 실패: %s", type(e).__name__)

    def fetch_file(self, photo_ref: str, dest_dir: Path) -> Path:
        """getFile → 파일서버 다운로드(2단계 내부 숨김). 보안규칙 위반·실패는 예외 전파(§2.4)."""
        fp = tg_get_file(self.token, photo_ref)
        return download_file(self.token, fp, dest_dir)

    def close(self) -> None:
        self._closed = True

    # ── 내부 전송 헬퍼 ──
    def _send_one(self, channel_id: int, body: str, markup: dict[str, Any] | None) -> int | None:
        params: dict[str, Any] = {"chat_id": channel_id, "text": body}
        if markup is not None:
            params["reply_markup"] = json.dumps(markup)
        try:
            resp = tg_call(self.token, "sendMessage", params, timeout=30)
        except _NET_ERRORS as e:
            log.warning("sendMessage 실패: %s", type(e).__name__)
            return None
        result = resp.get("result")
        mid = result.get("message_id") if isinstance(result, dict) else None
        return mid if isinstance(mid, int) else None

    def _edit_one(
        self, channel_id: int, message_id: int, body: str, markup: dict[str, Any] | None
    ) -> None:
        params: dict[str, Any] = {"chat_id": channel_id, "message_id": message_id, "text": body}
        if markup is not None:
            params["reply_markup"] = json.dumps(markup)
        try:
            tg_call(self.token, "editMessageText", params, timeout=30)
        except _NET_ERRORS as e:
            log.warning("editMessageText 실패: %s", type(e).__name__)


def _int_or_none(v: object) -> int | None:
    return v if isinstance(v, int) else None
