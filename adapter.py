#!/usr/bin/env python3
"""adapter.py — 플랫폼 무관 어댑터 계약(동결: docs/기능/디스코드_이관/02_계약.md §1·§2).

코어는 이 2개 dataclass(`Event`·`Button`)와 `Adapter` 6메서드만 안다. 텔레그램 update /
디스코드 이벤트는 어댑터가 `Event` 로 정규화하고, 코어의 `Button` 리스트를 플랫폼 UI 로 렌더한다.
stdlib 전용 — 플랫폼 라이브러리(urllib·discord.py)는 각 어댑터 파일에만 격리된다.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Button:
    """추상 버튼 스펙. 어댑터가 플랫폼 UI 로 렌더(TG inline_keyboard ↔ DC discord.ui.Button)."""

    label: str
    action: str  # 정규화 액션: "push"|"x"|"p"|"c"|"nb:ok"|"nb:later"
    arg: str = ""  # 액션 인자(프로젝트명·item_id·"mid:idx" 등)
    style: str = "default"  # "default"|"primary"|"danger" — 어댑터가 플랫폼 색으로 매핑


@dataclass(frozen=True)
class Event:
    """TG update / DC 이벤트를 정규화한 코어 입력."""

    kind: str  # "text"|"photo"|"button"|"command"
    channel_id: int  # 격리·라우팅 키 (chat.id ↔ channel.id)
    user_id: int  # 인가 키 (from.id ↔ author.id) — 허용목록 대조
    text: str = ""  # 본문 or 사진 캡션
    message_id: int | None = None  # 편집 대상
    action: str = ""  # 버튼 정규화 액션(kind=="button")
    action_arg: str = ""  # 버튼 인자
    callback_id: str | None = None  # ack 핸들 (callback_query_id ↔ interaction 토큰)
    photo_ref: str | None = None  # 파일 핸들 (file_id ↔ attachment.url)
    project: str | None = None  # DC 는 채널→프로젝트를 어댑터가 미리 채움. TG 는 None


class Adapter(Protocol):
    """플랫폼 어댑터 계약(동결). 코어는 이 인터페이스만 호출한다.

    계약 6메서드(poll·send·edit·ack·fetch_file·close) + H-1 목적 메서드 notify 1개(알림 발송 타겟을
    인가목록과 분리 — 아래 참조). notify 외 6메서드·2 dataclass 는 불변(과설계 금지, §5.1).
    `secrets`: 마스킹 대상(봇토큰·내부경로). 생성 시 주입·보관(§2.1) — 코어의 L-1 진행 마스킹이
    잘라내기 전에 참조하므로 계약면에 노출한다(공유 속성 1개).
    """

    secrets: list[str]

    def poll(self) -> Iterator[Event]:
        """이벤트 소스(블로킹 제너레이터). 네트워크 오류는 내부 로그·재시도, close() 후 종료."""
        ...

    def send(self, channel_id: int, text: str, buttons: list[Button] | None = None) -> int | None:
        """전송(마스킹·청킹·버튼렌더는 어댑터 흡수). 첫 청크 message_id | 실패 시 None."""
        ...

    def notify(self, user_id: int, text: str, buttons: list[Button] | None = None) -> int | None:
        """알림 발송(H-1) — 허용 user_id 개인에게. send 는 채널 전용이라 분리한다.

        인가목록(user_id)을 채널로 오용하면 디스코드에서 get_channel(user_id) 실패로 예약 알림이
        조용히 유실된다. notify 는 user_id → DM(디스코드)/1:1 chat(텔레그램)으로 해석해 발송한다.
        반환·예외 정책은 send 와 동일(실패는 로그·None). 청킹·마스킹·버튼렌더도 send 와 동형.
        """
        ...

    def edit(
        self, channel_id: int, message_id: int, text: str, buttons: list[Button] | None = None
    ) -> None:
        """진행 메시지 in-place 갱신(+오버플로 후속 발행). 실패는 로그만·계속."""
        ...

    def ack(self, callback_id: str | None, note: str | None = None) -> None:
        """버튼 탭 응답(TG answerCallbackQuery / DC followup). callback_id=None 이면 no-op."""
        ...

    def fetch_file(self, photo_ref: str, dest_dir: Path) -> Path:
        """파일 다운로드(도메인·크기·확장자·트래버설 잠금). 위반·실패는 예외 전파."""
        ...

    def close(self) -> None:
        """연결 정리(DC Gateway 종료 / TG no-op). 중복 호출 무해."""
        ...


# ── 플랫폼 무관 공유 유틸 ──────────────────────────────────────────────────
# 코어·모든 어댑터가 공유하므로 순환 import 를 피해 이 shared base 에 둔다(bridge 는
# 재-import 해 자기 표면으로 노출 = "코어 잔류" API 유지, 물리 위치만 여기).
def mask_secrets(text: str, secrets: list[str]) -> str:
    """토큰·내부 경로 등 비밀값을 '***'로 치환. 빈 문자열은 무시(텍스트 파괴 방지)."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


def _valid_id(s: object) -> bool:
    """알림 id 안전 규칙(방출·수신 계약 대칭): 비어있지 않고 ≤54자, [A-Za-z0-9_-] 만.

    이 규칙은 callback_data(nb:ok:<id>·nb:later:<id>) 로 왕복하므로 인바운드(parse_callback)와
    아웃바운드(load_schedules→notify_buttons) 양측이 같은 문을 써야 한다. 상한 54 = TG callback_data
    64B 캡에서 최장 접두 `nb:later:`(9B)를 뺀 여유. 길면 render_buttons 절단으로 왕복이 깨져(탭해도
    매칭 실패) 방출을 여기서 막는다(근본 차단, 실사용 id 는 전부 짧아 무영향).
    """
    return isinstance(s, str) and 0 < len(s) <= 54 and all(c.isalnum() or c in "-_" for c in s)
