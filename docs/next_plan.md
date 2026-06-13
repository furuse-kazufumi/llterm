# next_plan (正本) — llterm 自走ループ駆動 + GUI

> 最終更新: 2026-06-13 16:25 JST
> ★ 最終更新は **必ず `YYYY-MM-DD HH:MM JST` (時刻つき)** で書く。日付のみだと同日内の前後を
>   判定できず、共通進捗の並び順が信用できない(`progress.py` は本文のこの時刻を解析して
>   並び順の正に使い、時刻が無いと mtime にフォールバックする。ユーザー指摘 2026-06-13)。
> SESSION_SUMMARY.md は raptor Stop hook で自動上書きされるため、**このファイルが再開の正本**。
> 共通進捗 = `D:/projects/_shared/PROGRESS.md`(全 project 集約・`llterm-progress` で再生成)。

## 現在地

- llterm v0.2.0a0(公式 headless protocol `claude -p --session-id/--resume` で Claude Code を自走ループ駆動 + PySide6 GUI)。
- 2026-06-13 セッションで「ループが err=other→circuit_open で死ぬ」問題を根治し、進捗引継ぎ & HITL モデルを実装。
  続けて進捗サマリの 2 タブ化(実行中 / 共通)+ 記録時刻の正規化を実装。**全 408 テスト pass**。
- GUI 変更はプロセス再起動で反映(起動中の llterm には出ない)。auto-commit 監視が編集を逐次拾う。

## 直近の成果

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
   注入が次境界で即実行され飢餓しない / EXIT 整形と最終 sign-off のレビューが減って 1 周が速い)。
2. **【Claude・任意】ctx 過大計上の是正**(残課題)。orchestra 指揮者の context_tokens が cache 再読込で
   2549% 等に膨れ、毎ターン rotate して 1 セッション=1 ターンになる。`used_pct` の分子(context_tokens)を
   ツール往復で重複加算しない算定に直すか、orchestra は指揮者の最終 assistant usage のみ採るなどを検討。
3. **【Claude・任意】注入を orchestra フルレビュー対象から外す**検討(「要約して」等の問い合わせ系注入に
   実装+3レビュー+修正は過剰。注入ターンは run_turn_unreviewed 寄せ or 簡易判定の余地)。
4. **【人間・任意】`pip install -e .` 再実行**で `llterm-progress` を `.exe` コマンド登録(`py -3.11 -m llterm.progress` なら即利用可)。
4. **【Claude】共通サマリの自動更新トリガ配線**(loop rotate 後に `write_common_summary` / または Windows スケジュールタスクで `llterm-progress`)。
5. **【Claude・任意】共通タブを project 別サブタブ化**(現状は共通タブ 1 枚に全 project を時刻順で収録。1 project=1 タブが必要なら拡張)。
6. **【Claude】exit-prep プロンプトの標準フォーマット化**(next_plan.md を `## 現在地/直近の成果/次の一手/環境メモ` で更新)+ 他 project の next_plan.md 整備。

## 環境メモ

- 変更ファイル: `loop.py` / `worker.py` / `gui/app.py` / `i18n/messages.py` / `progress.py`(新規) + 各テスト + `pyproject.toml`(`llterm-progress` 追加)。
- codex は `danger-full-access`(claude の `--dangerously-skip-permissions` と同等の全権。ユーザー決定 2026-06-13)。
- orchestra は claude を 3 回/ターン(reviewer+lead+signoff)使うため claude.ai レート制限を踏みやすい(踏んでも reviewer は best-effort スキップ・codex で継続)。頻発するならレビュー奏者を gemini/codex 寄せでチューニング可。
- 詳細経緯 = raptor memory `project_llterm_native_claude_path`(5 バグ)/ `project_llterm_progress_hitl`(進捗 & HITL 設計)。
