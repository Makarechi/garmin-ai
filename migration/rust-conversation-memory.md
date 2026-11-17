# R25 Neutral conversation memory snapshots

The native snapshot reads one owner's analytic context under the existing write fence. It includes other conversations only when both conversations opted into sharing and the stored source epochs still match. The snapshot carries the exact target and source epochs. It keeps up to six turns from the last seven days within the Python 12,000-byte JSON budget. Forgetting rotates the target epoch and clears its state. Withdrawing sharing also removes dependent turns from the same owner's conversations.

The pinned Python dialogue service generated 12 fixture steps covering local context, opt-in, exact time limits, multibyte size limits, stale sources, forgetting, and changing or repeating sharing preferences. The native test compares returned turns and persisted conversation state. It also checks owner and write-lock guards. The optimized Linux arm64 run used 9,596,928 bytes peak with zero swap under a 32 MiB memory cap.
