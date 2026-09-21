# ASR wake gateway

Record once, even when your transcription server is asleep.

An initial single-owner gateway for an existing Speaches deployment. It durably accepts a multipart audio upload, wakes a backend if necessary, waits for readiness, forwards the upload, and retains the transcription for retrieval. It runs on an always-on CPU host; inference remains on your existing GPU machine.

**Status:** first implementation. Local queue/WSGI tests passed. The operator has built and run the container and verified real backend routing, wake-triggered transcription, and recovery of unfinished work across a gateway restart. Traefik browser ingress has been exercised. A browser cold-start failure prompted improved wake retries, job logs, retained failed uploads, and explicit retry. Those changes and the new browser login still need a real deployment check; Open WebUI integration also remains pending. These checks do not establish general deployment compatibility.

Follow [TESTING.md](TESTING.md) for the copy-paste integration test ladder: shell variables, routing diagnostics, audio uploads, cold start, restart recovery, Traefik, the recorder, and Open WebUI.

## Run on your own host

Copy `.env.example` to `.env` and edit its placeholders. Generate a gateway key with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. Keep `.env` private. Set:

- `ASR_BACKEND_URL`: your existing Speaches base URL ending in `/v1`.
- `ASR_HEALTH_URL`: a route that returns HTTP 200 only when Speaches is serving; normally `/health`. Test its behavior while the GPU host is asleep. A generic proxy landing page is not a readiness check.
- `ASR_MODEL`: the existing Speaches model ID, not its display name.
- `ASR_BACKEND_KEY`: the credential Speaches requires, if any. The gateway key is separate.
- `ASR_WAKE_MODE=tcp`, `ASR_WAKE_HOST`, `ASR_WAKE_PORT=9`: connect and immediately close, matching a connection-triggered systemd wake socket. No MAC address is needed in this mode.

The supplied `host.docker.internal:host-gateway` mapping is for Linux Docker. Verify it reaches the address on which your wake socket actually listens; custom bridge and rootless configurations may differ. The public example deliberately contains no actual host inventory.

```sh
docker compose up --build -d
```

The example binds only to localhost port 8080. Connect the service to your existing Traefik network and route HTTPS to container port 8080, or use the localhost port from a host-side proxy. A Traefik container cannot reach this service through its own localhost. Add your existing network configuration locally; it is intentionally not guessed here.

Keep one Gunicorn process and one replica. The dispatcher uses an exclusive file lock; do not use `--preload` or attempt multiple replicas. The SQLite volume must be local storage, not NFS. The example is intended for modest trusted-user concurrency, not an unauthenticated internet endpoint.

## Compatible synchronous API

```sh
curl https://asr.example.test/v1/audio/transcriptions \
  -H "Authorization: Bearer $GATEWAY_KEY" \
  -F 'model=your-speaches-model-id' \
  -F 'file=@recording.wav' \
  -F 'response_format=json'
```

The multipart body is stored and forwarded unchanged, including extra parameters understood by Speaches. The caller must supply its model. This gateway supports buffered JSON, plain text, VTT, and SRT results; it does not implement streaming transcription. Backend response validation is limited to status, size, and content type.

For Open WebUI, select its OpenAI-compatible STT provider and configure the gateway's `/v1` URL, gateway key, and actual model ID. This changes the STT configuration; it does not require replacing your LLM provider. Confirm the setting names in your installed UI.

The gateway waits up to 180 seconds by default, enough to begin testing a measured 72-second boot with room for short transcription. All client and proxy request timeouts must also allow boot plus transcription. A client can still time out sooner; the gateway cannot override that. On its own wait timeout, the gateway returns HTTP 504 with a job ID and status URL. The job remains queued/running. An unchanged synchronous client will not automatically retrieve it later.

`GET /v1/models` advertises the configured model even while the backend sleeps. It does not indicate that the GPU is already ready.

## Asynchronous API and recorder

Send the same multipart upload to `POST /jobs`. HTTP 202 means the full request was committed to SQLite. The response contains `id`, `status_url`, and `result_url`. Use your gateway bearer key for all job reads:

