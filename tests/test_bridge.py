"""bridge 코어 + 어댑터 계약 단위 테스트.

0단계 어댑터 이관 후: 순수 함수(코어 잔류)는 bridge 에서, 플랫폼 표면(어댑터 이동분)은
telegram_adapter 에서 import. 통합 디스패치는 정규화 `Event` + `FakeAdapter`(Adapter 계약 구현)로
검증한다 — 네트워크·subprocess 없이 코어가 어댑터를 어떻게 호출하는지만 본다(플랫폼 무관 →
0c 디스코드 어댑터에도 그대로 재사용). 검증 의미(동작)는 텔레그램 시절과 동일하게 보존한다.
"""

import dataclasses
import json
import os
import subprocess
import sys
import time
import urllib.error
from datetime import datetime
from pathlib import Path

import bridge
import pytest
import telegram_adapter
from adapter import Button, Event, _valid_id
from bridge import (
    build_compare_prompt,
    choice_buttons,
    due_notifications,
    due_snoozes,
    event_to_progress,
    fetch_stock,
    format_reply,
    handle_event,
    is_allowed,
    load_notify_state,
    load_project_labels,
    load_schedules,
    mask_secrets,
    notify_buttons,
    parse_caption_ticker,
    parse_choice_prompt,
    parse_message,
    parse_stock_response,
    project_buttons,
    project_label,
    push_buttons,
    resolve_project,
    resolve_target,
    run_claude,
    save_notify_state,
    stock_url,
    valid_ticker,
)
from telegram_adapter import (
    TelegramAdapter,
    chunk_text,
    download_file,
    encode_callback,
    extract_photo,
    load_offset,
    parse_callback,
    render_buttons,
    save_offset,
)

_ALLOWED = frozenset({777})
_ALLOWED2 = frozenset({777, 888})


class FakeAdapter:
    """Adapter 계약(secrets·poll·send·edit·ack·fetch_file·close) 구현 — 호출 기록용 테스트 더블."""

    def __init__(self, secrets=None, send_ids=None, fetch=None):
        self.secrets = secrets if secrets is not None else []
        self.sent = []  # (channel_id, text, buttons)
        self.notified = []  # (user_id, text, buttons) — H-1 알림 발송 타겟
        self.edited = []  # (channel_id, message_id, text, buttons)
        self.acked = []  # (callback_id, note)
        self.fetched = []  # (photo_ref, dest_dir)
        self.saves = []  # dispatch/nb 상태 저장 스파이용(테스트가 채움)
        self.runs = []  # run_claude_with_progress 스파이용(테스트가 채움)
        self._send_ids = iter(send_ids) if send_ids is not None else None
        self._fetch = fetch

    def poll(self):
        return iter(())

    def send(self, channel_id, text, buttons=None):
        self.sent.append((channel_id, text, buttons))
        if self._send_ids is not None:
            return next(self._send_ids, None)
        return 1

    def notify(self, user_id, text, buttons=None):
        self.notified.append((user_id, text, buttons))
        if self._send_ids is not None:
            return next(self._send_ids, None)
        return 1

    def edit(self, channel_id, message_id, text, buttons=None):
        self.edited.append((channel_id, message_id, text, buttons))

    def ack(self, callback_id, note=None):
        self.acked.append((callback_id, note))

    def fetch_file(self, photo_ref, dest_dir):
        self.fetched.append((photo_ref, dest_dir))
        if isinstance(self._fetch, BaseException):
            raise self._fetch
        if callable(self._fetch):
            return self._fetch(photo_ref, dest_dir)
        return Path(dest_dir) / "x.jpg"

    def close(self):
        pass


def _btn(user_id, action, arg="", *, message_id=99, callback_id="cq1", channel_id=None):
    """정규화 버튼 Event(어댑터가 parse_callback 로 만든 것과 동형)."""
    return Event(
        kind="button",
        channel_id=channel_id if channel_id is not None else user_id,
        user_id=user_id,
        action=action,
        action_arg=arg,
        message_id=message_id,
        callback_id=callback_id,
    )


def _txt(user_id, text, *, message_id=None, channel_id=None):
    return Event(
        kind="text",
        channel_id=channel_id if channel_id is not None else user_id,
        user_id=user_id,
        text=text,
        message_id=message_id,
    )


def _photo(user_id, caption="MU", *, photo_ref="f", channel_id=None):
    return Event(
        kind="photo",
        channel_id=channel_id if channel_id is not None else user_id,
        user_id=user_id,
        text=caption if caption is not None else "",
        photo_ref=photo_ref,
    )


def _fire(
    adapter,
    event,
    allowed=_ALLOWED,
    *,
    repo_root=None,
    target_root="root",
    claude_exe="claude",
    timeout=900,
):
    handle_event(
        adapter,
        event,
        allowed=allowed,
        claude_exe=claude_exe,
        repo_root=repo_root if repo_root is not None else Path(),
        target_root=target_root,
        timeout=timeout,
    )


def _assistant(*blocks):
    """assistant 이벤트 헬퍼 — message.content 블록 리스트로 감싼다."""
    return {"type": "assistant", "message": {"content": list(blocks)}}


# ---------------------------------------------------------------------------
# §5.2 #1 타입 불변성: Event·Button 은 frozen dataclass (필드 변이 차단)
# ---------------------------------------------------------------------------


def test_event_is_frozen_dataclass():
    ev = Event(kind="text", channel_id=1, user_id=2)
    assert dataclasses.is_dataclass(ev)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.user_id = 999  # 인가 키 변조 차단(코어 신뢰 입력 불변)


def test_button_is_frozen_dataclass():
    b = Button("L", "push")
    assert dataclasses.is_dataclass(b)
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.action = "x"


# ---------------------------------------------------------------------------
# parse_message: "<프로젝트> <지시...>" → (project, task) / 커맨드·형식불일치는 None
# ---------------------------------------------------------------------------


def test_parse_message_normal_two_words():
    assert parse_message("trading_info 헤더고쳐줘") == ("trading_info", "헤더고쳐줘")


def test_parse_message_multiword_task():
    assert parse_message("trading_info 헤더를 3행으로 정렬해줘") == (
        "trading_info",
        "헤더를 3행으로 정렬해줘",
    )


def test_parse_message_strips_surrounding_whitespace():
    assert parse_message("   trading_info   헤더 고쳐줘  ") == (
        "trading_info",
        "헤더 고쳐줘",
    )


def test_parse_message_single_word_is_none():
    assert parse_message("trading_info") is None


def test_parse_message_empty_string_is_none():
    assert parse_message("") is None


def test_parse_message_whitespace_only_is_none():
    assert parse_message("     ") is None


def test_parse_message_push_command_is_none():
    assert parse_message("push") is None


def test_parse_message_help_command_is_none():
    assert parse_message("/help") is None


def test_parse_message_projects_command_is_none():
    assert parse_message("/projects") is None


# ---------------------------------------------------------------------------
# push 별칭(PUSH_WORDS): 한글 "푸시" 계열도 push 라우팅. 정확 일치만.
# ---------------------------------------------------------------------------


def test_push_words_all_in_commands():
    assert bridge.PUSH_WORDS <= bridge.COMMANDS


def test_parse_message_push_aliases_are_none():
    for word in bridge.PUSH_WORDS:
        assert parse_message(word) is None


def test_parse_message_sentence_with_push_word_still_parses():
    assert parse_message("기록해주고 푸시해줘") == ("기록해주고", "푸시해줘")


def test_push_words_exact_match_only():
    assert "푸시해" in bridge.PUSH_WORDS
    assert "기록해주고 푸시해줘" not in bridge.PUSH_WORDS
    assert "push" in bridge.PUSH_WORDS


def test_push_word_casefold_matches_uppercase():
    for variant in ("Push", "PUSH", "pUsH"):
        assert variant.casefold() in bridge.PUSH_WORDS


def _fold(s):
    return "".join(s.split()).casefold()


def test_push_word_inner_space_folded():
    for variant in ("푸시 해줘", "푸시 해", "푸 시", "PUSH  "):
        assert _fold(variant) in bridge.PUSH_WORDS
    assert _fold("기록해주고 푸시해줘") not in bridge.PUSH_WORDS


def test_push_inner_space_routes_to_do_push(monkeypatch, tmp_path):
    # #2 배선: "푸시 해줘"(중간 공백)가 handle_event 텍스트 분기에서 do_push 로 라우팅되는지.
    pushes = []
    monkeypatch.setattr(bridge, "do_push", lambda root: pushes.append(root) or bridge.HEADER_DONE)
    fa = FakeAdapter()
    _fire(fa, _txt(777, "푸시 해줘"), repo_root=tmp_path, target_root=str(tmp_path))
    assert len(pushes) == 1  # do_push 호출됨
    assert fa.sent  # 결과 회신


# ---------------------------------------------------------------------------
# is_allowed(chat_id, allowed)
# ---------------------------------------------------------------------------


def test_is_allowed_true_when_in_set():
    assert is_allowed(12345, frozenset({12345, 67890})) is True


def test_is_allowed_false_when_not_in_set():
    assert is_allowed(99999, frozenset({12345, 67890})) is False


def test_is_allowed_false_when_empty_allowlist():
    assert is_allowed(12345, frozenset()) is False


# ---------------------------------------------------------------------------
# resolve_project: target_root 직속 폴더명 정확 일치만 / 트래버설 거부
# ---------------------------------------------------------------------------


def test_resolve_project_exact_match_success(tmp_path):
    (tmp_path / "trading_info").mkdir()
    result = resolve_project("trading_info", str(tmp_path))
    assert result is not None
    assert Path(result).name == "trading_info"
    assert Path(result).is_dir()


def test_resolve_project_case_insensitive_unique_fallback(tmp_path):
    (tmp_path / "trading_info").mkdir()
    result = resolve_project("Trading_Info", str(tmp_path))
    assert result is not None
    assert Path(result).name == "trading_info"
    assert Path(result).is_dir()


def test_resolve_project_exact_match_precedence(tmp_path):
    (tmp_path / "logs").mkdir()
    assert resolve_project("logs", str(tmp_path)) == str(tmp_path / "logs")


def test_resolve_project_partial_match_rejected(tmp_path):
    (tmp_path / "trading_info").mkdir()
    assert resolve_project("trading", str(tmp_path)) is None


def test_resolve_project_nonexistent_rejected(tmp_path):
    (tmp_path / "trading_info").mkdir()
    assert resolve_project("etf_info", str(tmp_path)) is None


def test_resolve_project_parent_traversal_rejected(tmp_path):
    assert resolve_project("..", str(tmp_path)) is None


def test_resolve_project_forward_slash_rejected(tmp_path):
    (tmp_path / "a").mkdir()
    assert resolve_project("a/b", str(tmp_path)) is None


def test_resolve_project_backslash_rejected(tmp_path):
    (tmp_path / "a").mkdir()
    assert resolve_project("a\\b", str(tmp_path)) is None


def test_resolve_project_absolute_path_rejected(tmp_path):
    real = tmp_path / "realproj"
    real.mkdir()
    assert resolve_project(str(real), str(tmp_path)) is None


def test_resolve_project_empty_name_rejected(tmp_path):
    assert resolve_project("", str(tmp_path)) is None


# ---------------------------------------------------------------------------
# resolve_target: ④ chat 선택 고정 해석
# ---------------------------------------------------------------------------


def test_resolve_target_explicit_project_first_word(tmp_path):
    (tmp_path / "trading_info").mkdir()
    got = resolve_target("trading_info 헤더 고쳐줘", str(tmp_path), None)
    assert got is not None
    name, path, task = got
    assert name == "trading_info"
    assert Path(path).name == "trading_info"
    assert task == "헤더 고쳐줘"


def test_resolve_target_uses_selection_when_first_word_not_project(tmp_path):
    (tmp_path / "trading_info").mkdir()
    got = resolve_target("시간대 별로 체크하는거 각 몇시에 오지?", str(tmp_path), "trading_info")
    assert got is not None
    name, _path, task = got
    assert name == "trading_info"
    assert task == "시간대 별로 체크하는거 각 몇시에 오지?"


