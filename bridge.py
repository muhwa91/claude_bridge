#!/usr/bin/env python3
"""telegram_bridge — 텔레그램 메시지로 Claude Code 작업을 원격 트리거하는 브리지.

Python 3.13 표준 라이브러리만 사용(외부 패키지 0). 단일 롱폴링 루프가 메시지를
직렬 처리한다: 인증 → 파싱 → 프로젝트 해석 → claude 실행 → 회신. `push` 답장 시에만
모노레포 루트에서 pull --rebase 후 push 한다.

보안 경계:
- chat ID 허용목록 필수. 미허용 메시지는 무회신·로그만.
- 메시지는 subprocess 리스트 인자(shell=False)로만 전달 — 셸 조립 금지.
- 봇 토큰은 .env·로컬 변수에만. os.environ·로그·자식 프로세스 env 어디에도 넣지 않는다.
- claude 권한은 --allowedTools 최소 스코프(일반 Bash·git push·네트워크 미부여).
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ── 경로 상수 ──────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).resolve().parent
LOG_DIR = PROJECT_DIR / "logs"
LOG_FILE = LOG_DIR / "bridge.log"
OFFSET_FILE = LOG_DIR / "offset"
PID_FILE = LOG_DIR / "bridge.pid"

# D5: Telegram 한도는 UTF-16 코드유닛 4096 기준이나 여기선 코드포인트로 분할하므로,
# 비-BMP 이모지 다량 시 초과 방지용 안전마진으로 4000 으로 낮춘다(완전 UTF-16 분할은 과함).
TELEGRAM_LIMIT = 4000
POLL_TIMEOUT = 25  # 텔레그램 롱폴링 대기(초)
COMMANDS = frozenset({"/help", "/start", "/projects", "/cancel", "push"})

# claude 헤드리스가 대상 폴더 상위의 루트 헌법(CLAUDE.md)을 로드하면 "세션 시작=신원 확인"
# 게이트에 걸려 작업 대신 인사를 반환한다. 이 정적 서문을 --append-system-prompt 로 주입해
# 원격 인증 맥락을 명시하고 그 게이트를 건너뛰게 한다. (사용자 task 는 여전히 stdin 전용 — C-1)
BRIDGE_SYSTEM_PROMPT = (
    "너는 telegram_bridge 를 통해 원격 실행되는 헤드리스 Claude 다. "
    "이 요청은 chat ID 허용목록으로 인증된 관리자의 원격 지시이며, 신원은 이미 확인됐다. "
    "따라서 세션 시작 신원 확인·비밀번호·작업 선택 메뉴를 절대 수행하지 말고, "
    "인사 없이 현재 작업 디렉터리의 프로젝트에서 지시된 작업만 바로 수행하라. "
    "작업을 마치면 변경사항을 Conventional Commit 메시지로 로컬 커밋하라. "
    "절대 push 하지 마라(push 는 관리자가 텔레그램에서 'push' 라고 답장해 승인한다). "
    "보호 대상(_Template/Dev, 루트 CLAUDE.md, 모델 설정)은 변경하지 마라. "
    "결과는 무엇을 했는지 1~3줄로 간결히 보고하라."
)

# claude CLI 허용 도구 화이트리스트(= 안전 경계). 일반 Bash·git push·네트워크 미포함.
ALLOWED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "Bash(git add *)",
    "Bash(git commit *)",
    "Bash(git status *)",
    "Bash(git diff *)",
    "Bash(ruff *)",
    "Bash(mypy *)",
    "Bash(pytest *)",
]

log = logging.getLogger("bridge")


# ══════════════════════════════════════════════════════════════════════════
# 순수 함수 (qa 병렬 테스트 대상 — 시그니처 고정)
# ══════════════════════════════════════════════════════════════════════════
def parse_message(text: str) -> tuple[str, str] | None:
    """ "<프로젝트> <지시>" → (project, task). 커맨드나 형식 불일치는 None."""
    stripped = text.strip()
    if not stripped or stripped in COMMANDS or stripped.startswith("/"):
        return None
    parts = stripped.split(maxsplit=1)
    if len(parts) < 2:
        return None
    project, task = parts[0], parts[1].strip()
    if not task:
        return None
    return project, task


def is_allowed(chat_id: int, allowed: frozenset[int]) -> bool:
    """chat_id 가 허용목록에 있는지."""
    return chat_id in allowed


def resolve_project(name: str, target_root: str) -> str | None:
    """target_root 직속 폴더명 정확 일치만 절대경로로 해석. 트래버설은 None."""
    if not name or name != name.strip():
        return None
    if ".." in name or "/" in name or "\\" in name or ":" in name:
        return None
    if Path(name).is_absolute():
        return None
    root = Path(target_root)
    candidate = root / name
    # Windows는 대소문자 무시라 is_dir()만으론 "Trading_Info"도 통과 — 폴더명 정확 비교로 판정
    try:
        exact = name in {p.name for p in root.iterdir()}
    except OSError:
        return None
    if exact and candidate.is_dir():
        return str(candidate)
    return None


def chunk_text(text: str, limit: int = 4096) -> list[str]:
    """텔레그램 한도(기본 4096)로 분할. 빈 문자열이면 [""]."""
    if text == "":
        return [""]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def mask_secrets(text: str, secrets: list[str]) -> str:
    """토큰·내부 경로 등 비밀값을 '***'로 치환."""
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return text


# ══════════════════════════════════════════════════════════════════════════
# 설정 · 저장소 상태
# ══════════════════════════════════════════════════════════════════════════
def load_env(path: Path) -> dict[str, str]:
    """.env 직접 파싱(KEY=VALUE, # 주석·빈 줄 무시, 양끝 따옴표 제거)."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def parse_allowed(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for tok in raw.split(","):
        tok = tok.strip()
        if tok:
            try:
                ids.add(int(tok))
            except ValueError:
                log.warning("허용목록에 숫자가 아닌 값 무시")
    return frozenset(ids)


def find_repo_root(start: Path) -> Path:
    """.git 이 있는 상위 폴더(모노레포 루트)를 찾는다."""
    for p in (start, *start.parents):
        if (p / ".git").exists():
            return p
    return start


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


def list_projects(target_root: str) -> list[str]:
    root = Path(target_root)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))


