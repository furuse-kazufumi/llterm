# SPDX-License-Identifier: Apache-2.0
"""i18n メッセージテーブル — key → {locale: text} の軽量辞書 (gettext 不使用)。

設計規律:
- **ja / en は全 key で完備**する (テスト ``test_i18n.py`` が完備性を検証)。
- zh / ko は後から各エントリに ``"zh": ...`` / ``"ko": ...`` を追加するだけで対応できる
  (:data:`llterm.i18n.SUPPORTED_LOCALES` への追加も忘れないこと)。
- placeholder は ``str.format`` の名前付き形式 (``{name}``)。ja / en で同じ placeholder
  集合を使う (テストが parity を検証)。
- ja 文字列は既存 UI 文字列と**バイト同一**を保つ (既存テストの文字列 assert を壊さない)。

key 命名: ``<領域>.<部位>.<名前>``
- ``cli.*``      — CLI のエラーメッセージ (emit / loop)
- ``runner.*``   — TurnRunner (claude / codex) のユーザー向けエラー
- ``rad.*``      — RAD 公開ゲートのエラー
- ``template.*`` — テンプレ registry の表示名 / 用途説明 (GUI コンボボックス)
- ``gui.*``      — GUI のラベル / tooltip / メッセージ / ダイアログ
- ``virtual.*``  — 仮想 claude の擬似表示テキスト
"""
from __future__ import annotations

