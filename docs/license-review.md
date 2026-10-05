# License scope and distribution review

The owner selected Apache-2.0 for this project's own code on 2026-10-04. The
repository includes the unmodified [Apache License 2.0 text](https://www.apache.org/licenses/LICENSE-2.0.txt)
and the wheel identifies `Apache-2.0` and includes that text. The license does
not replace the licenses of dependencies, container images, or user-supplied
extensions. Garmin AI is not affiliated with Garmin.

## Source and dependency check at the first community stage

The Git history at the baseline commit lists the `Makarechi` account and two
spelling/name variants associated with that account. A repository search found
no vendored dependency trees or third-party license headers in `src/` or
`scripts/`. This is a source review, not a warranty that every historical
contribution has been independently cleared.

The locked full Python environment was inspected through installed package
metadata. Most packages declare permissive licenses. Distribution-sensitive
entries include `python-telegram-bot` and `psycopg`/`psycopg-binary`
(`LGPL-3.0-only`), `certifi` (`MPL-2.0`), and packages with multiple embedded
licenses such as NumPy and SciPy. These are separate third-party works; none is
relicensed as Apache-2.0 here. Their license files and source availability
must be checked for each published image and release archive.

The Compose database image uses `timescale/timescaledb:latest-pg17` pinned by
digest. [Timescale's own terms](https://www.timescale.com/legal/licenses)
distinguish its Apache-2.0 Open Source edition from Community features under
the Timescale License. The default image must not be described as wholly
Apache-2.0. The app Dockerfile also uses pinned Python and uv images. Their
contents and licenses belong in the eventual image inventory, not in this
project's `LICENSE`.

## Before publishing a versioned image or release

For the exact release digest, regenerate an inventory of all direct and
transitive Python distributions, collect their license texts and required
notices, inspect the Python/uv/Timescale image contents and terms, and verify
the final artifact includes what each license requires. CP-05 release
packaging remains open until that artifact-level check is complete. This page
records a preliminary review and must not be used as a release clearance.
