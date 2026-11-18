# Erasing private file roots

The native file stage validates both private roots before the database erasure. It checks the working directory, home, state directory, and backup separation. It opens directories without following links and checks their identities again before removal. A repeat can finish after one root was already removed.

Three tests passed on macOS and native Linux ARM64. They removed 500 synthetic files, preserved separately stored state and backup files, rejected redirects, overlaps, and a replacement directory, and recovered from a fault after removing the first root. The Linux run peaked at 7,811,072 bytes in a 32 MiB container with swap disabled.
