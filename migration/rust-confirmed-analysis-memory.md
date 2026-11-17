# R25 Confirmed analysis memory

The native dialogue store retains an analysis turn after a matching delivered or read receipt. It checks the exact stored question and answer, the latest inbound revision, and the target and source memory epochs. It preserves the Python age, text and retained context limits. A delivery flag without a corresponding receipt cannot introduce stored analysis.

The pinned Python dialogue service generated 13 synthetic cases, including accepted-only delivery, repeated confirmation, mismatched text, edits, old and future messages, the exact age limit, multibyte text and changed memory epochs. The native test matched every stored state. It also rejected a synthetic delivered row after removal of the receipt. The Linux arm64 run used 7,491,584 bytes peak with zero swap in a 32 MiB container.
