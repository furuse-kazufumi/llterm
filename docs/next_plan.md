# next_plan (正本) — llterm 自走ループ駆動 + GUI

> 最終更新: 2026-07-10 07:08 JST
> ★ 最終更新は **必ず `YYYY-MM-DD HH:MM JST` (時刻つき)** で書く。日付のみだと同日内の前後を
>   判定できず、共通進捗の並び順が信用できない(`progress.py` は本文のこの時刻を解析して
>   並び順の正に使い、時刻が無いと mtime にフォールバックする。ユーザー指摘 2026-06-13)。
> SESSION_SUMMARY.md は raptor Stop hook で自動上書きされるため、**このファイルが再開の正本**。
> 共通進捗 = `D:/projects/_shared/PROGRESS.md`(全 project 集約・`llterm-progress` で再生成)。

## 現在地

- llterm v0.2.0a0(公式 headless protocol `claude -p --session-id/--resume` で Claude Code を自走ループ駆動 + PySide6 GUI)。
- 2026-06-13 セッションで「ループが err=other→circuit_open で死ぬ」問題を根治し、進捗引継ぎ & HITL モデルを実装。
  続けて 進捗サマリ 2 タブ化 / 記録時刻正規化 / 注入の飢餓解消 / レビュー過剰削減 / **緊急注入** /
  全行タイムスタンプ / ローテログ / **ctx 過大計上(2549%)の是正** を実装。**全 424 テスト pass**。
- GUI 変更はプロセス再起動で反映(起動中の llterm には出ない)。auto-commit 監視が編集を逐次拾う。
- `progress.py` の `next_plan` / `SESSION_SUMMARY` parser は stale boundary の回帰を重点的に潰し、
  現在の単体回帰は `tests/test_progress.py = 89 passed`。`D:/projects/_shared/PROGRESS.md` も
  **この `> 最終更新` 行と同じ記録時刻**の `llterm` を先頭に再生成する運用に戻した。
  GUI の共通タブ refresh は、いまは **1 回だけ collect した snapshot** から All / project 別タブを描き、
  同じ snapshot を `_shared/PROGRESS.md` へ別スレッドで best-effort 反映する。
  writer は latest pending snapshot へ coalesce するので、refresh 連打でも thread を増やし続けない。
  さらに close 時は background writer を **2.0s bounded / best-effort** で drain する。
  writer 例外時は active/thread フラグを確実に復旧し、`daemon=True` に戻して shutdown を bounded に保つ。
  pending-None での終了判定と active/thread reset も原子的にし、lost-wakeup で pending が宙吊りにならないようにした。
  正常 close の flush は維持するが、stuck write / crash を含む全終了経路まで強保証するものではなく、bounded shutdown 優先の best-effort。
  これで GUI 表示どうしの食い違いは避けつつ、fsync を GUI スレッドへ持ち込まない。
- 直近の横断回帰は `tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py`
  をまとめて流し、**411 passed**。今回の GUI writer hardening が host/codex/orchestra 側の回帰を起こしていないことまで確認済み。
- さらに `codex_runner` の interrupt 窓、GUI writer の registration clobber、query-like 判定の日本語 false-negative を追加修正し、
  関連スイート再実行で **413 passed** を確認した。
- `ClaudeRunner` にも同型の post-spawn interrupt race が残っていたため、
  proc 起動直後の kill 判定を `cancel` と `interrupt` の両方で対称化した。
  これで緊急注入が `Popen` 後 `_proc` 登録直後の窓へ入っても、そのターンを kill して
  `error_kind="interrupted"` を返せる。関連回帰を足した `tests/test_loop.py` は **106 passed**。
- さらに `ClaudeRunner` / `CodexRunner` の post-spawn interrupt 即返し経路で、
  `_proc` 参照と child handle が残らないよう bounded `wait()` + `_proc=None` の後始末を追加した。
  window-specific 回帰も spawn hook 内 interrupt に締め直し、早期 return が cleanup を飛ばさないことを固定した。
- 横断確認で `GeminiRunner` だけが旧型のまま残っていたため、
  post-spawn interrupt 判定と cleanup も `Claude` / `Codex` と同じ形へ対称化した。
  以後、3 つの subprocess runner はすべて `cancel` 優先・`interrupt` 一発消費・bounded reap の同一意味論で揃う。
- その後、idle 中に立った `_interrupted` が次ターンへ漏れる別軸の不整合も見つかったため、
  `ClaudeRunner` / `CodexRunner` / `GeminiRunner` はいずれも `run_turn()` 開始時に stale `_interrupted`
  をリセットするよう追補した。`worker.request_interrupt()` の説明も実態どおり「kill は no-op だが、
  idle flag は次ターン開始時に無効化される」へ修正した。
- 残っていた軽微な非対称として、`GeminiRunner` の timeout だけ理由テキストが空だった点も解消した。
  これで `Claude` / `Codex` / `Gemini` の timeout はすべて GUI/ledger で理由が読める。
- `GeminiRunner` timeout 回帰も「非空」ではなく `t("runner.gemini.timeout")` への**文言一致**
  まで締め直した。これで将来の別メッセージ混入では緑にならない。
- `codex_runner._provider_version()` は `FileNotFoundError` だけを恒久欠落として cache し、
  一過性 `OSError` / `SubprocessError` は cache せず次ターンで再観測可能に戻した。
- `is_query_like_injection()` は query fast-path を **単一問い合わせ節限定**へさらに保守化し、
  `and` / `、` / `および` / `と` / `また` を複合要求マーカーとして扱うようにした。
  これで `status summary and review the diff` や `現状をまとめて、バグも直して` は
  full review 側へ倒れる。
- `_shared/PROGRESS.md` の先頭インデックス行には hidden marker として
  `updated` + `mtime` を埋め、same-minute の readback 比較は `mtime` tie-break を使うようにした。
  表示上の時刻フォーマットは従来どおり分単位のまま、古い同分 snapshot だけを安全側で再 commit できる。
- **handoff の前提**: この正本は current working tree の `src/` + `tests/` + `docs/` 差分を前提にしている。
  docs だけ先行確定した状態を正としない。
- **記録上の注意**: `docs/next_plan.md` はこのセッション群の handoff 正本として追記蓄積しているため、
  `git diff` 上の行数は「このターンだけの変更量」ではなく**累積ログ全体**を含む。直近の
  docs-only 整形は、上の `最終更新` と明示した記録補正箇所だけを見ること。
- **最新の横断確認値**: `tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py`
  の最新安定 cross-suite 確認値は **447 passed**。今回 `loop.py` だけを触った追試では
  `tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py = 292 passed`、
  `tests/test_gui.py -k "summary_has_live_and_common_tabs or common_summary_writer_start_failure_resets_active or stale_worker_stream_signal_is_ignored or stale_worker_event_signal_is_ignored" = 4 passed` を再確認した。
  GUI 全量再実行は Windows 側の teardown access violation で途中停止したため、直近安定全量値は 447 pass のまま扱う。

## 直近の成果

### snapshot recency 比較 / stale worker signal / denylist 漏れを補修した (2026-07-10 07:07, 本セッション)
- **RAD 接地**:
  stale overwrite 判定は identity と recency を分ける必要があり、queued signal は current worker 以外を fail-closed に無視するのが安全側。
  query fast-path も mutation 動詞の取りこぼしは review gate bypass につながるため、曖昧な語は full review 側へ倒す。
- **実装**:
  `src/llterm/progress.py` は snapshot の同一性 fingerprint を維持したまま、stale/fresh の前後比較だけ
  `(updated, mtime)` に射影した `_snapshot_recency_key()` で判定するよう変更した。
  `src/llterm/gui/app.py` の `_on_stream()` / `_on_event()` は `sender() is not None and sender() is not self.worker`
  のとき stale worker の queued signal を無視し、`LoopWorker` なら `_retire_worker()` へ送る。
  `src/llterm/host/loop.py` の `_QUERY_INJECTION_NEGATIVE` には `整理` / `整える` / `統合` / `並べ替え` /
  `並び替え` / `移す` を追加した。housekeeping として `.gitignore` に `.tmp_pytest_*.txt` も追加し、
  一時ログの誤コミットを防いだ。
- **テスト補強**:
  `tests/test_progress.py` に「disk fresh の top project 名が小さく、writer stale の top 名が大きい」
  case を追加し、project 名の辞書順だけで stale write が勝たないことを固定。
  `tests/test_gui.py` には stale worker の stream/event signal が現 run を汚染しない回帰を追加。
  `tests/test_loop.py::test_query_like_injection_detector_is_conservative` には
  `進捗を整理して` が `False` になるケースを足した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_progress.py -k "recency_compare_ignores_project_name_order or write_common_summary_items_rewrites_when_lower_project_only_changes_same_minute or write_common_summary_items_does_not_clobber_same_minute_readback_mismatch"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py -k "stale_worker_stream_signal_is_ignored or stale_worker_event_signal_is_ignored or summary_has_live_and_common_tabs or common_summary_writer_start_failure_resets_active"` = **4 passed**。
  `py -3.11 -m pytest -q tests/test_loop.py -k "query_like_injection_detector_is_conservative"` = **1 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **292 passed**。
  `ruff check src/llterm/progress.py src/llterm/gui/app.py src/llterm/host/loop.py tests/test_progress.py tests/test_gui.py tests/test_loop.py --isolated --extend-ignore E702` = pass。
- **残余**:
  rotate handoff 中の `interrupted` を fail-closed 停止のままにするか、次ターン消費へ回すかは未決。
  今回は挙動を変えず、判断待ちの low-priority 残件として維持する。
  次の具体的な一手は、`tests/test_gui.py` 全量の Windows teardown access violation を最小再現に切り出し、
  stale worker signal ガード追加後でも再現するかを確認したうえで、`_cleanup_widgets` / worker retire /
  `deleteLater()` 周辺の寿命管理を順に疑うこと。

### rotate handoff の rate limit 救済と CLI `projects_root` 配線を追加した (2026-07-10 06:49, 本セッション)
- **RAD 接地**:
  fail-closed は「恒久失敗で止まる」には有効だが、rotate 地点の handoff まで一時 `rate_limited` を terminal 扱いすると
  長時間自走の可用性を不必要に落とす。既存の通常ターン経路と同じく、同一 provider / 同一 session で
  reset 待ちの 1 回 retry を許し、それでも失敗したら fail-closed 停止へ戻す形に寄せた。
- **実装**:
  `src/llterm/host/loop.py` の rotate 分岐は、handoff が `error_kind=="rate_limited"` を返したときだけ
  `_wait_until(resets_at)` を挟んで **1 回だけ** `_handoff_run_turn()` を再実行する。
  待機中 stop 要求なら `"stopped"` で終え、retry 後も失敗なら従来どおり `exit_prep_failed` で止まる。
  あわせて CLI `main()` に `--projects-root` を追加し、未指定時は `workdir.parent` を優先、
  使えない場合だけ `DEFAULT_PROJECTS_ROOT` へフォールバックするようにした。
- **テスト補強**:
  `tests/test_loop.py` に rotate handoff の `rate_limited -> wait -> retry success`、
  `retry failure -> exit_prep_failed`、`wait 中 stop -> stopped` を追加。
  CLI 側は `--projects-root` 明示指定と、未指定時に `workdir.parent` が使われることを固定した。
  既存の `test_rotate_does_not_refresh_shared_progress_when_exit_prep_fails` は、
  いま `rate_limited` が救済対象になったため terminal な `error_kind="other"` へ更新した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_loop.py -k "rotate_handoff_rate_limited or projects_root or rotate_does_not_refresh_shared_progress_when_exit_prep_fails or cli_projects_root"` = **5 passed**。
  `py -3.11 -m pytest -q tests/test_loop.py` = **112 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_orchestra_runner.py` = **179 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py -k "summary_has_live_and_common_tabs or common_summary_writer_start_failure_resets_active"` = **2 passed**。
  `ruff check src/llterm/host/loop.py tests/test_loop.py tests/test_codex_runner.py --isolated --extend-ignore E702` = pass。
- **残余 / 追跡**:
  `tests/test_gui.py` 全量は Windows teardown の access violation で今回も完走していない。
  直近安定値 447 pass は保持するが、今回の 2 pass smoke は全量の代替ではないことを記録しておく。

### rotate 前 handoff 失敗を fail-closed 停止へ変更した (2026-07-10 06:07, 本セッション)
- **RAD 接地**:
  fail-closed / durable handoff の既存パターンに合わせ、handoff 失敗時に context を捨てて rotate するより、
  その場で止めて次セッションへ誤った継続状態を持ち込まない側を採った。
- **実装**:
  `src/llterm/host/loop.py` の rotate 分岐は、`_handoff_run_turn()` が失敗した場合に
  ledger へ `exit_prep_failed` を記録したうえで、そのまま `stop_reason="exit_prep_failed"` で
  `_finish()` するよう変更した。新 session へ進まず fail-closed 停止するため、
  共有進捗未更新と context discard が同時に起きる窓を潰した。
- **テスト補強**:
  `tests/test_loop.py::test_rotate_does_not_refresh_shared_progress_when_exit_prep_fails` は
  `max_sessions=2` に広げ、handoff failure 後に `runner.calls == 2` のまま新 session へ進まないことと、
  `outcome.stop_reason == "exit_prep_failed"` を固定した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_loop.py -k "rotate_does_not_refresh_shared_progress_when_exit_prep_fails or rotate_then_stop_records_handoff or graceful_stop_does_not_refresh_shared_progress_when_handoff_fails"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **286 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py -k "summary_has_live_and_common_tabs or common_summary_writer_start_failure_resets_active"` = **2 passed**。
  `ruff check src/llterm/host/loop.py tests/test_loop.py --isolated --extend-ignore E702` = pass。

### common summary stale-guard を sidecar 化し、GUI/durable 回帰を解消した (2026-07-10 05:59, 本セッション)
- **妥当指摘の反映**:
  `src/llterm/progress.py` の same-minute stale-guard は index 行末 marker をやめ、
  hidden sidecar `._shared/PROGRESS.md.meta.json` へ snapshot fingerprint を持つ形へ切り替えた。
  これで GUI の `All` タブと durable `PROGRESS.md` から `<!-- ... -->` が消え、
  stale 判定も「先頭 1 件」ではなく snapshot 全体の `(name, updated, mtime)` 配列で比較できる。
  可視本文が stale だが meta は同一 snapshot のケースだけは安全に再 commit し、
  逆に同分の別 snapshot には再 commit しない。
  `src/llterm/gui/app.py` は common project tab の sort key を `(updated, mtime)` に揃え、
  writer thread の `start()` 失敗時も active/thread フラグを戻して durable write が恒久ジャムしないようにした。
- **テスト補強**:
  `tests/test_progress.py` は marker 非露出、same-minute sort の第二キー、
  `other` snapshot 非 clobber、下位 project のみ更新された same-minute snapshot の再 commit を固定。
  `tests/test_gui.py` には `All` タブ/共通ファイルへ `<!--` が出ないこと、
  writer `start()` 失敗時に active が寝ることを追加した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_progress.py -k "build_common_summary_breaks_same_minute_ties_with_mtime or write_common_summary_items_rewrites_when_lower_project_only_changes_same_minute or write_common_summary_items_rewrites_older_same_minute_snapshot_using_mtime_tiebreak or write_common_summary_items_does_not_clobber_same_minute_readback_mismatch"` = **4 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py -k "summary_has_live_and_common_tabs or common_summary_writer_start_failure_resets_active or refresh_common_summary_collects_once_and_writes_same_snapshot or common_summary_writer_coalesces_pending_snapshots"` = **4 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **447 passed**。
  `ruff check src/llterm/gui/app.py src/llterm/progress.py src/llterm/host/codex_runner.py src/llterm/host/gemini_runner.py src/llterm/host/loop.py tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py --isolated --extend-ignore E702` = pass。

### query denylist / same-minute sort / provider cleanup を補完した (2026-07-10 05:37, 本セッション)
- **妥当指摘の反映**:
  `src/llterm/host/loop.py` の `_QUERY_INJECTION_NEGATIVE` に `作り直` / `直す` / `直し` /
  `消す` / `消し` / `書き換` / `やり直` を追加し、活用形の mutation 動詞で
  review gate を bypass しにくくした。
  `src/llterm/progress.py` の `build_common_summary()` は sort key を `(updated, mtime)` に広げ、
  same-minute tie-break marker が index 先頭で実際に最新 snapshot を代表しやすいよう補正した。
  `src/llterm/host/gemini_runner.py` は watchdog timeout 後に 2 回目の `wait()` を入れて
  `Claude` / `Codex` と同じ bounded reap に揃え、`src/llterm/host/codex_runner.py` の
  provider version probe は `stdin=subprocess.DEVNULL` を明示して親 stdin 継承を外した。
- **テスト補強**:
  `tests/test_loop.py` に `進捗ファイルを作り直す` と
  `現在の進捗を見て、不要な項目を消す` の false-negative 回帰を追加した。
  `tests/test_progress.py` には同じ分・異なる `mtime` の複数 project で
  `beta` が先頭になる sort 回帰を追加した。
  `tests/test_gemini_runner.py` は timeout 後に `wait()` が 2 回呼ばれること、
  `tests/test_codex_runner.py` は provider version probe が `stdin=DEVNULL` で起動されることを固定した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_loop.py -k "query_like_injection_detector_is_conservative"` = **1 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py -k "build_common_summary_breaks_same_minute_ties_with_mtime or write_common_summary_items_rewrites_older_same_minute_snapshot_using_mtime_tiebreak or write_common_summary_items_does_not_clobber_same_minute_readback_mismatch"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gemini_runner.py -k "gemini_timeout_returns_visible_reason or gemini_timeout_waits_again_after_kill"` = **2 passed**。
  `py -3.11 -m pytest -q tests/test_codex_runner.py -k "provider_version_probe_uses_devnull_stdin or provider_version_transient_failure_is_not_cached or provider_version_not_found_is_cached_once"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **445 passed**。
  `ruff check src/llterm/host/loop.py src/llterm/progress.py src/llterm/host/gemini_runner.py src/llterm/host/codex_runner.py tests/test_loop.py tests/test_progress.py tests/test_gemini_runner.py tests/test_codex_runner.py --isolated --extend-ignore E702` = pass。

### query fast-path / shared progress tie-break を補強した (2026-07-10 05:15, 本セッション)
- **RAD 接地**: fast-path classification は複合要求を危険側に誤判定すると review gate を bypass する。
  summary/progress の問い合わせ最適化は「誤って full review に倒す」ほうが安全であり、
  single-writer projection も same-minute 比較の tie-break を持つほうが stale readback に強い。