# ── 단일 인스턴스 락(pidfile) ───────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        # D3: PID 생존뿐 아니라 이미지명이 python 계열인지 확인 — 재부팅 후 stale pid 를
        # 무관 프로세스가 재사용하면 락 오탐으로 브리지가 조용히 안 뜨는 것을 막는다.
        line = r.stdout.strip().lower()
        return str(pid) in line and "python" in line
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def acquire_lock(pidfile: Path) -> bool:
    """다른 인스턴스가 살아있으면 False(409 방지)."""
    if pidfile.exists():
        try:
            old = int(pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            return False
    pidfile.write_text(str(os.getpid()), encoding="utf-8")
    return True


# ══════════════════════════════════════════════════════════════════════════
# 텔레그램 API
# ══════════════════════════════════════════════════════════════════════════
def tg_call(token: str, method: str, params: dict[str, Any], timeout: int) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload: dict[str, Any] = json.load(resp)
    return payload


def get_updates(token: str, offset: int) -> dict[str, Any]:
    return tg_call(
        token,
        "getUpdates",
        {"timeout": POLL_TIMEOUT, "offset": offset},
        timeout=POLL_TIMEOUT + 10,
    )


def send_message(token: str, chat_id: int, text: str, secrets: list[str]) -> None:
    """마스킹 후 TELEGRAM_LIMIT 청크로 분할 전송."""
    safe = mask_secrets(text, secrets)
    for chunk in chunk_text(safe, TELEGRAM_LIMIT):
        body = chunk if chunk else "(빈 응답)"
        try:
            tg_call(token, "sendMessage", {"chat_id": chat_id, "text": body}, timeout=30)
        except (
            urllib.error.URLError,
            OSError,
            json.JSONDecodeError,
            http.client.HTTPException,
        ) as e:
            log.warning("sendMessage 실패: %s", type(e).__name__)


# ══════════════════════════════════════════════════════════════════════════
# claude 실행
# ══════════════════════════════════════════════════════════════════════════
def _kill_tree(proc: subprocess.Popen[str]) -> None:
    # D1: Windows 에서는 부모가 살아있을 때 `taskkill /T` 로 자식 트리를 먼저 열거·종료해야
    # 손자 프로세스까지 정리된다(부모를 먼저 죽이면 트리를 열거 못 해 손자 잔존). 그 다음 kill 폴백.
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
    with contextlib.suppress(OSError):
        proc.kill()


def run_claude(claude_exe: str, project_path: str, task: str, timeout: int) -> dict[str, Any]:
    """claude -p headless 실행 후 JSON 결과 파싱. 실패는 is_error 구조로 정규화.

    보안(C-1): 사용자 task 는 argv 에 두지 않고 **stdin 으로만** 전달한다. Windows 에서
    `shutil.which("claude")` 는 배치 shim(claude.CMD)으로 해석돼 argv 가 cmd.exe 재파싱을
    거치므로, task 를 인자로 넘기면 큰따옴표+`&` 로 명령 인젝션(RCE)이 가능하다
    (shell=False·리스트 인자로도 못 막음). argv 엔 정적·신뢰 플래그만 남긴다.
    """
    cmd = [
        claude_exe,
        "-p",
        "--output-format",
        "json",
        "--model",
        "opus",
        "--permission-mode",
        "default",
        "--append-system-prompt",
        BRIDGE_SYSTEM_PROMPT,
        "--allowedTools",
        *ALLOWED_TOOLS,
    ]
    # ponytail: Windows 프로세스 그룹으로 자식 트리까지 정리(타임아웃 시 taskkill /T).
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=project_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
        )
    except OSError as e:
        return {"is_error": True, "result": f"claude 실행 불가: {type(e).__name__}"}

    try:
        out, err = proc.communicate(input=task, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        # D1: 손자가 파이프를 물고 있으면 무한정 communicate() 가 hang 하므로 짧은 타임아웃 부여.
        try:
            proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError) as e:
            log.warning("kill 후 파이프 정리 실패(%s) — 무시하고 진행", type(e).__name__)
        return {"is_error": True, "result": f"타임아웃({timeout}s) 초과 — 작업을 중단했습니다."}

    if not out.strip():
        detail = err.strip()[:500] or f"rc={proc.returncode}"
        return {"is_error": True, "result": f"claude 응답 없음. {detail}"}
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        return {"is_error": True, "result": out.strip()[:1000]}
    # JSON 이 dict 가 아니면(list/스칼라) format_reply 가 .get 에서 죽으므로 정규화.
    if not isinstance(parsed, dict):
        return {"is_error": True, "result": out.strip()[:1000]}
    data: dict[str, Any] = parsed
    return data


