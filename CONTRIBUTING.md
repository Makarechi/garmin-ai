# Development workflow

`main` holds the latest completed and verified work. New work uses a focused
branch such as `feat/garmin-coverage` or `fix/sync-retry`.

1. Update local `main` from GitHub and create a branch.
2. Define what must work for the change to be complete.
3. Implement the change with suitable documentation and tests.
4. Run relevant checks, inspect the full diff, and check for private data.
5. Open a pull request describing the result, validation, and limitations.
6. Resolve failures before merging. Squash the pull request into `main`, delete
   its completed branch, and synchronize local `main`.

The owner has authorized routine PR creation and merging for this project.
Unresolved failures or required user input should be reported rather than
treated as completed work.

## Current checks

This repository currently contains documentation and Git configuration only.
Check changes with `git diff --check` and review the documents and ignore rules.
Application tests and CI must be introduced alongside executable functionality;
there is no application test suite yet.

## Data handling

Keep runtime data in ignored local directories. Only synthetic or explicitly
redacted fixtures may be committed. Examples must use placeholders for secrets.
Repository privacy does not make it safe to commit personal health records.
