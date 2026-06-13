# next_plan (正本) — llterm 自走ループ駆動 + GUI

> 最終更新: 2026-06-13 (EXIT 準備)
> SESSION_SUMMARY.md は raptor Stop hook で自動上書きされるため、**このファイルが再開の正本**。
> 共通進捗 = `D:/projects/_shared/PROGRESS.md`(全 project 集約・`llterm-progress` で再生成)。

## 現在地

- llterm v0.2.0a0(公式 headless protocol `claude -p --session-id/--resume` で Claude Code を自走ループ駆動 + PySide6 GUI)。
- 2026-06-13 セッションで「ループが err=other→circuit_open で死ぬ」問題を根治し、進捗引継ぎ & HITL モデルを実装。**全 400 テスト pass**。
- 未コミット: `tests/test_loop.py`(auto-commit 監視が拾う)。GUI 変更はプロセス再起動で反映。

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

## 次の一手 (優先順)

1. **【人間】`llterm` を再起動**して新機能を実機確認(承認確認不要トグルがメイン画面に出る / 注入→監督→回答→復帰の流れ / 安全弁)。
2. **【人間・任意】`pip install -e .` 再実行**で `llterm-progress` を `.exe` コマンド登録(`py -3.11 -m llterm.progress` なら即利用可)。
3. **【Claude・次セッション】GUI 進捗サマリの QTabWidget 化**(「共通(_shared/PROGRESS.md)」+ project 別タブ、各タブ next_plan.md を digest 表示)。
4. **【Claude】共通サマリの自動更新トリガ配線**(loop rotate 後に `write_common_summary` / または Windows スケジュールタスクで `llterm-progress`)。
5. **【Claude】exit-prep プロンプトの標準フォーマット化**(next_plan.md を `## 現在地/直近の成果/次の一手/環境メモ` で更新)+ 他 project の next_plan.md 整備。

## 環境メモ

- 変更ファイル: `loop.py` / `worker.py` / `gui/app.py` / `i18n/messages.py` / `progress.py`(新規) + 各テスト + `pyproject.toml`(`llterm-progress` 追加)。
- codex は `danger-full-access`(claude の `--dangerously-skip-permissions` と同等の全権。ユーザー決定 2026-06-13)。
- orchestra は claude を 3 回/ターン(reviewer+lead+signoff)使うため claude.ai レート制限を踏みやすい(踏んでも reviewer は best-effort スキップ・codex で継続)。頻発するならレビュー奏者を gemini/codex 寄せでチューニング可。
- 詳細経緯 = raptor memory `project_llterm_native_claude_path`(5 バグ)/ `project_llterm_progress_hitl`(進捗 & HITL 設計)。