- **実装**:
  `src/llterm/host/loop.py` の `_QUERY_INJECTION_SEQUENCE_MARKERS` に
  `and` / `、` / `，` / `および` / `と` / `また` を追加し、
  docstring も「fast-path は単一問い合わせ節のみ」と明記した。
  `src/llterm/progress.py` は共通進捗インデックス行へ hidden marker
  `<!-- updated=... mtime=... -->` を埋め、readback 比較は `(updated, mtime)` の tuple で判定する。
  これにより、手書き `最終更新` が同じ分でも source file の `mtime` が新しい snapshot を tie-break で優先できる。
  併せて `CodexRunner._provider_version()` の probe 例外後に `wait()` を足し、
  close drain docstring / orchestra aux bench key の前提コメントも実装に合わせて補足した。
- **テスト補強**:
  `tests/test_loop.py` に `status summary and review the diff` と
  `現状をまとめて、バグも直して` の false-negative 回帰を追加し、
  `tests/test_progress.py` には same-minute の older snapshot だけを `mtime` tie-break で
  再 commit するケースを追加した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_loop.py -k "query_like_injection_detector_is_conservative"` = **1 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py -k "write_common_summary_items_does_not_clobber_same_minute_readback_mismatch or write_common_summary_items_rewrites_older_same_minute_snapshot_using_mtime_tiebreak"` = **2 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py -k "summary_has_live_and_common_tabs or refresh_common_summary_collects_once_and_writes_same_snapshot or common_summary_writer_coalesces_pending_snapshots or close_drains_common_summary_writer_latest_pending or common_summary_writer_drain_timeout_is_bounded or common_summary_writer_recovers_after_write_exception or common_summary_writer_does_not_lose_pending_schedule_at_exit_boundary or common_summary_writer_finally_does_not_clobber_new_worker_registration"` = **8 passed**。
  `py -3.11 -m pytest -q tests/test_codex_runner.py -k "provider_version_not_found_is_cached_once or provider_version_transient_failure_is_not_cached or interrupt_during_probe_does_not_poison_provider_version_cache"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_loop.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_orchestra_runner.py -k "query_like or common_summary or provider_version or timeout_returns_visible_reason or idle_interrupt_does_not_poison_next_turn"` = **29 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **442 passed**。
  `ruff check src/llterm/host/loop.py src/llterm/progress.py src/llterm/host/codex_runner.py src/llterm/gui/app.py src/llterm/host/orchestra_runner.py tests/test_loop.py tests/test_progress.py --isolated --extend-ignore E702` = pass。

### `ClaudeRunner` の post-spawn interrupt race も閉じた (2026-07-10 03:36, 本セッション)
- **RAD 接地**: cancellation / interrupt は provider ごとに意味が揺れると review gate や緊急注入の契約が崩れる。
  `codex_runner` だけ先に対称化しても、`ClaudeRunner` に同じ窓が残っていれば loop 全体としては未収束。
- **妥当指摘の発見**: `src/llterm/host/loop.py` の `ClaudeRunner.run_turn()` は
  pre-start では `_cancelled` だけを見ていたわけではないが、`Popen` 後 `_proc` 登録直後の kill 判定が
  依然 `self._cancelled` のみだった。ここに `interrupt()` が入ると、現ターン即中断契約に反して
  子プロセスが最後まで走り得る。
- **実装**:
  `src/llterm/host/loop.py` の post-spawn 判定を `kill_now or interrupt_now` へ広げ、
  `interrupt_now` だけが立っている場合は `_interrupted` をその場で消費しつつ子を kill して
  即 `TurnResult(... error_kind="interrupted")` を返すようにした。これで `CodexRunner` と同じ停止意味論に揃う。
- **テスト補強**:
  `tests/test_loop.py::test_claude_runner_interrupt_after_spawn_returns_interrupted` を追加し、
  `subprocess.Popen` の hook で spawn 完了を同期してから `runner.interrupt()` を打ち、
  スレッドが短時間で `interrupted` を返して子も終了することを固定した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_loop.py -k "claude_runner_interrupt_after_spawn_returns_interrupted or claude_runner_cancel_kills_running_turn or interrupted_turn_continues_and_consumes_injection"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_loop.py` = **106 passed**。
  `py -3.11 -m pytest -q tests/test_loop.py tests/test_codex_runner.py -k "interrupt or cancel_before_start_is_sticky or consumes_injection or provider_version or query_like"` = **13 passed**。
  `ruff check src/llterm/host/loop.py tests/test_loop.py --isolated --extend-ignore E702` = pass。

### post-spawn interrupt 即返し経路の cleanup も対称化した (2026-07-10 03:58, 本セッション)
- **妥当指摘の反映**:
  `ClaudeRunner` と `CodexRunner` の post-spawn interrupt fast-path は、
  `try/finally` より前で `return TurnResult(... "interrupted")` していたため、kill 済み proc の
  `_proc` 参照クリアと bounded reap が走らないままだった。
- **実装**:
  `src/llterm/host/loop.py` と `src/llterm/host/codex_runner.py` の両方で、
  post-spawn `kill_now or interrupt_now` 分岐に `proc.wait(timeout=10)` と
  `if self._proc is proc: self._proc = None` を追加した。`_interrupted` の消費条件式も
  `interrupt_now and not kill_now` に揃え、cancel 優先の読み味を統一した。
- **テスト補強**:
  `tests/test_loop.py` / `tests/test_codex_runner.py` の window test は、
  `Popen` hook 内で `runner.interrupt()` を呼ぶ形へ変更し、`Popen` 後 `_proc` 登録前後の窓を
  決定的に踏むようにした。あわせて `runner._proc is None` を assert し、早期 return 経路の
  cleanup 漏れを固定した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_loop.py -k "interrupt_in_post_spawn_window_returns_interrupted_and_clears_proc or claude_runner_cancel_kills_running_turn"` = **2 passed**。
  `py -3.11 -m pytest -q tests/test_codex_runner.py -k "interrupt_in_post_spawn_window_returns_interrupted_and_clears_proc or cancel_before_start_is_sticky"` = **2 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **414 passed**。
  `ruff check src/llterm/host/loop.py src/llterm/host/codex_runner.py tests/test_loop.py tests/test_codex_runner.py --isolated --extend-ignore E702` = pass。

### `GeminiRunner` の post-spawn interrupt race / cleanup も揃えた (2026-07-10 04:05, 本セッション)
- **RAD 接地**: 同じ「subprocess を起こして headless agent を回す runner」なのに停止契約が provider ごとにずれると、
  緊急注入の再現性と review gate の fail-closed 性が落ちる。bounded cleanup も runner 間で共通化した方がよい。
- **妥当指摘の発見**:
  `src/llterm/host/gemini_runner.py` は `interrupt()` を持つ一方で、post-spawn 判定がまだ `cancel` しか見ておらず、
  `Popen` 後 `_proc` 登録直後の窓では `Claude` / `Codex` と同型の race が残っていた。
- **実装**:
  `GeminiRunner.run_turn()` の post-spawn 分岐を `kill_now or interrupt_now` へ広げ、
  `interrupt_now and not kill_now` のときは `_interrupted` を消費しつつ子を kill、
  `proc.wait(timeout=10)` と `_proc=None` の cleanup を済ませてから
  即 `TurnResult(... error_kind="interrupted")` を返すようにした。
- **テスト補強**:
  `tests/test_gemini_runner.py::test_interrupt_in_post_spawn_window_returns_interrupted_and_clears_proc`
  を追加し、`Popen` hook 内で `runner.interrupt()` を打って window-specific にその窓を踏ませ、
  `runner._proc is None` まで固定した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_gemini_runner.py -k "interrupt_in_post_spawn_window_returns_interrupted_and_clears_proc or cancel_before_start_is_sticky or multiline_prompt_reaches_child_intact_via_stdin"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **436 passed**。
  `ruff check src/llterm/host/gemini_runner.py tests/test_gemini_runner.py --isolated --extend-ignore E702` = pass。

### stale `_interrupted` を 3 runner の開始時にリセットするよう揃えた (2026-07-10 04:26, 本セッション)
- **妥当指摘の反映**:
  `orchestra_runner` だけは `run_turn()` 開始時に `_interrupted=False` を明示していた一方、
  `ClaudeRunner` / `CodexRunner` / `GeminiRunner` は idle 中の `interrupt()` で立った stale flag を
  次ターンへ持ち越し得た。特に worker は active runner を知らず全 runner に `interrupt()` を投げるため、
  非 active な fallback runner が後続ターンを無関係に `interrupted` で潰す経路が残っていた。
- **実装**:
  3 runner とも `cancel` の pre-start check 直後、`Popen` 前の lock 節で
  `self._interrupted = False` を入れ、**走行中に届いた interrupt だけ**を拾うよう整理した。
  `CodexRunner` の pre-provider-version 即 `interrupted` 返しも撤去し、開始時 stale flag をまず捨てた上で、
  実行中に来た interrupt は従来どおり provider-version probe / post-spawn / turn 末尾の各境界で拾う。
  `src/llterm/gui/worker.py` の docstring も「実行中でない runner への interrupt は完全 no-op」ではなく、
  kill は no-op だが idle flag は次ターン開始時にリセットされる旨へ修正した。
- **テスト補強**:
  `tests/test_loop.py::test_claude_runner_idle_interrupt_does_not_poison_next_turn`
  `tests/test_codex_runner.py::test_codex_runner_idle_interrupt_does_not_poison_next_turn`
  `tests/test_gemini_runner.py::test_gemini_runner_idle_interrupt_does_not_poison_next_turn`
  を追加し、idle runner へ `interrupt()` を打った後の次ターンが通常起動することを固定した。
  既存の post-spawn window test comment も、「登録前後を決定的に踏む」ではなく
  「Popen 後にフラグを立て、post-spawn 判定がそれを拾う」へ実態に合わせて補正した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_loop.py -k "idle_interrupt_does_not_poison_next_turn or interrupt_in_post_spawn_window_returns_interrupted_and_clears_proc or cancel_before_start_is_sticky"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_codex_runner.py -k "idle_interrupt_does_not_poison_next_turn or interrupt_in_post_spawn_window_returns_interrupted_and_clears_proc or cancel_before_start_is_sticky"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gemini_runner.py -k "idle_interrupt_does_not_poison_next_turn or interrupt_in_post_spawn_window_returns_interrupted_and_clears_proc or cancel_before_start_is_sticky"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **439 passed**。
  `ruff check src/llterm/host/loop.py src/llterm/host/codex_runner.py src/llterm/host/gemini_runner.py src/llterm/gui/worker.py tests/test_loop.py tests/test_codex_runner.py tests/test_gemini_runner.py --isolated --extend-ignore E702` = pass。

### `GeminiRunner` timeout の理由文も対称化した (2026-07-10 04:33, 本セッション)
- **妥当指摘の反映**:
  `GeminiRunner` だけ timeout 分岐が `text=""` の `err=other` で返っており、`Claude` / `Codex` と違って
  GUI と ledger で「なぜ落ちたか」が読めなかった。
- **実装**:
  `src/llterm/host/gemini_runner.py` の timeout 分岐を `t("runner.gemini.timeout")` へ差し替え、
  `src/llterm/i18n/messages.py` に ja/en の `runner.gemini.timeout` を追加した。
- **テスト補強**:
  `tests/test_gemini_runner.py::test_gemini_timeout_returns_visible_reason` を追加し、
  watchdog timeout でも `error_kind="other"` のまま `res.text.strip()` が空でないことを固定した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_gemini_runner.py -k "timeout_returns_visible_reason or idle_interrupt_does_not_poison_next_turn or interrupt_in_post_spawn_window_returns_interrupted_and_clears_proc"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **440 passed**。
  `ruff check src/llterm/host/gemini_runner.py src/llterm/i18n/messages.py tests/test_gemini_runner.py --isolated --extend-ignore E702` = pass。

### Gemini timeout 回帰を文言一致まで締めた (2026-07-10 04:42, 本セッション)
- **妥当指摘の反映**:
  `tests/test_gemini_runner.py::test_gemini_timeout_returns_visible_reason` は、前回まで
  `res.text.strip()` の非空しか見ておらず、timeout 専用文言かどうかまでは固定していなかった。
- **実装**:
  ロジック変更は入れず、テストだけ `from llterm.i18n import t` を使って
  `assert res.text == t("runner.gemini.timeout")` へ差し替えた。
- **検証**:
  `py -3.11 -m pytest -q tests/test_gemini_runner.py -k "gemini_timeout_returns_visible_reason or idle_interrupt_does_not_poison_next_turn or interrupt_in_post_spawn_window_returns_interrupted_and_clears_proc"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **440 passed**。
  `ruff check tests/test_gemini_runner.py --isolated --extend-ignore E702` = pass。

### `codex_runner._provider_version()` は一過性失敗を cache しないようにした (2026-07-10 04:51, 本セッション)
- **妥当指摘の反映**:
  `src/llterm/host/codex_runner.py` の `_provider_version()` は、Popen 例外を種別問わず空文字で cache しており、
  一過性 `OSError` でも runner 寿命中ずっと provenance が欠落し得た。停止起因の欠測を cache しない既存方針とも非対称だった。
- **実装**:
  `FileNotFoundError` だけを「CLI 恒久不在」として空文字 cache し、それ以外の
  `OSError` / `ValueError` / `SubprocessError` は空文字を返すだけで cache しないよう変更した。
  これで一過性失敗後は次ターンで再観測可能に戻る。
- **テスト補強**:
  既存の `test_provider_version_failure_is_cached_once` は `FileNotFoundError` 専用へ改名し、
  追加で `test_provider_version_transient_failure_is_not_cached` を入れて `OSError` では 2 回とも再試行され、
  `_provider_version_cache is None` のままなことを固定した。
- **docs 補正**:
  common writer drain は `2.0s bounded / best-effort` と明示し、handoff は current working tree の
  `src/` + `tests/` + `docs/` 差分を前提とする旨も追記した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_codex_runner.py -k "provider_version_not_found_is_cached_once or provider_version_transient_failure_is_not_cached or interrupt_during_probe_does_not_poison_provider_version_cache"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_gemini_runner.py tests/test_loop.py tests/test_orchestra_runner.py` = **441 passed**。
  `ruff check src/llterm/host/codex_runner.py tests/test_codex_runner.py docs/next_plan.md docs/SESSION_SUMMARY.md --isolated --extend-ignore E702` = pass。

### GUI の共通タブ refresh を single-snapshot + 非同期 durable write へ直した (2026-07-10 02:48, 本セッション)
- **RAD 接地**: event-sourcing / projection rebuild の既存知見どおり、`next_plan` が正本で
  `PROGRESS.md` は materialized view なので、1 回の source snapshot から複数 projection を派生させる方がよい。
  また event-driven / async loop の既存知見どおり、projection commit の fsync は UI スレッドと時間分離した方が安全。
- **妥当指摘の反映**: 前回案は `write_common_summary()` の内部 collect と GUI 側の再 collect が分かれており、
  その間に `next_plan` が更新されると All / durable file と project 別サブタブが別 snapshot になり得た。
  さらに `_refresh_common_summary()` が GUI スレッド上で毎回 file write + fsync まで踏んでいた。
- **実装**:
  `src/llterm/progress.py` に `write_common_summary_items(items, out_path)` と内部 helper
  `_write_common_summary_text()` を追加し、**既に収集済みの items から**共通 summary を commit できるようにした。
  `src/llterm/gui/app.py` の `_refresh_common_summary()` は `collect_progress()` を 1 回だけ呼び、
  その `items` から `build_common_summary(items)` で All タブを描き、同じ `items` で project 別タブを同期する。
  `_shared/PROGRESS.md` への durable 反映は `_schedule_common_summary_write(items)` が別スレッドで行う。
- **並行 writer の扱い**: snapshot writer は readback mismatch 時でも、**同一分の内容不一致**を理由に
  再集約・再上書きしない。これで遅延した古い GUI refresh が、同時刻の別 snapshot を blind 上書きする経路を避ける。
- **テスト補強**:
  `tests/test_progress.py::test_write_common_summary_items_does_not_clobber_same_minute_readback_mismatch` で
  snapshot writer が same-minute mismatch を再上書きしないことを固定。
  `tests/test_gui.py::test_refresh_common_summary_collects_once_and_writes_same_snapshot` で
  GUI refresh が `collect_progress()` を 1 回しか呼ばず、その snapshot を file write に渡すことを固定した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_progress.py -k "write_common_summary_items or same_minute or rewrites_when_readback_is_same_minute_but_stale"` = **2 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py -k "summary_has_live_and_common_tabs or refresh_common_summary_collects_once_and_writes_same_snapshot"` = **2 passed**。
  `ruff check src/llterm/progress.py src/llterm/gui/app.py tests/test_progress.py tests/test_gui.py --isolated` = pass。

### GUI の共通 summary writer を latest-pending coalescing にした (2026-07-10 02:51, 本セッション)
- **RAD 接地**: event-driven / async loop の既存知見どおり、background projection writer は
  producer を block しないだけでなく、古い pending job を意味的に圧縮できるなら圧縮した方がよい。
  今回の durable view は latest snapshot があれば十分なので、refresh ごとの thread 乱立は不要。
- **背景**: 前回の非同期化は GUI スレッドから fsync を外せていたが、refresh のたびに daemon thread を
  1 本ずつ spawn していた。短時間の連続 refresh では、古い snapshot が skip されても thread 数だけは増える。
- **実装**: `src/llterm/gui/app.py` の `_schedule_common_summary_write()` は
  `self._common_summary_write_pending` と `self._common_summary_write_active` を持つ単一 worker 方式へ変更した。
  走行中に新しい refresh が来たら pending を**最新 snapshot で上書き**し、worker は現在の書込み完了後に
  latest pending だけを追加で commit する。これで stale な中間 snapshot の durable write を減らせる。
- **テスト補強**: `tests/test_gui.py::test_common_summary_writer_coalesces_pending_snapshots` で、
  `alpha` 書込み中に `beta` / `gamma` を連続投入したとき、実際に commit されるのが `alpha` と最終の `gamma`
  だけであることを固定した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_gui.py -k "summary_has_live_and_common_tabs or refresh_common_summary_collects_once_and_writes_same_snapshot or common_summary_writer_coalesces_pending_snapshots"` = **3 passed**。
  `ruff check src/llterm/gui/app.py tests/test_gui.py --isolated` = pass。

### close 時に common summary writer を drain し、token tooltip provenance も reset するようにした (2026-07-10 02:57, 本セッション)
- **RAD 接地**: background projection は非同期でも、終了境界では K/provenance を吐き切る必要がある。
  また provenance 表示は stale だと false trust を生むので、セッション境界で text と tooltip を一緒に reset する方がよい。
- **妥当指摘の反映**:
  `daemon=True` writer だけだと `_schedule_common_summary_write()` 直後の close で latest pending が落ち得た。
  さらに `session_start` で `lbl_tokens` の text だけを idle に戻し、tooltip の `provider_version` が残留していた。
