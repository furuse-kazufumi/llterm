# spike 結果記録

## Task 6: 修飾キー検出 (spike_keys.py) — ✅ 成立 (2026-06-06 夜, ユーザー実機)

- 実行環境: ユーザーの実コンソール (Windows 11)。
- **判定: R12/R13 の技術前提が成立**。
  - Ctrl+Enter / Shift+Enter / Ctrl+↑ が `dwControlKeyState` の mods で区別できた (ユーザー実機確認「期待通り」)。
  - 観察事項: **Ctrl 押下中は vk=17 (VK_CONTROL) の keydown がキーリピートで連続発生**する。
    → 本実装 (input/keys.py decode) では非印字 char + 対象外 vk として Action.NONE に落ちるため無害
    (test_control_chars_ignored でカバー)。console.py の wRepeatCount 展開でも同様に NONE。
- 未確認 (本実装スモークで再確認): IME 確定文字が `char` にどう届くか (変換確定時のイベント形)。
  R10 の受け入れ確認 (Task 13 Step 5) で必ず見る。

## Task 7: 縮小 PTY + DECSTBM 入力欄分離 (spike_ptyrows.py) — 未実施

- 実行待ち: `py -3.11 spikes/spike_ptyrows.py -- pwsh -NoLogo`
- 判定基準: 子の出力・全画面再描画が下 4 行の `[LLTERM INPUT AREA]` を上書きしないこと。