# 회신 헤더(처리 성공은 전부 동일, 실패만 구분). 확인 사항은 하위 섹션.
HEADER_DONE = "[ ✅처리완료 ]"
HEADER_FAIL = "[ ❌처리실패 ]"
HEADER_NOTE = "[ 📌추가 확인사항 ]"


def format_reply(data: dict[str, Any]) -> str:
    """claude JSON 결과 → 텔레그램 회신 텍스트(헤더 + 본문)."""
    result = str(data.get("result", "")).strip()
    header = HEADER_FAIL if data.get("is_error") else HEADER_DONE
    return f"{header}\n\n{result}" if result else header


# ══════════════════════════════════════════════════════════════════════════
# git push (승인 시에만)
# ══════════════════════════════════════════════════════════════════════════
def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def git_status_note(root: Path) -> str:
    """run_claude 성공 후 실제 git 상태로 커밋/푸시 안내 문구 생성.

    ahead = origin/main 보다 앞선 로컬 커밋 수, dirty = 미커밋 변경 유무.
    git 실패는 안전 폴백(각 0/없음)으로 처리해 브리지가 죽지 않게 한다.
    """
    try:
        r = _git(root, "rev-list", "--count", "origin/main..HEAD")
        ahead = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0
    except (OSError, ValueError):
        ahead = 0
    try:
        s = _git(root, "status", "--porcelain")
        dirty = bool(s.stdout.strip()) if s.returncode == 0 else False
    except OSError:
        dirty = False

    if ahead > 0:
        note = f"로컬 커밋 {ahead}개 대기 — 'push' 로 원격 반영하세요."
        if dirty:
            note += " (+ 미커밋 변경 있음)"
        return note
    if dirty:
        return "변경이 있으나 커밋되지 않았습니다(확인 필요)."
    return "변경 없음."


def do_push(root: Path) -> str:
    """모노레포 루트에서 pull --rebase → push. 충돌 시 rebase abort·미푸시."""
    pull = _git(root, "pull", "--rebase", "origin", "main")
    if pull.returncode != 0:
        _git(root, "rebase", "--abort")
        tail = (pull.stderr or pull.stdout).strip()[-500:]
        return f"{HEADER_FAIL}\n\npull --rebase 실패 — rebase abort, 미푸시.\n{tail}"
    push = _git(root, "push", "origin", "main")
    if push.returncode != 0:
        tail = (push.stderr or push.stdout).strip()[-500:]
        return f"{HEADER_FAIL}\n\npush 실패.\n{tail}"
    return f"{HEADER_DONE}\n\npull --rebase 후 push 성공 — 원격 main 에 반영됐습니다."