def test_resolve_target_explicit_overrides_selection(tmp_path):
    (tmp_path / "trading_info").mkdir()
    (tmp_path / "etf_info").mkdir()
    name, path, task = resolve_target("etf_info 로그 봐줘", str(tmp_path), "trading_info")
    assert name == "etf_info"
    assert Path(path).name == "etf_info"
    assert task == "로그 봐줘"


def test_resolve_target_no_selection_no_project_none(tmp_path):
    (tmp_path / "trading_info").mkdir()
    assert resolve_target("시간대 별로 체크", str(tmp_path), None) is None


def test_resolve_target_stale_selection_rejected(tmp_path):
    assert resolve_target("작업 해줘", str(tmp_path), "gone_project") is None


def test_resolve_target_bare_project_name_empty_task(tmp_path):
    (tmp_path / "trading_info").mkdir()
    name, _path, task = resolve_target("trading_info", str(tmp_path), None)
    assert name == "trading_info"
    assert task == ""


def test_resolve_target_traversal_first_word_falls_through_to_selection(tmp_path):
    (tmp_path / "trading_info").mkdir()
    got = resolve_target("../etc 해줘", str(tmp_path), "trading_info")
    assert got is not None
    name, _path, task = got
    assert name == "trading_info"
    assert task == "../etc 해줘"


# ---------------------------------------------------------------------------
# chunk_text (telegram_adapter 로 이동 — 로직 무변경)
# ---------------------------------------------------------------------------


def test_chunk_text_under_limit_single_chunk():
    text = "a" * 100
    assert chunk_text(text, 4096) == [text]


def test_chunk_text_exactly_at_limit_single_chunk():
    text = "a" * 4096
    chunks = chunk_text(text, 4096)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunk_text_one_over_limit_splits_into_two():
    text = "a" * 4097
    chunks = chunk_text(text, 4096)
    assert len(chunks) == 2
    assert len(chunks[0]) == 4096
    assert len(chunks[1]) == 1


def test_chunk_text_empty_returns_list_with_empty_string():
    assert chunk_text("", 4096) == [""]


def test_chunk_text_every_chunk_within_limit():
    text = "b" * (4096 * 2 + 37)
    chunks = chunk_text(text, 4096)
    assert len(chunks) == 3
    assert all(len(c) <= 4096 for c in chunks)


def test_chunk_text_reconstructs_original_no_data_loss():
    text = "가나다" * 5000
    assert "".join(chunk_text(text, 4096)) == text


def test_chunk_text_custom_limit():
    chunks = chunk_text("abcde", limit=2)
    assert chunks == ["ab", "cd", "e"]
    assert all(len(c) <= 2 for c in chunks)


# ---------------------------------------------------------------------------
# mask_secrets (adapter 공유 유틸, bridge 재-export)
# ---------------------------------------------------------------------------


def test_mask_secrets_single_value():
    assert mask_secrets("token=abc123", ["abc123"]) == "token=***"


def test_mask_secrets_multiple_values():
    assert mask_secrets("id=42 token=xyz", ["42", "xyz"]) == "id=*** token=***"


def test_mask_secrets_all_occurrences_replaced():
    assert mask_secrets("xyz and xyz", ["xyz"]) == "*** and ***"


def test_mask_secrets_empty_list_keeps_original():
    assert mask_secrets("nothing secret here", []) == "nothing secret here"


def test_mask_secrets_empty_secret_string_does_not_destroy_text():
    # 빈 비밀문자열("")은 무시돼야 한다(str.replace("", "***") 텍스트 폭증 버그 방지).
    assert mask_secrets("hello", ["", "ell"]) == "h***o"


def test_mask_secrets_only_empty_secret_keeps_original():
    assert mask_secrets("hello", [""]) == "hello"


# ---------------------------------------------------------------------------
# format_reply(data)
# ---------------------------------------------------------------------------


def test_format_reply_success_header_no_cost():
    reply = format_reply({"result": "작업 완료", "is_error": False, "total_cost_usd": 0.05})
    assert reply.startswith("[ ✅처리완료 ]")
    assert "작업 완료" in reply
    assert "비용" not in reply
    assert "push" not in reply
    assert "커밋" not in reply


def test_format_reply_error_header():
    reply = format_reply({"result": "실행 실패", "is_error": True})
    assert reply.startswith("[ ❌처리실패 ]")
    assert "실행 실패" in reply
    assert "비용" not in reply


def test_format_reply_empty_result_header_only():
    assert format_reply({"result": "", "is_error": False}) == "[ ✅처리완료 ]"


def test_format_reply_error_empty_result_header_only():
    assert format_reply({"result": "", "is_error": True}) == "[ ❌처리실패 ]"


# ---------------------------------------------------------------------------
# event_to_progress(event) (순수, 코어 잔류)
# ---------------------------------------------------------------------------


def test_event_to_progress_text_narration():
    ev = _assistant({"type": "text", "text": "파일 목록을 확인합니다"})
    assert event_to_progress(ev) == "파일 목록을 확인합니다"


def test_event_to_progress_text_truncated_to_120():
    ev = _assistant({"type": "text", "text": "가" * 200})
    assert event_to_progress(ev) == "가" * 120


def test_event_to_progress_text_stripped():
    ev = _assistant({"type": "text", "text": "  여백 제거  "})
    assert event_to_progress(ev) == "여백 제거"


def test_event_to_progress_masks_secret_before_truncation():
    secret = "C:\\Users\\Home"
    cmd = "a" * 55 + secret + "tail"
    ev = _assistant({"type": "tool_use", "name": "Bash", "input": {"command": cmd}})
    line = event_to_progress(ev, [secret])
    assert secret not in line
    assert "C:\\Us" not in line
    assert "***" in line


def test_event_to_progress_empty_text_is_none():
    assert event_to_progress(_assistant({"type": "text", "text": "   "})) is None


def test_event_to_progress_read_basename_only():
    ev = _assistant({"type": "tool_use", "name": "Read", "input": {"file_path": "E:/a/b/br.py"}})
    assert event_to_progress(ev) == "📖 읽음: br.py"


def test_event_to_progress_edit_basename():
    ev = _assistant({"type": "tool_use", "name": "Edit", "input": {"file_path": "/x/y/app.py"}})
    assert event_to_progress(ev) == "✏️ 수정: app.py"


def test_event_to_progress_write_basename():
    ev = _assistant({"type": "tool_use", "name": "Write", "input": {"file_path": "note.md"}})
    assert event_to_progress(ev) == "✏️ 수정: note.md"


def test_event_to_progress_bash_command_prefix():
    ev = _assistant({"type": "tool_use", "name": "Bash", "input": {"command": "git commit -m x"}})
    assert event_to_progress(ev) == "⚡ 실행: git commit -m x"


def test_event_to_progress_bash_command_truncated_to_60():
    ev = _assistant({"type": "tool_use", "name": "Bash", "input": {"command": "a" * 100}})
    assert event_to_progress(ev) == "⚡ 실행: " + "a" * 60


def test_event_to_progress_other_tool_generic_icon():
    ev = _assistant({"type": "tool_use", "name": "Glob", "input": {"pattern": "*"}})
    assert event_to_progress(ev) == "🔧 Glob"


def test_event_to_progress_thinking_is_none():
    ev = _assistant({"type": "thinking", "thinking": "x", "signature": "y"})
    assert event_to_progress(ev) is None


def test_event_to_progress_system_init_is_none():
    assert event_to_progress({"type": "system", "subtype": "init", "model": "opus"}) is None


def test_event_to_progress_result_is_none():
    assert event_to_progress({"type": "result", "subtype": "success", "result": "DONE"}) is None


def test_event_to_progress_tool_result_is_none():
    ev = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "x"}]},
    }
    assert event_to_progress(ev) is None


def test_event_to_progress_rate_limit_is_none():
    assert event_to_progress({"type": "rate_limit_event", "rate_limit_info": {}}) is None


def test_event_to_progress_missing_file_path_placeholder():
    ev = _assistant({"type": "tool_use", "name": "Read", "input": {}})
    assert event_to_progress(ev) == "📖 읽음: ?"


def test_event_to_progress_malformed_content_is_none():
    assert event_to_progress({"type": "assistant", "message": {"content": "oops"}}) is None
    assert event_to_progress({"type": "assistant"}) is None


def test_event_to_progress_text_masks_secret():
    secret = "1234567890:ABCsecrettoken"
    ev = _assistant({"type": "text", "text": f"토큰은 {secret} 입니다"})
    line = event_to_progress(ev, [secret])
    assert line is not None
    assert secret not in line
    assert "***" in line


# ---------------------------------------------------------------------------
# git_status_note / do_push: _git 을 monkeypatch 해 분기 검증 (코어 잔류)
# ---------------------------------------------------------------------------


def _fake_git(mapping):
    def fake(_root, *args):
        for key, (rc, out, err) in mapping.items():
            if args[: len(key)] == key:
                return subprocess.CompletedProcess(["git", *args], rc, out, err)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    return fake


def test_git_status_note_ahead_dirty(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "3\n", ""), ("status",): (0, " M bridge.py\n", "")}),
    )
    note = bridge.git_status_note(Path())
    assert "3" in note
    assert "미커밋" in note


def test_git_status_note_ahead_clean(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "2\n", ""), ("status",): (0, "", "")}),
    )
    note = bridge.git_status_note(Path())
    assert "2" in note
    assert "미커밋" not in note


def test_git_status_note_no_ahead_dirty(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "0\n", ""), ("status",): (0, " M x.py\n", "")}),
    )
    assert bridge.git_status_note(Path()) == "변경이 있으나 커밋되지 않았습니다(확인 필요)."


def test_git_status_note_no_ahead_clean(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "0\n", ""), ("status",): (0, "", "")}),
    )
    assert bridge.git_status_note(Path()) == "변경 없음."


def test_git_status_note_revlist_fail_fallback(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (128, "", "fatal"), ("status",): (0, " M x.py\n", "")}),
    )
    assert bridge.git_status_note(Path()) == "변경이 있으나 커밋되지 않았습니다(확인 필요)."


def test_git_status_note_status_fail_fallback(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "0\n", ""), ("status",): (1, "", "fatal")}),
    )
    assert bridge.git_status_note(Path()) == "변경 없음."


def test_do_push_pull_fail_aborts(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("pull",): (1, "", "CONFLICT tail"), ("rebase",): (0, "", "")}),
    )
    result = bridge.do_push(Path())
    assert result.startswith(bridge.HEADER_FAIL)
    assert "pull --rebase 실패" in result
    assert "CONFLICT tail" in result


def test_do_push_push_fail(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("pull",): (0, "", ""), ("push",): (1, "", "rejected tail")}),
    )
    result = bridge.do_push(Path())
    assert result.startswith(bridge.HEADER_FAIL)
    assert "push 실패" in result
    assert "rejected tail" in result


def test_do_push_success(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("pull",): (0, "", ""), ("push",): (0, "", "")}),
    )
    assert bridge.do_push(Path()).startswith(bridge.HEADER_DONE)


def test_do_push_pull_uses_autostash(monkeypatch):
    seen = []

    def spy(_root, *args):
        seen.append(args)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(bridge, "_git", spy)
    bridge.do_push(Path())
    pull = next(a for a in seen if a[0] == "pull")
    assert "--autostash" in pull


def test_do_push_autostash_pop_conflict_isolates_and_warns(monkeypatch):
    seen = []

    def spy(_root, *args):
        seen.append(args)
        if args[:2] == ("ls-files", "-u"):
            return subprocess.CompletedProcess(["git", *args], 0, "100644 abc 1\tfile\n", "")
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(bridge, "_git", spy)
    result = bridge.do_push(Path())
    assert result.startswith(bridge.HEADER_DONE)
    assert "stash" in result and "⚠️" in result
    assert ("reset", "--hard", "HEAD") in seen
    assert any(a[0] == "push" for a in seen)


def test_do_push_no_pop_conflict_no_warning(monkeypatch):
    seen = []

    def spy(_root, *args):
        seen.append(args)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(bridge, "_git", spy)
    result = bridge.do_push(Path())
    assert result.startswith(bridge.HEADER_DONE)
    assert "stash" not in result and "⚠️" not in result
    assert ("reset", "--hard", "HEAD") not in seen


