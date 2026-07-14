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

import subprocess
from pathlib import Path

import bridge
from bridge import (
    chunk_text,
    format_reply,
    is_allowed,
    mask_secrets,
    parse_message,
    resolve_project,
)

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