```sh
curl https://asr.example.test/jobs/JOB_ID \
  -H "Authorization: Bearer $GATEWAY_KEY"
curl https://asr.example.test/jobs/JOB_ID/result \
  -H "Authorization: Bearer $GATEWAY_KEY"
```

Open `/` over HTTPS for the recorder. Sign in with `ASR_BROWSER_USERNAME` (default `owner`) and `ASR_BROWSER_PASSWORD` (a long passphrase, at least 16 characters). If the browser password is empty, the API key is accepted as the password for compatibility. This is a single owner account. The HTML form uses conventional username/password fields and a normal POST/redirect for password-manager recognition; the actual save prompt depends on the browser/manager.

The browser receives a Secure, HttpOnly, SameSite=Strict session cookie lasting `ASR_SESSION_SECONDS` (default 30 days). Cookie-authenticated mutations require a matching HTTPS Origin/Host; Traefik must preserve the browser Host header. TLS ends at Traefik; the application still serves HTTP internally. Rotating the API key or browser credentials invalidates sessions. Sign-out clears the local cookie; it does not centrally revoke copies of that session token. Browser login requires HTTPS; curl and Open WebUI continue using their API bearer key over your chosen internal route.

Models load automatically from the gateway's `/v1/models`, even while Speaches sleeps. The first is selected by default, and the browser remembers a previous selection if still available. Currently the gateway advertises only `ASR_MODEL`; this is not automatic discovery of all Speaches models.

The current job ID remains visible and is saved in localStorage. A transient polling failure reconnects up to three times, then offers **Check saved job**. A server-side failed job shows whether audio is retained and offers **Retry transcription**, creating a new job without re-recording. This retry uses a stable idempotency key if the response is lost. Legacy failed jobs whose audio was already deleted cannot be recovered. Unaccepted recordings remain only in browser memory and cannot survive a reload.

`Idempotency-Key` deduplicates the **exact multipart body and content type** within the retained job's lifetime. Reusing a key with changed content returns 409. Fresh `curl -F` calls generally generate different boundaries, so they are not byte-identical retries. The recorder retains its serialized request for retries. This deliberately narrow contract avoids pretending that raw-body hashing provides semantic deduplication.

## Failure model

Audio and metadata live in the same SQLite transaction, removing a separate file/database commit boundary. One dispatcher claims jobs and records an attempt token. After a process restart, unfinished jobs become eligible for retry. Tokens fence stale result writes.

Inference may execute twice if a backend finishes but its response is lost. We guarantee neither exactly-once inference nor safe retry for arbitrary side-effecting workloads. This implementation is specifically for transcription.

Transcription connection failures and selected transient HTTP statuses retry at most twice by default. A failed wake connection no longer immediately consumes an attempt: the dispatcher continues polling health and retries wake every `ASR_WAKE_RETRY_SECONDS` (default 10 seconds) within the readiness window. A successful wake trigger is not repeated within that window. Non-transient backend errors fail immediately. Error responses contain categories and status codes, not raw backend logs or URLs. A failing dispatcher makes the health endpoint return 503. Docker marks an unhealthy container but does not automatically restart it solely because of health status.

Defaults: 25 MiB complete multipart request, 128 retained jobs, 256 MiB total retained payload, 180-second readiness wait, 600-second backend socket timeout, 30-minute job scheduling deadline, one-hour completed-result retention. All correspond to `ASR_` environment fields in `Config`; values are seconds or bytes. Network timeouts are socket inactivity bounds, not a hard total deadline against a backend that trickles response bytes indefinitely.

Audio is dropped from the live row after success. Failed uploads remain until the failed job expires under the retention policy, so an explicit retry can create a new job. The failed original is retained for diagnosis; retrying therefore needs room for both copies. Result rows expire under the same policy. SQLite reuses freed pages and may keep a larger physical database/WAL footprint. Logical deletion is not secure erasure, and backups may retain old data. The 256 MiB payload cap is not a filesystem quota. Put a filesystem quota on the volume if a strict disk ceiling is required.