# ---------------------------------------------------------------------------
# Button 빌더(코어) + render_buttons(어댑터): 구 특화 키보드 4종 대체
# ---------------------------------------------------------------------------


def test_project_buttons_empty_renders_no_buttons():
    assert project_buttons([]) == []
    assert render_buttons([]) == {"inline_keyboard": []}


def test_project_buttons_action_arg_and_render(monkeypatch):
    monkeypatch.setattr(bridge, "PROJECT_LABELS", {"demo_proj": "데모 라벨"})
    btns = project_buttons(["demo_proj"])
    assert btns[0] == Button("데모 라벨", "p", "demo_proj")
    kb = render_buttons(btns)
    btn = kb["inline_keyboard"][0][0]
    assert btn["text"] == "데모 라벨"  # 표시는 등록 라벨
    assert btn["callback_data"] == "p:demo_proj"  # 라우팅은 폴더명 그대로


def test_project_label_registered_and_humanize(monkeypatch):
    monkeypatch.setattr(bridge, "PROJECT_LABELS", {"demo_proj": "데모 라벨"})
    assert project_label("demo_proj") == "데모 라벨"
    assert project_label("some_new_proj") == "some new proj"
    assert project_label("a-b_c") == "a b c"
    assert project_label("") == ""
    assert project_label("__") == "__"


def test_load_project_labels_normal(tmp_path):
    p = tmp_path / "project_labels.json"
    p.write_text('{"labels": {"trading_info": "주식 모니터링", "x": "엑스"}}', encoding="utf-8")
    assert load_project_labels(p) == {"trading_info": "주식 모니터링", "x": "엑스"}


def test_load_project_labels_missing_file_empty(tmp_path):
    assert load_project_labels(tmp_path / "nope.json") == {}


def test_load_project_labels_corrupt_empty(tmp_path):
    p = tmp_path / "project_labels.json"
    p.write_text("{ not json", encoding="utf-8")
    assert load_project_labels(p) == {}


def test_load_project_labels_no_labels_key_empty(tmp_path):
    p = tmp_path / "project_labels.json"
    p.write_text('{"other": {"a": "b"}}', encoding="utf-8")
    assert load_project_labels(p) == {}


def test_load_project_labels_drops_non_str_values(tmp_path):
    p = tmp_path / "project_labels.json"
    p.write_text('{"labels": {"ok": "라벨", "bad": 123, "list": ["x"]}}', encoding="utf-8")
    assert load_project_labels(p) == {"ok": "라벨"}


def test_load_project_labels_bom_absorbed(tmp_path):
    p = tmp_path / "project_labels.json"
    p.write_text('{"labels": {"trading_info": "주식 모니터링"}}', encoding="utf-8-sig")
    assert load_project_labels(p) == {"trading_info": "주식 모니터링"}


def test_load_project_labels_cp949_falls_back_empty(tmp_path):
    p = tmp_path / "project_labels.json"
    p.write_bytes('{"labels": {"x": "한글"}}'.encode("cp949"))
    assert load_project_labels(p) == {}


def test_render_buttons_two_per_row():
    kb = render_buttons(project_buttons(["a", "b", "c", "d", "e"]))
    rows = kb["inline_keyboard"]
    assert [len(r) for r in rows] == [2, 2, 1]
    assert rows[0][1]["callback_data"] == "p:b"
    assert rows[2][0]["callback_data"] == "p:e"


def test_render_buttons_callback_data_within_64_bytes():
    long_name = "가" * 100  # 3바이트 * 100 = 300바이트
    kb = render_buttons(project_buttons([long_name]))
    btn = kb["inline_keyboard"][0][0]
    assert btn["text"] == long_name  # 표시는 전체
    assert len(btn["callback_data"].encode("utf-8")) <= 64
    assert btn["callback_data"].startswith("p:가")  # 부분 멀티바이트 안 깨짐


def test_push_buttons_structure():
    kb = render_buttons(push_buttons())
    row = kb["inline_keyboard"][0]
    assert [b["callback_data"] for b in row] == ["push", "x"]
    assert row[0]["text"] == "✅ Push"
    assert row[1]["text"] == "❌ 취소"


def test_notify_buttons_callback_data():
    kb = render_buttons(notify_buttons("ti-kospi-open"))
    row = kb["inline_keyboard"][0]
    assert [b["callback_data"] for b in row] == ["nb:ok:ti-kospi-open", "nb:later:ti-kospi-open"]


def test_valid_id_prefix_budget_prevents_truncation_roundtrip():
    # 회귀 잠금(Low): TG callback_data 64B 캡 - 최장 접두 `nb:later:`(9B) → 55 여유. _valid_id 상한
    # 54 는 그 안쪽이라 54자 id 는 절단 없이 왕복 항등(탭 매칭 성공). 55자+ 는 방출 자체가 거부돼
    # 절단→왕복 불일치를 원천 차단한다.
    id54 = "a" * 54
    assert _valid_id(id54) is True
    cb = encode_callback("nb:later", id54)  # 최장 접두
    assert len(cb.encode("utf-8")) <= 64  # 절단 없음
    assert parse_callback(cb) == ("nb:later", id54)  # 왕복 항등
    assert _valid_id("a" * 55) is False  # 초과 → 방출 거부


def test_choice_buttons_structure():
    kb = render_buttons(choice_buttons(77, [("유지", "keep"), ("교체", "swap")]))
    flat = [b for row in kb["inline_keyboard"] for b in row]
    assert flat[0]["callback_data"] == "c:77:0"
    assert flat[1]["callback_data"] == "c:77:1"
    assert flat[-1] == {"text": "✏️ 직접입력", "callback_data": "c:77:other"}


def test_choice_buttons_two_per_row():
    kb = render_buttons(choice_buttons(1, [("a", "1"), ("b", "2"), ("c", "3")]))
    # 3 선택지 + 직접입력 = 4버튼 → 2개씩 2행.
    assert [len(r) for r in kb["inline_keyboard"]] == [2, 2]


def test_choice_buttons_callback_data_within_64_bytes():
    kb = render_buttons(choice_buttons(123456, [("긴라벨" * 30, "v")]))
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


# ---------------------------------------------------------------------------
# parse_callback / encode_callback (telegram_adapter): 콜백 프로토콜 왕복
# ---------------------------------------------------------------------------


def test_parse_callback_push():
    assert parse_callback("push") == ("push", "")


def test_parse_callback_cancel():
    assert parse_callback("x") == ("x", "")


def test_parse_callback_project():
    assert parse_callback("p:trading_info") == ("p", "trading_info")


def test_parse_callback_empty_project_name_rejected():
    assert parse_callback("p:") is None


def test_parse_callback_unknown_rejected():
    assert parse_callback("bogus") is None
    assert parse_callback("") is None
    assert parse_callback("push extra") is None


def test_parse_callback_nb_ok():
    assert parse_callback("nb:ok:ti-rollover") == ("nb:ok", "ti-rollover")


def test_parse_callback_nb_later():
    assert parse_callback("nb:later:ti-rollover") == ("nb:later", "ti-rollover")


def test_parse_callback_nb_empty_id_rejected():
    assert parse_callback("nb:ok:") is None
    assert parse_callback("nb:later:") is None


def test_parse_callback_nb_unsafe_id_rejected():
    assert parse_callback("nb:ok:bad/id") is None
    assert parse_callback("nb:ok:a b") is None
    assert parse_callback("nb:ok:" + "z" * 65) is None


def test_parse_callback_choice_index():
    assert parse_callback("c:55:0") == ("c", "55:0")
    assert parse_callback("c:55:12") == ("c", "55:12")


def test_parse_callback_choice_other():
    assert parse_callback("c:55:other") == ("c", "55:other")


def test_parse_callback_choice_rejects_bad():
    assert parse_callback("c:x:1") is None
    assert parse_callback("c:55:bad") is None
    assert parse_callback("c:55") is None
    assert parse_callback("c:55:1:2") is None


def test_parse_callback_choice_rejects_unicode_digits():
    assert parse_callback("c:" + chr(0xFF15) * 2 + ":1") is None  # 전각 숫자 msg_id
    assert parse_callback("c:55:" + chr(0x00B2)) is None  # 위첨자 숫자 idx


def test_encode_callback_is_inverse_of_parse():
    # §1.3 7종: encode(디코드 결과) == 원 문자열(무손실 왕복).
    for data in (
        "push",
        "x",
        "p:etf_info",
        "nb:ok:ti-open",
        "nb:later:ti-roll",
        "c:55:1",
        "c:55:other",
    ):
        parsed = parse_callback(data)
        assert parsed is not None
        assert encode_callback(*parsed) == data


def test_render_buttons_callback_within_tg_limit():
    # id≤64·name≤64 라 인코드 결과가 64바이트 캡 안(TG)·100자(DC) 여유.
    btns = [Button("L", "p", "x" * 64), Button("L", "nb:ok", "y" * 64)]
    for row in render_buttons(btns)["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


# ===========================================================================
# run_claude 스트리밍 리더(D-1/D-2/D-3) 통합 — 가짜 claude 실행 파일 (코어 잔류)
# ===========================================================================

FAKE_CLAUDE_PY = """\
import json
import sys
import time

data = sys.stdin.read()


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


if "STDERR_FLOOD" in data:
    for i in range(3000):
        sys.stderr.write("noise %d filler filler filler filler\\n" % i)
    sys.stderr.flush()

if "NO_RESULT" in data:
    sys.stderr.write("fatal: fake claude crashed\\n")
    sys.stderr.flush()
    sys.exit(3)

emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}})
emit({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "DONE_FAKE", "total_cost_usd": 0.01,
})

if "HANG" in data:
    time.sleep(30)
"""


def _fake_claude(tmp_path):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE_PY, encoding="utf-8")
    if os.name == "nt":
        shim = tmp_path / "fake_claude.cmd"
        shim.write_text(f'@echo off\r\n"{sys.executable}" "{script}"\r\n', encoding="utf-8")
    else:
        shim = tmp_path / "fake_claude.sh"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n', encoding="utf-8")
        shim.chmod(0o755)
    return str(shim)


def test_run_claude_normal_completion_returns_result(tmp_path):
    exe = _fake_claude(tmp_path)
    data = run_claude(exe, str(tmp_path), "just do it", timeout=30)
    assert data.get("result") == "DONE_FAKE"
    assert data.get("is_error") is False


def test_run_claude_breaks_on_result_before_timeout(tmp_path):
    exe = _fake_claude(tmp_path)
    start = time.monotonic()
    data = run_claude(exe, str(tmp_path), "HANG please", timeout=30)
    elapsed = time.monotonic() - start
    assert data.get("result") == "DONE_FAKE"
    assert data.get("is_error") is False
    assert elapsed < 20


def test_run_claude_stderr_flood_no_deadlock(tmp_path):
    exe = _fake_claude(tmp_path)
    start = time.monotonic()
    data = run_claude(exe, str(tmp_path), "STDERR_FLOOD then work", timeout=30)
    elapsed = time.monotonic() - start
    assert data.get("result") == "DONE_FAKE"
    assert elapsed < 20


def test_run_claude_no_result_falls_back_to_stderr(tmp_path):
    exe = _fake_claude(tmp_path)
    data = run_claude(exe, str(tmp_path), "NO_RESULT crash", timeout=30)
    assert data.get("is_error") is True
    assert "fatal" in str(data.get("result", ""))


# ===========================================================================
# handle_event 버튼 분기(구 handle_callback) — FakeAdapter 로 인가·라우팅 검증
# ===========================================================================


@pytest.fixture
def cb_env(monkeypatch):
    """FakeAdapter + do_push 스파이(코어 잔류 함수만 monkeypatch)."""
    pushes = []
    monkeypatch.setattr(
        bridge, "do_push", lambda root: pushes.append(root) or (bridge.HEADER_DONE + "\n\npush ok")
    )
    fa = FakeAdapter()
    fa.pushes = pushes
    return fa


def test_button_disallowed_user_nothing_called(cb_env, tmp_path):
    # 미허용 user 는 허용목록 게이트에서 즉시 거부 — ack·push·send 전부 미호출.
    _fire(cb_env, _btn(999, "push"), repo_root=tmp_path, target_root=str(tmp_path))
    assert cb_env.acked == [] and cb_env.sent == [] and cb_env.edited == []
    assert cb_env.pushes == []


