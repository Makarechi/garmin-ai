# Rust migration – JSON import rows

The JSON reader uses the existing 512,000-byte and 500-row limits. It
rejects duplicate keys throughout the document and accepts decimals only
when Python's shortest finite float representation preserves their exact
decimal value. Integers keep their full supported precision. Escaped
unpaired surrogates and NUL characters yield row-level reasons with no
source text, in the same order as the Python preview.

`check_file_json.py` compared 260 synthetic outcomes against pinned
Python 3.15.2 on Linux ARM64. The native test ran in a network-isolated
32 MiB container with a peak of 22,118,400 bytes, zero swap and no
out-of-memory events. Cases include a whole file at the byte ceiling,
500 object rows, a dense array, conflicting Unicode escapes, negative
zero, exact decimals and 4,300-digit integers.

The native reader stops at 1,000 nested containers, including when the
512,000-byte bound would admit a deeper document. Python's C decoder can
accept deeper inputs. The bound keeps recursive serialization or disposal
of the decoded values within the application stack budget. Import planning
and confirmation consume this reader in separate migration work.