- **実装**:
  `src/llterm/gui/app.py` に `_common_summary_writer_thread` と `_drain_common_summary_write()` を追加し、
  `closeEvent()` の accept 前に background writer を join して pending を吐き切るようにした。
  同時に `session_start` の `_reset_tokens(reset_tooltip=False)` を `_reset_tokens()` へ戻し、
  新セッション開始時は tooltip からも前セッションの provenance を消すよう修正した。
- **テスト補強**:
  `tests/test_gui.py::test_close_drains_common_summary_writer_latest_pending` で、close 経路が
  `alpha` 書込み中の pending `beta` まで吐き切ることを固定した。
  `tests/test_gui.py::test_tokens_reset_on_session_start` は、session start 後の tooltip が
  既定文言へ戻り `provider_version` を含まないことを確認する形に更新した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_gui.py -k "tokens_reset_on_session_start or close_drains_common_summary_writer_latest_pending or common_summary_writer_coalesces_pending_snapshots"` = **3 passed**。
  `ruff check src/llterm/gui/app.py tests/test_gui.py --isolated` = pass。

### common summary writer の例外復旧と bounded shutdown を直した (2026-07-10 03:06, 本セッション)
- **RAD 接地**: provenance writer は single-writer でも、例外で active フラグが固着すると durable projection が
  サイレント停止する。background I/O は bounded shutdown を壊さない範囲で動かすべきで、ここでは durability より
  先に「終了が詰まらない」不変条件を守る。
- **妥当指摘の反映**:
  1. `_writer()` は `write_common_summary_items()` 例外で黙って死に、`_common_summary_write_active=True` /
     `_common_summary_writer_thread=writer` が残留し得た。
  2. `daemon=False` は `_drain_common_summary_write(timeout=2.0)` の bounded 設計と矛盾し、終了時の無期限 wait を招き得た。
- **実装**:
  `src/llterm/gui/app.py` の `_writer()` は `try/finally` で全体を囲い、終了時に
  `_common_summary_write_active=False` と `_common_summary_writer_thread=None` をロック下で必ず復旧する。
  個々の `write_common_summary_items()` も `try/except` で包み、失敗時は stderr へ記録してループ継続する。
  あわせて writer thread は `daemon=True` へ戻し、close 時の明示 drain は残したまま shutdown の bounded 性を優先した。
- **テスト補強**:
  `tests/test_gui.py::test_common_summary_writer_recovers_after_write_exception` で、1 回目の write failure 後でも
  次の `_schedule_common_summary_write()` が新 worker を起動し、`beta` の durable write へ復帰することを固定した。
  `tests/test_gui.py::test_common_summary_writer_drain_timeout_is_bounded` で、
  blocked writer に対する `_drain_common_summary_write(timeout=0.05)` が短時間で戻ることを固定した。
  旧 `test_common_summary_writer_thread_is_non_daemon` は削除相当で、この挙動テストへ置き換えた。
- **検証**:
  `py -3.11 -m pytest -q tests/test_gui.py -k "close_drains_common_summary_writer_latest_pending or common_summary_writer_drain_timeout_is_bounded or common_summary_writer_recovers_after_write_exception or common_summary_writer_coalesces_pending_snapshots or tokens_reset_on_session_start"` = **5 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py` = **158 passed**。
  `ruff check src/llterm/gui/app.py tests/test_gui.py --isolated` = pass。

### common summary writer の lost-wakeup race を閉じた (2026-07-10 03:14, 本セッション)
- **RAD 接地**: single-writer loop では `pending 非 None => いつか flush` が基本不変条件で、終了分岐と state reset の ownership が曖昧だと
  message-queue の ack 漏れに近い宙吊りが起きる。ここは durability 以前に「pending を孤児化しない」ことを優先する。
- **妥当指摘の反映**:
  `_writer()` の `pending is None` 分岐は、ロックを外してから `finally` で `active=False/thread=None` に落ちる窓があり、
  その隙に `_schedule_common_summary_write()` が入ると `pending` だけ積まれて worker 不在になり得た。
- **実装**:
  `src/llterm/gui/app.py` の `pending is None` 分岐で、
  `self._common_summary_write_pending is None` 判定と
  `self._common_summary_write_active=False / self._common_summary_writer_thread=None / return`
  を**同じロック保持内**へ移した。`finally` の同リセットは予期せぬ例外時の保険として冪等に残した。
  ついでに writer failure ログは `traceback.print_exc()` も出すようにして、無人運用時の診断性を上げた。
- **テスト補強**:
  `tests/test_gui.py::test_common_summary_writer_does_not_lose_pending_schedule_at_exit_boundary` を追加し、
  worker の終了分岐に schedule を割り込ませても、最終的に `beta` が flush され
  `active=False / pending=None / thread=None` に収束することを固定した。
- **記述補正**:
  durability の説明は「通常 close では drain で latest pending を吐き切るが、stuck write を含む全終了経路での完全保証ではなく、
  bounded shutdown 優先の best-effort」として扱う。
- **検証**:
  `py -3.11 -m pytest -q tests/test_gui.py -k "does_not_lose_pending_schedule_at_exit_boundary or recovers_after_write_exception or drain_timeout_is_bounded"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_gui.py` = **159 passed**。
  `ruff check src/llterm/gui/app.py tests/test_gui.py --isolated` = pass。

### GUI/progress/host の横断回帰を再実行した (2026-07-10 03:15, 本セッション)
- **目的**: common summary writer まわりの変更が GUI ローカル回帰だけでなく、
  `progress.py`・`codex_runner.py`・`loop.py`・`orchestra_runner.py` の既存契約へ波及していないかを確認した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py`
  = **411 passed**。
  `ruff check src/llterm/gui/app.py src/llterm/progress.py src/llterm/host/codex_runner.py src/llterm/host/loop.py src/llterm/host/orchestra_runner.py tests/test_gui.py tests/test_progress.py tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py --isolated --extend-ignore E702`
  = pass。
- **判断**: 現時点で residual risk は durability/bounded-shutdown の仕様境界に限られ、既存の host/provider 契約を壊す回帰は見えていない。

### codex interrupt 窓 / GUI writer 登録 clobber / query-like 日本語 false-negative を修正した (2026-07-10 03:30, 本セッション)
- **RAD 接地**:
  fail-closed review gate は false-negative を減らす側へ倒すべきで、緊急注入 interrupt も cancel と対称に扱う方がよい。
  また single-writer loop では「いま登録されている worker の所有権」を壊さないことが重要で、後着 worker の登録を旧 worker が潰してはならない。
- **妥当指摘の反映**:
  1. `src/llterm/host/codex_runner.py` は proc 起動直後の kill 判定で `_cancelled` しか見ておらず、
     `interrupt()` が post-spawn 窓に入ると現ターン即中断契約が競合で破れ得た。
  2. `src/llterm/gui/app.py` の common summary writer は、正常 return 経路と `finally` の二重 reset により、
     後着 worker の registration を旧 worker が clobber し得た。
  3. `src/llterm/host/loop.py` の query-like 判定は `書いて` / `追記` などの日本語 mutation 動詞が欠けており、
     `get_status 関数を書いて` のような実装依頼が unreviewed 経路へ漏れ得た。
- **実装**:
  `src/llterm/host/codex_runner.py` は post-spawn の kill 判定で `_interrupted` も見るようにし、
  interrupt なら proc を kill して `error_kind="interrupted"` を返す形へ cancel と対称化した。
  `src/llterm/gui/app.py` は `finally` の active/thread reset を
  `self._common_summary_writer_thread is threading.current_thread()` のときだけに限定し、
  正常終了済みの旧 worker が後着 worker の登録を潰さないようにした。
  `src/llterm/host/loop.py` は `_QUERY_INJECTION_NEGATIVE` に
  `書く/書いて/書き込む/追記/挿入/差し替え/上書き/新規` を追加し、
  `status/progress/summary/summarize` は語境界ベースの regex で識別子内部一致を避けるよう締めた。
- **テスト補強**:
  `tests/test_codex_runner.py::test_codex_runner_interrupt_after_spawn_returns_interrupted`
  で post-spawn interrupt を固定。
  `tests/test_gui.py::test_common_summary_writer_finally_does_not_clobber_new_worker_registration`
  で後着 worker の registration が維持されることを固定。
  `tests/test_loop.py::test_query_like_injection_detector_is_conservative`
  に `get_status 関数を書いて` と `next_plan に追記して` を追加し、review gate へ落ちることを固定した。
- **検証**:
  `py -3.11 -m pytest -q tests/test_codex_runner.py tests/test_gui.py tests/test_loop.py tests/test_progress.py tests/test_orchestra_runner.py tests/test_loop.py`
  = **413 passed**。
  `ruff check src/llterm/host/codex_runner.py src/llterm/gui/app.py src/llterm/host/loop.py src/llterm/progress.py tests/test_codex_runner.py tests/test_gui.py tests/test_loop.py tests/test_progress.py tests/test_orchestra_runner.py --isolated --extend-ignore E702`
  = pass。

### 停止起因の空 `provider_version` は cache しないようにした (2026-07-10 02:39, 本セッション)
- **RAD 接地**: provenance は取れれば保持すべきだが、stop/interrupt で得られなかった観測値を
  durable cache へ確定値として残すべきではない。`re-observe vs cached replay` の既存知見どおり、
  停止で欠測した値は次回に再観測可能である方が closed-loop 的に正しい。
- **妥当指摘の反映**: `_provider_version()` は `cancel()` / `interrupt()` で probe を kill して
  空文字になった場合でも、その `""` を `_provider_version_cache` に永続化していた。
  これでは一度 stop が勝つと、同じ `CodexRunner` の以後の turn でも `--version` を再試行せず、
  telemetry provenance が恒久欠損になり得た。
- **実装**: `src/llterm/host/codex_runner.py` の `_provider_version()` は、
  probe 完了後に `_cancelled or _interrupted` を見て **停止起因の欠測**かどうかを判定し、
  その場合は空文字を返しても `_provider_version_cache` へは保存しないよう変更した。
  通常成功や通常失敗(実行不能など)の空文字は従来どおり cache する。
- **テスト補強**: `tests/test_codex_runner.py` に
  `test_interrupt_during_probe_does_not_poison_provider_version_cache` を追加し、
  slow probe を interrupt で止めた後の次ターンで fast probe を再実行し、
  `provider_version == "codex-cli recovered"` へ復帰することを固定した。
- **検証**: targeted `3 passed`、`py -3.11 -m pytest -q tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py -k "provider_version or interrupt or cancel or cumulative or codex or diagnostics"` = **44 passed**。
  `ruff check src/llterm/host/codex_runner.py src/llterm/host/loop.py src/llterm/host/orchestra_runner.py tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py --isolated --extend-ignore E702` = pass。

### 非 JSON 診断は既存 error text の後ろへ追記する形にした (2026-07-10 02:39, 本セッション)
- **妥当指摘の反映**: 失敗時の `agent/error text` が既にある場合でも、非 JSON 診断行を黙って捨てるのではなく、
  情報を落とさず一貫した形で `r.text` に残した方が観測性が高い。
- **実装**: `src/llterm/host/codex_runner.py` に `_append_diagnostic_text()` を追加し、
  `parse_codex_jsonl()` は `is_error=True` のとき、既存の `agent_text` / `error_text` を基底にして
  非 JSON 診断行(最大 4 行)を **重複なく末尾へ追記**するようにした。成功時は従来どおり非表示。
- **テスト補強**: `tests/test_codex_runner.py::test_parse_codex_appends_non_json_diagnostics_after_error_text`
  を追加し、`unexpected internal failure xyz` の後ろへ
  `fatal: backend disconnected unexpectedly` と `hint: retry with --verbose` が並んで残ることを固定した。

### 非 JSON 診断行は失敗時だけ補助テキストへ昇格するようにした (2026-07-10 02:34, 本セッション)
- **RAD 接地**: observability / evidence-synthesis 系の既存知見どおり、診断信号は成功 path を汚さず、
  失敗時だけ最小限の手掛かりとして露出する方がよい。分類契約と表示用 observability は分けて扱う。
- **背景**: `parse_codex_jsonl()` は probe で混ざる非 JSON 行を常に黙って捨てていた。
  成功時はそれでよいが、将来の codex 側が JSON event を出さずに非 JSON 診断だけ残した場合、
  `err=other` の原因テキストまで失って GUI / ledger の観測性が落ちる。
- **実装**: `src/llterm/host/codex_runner.py` で JSON decode failure 行を `diagnostic_lines` として別保持し、
  **成功時は従来どおり無視**、`is_error=True` かつ agent/error text が空のときだけ先頭数行を
  補助診断テキストへ昇格するようにした。rate-limit/auth の分類は引き続き制御チャネル
  (`error/turn.failed` + stderr) だけを見る。
- **テスト補強**: `tests/test_codex_runner.py` に
  `test_parse_codex_promotes_non_json_diagnostics_on_error` を追加し、JSON event が無い失敗でも
  非 JSON 診断が `r.text` へ出ることを固定した。既存の
  `test_parse_codex_ignores_non_json_probe_diagnostics` で成功時非表示も維持確認している。
- **検証**: targeted `4 passed`、`py -3.11 -m pytest -q tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py -k "provider_version or interrupt or cancel or cumulative or codex or diagnostics"` = **42 passed**。
  `ruff check src/llterm/host/codex_runner.py src/llterm/host/loop.py src/llterm/host/orchestra_runner.py tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py --isolated --extend-ignore E702` = pass。

### Codex version probe を cancel / interrupt から kill 可能にした (2026-07-10 02:30, 本セッション)
- **RAD 接地**: `loop_engineering_corpus_v2` の deadline / cancellation と provenance の指針どおり、
  provenance は auditability に有用でも、停止要求や緊急注入の fast-path を阻害してはいけない。
  version provenance は「取れれば載せる」補助情報に留め、executor-side 契約を優先する。
- **妥当指摘の反映**: 直前の修正では pre-start cancel 判定の後ろに `_provider_version()` の窓が残り、
  そこで `cancel()` / `interrupt()` が入ると `codex --version` 完了まで最大 10 秒待ち得た。
  `interrupt()` も pre-start で `_interrupted` を見ておらず、一発割り込み契約を満たせていなかった。
- **実装**: `src/llterm/host/codex_runner.py` に `_probe_proc` と `_provider_version_args()` を追加し、
  `provider_version` probe を **kill 可能な短命 subprocess** として扱うよう変更した。
  `cancel()` / `interrupt()` は `_proc` だけでなく `_probe_proc` も kill する。`run_turn()` は
  pre-start と probe 後の両方で `_cancelled` / `_interrupted` を再評価し、ここで止まった場合は
  `provider_version=""` のまま `cancelled` / `interrupted` を返す。
- **コメント補強**: `_provider_version()` の docstring に、version provenance は再現条件運搬には有用だが、
  pre-start の stop / 緊急注入が来たら空文字へ落としてでも非ブロッキング性を優先すると明記した。
- **テスト補強**: `tests/test_codex_runner.py` に
  `test_codex_runner_cancel_during_provider_version_probe_returns_fast` と
  `test_codex_runner_interrupt_during_provider_version_probe_returns_fast` を追加し、
  長時間 version probe 中でも main codex process を spawn せず即座に戻ることを固定した。
- **検証**: targeted `4 passed`、`py -3.11 -m pytest -q tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py -k "provider_version or interrupt or cancel or cumulative or codex"` = **41 passed**。
  `ruff check src/llterm/host/codex_runner.py src/llterm/host/loop.py src/llterm/host/orchestra_runner.py tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py --isolated --extend-ignore E702` = pass。

### Codex cancel fast-path を外部プロセス非起動へ戻した (2026-07-10 02:25, 本セッション)
- **RAD 接地**: `loop_engineering_corpus_v2` の deadline / cancellation 指針どおり、cancel fast-path の前に
  外部 I/O や子プロセス起動を置かない方が安全。provenance は有用でも、停止要求より優先してはいけない。
- **妥当指摘の反映**: `CodexRunner.run_turn()` は `provider_version` 導入後、`_cancelled` を見る前に
  `self._provider_version()` を必ず呼んでおり、`cancel()` 済みでも `codex --version` が最大 10 秒程度
  ブロックし得る回帰になっていた。
- **実装**: `src/llterm/host/codex_runner.py` で pre-start cancel 判定を `run_turn()` の最上流へ戻し、
  その経路では `provider_version=""` のまま即 `cancelled` を返すよう修正した。起動後 cancel /
  interrupted / timeout / not_found など、**すでに turn を開始した経路**では従来どおり
  `provider_version` を保持する。
- **テスト補強**: `tests/test_codex_runner.py::test_codex_runner_cancel_before_start_is_sticky` は
  `_provider_version()` が呼ばれたら失敗する形へ強化し、pre-start cancel が外部プロセス非起動で
  返ることを固定した。
- **検証**: `py -3.11 -m pytest -q tests/test_codex_runner.py -k "cancel_before_start_is_sticky or parse_codex_success or ignores_non_json_probe_diagnostics"` = **3 passed**。
  `ruff check src/llterm/host/codex_runner.py tests/test_codex_runner.py --isolated` = pass。

### `llterm.progress` CLI 直後の stale read は隔離再現しなかった (2026-07-10 02:19, 本セッション)
- **RAD 接地**: stale snapshot / concurrent overwrite 系の既存知見どおり、commit 経路と read 経路は
  分けて観測する。`hacker_corpus_v2` の OpenClaw 既知例も、`read-modify-write` の stale 上書き回避と
  `temp in verified parent + atomic replace` の pinning を重視しており、今回の `progress.py` hardening 方針と一致した。
- **実測 1**: 実ファイル `D:/projects/_shared/PROGRESS.md` は、`py -3.11 -m llterm.progress --projects-root D:/projects`
  実行前後とも Python read / PowerShell `Get-Content` の先頭行が一致し、同じ `llterm` 先頭行を返した。
- **実測 2**: 隔離した一時 `projects_root` を作り、`alpha/docs/next_plan.md` の `最終更新` を
  `2026-07-10 06:30` から `06:35` まで 6 回進めつつ、**毎回別プロセス**の `py -3.11 -m llterm.progress --projects-root <tmp>`
  実行直後に PowerShell `Get-Content` を読む probe を行った。6/6 回とも即座に最新分を読み、stale は再現しなかった。
- **判断**: 現時点では `write_common_summary()` の commit 経路に追加修正を入れる根拠は弱い。
  `progress.py` はいったん収束扱いに戻し、実運用で stale 実例が再発したときだけ
  `CLI 実行経路 / shell 側 readback / キャッシュ` のどこかへ観測を足す。

### Codex `--json` を 0.135.0 で再probeしても per-call 占有 event は無かった (2026-07-10 02:19, 本セッション)
- **RAD 接地**: telemetry は provenance と contract を明示し、観測できない値を推定で埋めない方が安全。
  `loop_engineering_corpus_v2` / `llm_corpus_v2` の provenance-aware 契約と同様、今回も
  `provider-managed cumulative` を保ったまま現物 schema を再確認した。
- **実測**: `codex-cli 0.135.0` に対して、隔離 temp dir で
  `codex exec --json --sandbox read-only --skip-git-repo-check -C <tmp> --ephemeral "Reply with exactly OK."`
  を実行。得られた event は `thread.started` / `turn.started` / `item.completed(agent_message)` /
  `turn.completed.usage` だけで、`usage` は `input/cached/output/reasoning_output_tokens` を含む一方、
  per-request / span / instantaneous context occupancy に相当する公開 field は出なかった。
- **追補**: 同 probe の stdout 末尾には `Reading additional input from stdin...` という**非 JSON 診断行**が
  1 行混ざった。`parse_codex_jsonl()` は元から JSON decode failure を黙って捨てるため runtime は問題ないが、
  実測形式を回帰に固定するためテストを追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_codex_runner.py -k "parse_codex_success or ignores_non_json_probe_diagnostics"` = **2 passed**。
  `ruff check src/llterm/host/codex_runner.py tests/test_codex_runner.py --isolated` = pass。