def test_gate_keys_on_user_id_not_channel_id(cb_env, tmp_path):
    # §3.1 핵심 인가 전환(chat.id→user_id) 회귀 잠금 — 그룹 시나리오:
    # channel_id 는 허용값(777)이지만 발신 user_id(999)는 비허용 → 반드시 차단.
    # 게이트가 channel_id 로 되돌아가면(777 허용) 이 테스트가 실패한다.
    _fire(cb_env, _btn(999, "push", channel_id=777), repo_root=tmp_path, target_root=str(tmp_path))
    assert cb_env.pushes == [] and cb_env.acked == [] and cb_env.edited == []


def test_gate_allows_user_regardless_of_channel(cb_env, tmp_path):
    # 게이트 키는 user_id 단일 — 허용 user 면 channel_id 가 허용목록에 없어도 통과.
    _fire(
        cb_env, _btn(777, "push", channel_id=123456), repo_root=tmp_path, target_root=str(tmp_path)
    )
    assert len(cb_env.pushes) == 1


def test_button_valid_project_sends_guide(cb_env, tmp_path):
    (tmp_path / "etf_info").mkdir()
    _fire(cb_env, _btn(777, "p", "etf_info"), repo_root=tmp_path, target_root=str(tmp_path))
    assert cb_env.pushes == []
    assert len(cb_env.sent) == 1
    chat_id, text, _b = cb_env.sent[0]
    assert chat_id == 777
    assert "etf_info" in text


def test_button_invalid_project_no_send(cb_env, tmp_path):
    _fire(cb_env, _btn(777, "p", "../secret"), repo_root=tmp_path, target_root=str(tmp_path))
    assert cb_env.sent == []
    assert cb_env.pushes == []


def test_button_push_calls_do_push_and_edits(cb_env, tmp_path):
    _fire(cb_env, _btn(777, "push"), repo_root=tmp_path, target_root=str(tmp_path))
    assert len(cb_env.pushes) == 1
    assert len(cb_env.edited) == 1
    _cid, mid, text, _b = cb_env.edited[0]
    assert mid == 99
    assert text.startswith(bridge.HEADER_DONE)


def test_button_cancel_edits_message(cb_env, tmp_path):
    _fire(cb_env, _btn(777, "x"), repo_root=tmp_path, target_root=str(tmp_path))
    assert cb_env.pushes == []
    assert cb_env.edited[0][2] == "취소했습니다."


def test_button_push_no_message_id_send_fallback(cb_env, tmp_path):
    _fire(cb_env, _btn(777, "push", message_id=None), repo_root=tmp_path, target_root=str(tmp_path))
    assert len(cb_env.pushes) == 1
    assert cb_env.edited == []
    assert len(cb_env.sent) == 1


def test_button_unknown_action_acked_then_ignored(cb_env, tmp_path):
    # 어댑터가 미해석 callback_data 를 action="" 로 정규화 → 코어는 ack 후 무시(라우팅 없음).
    _fire(cb_env, _btn(777, ""), repo_root=tmp_path, target_root=str(tmp_path))
    assert cb_env.sent == [] and cb_env.edited == [] and cb_env.pushes == []
    assert cb_env.acked == [("cq1", None)]  # 스피너만 종료


# ===========================================================================
# ① 시각 알림 — load_schedules / due_* / notify_state (순수, tmp_path)
# ===========================================================================

_KST = bridge._KST
_WED_0910 = datetime(2026, 7, 15, 9, 10, tzinfo=_KST)
_WED_0900 = datetime(2026, 7, 15, 9, 0, tzinfo=_KST)
_WED_0931 = datetime(2026, 7, 15, 9, 31, tzinfo=_KST)


def _item(**over):
    base = {"id": "x", "days": ["wed"], "at": "09:00", "grace_min": 30, "label": "L", "note": "N"}
    base.update(over)
    return base


def test_load_schedules_missing_file_empty(tmp_path):
    assert load_schedules(tmp_path / "nope.json") == []


def test_load_schedules_corrupt_empty(tmp_path):
    p = tmp_path / "notify.json"
    p.write_text("{ not json", encoding="utf-8")
    assert load_schedules(p) == []


def test_load_schedules_reads_items(tmp_path):
    p = tmp_path / "notify.json"
    p.write_text('{"items": [{"id": "a"}, "bad", {"id": "b"}]}', encoding="utf-8")
    assert [it["id"] for it in load_schedules(p)] == ["a", "b"]


def test_load_schedules_non_list_items_empty(tmp_path):
    p = tmp_path / "notify.json"
    p.write_text('{"items": "oops"}', encoding="utf-8")
    assert load_schedules(p) == []


def test_due_notifications_in_window():
    assert due_notifications([_item()], _WED_0910, set()) == [_item()]


def test_due_notifications_at_window_start_inclusive():
    assert due_notifications([_item()], _WED_0900, set()) == [_item()]


def test_due_notifications_at_window_end_inclusive():
    end = datetime(2026, 7, 15, 9, 30, tzinfo=_KST)
    assert due_notifications([_item()], end, set()) == [_item()]
    assert due_notifications([_item()], _WED_0931, set()) == []


def test_due_notifications_wrong_weekday_skipped():
    assert due_notifications([_item(days=["mon"])], _WED_0910, set()) == []


def test_due_notifications_dedup_by_fired():
    assert due_notifications([_item()], _WED_0910, {("x", "2026-07-15")}) == []


def test_due_notifications_before_window_skipped():
    early = datetime(2026, 7, 15, 8, 59, tzinfo=_KST)
    assert due_notifications([_item()], early, set()) == []


def test_due_notifications_malformed_at_skipped():
    assert due_notifications([_item(at="oops")], _WED_0910, set()) == []
    assert due_notifications([_item(at="25:00")], _WED_0910, set()) == []


def test_due_notifications_missing_grace_defaults_30():
    it = {"id": "x", "days": ["wed"], "at": "09:00"}
    assert due_notifications([it], _WED_0910, set()) == [it]


def test_due_snoozes_past_refire_returned():
    past = datetime(2026, 7, 15, 9, 0, tzinfo=_KST).isoformat()
    assert due_snoozes({"x": past}, _WED_0910) == ["x"]


def test_due_snoozes_future_not_returned():
    future = datetime(2026, 7, 15, 10, 0, tzinfo=_KST).isoformat()
    assert due_snoozes({"x": future}, _WED_0910) == []


def test_due_snoozes_corrupt_iso_skipped():
    assert due_snoozes({"x": "not-a-date"}, _WED_0910) == []


def test_notify_state_roundtrip(tmp_path):
    p = tmp_path / "notify_state.json"
    fired = {("x", "2026-07-15"), ("y", "2026-07-15")}
    snooze = {"z": "2026-07-15T09:00:00+09:00"}
    save_notify_state(p, fired, snooze)
    got_fired, got_snooze = load_notify_state(p, "2026-07-15")
    assert got_fired == fired
    assert got_snooze == snooze


def test_notify_state_prunes_stale_date(tmp_path):
    p = tmp_path / "notify_state.json"
    save_notify_state(
        p,
        {("today", "2026-07-15"), ("old", "2026-07-14")},
        {"fresh": "2026-07-15T09:00:00+09:00", "stale": "2026-07-14T09:00:00+09:00"},
    )
    fired, snooze = load_notify_state(p, "2026-07-15")
    assert fired == {("today", "2026-07-15")}
    assert snooze == {"fresh": "2026-07-15T09:00:00+09:00"}


def test_notify_state_missing_file_empty(tmp_path):
    assert load_notify_state(tmp_path / "nope.json", "2026-07-15") == (set(), {})


def test_notify_state_snooze_across_midnight_preserved(tmp_path):
    p = tmp_path / "notify_state.json"
    save_notify_state(p, set(), {"a": "2026-07-16T00:25:00+09:00"})
    _fired, snooze = load_notify_state(p, "2026-07-15")
    assert snooze == {"a": "2026-07-16T00:25:00+09:00"}


def test_load_schedules_rejects_unsafe_id(tmp_path):
    p = tmp_path / "notify.json"
    p.write_text(
        '{"items": [{"id": "ok-1"}, {"id": "bad/id"}, {"id": ""}, {"id": 5}]}',
        encoding="utf-8",
    )
    assert [it["id"] for it in load_schedules(p)] == ["ok-1"]


def test_due_snoozes_tz_naive_iso_skipped():
    assert due_snoozes({"a": "2026-07-15T09:00:00"}, _WED_0910) == []


# ---------------------------------------------------------------------------
# dispatch_notifications / handle_event nb 분기 — 전역 격리 + FakeAdapter
# ---------------------------------------------------------------------------


def _freeze_now(monkeypatch, fixed):
    class FakeDatetime(datetime):
        @classmethod
        def now(cls, *_args, **_kwargs):
            return fixed

    monkeypatch.setattr(bridge, "datetime", FakeDatetime)


@pytest.fixture
def notify_env(monkeypatch):
    """알림 전역 격리 + save_notify_state 스파이. FakeAdapter(send/edit/ack 기록)를 yield."""
    bridge.notify_fired.clear()
    bridge.notify_snooze.clear()
    fa = FakeAdapter(secrets=[])
    monkeypatch.setattr(
        bridge, "save_notify_state", lambda _p, f, s: fa.saves.append((set(f), dict(s)))
    )
    yield fa
    bridge.notify_fired.clear()
    bridge.notify_snooze.clear()


def test_dispatch_fans_out_to_all_allowed_and_marks_fired(notify_env, monkeypatch):
    _freeze_now(monkeypatch, _WED_0910)
    bridge.dispatch_notifications(notify_env, frozenset({111, 222}), [_item(id="a")])
    # H-1: 알림은 notify(user_id) 로 발송(send 아님) — 인가목록 user_id 가 발송 타겟.
    assert {u for u, _t, _b in notify_env.notified} == {111, 222}
    assert notify_env.sent == []  # send 로는 발송하지 않는다(채널 오용 유실 방지)
    for _u, _t, buttons in notify_env.notified:
        assert buttons[0] == Button("✅ 확인시작", "nb:ok", "a")  # nb:ok:a 로 왕복
    assert ("a", "2026-07-15") in bridge.notify_fired
    assert len(notify_env.saves) == 1


def test_dispatch_snooze_refires_then_pops(notify_env, monkeypatch):
    _freeze_now(monkeypatch, _WED_0931)  # 창 밖 → due 아님, 스누즈만 발송
    bridge.notify_fired.add(("a", "2026-07-15"))
    bridge.notify_snooze["a"] = datetime(2026, 7, 15, 9, 20, tzinfo=_KST).isoformat()
    bridge.dispatch_notifications(notify_env, frozenset({111}), [_item(id="a")])
    assert len(notify_env.notified) == 1
    assert "a" not in bridge.notify_snooze


def test_dispatch_due_and_snooze_no_double_send(notify_env, monkeypatch):
    _freeze_now(monkeypatch, _WED_0910)
    bridge.notify_snooze["a"] = datetime(2026, 7, 15, 9, 0, tzinfo=_KST).isoformat()
    bridge.dispatch_notifications(notify_env, frozenset({111}), [_item(id="a")])
    assert len(notify_env.notified) == 1  # 병합 시 한 번만


def test_dispatch_prunes_stale_date(monkeypatch, notify_env):
    _freeze_now(monkeypatch, datetime(2026, 7, 15, 3, 0, tzinfo=_KST))
    bridge.notify_fired.add(("old", "2026-07-14"))
    bridge.dispatch_notifications(notify_env, frozenset({111}), [])
    assert ("old", "2026-07-14") not in bridge.notify_fired


def test_dispatch_no_targets_no_send(notify_env, monkeypatch):
    _freeze_now(monkeypatch, _WED_0931)
    bridge.dispatch_notifications(notify_env, frozenset({111}), [_item(id="a")])
    assert notify_env.notified == []
    assert notify_env.sent == []
    assert notify_env.saves == []


def _write_schedules(monkeypatch, tmp_path, items):
    p = tmp_path / "notify.json"
    p.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bridge, "SCHEDULES_FILE", p)


