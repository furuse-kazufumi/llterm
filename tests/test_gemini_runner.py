# SPDX-License-Identifier: Apache-2.0
"""GeminiRunner (Gemini CLI agentic 奏者) の回帰テスト。

実 gemini を呼ばず、偽の JSON を吐く python 子プロセスを注入して検証する (課金/通信ゼロ)。
parse_gemini_json は gemini --output-format json の単一オブジェクト形に準拠。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from llterm.host.gemini_runner import (
    GeminiRunner,
    _extract_tokens,
    parse_gemini_json,
    summarize_gemini_event,
)


# ─── parse_gemini_json (純関数) ──────────────────────────────────


def test_parse_success_extracts_response() -> None:
    stdout = json.dumps({"response": "答えは 42", "stats": {"input_tokens": 30, "output_tokens": 5}})
    r = parse_gemini_json(stdout, exit_code=0)
    assert r.text == "答えは 42"
    assert r.is_error is False
    assert r.input_tokens == 30
    assert r.output_tokens == 5
    assert r.context_tokens == 35
    assert r.cost_usd == 0.0


def test_parse_error_on_nonzero_exit() -> None:
    r = parse_gemini_json('{"response": ""}', exit_code=1, stderr="boom")
    assert r.is_error is True
    assert r.error_kind == "other"


def test_parse_error_key_sets_error() -> None:
    stdout = json.dumps({"response": "", "error": {"message": "something failed"}})
    r = parse_gemini_json(stdout, exit_code=0)
    assert r.is_error is True


def test_parse_rate_limit_from_error() -> None:
    stdout = json.dumps({"error": {"message": "quota exceeded, try later"}})
    r = parse_gemini_json(stdout, exit_code=1)
    assert r.error_kind == "rate_limited"


def test_parse_auth_error() -> None:
    stdout = json.dumps({"error": {"message": "request had invalid authentication credentials"}})
    r = parse_gemini_json(stdout, exit_code=1)
    assert r.error_kind == "auth"


def test_parse_malformed_json_is_error() -> None:
    r = parse_gemini_json("not json{", exit_code=0)
    assert r.is_error is True
    assert r.text == ""


def test_extract_tokens_nested_shape() -> None:
    stats = {"models": {"gemini-2.5-flash": {"tokens": {"prompt": 11, "candidates": 7}}}}
    assert _extract_tokens(stats) == (11, 7)


def test_extract_tokens_missing_is_zero() -> None:
    assert _extract_tokens({}) == (0, 0)
    assert _extract_tokens(None) == (0, 0)


def test_summarize_response_event() -> None:
    assert summarize_gemini_event({"type": "response", "text": "hi"}) == [{"kind": "text", "text": "hi"}]
    assert summarize_gemini_event({"type": "init"}) == []


# ─── _build_args ─────────────────────────────────────────────────


def test_build_args_no_prompt_in_argv_uses_stdin() -> None:
    """プロンプトは argv に載せない (stdin 渡し)。json/yolo フラグが付く。"""
    args = GeminiRunner(model="gemini-2.5-flash")._build_args()
    assert "--output-format" in args
    assert args[args.index("--output-format") + 1] == "json"
    assert "--yolo" in args
    assert args[args.index("-m") + 1] == "gemini-2.5-flash"
    # プロンプト本文や -p は argv に無い
    assert "-p" not in args


def test_build_args_omits_model_when_empty() -> None:
    args = GeminiRunner()._build_args()
    assert "-m" not in args  # 空 = gemini 既定モデルに委ねる


def test_build_args_yolo_can_be_disabled() -> None:
    args = GeminiRunner(yolo=False)._build_args()
    assert "--yolo" not in args


# ─── GeminiRunner (偽の子プロセスで実走・課金ゼロ) ───────────────


_ECHO_STDIN_GEMINI = '''\
import json, sys
# gemini は piped stdin をプロンプトとして読む。受け取った全文を response に echo する
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
data = sys.stdin.read()
print(json.dumps({"response": data, "stats": {"input_tokens": 1, "output_tokens": 1}},
                 ensure_ascii=False))
'''


def _scripted_gemini(tmp_path: Path, body: str = _ECHO_STDIN_GEMINI) -> GeminiRunner:
    script = tmp_path / "fake_gemini.py"
    script.write_text(body, encoding="utf-8")

    class Scripted(GeminiRunner):
        def _build_args(self) -> list[str]:
            return [sys.executable, str(script)]

    return Scripted()


def test_multiline_prompt_reaches_child_intact_via_stdin(tmp_path: Path) -> None:
    """複数行プロンプトが改行後も全文 gemini 子へ届く (argv truncation の回帰防止)。"""
    runner = _scripted_gemini(tmp_path)
    prompt = "セッション再開。\n1) これをやる\n2) docs/REPORT.md に書く\n確認は求めない。"
    res = runner.run_turn(prompt=prompt, session_id="sid-1", resume=False, cwd=tmp_path)
    assert res.is_error is False
    assert res.text.rstrip("\n") == prompt   # 改行後も全文届く
    assert "docs/REPORT.md" in res.text
    assert res.session_id == "sid-1"          # loop の uuid を保持して返す


def test_cancel_before_start_is_sticky(tmp_path: Path) -> None:
    runner = _scripted_gemini(tmp_path)
    runner.cancel()
    t0 = time.monotonic()
    res = runner.run_turn(prompt="p", session_id="s", resume=False, cwd=tmp_path)
    assert res.error_kind == "cancelled"
    assert time.monotonic() - t0 < 2.0  # 子を spawn していない