### Codex telemetry に CLI version を載せた (2026-07-10 04:26, 本セッション)
- **RAD 接地**: stale / cumulative telemetry は、値だけでなく**どの provider 実装・どの CLI で観測したか**
  を運ぶ方が後続の切り分けに強い。`Executor-Side Progressive Risk-Gated Actuation` 系の
  evidence-gated 契約と同様、観測値の provenance を payload に寄せる。
- **実装**: `TurnResult` に `provider_version` を追加し、`SessionLoop` の `turn` event へ emit。
  `CodexRunner` は `codex --version` を 1 回だけ取得してキャッシュし、Codex turn の telemetry に
  `codex-cli 0.xxx.x` を載せるようにした。取得失敗で空文字になった場合も sentinel cache で
  **再実行は 1 回で止める**。また `interrupted / timeout / not_found` の早期 return と、**起動後**の
  `cancelled` return には `provider_version` を載せるよう揃えた。pre-start cancel だけは
  非ブロッキング性を優先して空文字のまま返す。`OrchestraRunner(CodexRunner)` 経由でも metadata が
  落ちないよう passthrough を追加している。
- **狙い**: docs を読まなくても、ledger / GUI 購読側 / 将来の telemetry consumer から
  「この `cumulative_only` 観測はどの Codex CLI で見たか」を直接辿れる。
- **検証**: targeted `4 passed`、`py -3.11 -m pytest -q tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py`
  = **157 passed**、`ruff check src/llterm/host/codex_runner.py src/llterm/host/loop.py src/llterm/host/orchestra_runner.py tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py --isolated --extend-ignore E702` = pass。
- **GUI 可視化の追加**: `provider_version` は token 欄の tooltip にも出すようにした。status row の
  表示文自体は増やさず、再現条件だけ hover で辿れる。`session_start` では provenance tooltip を保持し、
  run start / finish のみ base 説明へ戻すため、同一 run 中の session rotate でも観測元 version を追える。
  cumulative tooltip 文言も provider 固有の「Codex は...」から、provider-managed cumulative usage を
  説明する汎用文へ変更した。`gui.tip.tokens.version` の日本語も `プロバイダ版: ...` に揃えている。
- **検証 2**: `py -3.11 -m pytest -q tests/test_gui.py -k "token_label_distinguishes_codex_cumulative_usage or cumulative_tooltip_is_provider_generic or tokens_reset_on_session_start or turn_event_shows_codex_cumulative_tokens_with_cache or gui_shows_cumulative_codex_tokens_from_orchestra_turn_event"` = **5 passed**、
  `ruff check src/llterm/gui/app.py src/llterm/i18n/messages.py tests/test_gui.py --isolated` = pass。

### 共通進捗の書込み経路を temp+replace fallback にした (2026-07-10 04:42, 本セッション)
- **背景**: `build_common_summary()` は最新 `next_plan` を読めているのに、`py -3.11 -m llterm.progress` 後の
  `_shared/PROGRESS.md` が stale のまま見えるケースがあった。従来の `write_common_summary()` は
  `out_path.write_text(...)` を `OSError` ごと握り潰すだけで、書込み経路の一時失敗があると stale file を
  黙って残し得た。
- **実装**: `write_common_summary()` を `tmp write -> Path.replace(out) -> direct write fallback` の順に変更。
  その後、direct truncate fallback は破損優先になり得るため撤去し、現在は
  **`_commit_summary_text()` = `tmp write -> replace -> parent dir fsync`** の経路だけで commit する。
  さらに fixed `PROGRESS.md.tmp` はやめ、writer ごと一意な `PROGRESS.md.<uuid>.tmp` を使うようにして、
  複数 llterm / 複数プロセスでの tmp 衝突を避けた。`replace` と fallback write が両方失敗した場合でも、
  `finally` で tmp を掃除してゴミを残さない。書込み自体も helper 経由の `flush + fsync` へ揃え、
  親ディレクトリエントリの fsync も best-effort で試みる。
- **追補**: read-after-write mismatch は引き続き自己検知するが、**再上書きはしない**方針へ戻した。
  並行 writer がすでにより新しい `PROGRESS.md` を `replace()` 済みの可能性があり、ここで古い writer が
  `out_path.write_text(...)` を打つと新しい内容を stale 側へ巻き戻し得るためである。現在は mismatch を
  検知だけして fail-closed に留める。
- **追補 2**: 実測では「単独 writer なのに readback が古いまま残る」ケースもあったため、
  mismatch 時は**現在ファイルの最新時刻が期待 summary より古い場合だけ** `out_path.write_text(...)` を
  1 回だけ再試行するようにした。これで単独 stale は補正しつつ、並行 writer が既により新しい summary を
  commit 済みなら潰さない。
- **追補 3**: さらに「最新時刻は同じだが内容だけ stale」なケースに備え、readback mismatch 時は
  **現在の source 群から再集約した本文**も比較するようにした。いまの source 群から作り直した本文が
  元の期待値と一致する場合だけ再書込みするため、same-minute stale は補正でき、同じ分内で他 writer が
  source を進めていた場合は上書きしない。
- **テスト補強**: `tests/test_progress.py::test_write_common_summary_falls_back_when_replace_fails` に加え、
  `test_common_summary_tmp_path_is_unique`、
  `test_write_text_sync_flushes_and_fsyncs` と
  `test_commit_summary_text_fsyncs_parent_dir`、
  `test_write_common_summary_cleans_tmp_when_replace_and_fallback_fail`、
  `test_write_common_summary_does_not_clobber_newer_readback_mismatch` と
  `test_write_common_summary_rewrites_when_readback_is_older`、
  `test_write_common_summary_rewrites_when_readback_is_same_minute_but_stale` を追加し、
  一意 tmp 名 / double-failure 後の cleanup / readback mismatch 時に**新しい writer を潰さない**ことと、
  **古い内容だけを条件付きで補正する**ことを固定した。
- **検証**: targeted `7 passed`、`py -3.11 -m pytest -q tests/test_progress.py` = **88 passed**、
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### `_default_fmt()` も JST 固定に揃えた (2026-07-10 04:10, 本セッション)
- **妥当指摘の反映 1**: `parse_updated_at()` は `最終更新` を JST 固定で epoch 化するのに、
  `build_common_summary()` の既定 formatter `_default_fmt()` は `datetime.fromtimestamp()` の
  ローカルタイム表示だった。JST 以外のホストで再生成すると、記録値は JST 基準なのに表示だけ
  現地時刻へずれる不整合があった。
- **実装**: `_default_fmt()` を `datetime.fromtimestamp(epoch, tz=_JST)` へ変更し、
  共通進捗の表示も解析側と同じ JST 固定に統一した。
- **テスト補強**: `tests/test_progress.py` に `test_default_fmt_uses_jst` を追加し、
  formatter 自体が JST 基準の表示を返すことを固定した。
- **検証**: targeted `4 passed`、`py -3.11 -m pytest -q tests/test_progress.py` = **80 passed**、
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### `SESSION_SUMMARY` の `- **最終更新**:` も並び順の正に戻した (2026-07-10 04:02, 本セッション)
- **妥当指摘の反映**: `parse_updated_at()` が `candidate.startswith("最終更新")` 前提になっており、
  `SESSION_SUMMARY` 標準メタデータ行 `- **最終更新**: ...` を拾えなくなっていた。
  その結果 `collect_progress()` で `session_summary` source が mtime fallback に落ち、
  共通進捗の順序が git checkout / auto-commit 後に不安定化し得た。
- **実装**: `parse_updated_at()` は quote 正規化後の行が plain `最終更新...` でなければ
  `_SESSION_FIELD_RE` も見て、`最終更新` field なら `最終更新: ...` へ正規化して同じ parser に通すよう修正。
- **テスト補強**: `tests/test_progress.py` に
  `test_parse_updated_at_accepts_session_summary_metadata_line` と
  `test_collect_progress_uses_session_summary_recorded_timestamp` を追加し、
  `session_summary` source でも header timestamp が並び順の正になることを固定した。
- **検証**: targeted `4 passed`、`py -3.11 -m pytest -q tests/test_progress.py` = **79 passed**、
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### `parse_updated_at()` の探索範囲と scaffold default path の root 連動を修正 (2026-07-10 03:00, 本セッション)
- **妥当指摘の反映**: `parse_updated_at()` が `最終更新` を先頭 12 行に限定していたため、
  13 行目以降の正規ヘッダを mtime fallback 扱いにしていた。あわせて
  `scaffold_next_plan_text()` の default `project path` は `D:/projects/...` 直書きで、
  `DEFAULT_PROJECTS_ROOT` と分離していた。
- **実装**: `progress.py` で `parse_updated_at()` は code fence 外の全行を走査するよう修正し、
  dedicated な `最終更新` 行なら位置に関係なく採用するようにした。
  scaffold の default path は `f"D:/projects/{project_name}"` から
  `(DEFAULT_PROJECTS_ROOT / project_name).as_posix()` へ置換した。
- **テスト補強**: `tests/test_progress.py` に
  `最終更新` が 13 行目以降でも解釈される回帰と、default project path が
  `DEFAULT_PROJECTS_ROOT` 由来であることを固定する回帰を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "beyond_line_twelve or default_projects_root_fallback or explicit_project_path_fallback or date_only_is_none"` = **4 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py` = **68 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### `SESSION_SUMMARY` metadata block の解釈を先頭連続 field 行へ限定 (2026-07-10 03:08, 本セッション)
- **妥当性**: `- **...**:` を「最初の見出しまで」metadata とみなすと、前置き本文の後に現れる
  `- **README**:` 形式の通常 bullet まで stale metadata として誤解釈し得る。header block は
  H1 タイトル直後の連続 field 行だけに閉じた方が blast radius が小さい。
- **実装**: `parse_session_summary_seed()` に header-block gate を追加し、metadata 解析は
  `# Session Summary` の直後から、最初の非空・非field・非H1 行が出るまでに限定した。
  これ以降は pre-heading でも metadata を再開しない。
- **テスト補強**: `tests/test_progress.py` に、前置き本文の後ろに来る
  `- **プロジェクト**:` を metadata として再解釈しない回帰を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "reopen_metadata_after_preamble_text or extracts_metadata or keeps_bold_label_bullets_inside_sections"` = **4 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py` = **69 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **統合指示反映**: auto-generated / manual override の実データには、
  H1 の直後に `> 自動生成...` / `> 次回...` の引用前置きが入る。これらは metadata block を閉じずに
  neutral preamble として許可し、その後ろの `- **最終更新**:` / `- **プロジェクト**:` /
  `- **実レビュー対象**:` / `- **ブランチ**:` は引き続き拾うように修正した。
- **追試**: `py -3.11 -m pytest -q tests/test_progress.py -k "quoted_preamble_before_metadata or quoted_preamble_before_manual_metadata or reopen_metadata_after_preamble_text"` = **3 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py` = **71 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### `parse_updated_at()` は最初の `##` までのヘッダ部だけを見る (2026-07-10 03:18, 本セッション)
- **妥当性**: `最終更新` を全文走査すると、本文セクション内の引用ログや過去テンプレ断片にある
  `> 最終更新: ...` まで正本更新時刻と誤認し得る。必要なのは next_plan ヘッダ部の dedicated 行だけ。
- **実装**: `parse_updated_at()` は code fence 外を走査しつつ、最初の `## ` が出た時点で探索を止めるようにした。
  これにより、H1 下の quoted preamble や 13 行目以降の正規更新行は許容しつつ、
  本文セクション内の履歴引用は無視する。
- **テスト補強**: `tests/test_progress.py` に、quoted preamble の後ろの正規 `最終更新` は拾う一方、
  最初の `##` 以降に出る quoted historical `最終更新` は採用しない回帰を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "quoted_preamble_before_header_update or historical_update_after_first_section or beyond_line_twelve or body_mentions_and_code_fences"` = **4 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py` = **73 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **統合指示反映**: Markdown 引用の `>最終更新` / `>> 最終更新` も従来どおり受理するよう、
  引用接頭辞の除去を `> ` 固定から `>` 段数非依存へ広げた。
- **追試**: `py -3.11 -m pytest -q tests/test_progress.py -k "quoted_header_without_space or nested_quote_prefix or quoted_preamble_before_header_update or historical_update_after_first_section"` = **4 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py` = **75 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **統合指示反映 2**: quoted heading `> ## 過去ログ` でも探索が止まるよう、`##` 境界判定は
  引用接頭辞を正規化した後に行うよう修正した。あわせて引用剥がしは `> > 最終更新: ...` のような
  空白入り nested quote まで処理する helper に統一した。
- **追試 2**: `py -3.11 -m pytest -q tests/test_progress.py -k "nested_quote_prefix_with_spaces or stops_at_quoted_section_heading or nested_quote_prefix or historical_update_after_first_section"` = **4 passed**。
  `py -3.11 -m pytest -q tests/test_progress.py` = **77 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### Codex reasoning token の可視化 (2026-07-10 00:06, 本セッション)
- **実測**: `codex exec --json` の `turn.completed.usage` に `reasoning_output_tokens` が含まれることを確認。
  public telemetry では per-call / span 占有は依然見えないが、累積 usage の内訳としては拾える。
- **配線**: `TurnResult` に `reasoning_output_tokens` を追加し、`codex_runner.py` で parse、
  `orchestra_runner.py` で保持、`SessionLoop` の `turn` event へ emit するよう変更した。
- **GUI**: token 補助表示を `累積tok in/cache/reason/out: ... (ctx別)` に更新し、
  `ctx%` と混ぜずに Codex の reasoning 使用量を見せる形にした。
- **検証**: `py -3.11 -m pytest -q tests/test_codex_runner.py::test_parse_codex_success
  tests/test_codex_runner.py::test_parse_codex_huge_cumulative_usage_does_not_overcount
  tests/test_loop.py::test_turn_event_preserves_cumulative_token_metadata_through_orchestra
  tests/test_loop.py::test_on_event_emits_progress
  tests/test_gui.py::test_token_label_distinguishes_codex_cumulative_usage
  tests/test_gui.py::test_turn_event_shows_codex_cumulative_tokens_with_cache
  tests/test_gui.py::test_gui_shows_cumulative_codex_tokens_from_orchestra_turn_event
  tests/test_orchestra_runner.py::test_final_result_preserves_cached_tokens_and_usage_kind
  tests/test_orchestra_runner.py::test_interrupted_result_preserves_cached_tokens_and_usage_kind`
  = **9 passed**。`ruff check src/llterm/host/loop.py src/llterm/host/codex_runner.py
  src/llterm/host/orchestra_runner.py src/llterm/gui/app.py src/llterm/i18n/messages.py
  tests/test_codex_runner.py tests/test_loop.py tests/test_gui.py tests/test_orchestra_runner.py
  --isolated --extend-ignore E702` = pass。
- **統合指示反映**: この branch には reasoning token 差分以前の累積変更
  (`progress.py`, 共通サマリ UI, worker 寿命管理, provider/lead 選定など) も同居している。
  したがって上の `9 passed` は**最新差分の限定検証**として扱い、別途
  `py -3.11 -m pytest -q tests/test_progress.py tests/test_codex_runner.py tests/test_loop.py
  tests/test_gui.py tests/test_orchestra_runner.py` = **346 passed** を実行して、
  変更ファイル群全体の回帰も確認した。`ruff check src/llterm/progress.py
  src/llterm/host/codex_runner.py src/llterm/host/loop.py src/llterm/host/orchestra_runner.py
  src/llterm/gui/app.py src/llterm/i18n/messages.py tests/test_progress.py
  tests/test_codex_runner.py tests/test_loop.py tests/test_gui.py tests/test_orchestra_runner.py
  --isolated --extend-ignore E702` も pass。

### `SESSION_SUMMARY` 由来 scaffold の practical 化を一段追加 (2026-07-10 00:18, 本セッション)
- **RAD 接地**: `Context Before Code` / constraint-aware scaffolding 系の示唆どおり、再開用 scaffold は
  「最小」だけでなく、環境制約と起動手掛かりを少量継ぐ方が実運用で強い。一方で stale 面積は
  広げすぎないよう、`起動方法` / `環境メモ` の non-fenced 行だけを bounded に拾う方針にした。
- **実装**: `progress.py` の `SessionSummarySeed` に `environment_notes` を追加。
  `parse_session_summary_seed()` は `## 起動方法` / `## 環境メモ` 節の bullet と plain line を
  最大 4 件まで抽出し、`scaffold_next_plan_text()` はその 4 件を `## 環境メモ` へ差し込む。
  code fence 内の長い手順や実行ログは継がず、stale/過剰転記を避ける。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "extracts_environment_notes or carries_environment_notes or scaffold_next_plan_text_creates_canonical_sections or parse_session_summary_seed_extracts_metadata"` = **4 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **統合指示反映**: `latest_heading` は `起動方法` / `環境メモ` を候補から除外し、
  最初の実質セクション見出しだけを採るよう修正した。これで `## 現在地` に環境見出しが誤入しない。
  あわせて `environment_notes[:3]` の取りこぼしを直し、4 件入力時も 4 件とも scaffold へ残る。
- **追試**: `py -3.11 -m pytest -q tests/test_progress.py -k "skips_environment_heading_for_latest_heading or keeps_four_environment_notes or extracts_environment_notes or carries_environment_notes or scaffold_next_plan_text_creates_canonical_sections"` = **5 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **継続改善**: 5 件目以降の環境メモは silent drop せず、`環境メモの追加 N 件は未転記
  (stale/過剰転記防止のため上限 4 件)` を scaffold に出すようにした。bounded scaffold の方針は維持しつつ、
  情報が落ちた事実だけは見える化する。
- **追試 2**: `py -3.11 -m pytest -q tests/test_progress.py -k "omitted_environment_notes or keeps_four_environment_notes or extracts_environment_notes or scaffold_next_plan_text_marks_omitted_environment_notes"` = **4 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **統合指示反映 2**: `environment_notes_omitted` は「新規 5 件目以降」だけでなく、
  **保持済み 4 件の重複が上限超過側へ来たケース**も omitted 件数に含めるよう修正した。
  上限 `4` は `_MAX_ENVIRONMENT_NOTES` へ定数化し、scaffold 側の omitted 注記も同じ定数を参照する。