def test_button_nb_ok_edits_and_clears_snooze(notify_env, monkeypatch, tmp_path):
    _write_schedules(monkeypatch, tmp_path, [])
    bridge.notify_snooze["a"] = "2026-07-15T09:00:00+09:00"
    _fire(notify_env, _btn(777, "nb:ok", "a"), repo_root=tmp_path, target_root=str(tmp_path))
    assert notify_env.edited[0][2].startswith("✅")
    assert "a" not in bridge.notify_snooze
    assert len(notify_env.saves) == 1


def test_button_nb_ok_without_snooze_no_save(notify_env, monkeypatch, tmp_path):
    _write_schedules(monkeypatch, tmp_path, [])
    _fire(notify_env, _btn(777, "nb:ok", "a"), repo_root=tmp_path, target_root=str(tmp_path))
    assert notify_env.edited[0][2].startswith("✅")
    assert notify_env.saves == []


def test_button_nb_later_snoozes_and_saves(notify_env, monkeypatch, tmp_path):
    _freeze_now(monkeypatch, _WED_0910)  # 09:10 → +30분 = 09:40
    _fire(notify_env, _btn(777, "nb:later", "a"), repo_root=tmp_path, target_root=str(tmp_path))
    assert bridge.notify_snooze["a"].startswith("2026-07-15T09:40")
    assert len(notify_env.saves) == 1
    assert notify_env.edited[0][2].startswith("⏰")


def test_button_nb_disallowed_user_ignored(notify_env, tmp_path):
    _fire(notify_env, _btn(999, "nb:later", "a"), repo_root=tmp_path, target_root=str(tmp_path))
    assert notify_env.edited == []
    assert notify_env.saves == []
    assert bridge.notify_snooze == {}


def test_build_notify_check_prompt_contents():
    p = bridge.build_notify_check_prompt("코스피 개장", "야간선물→코스피 전환 확인")
    assert "코스피 개장" in p and "야간선물→코스피 전환 확인" in p
    assert "점검" in p and "제안" in p
    assert "수정·커밋은 하지 마라" in p


def test_button_nb_ok_runs_check_when_item_found(notify_env, monkeypatch, tmp_path):
    (tmp_path / "trading_info").mkdir()
    _write_schedules(
        monkeypatch,
        tmp_path,
        [{"id": "a", "project": "trading_info", "note": "개장 확인", "label": "코스피 개장"}],
    )
    runs = []

    def spy(_a, cid, _hdr, _exe, proj, task, _to, allowed_tools=None, **_k):
        runs.append((cid, proj, task, allowed_tools))

    monkeypatch.setattr(bridge, "run_claude_with_progress", spy)
    _fire(notify_env, _btn(777, "nb:ok", "a"), repo_root=tmp_path, target_root=str(tmp_path))
    assert len(runs) == 1
    cid, proj, task, allowed_tools = runs[0]
    assert cid == 777 and proj == str(tmp_path / "trading_info")
    assert "코스피 개장" in task and "개장 확인" in task
    assert allowed_tools == bridge.NOTIFY_CHECK_TOOLS
    assert "Read" in allowed_tools
    assert "Edit" not in allowed_tools and "Write" not in allowed_tools
    assert not any("commit" in t for t in allowed_tools)
    assert any("확인 실행 중" in t for _c, _m, t, _b in notify_env.edited)


def test_button_nb_ok_project_unresolved_errors(notify_env, monkeypatch, tmp_path):
    _write_schedules(
        monkeypatch, tmp_path, [{"id": "a", "project": "gone_proj", "note": "확인", "label": "L"}]
    )
    runs = []
    monkeypatch.setattr(bridge, "run_claude_with_progress", lambda *_a, **_k: runs.append(1))
    _fire(notify_env, _btn(777, "nb:ok", "a"), repo_root=tmp_path, target_root=str(tmp_path))
    assert runs == []
    assert any("찾지 못해" in t for _c, _m, t, _b in notify_env.edited)


def test_button_nb_ok_no_item_falls_back(notify_env, monkeypatch, tmp_path):
    _write_schedules(monkeypatch, tmp_path, [{"id": "other", "project": "x", "note": "n"}])
    runs = []
    monkeypatch.setattr(bridge, "run_claude_with_progress", lambda *_a, **_k: runs.append(1))
    _fire(notify_env, _btn(777, "nb:ok", "a"), repo_root=tmp_path, target_root=str(tmp_path))
    assert runs == []
    assert any("확인을 시작합니다" in t for _c, _m, t, _b in notify_env.edited)


# ===========================================================================
# ② 사진 대조 — 순수 함수 + download_file/fetch_stock + handle_event 사진 분기
# ===========================================================================


def test_extract_photo_picks_max_resolution():
    upd = {
        "message": {
            "photo": [
                {"file_id": "s", "width": 90, "height": 90},
                {"file_id": "m", "width": 320, "height": 240},
                {"file_id": "l", "width": 1280, "height": 960},
            ]
        }
    }
    assert extract_photo(upd) == "l"


def test_extract_photo_unordered_still_max():
    upd = {
        "message": {
            "photo": [
                {"file_id": "big", "width": 1000, "height": 1000},
                {"file_id": "small", "width": 10, "height": 10},
            ]
        }
    }
    assert extract_photo(upd) == "big"


def test_extract_photo_no_photo_key_none():
    assert extract_photo({"message": {"text": "hi"}}) is None
    assert extract_photo({"message": {}}) is None
    assert extract_photo({}) is None


def test_extract_photo_empty_list_none():
    assert extract_photo({"message": {"photo": []}}) is None


def test_extract_photo_ignores_non_dict_sizes():
    upd = {"message": {"photo": ["oops", {"file_id": "ok", "width": 5, "height": 5}]}}
    assert extract_photo(upd) == "ok"


def test_valid_ticker_accepts_common():
    for t in ("MU", "AAPL", "NQ=F", "^KS11", "005930", "0167A0", "BRK-B"):
        assert valid_ticker(t)


def test_valid_ticker_rejects_ssrf_and_traversal():
    for bad in ("../etc", "A/B", "A\\B", "a b", "MU:8000", "..", "mu", "x" * 16, ""):
        assert not valid_ticker(bad)


def test_parse_caption_ticker_extracts_first_valid():
    assert parse_caption_ticker("trading_info MU 대조") == "MU"
    assert parse_caption_ticker("mu") == "MU"
    assert parse_caption_ticker("NQ=F 확인해줘") == "NQ=F"


def test_parse_caption_ticker_none_when_no_ticker():
    assert parse_caption_ticker("사진 좀 봐줘") is None
    assert parse_caption_ticker("") is None


def test_stock_url_fixed_host_path():
    assert stock_url("MU") == "http://127.0.0.1:8000/api/stocks/MU"


def test_stock_url_rejects_invalid_ticker():
    with pytest.raises(ValueError, match="ticker"):
        stock_url("../secret")
    with pytest.raises(ValueError):
        stock_url("A/B")


def test_parse_stock_response_full():
    payload = {
        "change_percent": -3.1,
        "change_amount": -4.2,
        "session": "정규장",
        "is_trading_day": True,
        "current_price": 131.0,
        "name": "마이크론",
    }
    out = parse_stock_response(payload)
    assert out["change_percent"] == -3.1
    assert out["session"] == "정규장"
    assert out["name"] == "마이크론"


def test_parse_stock_response_missing_fields_none():
    out = parse_stock_response({"session": "프리마켓"})
    assert out["change_percent"] is None
    assert out["change_amount"] is None
    assert out["session"] == "프리마켓"


def test_build_compare_prompt_contains_values_and_no_commit():
    prompt = build_compare_prompt(Path("logs/photos/x.jpg"), "MU", {"change_percent": -3.1})
    assert "MU" in prompt
    assert "-3.1" in prompt
    assert "x.jpg" in prompt
    assert "커밋은 하지" in prompt


# --- download_file / fetch_stock: urllib monkeypatch (telegram_adapter) ---


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
    # fetch_stock=urlopen, download_file=리다이렉트 차단 opener(_NOREDIRECT_OPENER.open).
    # 둘 다 같은 가짜 resp 로 패치해 기존 검증 의미를 보존(M-3 opener 도입 후).
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: resp)
    monkeypatch.setattr(telegram_adapter._NOREDIRECT_OPENER, "open", lambda *_a, **_k: resp)


def test_download_file_writes_basename_only(monkeypatch, tmp_path):
    _patch_urlopen(monkeypatch, _FakeResp(b"\xff\xd8jpegdata"))
    dest = download_file("TOKEN", "photos/file_99.jpg", tmp_path)
    assert dest.name == "file_99.jpg"
    assert dest.parent == tmp_path
    assert dest.read_bytes() == b"\xff\xd8jpegdata"


def test_download_file_traversal_path_stays_in_dest(monkeypatch, tmp_path):
    _patch_urlopen(monkeypatch, _FakeResp(b"x"))
    dest = download_file("T", "a/../../evil.png", tmp_path)
    assert dest.name == "evil.png"
    assert dest.parent == tmp_path


def test_download_file_rejects_bad_extension(monkeypatch, tmp_path):
    _patch_urlopen(monkeypatch, _FakeResp(b"x"))
    with pytest.raises(ValueError, match="확장자"):
        download_file("T", "photos/x.gif", tmp_path)


def test_download_file_rejects_oversize_body(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_adapter, "MAX_PHOTO_BYTES", 4)
    _patch_urlopen(monkeypatch, _FakeResp(b"toolongbody"))
    with pytest.raises(ValueError, match=r"10MB|상한"):
        download_file("T", "photos/x.jpg", tmp_path)


def test_download_file_rejects_oversize_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_adapter, "MAX_PHOTO_BYTES", 4)
    _patch_urlopen(monkeypatch, _FakeResp(b"ok", headers={"Content-Length": "999"}))
    with pytest.raises(ValueError, match=r"10MB|상한"):
        download_file("T", "photos/x.jpg", tmp_path)


def test_noredirect_handler_blocks_3xx():
    # M-3: redirect_request→None → urllib 이 3xx 를 HTTPError 로 승격(추종 안 함).
    h = telegram_adapter._NoRedirectHandler()
    internal = "http://169.254.169.254/latest/"
    assert h.redirect_request(None, None, 302, "Found", {}, internal) is None


def _raise_302(*_a, **_k):
    raise urllib.error.HTTPError(
        "https://api.telegram.org/file/botT/photos/x.jpg",
        302,
        "redirect blocked",
        {},  # type: ignore[arg-type]
        None,
    )


def test_download_file_rejects_redirect(monkeypatch, tmp_path):
    # M-3: 화이트리스트 호스트가 내부주소로 3xx → opener 가 추종 대신 HTTPError → 다운로드 거부.
    monkeypatch.setattr(telegram_adapter._NOREDIRECT_OPENER, "open", _raise_302)
    with pytest.raises(urllib.error.HTTPError):
        download_file("T", "photos/x.jpg", tmp_path)


def test_download_file_uses_noredirect_opener_not_urlopen(monkeypatch, tmp_path):
    # M-3: download_file 은 리다이렉트 차단 opener 를 쓴다 — 소박한 urlopen(추종)이면 실패해야.
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("urlopen 직접 호출 금지")),
    )
    monkeypatch.setattr(
        telegram_adapter._NOREDIRECT_OPENER, "open", lambda *_a, **_k: _FakeResp(b"\xff\xd8ok")
    )
    dest = download_file("T", "photos/x.jpg", tmp_path)
    assert dest.read_bytes() == b"\xff\xd8ok"


def test_fetch_stock_parses_response(monkeypatch):
    body = b'{"change_percent": -3.1, "session": "\\uc815\\uaddc\\uc7a5", "name": "MU"}'
    _patch_urlopen(monkeypatch, _FakeResp(body))
    out = fetch_stock("MU")
    assert out["change_percent"] == -3.1
    assert out["session"] == "정규장"


def test_fetch_stock_rejects_invalid_ticker_before_network(monkeypatch):
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("네트워크 호출되면 안 됨")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(ValueError):
        fetch_stock("../etc/passwd")
    assert called["n"] == 0


# --- handle_event 사진 분기 오케스트레이션 (FakeAdapter.fetch_file + fetch_stock 스파이) ---