# ══════════════════════════════════════════════════════════════════════════
# 메시지 처리
# ══════════════════════════════════════════════════════════════════════════
HELP_TEXT = (
    "텔레그램 브리지 사용법\n"
    "• <프로젝트명> <작업지시> — 해당 프로젝트에서 Claude 작업 실행\n"
    "• /projects — 대상 프로젝트 목록\n"
    "• push — 로컬 커밋을 원격에 반영(pull --rebase 후 push)\n"
    "• /help — 이 도움말\n"
    "예) etf_info 오늘 데이터 정확도 로그 확인해줘"
)


def handle_update(
    upd: dict[str, Any],
    token: str,
    allowed: frozenset[int],
    claude_exe: str,
    repo_root: Path,
    target_root: str,
    timeout: int,
    secrets: list[str],
) -> None:
    # D6: edited_message 는 무시한다 — 원격 코드실행 브리지라, 이미 처리한 메시지를 편집하면
    # claude 작업이 재실행되는 것을 막기 위해 신규 message 만 트리거로 삼는다.
    msg = upd.get("message")
    if not isinstance(msg, dict):
        return
    chat = msg.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    if not isinstance(chat_id, int):
        return
    if not is_allowed(chat_id, allowed):
        log.warning("미허용 chat_id=%s 메시지 무시", chat_id)
        return

    text = msg.get("text")
    if not isinstance(text, str):
        send_message(token, chat_id, "텍스트 메시지만 처리합니다.", secrets)
        return
    stripped = text.strip()

    if stripped in ("/help", "/start") or (stripped.startswith("/") and stripped not in COMMANDS):
        log.info("chat=%s cmd=/help", chat_id)
        send_message(token, chat_id, HELP_TEXT, secrets)
        return
    if stripped == "/projects":
        names = list_projects(target_root)
        body = "대상 프로젝트\n" + ("\n".join(f"• {n}" for n in names) or "(없음)")
        log.info("chat=%s cmd=/projects count=%d", chat_id, len(names))
        send_message(token, chat_id, body, secrets)
        return
    if stripped == "/cancel":
        send_message(token, chat_id, "직렬 처리라 취소할 대기 작업이 없습니다.", secrets)
        return
    if stripped == "push":
        log.info("chat=%s cmd=push", chat_id)
        result = do_push(repo_root)
        send_message(token, chat_id, result, secrets)
        outcome = "완료" if result.startswith(HEADER_DONE) else "실패"
        log.info("chat=%s push 결과=%s", chat_id, outcome)
        return

    parsed = parse_message(text)
    if parsed is None:
        send_message(token, chat_id, f"형식을 이해하지 못했습니다.\n\n{HELP_TEXT}", secrets)
        return
    project, task = parsed
    proj_path = resolve_project(project, target_root)
    if proj_path is None:
        names = list_projects(target_root)
        body = f"'{project}' 프로젝트를 찾지 못했습니다.\n대상: " + (", ".join(names) or "(없음)")
        # 보안: 사용자 입력 project 를 %r 로 로깅해 개행 위조(로그 포깅)를 차단.
        log.warning("chat=%s 알수없는 프로젝트=%r", chat_id, project)
        send_message(token, chat_id, body, secrets)
        return

    log.info("chat=%s 실행 project=%s", chat_id, project)
    send_message(token, chat_id, f"[{project}] 작업 시작… 완료까지 대기하세요.", secrets)
    data = run_claude(claude_exe, proj_path, task, timeout)
    reply = format_reply(data)
    if not data.get("is_error"):
        try:
            reply += f"\n\n\n{HEADER_NOTE}\n\n{git_status_note(repo_root)}"
        except Exception as e:  # git 조회 실패로 회신이 막히지 않게(타입만 기록)
            log.warning("git_status_note 실패: %s", type(e).__name__)
    send_message(token, chat_id, reply, secrets)
    outcome = "error" if data.get("is_error") else "ok"
    log.info("chat=%s 완료 project=%s 결과=%s", chat_id, project, outcome)