- **追試 3**: `py -3.11 -m pytest -q tests/test_progress.py -k "omitted_environment_notes or duplicate_omitted_environment_notes or marks_omitted_environment_notes or keeps_four_environment_notes"` = **4 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **実データ追従 3**: manual EXIT prep override 系の `SESSION_SUMMARY` が使う
  `- **作業プロジェクト**:` と `## 次の具体的一手` も parser が受理するようにした。
  これで `onocollo-complete` 型の手動 summary でも scaffold の `次の一手` が fallback に落ちない。
- **実データ追従 4**: auto-summary の `## 直近の git log` / `## 現在の git status` /
  `## 直近 2 時間に変更されたファイル` は `latest_heading` 候補から除外した。
  単なる運搬用 section を `現在地` の「最新セクション見出し」と誤認させない。
- **追試 4**: `py -3.11 -m pytest -q tests/test_progress.py -k "concrete_next_step_heading or project_alias or noisy_git_headings or creates_canonical_sections or parse_session_summary_seed_extracts_metadata"` = **5 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **統合指示反映 5**: `SESSION_SUMMARY` の metadata 行は ASCII `:` だけでなく全角 `：` も
  `_SESSION_FIELD_RE` で受理するよう修正した。`- **作業プロジェクト**： ...` 形式でも
  `project_label` / `updated_label` / branch alias が取りこぼれない。
- **追試 5**: `作業プロジェクト` + `次の具体的一手` + noisy git headings が同居する
  実データ寄りケースを 1 本追加した。`py -3.11 -m pytest -q
  tests/test_progress.py::test_parse_session_summary_seed_accepts_concrete_next_step_heading
  tests/test_progress.py::test_parse_session_summary_seed_skips_noisy_git_headings_for_latest_heading
  tests/test_progress.py::test_scaffold_next_plan_text_uses_project_alias_and_concrete_next_step_heading
  tests/test_progress.py::test_scaffold_next_plan_text_handles_real_data_style_alias_heading_and_noise_together`
  = **4 passed**。`ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **実データ追従 5**: manual EXIT prep override に出る `- **実レビュー対象**:` も
  `SessionSummarySeed.review_target_label` として保持し、scaffold の `## 現在地` に
  `実レビュー対象: ...` を差し込むようにした。review project と live target がズレるケースでも
  再開時の文脈が落ちない。
- **追試 6**: `py -3.11 -m pytest -q
  tests/test_progress.py::test_parse_session_summary_seed_extracts_metadata
  tests/test_progress.py::test_parse_session_summary_seed_accepts_concrete_next_step_heading
  tests/test_progress.py::test_scaffold_next_plan_text_uses_project_alias_and_concrete_next_step_heading
  tests/test_progress.py::test_scaffold_next_plan_text_handles_real_data_style_alias_heading_and_noise_together`
  = **4 passed**。`ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **実データ追従 6**: manual review 系 summary の `## 確定 Findings` からも、
  先頭 3 件だけを `finding_highlights` として抽出するようにした。scaffold の `## 直近の成果` は
  `今回の要点` bullet に重複しない範囲でこれを追記するため、review continuation で severity 上位の
  finding 文脈が落ちにくい。bounded carryover の上限は 3 件に固定し、過剰転記は避ける。
- **追試 7**: `py -3.11 -m pytest -q
  tests/test_progress.py::test_parse_session_summary_seed_extracts_metadata
  tests/test_progress.py::test_parse_session_summary_seed_accepts_concrete_next_step_heading
  tests/test_progress.py::test_parse_session_summary_seed_extracts_finding_highlights
  tests/test_progress.py::test_scaffold_next_plan_text_uses_project_alias_and_concrete_next_step_heading
  tests/test_progress.py::test_scaffold_next_plan_text_carries_finding_highlights
  tests/test_progress.py::test_scaffold_next_plan_text_handles_real_data_style_alias_heading_and_noise_together`
  = **6 passed**。`ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **統合指示反映 6**: `直近の成果` への carryover 上限を、`latest_bullets` 3 件 +
  `finding_highlights` 3 件の **合計 6 件**へ明示化した。これで `今回の要点` が 2 件以上ある
  実データでも、2 件目以降の finding が `len(recent_results) < 4` で静かに落ちない。
- **実データ追従の整理**: `SESSION_SUMMARY` scaffold はこのセッションで、
  1) `- **...**:` を header block 限定の metadata 解釈に絞り、section 本文の `**置換**` / `**README**` /
     `**テスト**` や `起動方法` 内の `**テンプレ(機能別)**` / `**RAD 連携**` を壊さず保持するよう修正、
  2) `## 除外済みメモ` を `exclusion_highlights` として bounded carryover しつつ `latest_heading`
     候補から除外、
  3) scaffold の `project path` / `初期 scaffold 元` は summary 側 metadata より**実 `project_dir`**
     を優先するよう修正、
  まで反映済み。
- **対応する targeted 検証**:
  - bold-label section bullet 保持: **5 passed**
  - `除外済みメモ` carryover + `latest_heading` 除外: **4 passed**
  - 実 `project_dir` 優先 (`metadata missing` / `stale metadata`): **4 passed**
  いずれも `ruff check src/llterm/progress.py tests/test_progress.py --isolated` は pass。
- **`tests/test_progress.py` の状態整理**: この節を書いた時点では単体ファイルの全体回帰は **62 passed** だった。
  旧値の `57/59/61 passed` はさらに前の通過数であり、この節では当時点値としてのみ扱う。
  **現在 handoff が参照すべき最新値は冒頭/環境メモに記した `80 passed`**。
- **広域回帰との関係**: 先頭で記録した **346 passed** は `progress.py` だけでなく
  `tests/test_codex_runner.py` / `tests/test_loop.py` / `tests/test_gui.py` /
  `tests/test_orchestra_runner.py` を含む**変更ファイル群全体**の回帰値であり、
  この時点の `62 passed` は **`tests/test_progress.py` 単体**の当時点全体回帰値。
- **RAD 再確認**: `loop_engineering_corpus_v2` の `fail-safe defaults` / `evidence-gated rollback`
  系ノートに沿って、今回の parser は「section 文脈が不明なら metadata と見なさない」「stale metadata より
  live `project_dir` を優先する」方向で blast radius を局所化している。
- **stale 境界の追加整理**: `branch_label` が無い summary では scaffold に
  `branch は \`不明\`` / `直近 branch: \`不明\`` を出さないようにした。`branch` は stale 化しやすい割に、
  不明値だと再開判断のノイズしか増やさないため。known branch があるときだけ `現在地/環境メモ` に出す。
- **追試 12**: `py -3.11 -m pytest -q tests/test_progress.py -k
  "unknown_branch_lines or carries_environment_notes or uses_project_alias_and_concrete_next_step_heading or project_path_fallback"`
  = **4 passed**。`ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **統合指示反映 10**: unknown sentinel を branch なし扱いへ正規化した。`- **ブランチ**: \`不明\`` や
  `- **ブランチ**: \`unknown\`` のように summary 側が unknown を明示していても、
  scaffold では `branch は ...` / `直近 branch: ...` を再出力しない。
- **追試 13**: `py -3.11 -m pytest -q tests/test_progress.py -k
  "unknown_branch_lines or english_unknown_branch_sentinel or uses_project_alias_and_concrete_next_step_heading"`
  = **3 passed**。`ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **統合指示反映 11**: unknown sentinel の正規化を `branch` だけでなく
  `project_label` / `review_target_label` にも横展開した。`- **実レビュー対象**: \`不明\`` や
  `- **プロジェクト**: \`unknown\`` のような値は carryover せず、review target は非表示、
  project path は explicit `project_dir` / fallback path を優先する。
- **追試 14**: `py -3.11 -m pytest -q tests/test_progress.py -k
  "unknown_branch_lines or english_unknown_branch_sentinel or unknown_review_target_sentinel or unknown_project_label_to_explicit_path or uses_project_alias_and_concrete_next_step_heading"`
  = **5 passed**。`ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **全体回帰 4**: unknown sentinel 正規化を `project/review_target/branch` へ横展開した後、
  `py -3.11 -m pytest -q tests/test_progress.py` を再実行し **66 passed** を確認。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` も継続 pass。

### Codex per-call 占有の再probe (2026-07-10 01:22, 本セッション)
- **再現条件**: 観測時の CLI は `codex-cli 0.135.0` (`codex --version`)。
- `codex exec --json "Respond with exactly OK."` を再実行。観測された event は
  `thread.started / turn.started / item.completed(agent_message) / turn.completed(usage)` のみで、
  やはり per-request / span / instantaneous occupancy に相当する公開 event は出なかった。
- `codex exec --json "Run pwd and then reply with exactly OK."` でも追加で見えたのは
  `item.started(item.type=command_execution)` と `item.completed(command_execution)` までで、
  token usage は依然 `turn.completed.usage` の累積のみだった。
- `codex debug --help` / `codex features` も確認したが、現行 CLI には `exec --json` の event 粒度を
  さらに細かくする公開フラグや feature は見当たらなかった。つまりこの観測時点での公開観測面は
  `thread/turn/item/usage` レベルまでで打ち止め。
- 現在も結論は維持: **Codex CLI の公開 JSONL からは turn 内部 API 往復ごとの usage は取得できず、
  llterm 側では `ctx n/a` + cumulative usage 表示が妥当**。将来バージョンで event schema が増えた時だけ
  再度 instrumentation を検討する。

### 統合修正: `確定 Findings` も latest heading 候補から除外 (2026-07-10 01:31, 本セッション)
- `finding_highlights` は carryover 対象だが、`最新セクション見出し` としては不適切なので、
  `parse_session_summary_seed()` の `latest_heading` 選定から `## 確定 Findings` も除外した。
  これで review summary が findings から始まっても、scaffold の `現在地` に
  `最新セクション見出し: \`確定 Findings\`` が紛れ込まない。
- `py -3.11 -m pytest -q
  tests/test_progress.py::test_parse_session_summary_seed_extracts_finding_highlights
  tests/test_progress.py::test_parse_session_summary_seed_skips_findings_heading_for_latest_heading
  tests/test_progress.py::test_scaffold_next_plan_text_carries_finding_highlights`
  = **3 passed**。`ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- **統合指示反映 8**: 症状ベースの回帰として、`## 確定 Findings` が最初の実質見出しで
  そのまま `## 次の具体的一手` に続くケースでも、scaffold 出力に
  `最新セクション見出し: \`確定 Findings\`` が出ないことを追加で固定した。
- **追試 11**: `py -3.11 -m pytest -q
  tests/test_progress.py::test_parse_session_summary_seed_skips_findings_heading_for_latest_heading
  tests/test_progress.py::test_scaffold_next_plan_text_does_not_show_findings_as_latest_heading`
  = **2 passed**。`ruff check tests/test_progress.py --isolated` = pass。
- **回帰確認 (当時点)**: incremental 追記が多くなった時点で `py -3.11 -m pytest -q tests/test_progress.py`
  を全体実行し、**57 passed** を確認していた。以後さらに回帰テストを追加し、この節の時点では
  **62 passed**、その後さらに積み増して**現在の最新全体回帰値は `80 passed`**。

### ループ不具合の根治(5 原因・全て実 CLI で実証 + テスト)
1. claude が PATH 不在で FileNotFound → `ClaudeRunner` が `~/.local/bin` を PATH 非依存解決。
2. `claude --session-id` の UUID 厳格 + **再利用衝突** → レビュー系は毎回フレッシュ uuid4 に写像。
3. `codex exec resume` が `-s/-C/--color` 非対応で exit 2 → resume 引数を修正 + `-c sandbox_mode` 付与。
4. codex が workspace-write では Windows で書けない → 既定 `danger-full-access`(new=-s / resume=-c sandbox_mode)。
5. 起動不可を `error_kind="unavailable"` 化 → 別プロバイダへ即フォールバック / 無ければ明示停止。

### 進捗引継ぎの集約
- `llterm/progress.py` + CLI `llterm-progress`(= `py -3.11 -m llterm.progress`)。
- 各 project の `docs/next_plan.md`(正本)を集約 → `D:/projects/_shared/PROGRESS.md`(ヘッダ=project→最新更新日の新しい順、本文=各 project 全文・更新日時付き)。

### HITL autonomy モデル
- 「承認確認不要」を**メイン画面のライブトグル**化(走行中も ON/OFF 可)。`autonomy_fn` で**毎ターン動的評価**。
- **注入で自動 OFF(監督モード)** / **確認回答後に自動 ON(ループ復帰)**。注入はキュー積み。
- **安全弁(常時・autonomy 不問)**: 不可逆/危険操作は ①next_plan.md 更新 → ②`⟦LLTERM_CHOICE⟧` で承認 → ③回答後に決定要約を next_plan.md へ追記、を必須化。

### ctx 過大計上 (2549%) の是正 (2026-06-13 17:10、本セッション・残課題を解消)
- **原因**: Codex の `turn.completed.usage` は 1 ターン内の全内部 API 往復の **累積**(各往復で文脈を再送 →
  N×文脈)。`input+cached ≈ 5.1M = 窓 200k の 2549%`(物理的に窓超え=累積の証拠)。これを占有率にすると
  毎ターン閾値超で rotate し 1 セッション=1 ターンに縮退していた。
- **修正**: `parse_codex_jsonl` の `context_tokens` を **0 固定**(codex は exec resume で自前圧縮するため
  占有ベース rotate 不要 → turn 数 `max_turns_per_session=50` で rotate)。`used_pct` を **[0,1] にクランプ**
  (どの runner の累積 usage が紛れても rotate 判定を壊さない防御)。input/output tokens は情報として保持。
- 検証: **全 424 テスト pass**。codex_runner は ruff/mypy クリーン。
- ★ 表示の注記: orchestra/codex のターンは ctx% が低く (≒0%) 出る = 「codex 自前管理」の正しい挙動。
  進捗は session/turn カウンタと出力ログで追える。codex の累積トークンを「今ターンの使用量」として
  別表示したい場合は GUI 拡張で対応可 (任意・占有率とは別欄)。

### 共通サマリ自動更新トリガ + exit-prep 標準化 (2026-07-09 21:34、本セッション)
- **共通進捗の自動更新**: `progress.py` に `infer_common_summary_paths()` /
  `refresh_common_summary_for_project()` を追加。project 配下の任意パスから親方向に
  progress 正本を持つ root を推定し、`_shared/PROGRESS.md` を fail-safe に再生成できるようにした。
- **loop 配線**: `SessionLoop` が **rotate 後 / graceful Stop の handoff 後**に
  `refresh_common_summary_for_project(self.workdir)` を呼び、`docs/next_plan.md` の更新を
  共通進捗へ決定論的に反映するようにした。別途スケジューラを組まなくても、ループ境界だけで同期される。
- **exit-prep 指示の標準化**: `DEFAULT_EXIT_PREP_PROMPT` を
  `docs/SESSION_SUMMARY.md` + `docs/next_plan.md` 更新へ明示化し、
  `> 最終更新: YYYY-MM-DD HH:MM JST` / `## 現在地` / `## 直近の成果` /
  `## 次の一手` / `## 環境メモ` を必須セクションとして指定した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py tests/test_loop.py` = **108 passed**。
  新規 3 テストで `shared PROGRESS` 生成 / rotate 後の自動更新 / exit-prep 文面を個別確認。

### 統合指示反映: projects_root 明示化 + graceful stop 追試 (2026-07-09 21:40, 本セッション)
- **妥当指摘の反映 1**: `infer_common_summary_paths()` の曖昧な「最寄り親」推定を撤回し、
  **既知の `projects_root` 直下 project に限定**して `_shared/PROGRESS.md` を解決する形へ変更。
  これにより、ネストした subproject や途中階層の `docs/next_plan.md` に引っ張られて
  意図しない `_shared` を更新するリスクを潰した。`projects_root` 不明時は **更新しない**
  (`fail-closed`)。
- **妥当指摘の反映 2**: `SessionLoop.projects_root` を追加し、GUI は `self.projects_root` を明示注入。
  CLI も既定 `D:/projects` を渡すが、対象 `workdir` がそこに属さなければ no-op のまま。
- **妥当指摘の反映 3**: graceful stop handoff 側でも `_shared/PROGRESS.md` が更新されることを
  テスト追加。rotate / graceful stop の両経路を now cover。
- **妥当指摘の反映 4**: `_refresh_common_progress()` の動的 import と `except Exception` を除去。
  top-level import に戻し、広すぎる例外捕捉は不要化した。
- **不要指摘の扱い**: 「`ruff` 未実施」はこの節の時点では該当しない。今回は差分ファイルに対して
  `ruff check ... --isolated` と `tests/test_loop.py --extend-ignore E702` を実行し、
  **新規差分は clean** を確認済み。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py tests/test_loop.py` = **111 passed**。
  `ruff check src/llterm/progress.py src/llterm/host/loop.py src/llterm/gui/app.py tests/test_progress.py --isolated`
  = pass。`tests/test_loop.py` は既存 `E702` を除外して新規差分のみ確認。

### Claude limit 下の代替運用強化 (2026-07-09 21:45, 本セッション)
- **RAD 接地の示唆**: `loop_engineering_corpus_v2` の circuit breaker / bulkhead / fail-safe default に従い、
  limit 中の依存を毎ターン叩き続ける補助ループを止めるのが筋、と判断。
- **lead を Claude 固定から解除**: `gui/app.py` `_lead_runner()` を自動選定化。
  Claude 主運用では従来どおり Claude を優先する一方、**Codex 主運用では lead も Codex を優先**し、
  「指揮者は Codex なのに責任者だけ Claude 固定」で limit に引きずられる構造を除去。
- **aux role の自動 bench**: `orchestra_runner.py` が reviewer / factchecker / lead の
  `rate_limited` / `auth` / `unavailable` を検知したら、その補助役を**以後のターンから bench**
  するようにした。これにより Claude reviewer/lead が limit でも、Codex 主の llterm は
  毎ターン無駄に Claude を叩かず継続できる。
- **i18n / UI 文言更新**: 「責任者=Claude 固定」を `自動選定` へ更新し、review panel tooltip でも
  aux bench を説明。UI 表示と実挙動を一致させた。
- **不要指摘の扱い**: 「Claude は常に backbone であるべき」は今回は不採用。Claude limit 時の
  代替運用を優先する要件と衝突するため、**必須 backbone ではない**方へ寄せた。
- **検証**: `py -3.11 -m pytest -q tests/test_gui.py tests/test_orchestra_runner.py`
  = **163 passed**。`ruff check src/llterm/gui/app.py src/llterm/host/orchestra_runner.py
  src/llterm/i18n/messages.py tests/test_gui.py tests/test_orchestra_runner.py --isolated` = pass。