@pytest.fixture
def photo_env(monkeypatch):
    """run_claude_with_progress / fetch_stock 스파이 + FakeAdapter(fetch_file 기록)."""
    fa = FakeAdapter(secrets=[])

    def fake_run(*args, **_k):
        # (adapter, channel_id, header, exe, proj, task, timeout, allowed_tools?)
        fa.runs.append({"allowed_tools": args[7] if len(args) > 7 else None})
        return {"result": "✅ 일치", "is_error": False}

    monkeypatch.setattr(bridge, "run_claude_with_progress", fake_run)
    monkeypatch.setattr(
        bridge, "fetch_stock", lambda _t: {"change_percent": -3.1, "session": "정규장"}
    )
    return fa


def test_photo_no_caption_prompts_no_run(photo_env, tmp_path):
    (tmp_path / "trading_info").mkdir()
    _fire(photo_env, _photo(777, caption=None), repo_root=tmp_path, target_root=str(tmp_path))
    assert photo_env.runs == []
    assert any("캡션" in t for _c, t, _b in photo_env.sent)


def test_photo_no_photo_ref_prompts_no_run(photo_env, tmp_path):
    (tmp_path / "trading_info").mkdir()
    _fire(
        photo_env,
        _photo(777, caption="MU", photo_ref=None),
        repo_root=tmp_path,
        target_root=str(tmp_path),
    )
    assert photo_env.runs == []
    assert any("사진을 읽지" in t for _c, t, _b in photo_env.sent)


def test_photo_download_fail_graceful(monkeypatch, tmp_path):
    (tmp_path / "trading_info").mkdir()
    fa = FakeAdapter(secrets=[], fetch=OSError("net down"))
    monkeypatch.setattr(bridge, "run_claude_with_progress", lambda *_a, **_k: fa.runs.append(1))
    monkeypatch.setattr(bridge, "fetch_stock", lambda _t: {})
    _fire(fa, _photo(777, caption="MU"), repo_root=tmp_path, target_root=str(tmp_path))
    assert fa.runs == []
    assert any("내려받지" in t for _c, t, _b in fa.sent)


def test_photo_rest_fail_graceful(photo_env, monkeypatch, tmp_path):
    (tmp_path / "trading_info").mkdir()

    def boom(_t):
        raise OSError("conn refused")

    monkeypatch.setattr(bridge, "fetch_stock", boom)
    _fire(photo_env, _photo(777, caption="MU"), repo_root=tmp_path, target_root=str(tmp_path))
    assert photo_env.runs == []
    assert any("REST 응답 없음" in t for _c, t, _b in photo_env.sent)


def test_photo_normal_runs_with_read_only_tools(photo_env, tmp_path):
    # M-1 회귀 잠금: 사진 대조 run 은 반드시 Read 전용 도구셋으로 호출.
    (tmp_path / "trading_info").mkdir()
    _fire(
        photo_env,
        _photo(777, caption="trading_info MU 대조"),
        repo_root=tmp_path,
        target_root=str(tmp_path),
    )
    assert len(photo_env.runs) == 1
    assert photo_env.runs[0]["allowed_tools"] == ["Read"]
    assert photo_env.fetched  # 사진 다운로드 시도됨


def test_photo_disallowed_user_never_downloads(tmp_path):
    # 보안 회귀 잠금: 미허용 user 는 게이트에서 차단 → fetch_file 미도달.
    fa = FakeAdapter(secrets=[])
    _fire(
        fa,
        _photo(999, caption="MU"),
        allowed=frozenset({777}),
        repo_root=tmp_path,
        target_root=str(tmp_path),
    )
    assert fa.fetched == []
    assert fa.sent == []


def test_photo_allowed_user_triggers_handler(photo_env, tmp_path):
    # 허용 user 사진(캡션 없음) → 핸들러 진입해 "캡션" 안내.
    _fire(
        photo_env,
        _photo(777, caption=None),
        allowed=frozenset({777}),
        repo_root=tmp_path,
        target_root=str(tmp_path),
    )
    assert any("캡션" in t for _c, t, _b in photo_env.sent)


# ===========================================================================
# ③ 버튼 선택지 — parse_choice_prompt(순수) + handle_event c 분기 · await_reply
# ===========================================================================


def test_parse_choice_prompt_normal():
    out = parse_choice_prompt("옵션을 고르세요.\n❓선택: [유지|keep]|[교체|swap]")
    assert out == ("옵션을 고르세요.", [("유지", "keep"), ("교체", "swap")])


def test_parse_choice_prompt_inline_question_default():
    out = parse_choice_prompt("❓선택: [예|yes]|[아니오|no]")
    assert out == ("선택하세요", [("예", "yes"), ("아니오", "no")])


def test_parse_choice_prompt_colon_newline():
    out = parse_choice_prompt("무엇을 할까요?\n❓선택:\n[유지|keep]|[교체|swap]")
    assert out == ("무엇을 할까요?", [("유지", "keep"), ("교체", "swap")])


def test_parse_choice_prompt_multiline_choices():
    out = parse_choice_prompt("❓선택:\n[예|yes]\n[아니오|no]")
    assert out == ("선택하세요", [("예", "yes"), ("아니오", "no")])


def test_parse_choice_prompt_non_choice_none():
    assert parse_choice_prompt("작업을 완료했습니다.") is None
    assert parse_choice_prompt("") is None


def test_parse_choice_prompt_broken_grammar_none():
    assert parse_choice_prompt("❓선택: [값없음]") is None
    assert parse_choice_prompt("❓선택: []|[|]") is None
    assert parse_choice_prompt("❓선택: 아무거나") is None


def test_parse_choice_prompt_skips_malformed_keeps_valid():
    out = parse_choice_prompt("❓선택: [좋음|a]|[깨짐]|[나쁨|b]")
    assert out == ("선택하세요", [("좋음", "a"), ("나쁨", "b")])


def test_parse_choice_prompt_uses_last_marker():
    text = "설명 ❓선택: [무시|x]\n최종 질문\n❓선택: [진짜A|a]|[진짜B|b]"
    out = parse_choice_prompt(text)
    assert out is not None
    assert out[1] == [("진짜A", "a"), ("진짜B", "b")]


# --- handle_event c 분기 · await_reply 라우팅 (resume_run 스파이) ---


@pytest.fixture
def choice_env(monkeypatch):
    """pending 격리 + resume_run 스파이. FakeAdapter(ack/send/edit 기록)를 yield."""
    bridge.pending.clear()
    fa = FakeAdapter(secrets=[])
    fa.resumes = []

    def fake_resume(_a, _cid, _exe, proj, answer, question, sid, _to, user_id=None):
        fa.resumes.append(
            {"proj": proj, "answer": answer, "sid": sid, "question": question, "user_id": user_id}
        )

    monkeypatch.setattr(bridge, "resume_run", fake_resume)
    yield fa
    bridge.pending.clear()


def _pending_entry(await_reply=False, chat_id=777, user_id=None):
    return {
        "chat_id": chat_id,
        "user_id": user_id if user_id is not None else chat_id,  # M-1 소유 키(기본=chat_id)
        "session_id": "sid1",
        "project_path": "/proj",
        "choices": [("유지", "keep"), ("교체", "swap")],
        "question": "무엇을?",
        "await_reply": await_reply,
    }


def test_choice_selection_resumes(choice_env):
    bridge.pending[50] = _pending_entry()
    _fire(choice_env, _btn(777, "c", "50:1"), target_root="root")
    assert len(choice_env.resumes) == 1
    r = choice_env.resumes[0]
    assert r["answer"] == "swap" and r["sid"] == "sid1" and r["proj"] == "/proj"
    assert 50 not in bridge.pending
    assert any("교체" in t for _c, _m, t, _b in choice_env.edited)


def test_choice_other_sets_await(choice_env):
    bridge.pending[50] = _pending_entry()
    _fire(choice_env, _btn(777, "c", "50:other"), target_root="root")
    assert bridge.pending[50]["await_reply"] is True
    assert choice_env.resumes == []
    assert any("답장으로" in t for _c, t, _b in choice_env.sent)


def test_choice_expired_pending(choice_env):
    _fire(choice_env, _btn(777, "c", "99:0"), target_root="root")
    assert choice_env.resumes == []
    assert any("만료" in t for _c, _m, t, _b in choice_env.edited)


def test_choice_out_of_range_ignored(choice_env):
    bridge.pending[50] = _pending_entry()  # 선택지 2개(0,1)
    _fire(choice_env, _btn(777, "c", "50:5"), target_root="root")
    assert choice_env.resumes == []
    assert 50 in bridge.pending


def test_choice_disallowed_user_blocked(choice_env):
    bridge.pending[50] = _pending_entry()
    _fire(choice_env, _btn(999, "c", "50:0"), target_root="root")
    assert choice_env.resumes == []
    assert choice_env.acked == []  # 허용목록 게이트에서 즉시 차단(ack 도 안 함)
    assert bridge.pending[50]["await_reply"] is False


def test_await_reply_routes_text_to_resume(choice_env):
    bridge.pending[50] = _pending_entry(await_reply=True)
    _fire(choice_env, _txt(777, "직접 입력한 답"), target_root="root")
    assert len(choice_env.resumes) == 1
    assert choice_env.resumes[0]["answer"] == "직접 입력한 답"
    assert 50 not in bridge.pending


def test_await_reply_cancel_clears(choice_env):
    bridge.pending[50] = _pending_entry(await_reply=True)
    _fire(choice_env, _txt(777, "/cancel"), target_root="root")
    assert 50 not in bridge.pending
    assert choice_env.resumes == []
    assert any("취소" in t for _c, t, _b in choice_env.sent)


def test_await_reply_slash_command_falls_through(choice_env, tmp_path):
    (tmp_path / "etf_info").mkdir()
    bridge.pending[50] = _pending_entry(await_reply=True)
    _fire(choice_env, _txt(777, "/projects"), target_root=str(tmp_path))
    assert choice_env.resumes == []
    assert any("대상 프로젝트" in t for _c, t, _b in choice_env.sent)
    assert 50 in bridge.pending


def test_await_reply_non_slash_still_routes_to_resume(choice_env):
    bridge.pending[50] = _pending_entry(await_reply=True)
    _fire(choice_env, _txt(777, "push"), target_root="root")
    assert len(choice_env.resumes) == 1
    assert choice_env.resumes[0]["answer"] == "push"
    assert 50 not in bridge.pending


def test_choice_other_chat_rejected(choice_env):
    bridge.pending[50] = _pending_entry(chat_id=777)
    _fire(choice_env, _btn(888, "c", "50:1"), allowed=_ALLOWED2, target_root="root")
    assert choice_env.resumes == []
    assert 50 in bridge.pending


def test_await_reply_other_chat_not_routed(choice_env):
    bridge.pending[50] = _pending_entry(await_reply=True, chat_id=777)
    _fire(choice_env, _txt(888, "가로채기 시도"), allowed=_ALLOWED2, target_root="root")
    assert choice_env.resumes == []
    assert 50 in bridge.pending


def test_cancel_other_chat_keeps_await(choice_env):
    bridge.pending[50] = _pending_entry(await_reply=True, chat_id=777)
    _fire(choice_env, _txt(888, "/cancel"), allowed=_ALLOWED2, target_root="root")
    assert 50 in bridge.pending


# --- M-1: 같은 채널·다른 user 격리(공유 채널 다중 유저 세션탈취 차단) ---


def test_choice_same_channel_other_user_rejected(choice_env):
    # 같은 채널(100)이라도 소유자(777)가 아닌 user(888)는 선택을 소비 못 한다.
    bridge.pending[50] = _pending_entry(chat_id=100, user_id=777)
    _fire(
        choice_env,
        _btn(888, "c", "50:1", channel_id=100),
        allowed=_ALLOWED2,
        target_root="root",
    )
    assert choice_env.resumes == []
    assert 50 in bridge.pending  # 미소비
    assert any("만료" in t for _c, _m, t, _b in choice_env.edited)


