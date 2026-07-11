import json
from pathlib import Path
from llterm.ctl.queue import CtlQueue
from llterm.ctl.schema import CtlCommand


def _mk(tmp_path: Path) -> CtlQueue:
    return CtlQueue(tmp_path / ".llterm")


def test_submit_creates_queue_file(tmp_path: Path):
    q = _mk(tmp_path)
    cmd = CtlCommand(id="ctl-1", action="rotate", reason="r")
    q.submit(cmd)
    files = list((tmp_path / ".llterm" / "queue").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8"))["id"] == "ctl-1"


def test_submit_is_atomic_no_temp_leftover(tmp_path: Path):
    """submit はアトミック (temp→os.replace)。中間 .tmp を残さず最終 .json だけ残る。"""
    q = _mk(tmp_path)
    q.submit(CtlCommand(id="ctl-1", action="rotate", reason="r"))
    qdir = tmp_path / ".llterm" / "queue"
    assert len(list(qdir.glob("*.json"))) == 1
    assert not list(qdir.glob("*.tmp"))  # 中間ファイルが残らない


def test_poll_transient_oserror_is_not_quarantined(tmp_path: Path, monkeypatch) -> None:
    """読取中の一時的 OSError (共有違反等) は隔離せず、正当タスクを queue に残す (次 tick 再試行)。"""
    q = _mk(tmp_path)
    q.submit(CtlCommand(id="ctl-x", action="rotate", reason="r"))

    def _flaky_read(self: Path, *a: object, **k: object) -> str:
        raise OSError("transiently locked")

    monkeypatch.setattr(Path, "read_text", _flaky_read)
    assert q.poll() is None  # 読めない → 取り出さない
    assert list((tmp_path / ".llterm" / "queue").glob("*ctl-x*"))  # queue に残る
    assert not list((tmp_path / ".llterm" / "rejected").glob("*"))  # 隔離されない


def test_recover_inflight_requeues_unfinished(tmp_path: Path):
    """poll→finish 間クラッシュで inflight に残った未完了タスクを queue へ戻し再処理可能にする。"""
    q = _mk(tmp_path)
    q.submit(CtlCommand(id="ctl-a", action="rotate", reason="r"))
    got = q.poll()  # → inflight へ移動
    assert got is not None and got.id == "ctl-a"
    assert not list((tmp_path / ".llterm" / "queue").glob("*.json"))  # queue は空
    assert q.recover_inflight() == 1
    assert not list((tmp_path / ".llterm" / "inflight").glob("*.json"))  # inflight 掃けた
    assert list((tmp_path / ".llterm" / "queue").glob("*ctl-a*"))       # queue へ戻った
    again = q.poll()
    assert again is not None and again.id == "ctl-a"                    # 再処理できる


def test_recover_inflight_skips_completed(tmp_path: Path):
    """results に完了記録がある残骸は戻さず掃除する (二重実行の防止)。"""
    q = _mk(tmp_path)
    q.submit(CtlCommand(id="ctl-b", action="rotate", reason="r"))
    q.poll()  # → inflight/<seq>-ctl-b.json
    # finish の results 書込みだけ済んで inflight unlink 前に落ちた状態を模擬
    (tmp_path / ".llterm" / "results" / "ctl-b.json").write_text(
        '{"id":"ctl-b","ok":true}', encoding="utf-8")
    assert q.recover_inflight() == 0                                    # 完了済み → 戻さない
    assert not list((tmp_path / ".llterm" / "inflight").glob("*.json"))  # 残骸を掃除
    assert not list((tmp_path / ".llterm" / "queue").glob("*.json"))


def test_prune_results_caps_growth(tmp_path: Path):
    """results/ は新しい keep 件に保たれる (長時間運用の無制限成長を防ぐ)。"""
    q = _mk(tmp_path)
    for i in range(8):
        q.submit(CtlCommand(id=f"ctl-{i}", action="rotate", reason="r"))
        cmd = q.poll()
        assert cmd is not None
        q.finish(cmd, ok=True, result="x")
    q._prune_results(keep=3)
    assert len(list((tmp_path / ".llterm" / "results").glob("*.json"))) == 3


def test_poll_consumes_in_order_and_moves_to_inflight(tmp_path: Path):
    q = _mk(tmp_path)
    q.submit(CtlCommand(id="ctl-a", action="query-state", reason="r"))
    q.submit(CtlCommand(id="ctl-b", action="rotate", reason="r"))
    got = q.poll()
    assert got is not None and got.id == "ctl-a"          # FIFO
    # ファイル名は <seq>-ctl-a.json (連番 prefix) なので glob は *ctl-a* で照合する
    assert not list((tmp_path / ".llterm" / "queue").glob("*ctl-a*"))
    assert list((tmp_path / ".llterm" / "inflight").glob("*ctl-a*"))


def test_poll_empty_returns_none(tmp_path: Path):
    assert _mk(tmp_path).poll() is None


def test_poll_skips_broken_json_and_quarantines(tmp_path: Path):
    q = _mk(tmp_path)
    qdir = tmp_path / ".llterm" / "queue"
    qdir.mkdir(parents=True)
    (qdir / "0000-bad.json").write_text("{broken", encoding="utf-8")
    q.submit(CtlCommand(id="ctl-ok", action="rotate", reason="r"))
    got = q.poll()
    assert got is not None and got.id == "ctl-ok"          # 壊れた方は飛ばす
    assert list((tmp_path / ".llterm" / "rejected").glob("*bad*"))  # 隔離される


def test_write_result_and_finish(tmp_path: Path):
    q = _mk(tmp_path)
    q.submit(CtlCommand(id="ctl-1", action="query-state", reason="r"))
    cmd = q.poll()
    q.finish(cmd, ok=True, result={"state": "alive"})
    res_files = list((tmp_path / ".llterm" / "results").glob("ctl-1*.json"))
    assert len(res_files) == 1
    rec = json.loads(res_files[0].read_text(encoding="utf-8"))
    assert rec["ok"] is True and rec["result"]["state"] == "alive"
    assert not list((tmp_path / ".llterm" / "inflight").glob("ctl-1*"))


def test_duplicate_id_rejected_on_submit(tmp_path: Path):
    q = _mk(tmp_path)
    q.submit(CtlCommand(id="ctl-1", action="rotate", reason="r"))
    import pytest
    with pytest.raises(FileExistsError):
        q.submit(CtlCommand(id="ctl-1", action="rotate", reason="r"))


def test_quarantine_recorded_in_ledger(tmp_path: Path):
    # レビュー finding (high/fail-closed): quarantine が ledger に痕跡ゼロだった
    from llterm.ctl.ledger import Ledger
    led_path = tmp_path / ".llterm" / "ledger.jsonl"
    q = CtlQueue(tmp_path / ".llterm", ledger=Ledger(led_path))
    qdir = tmp_path / ".llterm" / "queue"
    qdir.mkdir(parents=True)
    (qdir / "0000-evil.json").write_text("{broken", encoding="utf-8")
    assert q.poll() is None
    led = led_path.read_text(encoding="utf-8")
    assert '"quarantined"' in led
    assert "0000-evil" in led


def test_poll_survives_unreadable_entry(tmp_path: Path):
    # レビュー finding (medium): UnicodeDecodeError 等で tick ごと死んでいた
    q = _mk(tmp_path)
    qdir = tmp_path / ".llterm" / "queue"
    qdir.mkdir(parents=True)
    (qdir / "0000-binary.json").write_bytes(b"\xff\xfe\x00\x80broken")
    q.submit(CtlCommand(id="ctl-ok2", action="rotate", reason="r"))
    got = q.poll()                                    # 例外で死なず次へ進む
    assert got is not None and got.id == "ctl-ok2"
    assert list((tmp_path / ".llterm" / "rejected").glob("*binary*"))


def test_quarantine_name_collision_gets_unique_suffix(tmp_path: Path):
    # rejected/ に同名既存でも上書き・例外なしで隔離できる
    q = _mk(tmp_path)
    qdir = tmp_path / ".llterm" / "queue"
    rej = tmp_path / ".llterm" / "rejected"
    qdir.mkdir(parents=True)
    rej.mkdir(parents=True)
    (rej / "0000-bad.json").write_text("earlier", encoding="utf-8")
    (qdir / "0000-bad.json").write_text("{broken", encoding="utf-8")
    assert q.poll() is None
    assert len(list(rej.glob("0000-bad*"))) == 2      # 両方残る (消さない)