### 統合指示反映: aux cooldown 復帰 + lead 表示整合 (2026-07-09 21:52, 本セッション)
- **妥当指摘の反映 1**: `orchestra_runner.py` の aux bench を永久停止から
  **cooldown 付き一時 bench + half-open 再試行**へ変更。`rate_limited=3 turn`、
  `auth/unavailable=1 turn` とし、一時障害や再認証後に reviewer / factchecker / lead が
  自然復帰できるようにした。毎ターンの無駄打ちは防ぎつつ、静かな品質劣化の固定化を避ける。
- **妥当指摘の反映 2**: `i18n/messages.py` の review aggregate / sign-off 文言を
  `Claude(責任者)` 固定から **`{lead}` 動的表示**へ変更。`gui/app.py` 側も review event の
  `lead` フィールドを埋めて表示するようにし、Codex 主運用でも UI と実挙動が一致するようにした。
- **テスト補強**: `tests/test_gui.py` に aggregate / sign-off の dynamic lead 表示を追加。
  `tests/test_orchestra_runner.py` では rate limit 後の cooldown 復帰、auth 後の短期復帰、
  lead フォールバック継続を検証するケースを追加した。
- **不要指摘の扱い**: 「コード差分が提供されていない」はここでは不採用。ローカル repo の実コードを
  直接読める環境なので、報告文ではなく実装本体の確認が優先され、指摘自体は作業ブロッカーではない。
- **検証**: `py -3.11 -m pytest -q tests/test_gui.py tests/test_orchestra_runner.py`
  = **166 passed**。`ruff check src/llterm/gui/app.py src/llterm/host/orchestra_runner.py
  src/llterm/i18n/messages.py tests/test_gui.py tests/test_orchestra_runner.py --isolated` = pass。

### Claude limit 下の degraded-mode 可視化 (2026-07-09 21:55, 本セッション)
- **優先課題 2 を着手**: `next_plan` で残していた「Claude 補助役を bench 済み / Codex 継続中」を
  画面から分かるようにした。quiet skip だけでは「レビューが消えた」ように見えるため、degraded mode を
  明示する 1 行が必要だった。
- **review stream 追加**: `orchestra_runner.py` が reviewer / factchecker / lead を
  `rate_limited/auth/unavailable` で cooldown bench した瞬間に `phase="aux_benched"` イベントを送る。
  payload は `runner / conductor / error_kind / cooldown_turns` を含み、継続主体を明示する。
- **GUI 表示**: `gui/app.py` + `i18n/messages.py` に
  `補助役 (claude) を 3 ターン bench (rate_limited) — codex で継続`
  相当のログ行を追加。Claude limit 中でも「故障ではなく graceful degradation で続行中」と判断できる。
- **テスト補強**: `tests/test_orchestra_runner.py` に aux bench イベントの payload 検証、
  `tests/test_gui.py` に degraded-mode 文言描画の検証を追加。
- **検証**: `py -3.11 -m pytest -q tests/test_gui.py tests/test_orchestra_runner.py`
  = **168 passed**。`ruff check src/llterm/gui/app.py src/llterm/host/orchestra_runner.py
  src/llterm/i18n/messages.py tests/test_gui.py tests/test_orchestra_runner.py --isolated` = pass。

### 問い合わせ系注入の fast path (2026-07-09 21:58, 本セッション)
- **優先課題 3 を一部実装**: 「進捗を要約して」「何をした?」のような**非編集問い合わせ**まで
  orchestra フルレビューへ流すのは過剰なので、`loop.py` に保守的 classifier
  `is_query_like_injection()` を追加した。summary/status/progress 系シグナルがあり、
  かつ fix/edit/update 系動詞を含まない注入だけを fast path 対象にする。
- **実行経路**: `SessionLoop.run()` が注入原文を保持し、問い合わせ系注入 + runner が
  `run_turn_unreviewed()` を持つ場合だけ、そのターンを **unreviewed** で実行する。
  handoff 用 seam を再利用しており、通常ターンや修正依頼を巻き込まない。
- **観測性**: `task` イベントに `review_mode=normal|unreviewed` を追加。この節の時点では GUI 表示には
  使っていないが、後続で「この注入は簡易経路だった」と見せたくなったときの seam になる。
- **テスト**: `tests/test_loop.py` に classifier の保守性、問い合わせ系注入が unreviewed 経路へ
  乗ること、修正依頼混在の注入は通常経路に残ることを追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_loop.py -k "query_like_injection or injected_task_marked_in_event or next_prompt_injection_is_high_priority or injection_consumed_after_rotate_not_starved"`
  = **5 passed**。`py -3.11 -m pytest -q tests/test_gui.py -k "review_aux_benched_shows_degraded_mode_line or review_aggregate_and_signoff_show_dynamic_lead" tests/test_orchestra_runner.py -k "aux_bench_event_emitted_with_conductor_context or auth_lead_retries_after_single_turn_cooldown or rate_limited_reviewer_retries_after_cooldown"`
  = **3 passed**。`ruff check src/llterm/host/loop.py src/llterm/gui/app.py src/llterm/host/orchestra_runner.py src/llterm/i18n/messages.py tests/test_loop.py tests/test_gui.py tests/test_orchestra_runner.py --isolated --extend-ignore E702` = pass。

### 統合指示反映: query-like fast path の tightening + telemetry 整合 (2026-07-09 22:03, 本セッション)
- **妥当指摘の反映 1**: `is_query_like_injection()` を sequence-aware に締めた。
  `その後` / `and then` / `also` などの複合要求マーカーを拒否し、mutation 語彙も
  `fix/修正` だけでなく `refactor/rename/move/cleanup/rewrite/replace` 等へ拡張した。
  これで `進捗を要約して、その後リファクタして` や `summarize progress and then rename X`
  は fast path に乗らない。
- **妥当指摘の反映 2**: `task.review_mode` は **実際に `run_turn_unreviewed()` を使える runner**
  のときだけ `unreviewed` を出すようにした。`supports_unreviewed_turns()` を追加し、
  plain `TurnRunner` が通常 `run_turn()` へフォールバックするケースでは telemetry も
  `normal` のままにして、GUI / 監査 / 後段解析との食い違いを防いだ。
- **妥当指摘の反映 3**: GUI 側の統合テストを追加。`tests/test_gui.py` で
  `_on_event("task", review_mode="unreviewed")` の表示と、
  `_on_stream(kind="review", phase="aux_benched")` の degraded-mode 表示を
  **配線込み**で確認するようにした。描画ヘルパ単体だけには依存しない。
- **不要指摘の扱い**: 「コード差分が提供されていない」は今回も不採用。ローカル repo の
  実装本体を直接読めるため、報告文より実コード確認が優先で、レビュー不能の前提は当たらない。
- **検証**: `py -3.11 -m pytest -q tests/test_loop.py -k "query_like_injection or injected_task_marked_in_event"`
  = **4 passed**。`py -3.11 -m pytest -q tests/test_gui.py -k "injected_task_shown_at_consumption or unreviewed_path_shown_at_consumption or review_aux_benched_shows_degraded_mode_line"`
  = **3 passed**。`ruff check src/llterm/host/loop.py src/llterm/gui/app.py src/llterm/i18n/messages.py tests/test_loop.py tests/test_gui.py --isolated --extend-ignore E702` = pass。

### Qt/worker access violation の収束 (2026-07-09 22:06, 本セッション)
- **症状**: `tests/test_gui.py` の choice/QThread 経路で、前回は sporadic に
  `Windows fatal exception: access violation` が出ていた。RAD では watchdog/liveness と
  observability の観点から、イベント境界ではなく**状態 (寿命) を deterministic に畳む**
  方が筋と判断。
- **修正 1**: `MainWindow.start_loop()` の `LoopWorker` を **parent=self** で生成し、
  `_on_finished()` では sender が現 worker なら `self.worker = None` に戻すよう変更。
  終了済み QThread を握り続けず、次回 start は fresh worker を張る。
- **修正 2**: `tests/test_gui.py` に `autouse` cleanup fixture を追加し、各テスト後に
  top-level widget を `close()` + `deleteLater()` して `QApplication.processEvents()` を流す。
  これで module-scope `QApplication` 配下に古い `MainWindow` / child QObject をぶら下げたまま
  次テストへ進む状態を避ける。
- **修正 3**: `_run_until_finished()` は `win.worker` の live 参照をその場でコピーして待つようにした。
  `_on_finished()` が途中で `self.worker=None` にしても、テストの `wait()` 対象がぶれない。
- **テスト補強**: `tests/test_gui.py::test_finished_run_clears_worker_reference` を追加し、
  自然終了後に `win.worker is None` へ戻ることを固定。choice dialog 経路と end-to-end も再実行。
- **検証**: `py -3.11 -m pytest -q tests/test_gui.py` = **142 passed**。
  `py -3.11 -m pytest -q tests/test_loop.py -k "query_like_injection or injected_task_marked_in_event or next_prompt_injection_is_high_priority" tests/test_orchestra_runner.py -k "aux_bench_event_emitted_with_conductor_context or rate_limited_reviewer_retries_after_cooldown or auth_lead_retries_after_single_turn_cooldown"` = **3 passed**。
  `ruff check src/llterm/gui/app.py tests/test_gui.py --isolated` および関連差分 lint = pass。

### 統合指示反映: worker retire / stale-finish race / cleanup hardening (2026-07-09 22:13, 本セッション)
- **妥当指摘の反映 1**: `MainWindow` に `_retired_workers` を追加し、終了済み `LoopWorker` は
  `_on_finished()` で retire した後、次回 `start_loop()` / `closeEvent()` の安全地点で
  `_reap_retired_workers()` が `deleteLater()` するよう変更。`DeferredDelete` も明示 flush し、
  old worker が run ごとに窓配下へ溜まる状態を解消した。
- **妥当指摘の反映 2**: `start_loop()` は `self.worker is not None` の時点で再起動を拒否する
  よう変更。これにより、旧 worker が `isRunning()==False` でも `_on_finished()` 未処理な短い窓で
  新 run を始められず、遅延した `finished_outcome` が新 run の UI 状態を壊す race を潰した。
  `_on_finished()` も `sender is self.worker` を要求し、stale queued outcome は無視する。
- **妥当指摘の反映 3**: `tests/test_gui.py` の cleanup fixture を harden し、
  `close()` の前に生存 worker を `force stop + wait`、`closeEvent()` が `ignore()` した窓には
  `deleteLater()` を掛けないようにした。これで graceful stop 中の `MainWindow` を親ごと
  破棄して再度 QThread ライフサイクル事故を起こす経路を避ける。
- **テスト補強**: GUI テストに
  `test_start_loop_waits_for_finished_slot_to_clear_worker` /
  `test_next_start_reaps_retired_worker` を追加し、pending-finish 窓では再 start しないこと、
  次回 start 前に old worker が 1 本へ収束することを固定した。
- **不要指摘の扱い**: 「コード差分が提示されていない」はここでも不採用。ローカル repo の
  実コード確認が可能で、レビュー不能の前提は当たらない。
- **検証**: `py -3.11 -m pytest -q tests/test_gui.py` = **144 passed**。
  `ruff check src/llterm/gui/app.py tests/test_gui.py --isolated` = pass。

### Codex の token 補助表示を status row に追加 (2026-07-09 22:17, 本セッション)
- **背景**: RAD の observability 原則どおり、`ctx%` (瞬間占有) と `codex usage` (1 ターン累積) を
  同じ指標として混ぜない方がよい。`codex` は `context_tokens=0` が正しい一方、何も見えないと
  「使っていない」ように見えるので、補助メトリクスを別欄に分離した。
- **実装**: `SessionLoop` の `turn` イベントに `provider / input_tokens / output_tokens` を追加。
  GUI は `lbl_tokens` を status row へ追加し、通常奏者は `tok in/out: X/Y`、`codex` は
  **`累積tok in/out: X/Y`** と表示する。これで `ctx%` は rotate 判定専用、token 欄は
  情報表示専用と役割が分かれる。
- **i18n**: `messages.py` に `gui.tip.tokens`, `gui.tokens.idle`, `gui.tokens.normal`,
  `gui.tokens.codex` を追加。Codex だけ文言で「累積」であることを明示した。
- **テスト補強**: `tests/test_loop.py` で `turn` payload に `provider/input/output` が
  乗ることを固定。`tests/test_gui.py` で token 欄の通常/`codex` 表示差と `turn` イベント経由の
  更新を確認した。
- **検証**: `py -3.11 -m pytest -q tests/test_loop.py -k "on_event_emits_progress" tests/test_gui.py -k "token_label_distinguishes_codex_cumulative_usage or render_slots_update_widgets"`
  = **2 passed**。`ruff check src/llterm/host/loop.py src/llterm/gui/app.py src/llterm/i18n/messages.py tests/test_gui.py tests/test_loop.py --isolated --extend-ignore E702` = pass。

### 統合指示反映: Orchestra→codex 正規化 + token 欄 stale reset (2026-07-09 22:20, 本セッション)
- **妥当指摘の反映 1**: `SessionLoop.provider_name()` が `OrchestraRunner` を受けたら
  `conductor` を剥いて判定するよう修正。これにより、既定のレビュー付き実行で
  `CodexRunner` が orchestra に包まれていても `turn.provider == "codex"` となり、
  GUI の `累積tok` 表示へ正しく入る。
- **妥当指摘の反映 2**: `lbl_tokens` は run/session/finish 境界で `_reset_tokens()` により
  `tok: -` へ戻すようにした。`start_loop()` 直後、新 session 開始時、`_on_finished()` で
  stale 値を消し、provider switch や再実行直後に前回値を「現在値」と誤読しにくくした。
- **テスト補強**: `tests/test_loop.py` に orchestra→conductor 正規化の単体確認を追加。
  `tests/test_gui.py` には `session_start` と `start/finish` で token 欄が idle へ戻ることを
  追加した。
- **不要指摘の扱い**: 「コード差分が提示されていない」はここでも不採用。ローカル repo の
  実コード確認が可能で、レビュー不能の前提は当たらない。
- **検証**: `py -3.11 -m pytest -q tests/test_loop.py -k "provider_name_uses_orchestra_conductor or on_event_emits_progress" tests/test_gui.py -k "token_label_distinguishes_codex_cumulative_usage or tokens_reset_on_session_start or tokens_reset_on_start_and_finish or render_slots_update_widgets"`
  = **4 passed**。`ruff check src/llterm/host/loop.py src/llterm/gui/app.py tests/test_loop.py tests/test_gui.py --isolated --extend-ignore E702` = pass。

### 共通タブを project 別サブタブ化 (2026-07-09 22:24, 本セッション)
- **RAD 接地**: HCI corpus の `Language Scent` を踏まえ、長い共通サマリ 1 枚より
  `All + project別` の情報手掛かりを置く方が、横断 overview と個別 drill-down を往復しやすい。
- **実装**: `gui/app.py` の top-level `summary_tabs` は 2 枚 (`実行中 / 共通`) のまま維持しつつ、
  `共通` タブ内を `common_tabs` に変更。`All` には従来の全 project 集約全文を残し、
  refresh 時に `collect_progress()` の並び順で project ごとの read-only subtabs を再生成する。
- **内容**: 各 project タブは `更新時刻 / source / path` を先頭に出し、その project の
  `docs/next_plan.md` 全文を単独表示する。総覧タブを壊さず、目的 project だけ素早く開ける。
- **テスト補強**: `tests/test_gui.py` で `summary_tabs=2` 維持、`common_tabs=All+project数`、
  時刻順のサブタブ順序、個別タブ内の `source/本文` を確認した。
- **検証**: `py -3.11 -m pytest -q tests/test_gui.py -k "summary_has_live_and_common_tabs or render_slots_update_widgets or tokens_reset_on_start_and_finish"`
  = **3 passed**。`ruff check src/llterm/gui/app.py src/llterm/i18n/messages.py tests/test_gui.py --isolated` = pass。

### 統合指示反映: 個別タブの file-time 明示 + stale worker 再始動回復 (2026-07-09 22:28, 本セッション)
- **妥当指摘の反映 1**: project 個別タブの `> 更新:` にも `updated_source` を反映し、
  `mtime` フォールバック時は **`(ファイル時刻)`** を明示するよう修正した。`All` ビューだけでなく
  drill-down 側でも時刻の信頼度を誤認しない。
- **妥当指摘の反映 2**: `start_loop()` は `self.worker is not None` だけで停止せず、
  **`isRunning()` 中だけ再始動を拒否**し、停止済み stale worker はその場で切り離して回収した上で
  fresh worker を起動するよう戻した。`_on_finished()` の queued slot 取りこぼしがあっても
  Start が永久に効かなくなる経路を避ける。
- **テスト補強**: `tests/test_gui.py` の stale-worker ケースは「再始動を拒否する」固定から
  **「停止済み stale なら新 run を開始できる」** 確認へ更新。加えて file-time fallback を
  個別タブ本文で検証するテストを追加した。
- **不要指摘の扱い**: Gemini の「報告範囲の不一致」は実装バグではないため今回は不採用。
  作業対象はコードとテストの妥当性であり、報告文の網羅性はランタイム挙動を変えない。
- **検証**: `py -3.11 -m pytest -q tests/test_gui.py -k "summary_has_live_and_common_tabs or start_loop_waits_for_finished_slot_to_clear_worker or common_project_tab_marks_file_time_fallback or next_start_reaps_retired_worker"`
  = **4 passed**。`ruff check src/llterm/gui/app.py tests/test_gui.py --isolated` = pass。

### Codex 累積 usage の性質を telemetry に明示 (2026-07-09 22:34, 本セッション)
- **RAD 接地**: observability 三本柱どおり、制御ループが使う信号は「何を観測しているか」を
  明示しないと誤制御を招く。`codex` は瞬間占有を観測できず、`turn.completed` の累積 usage だけが
  取れるため、その差をイベントと UI に露出した。
- **実装**: `TurnResult` に `cached_input_tokens` と `token_usage_kind` (`instant|cumulative`) を追加。
  `codex_runner.py` は `cached_input_tokens` と `token_usage_kind=\"cumulative\"` を埋め、
  `loop.py` の `turn` イベントもそれを GUI へ流すようにした。
- **GUI 表示**: `gui/app.py` / `i18n/messages.py` の token 欄は、通常奏者では従来どおり
  `tok in/out`、`codex` では **`累積tok in/cache/out: ... (ctx別)`** と表示する。
  これで `ctx%` が瞬間占有、token 欄が `turn.completed` 累積 usage だと画面上で区別できる。
