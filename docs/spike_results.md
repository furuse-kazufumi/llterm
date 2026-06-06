# spike 結果記録

## Task 6: 修飾キー検出 (spike_keys.py) — ✅ 完全成立 (2026-06-06 夜, ユーザー実機・完全ログ取得)

- 実行環境: ユーザーの実コンソール (Windows 11, PowerShell)。
- **判定: R12/R13 の技術前提が完全成立** (実ログより):
  - 素の Enter: `vk=13 char='\r' mods=- state=0x0`
  - **Ctrl+Enter: `vk=13 char='\n' mods=CTRL state=0x8`** — char が `\n` に変わる (legacy console 伝統挙動)。
    decoder は vk+mods 判定なので影響なし。
  - **Shift+Enter: `vk=13 char='\r' mods=SHIFT state=0x10`**
  - **Ctrl+↑: `vk=38 char='\x00' mods=CTRL state=0x108`** — 0x100 = ENHANCED_KEY (矢印等の拡張キー
    フラグ)。decoder は SHIFT/CTRL ビットのみ参照するため影響なし。
  - 観察事項: **Ctrl/Shift 押下中は vk=17/16 (VK_CONTROL/VK_SHIFT) 単独 keydown がキーリピートで連続発生**。
    → 本実装 (input/keys.py decode) では非印字 char + 対象外 vk として Action.NONE に落ちるため無害
    (test_control_chars_ignored でカバー)。console.py の wRepeatCount 展開でも同様に NONE。
- 未確認 (本実装スモークで再確認): IME 確定文字が `char` にどう届くか (変換確定時のイベント形)。
  R10 の受け入れ確認 (Task 13 Step 5) で必ず見る。

## Task 7: 縮小 PTY + DECSTBM 入力欄分離 (spike_ptyrows.py) — 未実施

- 実行待ち: `py -3.11 spikes/spike_ptyrows.py -- pwsh -NoLogo`
- 判定基準: 子の出力・全画面再描画が下 4 行の `[LLTERM INPUT AREA]` を上書きしないこと。
