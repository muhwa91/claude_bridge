"""bridge.py 순수 함수 단위 테스트 (계약 기반).

계약 출처: docs/features/telegram_bridge/01-계획.md "순수 함수 계약" 섹션.
    parse_message(text) -> tuple[str, str] | None
    is_allowed(chat_id, allowed: frozenset[int]) -> bool
    resolve_project(name, target_root) -> str | None
    chunk_text(text, limit=4096) -> list[str]
    mask_secrets(text, secrets: list[str]) -> str

표준 pytest 만 사용(내장 tmp_path 픽스처 허용). 네트워크·subprocess 호출 없음.
bridge.py 가 병렬 구현 중이라 임포트가 실패할 수 있으나, 파일 작성이 산출물이다.
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import bridge
import pytest
from bridge import (
    build_compare_prompt,
    choice_keyboard,
    chunk_text,
    download_file,
    due_notifications,
    due_snoozes,
    event_to_progress,
    extract_photo,
    fetch_stock,
    format_reply,
    handle_callback,
    handle_photo,
    handle_update,
    is_allowed,
    load_notify_state,
    load_schedules,
    mask_secrets,
    notify_keyboard,
    parse_callback,
    parse_caption_ticker,
    parse_choice_prompt,
    parse_message,
    parse_stock_response,
    project_keyboard,
    push_keyboard,
    resolve_project,
    resolve_target,
    run_claude,
    save_notify_state,
    stock_url,
    valid_ticker,
)


def _assistant(*blocks):
    """assistant 이벤트 헬퍼 — message.content 블록 리스트로 감싼다."""
    return {"type": "assistant", "message": {"content": list(blocks)}}


# ---------------------------------------------------------------------------
# parse_message: "<프로젝트> <지시...>" → (project, task) / 커맨드·형식불일치는 None
# ---------------------------------------------------------------------------


def test_parse_message_normal_two_words():
    assert parse_message("trading_info 헤더고쳐줘") == ("trading_info", "헤더고쳐줘")


def test_parse_message_multiword_task():
    # 첫 토큰만 프로젝트, 나머지 전체가 지시(공백 포함 보존)
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
    # 지시 없이 프로젝트명만 → None
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
# push 별칭(PUSH_WORDS): 한글 "푸시" 계열도 push 라우팅. 정확 일치만 — 문장 속은 claude 작업.
# ---------------------------------------------------------------------------


def test_push_words_all_in_commands():
    # parse_message 가 별칭을 프로젝트명으로 오해하지 않도록 COMMANDS 에 포함돼야 한다.
    assert bridge.PUSH_WORDS <= bridge.COMMANDS


def test_parse_message_push_aliases_are_none():
    # bare 별칭(정확 일치)은 커맨드로 인식 → None(handle_update 가 do_push 라우팅).
    for word in bridge.PUSH_WORDS:
        assert parse_message(word) is None


def test_parse_message_sentence_with_push_word_still_parses():
    # 문장 전체는 push 아님 — 정상 파싱돼 claude 작업으로 가야 한다(부분매칭 금지).
    assert parse_message("기록해주고 푸시해줘") == ("기록해주고", "푸시해줘")


def test_push_words_exact_match_only():
    # 정확 일치만 push — 문장은 PUSH_WORDS 멤버가 아니다(오탐 방지 계약).
    assert "푸시해" in bridge.PUSH_WORDS
    assert "기록해주고 푸시해줘" not in bridge.PUSH_WORDS
    assert "push" in bridge.PUSH_WORDS


def test_push_word_casefold_matches_uppercase():
    # 폰 키보드 자동 대문자화("Push"/"PUSH")도 handle_update 의 casefold 로 push 인식.
    for variant in ("Push", "PUSH", "pUsH"):
        assert variant.casefold() in bridge.PUSH_WORDS


# ---------------------------------------------------------------------------
# is_allowed(chat_id, allowed)
# ---------------------------------------------------------------------------


def test_is_allowed_true_when_in_set():
    assert is_allowed(12345, frozenset({12345, 67890})) is True


def test_is_allowed_false_when_not_in_set():
    assert is_allowed(99999, frozenset({12345, 67890})) is False


def test_is_allowed_false_when_empty_allowlist():
    # 허용목록이 비면 아무도 통과 못 함(허용목록 제거=전면 차단, 보안 기본값)
    assert is_allowed(12345, frozenset()) is False


# ---------------------------------------------------------------------------
# resolve_project: target_root 직속 폴더명 정확 일치만 / 트래버설 거부
# ---------------------------------------------------------------------------


def test_resolve_project_exact_match_success(tmp_path):
    (tmp_path / "trading_info").mkdir()
    result = resolve_project("trading_info", str(tmp_path))
    assert result is not None
    # 반환이 절대/상대 어느 쪽이든 실제 대상 폴더를 가리켜야 한다
    assert Path(result).name == "trading_info"
    assert Path(result).is_dir()


def test_resolve_project_case_mismatch_rejected(tmp_path):
    # Windows 파일시스템은 대소문자 무시라, 정확 일치는 listdir 문자열 비교여야 함.
    # 나이브한 os.path.isdir(join(root, name)) 구현이면 이 테스트가 잡아낸다.
    (tmp_path / "trading_info").mkdir()
    assert resolve_project("Trading_Info", str(tmp_path)) is None


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
    # 실재하는 폴더의 절대경로라도, 폴더명 아닌 경로면 거부
    real = tmp_path / "realproj"
    real.mkdir()
    assert resolve_project(str(real), str(tmp_path)) is None


def test_resolve_project_empty_name_rejected(tmp_path):
    assert resolve_project("", str(tmp_path)) is None


# ---------------------------------------------------------------------------
# resolve_target: ④ chat 선택 고정 해석 (명시 우선 · 선택 fallback · 첫 진입/stale None)
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
    # (a) 버튼 선택 후 프로젝트명 없는 메시지 → 선택 프로젝트 + 메시지 전체가 task
    (tmp_path / "trading_info").mkdir()
    got = resolve_target("시간대 별로 체크하는거 각 몇시에 오지?", str(tmp_path), "trading_info")
    assert got is not None
    name, _path, task = got
    assert name == "trading_info"
    assert task == "시간대 별로 체크하는거 각 몇시에 오지?"  # 첫 단어까지 포함한 전체


def test_resolve_target_explicit_overrides_selection(tmp_path):
    # (b) 프로젝트명 명시 → 명시 우선(선택이 있어도 그걸 덮어쓸 이름을 반환)
    (tmp_path / "trading_info").mkdir()
    (tmp_path / "etf_info").mkdir()
    name, path, task = resolve_target("etf_info 로그 봐줘", str(tmp_path), "trading_info")
    assert name == "etf_info"
    assert Path(path).name == "etf_info"
    assert task == "로그 봐줘"


def test_resolve_target_no_selection_no_project_none(tmp_path):
    # (c) 선택 없고 프로젝트명 아님 → None(첫 진입 안내)
    (tmp_path / "trading_info").mkdir()
    assert resolve_target("시간대 별로 체크", str(tmp_path), None) is None


def test_resolve_target_stale_selection_rejected(tmp_path):
    # 선택된 폴더가 사라졌으면(resolve None) 선택 fallback 무효 → None(트래버설/무효 방어 재통과)
    assert resolve_target("작업 해줘", str(tmp_path), "gone_project") is None


def test_resolve_target_bare_project_name_empty_task(tmp_path):
    # 프로젝트명만(작업 없음) → 이름·경로 반환, task="" (호출측이 선택만 고정)
    (tmp_path / "trading_info").mkdir()
    name, _path, task = resolve_target("trading_info", str(tmp_path), None)
    assert name == "trading_info"
    assert task == ""


def test_resolve_target_traversal_first_word_falls_through_to_selection(tmp_path):
    # 첫 단어가 트래버설이면 명시 실패 → 유효 선택으로 fallback(전체 메시지 task)
    (tmp_path / "trading_info").mkdir()
    got = resolve_target("../etc 해줘", str(tmp_path), "trading_info")
    assert got is not None
    name, _path, task = got
    assert name == "trading_info"
    assert task == "../etc 해줘"


# ---------------------------------------------------------------------------
# chunk_text(text, limit=4096)
# ---------------------------------------------------------------------------


def test_chunk_text_under_limit_single_chunk():
    text = "a" * 100
    assert chunk_text(text) == [text]


def test_chunk_text_exactly_at_limit_single_chunk():
    text = "a" * 4096
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_chunk_text_one_over_limit_splits_into_two():
    # 경계 검증: 4097자 → 2개 [4096, 1]
    text = "a" * 4097
    chunks = chunk_text(text)
    assert len(chunks) == 2
    assert len(chunks[0]) == 4096
    assert len(chunks[1]) == 1


def test_chunk_text_empty_returns_list_with_empty_string():
    # 계약: 빈 문자열이면 [""] (빈 리스트가 아님)
    assert chunk_text("") == [""]


def test_chunk_text_every_chunk_within_limit():
    text = "b" * (4096 * 2 + 37)
    chunks = chunk_text(text)
    assert len(chunks) == 3
    assert all(len(c) <= 4096 for c in chunks)


def test_chunk_text_reconstructs_original_no_data_loss():
    # 분할이 데이터를 잃거나 중복시키지 않아야 한다
    text = "가나다" * 5000
    assert "".join(chunk_text(text)) == text


def test_chunk_text_custom_limit():
    chunks = chunk_text("abcde", limit=2)
    assert chunks == ["ab", "cd", "e"]
    assert all(len(c) <= 2 for c in chunks)


# ---------------------------------------------------------------------------
# mask_secrets(text, secrets)
# ---------------------------------------------------------------------------


def test_mask_secrets_single_value():
    assert mask_secrets("token=abc123", ["abc123"]) == "token=***"


def test_mask_secrets_multiple_values():
    result = mask_secrets("id=42 token=xyz", ["42", "xyz"])
    assert result == "id=*** token=***"


def test_mask_secrets_all_occurrences_replaced():
    # 같은 비밀값이 여러 번 나오면 전부 치환
    assert mask_secrets("xyz and xyz", ["xyz"]) == "*** and ***"


def test_mask_secrets_empty_list_keeps_original():
    assert mask_secrets("nothing secret here", []) == "nothing secret here"


def test_mask_secrets_empty_secret_string_does_not_destroy_text():
    # 빈 비밀문자열("")은 무시돼야 한다. 나이브한 str.replace("", "***")는
    # 모든 글자 사이에 ***를 삽입해 텍스트를 파괴/폭증시킨다 → 그 버그를 잡는다.
    result = mask_secrets("hello", ["", "ell"])
    assert result == "h***o"


def test_mask_secrets_only_empty_secret_keeps_original():
    result = mask_secrets("hello", [""])
    assert result == "hello"


# ---------------------------------------------------------------------------
# format_reply(data): claude JSON 결과 → 텔레그램 회신 텍스트 (순수 함수)
# ---------------------------------------------------------------------------


def test_format_reply_success_header_no_cost():
    reply = format_reply({"result": "작업 완료", "is_error": False, "total_cost_usd": 0.05})
    assert reply.startswith("[ ✅처리완료 ]")
    assert "작업 완료" in reply
    # 비용은 표시하지 않는다(정책). 고정 push/커밋 안내도 붙이지 않는다
    # (실제 커밋/푸시 안내는 handle_update 가 git 상태를 조회해 덧붙임).
    assert "비용" not in reply
    assert "push" not in reply
    assert "커밋" not in reply


def test_format_reply_error_header():
    reply = format_reply({"result": "실행 실패", "is_error": True})
    assert reply.startswith("[ ❌처리실패 ]")
    assert "실행 실패" in reply
    assert "비용" not in reply


def test_format_reply_empty_result_header_only():
    reply = format_reply({"result": "", "is_error": False})
    assert reply == "[ ✅처리완료 ]"
    assert "비용" not in reply


def test_format_reply_error_empty_result_header_only():
    # 실패 + 빈 result → 실패 헤더 단독
    reply = format_reply({"result": "", "is_error": True})
    assert reply == "[ ❌처리실패 ]"


# ---------------------------------------------------------------------------
# event_to_progress(event): stream-json 이벤트 → 진행 표시 한 줄 (순수 함수, 계약)
# 스키마는 실제 `claude --output-format stream-json --verbose` 방출을 실측해 맞춤.
# ---------------------------------------------------------------------------


def test_event_to_progress_text_narration():
    ev = _assistant({"type": "text", "text": "파일 목록을 확인합니다"})
    assert event_to_progress(ev) == "파일 목록을 확인합니다"


def test_event_to_progress_text_truncated_to_120():
    ev = _assistant({"type": "text", "text": "가" * 200})
    result = event_to_progress(ev)
    assert result == "가" * 120


def test_event_to_progress_text_stripped():
    ev = _assistant({"type": "text", "text": "  여백 제거  "})
    assert event_to_progress(ev) == "여백 제거"


def test_event_to_progress_masks_secret_before_truncation():
    # L-1: 경계에서 비밀값이 쪼개져 조각이 새지 않도록, 잘라내기 전에 마스킹해야 한다.
    secret = "C:\\Users\\Home"
    # secret 이 60자 경계에 걸치도록 배치(prefix 55 → secret 이 55~68 위치).
    # 마스킹을 안 하면 [:60] 이 "...C:\\Us" 같은 조각을 남긴다.
    cmd = "a" * 55 + secret + "tail"
    ev = _assistant({"type": "tool_use", "name": "Bash", "input": {"command": cmd}})
    line = event_to_progress(ev, [secret])
    assert secret not in line  # 잘린 조각도 없어야 함
    assert "C:\\Us" not in line  # 경계에서 쪼개진 조각도 없어야 함
    assert "***" in line


def test_event_to_progress_empty_text_is_none():
    ev = _assistant({"type": "text", "text": "   "})
    assert event_to_progress(ev) is None


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
    ev = _assistant({"type": "thinking", "thinking": "내부 추론", "signature": "x"})
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
    # message.content 가 리스트가 아니면 안전하게 None
    assert event_to_progress({"type": "assistant", "message": {"content": "oops"}}) is None
    assert event_to_progress({"type": "assistant"}) is None


# ---------------------------------------------------------------------------
# git_status_note / do_push: _git 을 monkeypatch(pytest 내장)해 분기 검증
# ---------------------------------------------------------------------------


def _fake_git(mapping):
    """subcommand 튜플 접두어 → (returncode, stdout, stderr) 매핑으로 _git 을 대체."""

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
    note = bridge.git_status_note(Path())
    assert note == "변경이 있으나 커밋되지 않았습니다(확인 필요)."


def test_git_status_note_no_ahead_clean(monkeypatch):
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (0, "0\n", ""), ("status",): (0, "", "")}),
    )
    assert bridge.git_status_note(Path()) == "변경 없음."


def test_git_status_note_revlist_fail_fallback(monkeypatch):
    # rev-list 실패 → ahead 0 안전 폴백(크래시 없이 dirty 만 반영)
    monkeypatch.setattr(
        bridge,
        "_git",
        _fake_git({("rev-list",): (128, "", "fatal"), ("status",): (0, " M x.py\n", "")}),
    )
    assert bridge.git_status_note(Path()) == "변경이 있으나 커밋되지 않았습니다(확인 필요)."


def test_git_status_note_status_fail_fallback(monkeypatch):
    # status 실패 → dirty False 안전 폴백
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
    result = bridge.do_push(Path())
    assert result.startswith(bridge.HEADER_DONE)


# ---------------------------------------------------------------------------
# project_keyboard / push_keyboard / parse_callback: 인라인 키보드 (순수 함수)
# ---------------------------------------------------------------------------


def test_project_keyboard_empty_no_buttons():
    assert project_keyboard([]) == {"inline_keyboard": []}


def test_project_keyboard_callback_data_prefix():
    kb = project_keyboard(["etf_info"])
    btn = kb["inline_keyboard"][0][0]
    assert btn["text"] == "etf_info"
    assert btn["callback_data"] == "p:etf_info"


def test_project_keyboard_two_per_row():
    kb = project_keyboard(["a", "b", "c", "d", "e"])
    rows = kb["inline_keyboard"]
    # 2개씩 → [a,b][c,d][e]
    assert [len(r) for r in rows] == [2, 2, 1]
    assert rows[0][1]["callback_data"] == "p:b"
    assert rows[2][0]["callback_data"] == "p:e"


def test_project_keyboard_callback_data_within_64_bytes():
    # 초과 이름은 callback_data 만 64바이트로 절단, 표시 text 는 전체.
    long_name = "가" * 100  # 3바이트 * 100 = 300바이트
    kb = project_keyboard([long_name])
    btn = kb["inline_keyboard"][0][0]
    assert btn["text"] == long_name
    assert len(btn["callback_data"].encode("utf-8")) <= 64
    # 부분 멀티바이트가 깨지지 않게 잘려야 한다(디코드 가능).
    assert btn["callback_data"].startswith("p:가")


def test_push_keyboard_structure():
    kb = push_keyboard()
    row = kb["inline_keyboard"][0]
    assert [b["callback_data"] for b in row] == ["push", "x"]
    assert row[0]["text"] == "✅ Push"
    assert row[1]["text"] == "❌ 취소"


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


# ---------------------------------------------------------------------------
# event_to_progress — text 경로 마스킹 (L-1 보강, 순수 함수)
# ---------------------------------------------------------------------------


def test_event_to_progress_text_masks_secret():
    # L-1: assistant text 블록에 비밀값이 섞여도 잘라내기 전에 마스킹돼야 한다.
    secret = "1234567890:ABCsecrettoken"
    ev = _assistant({"type": "text", "text": f"토큰은 {secret} 입니다"})
    line = event_to_progress(ev, [secret])
    assert line is not None
    assert secret not in line
    assert "***" in line


# ===========================================================================
# 아래는 순수 함수가 아닌 통합/보안 테스트 — subprocess·monkeypatch 사용.
#   run_claude 스트리밍 리더(D-1/D-2/D-3) 통합 + handle_callback 인가·라우팅.
# 디버거 재현 시나리오를 가짜 claude 실행 파일로 재현한다.
# ===========================================================================

# 가짜 claude 본체: stdin(task) 내용으로 동작을 분기해 NDJSON 을 stdout 으로 방출한다.
#  - STDERR_FLOOD: result 전에 stderr 로 대량 출력(파이프 버퍼 포화 → 드레인 없으면 데드락)
#  - NO_RESULT   : result 없이 stderr 에 진단 남기고 종료(폴백 경로)
#  - HANG        : result 방출 후 stdout 을 닫지 않고 장기 sleep(손자 fd 점유 재현)
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
    """가짜 claude 실행 파일(shim)을 만들고 경로를 반환.

    run_claude 가 claude_exe 뒤에 고정 플래그를 붙이므로, shim 은 그 인자를 무시하고
    python 으로 fake 스크립트를 실행한다(stdin/stdout/stderr 상속). 실제 claude 도
    Windows 에선 .CMD 배치 shim 이라 Popen 직접 실행이 동작한다(C-1 실측).
    """
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
    # break-on-result(D-2)가 정상(비-행) 작업 완료를 깨지 않는지 — 여전히 result 반환.
    exe = _fake_claude(tmp_path)
    data = run_claude(exe, str(tmp_path), "just do it", timeout=30)
    assert data.get("result") == "DONE_FAKE"
    assert data.get("is_error") is False


def test_run_claude_breaks_on_result_before_timeout(tmp_path):
    # D-2: fake 는 result 방출 후 stdout 을 닫지 않고 30초 sleep. run_claude 는
    # 30초 타임아웃을 기다리지 않고 result 를 즉시 반환해야 한다(break-on-result).
    exe = _fake_claude(tmp_path)
    start = time.monotonic()
    data = run_claude(exe, str(tmp_path), "HANG please", timeout=30)
    elapsed = time.monotonic() - start
    assert data.get("result") == "DONE_FAKE"  # D-3: 잔존 자식이 있어도 정상 결과 반환
    assert data.get("is_error") is False
    assert elapsed < 20  # 30초 데드라인을 안 기다림(break 실패 시 ~30초 → 실패로 검출)


def test_run_claude_stderr_flood_no_deadlock(tmp_path):
    # D-1: result 전에 stderr 로 대량 출력(파이프 버퍼 초과). 드레인 스레드가 배수하지
    # 않으면 자식이 stderr write 에서 블록 → result 미도달 → 거짓 타임아웃.
    exe = _fake_claude(tmp_path)
    start = time.monotonic()
    data = run_claude(exe, str(tmp_path), "STDERR_FLOOD then work", timeout=30)
    elapsed = time.monotonic() - start
    assert data.get("result") == "DONE_FAKE"
    assert elapsed < 20


def test_run_claude_no_result_falls_back_to_stderr(tmp_path):
    # D-1 폴백: result 없이 종료 시 드레인 버퍼(stderr tail)로 진단을 반환.
    exe = _fake_claude(tmp_path)
    data = run_claude(exe, str(tmp_path), "NO_RESULT crash", timeout=30)
    assert data.get("is_error") is True
    assert "fatal" in str(data.get("result", ""))


# ---------------------------------------------------------------------------
# handle_callback — 인라인 버튼 인가·라우팅 (네트워크·push 함수 monkeypatch)
# ---------------------------------------------------------------------------

_ALLOWED = frozenset({777})


@pytest.fixture
def cb_spy(monkeypatch):
    """answer_callback·send_message·edit_message·do_push 를 스파이로 대체."""
    calls = {"answer": [], "send": [], "edit": [], "push": []}

    def fake_answer(_token, cq_id):
        calls["answer"].append(cq_id)

    def fake_send(_token, chat_id, text, _secrets, reply_markup=None):
        calls["send"].append((chat_id, text, reply_markup))

    def fake_edit(_token, chat_id, message_id, text, _secrets):
        calls["edit"].append((chat_id, message_id, text))

    def fake_push(root):
        calls["push"].append(root)
        return bridge.HEADER_DONE + "\n\npush ok"

    monkeypatch.setattr(bridge, "answer_callback", fake_answer)
    monkeypatch.setattr(bridge, "send_message", fake_send)
    monkeypatch.setattr(bridge, "edit_message", fake_edit)
    monkeypatch.setattr(bridge, "do_push", fake_push)
    return calls


def _cq(chat_id, data, cq_id="cq1", message_id=99):
    msg = {"chat": {"id": chat_id}}
    if message_id is not None:
        msg["message_id"] = message_id
    return {"id": cq_id, "from": {"id": chat_id}, "message": msg, "data": data}


def test_callback_disallowed_chat_nothing_called(cb_spy, tmp_path):
    # HIGH: 미허용 chat 은 허용목록 게이트에서 즉시 거부 — answer·push·send 전부 미호출.
    handle_callback(_cq(999, "push"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert cb_spy == {"answer": [], "send": [], "edit": [], "push": []}


def test_callback_valid_project_sends_guide(cb_spy, tmp_path):
    # HIGH: p:<유효> → resolve_project 통과 시 안내만 발송(push·edit 없음).
    (tmp_path / "etf_info").mkdir()
    handle_callback(_cq(777, "p:etf_info"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert cb_spy["push"] == []
    assert len(cb_spy["send"]) == 1
    chat_id, text, _markup = cb_spy["send"][0]
    assert chat_id == 777
    assert "etf_info" in text


def test_callback_invalid_project_no_send(cb_spy, tmp_path):
    # HIGH: p:<트래버설> → resolve_project None → 무시(발송 없음).
    handle_callback(_cq(777, "p:../secret"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert cb_spy["send"] == []
    assert cb_spy["push"] == []


def test_callback_push_calls_do_push_and_edits(cb_spy, tmp_path):
    # MEDIUM: push → do_push 호출 후 결과로 원본 메시지 edit.
    handle_callback(_cq(777, "push"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert len(cb_spy["push"]) == 1
    assert len(cb_spy["edit"]) == 1
    _cid, mid, text = cb_spy["edit"][0]
    assert mid == 99
    assert text.startswith(bridge.HEADER_DONE)


def test_callback_cancel_edits_message(cb_spy, tmp_path):
    # MEDIUM: x → "취소" 로 edit, push 없음.
    handle_callback(_cq(777, "x"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert cb_spy["push"] == []
    assert cb_spy["edit"][0][2] == "취소했습니다."


def test_callback_push_no_message_id_send_fallback(cb_spy, tmp_path):
    # MEDIUM: message_id 없으면 edit 대신 send 폴백.
    handle_callback(_cq(777, "push", message_id=None), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert len(cb_spy["push"]) == 1
    assert cb_spy["edit"] == []
    assert len(cb_spy["send"]) == 1


def test_callback_unknown_data_ignored(cb_spy, tmp_path):
    # MEDIUM: parse_callback None(알 수 없는 data) → 라우팅 액션 없음(스피너만 종료).
    handle_callback(_cq(777, "bogus"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert cb_spy["send"] == []
    assert cb_spy["edit"] == []
    assert cb_spy["push"] == []
    assert cb_spy["answer"] == ["cq1"]


# ===========================================================================
# ① 시각 알림 — load_schedules / due_notifications / due_snoozes / notify_keyboard
#   parse_callback(nb) / notify_state 왕복. 전부 순수(파일은 tmp_path).
# 계약: docs/features/remote-assistant/01-계획.md "① 시각 알림" 섹션.
# ===========================================================================

_KST = bridge._KST  # 고정 오프셋 +09:00(tzdata 불필요) — 프로덕션과 동일 tz 로 테스트
# 2026-07-15 = 수요일.
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
    items = load_schedules(p)
    assert [it["id"] for it in items] == ["a", "b"]  # dict 아닌 항목 제거


def test_load_schedules_non_list_items_empty(tmp_path):
    p = tmp_path / "notify.json"
    p.write_text('{"items": "oops"}', encoding="utf-8")
    assert load_schedules(p) == []


def test_due_notifications_in_window():
    assert due_notifications([_item()], _WED_0910, set()) == [_item()]


def test_due_notifications_at_window_start_inclusive():
    assert due_notifications([_item()], _WED_0900, set()) == [_item()]


def test_due_notifications_at_window_end_inclusive():
    # 09:00 + 30분 = 09:30 경계 포함, 09:31 은 밖.
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
    it = {"id": "x", "days": ["wed"], "at": "09:00"}  # grace 없음
    assert due_notifications([it], _WED_0910, set()) == [it]


def test_due_snoozes_past_refire_returned():
    past = datetime(2026, 7, 15, 9, 0, tzinfo=_KST).isoformat()
    assert due_snoozes({"x": past}, _WED_0910) == ["x"]


def test_due_snoozes_future_not_returned():
    future = datetime(2026, 7, 15, 10, 0, tzinfo=_KST).isoformat()
    assert due_snoozes({"x": future}, _WED_0910) == []


def test_due_snoozes_corrupt_iso_skipped():
    assert due_snoozes({"x": "not-a-date"}, _WED_0910) == []


def test_notify_keyboard_callback_data():
    kb = notify_keyboard("ti-kospi-open")
    row = kb["inline_keyboard"][0]
    assert [b["callback_data"] for b in row] == ["nb:ok:ti-kospi-open", "nb:later:ti-kospi-open"]


def test_parse_callback_nb_ok():
    assert parse_callback("nb:ok:ti-rollover") == ("nb:ok", "ti-rollover")


def test_parse_callback_nb_later():
    assert parse_callback("nb:later:ti-rollover") == ("nb:later", "ti-rollover")


def test_parse_callback_nb_empty_id_rejected():
    assert parse_callback("nb:ok:") is None
    assert parse_callback("nb:later:") is None


def test_parse_callback_nb_unsafe_id_rejected():
    # charset 밖(경로·주입 방어) → None.
    assert parse_callback("nb:ok:bad/id") is None
    assert parse_callback("nb:ok:a b") is None
    assert parse_callback("nb:ok:" + "z" * 65) is None


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
    assert fired == {("today", "2026-07-15")}  # 지난 날짜 fired 제거
    assert snooze == {"fresh": "2026-07-15T09:00:00+09:00"}  # 지난 날짜 스누즈 제거


def test_notify_state_missing_file_empty(tmp_path):
    assert load_notify_state(tmp_path / "nope.json", "2026-07-15") == (set(), {})


def test_notify_state_snooze_across_midnight_preserved(tmp_path):
    # 🟡 fix1: 23:55 스누즈 → refire 익일 00:25. 자정 전 재로드 시 내일 refire 가 보존돼야 한다.
    # (구현이 == today 였다면 폐기됐다. >= today 로 지난 날짜만 폐기.)
    p = tmp_path / "notify_state.json"
    save_notify_state(p, set(), {"a": "2026-07-16T00:25:00+09:00"})
    _fired, snooze = load_notify_state(p, "2026-07-15")
    assert snooze == {"a": "2026-07-16T00:25:00+09:00"}


def test_load_schedules_rejects_unsafe_id(tmp_path):
    # 🟢 fix3: 방출측 id 검증 대칭 — charset 위반·과길이 항목은 조용히 skip.
    p = tmp_path / "notify.json"
    p.write_text(
        '{"items": [{"id": "ok-1"}, {"id": "bad/id"}, {"id": ""}, {"id": 5}]}',
        encoding="utf-8",
    )
    assert [it["id"] for it in load_schedules(p)] == ["ok-1"]


def test_due_snoozes_tz_naive_iso_skipped():
    # 🟢 fix2: tz-naive ISO(상태파일 손상) → aware↔naive 비교 TypeError 를 삼키고 skip.
    assert due_snoozes({"a": "2026-07-15T09:00:00"}, _WED_0910) == []


# ---------------------------------------------------------------------------
# 상태변이·오케스트레이션: dispatch_notifications / handle_callback nb 분기
#   전역 상태(notify_fired/notify_snooze) 격리 + send/edit/answer/save 스파이.
# ---------------------------------------------------------------------------


def _freeze_now(monkeypatch, fixed):
    """bridge.datetime.now() 를 fixed 로 고정(fromisoformat 등은 실제 위임)."""

    class FakeDatetime(datetime):
        @classmethod
        def now(cls, *_args, **_kwargs):
            return fixed

    monkeypatch.setattr(bridge, "datetime", FakeDatetime)


@pytest.fixture
def notify_env(monkeypatch):
    """알림 전역 상태 격리 + send/edit/answer/save_notify_state 스파이."""
    bridge.notify_fired.clear()
    bridge.notify_snooze.clear()
    calls = {"send": [], "edit": [], "answer": [], "save": []}

    def fake_send(_token, chat_id, text, _secrets, reply_markup=None):
        calls["send"].append((chat_id, text, reply_markup))

    def fake_edit(_token, chat_id, message_id, text, _secrets):
        calls["edit"].append((chat_id, message_id, text))

    def fake_answer(_token, cq_id):
        calls["answer"].append(cq_id)

    def fake_save(_path, fired, snooze):
        calls["save"].append((set(fired), dict(snooze)))

    monkeypatch.setattr(bridge, "send_message", fake_send)
    monkeypatch.setattr(bridge, "edit_message", fake_edit)
    monkeypatch.setattr(bridge, "answer_callback", fake_answer)
    monkeypatch.setattr(bridge, "save_notify_state", fake_save)
    yield calls
    bridge.notify_fired.clear()
    bridge.notify_snooze.clear()


def test_dispatch_fans_out_to_all_allowed_and_marks_fired(notify_env, monkeypatch):
    _freeze_now(monkeypatch, _WED_0910)
    bridge.dispatch_notifications("T", frozenset({111, 222}), [], [_item(id="a")])
    # 허용목록 chat 전체에 팬아웃, 각 발송에 notify_keyboard 첨부
    assert {c for c, _t, _m in notify_env["send"]} == {111, 222}
    for _c, _t, markup in notify_env["send"]:
        assert markup["inline_keyboard"][0][0]["callback_data"] == "nb:ok:a"
    assert ("a", "2026-07-15") in bridge.notify_fired
    assert len(notify_env["save"]) == 1


def test_dispatch_snooze_refires_then_pops(notify_env, monkeypatch):
    _freeze_now(monkeypatch, _WED_0931)  # 스케줄 창 밖 → due 아님, 스누즈만 발송
    bridge.notify_fired.add(("a", "2026-07-15"))
    bridge.notify_snooze["a"] = datetime(2026, 7, 15, 9, 20, tzinfo=_KST).isoformat()  # 지남
    bridge.dispatch_notifications("T", frozenset({111}), [], [_item(id="a")])
    assert len(notify_env["send"]) == 1
    assert "a" not in bridge.notify_snooze  # 발송 후 pop(1회성)


def test_dispatch_due_and_snooze_no_double_send(notify_env, monkeypatch):
    _freeze_now(monkeypatch, _WED_0910)  # 창 안 → due 이면서 스누즈도 지남
    bridge.notify_snooze["a"] = datetime(2026, 7, 15, 9, 0, tzinfo=_KST).isoformat()
    bridge.dispatch_notifications("T", frozenset({111}), [], [_item(id="a")])
    assert len(notify_env["send"]) == 1  # 병합 시 한 번만


@pytest.mark.usefixtures("notify_env")
def test_dispatch_prunes_stale_date(monkeypatch):
    _freeze_now(monkeypatch, datetime(2026, 7, 15, 3, 0, tzinfo=_KST))
    bridge.notify_fired.add(("old", "2026-07-14"))
    bridge.dispatch_notifications("T", frozenset({111}), [], [])
    assert ("old", "2026-07-14") not in bridge.notify_fired  # 날짜 롤오버 정리


def test_dispatch_no_targets_no_send(notify_env, monkeypatch):
    _freeze_now(monkeypatch, _WED_0931)  # 창 밖·스누즈 없음
    bridge.dispatch_notifications("T", frozenset({111}), [], [_item(id="a")])
    assert notify_env["send"] == []
    assert notify_env["save"] == []


def test_callback_nb_ok_edits_and_clears_snooze(notify_env, tmp_path):
    bridge.notify_snooze["a"] = "2026-07-15T09:00:00+09:00"
    handle_callback(_cq(777, "nb:ok:a"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert notify_env["edit"][0][2].startswith("✅")  # 확인 접수·버튼 제거
    assert "a" not in bridge.notify_snooze
    assert len(notify_env["save"]) == 1  # 스누즈 변화 → save


def test_callback_nb_ok_without_snooze_no_save(notify_env, tmp_path):
    handle_callback(_cq(777, "nb:ok:a"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert notify_env["edit"][0][2].startswith("✅")
    assert notify_env["save"] == []  # 변화 없으면 불필요한 IO 없음


def test_callback_nb_later_snoozes_and_saves(notify_env, monkeypatch, tmp_path):
    _freeze_now(monkeypatch, _WED_0910)  # 09:10 → +30분 = 09:40
    handle_callback(_cq(777, "nb:later:a"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert bridge.notify_snooze["a"].startswith("2026-07-15T09:40")
    assert len(notify_env["save"]) == 1
    assert notify_env["edit"][0][2].startswith("⏰")  # "30분 뒤" 안내·버튼 제거


def test_callback_nb_disallowed_chat_ignored(notify_env, tmp_path):
    # 보안: 미허용 chat 은 nb 분기도 게이트에서 즉시 거부.
    handle_callback(_cq(999, "nb:later:a"), "T", _ALLOWED, tmp_path, str(tmp_path), [])
    assert notify_env["edit"] == []
    assert notify_env["save"] == []
    assert bridge.notify_snooze == {}


# ===========================================================================
# ② 사진 대조 — extract_photo / valid_ticker / parse_caption_ticker / stock_url
#   parse_stock_response / build_compare_prompt (순수) + download_file/fetch_stock
#   (urllib monkeypatch). 계약: 01-계획.md "② 사진 대조". 실제 네트워크 없음.
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
    # 오름차순 가정에 의존하지 않는다 — 큰 것이 배열 앞에 와도 선택.
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
    assert parse_caption_ticker("mu") == "MU"  # 대문자화
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
    # change_percent 는 현재가 없을 때 응답에서 빠질 수 있다 → None 안전.
    out = parse_stock_response({"session": "프리마켓"})
    assert out["change_percent"] is None
    assert out["change_amount"] is None
    assert out["session"] == "프리마켓"


def test_build_compare_prompt_contains_values_and_no_commit():
    prompt = build_compare_prompt(Path("logs/photos/x.jpg"), "MU", {"change_percent": -3.1})
    assert "MU" in prompt
    assert "-3.1" in prompt
    assert "x.jpg" in prompt
    assert "커밋은 하지" in prompt  # 되확인 안전핀(③ 전 폴백)


# --- download_file / fetch_stock: urllib monkeypatch(실제 네트워크 없음) ---


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
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: resp)


def test_download_file_writes_basename_only(monkeypatch, tmp_path):
    _patch_urlopen(monkeypatch, _FakeResp(b"\xff\xd8jpegdata"))
    dest = download_file("TOKEN", "photos/file_99.jpg", tmp_path)
    assert dest.name == "file_99.jpg"
    assert dest.parent == tmp_path  # 경로 성분 제거(트래버설 차단)
    assert dest.read_bytes() == b"\xff\xd8jpegdata"


def test_download_file_traversal_path_stays_in_dest(monkeypatch, tmp_path):
    # file_path 에 상위 이동이 있어도 basename 만 → dest_dir 밖으로 못 나감.
    _patch_urlopen(monkeypatch, _FakeResp(b"x"))
    dest = download_file("T", "a/../../evil.png", tmp_path)
    assert dest.name == "evil.png"
    assert dest.parent == tmp_path


def test_download_file_rejects_bad_extension(monkeypatch, tmp_path):
    _patch_urlopen(monkeypatch, _FakeResp(b"x"))
    with pytest.raises(ValueError, match="확장자"):
        download_file("T", "photos/x.gif", tmp_path)


def test_download_file_rejects_oversize_body(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "MAX_PHOTO_BYTES", 4)
    _patch_urlopen(monkeypatch, _FakeResp(b"toolongbody"))
    with pytest.raises(ValueError, match=r"10MB|상한"):
        download_file("T", "photos/x.jpg", tmp_path)


def test_download_file_rejects_oversize_content_length(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "MAX_PHOTO_BYTES", 4)
    _patch_urlopen(monkeypatch, _FakeResp(b"ok", headers={"Content-Length": "999"}))
    with pytest.raises(ValueError, match=r"10MB|상한"):
        download_file("T", "photos/x.jpg", tmp_path)


def test_fetch_stock_parses_response(monkeypatch):
    body = b'{"change_percent": -3.1, "session": "\\uc815\\uaddc\\uc7a5", "name": "MU"}'
    _patch_urlopen(monkeypatch, _FakeResp(body))
    out = fetch_stock("MU")
    assert out["change_percent"] == -3.1
    assert out["session"] == "정규장"


def test_fetch_stock_rejects_invalid_ticker_before_network(monkeypatch):
    # SSRF: 무효 ticker 는 URL 조립 단계(stock_url)에서 차단 — 네트워크 도달 전.
    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("네트워크 호출되면 안 됨")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(ValueError):
        fetch_stock("../etc/passwd")
    assert called["n"] == 0


# --- handle_photo 오케스트레이션 (download/REST/run 스파이) + 게이트 대칭 ---


def _photo_upd(chat_id, caption="MU", with_photo=True):
    msg = {"chat": {"id": chat_id}}
    if caption is not None:
        msg["caption"] = caption
    if with_photo:
        msg["photo"] = [{"file_id": "f", "width": 100, "height": 100}]
    return {"message": msg}


@pytest.fixture
def photo_spy(monkeypatch):
    """send/run_claude_with_progress/tg_get_file/download_file/fetch_stock 를 해피패스 스파이로."""
    calls = {"send": [], "run": [], "getfile": 0, "download": 0, "fetch": 0}

    def fake_send(_t, chat_id, text, _s, _reply_markup=None):
        calls["send"].append((chat_id, text))

    def fake_run(*args):  # (token, chat_id, header, exe, proj, task, timeout, secrets, allowed)
        calls["run"].append({"allowed_tools": args[8] if len(args) > 8 else None})
        return {"result": "✅ 일치", "is_error": False}

    def fake_getfile(_t, _fid):
        calls["getfile"] += 1
        return "photos/x.jpg"

    def fake_download(_t, _fp, dest_dir):
        calls["download"] += 1
        return dest_dir / "x.jpg"  # 존재하지 않아도 unlink(missing_ok=True) 안전

    def fake_fetch(_ticker):
        calls["fetch"] += 1
        return {"change_percent": -3.1, "session": "정규장"}

    monkeypatch.setattr(bridge, "send_message", fake_send)
    monkeypatch.setattr(bridge, "run_claude_with_progress", fake_run)
    monkeypatch.setattr(bridge, "tg_get_file", fake_getfile)
    monkeypatch.setattr(bridge, "download_file", fake_download)
    monkeypatch.setattr(bridge, "fetch_stock", fake_fetch)
    return calls


def test_photo_no_caption_prompts_no_run(photo_spy, tmp_path):
    (tmp_path / "trading_info").mkdir()
    handle_photo(_photo_upd(777, caption=None), 777, "T", "c", str(tmp_path), 60, [])
    assert photo_spy["run"] == []  # claude 미호출
    assert any("캡션" in t for _c, t in photo_spy["send"])


def test_photo_no_photo_size_prompts_no_run(photo_spy, tmp_path):
    (tmp_path / "trading_info").mkdir()
    upd = _photo_upd(777, caption="MU", with_photo=False)
    handle_photo(upd, 777, "T", "c", str(tmp_path), 60, [])
    assert photo_spy["run"] == []
    assert any("사진을 읽지" in t for _c, t in photo_spy["send"])


def test_photo_download_fail_graceful(photo_spy, monkeypatch, tmp_path):
    (tmp_path / "trading_info").mkdir()

    def boom(*_a):
        raise OSError("net down")

    monkeypatch.setattr(bridge, "tg_get_file", boom)
    handle_photo(_photo_upd(777), 777, "T", "c", str(tmp_path), 60, [])
    assert photo_spy["run"] == []
    assert any("내려받지" in t for _c, t in photo_spy["send"])


def test_photo_rest_fail_graceful(photo_spy, monkeypatch, tmp_path):
    (tmp_path / "trading_info").mkdir()

    def boom(_t):
        raise OSError("conn refused")  # 서버(:8000) 미기동 상황

    monkeypatch.setattr(bridge, "fetch_stock", boom)
    handle_photo(_photo_upd(777), 777, "T", "c", str(tmp_path), 60, [])
    assert photo_spy["run"] == []
    assert any("REST 응답 없음" in t for _c, t in photo_spy["send"])


def test_photo_normal_runs_with_read_only_tools(photo_spy, tmp_path):
    # M-1 회귀 잠금: 사진 대조 run 은 반드시 Read 전용 도구셋으로 호출.
    (tmp_path / "trading_info").mkdir()
    handle_photo(
        _photo_upd(777, caption="trading_info MU 대조"), 777, "T", "c", str(tmp_path), 60, []
    )
    assert len(photo_spy["run"]) == 1
    assert photo_spy["run"][0]["allowed_tools"] == ["Read"]
    assert photo_spy["fetch"] == 1


def test_photo_disallowed_chat_never_downloads(monkeypatch, tmp_path):
    # 보안 회귀 잠금: 미허용 chat 의 사진은 handle_update 게이트에서 차단 → handle_photo 미도달.
    reached = []
    monkeypatch.setattr(bridge, "handle_photo", lambda *_a, **_k: reached.append(1))
    monkeypatch.setattr(bridge, "send_message", lambda *_a, **_k: None)
    handle_update(_photo_upd(999), "T", frozenset({777}), "c", tmp_path, str(tmp_path), 60, [])
    assert reached == []


def test_photo_allowed_chat_triggers_handler(monkeypatch, tmp_path):
    reached = []
    monkeypatch.setattr(bridge, "handle_photo", lambda *_a, **_k: reached.append(1))
    monkeypatch.setattr(bridge, "send_message", lambda *_a, **_k: None)
    handle_update(_photo_upd(777), "T", frozenset({777}), "c", tmp_path, str(tmp_path), 60, [])
    assert reached == [1]


# ===========================================================================
# ③ 버튼 선택지 — parse_choice_prompt / choice_keyboard / parse_callback(c)
#   handle_callback c 분기 · await_reply 라우팅. 계약: 01-계획.md "③ 버튼 선택지".
# ===========================================================================


def test_parse_choice_prompt_normal():
    out = parse_choice_prompt("옵션을 고르세요.\n❓선택: [유지|keep]|[교체|swap]")
    assert out == ("옵션을 고르세요.", [("유지", "keep"), ("교체", "swap")])


def test_parse_choice_prompt_inline_question_default():
    # 질문 텍스트가 없으면 기본 문구.
    out = parse_choice_prompt("❓선택: [예|yes]|[아니오|no]")
    assert out == ("선택하세요", [("예", "yes"), ("아니오", "no")])


def test_parse_choice_prompt_colon_newline():
    # 🟡 회귀: claude 가 콜론 뒤 개행해도 tail 전체 스캔으로 파싱(왕복 붕괴 방지).
    out = parse_choice_prompt("무엇을 할까요?\n❓선택:\n[유지|keep]|[교체|swap]")
    assert out == ("무엇을 할까요?", [("유지", "keep"), ("교체", "swap")])


def test_parse_choice_prompt_multiline_choices():
    # 선택지가 여러 줄에 걸쳐도 대괄호 그룹을 모두 수집.
    out = parse_choice_prompt("❓선택:\n[예|yes]\n[아니오|no]")
    assert out == ("선택하세요", [("예", "yes"), ("아니오", "no")])


def test_parse_choice_prompt_non_choice_none():
    assert parse_choice_prompt("작업을 완료했습니다.") is None
    assert parse_choice_prompt("") is None


def test_parse_choice_prompt_broken_grammar_none():
    assert parse_choice_prompt("❓선택: [값없음]") is None  # `|` 누락
    assert parse_choice_prompt("❓선택: []|[|]") is None  # 빈 항목·빈 라벨/값
    assert parse_choice_prompt("❓선택: 아무거나") is None  # 대괄호 없음


def test_parse_choice_prompt_skips_malformed_keeps_valid():
    out = parse_choice_prompt("❓선택: [좋음|a]|[깨짐]|[나쁨|b]")
    assert out == ("선택하세요", [("좋음", "a"), ("나쁨", "b")])


def test_parse_choice_prompt_uses_last_marker():
    # 여러 줄 중 마지막 ❓선택 줄만 파싱(마지막 줄 규약).
    text = "설명 ❓선택: [무시|x]\n최종 질문\n❓선택: [진짜A|a]|[진짜B|b]"
    out = parse_choice_prompt(text)
    assert out is not None
    assert out[1] == [("진짜A", "a"), ("진짜B", "b")]


def test_choice_keyboard_structure():
    kb = choice_keyboard(77, [("유지", "keep"), ("교체", "swap")])
    flat = [b for row in kb["inline_keyboard"] for b in row]
    assert flat[0]["callback_data"] == "c:77:0"
    assert flat[1]["callback_data"] == "c:77:1"
    assert flat[-1] == {"text": "✏️ 직접입력", "callback_data": "c:77:other"}


def test_choice_keyboard_two_per_row():
    kb = choice_keyboard(1, [("a", "1"), ("b", "2"), ("c", "3")])
    # 3 선택지 + 직접입력 = 4버튼 → 2개씩 2행.
    assert [len(r) for r in kb["inline_keyboard"]] == [2, 2]


def test_choice_keyboard_callback_data_within_64_bytes():
    kb = choice_keyboard(123456, [("긴라벨" * 30, "v")])
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64


def test_parse_callback_choice_index():
    assert parse_callback("c:55:0") == ("c", "55:0")
    assert parse_callback("c:55:12") == ("c", "55:12")


def test_parse_callback_choice_other():
    assert parse_callback("c:55:other") == ("c", "55:other")


def test_parse_callback_choice_rejects_bad():
    assert parse_callback("c:x:1") is None  # msg_id 비정수
    assert parse_callback("c:55:bad") is None  # sel 비정수·비other
    assert parse_callback("c:55") is None  # 파트 부족
    assert parse_callback("c:55:1:2") is None  # 파트 초과


def test_parse_callback_choice_rejects_unicode_digits():
    # L-3: 전각·위첨자 등 유니코드 숫자(int 통과, isascii 실패)를 차단.
    fullwidth = "c:" + chr(0xFF15) * 2 + ":1"  # 전각 숫자 msg_id(FULLWIDTH DIGIT FIVE)
    superscript = "c:55:" + chr(0x00B2)  # 위첨자 숫자 idx(SUPERSCRIPT TWO)
    assert parse_callback(fullwidth) is None
    assert parse_callback(superscript) is None


# --- handle_callback c 분기 · await_reply 라우팅 (resume_run 스파이) ---


@pytest.fixture
def choice_env(monkeypatch):
    """pending 격리 + answer/send/edit/resume_run 스파이(실제 claude 미실행)."""
    bridge.pending.clear()
    calls = {"answer": [], "send": [], "edit": [], "resume": []}

    def fake_answer(_t, cq_id):
        calls["answer"].append(cq_id)

    def fake_send(_t, chat_id, text, _s, _rm=None):
        calls["send"].append((chat_id, text))

    def fake_edit(_t, _cid, message_id, text, _s):
        calls["edit"].append((message_id, text))

    def fake_resume(_tok, _cid, _exe, proj, answer, question, sid, _to, _sec):
        calls["resume"].append({"proj": proj, "answer": answer, "sid": sid, "question": question})

    monkeypatch.setattr(bridge, "answer_callback", fake_answer)
    monkeypatch.setattr(bridge, "send_message", fake_send)
    monkeypatch.setattr(bridge, "edit_message", fake_edit)
    monkeypatch.setattr(bridge, "resume_run", fake_resume)
    yield calls
    bridge.pending.clear()


def _pending_entry(await_reply=False, chat_id=777):
    return {
        "chat_id": chat_id,
        "session_id": "sid1",
        "project_path": "/proj",
        "choices": [("유지", "keep"), ("교체", "swap")],
        "question": "무엇을?",
        "await_reply": await_reply,
    }


def test_callback_choice_selection_resumes(choice_env):
    bridge.pending[50] = _pending_entry()
    handle_callback(_cq(777, "c:50:1"), "T", _ALLOWED, Path(), "root", [], "claude", 60)
    assert len(choice_env["resume"]) == 1
    r = choice_env["resume"][0]
    assert r["answer"] == "swap" and r["sid"] == "sid1" and r["proj"] == "/proj"
    assert 50 not in bridge.pending  # 소비(중복 탭 방지)
    assert any("교체" in t for _mid, t in choice_env["edit"])  # 버튼 제거 edit


def test_callback_choice_other_sets_await(choice_env):
    bridge.pending[50] = _pending_entry()
    handle_callback(_cq(777, "c:50:other"), "T", _ALLOWED, Path(), "root", [], "claude", 60)
    assert bridge.pending[50]["await_reply"] is True
    assert choice_env["resume"] == []
    assert any("답장으로" in t for _c, t in choice_env["send"])


def test_callback_choice_expired_pending(choice_env):
    # 재시작 등으로 보류맵에 없으면 만료 안내(claude 미실행).
    handle_callback(_cq(777, "c:99:0"), "T", _ALLOWED, Path(), "root", [], "claude", 60)
    assert choice_env["resume"] == []
    assert any("만료" in t for _mid, t in choice_env["edit"])


def test_callback_choice_out_of_range_ignored(choice_env):
    bridge.pending[50] = _pending_entry()  # 선택지 2개(0,1)
    handle_callback(_cq(777, "c:50:5"), "T", _ALLOWED, Path(), "root", [], "claude", 60)
    assert choice_env["resume"] == []  # 범위 밖 → 무시
    assert 50 in bridge.pending  # 소비 안 함


def test_callback_choice_disallowed_chat_blocked(choice_env):
    bridge.pending[50] = _pending_entry()
    handle_callback(_cq(999, "c:50:0"), "T", _ALLOWED, Path(), "root", [], "claude", 60)
    assert choice_env["resume"] == []
    assert choice_env["answer"] == []  # 허용목록 게이트에서 즉시 차단
    assert bridge.pending[50]["await_reply"] is False


def _text_upd(chat_id, text):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def test_await_reply_routes_text_to_resume(choice_env):
    bridge.pending[50] = _pending_entry(await_reply=True)
    handle_update(_text_upd(777, "직접 입력한 답"), "T", _ALLOWED, "claude", Path(), "root", 60, [])
    assert len(choice_env["resume"]) == 1
    assert choice_env["resume"][0]["answer"] == "직접 입력한 답"
    assert 50 not in bridge.pending  # 소비


def test_await_reply_cancel_clears(choice_env):
    bridge.pending[50] = _pending_entry(await_reply=True)
    handle_update(_text_upd(777, "/cancel"), "T", _ALLOWED, "claude", Path(), "root", 60, [])
    assert 50 not in bridge.pending
    assert choice_env["resume"] == []  # /cancel 은 resume 하지 않음
    assert any("취소" in t for _c, t in choice_env["send"])


# --- M-1: pending chat_id 격리 (타 chat 세션 탈취 방지) ---

_ALLOWED2 = frozenset({777, 888})


def test_callback_choice_other_chat_rejected(choice_env):
    bridge.pending[50] = _pending_entry(chat_id=777)  # 777 소유
    handle_callback(_cq(888, "c:50:1"), "T", _ALLOWED2, Path(), "root", [], "claude", 60)
    assert choice_env["resume"] == []  # 888 은 이어받지 못함(만료 취급)
    assert 50 in bridge.pending  # 777 항목 그대로


def test_await_reply_other_chat_not_routed(choice_env):
    bridge.pending[50] = _pending_entry(await_reply=True, chat_id=777)
    handle_update(_text_upd(888, "가로채기 시도"), "T", _ALLOWED2, "claude", Path(), "root", 60, [])
    assert choice_env["resume"] == []  # 888 답장이 777 세션으로 안 감
    assert 50 in bridge.pending  # 777 대기 유지


@pytest.mark.usefixtures("choice_env")
def test_cancel_other_chat_keeps_await():
    bridge.pending[50] = _pending_entry(await_reply=True, chat_id=777)
    handle_update(_text_upd(888, "/cancel"), "T", _ALLOWED2, "claude", Path(), "root", 60, [])
    assert 50 in bridge.pending  # 888 /cancel 이 777 대기를 해제하지 못함


# --- 핵심 배선 회귀 잠금: _render_choices / resume_run / run_claude_with_progress ---


def test_render_choices_registers_pending_and_keyboard(monkeypatch):
    bridge.pending.clear()
    kb_calls = []
    monkeypatch.setattr(bridge, "send_message_get_id", lambda *_a: 200)
    monkeypatch.setattr(
        bridge, "edit_message_reply_markup", lambda _t, _c, mid, kb: kb_calls.append((mid, kb))
    )
    bridge._render_choices("T", 777, "/proj", "sid-abc", ("Q", [("유지", "keep")]), [])
    assert 200 in bridge.pending
    e = bridge.pending[200]
    assert e["chat_id"] == 777 and e["session_id"] == "sid-abc" and e["project_path"] == "/proj"
    assert kb_calls and kb_calls[0][0] == 200  # 얻은 message_id 로 키보드 부착
    bridge.pending.clear()


def test_render_choices_skips_without_session_id(monkeypatch):
    bridge.pending.clear()
    monkeypatch.setattr(bridge, "send_message_get_id", lambda *_a: 200)
    bridge._render_choices("T", 777, "/proj", None, ("Q", [("a", "1")]), [])  # session_id 없음
    assert bridge.pending == {}
    bridge._render_choices("T", 777, "/proj", 123, ("Q", [("a", "1")]), [])  # 비-str
    assert bridge.pending == {}


def test_render_choices_masks_label(monkeypatch):
    # L-2: 라벨은 마스킹 안 된 result 재파싱분 → 버튼 text·저장분 모두 마스킹돼야.
    bridge.pending.clear()
    captured = {}
    monkeypatch.setattr(bridge, "send_message_get_id", lambda *_a: 200)
    monkeypatch.setattr(
        bridge, "edit_message_reply_markup", lambda _t, _c, _m, kb: captured.update(kb=kb)
    )
    bridge._render_choices("T", 777, "/p", "sid-1", ("Q", [("토큰SECRET표시", "v")]), ["SECRET"])
    label = captured["kb"]["inline_keyboard"][0][0]["text"]
    assert "SECRET" not in label and "***" in label
    assert bridge.pending[200]["choices"][0][0] == label  # 저장분도 마스킹
    bridge.pending.clear()


def test_resume_run_fallback_on_resume_error(monkeypatch):
    # 🟢 resume 실패(is_error) → --resume 없이 직전 질문+답 요약 재주입 폴백.
    calls = []

    def stub(_tok, _cid, _hdr, _exe, _proj, task, _to, _sec, **kw):
        calls.append({"task": task, "resume": kw.get("resume")})
        return {"is_error": len(calls) == 1, "result": ""}  # 첫(resume) 실패, 폴백 성공

    monkeypatch.setattr(bridge, "run_claude_with_progress", stub)
    bridge.resume_run("T", 777, "claude", "/p", "내 답", "원 질문", "sid-1", 60, [])
    assert len(calls) == 2
    assert calls[0]["resume"] == "sid-1"  # 1차: resume 시도
    assert calls[1]["resume"] is None  # 2차: 폴백(resume 없음)
    assert "원 질문" in calls[1]["task"] and "내 답" in calls[1]["task"]  # 맥락 재주입


def test_rcwp_read_only_skips_choice_render(monkeypatch):
    # 사진 Read 경로(allowed_tools=["Read"])는 ❓선택이 있어도 버튼 렌더 안 함.
    bridge.pending.clear()

    def fake_run(*_a, **_k):
        return {"result": "Q\n❓선택: [a|1]|[b|2]", "is_error": False, "session_id": "s"}

    monkeypatch.setattr(bridge, "run_claude", fake_run)
    monkeypatch.setattr(bridge, "send_message_get_id", lambda *_a: 10)
    monkeypatch.setattr(bridge, "edit_message", lambda *_a: None)
    monkeypatch.setattr(bridge, "send_message", lambda *_a, **_k: None)
    monkeypatch.setattr(bridge, "edit_message_reply_markup", lambda *_a: None)
    bridge.run_claude_with_progress("T", 777, "H", "c", "/p", "task", 60, [], ["Read"])
    assert bridge.pending == {}
    bridge.pending.clear()


def test_rcwp_full_path_renders_and_hides_marker(monkeypatch):
    # full 경로: ❓선택 감지 → 버튼 렌더 + 최종 회신에서 마커 줄 숨김(#5).
    bridge.pending.clear()
    edits = []
    monkeypatch.setattr(
        bridge,
        "run_claude",
        lambda *_a, **_k: {
            "result": "고르세요\n❓선택: [유지|keep]|[교체|swap]",
            "is_error": False,
            "session_id": "sid-1",
        },
    )
    ids = iter([10, 11])
    monkeypatch.setattr(bridge, "send_message_get_id", lambda *_a: next(ids))
    monkeypatch.setattr(bridge, "edit_message", lambda _t, _c, _m, txt, _s: edits.append(txt))
    monkeypatch.setattr(bridge, "send_message", lambda *_a, **_k: None)
    monkeypatch.setattr(bridge, "edit_message_reply_markup", lambda *_a: None)
    bridge.run_claude_with_progress("T", 777, "H", "c", "/p", "task", 60, [])
    assert edits and all("❓선택" not in t for t in edits)  # 내부 문법 미노출
    assert 11 in bridge.pending  # 버튼 메시지(두 번째 id)에 보류맵 등록
    assert bridge.pending[11]["chat_id"] == 777
    bridge.pending.clear()


# ===========================================================================
# ④ chat 프로젝트 선택 고정 — 버튼 탭 → 프로젝트명 생략 실행 · 명시 우선 갱신 · chat 격리.
#   handle_callback(p:) / handle_update 배선. run_claude_with_progress·git 스파이.
# ===========================================================================


@pytest.fixture
def sel_env(monkeypatch):
    """chat_selection 격리 + run_claude_with_progress·send·git 스파이(실제 claude 미실행)."""
    bridge.chat_selection.clear()
    calls = {"run": [], "send": []}

    def fake_run(_tok, chat_id, _hdr, _exe, proj_path, task, _to, _sec, **_kw):
        calls["run"].append((chat_id, proj_path, task))
        return {"is_error": False, "result": "ok"}

    def fake_send(_t, chat_id, text, _s, _rm=None):
        calls["send"].append((chat_id, text))

    monkeypatch.setattr(bridge, "run_claude_with_progress", fake_run)
    monkeypatch.setattr(bridge, "send_message", fake_send)
    monkeypatch.setattr(bridge, "answer_callback", lambda *_a: None)
    monkeypatch.setattr(bridge, "git_status_note", lambda _root: "변경 없음.")
    monkeypatch.setattr(bridge, "git_ahead", lambda _root: 0)
    yield calls
    bridge.chat_selection.clear()


def _sel_msg(chat_id, text):
    return {"message": {"chat": {"id": chat_id}, "text": text}}


def test_button_select_then_bare_task_uses_selection(sel_env, tmp_path):
    # (a) 버튼 탭으로 선택 고정 → 프로젝트명 없는 메시지가 그 프로젝트로 실행(전체가 task)
    (tmp_path / "trading_info").mkdir()
    root = str(tmp_path)
    handle_callback(_cq(777, "p:trading_info"), "T", _ALLOWED, tmp_path, root, [])
    assert bridge.chat_selection[777] == "trading_info"
    handle_update(
        _sel_msg(777, "시간대 별로 체크 각 몇시?"), "T", _ALLOWED, "c", tmp_path, root, 900, []
    )
    assert sel_env["run"] == [(777, str(tmp_path / "trading_info"), "시간대 별로 체크 각 몇시?")]


def test_explicit_message_updates_selection(sel_env, tmp_path):
    # (b) 명시 우선 + 선택 갱신 → 이후 프로젝트명 생략 시 갱신된 선택으로 실행
    (tmp_path / "trading_info").mkdir()
    (tmp_path / "etf_info").mkdir()
    root = str(tmp_path)
    handle_update(
        _sel_msg(777, "trading_info 헤더 고쳐"), "T", _ALLOWED, "c", tmp_path, root, 900, []
    )
    assert bridge.chat_selection[777] == "trading_info"
    handle_update(_sel_msg(777, "etf_info 로그 봐줘"), "T", _ALLOWED, "c", tmp_path, root, 900, [])
    assert bridge.chat_selection[777] == "etf_info"  # 명시로 덮어쓰기
    handle_update(_sel_msg(777, "이번엔 이거 해줘"), "T", _ALLOWED, "c", tmp_path, root, 900, [])
    assert sel_env["run"][-1][:2] == (777, str(tmp_path / "etf_info"))
    assert sel_env["run"][-1][2] == "이번엔 이거 해줘"


def test_no_selection_no_project_errors(sel_env, tmp_path):
    # (c) 선택 없고 프로젝트명 아님 → 실행 없이 "프로젝트 못 찾음" 안내
    (tmp_path / "trading_info").mkdir()
    handle_update(
        _sel_msg(777, "시간대 별로 체크"), "T", _ALLOWED, "c", tmp_path, str(tmp_path), 900, []
    )
    assert sel_env["run"] == []
    assert any("찾지 못했" in t for _c, t in sel_env["send"])
    assert 777 not in bridge.chat_selection


def test_selection_isolated_per_chat(sel_env, tmp_path):
    # (d) 한 chat 의 선택은 다른 chat 으로 새지 않는다(M-1 격리)
    (tmp_path / "trading_info").mkdir()
    root = str(tmp_path)
    allowed = frozenset({777, 888})
    handle_callback(_cq(777, "p:trading_info"), "T", allowed, tmp_path, root, [])
    assert bridge.chat_selection == {777: "trading_info"}  # 888 미설정
    # chat 888 은 선택 없음 → 프로젝트명 없는 메시지는 에러(실행 없음)
    handle_update(_sel_msg(888, "시간대 별로"), "T", allowed, "c", tmp_path, root, 900, [])
    assert sel_env["run"] == []
    assert 888 not in bridge.chat_selection


def test_bare_project_name_pins_selection_without_running(sel_env, tmp_path):
    # 프로젝트명만 보내면(작업 없음) 선택만 고정하고 안내(실행 없음)
    (tmp_path / "trading_info").mkdir()
    root = str(tmp_path)
    handle_update(_sel_msg(777, "trading_info"), "T", _ALLOWED, "c", tmp_path, root, 900, [])
    assert bridge.chat_selection[777] == "trading_info"
    assert sel_env["run"] == []
    assert any("선택했습니다" in t for _c, t in sel_env["send"])