- **テスト補強**: `tests/test_codex_runner.py` に `cached_input_tokens` / `token_usage_kind`
  の parse 固定、`tests/test_loop.py` に `turn` payload 固定、`tests/test_gui.py` に
  cumulative+cache 表示の確認を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_codex_runner.py -k "parse_codex_success or huge_cumulative_usage" tests/test_loop.py -k "on_event_emits_progress" tests/test_gui.py -k "token_label_distinguishes_codex_cumulative_usage or render_slots_update_widgets or turn_event_shows_codex_cumulative_tokens_with_cache"`
  = **3 passed**。`ruff check src/llterm/host/loop.py src/llterm/host/codex_runner.py src/llterm/gui/app.py src/llterm/i18n/messages.py tests/test_codex_runner.py tests/test_loop.py tests/test_gui.py --isolated --extend-ignore E702` = pass。

### 統合指示反映: Orchestra 経路で cumulative token metadata を保持 (2026-07-09 22:40, 本セッション)
- **妥当指摘の反映 1**: `orchestra_runner.py` が `TurnResult` を再構成する全経路で
  `cached_input_tokens` と `token_usage_kind` を引き継ぐよう修正した。これにより
  **`OrchestraRunner(CodexRunner)` の通常経路でも** `SessionLoop` / GUI が
  `累積tok in/cache/out` を正しく表示する。
- **妥当指摘の反映 2**: `tests/test_orchestra_runner.py` に final result と
  レビュー中 interrupt 経路の両方で metadata が落ちないことを追加。`tests/test_loop.py` は
  `SessionLoop` の `turn` event が orchestra 経由でも `provider=codex` と
  cumulative metadata を保持することを確認。`tests/test_gui.py` には、その event を受けた GUI が
  `累積tok in/cache/out: ... (ctx別)` を出す統合テストを追加した。
- **不要指摘の扱い**: Gemini の「差分が提示されていない」は今回も不採用。ローカル repo の
  実コードを直接確認できるため、レビュー不能の前提にはならない。
- **不要指摘の扱い 2**: `_shared/PROGRESS.md` 更新コスト/排他の一般論は今回の token regression と
  独立で、直近の不具合修正対象ではないため今回は見送った。
- **検証**: `py -3.11 -m pytest -q tests/test_orchestra_runner.py::test_final_result_preserves_cached_tokens_and_usage_kind tests/test_orchestra_runner.py::test_interrupted_result_preserves_cached_tokens_and_usage_kind tests/test_loop.py::test_turn_event_preserves_cumulative_token_metadata_through_orchestra tests/test_gui.py::test_gui_shows_cumulative_codex_tokens_from_orchestra_turn_event`
  = **4 passed**。`ruff check src/llterm/host/orchestra_runner.py tests/test_orchestra_runner.py tests/test_loop.py tests/test_gui.py --isolated --extend-ignore E702` = pass。

### 共通進捗に next_plan 正規フォーマット監査を追加 (2026-07-09 22:40, 本セッション)
- **RAD 接地**: MAPE-K / information scent の観点では、集約ビューに「見えない欠落」があると
  人手の再確認コストが跳ねる。そこで、共通進捗に **欠落ヘッダそのものを露出**して、
  どの project が標準フォーマットから外れているかを一目で追えるようにした。
- **実装**: `progress.py` に `progress_format_gaps()` を追加し、
  `最終更新(YYYY-MM-DD HH:MM JST)` / `## 現在地` / `## 直近の成果` / `## 次の一手` /
  `## 環境メモ` の欠落を `ProjectProgress.format_gaps` へ保持するようにした。
- **表示**: 共通サマリのインデックス行に `[format: ...]` を追加し、本文セクションと GUI の
  project 個別タブにも `format gaps: ...` を出すようにした。`llterm-progress` を再実行し、
  `D:/projects/_shared/PROGRESS.md` も新注記つきで再生成済み。
- **実監査結果 (当時点)**: この時点では未整備なのは `fullsense`, `llcore`, `onocollo-complete`
  の 3 件だった。欠落は `_shared/PROGRESS.md` 冒頭にそのまま表示される。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "format_gaps or build_common_summary_includes_format_gap_annotations or collect_progress_scans_and_skips" tests/test_gui.py -k "summary_has_live_and_common_tabs or common_project_tab_marks_file_time_fallback"`
  = **2 passed**。`ruff check src/llterm/progress.py src/llterm/gui/app.py tests/test_progress.py tests/test_gui.py --isolated` = pass。

### 統合指示反映: 監査をコードフェンス外の実ヘッダへ限定 + GUI timestamp fail-safe 復元 (2026-07-09 22:45, 本セッション)
- **妥当指摘の反映 1**: `progress.py` の `parse_updated_at()` と `progress_format_gaps()` は
  Markdown 全文の部分一致をやめ、**コードフェンス外の実行テキストだけ**を対象にした。
  `最終更新:` は文頭近くの dedicated 行だけを採用し、`## 現在地` などの必須見出しも
  quoted past log や fenced code block では満たしたことにしない。これで正規フォーマット監査の
  false negative を抑えた。
- **妥当指摘の反映 2**: `gui/app.py` の個別 project タブ描画は `datetime.fromtimestamp()` 直呼びをやめ、
  `progress._default_fmt()` の fail-safe を再利用するよう修正した。異常 timestamp を含む 1 project が
  あっても `> 更新: ?` に落として、共通タブ全体の更新を殺さない。
- **テスト補強**: `tests/test_progress.py` に本文中 mention / code fence / quote を誤検知しない
  ケースを追加し、`tests/test_gui.py` には巨大 timestamp でも個別タブ描画が `?` で継続する
  ケースを追加した。
- **不要指摘の扱い**: Gemini の「diff が提示されていない」は今回も不採用。ローカル repo の
  実コードを直接確認できるため、レビュー不能の前提にはならない。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "ignores_body_mentions_and_code_fences or ignores_quoted_and_fenced_headings or format_gaps_reports_missing_canonical_sections" tests/test_gui.py -k "common_project_tab_handles_bad_timestamp_failsafe or common_project_tab_marks_file_time_fallback"`
  = **2 passed**。`ruff check src/llterm/progress.py src/llterm/gui/app.py tests/test_progress.py tests/test_gui.py --isolated` = pass。

### 他 project の next_plan 正規化を完了 (2026-07-09 22:46, 本セッション)
- **実施内容**: 監査で残っていた `fullsense`, `llcore`, `onocollo-complete` の
  `docs/next_plan.md` を最小差分で更新し、`> 最終更新: YYYY-MM-DD HH:MM JST` と
  `## 現在地 / 直近の成果 / 次の一手 / 環境メモ` を機械可読に揃えた。
- **監査ルール調整**: `progress.py` 側では `JST` 付き更新行と
  `## 次の一手 (優先順)` のような suffix 付き見出しを正当な変種として受理するよう修正した。
- **結果**: `collect_progress(D:/projects)` の `format_gaps` は空になり、
  `_shared/PROGRESS.md` 先頭から `[format: ...]` 注記が消えた。現在は共通進捗の
  `next_plan` 正規フォーマット未整備 project は 0 件。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "parse_updated_at_with_time or accepts_heading_suffixes or reports_missing_canonical_sections"` = **3 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
  さらに `py -3.11 -m llterm.progress` を再実行し、`D:/projects/_shared/PROGRESS.md` を再生成済み。

### 統合指示反映: JST 固定解釈 + 見出し順序監査 + exit-prep 失敗時の no-refresh (2026-07-09 22:52, 本セッション)
- **妥当指摘の反映 1**: `progress.py` の `parse_updated_at()` は `JST` を
  **UTC+09:00 固定**で epoch 化するよう修正した。これでホスト環境のローカル timezone に依存せず、
  `D:/projects/_shared/PROGRESS.md` の並び順が安定する。
- **妥当指摘の反映 2**: `progress_format_gaps()` は必須見出しの存在だけでなく、
  `現在地 → 直近の成果 → 次の一手 → 環境メモ` の**順序**も検証し、違反時は
  `見出し順序` を gap として返すようにした。これに合わせて `llcore/docs/next_plan.md` は
  `直近の成果` 見出しを前方へ再配置し、監査 0 件へ戻した。
- **妥当指摘の反映 3**: `loop.py` は handoff/rotate 用の exit-prep ターンが
  `is_error=True` のとき、`_refresh_common_progress()` と `exit_prep` 記録を実行しないよう変更した。
  失敗時は `exit_prep_failed` を ledger に残し、stale な `next_plan.md` を「同期済み」に見せない。
- **不要指摘の扱い**: Gemini の「報告文と差分の乖離」等は実装バグではないため今回は不採用。
  ここで優先するのは runtime 挙動と監査条件の整合である。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "parse_updated_at_with_time or parse_updated_at_fullwidth_colon or reports_out_of_order_sections" tests/test_loop.py -k "does_not_refresh_shared_progress_when_exit_prep_fails or does_not_refresh_shared_progress_when_handoff_fails"` = **2 passed**。
  `ruff check src/llterm/progress.py src/llterm/host/loop.py tests/test_progress.py tests/test_loop.py --isolated --extend-ignore E702` = pass。
  その後 `collect_progress(D:/projects)` の `format_gaps` が空であることを確認し、`py -3.11 -m llterm.progress` で共通進捗も再生成済み。

### SESSION_SUMMARY 代用 project の next_plan 移行支援を自動化 (2026-07-09 22:56, 本セッション)
- **RAD 接地**: `loop_engineering_corpus_v2` の GitOps / desired-vs-actual / level-triggered の流儀に従い、
  再開の正本を `SESSION_SUMMARY` の揮発 projection から `next_plan` の durable state へ寄せる helper を追加した。
- **実装**: `progress.py` に `parse_session_summary_seed()` / `scaffold_next_plan_text()` /
  `scaffold_next_plan_for_project()` / `scaffold_missing_next_plans()` を追加し、
  `llterm-progress --scaffold-next-plan [all|PROJECT...]` で `SESSION_SUMMARY` 代用 project に
  正規フォーマットの `docs/next_plan.md` を新設できるようにした。既存 `next_plan.md` は上書きしない。
- **実適用**: `browser-use-project`, `llive`, `llmesh`, `llmesh-suite`, `llove` に scaffold を生成。
  これで `collect_progress(D:/projects)` 上の 9 project すべてが `source=next_plan` となり、
  共通進捗は `SESSION_SUMMARY` 代用に依存しなくなった。
- **テスト補強**: `tests/test_progress.py` に metadata parse、canonical scaffold 生成、
  既存 `next_plan` 非上書き、session_summary-only project だけを対象にする batch 作成を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "scaffold or parse_session_summary_seed or parse_updated_at_with_time or reports_out_of_order_sections"` = **6 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
  `py -3.11 -m llterm.progress --scaffold-next-plan all --projects-root D:/projects --stdout` で生成対象を確認し、
  `py -3.11 -m llterm.progress --projects-root D:/projects` で `D:/projects/_shared/PROGRESS.md` を再生成済み。

### 統合指示反映: scaffold 時刻の mtime fallback + CLI 契約統一 (2026-07-09 23:00, 本セッション)
- **妥当指摘の反映 1**: `progress.py` の scaffold 更新時刻は、`SESSION_SUMMARY` の `最終更新` が
  解釈不能なとき `datetime.now()` を入れるのをやめ、**元の `docs/SESSION_SUMMARY.md` の mtime** へ
  フォールバックするよう修正した。これで古い project を scaffold しただけで共通進捗の先頭へ
  飛び出す誤順位を防ぎ、既存の「本文時刻が無ければ mtime」の原則と整合する。
- **妥当指摘の反映 2**: `llterm-progress --scaffold-next-plan ...` も通常モードと同様に、
  存在しない `--projects-root` を **`return 2` の明示エラー**に統一した。以前の
  `scaffolded 0 next_plan.md` で成功扱いになる分岐差をなくした。
- **テスト補強**: `tests/test_progress.py` に、`fallback_epoch` が `> 最終更新:` へ
  `JST` 整形で反映されることと、無効 `projects-root` で scaffold CLI が `rc=2` を返すことを追加した。
- **不要指摘の扱い**: Gemini の一般論的な運用リスク列挙は今回は不採用。今回の修正対象は
  scaffold 時刻と CLI 契約の具体的な不整合であり、抽象的な将来懸念までは広げない。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "scaffold or progress_main_scaffold_errors_for_missing_projects_root or parse_session_summary_seed or parse_updated_at_with_time"` = **7 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### Codex の ctx 不観測を telemetry/UI に明示 (2026-07-09 23:05, 本セッション)
- **RAD 接地**: `loop_engineering_corpus_v2` の observability 原則どおり、制御ループは
  「観測できないものを 0 と誤認しない」方が重要。`codex` は per-call の瞬間占有を返さず、
  `turn.completed.usage` の累積値しかないため、`ctx 0%` を「空いている」と見せるのは誤誘導になる。
- **実装**: `TurnResult` に `context_observable: bool` を追加し、`CodexRunner` は
  `context_observable=False` を返すようにした。`SessionLoop` の `turn` event でもこのフラグを流し、
  `OrchestraRunner(CodexRunner)` 経路でも metadata が落ちないよう通した。
- **GUI 表示**: `gui/app.py` / `i18n/messages.py` で、`context_observable=False` のターンは
  ログ見出しを **`ctx n/a (provider管理)`**、ctx バー書式を **`ctx n/a (rotate X%)`** に変更。
  token 欄の `累積tok in/cache/out: ... (ctx別)` と合わせて、「累積 usage は見えるが瞬間占有は不明」
  が画面で分かるようにした。
- **テスト補強**: `tests/test_codex_runner.py` に `context_observable=False`、`tests/test_loop.py` に
  turn event 伝播、`tests/test_orchestra_runner.py` に orchestra 経路での保持、`tests/test_gui.py` に
  `ctx n/a` 表示と ctx バー書式の確認を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_codex_runner.py -k "parse_codex_success" tests/test_loop.py -k "on_event_emits_progress or preserves_cumulative_token_metadata_through_orchestra" tests/test_orchestra_runner.py -k "preserves_cached_tokens_and_usage_kind" tests/test_gui.py -k "turn_event_shows_codex_cumulative_tokens_with_cache or gui_shows_cumulative_codex_tokens_from_orchestra_turn_event or ctx_bar_shows_rotate_threshold"` = **3 passed**。
  `ruff check src/llterm/host/loop.py src/llterm/host/codex_runner.py src/llterm/host/orchestra_runner.py src/llterm/gui/app.py src/llterm/i18n/messages.py tests/test_codex_runner.py tests/test_loop.py tests/test_orchestra_runner.py tests/test_gui.py --isolated --extend-ignore E702` = pass。

### 統合指示反映: `unknown` を `0%` として emit/描画しない (2026-07-09 23:09, 本セッション)
- **妥当指摘の反映 1**: `loop.py` の `turn` event は、`context_observable=False` のとき
  `used_pct=0.0` を流すのをやめ、**`used_pct=None`** を emit するよう修正した。これで
  既存 telemetry consumer も数値 `0%` を「unknown」と誤解しにくくなり、`context_observable` と
  数値の意味が食い違わない。
- **妥当指摘の反映 2**: `gui/app.py` は `context_observable=False` のとき `used_pct` を
  数値化せず `pct=0` の内部値に留め、ctx バーの chunk を **transparent** にするよう変更した。
  文言だけ `ctx n/a` でバー自体は空の 0% に見える状態をやめ、視覚的にも「未知」を示す。
- **テスト補強**: `tests/test_loop.py` に orchestra 経路の `used_pct is None` を追加し、
  `tests/test_gui.py` では通常 turn で ctx バー style が空、Codex unobservable turn では
  `transparent` style と `ctx n/a` 表示になることを固定した。
- **不要指摘の扱い**: Gemini の報告範囲・文書化まわりの指摘は今回は不採用。今回の修正対象は
  telemetry と GUI の意味論の不整合であり、報告文の網羅性ではない。
- **検証**: `py -3.11 -m pytest -q tests/test_loop.py -k "preserves_cumulative_token_metadata_through_orchestra or on_event_emits_progress" tests/test_gui.py -k "render_slots_update_widgets or turn_event_shows_codex_cumulative_tokens_with_cache or gui_shows_cumulative_codex_tokens_from_orchestra_turn_event"` = **3 passed**。
  `ruff check src/llterm/host/loop.py src/llterm/gui/app.py tests/test_loop.py tests/test_gui.py --isolated --extend-ignore E702` = pass。

### Codex `--json` 実測で per-call/span 不在を確認し、理由文字列を露出 (2026-07-09 23:14, 本セッション)
- **実測**: `codex --help` / `codex exec --help` / `codex exec resume --help` を確認した上で、
  `Reply with exactly OK. Do not use tools.` の最小 probe を `codex exec --json --ephemeral ...` で実行。
  出たイベントは **`thread.started / turn.started / item.completed(agent_message) / turn.completed(usage)` のみ**で、
  per-request / span / intermediate occupancy に相当する公開イベントは確認できなかった。
- **実装**: `TurnResult` に `context_observable_reason` を追加し、`CodexRunner` は
  `context_observable=False, context_observable_reason="cumulative_only"` を返すようにした。
  `SessionLoop` の `turn` event と `OrchestraRunner` の合成経路にも同理由を通している。
- **GUI 表示**: `gui/app.py` / `i18n/messages.py` で、ctx バー tooltip を動的化。
  `context_observable_reason="cumulative_only"` のときは
  **「turn.completed の累積 usage しか返らず、瞬間の context 占有は観測できない」** と説明する。
- **テスト補強**: `tests/test_codex_runner.py` に reason の固定、`tests/test_loop.py` に
  orchestra 経由 event の reason、`tests/test_gui.py` に ctx tooltip の説明文を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_codex_runner.py -k "parse_codex_success" tests/test_loop.py -k "preserves_cumulative_token_metadata_through_orchestra" tests/test_gui.py -k "turn_event_shows_codex_cumulative_tokens_with_cache or gui_shows_cumulative_codex_tokens_from_orchestra_turn_event"` = **2 passed**。
  `ruff check src/llterm/host/loop.py src/llterm/host/codex_runner.py src/llterm/host/orchestra_runner.py src/llterm/gui/app.py src/llterm/i18n/messages.py tests/test_codex_runner.py tests/test_loop.py tests/test_gui.py --isolated --extend-ignore E702` = pass。

### 統合指示反映: rotate も `ctx n/a` 表示 + reason の Loop 補完 (2026-07-09 23:18, 本セッション)
- **妥当指摘の反映 1**: `loop.py` は `TurnResult.context_observable=False` かつ
  `context_observable_reason=""` の場合でも、`token_usage_kind="cumulative"` なら
  **`cumulative_only` を自動補完**する `normalize_context_observable_reason()` を追加した。
  これで bare な Codex 相当 `TurnResult` を返すテスト runner / 将来 runner でも、Loop/Orchestra 経由の
  telemetry reason が空にならない。
- **妥当指摘の反映 2**: `turn` event だけでなく `rotate` event も
  `used_pct=None, context_observable=False` を流すよう修正した。GUI はこれを受けて
  `rotate (ctx 0%)` ではなく **`rotate (ctx n/a)`** を表示する。
