# SPDX-License-Identifier: Apache-2.0
"""llterm L3 (GUI) — PySide6 製。端末を使わない自走ループの窓口。

端末 (PTY/ConPTY/win32-input-mode) を捨て GUI にしたことで、長テキストのフリーズ・
ちらつき・桁ずれ・IME 飛び・複数行ペースト・Enter 誤送信・矢印衝突 (spec R3/R4/R8/R10-R13)
が「端末固有の難問」から「Qt ウィジェットの標準挙動」になる。
"""