# ══════════════════════════════════════════════════════════════════════════
# 메인 루프
# ══════════════════════════════════════════════════════════════════════════
def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    setup_logging()
    if sys.version_info < (3, 12, 3):
        log.error(
            "Python 3.12.3+ 필요(현재 %s). 종료.",
            ".".join(map(str, sys.version_info[:3])),
        )
        return 1
    env = load_env(PROJECT_DIR / ".env")
    token = env.get("TG_BOT_TOKEN", "").strip()
    allowed = parse_allowed(env.get("TG_ALLOWED_CHAT_IDS", ""))
    try:
        timeout = int(env.get("CLAUDE_TIMEOUT_SEC", "900"))
    except ValueError:
        timeout = 900
    target_root_rel = env.get("TARGET_ROOT", "Hachiware/_Project").strip()

    if not token:
        log.error(".env 에 TG_BOT_TOKEN 이 없습니다. .env.example 참고.")
        return 1
    if not allowed:
        log.error(".env 에 TG_ALLOWED_CHAT_IDS 가 없습니다(허용목록 필수). 종료.")
        return 1
    claude_exe = shutil.which("claude")
    if not claude_exe:
        log.error("claude CLI 를 PATH 에서 찾지 못했습니다.")
        return 1

    repo_root = find_repo_root(PROJECT_DIR)
    target_root = str((repo_root / target_root_rel).resolve())
    # 회신 마스킹 대상: 봇 토큰(필수) + 내부 절대경로(사용자명 노출 방지).
    secrets = [token, str(repo_root), str(Path.home())]

    if not acquire_lock(PID_FILE):
        log.error("다른 브리지 인스턴스가 실행 중입니다(pidfile). 종료.")
        return 1

    log.info("브리지 시작. target_root=%s allowed=%d개", target_root, len(allowed))
    offset = load_offset(OFFSET_FILE)
    try:
        while True:
            try:
                resp = get_updates(token, offset)
            except (
                urllib.error.URLError,
                OSError,
                json.JSONDecodeError,
                http.client.HTTPException,
            ) as e:
                log.warning("getUpdates 실패(%s) — 5초 후 재시도", type(e).__name__)
                time.sleep(5)
                continue
            if not resp.get("ok"):
                log.warning("getUpdates ok=false — 5초 후 재시도")
                time.sleep(5)
                continue
            for upd in resp.get("result", []):
                # D4: update_id 를 try 밖에서 먼저 추출해 정상 update 면 offset 을 선진행한다.
                # (handle_update 예외로 offset 이 안 막혀 같은 배치가 재수신되는 핫루프를 막는다.)
                uid = upd.get("update_id") if isinstance(upd, dict) else None
                if isinstance(uid, int):
                    offset = uid + 1
                try:
                    handle_update(
                        upd, token, allowed, claude_exe, repo_root, target_root, timeout, secrets
                    )
                except Exception as e:  # 한 메시지 오류로 루프가 죽지 않게(타입만 기록)
                    log.error("update 처리 중 예외: %s", type(e).__name__)
                try:
                    save_offset(OFFSET_FILE, offset)  # 포이즌 메시지 재처리 방지(진행)
                except OSError as e:
                    log.error("offset 저장 실패: %s", type(e).__name__)
    except KeyboardInterrupt:
        log.info("종료 요청(Ctrl+C).")
    finally:
        PID_FILE.unlink(missing_ok=True)
    return 0


def _selftest() -> None:
    """순수 함수 스모크(보안 경계 = resolve_project 트래버설 거부). qa 의 pytest 와 별개."""
    assert parse_message("etf_info 정확도 확인") == ("etf_info", "정확도 확인")
    assert parse_message("/help") is None
    assert parse_message("push") is None
    assert parse_message("solo") is None
    assert parse_message("   ") is None
    assert is_allowed(7, frozenset({7})) and not is_allowed(1, frozenset({7}))
    assert resolve_project("..", str(PROJECT_DIR)) is None
    assert resolve_project("../x", str(PROJECT_DIR)) is None
    assert resolve_project("a/b", str(PROJECT_DIR)) is None
    assert resolve_project("a\\b", str(PROJECT_DIR)) is None
    assert resolve_project("C:", str(PROJECT_DIR)) is None
    assert resolve_project("nope_missing", str(PROJECT_DIR)) is None
    assert resolve_project("logs", str(PROJECT_DIR)) == str(PROJECT_DIR / "logs")
    assert chunk_text("") == [""]
    assert chunk_text("abcd", 2) == ["ab", "cd"]
    assert mask_secrets("tok=SECRET here", ["SECRET"]) == "tok=*** here"
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        sys.exit(main())