- **GUI 整合**: `gui/app.py` / `i18n/messages.py` に `gui.msg.rotate_unobservable` を追加し、
  Codex 系 rotate のログ行も「瞬間 ctx は不観測」という仕様と矛盾しないよう揃えた。
- **テスト補強**: `tests/test_loop.py` の orchestra 経由回帰テストは
  `context_observable_reason == "cumulative_only"` を pass するようになり、`tests/test_gui.py` には
  `rotate_event_shows_ctx_na_when_unobservable` を追加した。
- **不要指摘の扱い**: Gemini の報告文・diff 提示まわりは今回も不採用。ここで直すべきは
  rotate 表示と reason 補完の runtime 不整合であり、報告書式ではない。
- **検証**: `py -3.11 -m pytest -q tests/test_loop.py -k "preserves_cumulative_token_metadata_through_orchestra" tests/test_gui.py -k "rotate_event_shows_ctx_na_when_unobservable or gui_shows_cumulative_codex_tokens_from_orchestra_turn_event or turn_event_shows_codex_cumulative_tokens_with_cache"` = **3 passed**。
  `ruff check src/llterm/host/loop.py src/llterm/gui/app.py src/llterm/i18n/messages.py tests/test_loop.py tests/test_gui.py --isolated --extend-ignore E702` = pass。

### Telemetry 契約を `context_state` 列挙値で安定化 (2026-07-09 23:21, 本セッション)
- **RAD 接地**: `loop_engineering_corpus_v2` の schema/versioning 指針どおり、consumer に
  bool+reason の組み合わせ解釈を毎回強いない方が契約として安定する。そこで互換維持のまま
  1 フィールド列挙を追加した。
- **実装**: `SessionLoop.context_state()` を追加し、
  `measured / provider_managed_cumulative / unobservable` の 3 値へ正規化するようにした。
  `turn` / `rotate` event は既存の `context_observable` と `context_observable_reason` を残しつつ、
  追加で `context_state` を emit する。
- **狙い**: telemetry consumer は今後 `used_pct is None` と `context_state` だけ見ればよく、
  Codex 系の「provider が累積 usage しか返さない」ケースも一意に判定できる。
- **テスト補強**: `tests/test_loop.py` に `context_state_normalizes_cumulative_unknown` を追加し、
  通常 turn で `measured`、Codex orchestra 経路で `provider_managed_cumulative` が出ることを固定した。
- **検証**: `py -3.11 -m pytest -q tests/test_loop.py -k "context_state_normalizes_cumulative_unknown or on_event_emits_progress or preserves_cumulative_token_metadata_through_orchestra"` = **3 passed**。
  `ruff check src/llterm/host/loop.py tests/test_loop.py --isolated --extend-ignore E702` = pass。

### Telemetry 互換性の巻き戻し + provider 名の正規化 (2026-07-09 23:27, 本セッション)
- **RAD 接地**: 互換性系の既存知見どおり、新メタデータを足しても旧フィールドの意味を静かに変えない方が安全。
  そこで `used_pct` は `float` 契約へ戻し、意味の分岐は `context_state` / `context_observable` 側へ寄せた。
- **実装**: `SessionLoop` の `turn` / `rotate` event は、`context_observable=False` でも `used_pct`
  を常に `float` で emit するよう戻した。GUI は従来どおり `context_observable` を見て `ctx n/a`
  表示を続けるので、見た目は保ったまま既存 consumer の `float()` / 比較前提を壊さない。
- **provider 名**: `provider_name()` は手書きの Claude/Codex 2 件だけでなく、
  `provider_label()` 実装を優先し、無い場合も `GeminiRunner` / `OpenAICompatRunner` /
  `VirtualClaudeRunner` を Orchestra と同じラベルへ正規化した。これで telemetry だけクラス名が
  流れるズレを止めた。
- **テスト補強**: `tests/test_loop.py` に `provider_label()` 優先の回帰を追加し、
  `OrchestraRunner(Codex)` 経由の `used_pct == 0.0` と cumulative metadata 維持を固定した。

### `SESSION_SUMMARY` 由来 scaffold を minimal から practical へ寄せた (2026-07-09 23:25, 本セッション)
- **RAD 接地**: handoff / progress summary 系の既存知見に合わせ、雛形は「空欄だけの器」ではなく
  最新セクションと直近 TODO を少し引き継ぐ方が再開コストを下げる。一方で stale 情報を広げすぎないため、
  抽出対象は `最新見出し` と `## 次にやるべきこと` の bullet に限定した。
- **実装**: `progress.py` の `parse_session_summary_seed()` を拡張し、
  `latest_heading` / `latest_bullets` / `next_steps` を取得するようにした。
  `scaffold_next_plan_text()` はこれを使って `## 現在地` に最新見出し、`## 直近の成果` に最新 bullet、
  `## 次の一手` に既存 `SESSION_SUMMARY` の TODO を差し込む。抽出できない場合は従来の安全側 fallback を維持。
- **テスト補強**: `tests/test_progress.py` に metadata 抽出と richer scaffold 生成の回帰を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "parse_session_summary_seed_extracts_metadata or scaffold_next_plan_text_creates_canonical_sections or scaffold_next_plan_text_falls_back_to_mtime_not_now or scaffold_missing_next_plans_only_targets_session_summary_projects"` = **4 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### 統合指示反映: `次にやるべきこと (...)` 見出しと scaffold CLI 契約を修正 (2026-07-09 23:31, 本セッション)
- **妥当指摘の反映 1**: `parse_session_summary_seed()` の `next_steps` 抽出は、完全一致だけでなく
  `## 次にやるべきこと (…注釈…)` / `（…）` / `(...)` の suffix 付き見出しも受理するようにした。
  実データの `docs/SESSION_SUMMARY.md` 形式に追従し、scaffold の `## 次の一手` が不要 fallback へ落ちない。
- **妥当指摘の反映 2**: `llterm-progress --scaffold-next-plan` は、引数無しなら全件実行せず
  `PROJECT... または all を要求` で `rc=2` にした。`all` 明示時だけ全件 scaffold する形へ寄せ、
  help 文と実挙動を一致させた。
- **テスト補強**: `tests/test_progress.py` に suffix 付き `次にやるべきこと` 抽出と、
  `--scaffold-next-plan` 単独が明示エラーになる回帰を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "parse_session_summary_seed_extracts_metadata or accepts_next_steps_heading_suffix or scaffold_next_plan_text_creates_canonical_sections or progress_main_scaffold_requires_explicit_target or progress_main_scaffold_errors_for_missing_projects_root"` = **5 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### `SESSION_SUMMARY` の番号付き列挙も scaffold 抽出対象にした (2026-07-09 23:36, 本セッション)
- **RAD 接地**: 再開用 handoff は実データの書式ゆらぎに強い方がよい。`SESSION_SUMMARY` には
  `-` / `*` だけでなく `1. 2. 3.` の番号付き列挙も使われるため、bullet 限定だと `次にやるべきこと`
  をまた空振りし得る。
- **実装**: `progress.py` に `_summary_list_item()` を追加し、`parse_session_summary_seed()` の
  `latest_bullets` / `next_steps` 抽出を `-` / `*` / `1.` 形式へ共通対応させた。
- **テスト補強**: `tests/test_progress.py` に番号付き `latest_bullets` と `next_steps` の抽出回帰を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "accepts_numbered_next_steps or accepts_next_steps_heading_suffix or parse_session_summary_seed_extracts_metadata or scaffold_next_plan_text_creates_canonical_sections"` = **4 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### `SESSION_SUMMARY` の複数行 bullet も 1 項目として引き継ぐようにした (2026-07-09 23:34, 本セッション)
- **RAD 接地**: 実 handoff では bullet 本文の次行に補足条件や検証方針がぶら下がる。先頭行だけ抜くと
  scaffold の `次の一手` が粗くなり、再開時に重要な条件が落ちる。
- **実装**: `progress.py` に `_collect_summary_list_item()` を追加し、`-` / `*` / `1.` の list item に続く
  インデント継続行を 1 項目へ畳んで `next_steps` / `latest_bullets` に入れるようにした。
- **テスト補強**: `tests/test_progress.py` に `handoff 自己検証ゲート` のような複数行 bullet を 1 項目として
  保持する回帰を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "keeps_indented_continuation_lines or accepts_star_bullets or accepts_numbered_next_steps or accepts_next_steps_heading_suffix or parse_session_summary_seed_extracts_metadata"` = **5 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### nested bullet と空行 paragraph 継続も同一 item として保持 (2026-07-09 23:40, 本セッション)
- **妥当指摘の反映**: これまでの `_collect_summary_list_item()` は
  `- parent /  - child` を sibling 扱いし、さらに空行を挟んだ継続 paragraph もそこで切っていた。
  そのままでは scaffold の `next_steps` が増殖・変質しうる。
- **実装**: list item 解析を `(indent, text)` ベースに組み替え、**同階層以下の新 item だけ**を区切りとみなし、
  より深いインデントの nested bullet / 補足行 / 空行後の継続 paragraph は親 item に畳み込むようにした。
- **テスト補強**: `tests/test_progress.py` に
  `nested bullet` と `blank-line continuation paragraph` の 2 回帰を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "keeps_nested_bullets_inside_parent_item or keeps_blank_line_continuation_paragraph or keeps_indented_continuation_lines or accepts_star_bullets or accepts_numbered_next_steps"` = **5 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### `次にやるべきこと` の完了済み項目を scaffold から除外 (2026-07-09 23:45, 本セッション)
- **RAD 接地**: stale/missing state を減らすには、更新済みの TODO をそのまま再注入しない方がよい。
  実データの `SESSION_SUMMARY` でも `~~多言語対応~~ — 実装完了` のような打消し済み項目が残るため、
  scaffold の `次の一手` に混ぜると再開ノイズになる。
- **実装**: `progress.py` に `_is_completed_summary_item()` を追加し、
  `次にやるべきこと` セクションで `~~...~~` 形式の完了済み項目は `next_steps` へ入れないようにした。
  `直近の成果` 側は履歴性があるため除外しない。
- **テスト補強**: `tests/test_progress.py` に番号付き completed item を飛ばして未完項目だけ残す回帰を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "skips_completed_next_steps or keeps_nested_bullets_inside_parent_item or keeps_blank_line_continuation_paragraph or accepts_numbered_next_steps"` = **4 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### `~~完了~~ ... 残:` の partial-complete からは残作業だけ救う (2026-07-09 23:49, 本セッション)
- **妥当指摘の反映**: `~~完了~~` で始まる項目を丸ごと落とすと、
  `~~多言語対応~~ — 実装完了。残: zh/ko 訳 + GUI 言語切替 UI` のような一部完了項目まで消えてしまう。
- **実装**: `progress.py` の completed 判定を `_normalize_next_step_item()` へ置き換え、
  `残:` があればその後ろだけを `next_steps` へ残し、完全完了項目だけ `None` 扱いで落とすようにした。
- **テスト補強**: `tests/test_progress.py` に partial-complete 項目から
  `zh/ko 訳 + GUI 言語切替 UI` だけを救う回帰を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "skips_completed_next_steps or keeps_remaining_work_from_partial_completion or keeps_nested_bullets_inside_parent_item or keeps_blank_line_continuation_paragraph"` = **4 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### `残：` / `remaining:` も partial-complete の残作業 marker として扱う (2026-07-09 23:54, 本セッション)
- **RAD 接地**: handoff は書式が完全固定ではないため、partial-complete の残作業 marker も最小限の揺れを吸収した方が stale state を減らせる。
- **実装**: `_normalize_next_step_item()` の残作業抽出を `残:` に加えて `残：` と `remaining:` にも広げた。
  completed 判定自体は `~~...~~` 前提のままなので、誤検知面積は増やしていない。
- **テスト補強**: `tests/test_progress.py` に fullwidth colon と英語 `remaining:` の両方を追加した。
- **検証**: `py -3.11 -m pytest -q tests/test_progress.py -k "keeps_remaining_work_from_partial_completion or keeps_remaining_work_from_fullwidth_or_english_marker or skips_completed_next_steps"` = **3 passed**。
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。

### 緊急注入 + 全行タイムスタンプ + ローテログ + 記事ストック (2026-06-13 16:56、本セッション)
- **緊急注入 (interrupt)**: 恒久 cancel と別に **一発 interrupt** を全 runner (Claude/Codex/Gemini/Orchestra) へ追加。
  run_turn は `error_kind="interrupted"` を返し、loop は**停止せず注入を次ターンで必ず消費 (スキップ防止)**。
  worker `inject(emergency=True)` = **キュー先頭挿入 + 全 runner interrupt**。GUI に「⚡緊急注入」ボタン。
- **出力ログの各行に時刻**: `_append` (唯一のファネル) で全行先頭に `[HH:MM:SS]` を一括付与 (空行は素通し)。
- **ローテログ**: `gui/termlog.py` `TerminalLog` — **1 時間単位ファイル・行単位 append・1 週間保持・古いものから削除・fail-safe**。
  置き場 = `~/.llterm/logs/`。Qt 非依存で単体テスト済。
- **記事ストック**: 本セッションの内容を `docs/ARTICLE_SEEDS.md` に 5 種 (注入飢餓/過剰レビュー/緊急割り込み/
  トレーサビリティ/flaky 教訓) として保存。ja 正本→en/zh/ko 展開・honest disclosure 核。
- 検証: **全 421 テスト pass (2 回連続・choice 系 race を delay で決定論化)**。変更箇所は ruff/mypy クリーン
  (既存 lint 2 件=uuid セミコロン / QThread.event 名衝突 は本変更外)。

### 注入の高優先化 + orchestra レビュー過剰の削減 (2026-06-13 16:25、本セッション・実走ログから発覚)
- **注入の飢餓を解消**: `loop.py` は注入を **継続ターン (`_continue_prompt`) でしか消費していなかった**。
  orchestra は ctx 過大計上 (実走で **ctx 2549%**) で毎ターン rotate するため `_continue_prompt` に
  到達せず、注入 (例: 「進捗を要約できますか？」) が**永久に飲み込まれていた**。→ `_take_injection()` を
  新セッション **opener でも消費**するようにし、rotate を挟んでも次境界で必ず実行される (高優先・飢餓なし)。
- **EXIT 整形のレビュー除去**: rotate のたびの handoff/exit準備が **orchestra フルレビュー (実装+3レビュー+
  集約+修正+sign-off ≈ 7 AI 呼び出し) をまるごと再実行**していた。→ `OrchestraRunner.run_turn_unreviewed()`
  (指揮者のみ) を追加し、`loop._handoff_run_turn()` が handoff/exit準備でそれを使う (記録ターンに 3-AI レビューは過剰)。
- **冗長な最終 sign-off を既定 OFF**: `app.py` の OrchestraRunner を `final_signoff=False` に
  (lead 総合判断=集約が既に審判。修正後の再レビューは「レビューにレビューを重ねる」冗長)。
- 検証: **全 411 テスト pass / 変更箇所は ruff・mypy クリーン**(既存の lint 2 件は本変更外)。
- ★ 残課題: **ctx 2549% の過大計上**(orchestra 指揮者のツール多用×cache 再読込の重複加算)で毎ターン rotate
  し 1 セッション=1 ターンになる。上記で実害 (注入飢餓/レビュー二重) は潰したが、計上自体の是正は別途。

### GUI 進捗サマリの QTabWidget 化 + 記録時刻の正規化 (2026-06-13 16:02、本セッション)
- **タブ化**: 進捗サマリを **「実行中」(選択/実行中 project の SESSION_SUMMARY)** と
  **「共通」(全 project の next_plan.md 集約・記録時刻の新しい順)** の 2 タブに分割
  (`gui/app.py` `summary_tabs` / `common_view` / `_refresh_common_summary`)。
  ※前回は「次セッション送り」で UI 未着手だったため表示が変わって見えなかった分を実装。
- **記録時刻を並び順の正に**: `progress.py` `parse_updated_at()` が本文の
  `最終更新: YYYY-MM-DD HH:MM` を解析し、`collect_progress` がそれを並び順の正に採用
  (内容ベース = git/auto-commit で mtime が動いても正しい)。時刻が無ければ mtime
  フォールバックし共通インデックスに `(ファイル時刻)` と明示。**全 408 テスト pass / ruff・mypy クリーン**。

## 次の一手 (優先順)

1. **【人間】`llterm` を再起動**して新機能を実機確認(進捗サマリ 2 タブ「実行中 / 共通」/
   注入が次境界で即実行され飢餓しない / EXIT 整形と最終 sign-off のレビューが減って 1 周が速い /
   **⚡緊急注入ボタンで現ターンを切って即実行** / 出力ログ各行に時刻 / `~/.llterm/logs/` に時間別ログ /
   **Codex 主 + Claude limit 状態でも lead が Codex 優先になり、Claude reviewer/lead が 1 回失敗後 bench される**こと)。
2. **【Claude・任意】codex の per-call 占有は引き続き未公開として扱う**(`turn.completed` 累積 usage は
   `ctx n/a` として明示済み。**2026-07-10 02:19 JST の再probeでも** `thread.started / turn.started /
   item.completed / turn.completed.usage` 以外は出ず、per-request / span 相当の公開 field は無かった。
   今後は Codex CLI 側の新バージョンで `--json` schema が増えた時だけ取り込みを再検討する)。
3. **【人間・任意】`pip install -e .` 再実行**で `llterm-progress` を `.exe` コマンド登録(`py -3.11 -m llterm.progress` なら即利用可)。
4. **【Claude・任意】`progress.py` は新しい race 実例が出るまで追加 hardening を止める**。
   実ファイル読取りと隔離 `projects_root` 6 連続 probe の両方で stale は再現しなかったため、
   残件があるとしても `CLI 実行経路 / shell 側 readback / キャッシュ` の環境依存に寄っている可能性が高い。
   新しい実例が出たときだけ観測を増やし、再現不能のまま書込み経路へ追加変更は入れない。

## 環境メモ

- 変更ファイル: `loop.py` / `worker.py` / `gui/app.py` / `i18n/messages.py` / `progress.py`(新規) + 各テスト + `pyproject.toml`(`llterm-progress` 追加)。
- `progress.py` 周辺の最新回帰: `py -3.11 -m pytest -q tests/test_progress.py` = **80 passed**、
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- EXIT 時点の最新値: `py -3.11 -m pytest -q tests/test_progress.py` = **88 passed**、
  `ruff check src/llterm/progress.py tests/test_progress.py --isolated` = pass。
- codex は `danger-full-access`(claude の `--dangerously-skip-permissions` と同等の全権。ユーザー決定 2026-06-13)。
- orchestra は claude を 3 回/ターン(reviewer+lead+signoff)使うため claude.ai レート制限を踏みやすい(踏んでも reviewer は best-effort スキップ・codex で継続)。頻発するならレビュー奏者を gemini/codex 寄せでチューニング可。
- 詳細経緯 = raptor memory `project_llterm_native_claude_path`(5 バグ)/ `project_llterm_progress_hitl`(進捗 & HITL 設計)。
