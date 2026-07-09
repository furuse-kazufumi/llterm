# llterm Session Summary — 2026-07-10 (EXIT 準備 / shared progress hardening 収束点)

> 自動生成・追記運用メモ: 次回開始時はまず `docs/next_plan.md` を正本として読み、ここは直近セッションの
> 補助文脈として使うこと。`_shared/PROGRESS.md` は派生ビューなので、差異があれば `next_plan` を優先する。

## 2026-07-10 EXIT 準備まとめ
- **snapshot recency / stale worker queued signal / denylist 漏れを補修**:
  `src/llterm/progress.py` は snapshot の stale 判定に project 名を使わず、
  `(updated, mtime)` 専用キー `_snapshot_recency_key()` で freshness を比べるよう変更した。
  `src/llterm/gui/app.py` の `_on_stream()` / `_on_event()` は current worker 以外の queued signal
  を無視し、sender が `LoopWorker` なら retire へ送る。
  `src/llterm/host/loop.py` の query denylist には `整理` / `整える` / `統合` /
  `並べ替え` / `並び替え` / `移す` を追加した。`.gitignore` には `.tmp_pytest_*.txt` を追加。
  追試は `tests/test_progress.py` targeted = 3 passed、`tests/test_gui.py` targeted = 4 passed、
  `tests/test_loop.py -k "query_like_injection_detector_is_conservative"` = 1 passed、
  host/progress 横断 = 292 passed。
- **未決の残余**:
  rotate handoff 中の `interrupted` を fail-closed 停止のまま許容するか、次ターン消費へ戻すかは未決。
  いまはコード変更せず残余として維持。
  新セッションの最優先は、`tests/test_gui.py` 全量で残る Windows teardown access violation の最小再現化と、
  stale worker signal ガード追加後の `LoopWorker` 寿命管理 (`_retire_worker` / `deleteLater` / cleanup fixture) の切り分け。
- **rotate handoff の一時 rate limit を 1 回救済**:
  `src/llterm/host/loop.py` の rotate 分岐は、handoff が `rate_limited` を返したときだけ
  `_wait_until(resets_at)` 後に同一 provider / 同一 session で 1 回 retry するよう変更した。
  待機中 stop 要求なら `"stopped"`、retry 後も失敗なら `exit_prep_failed` で fail-closed 停止。
  `tests/test_loop.py` に success / retry failure / wait 中 stop の 3 本を追加し、
  `tests/test_loop.py = 112 passed` を確認。
- **CLI `--projects-root` を追加**:
  `main()` はこれまで `DEFAULT_PROJECTS_ROOT` を固定注入していたが、いまは `--projects-root`
  明示指定を優先し、未指定時は `workdir.parent` を projects root 候補に使う。
  `tests/test_loop.py` で explicit override と parent fallback を固定した。
- **GUI 全量の Windows teardown crash は追跡残し**:
  `tests/test_gui.py` 全量は今回も Windows teardown access violation で安定完走していない。
  直近安定値は 447 passed のまま、今回の再確認は smoke 2 pass に留まることを明示する。
- **rotate 前 handoff failure は fail-closed 停止へ変更**:
  `src/llterm/host/loop.py` の rotate 分岐は、`_handoff_run_turn()` が失敗した場合に
  そのまま `stop_reason="exit_prep_failed"` で停止するよう変更した。
  これで handoff 未更新のまま fresh session へ進んで context を捨てる窓を閉じた。
  `tests/test_loop.py::test_rotate_does_not_refresh_shared_progress_when_exit_prep_fails` は
  `max_sessions=2` へ広げ、handoff failure 後に `runner.calls == 2` のまま止まることを固定。
  追試は `tests/test_loop.py -k "rotate_does_not_refresh_shared_progress_when_exit_prep_fails or rotate_then_stop_records_handoff or graceful_stop_does_not_refresh_shared_progress_when_handoff_fails" = 3 passed`。
- **common summary stale-guard を sidecar metadata へ移動**:
  `src/llterm/progress.py` の same-minute stale-guard は index 行末の marker を廃止し、
  `._shared/PROGRESS.md.meta.json` の snapshot fingerprint 比較へ切り替えた。
  これで GUI `All` タブ / durable `PROGRESS.md` から `<!-- ... -->` が消え、
  stale 判定も先頭 1 project ではなく snapshot 全体の `(name, updated, mtime)` 配列で比較できる。
  可視本文だけ stale で meta が同一 snapshot のケースは安全に再 commit し、
  別 snapshot の same-minute readback には再 commit しない。