The API has one owner/key; browser sessions represent that same owner. Anybody holding the API key or an authenticated owner session can retrieve any known job ID. No multi-user isolation is claimed. There is no CORS access, no arbitrary request-supplied backend URL, no shell wake command, and no automatic shutdown of the GPU host. Protect the persistent volume as private data.

## Wake adapters

| Mode | Configuration | Behavior |
| --- | --- | --- |
| `tcp` | `ASR_WAKE_HOST`, `ASR_WAKE_PORT` | Open then close a TCP connection |
| `http` | `ASR_WAKE_URL`, optional `ASR_WAKE_KEY` | HTTP POST to a fixed configured endpoint |
| `wol` | `ASR_WAKE_HOST` (broadcast address), `ASR_WAKE_MAC`, `ASR_WAKE_PORT` | Send a UDP magic packet; verify Docker network delivery |
| `none` | No wake settings | Only wait for backend readiness |

Backend and wake HTTP requests use TLS verification, do not follow redirects, and ignore ambient HTTP proxy variables. Configure the direct reachable route and appropriate trusted certificate chain.

## Test and publish

No third-party packages are needed for core and WSGI tests:

```sh
python3 -m unittest discover -s tests -v
ASR_NETWORK_TESTS=1 python3 -m unittest discover -s tests -v
node --test tests/recorder.test.cjs
```

The Node controller tests use DOM/fetch stubs and do not establish actual browser microphone or password-manager behavior. The second Python command also binds loopback sockets to exercise the real TCP wake adapter and HTTP forwarding. CI runs it and builds the container. Tests include forced process termination, stale attempts, transient and permanent backend failures, delayed readiness, retained requests after synchronous timeout, limits, authentication, and expiry.

CI also runs `python tests/browser_login.py` using Playwright/Chromium and a temporary local HTTPS server. This regression reproduces native form failure under `Referrer-Policy: no-referrer`, then checks login, model selection, and session persistence with the actual application header. The application uses `same-origin`: `no-referrer` can cause native form POSTs to send `Origin: null`. The browser test does not automate the password-manager save popup or microphone recording.

The container uses Gunicorn; the dependency-free development entry point is `python3 -m asr_gateway.serve` with the same environment configuration and `ASR_DATABASE` set to a writable path. Its development HTTP server is not a production server.

The manual **Publish container** workflow tests, builds, and publishes `ghcr.io/<repository-owner>/<repository-name>:<version>`. Invoke it with a numeric version such as `0.1.0`; publishing is never triggered by a pull request. Make the GHCR package public afterward. The image build context has an allowlist, excluding runtime configuration and recordings. The first image targets the CI runner's amd64 architecture, suitable for the intended CPU host.

## Real-hardware acceptance tests

Wake-triggered transcription and async restart recovery have passed on the operator's deployment. Continue with [the Traefik ingress test](TESTING.md#9-gateway-through-traefik), then the recorder and Open WebUI. The runbook records what has been tested and what remains pending.

## Job logs and diagnostics

```sh
docker compose logs -f --tail=100 gateway
docker compose exec gateway python -m asr_gateway.jobs --recent 5
docker compose exec gateway python -m asr_gateway.jobs JOB_ID
```

Job events go to stdout and include job ID, attempt, elapsed time, state/stage, HTTP status, and exception class/errno where applicable. They exclude raw exception messages, backend addresses, credentials, audio, and transcription text. Events include `job_accepted`, `job_claimed`, `backend_waiting`, `wake_sent`, `wake_failed`, `job_transcribing`, `job_requeued`, `job_failed`, and `job_succeeded`. The read-only CLI displays job metadata and whether audio is retained, never payload contents. Logs only cover activity after deploying this version.

`backend_connection_error` with `stage=transcription` means the inference request failed at the transport layer. `wake_failed` identifies the wake adapter separately and does not immediately fail the job. `backend_waiting` reports changes in health failure details; `backend_not_ready` means the readiness window expired. Use this evidence to distinguish a socket problem from an inference disconnect before changing timeouts.
