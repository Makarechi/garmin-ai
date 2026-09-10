# Independent local instances (GA-25, first phase)

Use a separate clone and private directories for each owner. In a new clone run:

```sh
uv run python scripts/configure.py --instance garmin-owner-a --db-port 55433 --api-port 8081
```

For another owner use another clone, another name, and two other unused ports.
Compose scopes its network and PostgreSQL volume by the persisted project name.
Local application image tags are also scoped, so building one clone does not replace
another clone's image tag. All published ports remain bound to 127.0.0.1.
The container database endpoint remains db:5432; the generated host URL uses the
selected port. Check port availability before starting Compose.

Setup without arguments retains the previous garmin-ai / 55432 / 8080 defaults.
Repeat setup without arguments to preserve names, ports, generated keys and absolute
storage paths. Setup refuses CLI identity/port changes after a real database URL has
been saved; renaming a running Compose project requires a separately planned migration.
Do not override COMPOSE_PROJECT_NAME in the shell or pass docker compose -p with a
conflicting name: Docker gives those overrides precedence over the file.

Distinct project names are required across all clones on the host. Do not copy another
owner's .env, token store, archive, backup key or credentials. Configure a separate bot
and owner for each instance. This is independent single-owner deployment, not a shared
multi-user database. The GCP override's GA_IMAGE remains an explicit operator choice.

Validation covers two synthetic setup directories, preserved settings and keys,
invalid arguments, protected existing identity and resolved Compose names/ports/volumes.
No production migration or simultaneous live-owner deployment is performed.
Owner pairing and a complete guided authentication/privacy wizard remain later phases.