def test_choice_same_channel_owner_consumes(choice_env):
    # 소유자(777) 본인은 같은 채널(100)에서 정상 소비.
    bridge.pending[50] = _pending_entry(chat_id=100, user_id=777)
    _fire(
        choice_env,
        _btn(777, "c", "50:1", channel_id=100),
        allowed=_ALLOWED2,
        target_root="root",
    )
    assert len(choice_env.resumes) == 1
    assert choice_env.resumes[0]["user_id"] == 777  # 소유자로 재실행
    assert 50 not in bridge.pending


def test_await_reply_same_channel_other_user_not_routed(choice_env):
    bridge.pending[50] = _pending_entry(await_reply=True, chat_id=100, user_id=777)
    _fire(
        choice_env,
        _txt(888, "가로채기 시도", channel_id=100),
        allowed=_ALLOWED2,
        target_root="root",
    )
    assert choice_env.resumes == []
    assert 50 in bridge.pending  # 남의 대기 안 건드림


def test_cancel_same_channel_other_user_keeps_await(choice_env):
    bridge.pending[50] = _pending_entry(await_reply=True, chat_id=100, user_id=777)
    _fire(
        choice_env,
        _txt(888, "/cancel", channel_id=100),
        allowed=_ALLOWED2,
        target_root="root",
    )
    assert 50 in bridge.pending  # 888 의 /cancel 은 777 의 대기를 해제 못 함


# --- 핵심 배선 회귀 잠금: _render_choices / resume_run / run_claude_with_progress ---


def test_render_choices_registers_pending_and_keyboard():
    bridge.pending.clear()
    fa = FakeAdapter(secrets=[], send_ids=[200])
    bridge._render_choices(fa, 100, "/proj", "sid-abc", ("Q", [("유지", "keep")]), 777)
    assert 200 in bridge.pending
    e = bridge.pending[200]
    assert e["chat_id"] == 100 and e["session_id"] == "sid-abc" and e["project_path"] == "/proj"
    assert e["user_id"] == 777  # M-1: 선택지 소유자 저장(공유 채널 세션탈취 차단)
    # 얻은 message_id(200)로 키보드 부착(edit 에 buttons).
    assert fa.edited and fa.edited[0][1] == 200
    assert fa.edited[0][3] == choice_buttons(200, [("유지", "keep")])
    bridge.pending.clear()


def test_render_choices_skips_without_session_id():
    bridge.pending.clear()
    fa = FakeAdapter(secrets=[], send_ids=[200, 200])
    bridge._render_choices(fa, 777, "/proj", None, ("Q", [("a", "1")]))
    assert bridge.pending == {}
    bridge._render_choices(fa, 777, "/proj", 123, ("Q", [("a", "1")]))
    assert bridge.pending == {}
    bridge.pending.clear()


def test_render_choices_masks_label():
    # L-2: 라벨은 마스킹 안 된 result 재파싱분 → 버튼 text·저장분 모두 마스킹돼야.
    bridge.pending.clear()
    fa = FakeAdapter(secrets=["SECRET"], send_ids=[200])
    bridge._render_choices(fa, 777, "/p", "sid-1", ("Q", [("토큰SECRET표시", "v")]))
    label = fa.edited[0][3][0].label
    assert "SECRET" not in label and "***" in label
    assert bridge.pending[200]["choices"][0][0] == label  # 저장분도 마스킹
    bridge.pending.clear()


def test_resume_run_fallback_on_resume_error(monkeypatch):
    calls = []

    def stub(_a, _cid, _hdr, _exe, _proj, task, _to, _allow=None, resume=None, user_id=None):
        calls.append({"task": task, "resume": resume, "user_id": user_id})
        return {"is_error": len(calls) == 1, "result": ""}  # 첫(resume) 실패, 폴백 성공

    monkeypatch.setattr(bridge, "run_claude_with_progress", stub)
    bridge.resume_run(
        FakeAdapter(), 777, "claude", "/p", "내 답", "원 질문", "sid-1", 60, user_id=777
    )
    assert len(calls) == 2
    assert calls[0]["resume"] == "sid-1"
    assert calls[1]["resume"] is None
    assert calls[0]["user_id"] == 777 and calls[1]["user_id"] == 777  # M-1: 폴백에도 소유자 전파
    assert "원 질문" in calls[1]["task"] and "내 답" in calls[1]["task"]


def test_rcwp_read_only_skips_choice_render(monkeypatch):
    bridge.pending.clear()
    monkeypatch.setattr(
        bridge,
        "run_claude",
        lambda *_a, **_k: {
            "result": "Q\n❓선택: [a|1]|[b|2]",
            "is_error": False,
            "session_id": "s",
        },
    )
    fa = FakeAdapter(secrets=[], send_ids=[10])
    bridge.run_claude_with_progress(fa, 777, "H", "c", "/p", "task", 60, ["Read"])
    assert bridge.pending == {}
    bridge.pending.clear()


def test_rcwp_full_path_renders_and_hides_marker(monkeypatch):
    bridge.pending.clear()
    monkeypatch.setattr(
        bridge,
        "run_claude",
        lambda *_a, **_k: {
            "result": "고르세요\n❓선택: [유지|keep]|[교체|swap]",
            "is_error": False,
            "session_id": "sid-1",
        },
    )
    fa = FakeAdapter(secrets=[], send_ids=[10, 11])
    bridge.run_claude_with_progress(fa, 777, "H", "c", "/p", "task", 60)
    assert fa.edited and all("❓선택" not in t for _c, _m, t, _b in fa.edited)
    assert 11 in bridge.pending  # 버튼 메시지(두 번째 id)에 보류맵 등록
    assert bridge.pending[11]["chat_id"] == 777
    bridge.pending.clear()


def test_rcwp_choice_sets_choice_rendered_flag(monkeypatch):
    bridge.pending.clear()
    monkeypatch.setattr(
        bridge,
        "run_claude",
        lambda *_a, **_k: {
            "result": "고르세요\n❓선택: [유지|keep]|[교체|swap]",
            "is_error": False,
            "session_id": "sid-1",
        },
    )
    fa = FakeAdapter(secrets=[], send_ids=[10, 11])
    data = bridge.run_claude_with_progress(fa, 777, "H", "c", "/p", "task", 60)
    assert data.get("choice_rendered") is True
    bridge.pending.clear()


def test_rcwp_no_choice_no_flag(monkeypatch):
    bridge.pending.clear()
    monkeypatch.setattr(
        bridge,
        "run_claude",
        lambda *_a, **_k: {"result": "끝", "is_error": False, "session_id": "s"},
    )
    fa = FakeAdapter(secrets=[], send_ids=[10])
    data = bridge.run_claude_with_progress(fa, 777, "H", "c", "/p", "task", 60)
    assert not data.get("choice_rendered")
    bridge.pending.clear()


def test_rcwp_timeout_stale_progress_does_not_overwrite_final(monkeypatch):
    # 회귀 잠금(Medium): 타임아웃 킬 후에도 리더 스레드가 잠깐 살아 on_event 를 더 밀 수 있다.
    # finished 가드가 없으면 그 스테일 진행 edit 가 최종 결과 edit 뒤에 도착해 덮어쓴다.
    # throttle 을 0 으로 낮춰(실제론 킬~join 지연이 2.5s 를 넘김) 스테일 이벤트가 실제로 edit 를
    # 시도하게 만든다 — 가드가 없으면 이 테스트가 실패해야 한다(회귀 실효성 보장).
    bridge.pending.clear()
    monkeypatch.setattr(bridge, "PROGRESS_THROTTLE_SEC", 0)
    captured = {}

    def fake_run(_exe, _path, _task, _to, on_event, *_a, **_k):
        on_event(_assistant({"type": "text", "text": "진행 중 첫 줄"}))  # 정상 진행 edit 1회
        captured["on_event"] = on_event  # 완료 후 잔존 리더가 밀 이벤트를 재현하려 참조 보관
        return {"is_error": True, "result": "타임아웃(60s) 초과 — 작업을 중단했습니다."}

    monkeypatch.setattr(bridge, "run_claude", fake_run)
    fa = FakeAdapter(secrets=[], send_ids=[10])
    bridge.run_claude_with_progress(fa, 777, "H", "c", "/p", "task", 60)
    final_text = fa.edited[-1][2]
    assert "타임아웃" in final_text  # 반환 직후 최종 상태 = 타임아웃 결과
    # 스테일 리더가 완료 후 진행 이벤트를 더 밀어도 finished 가드로 무시(throttle=0 라도).
    captured["on_event"](_assistant({"type": "text", "text": "스테일 진행 줄"}))
    assert fa.edited[-1][2] == final_text  # 새 edit 미발생(최종 결과 보존)
    assert all("스테일 진행 줄" not in txt for _c, _m, txt, _b in fa.edited)
    bridge.pending.clear()


def test_handle_text_skips_git_note_when_choice_rendered(monkeypatch, tmp_path):
    (tmp_path / "etf_info").mkdir()
    bridge.chat_selection.clear()
    monkeypatch.setattr(
        bridge,
        "run_claude_with_progress",
        lambda *_a, **_k: {"is_error": False, "result": "ok", "choice_rendered": True},
    )
    note_calls = []
    monkeypatch.setattr(bridge, "git_status_note", lambda _r: note_calls.append(1) or "변경 없음.")
    monkeypatch.setattr(bridge, "git_ahead", lambda _r: 0)
    fa = FakeAdapter(secrets=[])
    _fire(fa, _txt(777, "etf_info 뭐 골라줘"), repo_root=tmp_path, target_root=str(tmp_path))
    assert note_calls == []
    assert all(bridge.HEADER_NOTE not in t for _c, t, _b in fa.sent)
    bridge.chat_selection.clear()


def _git_note_env(monkeypatch, tmp_path, ahead):
    (tmp_path / "etf_info").mkdir()
    bridge.chat_selection.clear()
    monkeypatch.setattr(
        bridge, "run_claude_with_progress", lambda *_a, **_k: {"is_error": False, "result": "ok"}
    )
    monkeypatch.setattr(bridge, "git_ahead", lambda _r: ahead)
    monkeypatch.setattr(bridge, "git_status_note", lambda _r: f"로컬 커밋 {ahead}개 대기 — ...")
    fa = FakeAdapter(secrets=[])
    _fire(fa, _txt(777, "etf_info 로그 봐줘"), repo_root=tmp_path, target_root=str(tmp_path))
    bridge.chat_selection.clear()
    return [t for _c, t, _b in fa.sent]


def test_handle_text_unsupported_message_prompts_text_only():
    # 어댑터가 비지원 메시지(스티커 등)를 text="" 로 정규화 → 코어가 "텍스트만 처리" 안내.
    fa = FakeAdapter()
    _fire(fa, _txt(777, ""), target_root="root")
    assert any("텍스트 메시지만" in t for _c, t, _b in fa.sent)


def test_handle_text_skips_note_when_no_ahead(monkeypatch, tmp_path):
    sent = _git_note_env(monkeypatch, tmp_path, ahead=0)
    assert all(bridge.HEADER_NOTE not in t for t in sent)


def test_handle_text_sends_note_when_ahead(monkeypatch, tmp_path):
    sent = _git_note_env(monkeypatch, tmp_path, ahead=2)
    assert any(bridge.HEADER_NOTE in t for t in sent)


# ===========================================================================
# ④ chat 프로젝트 선택 고정 — 버튼 탭 → 이름 생략 실행 · 명시 우선 · chat 격리
# ===========================================================================


@pytest.fixture
def sel_env(monkeypatch):
    """chat_selection 격리 + run_claude_with_progress·git 스파이. FakeAdapter 를 yield."""
    bridge.chat_selection.clear()
    fa = FakeAdapter(secrets=[])

    def fake_run(_a, cid, _hdr, _exe, proj_path, task, _to, *_args, **_kw):
        fa.runs.append((cid, proj_path, task))
        return {"is_error": False, "result": "ok"}

    monkeypatch.setattr(bridge, "run_claude_with_progress", fake_run)
    monkeypatch.setattr(bridge, "git_status_note", lambda _r: "변경 없음.")
    monkeypatch.setattr(bridge, "git_ahead", lambda _r: 0)
    yield fa
    bridge.chat_selection.clear()


