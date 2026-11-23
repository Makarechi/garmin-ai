# GCP single-host deployment

## Native release bundle

Use a verified Linux AMD64 [installation bundle](release-install.md) on the
dedicated Debian host. Extract it as the deployment user. Bootstrap a fresh host
as root, then configure and start the release as the deployment user:

```sh
sudo ./deploy/gcp/bootstrap.sh
./install.sh --gcp setup
./install.sh --gcp start
./install.sh --gcp status
```

The GCP profile applies the small-host memory limits and bounded Docker logs.
The host needs Docker, Compose, and the standard Debian shell tools.
The application and worker run their own readiness checks. Setup, demo, backup,
recovery, and host retention use the binaries packaged with the release.

When upgrading, keep the existing Compose project name and storage paths.
Follow the [backup and upgrade procedure](release-install.md#update-safely)
before starting the replacement. Run the same `--gcp` form for `stop`,
`backup`, and `unpack-backup`. Keep the prior release available for rollback.
An already bootstrapped host can continue to use its existing Docker installation.

For an offline upload, export the complete Docker image from the build host.
Use the same repository as the pinned reference in `release.env` and keep the
versioned tag. A platform-filtered export can change the reference when the
image includes attestations.

```sh
# On the build host, after building and verifying the AMD64 release.
docker image save garmin-ai:gcp-RELEASE_ID | gzip > garmin-ai-gcp-RELEASE_ID.tar.gz

# On the deployment host, after transferring the verified archive.
docker image load -i garmin-ai-gcp-RELEASE_ID.tar.gz
image=$(sed -n 's/^GA_APP_IMAGE=//p' release.env)
docker image inspect "$image" --format '{{.Os}}/{{.Architecture}} {{.Id}}'
```

Continue with the uploaded image once Docker resolves the exact release
reference for `linux/amd64`. Stage the pinned database image as well when
installing a clean host offline.

For the optional browser sign-in gateway, set these values in the private
`.env` before starting the profile. Substitute the hostname approved for Garmin
sign-in.

```dotenv
COMPOSE_PROFILES=garmin-web-auth
GA_GARMIN_AUTH_HOST=garmin.example.org
GA_GARMIN_AUTH_URL=https://garmin.example.org/garmin-auth
```

The gateway serves the sign-in path on HTTPS. The main API and database ports
remain on the loopback interface. Configure DNS and incoming gateway traffic
only when enabling this feature.

Once both application services are healthy, follow the
[native cleanup installation](release-cleanup.md#native-release-bundle).
