"""VT 応答分離フィルタ — 実端末からの端末応答シーケンスをキー入力と分離する.

実機知見 (2026-06-07): 子 (claude) は起動時に端末能力クエリ (DA1 `CSI c`, OSC 11 等) を
発する。llterm はそれを素通しで実端末へ書くため、実端末は応答 (`CSI ? 61;... c`,
`OSC 11 ; rgb:... ST`) を**コンソール入力**に書き戻す。これをキー入力と誤認すると
(1) 入力欄が `[?61;...` で汚染され、(2) 子は応答を受け取れず能力検出が壊れる。

本フィルタは KeyEvent ストリームから応答シーケンスを取り出し、呼び出し側 (App) が
そのまま子 PTY へ write_raw() で転送できる形で返す。

状態機械:
- ESC → '[' で CSI モード (final byte 0x40-0x7E で完結)
- ESC → ']' で OSC モード (BEL または ESC \\ (ST) で完結)
- ESC 後に上記以外 → ユーザーの Esc キーとみなし、ESC とその文字を両方キー側へ flush
- 純 vk イベント (char が空/NUL) は収集中でも常にキー側へ (矢印キー等を巻き込まない)
- バッチ境界をまたぐ分割到着に対応 (モードを feed() 間で保持)
"""
from __future__ import annotations

from llterm.input.keys import KeyEvent

_ESC = "\x1b"
_BEL = "\x07"


class VtResponseFilter:
    def __init__(self) -> None:
        self._mode: str | None = None      # None / "esc" / "csi" / "osc" / "osc_esc"
        self._seq: list[str] = []
        self._esc_event: KeyEvent | None = None   # ESC 単押し flush 用に保持

    def feed(self, events: list[KeyEvent]) -> tuple[list[KeyEvent], list[str]]:
        """イベント列を (通常キー, 完結した応答シーケンス) に分離する."""
        keys: list[KeyEvent] = []
        responses: list[str] = []
        for ev in events:
            ch = ev.char
            if not ch or ch == "\x00":
                keys.append(ev)            # 純 vk イベントは常にキー (収集を乱さない)
                continue

            if self._mode is None:
                if ch == _ESC:
                    self._mode = "esc"
                    self._seq = [ch]
                    self._esc_event = ev
                else:
                    keys.append(ev)
            elif self._mode == "esc":
                if ch == "[":
                    self._mode = "csi"
                    self._seq.append(ch)
                elif ch == "]":
                    self._mode = "osc"
                    self._seq.append(ch)
                else:
                    # シーケンスでない: ユーザー Esc + 後続文字として両方 flush
                    if self._esc_event is not None:
                        keys.append(self._esc_event)
                    keys.append(ev)
                    self._reset()
            elif self._mode == "csi":
                self._seq.append(ch)
                if "\x40" <= ch <= "\x7e":         # final byte で完結
                    responses.append("".join(self._seq))
                    self._reset()
            elif self._mode == "osc":
                if ch == _BEL:
                    self._seq.append(ch)
                    responses.append("".join(self._seq))
                    self._reset()
                elif ch == _ESC:
                    self._mode = "osc_esc"          # ST (ESC \) の途中かもしれない
                    self._seq.append(ch)
                else:
                    self._seq.append(ch)
            elif self._mode == "osc_esc":
                self._seq.append(ch)
                if ch == "\\":                      # ST 完結
                    responses.append("".join(self._seq))
                    self._reset()
                else:
                    self._mode = "osc"              # ST でなかった: OSC 本文継続
        return keys, responses

    def _reset(self) -> None:
        self._mode = None
        self._seq = []
        self._esc_event = None