- **GUI 側の sort / writer start 失敗も補正**:
  `src/llterm/gui/app.py` の common project tab は `(updated, mtime)` sort に揃えた。
  background common writer は `start()` 失敗時も `_common_summary_write_active=False` /
  `_common_summary_writer_thread=None` に戻し、ランタイム中の durable write ジャムを防ぐ。
- **今回の検証**:
  `py -3.11 -m pytest -q tests/test_progress.py -k "build_common_summary_breaks_same_minute_ties_with_mtime or write_common_summary_items_rewrites_when_lower_project_only_changes_same_minute or write_common_summary_items_rewrites_older_same_minute_snapshot_using_mtime_tiebreak or write_common_summary_items_does_not_clobber_same_minute_readback_mismatch"` = **4 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py -k "summary_has_live_and_common_tabs or common_summary_writer_start_failure_resets_active or refresh_common_summary_collects_once_and_writes_same_snapshot or common_summary_writer_coalesces_pending_snapshots"` = **4 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **447 passed**。
- **query denylist の活用形補完**: `src/llterm/host/loop.py` の `_QUERY_INJECTION_NEGATIVE`
  に `作り直` / `直す` / `直し` / `消す` / `消し` / `書き換` / `やり直` を追加した。
  これで `進捗ファイルを作り直す` や `不要な項目を消す` のような口語 mutation でも
  fast-path を通らず full review 側へ倒れる。
- **same-minute sort の第二キー追加**: `src/llterm/progress.py` の `build_common_summary()` は
  sort key を `(updated, mtime)` に広げた。hidden marker の `(updated, mtime)` 比較だけでなく、
  index 先頭の代表 project 自体も same-minute で `mtime` 最新が来るよう補正した。
- **provider cleanup の補足修正**:
  `src/llterm/host/gemini_runner.py` は timeout 後の `_kill(proc)` のあとに
  `proc.wait(timeout=10)` を追加して bounded reap を保証。
  `src/llterm/host/codex_runner.py` の version probe は `stdin=subprocess.DEVNULL` を明示し、
  親 stdin を継承しないようにした。
- **今回の検証**:
  `py -3.11 -m pytest -q tests/test_loop.py -k "query_like_injection_detector_is_conservative"` = **1 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py -k "build_common_summary_breaks_same_minute_ties_with_mtime or write_common_summary_items_rewrites_older_same_minute_snapshot_using_mtime_tiebreak or write_common_summary_items_does_not_clobber_same_minute_readback_mismatch"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gemini_runner.py -k "gemini_timeout_returns_visible_reason or gemini_timeout_waits_again_after_kill"` = **2 passed**。
  `py -3.11 -m pytest -q tests/test_codex_runner.py -k "provider_version_probe_uses_devnull_stdin or provider_version_transient_failure_is_not_cached or provider_version_not_found_is_cached_once"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **445 passed**。
- **query fast-path を単一問い合わせ節へ保守化**: `src/llterm/host/loop.py` の
  `_QUERY_INJECTION_SEQUENCE_MARKERS` に `and` / `、` / `，` / `および` / `と` / `また`
  を追加し、`status summary and review the diff` や `現状をまとめて、バグも直して`
  のような複合要求は full review 側へ倒すようにした。fast-path は最適化にすぎず、
  曖昧な複合節を review 付きへ倒すほうを正と明記。
- **same-minute 共通進捗 tie-break を追加**: `src/llterm/progress.py` の共通進捗インデックス行へ
  hidden marker `<!-- updated=... mtime=... -->` を埋め、readback 比較を `(updated, mtime)` の
  tuple へ引き上げた。表示時刻は従来どおり分単位のまま、同じ分の手書き時刻でも
  source file `mtime` が新しい snapshot を stale readback 補正で優先できる。
- **低 nit の補足**:
  `src/llterm/host/codex_runner.py` の provider version probe 例外後に `wait()` を追加し、
  `src/llterm/gui/app.py` の `_drain_common_summary_write()` docstring は
  「close 終端だけ GUI スレッド同期 flush を許す例外」と明記した。
  `src/llterm/host/orchestra_runner.py` の `id(runner)` key には、runner が強参照される前提コメントを足した。
- **今回の検証**:
  `py -3.11 -m pytest -q tests/test_loop.py -k "query_like_injection_detector_is_conservative"` = **1 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py -k "write_common_summary_items_does_not_clobber_same_minute_readback_mismatch or write_common_summary_items_rewrites_older_same_minute_snapshot_using_mtime_tiebreak"` = **2 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py -k "summary_has_live_and_common_tabs or refresh_common_summary_collects_once_and_writes_same_snapshot or common_summary_writer_coalesces_pending_snapshots or close_drains_common_summary_writer_latest_pending or common_summary_writer_drain_timeout_is_bounded or common_summary_writer_recovers_after_write_exception or common_summary_writer_does_not_lose_pending_schedule_at_exit_boundary or common_summary_writer_finally_does_not_clobber_new_worker_registration"` = **8 passed**。
  `py -3.11 -m pytest -q tests/test_codex_runner.py -k "provider_version_not_found_is_cached_once or provider_version_transient_failure_is_not_cached or interrupt_during_probe_does_not_poison_provider_version_cache"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_loop.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_orchestra_runner.py -k "query_like or common_summary or provider_version or timeout_returns_visible_reason or idle_interrupt_does_not_poison_next_turn"` = **29 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **442 passed**。
- **GUI 共通タブの single-snapshot 化**: `src/llterm/gui/app.py` の `_refresh_common_summary()` は、
  `collect_progress()` を 1 回だけ呼んだ snapshot から All / project 別タブを描くようにした。
  `_shared/PROGRESS.md` への反映は `write_common_summary_items()` を使って同じ snapshot を別スレッドで commit する。
  これで GUI 内の projection 同士のズレと、GUI スレッド上の fsync を同時に避ける。
- **GUI common writer の coalescing**: `_schedule_common_summary_write()` は単一 worker + latest pending 方式へ変更した。
  refresh 連打時は古い中間 snapshot を捨てて最新 pending だけを書き、daemon thread を増やし続けない。
- **GUI close 時の writer drain + tooltip reset**: `closeEvent()` は background common writer を
  **2.0s bounded / best-effort** で drain してから閉じる。
  `session_start` では token 表示 text だけでなく tooltip も reset し、前セッションの `provider_version` を残さない。
- **GUI common writer の例外復旧 + daemon 回帰**: `_writer()` は write 例外時も active/thread フラグを必ず復旧する。
  writer thread は `daemon=True` に戻し、close 時の明示 drain を残したまま shutdown の bounded 性を優先した。
- **GUI common writer の lost-wakeup race 修正**: `pending is None` 分岐で active/thread の reset までを同じロック内へ移した。
  schedule が終了分岐へ割り込んでも pending が worker 不在で宙吊りにならないようにした。
- **横断回帰の再確認**: `tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py`
  をまとめて再実行し、411 passed / ruff clean。GUI writer hardening が host/provider 契約へ波及していないことを確認した。
- **追加修正 3 件**:
  1. `CodexRunner.run_turn()` の proc 起動直後 kill 判定に `_interrupted` を含め、post-spawn interrupt を cancel と対称化。
  2. common summary writer の `finally` reset は「自分が現登録 thread のときだけ」に限定し、後着 worker の registration clobber を防止。
  3. `is_query_like_injection()` の否定語に `書いて/追記` などの日本語 mutation 動詞を追加し、
     `status/progress/summary` は語境界寄りに締めて review gate の false-negative を減らした。
- **ClaudeRunner interrupt 窓も対称化**: `src/llterm/host/loop.py` の `ClaudeRunner.run_turn()` は
  `Popen` 後 `_proc` 登録直後の kill 判定が `cancel` のみだったため、`interrupt()` がその窓へ入ると
  現ターン即中断契約を破り得た。post-spawn 判定を `cancel or interrupt` へ広げ、interrupt なら
  即 kill + `error_kind="interrupted"` を返すよう修正した。`tests/test_loop.py` に
  spawn 完了同期つき回帰を追加し、`tests/test_loop.py = 106 passed` を確認した。
- **post-spawn interrupt fast-path の cleanup も補強**: `ClaudeRunner` / `CodexRunner` とも、
  post-spawn interrupt 即返しが `try/finally` より前だったため `_proc` 参照と child handle が残り得た。
  両 runner に `proc.wait(timeout=10)` と `if self._proc is proc: self._proc = None` を追加し、
  `_interrupted` 消費条件も `interrupt and not cancel` へ揃えた。回帰は `Popen` hook 内 interrupt へ
  締め直し、window-specific に `runner._proc is None` まで固定した。
- **GeminiRunner も同じ停止意味論へ揃えた**: `src/llterm/host/gemini_runner.py` は
  `interrupt()` を持つのに post-spawn 判定が旧来の `cancel` のみだったため、
  `Claude` / `Codex` と同型の窓が残っていた。`kill_now or interrupt_now` 判定、
  `interrupt and not cancel` の一発消費、bounded `wait()`、`_proc=None` cleanup を追加し、
  `tests/test_gemini_runner.py` に window-specific 回帰を足した。横断セットは
  `tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py = 436 passed`。
- **stale `_interrupted` の開始時リセットも 3 runner に追加**: `orchestra_runner` は元から
  ターン開始時に `_interrupted=False` を入れていたが、`Claude` / `Codex` / `Gemini` には無く、
  idle 中に `worker.request_interrupt()` で立った flag が fallback runner の次ターンを
  無関係に `interrupted` で潰し得た。3 runner とも `cancel` pre-check 直後に stale `_interrupted`
  をリセットし、「走行中に届いた interrupt だけ」を拾うよう修正した。`worker.py` の docstring も
  実態に合わせて補正。回帰は各 runner の idle interrupt ケースを追加し、横断セットは
  `tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py = 439 passed`。
- **Gemini timeout 文言も補完**: `GeminiRunner` の timeout は `err=other` だが理由文が空だったため、
  `runner.gemini.timeout` の i18n キーを追加して `src/llterm/host/gemini_runner.py` から返すようにした。
  `tests/test_gemini_runner.py` に watchdog timeout 時でも `res.text` が空でない回帰を追加し、
  横断セットは `tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py = 440 passed`。
- **Gemini timeout 回帰を文言一致に締め直し**: `tests/test_gemini_runner.py::test_gemini_timeout_returns_visible_reason`
  は前回まで非空しか見ていなかったため、`t("runner.gemini.timeout")` との一致へ差し替えた。
  実装ロジックの変更はなし。これで timeout 専用文言以外の別メッセージ混入では緑にならない。
- **provider_version 例外 cache を非対称解消**: `src/llterm/host/codex_runner.py` の `_provider_version()` は
  これまで Popen 例外を種別問わず空文字で cache していたが、今は `FileNotFoundError` だけを恒久欠落として cache し、
  一過性 `OSError` / `SubprocessError` は cache しない。`tests/test_codex_runner.py` では
  `not_found_is_cached_once` と `transient_failure_is_not_cached` の 2 本に分けて固定し、
  横断セットは `tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py = 441 passed`。
- **writer drain / handoff 前提の文言補正**: common summary writer の close 時 flush は
  `2.0s bounded / best-effort` と明記し、この summary / `docs/next_plan.md` は current working tree の
  `src/` + `tests/` + `docs/` 差分を前提とする旨も追記した。
- **handoff 前提の明示**: 本 summary / `docs/next_plan.md` は current working tree の `src/` + `tests/` +
  `docs/` 差分を前提にしており、docs だけ独立確定した状態は正としない。最新の横断確認値は
  `tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py = 447 passed`。
- **provider_version cache 調整**: `src/llterm/host/codex_runner.py` の `_provider_version()` は、
  `cancel()` / `interrupt()` で probe が止まった結果の空文字を cache しないようにした。
  停止起因の欠測は次ターンで再観測可能に保ち、正常系では provenance が復帰する。
- **非 JSON 診断の結合方法を固定**: 失敗時の非 JSON 診断行は、既存の error text があればその後ろへ
  改行追記する。成功時は引き続き非表示。これで情報損失と表示順の揺れを避ける。
- **非 JSON 診断行の扱いを調整**: `src/llterm/host/codex_runner.py` の `parse_codex_jsonl()` は、
  非 JSON 行を成功時は無視したまま、失敗時だけ補助診断テキストへ昇格するようにした。
  これで `error/turn.failed` 無しの `err=other` でも原因手掛かりを GUI / ledger に残せる。
- **Codex version probe race 修正**: `src/llterm/host/codex_runner.py` の `provider_version` 取得は
  `_probe_proc` で追跡する kill 可能 subprocess へ変えた。`cancel()` / `interrupt()` は
  main turn だけでなく version probe も kill し、pre-start / probe 後の両方で
  `cancelled` / `interrupted` を返せるようにした。停止要求が勝った場合の `provider_version` は空文字に落とす。
- **Codex cancel fast-path 修正**: `src/llterm/host/codex_runner.py` の `run_turn()` は、
  `cancel()` 済みなら `codex --version` を呼ばず即 `cancelled` を返すよう戻した。
  provenance より停止要求を優先し、pre-start cancel だけは `provider_version=""` のまま返す。
- **shared progress 書込み hardening**: `src/llterm/progress.py` の `write_common_summary()` は
  いま `tmp write -> replace -> parent dir fsync` の `_commit_summary_text()` 経路へ統一済み。
  直接 `PROGRESS.md` を `"w"` truncate する fallback は撤去した。
- **stale 再現確認の追試**: 実ファイル `D:/projects/_shared/PROGRESS.md` は
  `py -3.11 -m llterm.progress --projects-root D:/projects` 実行前後で Python read / PowerShell read が一致。
  さらに隔離 `projects_root` 上で `next_plan` の `最終更新` を 6 回進めながら毎回別プロセス CLI 実行直後に
  `Get-Content` を読んでも、6/6 回とも最新値が返り stale は再現しなかった。
- **stale readback 補正の現状**:
  - readback mismatch 時は、より新しい並行 writer を潰さないよう fail-closed を維持。
  - ただし単独 writer の stale 残留を潰すため、
    1. 現在ファイルの最新時刻が期待 summary より古い、または
    2. 最新時刻は同じでも、**現在の source 群から再集約した本文**が期待値と一致する
    場合に限って、同じ commit 経路を 1 回だけ再試行する。
- **durability 補強**: `_write_text_sync()` で file `flush + fsync`、さらに rename 後に
  `_fsync_parent_dir()` を best-effort 実行する形へ揃えた。
- **テスト到達点**: `py -3.11 -m pytest -q tests/test_progress.py` = **89 passed**、
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **Codex probe 追記**: `codex-cli 0.135.0` の `codex exec --json` 再probeでも
  `thread.started / turn.started / item.completed / turn.completed.usage` 以外の公開 event は出ず、
  per-call / span の瞬間占有は未観測のまま。stdout 末尾に非 JSON 診断行
  `Reading additional input from stdin...` が混ざるケースを確認したため、parser が黙って無視できる回帰を追加した。
- **実ファイル状態**: `docs/next_plan.md` と `D:/projects/_shared/PROGRESS.md` は
  **同じ記録時刻の `llterm` を先頭にそろえる**運用へ戻した。`next_plan` の `> 最終更新` を更新したら、
  共通進捗もその時刻で再生成して整合を保つ。

## 次の具体的一手
1. `progress.py` 系は**再現不能のまま追加 hardening しない**。新しい stale 実例が出た時だけ、
   `CLI 実行経路 / shell 側 readback / キャッシュ` のどこでずれるかを追加観測する。
2. `Codex` の per-call 占有は引き続き **未公開 schema** として扱い、今後も `ctx n/a` を維持する。
   ただし `codex --json` の event schema や `usage` field が将来増えたら再probeして取り込む。
3. 実測済みの非 JSON 診断行 (`Reading additional input from stdin...`) は parser が黙って無視できる前提でよい。
   以後の probe で別の診断ノイズが出たら同じく回帰へ固定する。

---

# llterm Session Summary — 2026-06-12 (i18n 実装 + GUI 大幅強化)

## 2026-06-12 (夜) 多言語対応 (i18n) 実装完了
- **`src/llterm/i18n/`** 新設: 軽量テーブル方式 (gettext 不使用)。`messages.py` =
  `MESSAGES: dict[key, {ja/en}]` (約 90 key、zh/ko は後から追加可能な構造)。
  `t(key, **kwargs)` = locale 解決 (`LLTERM_LANG` → OS locale → 既定 ja) + fail-safe
  (未知 key→key / 訳欠落→ja fallback / format 失敗→未整形。例外を絶対に外へ出さない)。
- **置換**: GUI 全文字列 40+ (ラベル/tooltip/ダイアログ/出力メッセージ/cost種別) /
  CLI エラー (emit 英語・loop 日本語の混在を統一) / テンプレ registry label・description
  (property 化でアクセス時 locale 解決) / 仮想 claude 表示 3 件。ja はバイト同一維持。
- **対象外と判断**: ループ指示プロンプト (DEFAULT_RESUME_PROMPT 等・テンプレ builder の
  resume/continue prompt・rad expand prompt) = Claude への指示文でありユーザー向け表示で
  ないため。表示 locale でエージェント挙動を変えない (README の i18n 節に明記)。
- **README**: README.en.md 完全英訳新設 + 相互言語リンク + i18n 節 (両言語)。
- **テスト**: 186 → **207 passed** (i18n 19 + templates locale 2 追加)、ruff clean。
  conftest で `LLTERM_LANG=ja` をプロセス固定 (UI 文字列 assert の安定化)。
- **残課題**: zh/ko 訳の追加 / GUI 内の言語切替 UI (現状は env 変数のみ) /
  argparse --help 文の i18n / `llterm-loop` outcome ブロック (機械可読のため据置)。

---

# (前回) llterm Session Summary — 2026-06-12 (GUI 大幅強化セッション)

## 2026-06-12 セッション総括 (この日の全コミット)
GUI を一気に実用レベルへ。コミット順:
1. **streaming+color** — claude 応答のリアルタイム表示 + One Dark セマンティックカラー(下記詳細)。
2. **多視点レビュー修正 17 件** — cancel 消失レース / CREATE_NO_WINDOW / auth 誤分類限定 /
   subagent 区別 / CR 正規化 / npm shim fail-closed 等。
3. **設定永続化** — `~/.llterm/gui_settings.json`(project/effort/threshold/window/cost/template/
   geometry/codex_fallback…)。型不正でも fail-safe で既定起動。
4. **effort 選択** — `--effort low..max`(既定 max)。/effort はタスク注入では効かない・ultracode は
   vanilla claude に無い(max が最上位)を実機確認。
5. **注入タスク可視化 + session 進捗 + 設定 fail-safe** — `▶ 注入タスク実行`、`session N/max`、
   ctx バーに rotate 閾値併記。
6. **タイムスタンプ** — 指令時/応答受信時/境界に `[HH:MM:SS]`。
7. **cost 課金有無の明示 + model 表示** — `cost(報告値・課金なし)` / API キー時のみ赤字 `実課金` /
   ステータス行に `model: <name> effort=<x>`。
8. **進捗サマリ パネル** — 右側スプリッタに SESSION_SUMMARY 全文をスクロール表示・選択コピー可
   (単語をタスク注入に流用可)・↻更新。
9. **ctx% 分子バグ修正** — context_tokens を「最後のメイン assistant の usage(瞬間占有)」に変更。
   従来は result.usage(全往復累計)で過大評価し fullsense で 156%→100% 張付き。実機 4.3% vs 旧 8.4%。
10. **graceful Stop + ×終了確認 + ウィンドウアイコン** — Stop 1回目=作業記録(handoff)してから停止
    +砂時計、2回目=強制 kill。×は確認ダイアログ。assets/llterm-icon.ico をタイトルバーに。
11. **レート制限の自動再開** — rate_limit_event の resetsAt まで中断可能に待機 → 同ターン再試行。
    allowed は誤検知しない。auth 優先。
12. **Codex プロバイダ・チェーン** — Claude がレート制限なら CodexRunner(codex exec --json、
    ChatGPT Pro サブスク=課金なし)に切替、resetsAt 復活で Claude へ戻す。`_blocked_until` で
    プロバイダ別解除管理、SESSION_SUMMARY が cross-provider 継続を橋渡し。GUI「Codex 切替」トグル。

テスト 186 passed + ruff clean。実 claude / 実 codex で各機能を実機確認済み。

## 次にやるべきこと (TRIZ ideation の生存案 / 未着手)
- **handoff 自己検証ゲート** (rotate 直前に SESSION_SUMMARY の充足を検証、不足なら 1 回再執筆。
  検証は「exit_prep 前スナップショット比較 + 非空 + mtime」の複合シグナルに絞る)
- **ctl-gated 自律 rotate** (休眠中の ctl 制御プレーンを結線、閾値未満でも意味境界で自己申告 rotate)
- **failure postmortem 可視化** (circuit_open 時に直近エラーを 1 行要約して ledger/GUI に)
- **Watch Pin** (監視語を宣言しヒット時ハイライト) / **Heartbeat Digest** (周期サンプリング 1 行)
- ~~**多言語対応 (i18n)**~~ — **実装完了 (2026-06-12 夜、上記参照)**。残: zh/ko 訳 + GUI 言語切替 UI

---

# (詳細) 2026-06-12: 「claude の応答が GUI に表示されない」問題を解消

**根本原因** (ledger 実証): `ClaudeRunner` が `proc.communicate()` でターン完了まで全ブロック
し、GUI はターン完了後に最終 result テキスト 1 個を無着色で出すだけだった。自律 1 ターンは
数分〜数十分かかるため、その間 GUI は完全無表示 → ユーザーは故障と判断し Stop していた
(2026-06-12 03:20 UTC の実走 ledger で session_start 2.5 分後に cancelled を確認)。

**修正内容**:
- `loop.py ClaudeRunner` — stdout を**行単位リアルタイム読み**に変更 (communicate 廃止)。
  stderr 排出スレッド (pipe デッドロック防止) + `threading.Timer` watchdog (timeout 1800→7200s、
  自律長ターンの途中 kill 防止)。`on_stream` コールバックで要約イベントを逐次通知。
  `_build_args()` 分離 (テストが偽の子プロセスを注入する seam)。
- `loop.py summarize_stream_event()` — stream-json 1 イベント → GUI 用軽量 dict 列の純関数。
  **実 claude 2.1.174 の実出力で確認済** (init/assistant text/thinking/tool_use/user tool_result/
  result。hook_started 等の system と rate_limit_event は表示しない)。
- `worker.py` — `stream` シグナル追加。runner が `on_stream` を持てば購読 (duck-typing)。
- `app.py` — **セマンティックカラー描画** (One Dark 系 PALETTE + ダーク背景、html.escape 済み
  appendHtml)。応答=本文色 / セッション境界=黄 / ターン=青 / ツール=シアン / エラー=赤 /
  rotate=マゼンタ / 注入=緑 / 補助=灰。`_streamed_text` カウンタでストリーム済み応答の
  turn 完了時二重表示を防止。stream-json は ANSI を含まないため端末色パススルーではなく
  イベント種別で llterm 自身が着色する設計。
- `virtual.py` — 仮想 claude も同形の stream イベントを発行 (課金ゼロで表示経路を検証可能)。

**実走確認 (2026-06-12)**:
- `--resume <sid>` は**同一 session_id の in-place 継続** (fork しない) — ループの resume 設計は正
- `parse_stream_json` のフィールド名は実出力と一致 (result/total_cost_usd/usage/num_turns)
- 実 claude 1 ターン smoke: 2.9s init → **18.1s 初回テキスト表示** → 38.4s ターン完了
  (旧実装では 38.4s まで無表示)

**多視点レビュー修正 (同日, 30 agents の敵対的検証で 17 件確定 → 全件修正)**:
- **cancel 消失レース** — run_turn 入口の `_cancelled` リセット廃止 (cancel は恒久・runner は
  Start ごと新規)。Popen 中の cancel も `_proc` 代入と同 lock 区間で検知し即 kill
- **rotate 時の stop 再チェック** — Stop 後に exit準備の新規 claude が起動しない
- **CREATE_NO_WINDOW** — pythonw (gui-scripts) 起動で毎ターン console window が出ていた
- **rate_limit_event 可視化** — status≠allowed を赤太字 + リセット時刻表示 (黙殺しない)
- **auth 誤分類の限定** — auth 判定を stderr/非JSON診断行/result 本文に限定 (transcript 内の
  "authentication" 等で自走が不要停止しない)
- **subagent 区別表示** — parent_tool_use_id 非 null は ⤷ 付き灰色 + 二重表示判定に数えない
- **contextWindow 動的分母** — result の modelUsage.contextWindow (fable-5=1M) を used_pct の
  分母に採用 (既定 200K のままでは使用率 5 倍過大 → rotate が 5 倍早かった)
- その他: エラーターン text の握り潰し防止 / CR 正規化 (二重改行) / npm shim fail-closed /
  EOF 後 wait(30s) 上限 / timeout-完了競合ガード / returncode None ガード / cancelled 即停止 /
  worker の on_stream 上書き防止 / 巨大 tool_result の preview 走査を先頭 4KB に限定
- テスト **117 passed** (新規 28: watchdog/cancel 経路・auth 限定・subagent・rate_limit 等)、ruff clean

---

# (前回) llterm Session Summary — 2026-06-11 夜 (L2 自走エンジン + L3 Qt GUI)

## このセッションでやったこと
端末 (TUI) を捨てて **GUI 化**する方針転換 + **ループ駆動の心臓 (L2) を新規実装**。
ユーザー指摘「llterm は使いにくい、なぜ GUI でない、Qt 使えば済む」を受け、L3 を端末から
PySide6 GUI へ。会話で壊れ続けた問題は全部 terminal_io 由来 (win32-input-mode / ConPTY /
カーソル競合) で、GUI なら丸ごと消える、という判断。

### 新規実装 (commit 4207e67)
- **L2 `src/llterm/host/loop.py`** — 公式 headless protocol で自走するループ駆動本体。**端末を通らない**。
  - `claude -p <prompt> --output-format stream-json --verbose --session-id/--resume` を回す
    (フラグは `claude --help` で実在確認済)。
  - ループ = `新session → 前回の続き(resume) → 使用率70% → exit準備 → 畳む → 新session`。
  - 安全: circuit breaker / cost cap / **auth(再ログイン)検知で fail-closed 停止** / 監査 ledger /
    `stdin=DEVNULL`(orphan-reader hang を構造的に排除) / 無制限自走は CLI が拒否(課金保護)。
  - GUI 連携: `on_event` 進捗通知 / `should_stop` 協調停止 / `next_prompt` タスク注入。
  - `parse_stream_json` は防御的(フィールド欠落・壊れ行に耐える)。`TurnRunner` Protocol で mock 注入可。
- **L3 `src/llterm/gui/`** — PySide6 GUI(`app.py` 窓 / `worker.py` QThread / `virtual.py` 仮想claude)。
  出力(リングバッファ)/ctx% バー/cost/session 番号/Start・Stop/注入欄(Ctrl+Enter 送信)。
- **仮想 claude `gui/virtual.py`** — 実 claude 不要・課金ゼロ。使用率が増え 70% で rotate する擬似。
  fail_every / auth_after でエラー・認証切れ経路も模擬。

### 検証 (仮想 claude + offscreen, 課金ゼロ)
- 新規 **27 tests green**(`test_loop.py` 21 + `test_gui.py` 6)。フル 107 passed。
  - 失敗 1 = `test_pty_host::test_pty_roundtrip_with_python_child` は **既知 flaky**(全体実行時のみの
    winpty PermissionError。単体では 4 passed)。今回捨てる旧 PTY 端末側で本作業と無関係。
- GUI は **offscreen で main() rc=0**(`show()` まで起動確認)。`--real` 課金ガード rc=2 確認。
- entry points: `llterm-loop` / `llterm-gui` 登録済(editable 再インストール済)。

## 起動方法
```powershell
llterm-gui            # GUI 起動 → コンボボックスでプロジェクトを選び Start
llterm-gui --real     # 起動時に「実 claude (サブスク認証)」を選択状態にする
#   py -3.11 -m llterm.gui でも可
```
- GUI = プロジェクト選択コンボボックス(D:/projects 探索)+ 実/仮想トグル + 最大 session +
  **設定行(rotate閾値/窓tokens/コスト上限)** + **テンプレ選択(用途ツールチップ付き)** + 公開ボタン。
- **実 claude は claude.ai サブスク認証(`ClaudeRunner` が ANTHROPIC_API_KEY 系 env を外す)→ 従量課金なし**
  (Max 定額の範囲。制約は $ でなくレート制限)。仮想 claude(トグル off=既定)は課金ゼロのプレビュー。
- **テンプレ(機能別)**: `general` / `rad_expand`(RAD 拡張) / `green_keeper` / `doc_update`。
  GUI のコンボボックスで選択(各項目に用途ツールチップ)、`--template <key>` で CLI からも。
- **RAD 連携**: 参照=「RAD 参照」トグル(`--rad`、作業前に `D:/docs/*_corpus_v2` を grep)。
  拡張=`rad_expand` テンプレで分野を **staging** に生成 → **「公開」ボタン(人間ゲート)で live へ昇格**
  (`llterm-rad publish <domain>` でも可)。**自走ループは live を絶対に上書きしない**(共有 RAD 保護)。
- CLI 単体: `llterm-loop --workdir <対象> --dry-run --max-sessions 2`(仮想)。

## 次にやるべきこと
1. ~~課金モデルの確定~~ **解決済 (2026-06-12)**: claude は OAuth(claude.ai サブスク)認証 +
   env に ANTHROPIC_API_KEY 有り。`ClaudeRunner(use_subscription=True)` が API キー env を外して
   サブスク認証を強制 → **新たな従量課金なし**(レート制限内)。
2. ~~実 claude 初回走 (1回)~~ **解決済 (2026-06-12)**: stream-json 実フォーマット確認、
   `parse_stream_json` 一致、`--resume` は同一 ID in-place 継続を実証。
3. ~~応答のリアルタイム表示~~ **解決済 (2026-06-12)**: メッセージ級ストリーミング + カラー表示
   (上記参照)。token 級 (`--include-partial-messages`) は v2 候補のまま。
4. **v2**: L1 制御プレーン(`ctl/schema`+`gate`)をループに結線し rotate/inject を**監査・HITL ゲート**化 /
   token 級ストリーミング表示 / GUI に Approve ボタン / Stop の即時性(現状は実行中ターンの kill まで)。

## 設計の要点(なぜこの形か)
- **層分離**: L1 ctl(既存)+ L2 loop(新・headless・表示非依存)+ L3 Qt GUI(新)。L2 はGUIが無くても回る。
- prior art(GitHub調査): `frankbria/ralph-claude-code`(--resume+EXIT_SIGNAL+circuit breaker)、
  `claude-resurrect`(summary→self-exit→--resume)が機構的に近い。Ralph 本家は「毎回 fresh context で
  捨て切る」、本実装は「70% まで resume で引き継ぎ→exit準備→新session」のハイブリッド。
- 唯一の人間介在点 = **再ログイン**(構造的上限。それ以外は自動)。
