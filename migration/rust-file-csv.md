# Bounded CSV input for tracker imports

The CSV parser preserves UTF-8 headers, row order, empty fields, quoted
line endings and escaped quotes. It matches the Python file and row budgets
and rejects malformed syntax, missing or surplus columns and duplicate
headers. Headers are retained once even when every row has the same long
field name.

`check_file_csv.py` uses the pinned Python importer to generate 291
synthetic cases. On native Linux ARM64 the results matched, including exact
byte and field limits, multiline fields, Unicode, invalid input and
260 deterministic generated cases. The process peaked at 10.6 MiB under
a 32 MiB limit with zero swap or OOM events. Mapping, preview and apply use
this input format in subsequent changes.
