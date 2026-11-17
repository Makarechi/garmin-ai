# Legacy UTF-8 JSON digests

The Rust domain library implements the sorted, literal-Unicode byte layout used by
`archive.LocalArchive.put_json` and the compact variant used by `source_runtime._digest`.
Both use lowercase SHA-256. Choose the layout for the contract before hashing. The
caller must provide an input size limit.

The checked-in input and two expected byte strings were generated with CPython 3.15.2
from baseline `ee3a14a48a6ad30c1085951bac2f53c9088c2450`. Tests check exact bytes and
digest for both layouts. The sample covers separate boolean and number values, omitted
and null fields, wide integers, repeated keys, negative zero, exponent cutoffs, paired
UTF-16 escapes, and Unicode reordering. Additional cases reject oversize documents, the
4,301st integer digit, invalid UTF-8, lone surrogate escapes, and nonfinite numbers.

A separate seeded comparison of 118,872 finite floats (seed `468014`) covered random
binary64 payloads, each power of ten and its immediate neighboring samples, and
decimal halfway cases. The Python and Rust archive representations had SHA-256
`d62fd488fb4357371d67d59468f042da3cad3b86801c0c86ff76594c110547b6` in both runs.
[CPython JSON settings](https://docs.python.org/3.15/library/json.html)
[Żmij number formatting](https://github.com/dtolnay/zmij)

The parser currently accepts UTF-8 and up to 200 nested containers. Before wiring this
library to a production input, compare that input's accepted encoding and nesting to
these limits. The ASCII-escaped schema digests and filesystem archive integration are
separate migration work.