MESSAGES: dict[str, dict[str, str]] = {
    # ─── CLI: llterm-ctl emit ───────────────────────────────────────
    "cli.emit.unknown_action": {
        "ja": "error: 未知の action {action!r} (許可: {allowed})",
        "en": "error: unknown action {action!r} (allowed: {allowed})",
    },
    "cli.emit.reason_required": {
        "ja": "error: --reason は必須です (監査必須)",
        "en": "error: --reason is required (audit-mandatory)",
    },
    "cli.emit.bad_arg": {
        "ja": "error: --arg は KEY=VALUE 形式で指定してください: {value!r}",
        "en": "error: --arg expects KEY=VALUE, got {value!r}",
    },
    "cli.emit.submit_failed": {
        "ja": "error: 投函に失敗しました: {error}",
        "en": "error: submit failed: {error}",
    },
    # ─── CLI: llterm-loop ───────────────────────────────────────────
    "cli.loop.workdir_missing": {
        "ja": "error: --workdir が存在しません: {workdir}",
        "en": "error: --workdir does not exist: {workdir}",
    },
    "cli.loop.no_budget": {
        "ja": "error: --max-sessions か --max-cost のどちらかを指定してください "
              "(無制限自走は課金保護のため拒否)",
        "en": "error: specify --max-sessions or --max-cost "
              "(unlimited self-driving is refused for billing protection)",
    },
    "cli.loop.unknown_template": {
        "ja": "error: 未知のテンプレ: {template} (利用可能: {available})",
        "en": "error: unknown template: {template} (available: {available})",
    },
    # ─── TurnRunner のユーザー向けエラー ─────────────────────────────
    "runner.claude.npm_shim": {
        "ja": "claude が npm shim ({path}) でしか見つかりません。"
              "native インストールの claude.exe が必要です",
        "en": "claude was only found as an npm shim ({path}). "
              "A native claude.exe installation is required",
    },
    "runner.codex.not_found": {
        "ja": "codex が見つかりません",
        "en": "codex was not found",
    },
    # ─── RAD 公開ゲート ──────────────────────────────────────────────
    "rad.staging_missing": {
        "ja": "staging が存在しません: {staging}",
        "en": "staging does not exist: {staging}",
    },
    # ─── テンプレ registry (GUI コンボボックス表示) ───────────────────
    "template.general.label": {
        "ja": "汎用自走",
        "en": "General self-drive",
    },
    "template.general.description": {
        "ja": "前回の続きを自律継続する既定モード。SESSION_SUMMARY / next_plan を読んで"
              "最優先タスクを進める。",
        "en": "Default mode that autonomously continues previous work. Reads "
              "SESSION_SUMMARY / next_plan and advances the top-priority task.",
    },
    "template.rad_expand.label": {
        "ja": "RAD 拡張 (staging)",
        "en": "RAD expansion (staging)",
    },
    "template.rad_expand.description": {
        "ja": "指定分野の RAD コーパスを取得→階層スキル化し staging に生成する。"
              "共有 live への公開は『公開』ボタン(人間ゲート)でのみ行う。",
        "en": "Fetches the RAD corpus for the given domain, builds the skill "
              "hierarchy, and writes it to staging only. Promotion to the shared "
              "live tree happens solely via the Publish button (human gate).",
    },
    "template.rad_expand.param_label": {
        "ja": "分野名 (例: robotics)",
        "en": "Domain name (e.g. robotics)",
    },
    "template.green_keeper.label": {
        "ja": "テスト緑維持",
        "en": "Keep tests green",
    },
    "template.green_keeper.description": {
        "ja": "test / lint / 型チェックを安全な範囲(非破壊修復)で緑に保つ。破壊操作はしない。",
        "en": "Keeps tests / lint / type checks green within safe bounds "
              "(non-destructive fixes only). No destructive operations.",
    },
    "template.doc_update.label": {
        "ja": "ドキュメント整備",
        "en": "Documentation upkeep",
    },
    "template.doc_update.description": {
        "ja": "README / docs を実コードと整合させる。憶測で書かず必ずコードを確認する。",
        "en": "Aligns README / docs with the actual code. Always verifies the "
              "code first — never writes from guesswork.",
    },
    "template.security_audit.label": {
        "ja": "セキュリティ監査",
        "en": "Security audit",
    },
    "template.security_audit.description": {
        "ja": "raptor の /scan(Semgrep)で SAST 監査し docs/SECURITY_AUDIT.md に報告。"
              "read-only(修正/push しない)。",
        "en": "Runs a SAST audit via raptor /scan (Semgrep) and reports to "
              "docs/SECURITY_AUDIT.md. Read-only (no fixes, no push).",
    },
    # ─── GUI: ウィンドウ / 設定行のラベルと tooltip ───────────────────
    "gui.window.title": {
        "ja": "llterm — Claude Code 自走ループ (GUI)",
        "en": "llterm — Claude Code self-driving loop (GUI)",
    },
    "gui.label.project": {
        "ja": "プロジェクト:",
        "en": "Project:",
    },
    "gui.check.real": {
        "ja": "実 claude (claude.ai サブスク認証)",
        "en": "Real claude (claude.ai subscription auth)",
    },
    "gui.tip.real": {
        "ja": "off = 仮想 claude (課金ゼロ)。on = サブスク認証で実走 (従量課金なし・レート制限内)",
        "en": "off = virtual claude (zero cost). on = real run with subscription "
              "auth (no metered billing; bounded by rate limits)",
    },
    "gui.check.rad": {
        "ja": "RAD 参照",
        "en": "RAD reference",
    },
    "gui.tip.rad": {
        "ja": "新規作業前に RAD コーパス (D:/docs/*_corpus_v2) を grep して研究接地する",
        "en": "Greps the RAD corpus (D:/docs/*_corpus_v2) before new work to "
              "ground it in prior research",
    },
    "gui.check.autonomy": {
        "ja": "承認確認不要(完全自律)",
        "en": "No approval prompts (fully autonomous)",
    },
    "gui.tip.autonomy": {
        "ja": "ON: 人間確認を待たず自律で判断・継続(停止しない)。OFF(既定): 安全側",
        "en": "ON: decides and continues autonomously without waiting for human "
              "confirmation (does not stop). OFF (default): safe side",
    },
    "gui.check.codex": {
        "ja": "Codex 切替",
        "en": "Codex fallback",
    },
    "gui.tip.codex": {
        "ja": "ON: Claude がレート制限に達したら Codex に切り替えて作業を継続し、"
              "Claude のリセット時刻が来たら自動で戻す (ChatGPT Pro サブスク=課金なし)。実 claude のみ有効",
        "en": "ON: when Claude hits a rate limit, switches to Codex to keep "
              "working and switches back automatically once Claude's reset time "
              "arrives (ChatGPT Pro subscription = no extra billing). Real claude only",
    },
    "gui.label.max_sessions": {
        "ja": "最大session:",
        "en": "Max sessions:",
    },
    "gui.label.threshold": {
        "ja": "rotate閾値:",
        "en": "Rotate threshold:",
    },
    "gui.tip.threshold": {
        "ja": "この使用率で rotate (exit準備 → 新セッション)",
        "en": "Rotates at this context usage (exit prep → new session)",
    },
    "gui.label.window_tokens": {
        "ja": "窓tokens:",
        "en": "Window tokens:",
    },
    "gui.tip.window_tokens": {
        "ja": "コンテキスト窓サイズ (使用率の分母)。実 claude が実窓サイズ (modelUsage.contextWindow) "
              "を報告した場合はそちらを優先する",
        "en": "Context window size (denominator of usage). If real claude "
              "reports the actual window size (modelUsage.contextWindow), that "
              "value takes precedence",
    },
    "gui.label.max_cost": {
        "ja": "コスト上限$(0=無制限):",
        "en": "Cost cap $ (0 = unlimited):",
    },
    "gui.tip.max_cost": {
        "ja": "報告コストの累計上限 (サブスクでは governor。0 で無制限)",
        "en": "Cumulative cap on reported cost (a governor under subscription; "
              "0 = unlimited)",
    },
    "gui.label.effort": {
        "ja": "effort:",
        "en": "effort:",
    },
    "gui.effort.default_item": {
        "ja": "(claude既定)",
        "en": "(claude default)",
    },
    "gui.tip.effort": {
        "ja": "claude の思考努力レベル (--effort)。max が最上位。実 claude のみ有効。"
              "注: raptor の『ultracode』は vanilla claude には無いため max を使う",
        "en": "claude's reasoning effort level (--effort). max is the highest. "
              "Real claude only. Note: raptor's 'ultracode' does not exist in "
              "vanilla claude — use max",
    },
    "gui.label.model": {
        "ja": "model:",
        "en": "model:",
    },
    "gui.model.default_item": {
        "ja": "(claude既定)",
        "en": "(claude default)",
    },
    "gui.tip.model_select": {
        "ja": "実 claude のモデル (--model)。opus=高品質だが token 消費が大きい、"
              "sonnet/haiku=軽量・高速で token 節約。(claude既定) は claude 側の保存既定に委ねる。"
              "alias は最新世代へ解決。実 claude のみ有効。",
        "en": "Real claude model (--model). opus = highest quality but heavy token use; "
              "sonnet/haiku = lighter, faster, cheaper on tokens. '(claude default)' defers "
              "to claude's saved default. Aliases resolve to the latest generation. Real claude only.",
    },
    "gui.label.template": {
        "ja": "テンプレ:",
        "en": "Template:",
    },
    "gui.placeholder.param": {
        "ja": "(テンプレ引数)",
        "en": "(template argument)",
    },
    "gui.placeholder.param_unused": {
        "ja": "(引数不要)",
        "en": "(no argument needed)",
    },
    "gui.btn.publish": {
        "ja": "公開(staging→live)",
        "en": "Publish (staging→live)",
    },
    "gui.tip.publish": {
        "ja": "RAD 拡張の staging を共有 live へ昇格する公開ゲート(人間の明示操作)。",
        "en": "Publish gate that promotes RAD expansion staging to the shared "
              "live tree (explicit human action).",
    },
    # ─── GUI: ステータス行 / パネル ──────────────────────────────────
    "gui.state.idle": {
        "ja": "idle",
        "en": "idle",
    },
    "gui.tip.state": {
        "ja": "ループの状態 (idle / running / stopping / done)",
        "en": "Loop state (idle / running / stopping / done)",
    },
    "gui.tip.model": {
        "ja": "実行中の claude モデル (init イベントから取得) と effort",
        "en": "Running claude model (taken from the init event) and effort",
    },
    "gui.tip.session": {
        "ja": "現在のセッション / 最大セッション と、セッション内ターン数",
        "en": "Current session / max sessions, and turn count within the session",
    },
    "gui.tip.ctx": {
        "ja": "現セッションのコンテキスト使用率。rotate 閾値に達すると新セッションへ畳む",
        "en": "Context usage of the current session. Folds into a new session "
              "when the rotate threshold is reached",
    },
    "gui.summary.title": {
        "ja": "進捗サマリ (SESSION_SUMMARY)",
        "en": "Progress summary (SESSION_SUMMARY)",
    },
    "gui.btn.refresh": {
        "ja": "↻ 更新",
        "en": "↻ Refresh",
    },
    "gui.tip.refresh": {
        "ja": "docs/SESSION_SUMMARY.md を再読込 (走行中でも最新を取得)",
        "en": "Reload docs/SESSION_SUMMARY.md (fetches the latest even while running)",
    },
    "gui.placeholder.summary": {
        "ja": "docs/SESSION_SUMMARY.md がまだありません",
        "en": "docs/SESSION_SUMMARY.md does not exist yet",
    },
    "gui.placeholder.input": {
        "ja": "タスク注入 / 指示 (Ctrl+Enter で送信)",
        "en": "Inject task / instruction (Ctrl+Enter to send)",
    },
    "gui.btn.start": {
        "ja": "Start",
        "en": "Start",
    },
    "gui.btn.stop": {
        "ja": "Stop",
        "en": "Stop",
    },
    "gui.btn.force_stop": {
        "ja": "強制停止",
        "en": "Force stop",
    },
    "gui.btn.send": {
        "ja": "Send (Ctrl+Enter)",
        "en": "Send (Ctrl+Enter)",
    },
    "gui.progress.idle": {
        "ja": "進捗: -",
        "en": "Progress: -",
    },
    "gui.progress.prefix": {
        "ja": "進捗",
        "en": "Progress",
    },
    "gui.progress.handoff_prefix": {
        "ja": "進捗(handoff)",
        "en": "Progress (handoff)",
    },
    "gui.progress.starting": {
        "ja": "進捗: 開始…",
        "en": "Progress: starting…",
    },
    "gui.tip.progress": {
        "ja": "直近の応答 / rotate 時の SESSION_SUMMARY からの進捗要約",
        "en": "One-line progress summary from the latest response / "
              "SESSION_SUMMARY at rotate",
    },
    # ─── GUI: cost ラベルの種別 (suffix) ─────────────────────────────
    "gui.cost.reported": {
        "ja": "報告値",
        "en": "reported",
    },
    "gui.cost.subscription": {
        "ja": "報告値・課金なし",
        "en": "reported, no charge",
    },
    "gui.cost.billed": {
        "ja": "実課金",
        "en": "billed",
    },
    "gui.cost.virtual": {
        "ja": "仮想・課金なし",
        "en": "virtual, no charge",
    },
    # ─── GUI: 実行モード / 状態文字列 ────────────────────────────────
    "gui.mode.real_billed": {
        "ja": "実claude(API=実課金)",
        "en": "real claude (API = billed)",
    },
    "gui.mode.real_subscription": {
        "ja": "実claude(サブスク=課金なし)",
        "en": "real claude (subscription = no charge)",
    },
    "gui.mode.virtual": {
        "ja": "仮想claude(課金なし)",
        "en": "virtual claude (no charge)",
    },
    "gui.state.running": {
        "ja": "running [{mode}] {template}",
        "en": "running [{mode}] {template}",
    },
    "gui.state.stopping": {
        "ja": "stopping… (作業内容を記録して停止します)",
        "en": "stopping… (recording work before stopping)",
    },
    "gui.state.handoff": {
        "ja": "作業内容を記録中…",
        "en": "Recording work in progress…",
    },
    "gui.state.rate_limited": {
        "ja": "レート制限: 待機中{when}",
        "en": "Rate limit: waiting{when}",
    },
    "gui.state.resumed": {
        "ja": "running (制限解除・再開)",
        "en": "running (limit lifted, resumed)",
    },
    # ─── GUI: 出力ビューのメッセージ ─────────────────────────────────
    "gui.msg.no_project": {
        "ja": "error: プロジェクトが選択されていません (コンボボックスから選んでください)",
        "en": "error: no project selected (choose one from the combo box)",
    },
    "gui.msg.loop_start": {
        "ja": "=== loop 開始 [{mode}] template={template} workdir={workdir} "
              "max_session={max_sessions}{effort_note} ===",
        "en": "=== loop start [{mode}] template={template} workdir={workdir} "
              "max_session={max_sessions}{effort_note} ===",
    },
    "gui.msg.stop_graceful": {
        "ja": "■ 停止要求: 現ターン完了後に作業内容を記録してから停止します "
              "(もう一度 Stop で強制停止)",
        "en": "■ Stop requested: will record work after the current turn, then "
              "stop (press Stop again to force)",
    },
    "gui.msg.stop_force": {
        "ja": "■ 強制停止: 実行中ターンを中断します (作業記録なし)",
        "en": "■ Force stop: interrupting the running turn (no work record)",
    },
    "gui.msg.inject_accepted": {
        "ja": ">> [注入受付] {text}",
        "en": ">> [inject accepted] {text}",
    },
    "gui.msg.inject_pending": {
        "ja": "  (loop 未起動: Start 後に反映されます)",
        "en": "  (loop not running: applied after Start)",
    },
    "gui.msg.promote_need_domain": {
        "ja": "error: 公開する分野名を引数欄に入れてください",
        "en": "error: enter the domain name to publish in the argument field",
    },
    "gui.msg.promote_no_staging": {
        "ja": "error: staging がありません: {staging}",
        "en": "error: staging not found: {staging}",
    },
    "gui.msg.promote_failed": {
        "ja": "公開失敗: {error}",
        "en": "Publish failed: {error}",
    },
    "gui.msg.promoted": {
        "ja": "✓ 公開: {live}",
        "en": "✓ Published: {live}",
    },
    "gui.msg.handoff": {
        "ja": "■ 作業内容を記録中 (SESSION_SUMMARY を更新)…",
        "en": "■ Recording work (updating SESSION_SUMMARY)…",
    },
    "gui.msg.rate_limited_wait": {
        "ja": "⏸ レート制限に到達。{when}待機して自動再開します (Stop で中断可)",
        "en": "⏸ Rate limit reached. Waiting{when}; auto-resumes "
              "(press Stop to interrupt)",
    },
    "gui.when.until": {
        "ja": " {time} まで",
        "en": " until {time}",
    },
    "gui.msg.resumed": {
        "ja": "▶ レート制限リセット — 自走を再開します",
        "en": "▶ Rate limit reset — resuming self-drive",
    },
    "gui.msg.resumed_with": {
        "ja": "▶ レート制限リセット — 自走を再開します ({provider})",
        "en": "▶ Rate limit reset — resuming self-drive ({provider})",
    },
    "gui.model.switched": {
        "ja": "model: {provider} (切替)",
        "en": "model: {provider} (switched)",
    },
    "gui.msg.provider_switch": {
        "ja": "⇄ プロバイダ切替 → {provider} で継続 (SESSION_SUMMARY から再開)",
        "en": "⇄ Provider switch → continuing with {provider} (resumes from "
              "SESSION_SUMMARY)",
    },
    "gui.msg.task_injected": {
        "ja": "▶ 注入タスク実行: {prompt}",
        "en": "▶ Executing injected task: {prompt}",
    },
    "gui.msg.task_sent": {
        "ja": "▶ 指令送信 (turn {turn})",
        "en": "▶ Instruction sent (turn {turn})",
    },
    "gui.msg.turn_head": {
        "ja": "[turn {turn}] 応答受信 ctx {pct}%{err_note}",
        "en": "[turn {turn}] response received ctx {pct}%{err_note}",
    },
    "gui.msg.session_start": {
        "ja": "--- {label} 開始 ({sid}) ---",
        "en": "--- {label} started ({sid}) ---",
    },
    "gui.msg.rotate": {
        "ja": "--- rotate (ctx {pct}%) → exit準備 & 新セッションへ ---",
        "en": "--- rotate (ctx {pct}%) → exit prep & new session ---",
    },
    "gui.msg.auth_required": {
        "ja": "⚠ 再ログインが必要です (claude /login)。認証後に Start で再開してください "
              "— 構造的に唯一の人間介在点。",
        "en": "⚠ Re-login required (claude /login). Restart with Start after "
              "authenticating — structurally the only human intervention point.",
    },
    "gui.stream.tool_error": {
        "ja": "  ↳ エラー: {preview}",
        "en": "  ↳ error: {preview}",
    },
    "gui.stream.rate_limit": {
        "ja": "⚠ レート制限: {status}{when}",
        "en": "⚠ Rate limit: {status}{when}",
    },
    "gui.stream.rate_limit_reset": {
        "ja": " (リセット: {time})",
        "en": " (resets: {time})",
    },
    # ─── GUI: ダイアログ ─────────────────────────────────────────────
    "gui.dialog.promote.title": {
        "ja": "RAD 公開ゲート",
        "en": "RAD publish gate",
    },
    "gui.dialog.promote.body": {
        "ja": "分野「{domain}」の staging を共有 live へ公開しますか?\n"
              "  staging: {staging}\n  live: {live}\n"
              "既存 live はバックアップされます。",
        "en": "Publish the staging of domain \"{domain}\" to the shared live "
              "tree?\n  staging: {staging}\n  live: {live}\n"
              "The existing live tree will be backed up.",
    },
    "gui.dialog.close.title": {
        "ja": "終了確認",
        "en": "Confirm exit",
    },
    "gui.dialog.close.body": {
        "ja": "ループが実行中です。作業内容を記録して安全に終了しますか?\n"
              "「はい」= 記録してから終了 /「いいえ」= 終了しない\n"
              "(記録完了までウィンドウは開いたまま・砂時計表示になります)",
        "en": "The loop is running. Record the work and exit safely?\n"
              "\"Yes\" = record then exit / \"No\" = do not exit\n"
              "(the window stays open with a busy cursor until recording completes)",
    },
    # ─── 仮想 claude の擬似表示テキスト ──────────────────────────────
    "virtual.turn_text": {
        "ja": "[virtual claude] 処理: {prompt}\n"
              "  session={sid} ctx≈{ctx:,} tok (turn #{n})",
        "en": "[virtual claude] processed: {prompt}\n"
              "  session={sid} ctx≈{ctx:,} tok (turn #{n})",
    },
    "virtual.tool_detail": {
        "ja": "turn #{n} を擬似実行",
        "en": "simulating turn #{n}",
    },
    "virtual.tool_result": {
        "ja": "(仮想ツール結果)",
        "en": "(virtual tool result)",
    },
}