def test_button_select_then_bare_task_uses_selection(sel_env, tmp_path):
    (tmp_path / "trading_info").mkdir()
    root = str(tmp_path)
    _fire(sel_env, _btn(777, "p", "trading_info"), repo_root=tmp_path, target_root=root)
    assert bridge.chat_selection[777] == "trading_info"
    _fire(sel_env, _txt(777, "시간대 별로 체크 각 몇시?"), repo_root=tmp_path, target_root=root)
    assert sel_env.runs == [(777, str(tmp_path / "trading_info"), "시간대 별로 체크 각 몇시?")]


def test_explicit_message_updates_selection(sel_env, tmp_path):
    (tmp_path / "trading_info").mkdir()
    (tmp_path / "etf_info").mkdir()
    root = str(tmp_path)
    _fire(sel_env, _txt(777, "trading_info 헤더 고쳐"), repo_root=tmp_path, target_root=root)
    assert bridge.chat_selection[777] == "trading_info"
    _fire(sel_env, _txt(777, "etf_info 로그 봐줘"), repo_root=tmp_path, target_root=root)
    assert bridge.chat_selection[777] == "etf_info"
    _fire(sel_env, _txt(777, "이번엔 이거 해줘"), repo_root=tmp_path, target_root=root)
    assert sel_env.runs[-1][:2] == (777, str(tmp_path / "etf_info"))
    assert sel_env.runs[-1][2] == "이번엔 이거 해줘"


def test_no_selection_no_project_errors(sel_env, tmp_path):
    (tmp_path / "trading_info").mkdir()
    _fire(sel_env, _txt(777, "시간대 별로 체크"), repo_root=tmp_path, target_root=str(tmp_path))
    assert sel_env.runs == []
    assert any("찾지 못했" in t for _c, t, _b in sel_env.sent)
    assert 777 not in bridge.chat_selection


def test_selection_isolated_per_chat(sel_env, tmp_path):
    (tmp_path / "trading_info").mkdir()
    root = str(tmp_path)
    allowed = frozenset({777, 888})
    _fire(
        sel_env,
        _btn(777, "p", "trading_info"),
        allowed=allowed,
        repo_root=tmp_path,
        target_root=root,
    )
    assert bridge.chat_selection == {777: "trading_info"}
    _fire(sel_env, _txt(888, "시간대 별로"), allowed=allowed, repo_root=tmp_path, target_root=root)
    assert sel_env.runs == []
    assert 888 not in bridge.chat_selection


def test_event_project_used_as_channel_selection(sel_env, tmp_path):
    # 계약 §1.4: 디스코드 채널명(event.project)이 실존 프로젝트면 접두 없는 지시도 그 프로젝트로
    # 실행한다("채널=프로젝트" UX). chat_selection 없이 event.project 만으로 라우팅되는지 잠금.
    (tmp_path / "etf_info").mkdir()
    root = str(tmp_path)
    ev = Event(kind="text", channel_id=555, user_id=777, text="로그 봐줘", project="etf_info")
    _fire(sel_env, ev, repo_root=tmp_path, target_root=root)
    assert sel_env.runs == [(555, str(tmp_path / "etf_info"), "로그 봐줘")]


def test_event_project_nonexistent_falls_through(sel_env, tmp_path):
    # 채널명이 실존 프로젝트가 아니면(일반 채널) 기존 "못 찾음" 경로와 100% 동일 — 새 규칙 없음.
    (tmp_path / "etf_info").mkdir()
    ev = Event(kind="text", channel_id=555, user_id=777, text="로그 봐줘", project="없는채널")
    _fire(sel_env, ev, repo_root=tmp_path, target_root=str(tmp_path))
    assert sel_env.runs == []
    assert any("찾지 못했" in t for _c, t, _b in sel_env.sent)


def test_telegram_project_none_unaffected(sel_env, tmp_path):
    # 텔레그램은 project=None → event.project 분기 무영향, 기존 chat_selection 경로 그대로.
    (tmp_path / "trading_info").mkdir()
    root = str(tmp_path)
    _fire(sel_env, _btn(777, "p", "trading_info"), repo_root=tmp_path, target_root=root)
    _fire(sel_env, _txt(777, "시간대 체크"), repo_root=tmp_path, target_root=root)
    assert sel_env.runs == [(777, str(tmp_path / "trading_info"), "시간대 체크")]


def test_bare_project_name_pins_selection_without_running(sel_env, monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "PROJECT_LABELS", {"trading_info": "데모 라벨"})
    (tmp_path / "trading_info").mkdir()
    root = str(tmp_path)
    _fire(sel_env, _txt(777, "trading_info"), repo_root=tmp_path, target_root=root)
    assert bridge.chat_selection[777] == "trading_info"
    assert sel_env.runs == []
    assert any(
        "데모 라벨" in t and "trading_info" in t and "선택" in t for _c, t, _b in sel_env.sent
    )


# ===========================================================================
# 어댑터 정규화 — TelegramAdapter update dict → Event (§1.4)
# ===========================================================================


def _ta(tmp_path):
    return TelegramAdapter("T", [], tmp_path / "offset")


def test_adapter_normalizes_text_update(tmp_path):
    upd = {"message": {"chat": {"id": 777}, "from": {"id": 777}, "message_id": 5, "text": "hi"}}
    events = list(_ta(tmp_path)._to_events(upd))
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "text" and ev.channel_id == 777 and ev.user_id == 777
    assert ev.text == "hi" and ev.message_id == 5


def test_adapter_normalizes_non_text_to_empty(tmp_path):
    # 스티커 등(text·photo 없음) → text="" 로 정규화(코어가 "텍스트만 처리").
    upd = {"message": {"chat": {"id": 777}, "from": {"id": 777}, "message_id": 5}}
    ev = next(iter(_ta(tmp_path)._to_events(upd)))
    assert ev.kind == "text" and ev.text == ""


def test_adapter_normalizes_photo_update(tmp_path):
    upd = {
        "message": {
            "chat": {"id": 777},
            "from": {"id": 777},
            "message_id": 9,
            "caption": "MU",
            "photo": [{"file_id": "big", "width": 800, "height": 600}],
        }
    }
    ev = next(iter(_ta(tmp_path)._to_events(upd)))
    assert ev.kind == "photo" and ev.text == "MU" and ev.photo_ref == "big"


def test_adapter_normalizes_callback_update(tmp_path):
    cq = {
        "id": "cq9",
        "from": {"id": 777},
        "message": {"chat": {"id": 777}, "message_id": 42},
        "data": "p:etf_info",
    }
    ev = next(iter(_ta(tmp_path)._to_events({"callback_query": cq})))
    assert ev.kind == "button" and ev.action == "p" and ev.action_arg == "etf_info"
    assert ev.callback_id == "cq9" and ev.message_id == 42 and ev.user_id == 777


def test_adapter_callback_unknown_data_action_empty(tmp_path):
    # 미해석 callback_data → action="" (코어가 ack 후 무시).
    cq = {"id": "cq9", "from": {"id": 777}, "message": {"chat": {"id": 777}}, "data": "bogus"}
    ev = next(iter(_ta(tmp_path)._to_events({"callback_query": cq})))
    assert ev.kind == "button" and ev.action == ""


def test_adapter_ignores_edited_message(tmp_path):
    # D6: edited_message 는 무시(신규 message 만 트리거).
    upd = {"edited_message": {"chat": {"id": 777}, "text": "x"}}
    assert list(_ta(tmp_path)._to_events(upd)) == []


# ===========================================================================
# 어댑터 송신 — 마스킹·청킹·오버플로·ack (코어→어댑터로 이동한 로직 회귀 잠금)
# ===========================================================================


def _spy_tg(monkeypatch):
    """telegram_adapter.tg_call 을 기록 스파이로 대체(네트워크 없이 method·params 확인)."""
    calls = []

    def fake(_token, method, params, **_kw):  # timeout 은 키워드로 옴
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 100 + len(calls)}}

    monkeypatch.setattr(telegram_adapter, "tg_call", fake)
    return calls


def test_adapter_send_chunks_and_buttons_last(monkeypatch, tmp_path):
    calls = _spy_tg(monkeypatch)
    ta = TelegramAdapter("T", [], tmp_path / "offset", limit=5)
    mid = ta.send(777, "abcdefghij", [Button("L", "push")])  # 10자 / limit 5 → 2 청크
    sends = [p for m, p in calls if m == "sendMessage"]
    assert len(sends) == 2
    assert "reply_markup" not in sends[0]  # 버튼은 마지막 청크에만
    assert "reply_markup" in sends[1]
    assert mid == 101  # 첫 청크 message_id 반환


def test_adapter_send_masks_secrets(monkeypatch, tmp_path):
    calls = _spy_tg(monkeypatch)
    ta = TelegramAdapter("T", ["SECRET"], tmp_path / "offset")
    ta.send(777, "tok=SECRET")
    assert calls[0][1]["text"] == "tok=***"


def test_adapter_edit_overflow_edits_then_sends(monkeypatch, tmp_path):
    # 오버플로(§2.2): 첫 청크는 in-place 편집, 나머지는 후속 발행.
    calls = _spy_tg(monkeypatch)
    ta = TelegramAdapter("T", [], tmp_path / "offset", limit=5)
    ta.edit(777, 42, "abcdefghij")  # 2 청크
    methods = [m for m, _p in calls]
    assert methods == ["editMessageText", "sendMessage"]


def test_adapter_ack_answers_and_none_is_noop(monkeypatch, tmp_path):
    calls = _spy_tg(monkeypatch)
    ta = TelegramAdapter("T", [], tmp_path / "offset")
    ta.ack("cq1")
    assert calls[0][0] == "answerCallbackQuery"
    ta.ack(None)  # callback_id=None → no-op
    assert len(calls) == 1


def test_adapter_send_returns_none_on_network_error(monkeypatch, tmp_path):
    # §2/§3.3: 전송 실패는 로그만·None 반환(코어 직렬 루프가 죽지 않게).
    def boom(*_a, **_kw):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(telegram_adapter, "tg_call", boom)
    ta = TelegramAdapter("T", [], tmp_path / "offset")
    assert ta.send(777, "hi") is None


def test_adapter_edit_swallows_network_error(monkeypatch, tmp_path):
    # §2.2/§3.3: edit 는 rate-limit·네트워크 오류를 삼키고 계속(raise 안 함).
    def boom(*_a, **_kw):
        raise urllib.error.URLError("rate limited")

    monkeypatch.setattr(telegram_adapter, "tg_call", boom)
    ta = TelegramAdapter("T", [], tmp_path / "offset")
    ta.edit(777, 42, "짧은 갱신")  # 예외가 전파되면 테스트 실패(반환 None, 조용히 계속)


# ===========================================================================
# 어댑터 offset 영속(§2.5) — 포이즌 메시지 재처리 방지(load/save + poll 선진행)
# ===========================================================================


def test_offset_roundtrip(tmp_path):
    p = tmp_path / "offset"
    save_offset(p, 4242)  # 임시파일→원자 교체(D2)
    assert load_offset(p) == 4242


def test_offset_missing_or_corrupt_returns_zero(tmp_path):
    assert load_offset(tmp_path / "nope") == 0  # 파일 없음
    p = tmp_path / "offset"
    p.write_text("garbage", encoding="utf-8")
    assert load_offset(p) == 0  # 손상값도 0(방어적) — 단, 0 이면 전량 재수신 위험은 감수


def test_poll_advances_and_persists_offset(monkeypatch, tmp_path):
    # D4/§2.5: update_id 를 먼저 추출해 offset 을 선진행·영속 → 포이즌 메시지 재수신 핫루프 방지.
    ta = TelegramAdapter("T", [], tmp_path / "offset")
    upd = {
        "update_id": 50,
        "message": {"chat": {"id": 777}, "from": {"id": 777}, "message_id": 1, "text": "hi"},
    }
    batches = iter([{"ok": True, "result": [upd]}])

    def fake(_tok, _method, _params, **_kw):
        b = next(batches, None)
        if b is None:
            ta.close()  # 두 번째 폴에서 루프 종료(무한 제너레이터 방지)
            return {"ok": True, "result": []}
        return b

    monkeypatch.setattr(telegram_adapter, "tg_call", fake)
    events = list(ta.poll())
    assert [e.text for e in events] == ["hi"]
    assert load_offset(tmp_path / "offset") == 51  # update_id+1 영속(다음 폴에서 재수신 안 함)
