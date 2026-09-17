# ASR wake gateway

Record once, even when your transcription server is asleep.

An initial single-owner gateway for an existing Speaches deployment. It durably accepts a multipart audio upload, wakes a backend if necessary, waits for readiness, forwards the upload, and retains the transcription for retrieval. It runs on an always-on CPU host; inference remains on your existing GPU machine.

**Status:** first implementation. Local queue and WSGI tests have run; real socket tests, the container build, Open WebUI integration, and browser microphone behavior still need validation outside the development sandbox. No image has been published. This is not yet a verified drop-in production deployment.

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

Open `/` for a minimal recorder: enter the gateway key and model ID, record, stop, wait, and copy. Microphone and clipboard access need HTTPS or localhost. The page stores only the latest job ID in localStorage; the key remains in page memory. After reloading, re-enter the key and press **Check saved job**. Unaccepted recordings are held only in browser memory and cannot survive a reload; a failed upload offers retry. Browser testing remains outstanding.

`Idempotency-Key` deduplicates the **exact multipart body and content type** within the retained job's lifetime. Reusing a key with changed content returns 409. Fresh `curl -F` calls generally generate different boundaries, so they are not byte-identical retries. The recorder retains its serialized request for retries. This deliberately narrow contract avoids pretending that raw-body hashing provides semantic deduplication.

## Failure model

Audio and metadata live in the same SQLite transaction, removing a separate file/database commit boundary. One dispatcher claims jobs and records an attempt token. After a process restart, unfinished jobs become eligible for retry. Tokens fence stale result writes.

Inference may execute twice if a backend finishes but its response is lost. We guarantee neither exactly-once inference nor safe retry for arbitrary side-effecting workloads. This implementation is specifically for transcription.

Connection failures and selected transient HTTP statuses retry at most twice by default. Non-transient backend errors fail immediately. Error responses contain categories and status codes, not raw backend logs or URLs. A failing dispatcher makes the health endpoint return 503. Docker marks an unhealthy container but does not automatically restart it solely because of health status.

Defaults: 25 MiB complete multipart request, 128 retained jobs, 256 MiB total retained payload, 180-second readiness wait, 600-second backend socket timeout, 30-minute job scheduling deadline, one-hour completed-result retention. All correspond to `ASR_` environment fields in `Config`; values are seconds or bytes. Network timeouts are socket inactivity bounds, not a hard total deadline against a backend that trickles response bytes indefinitely.

Audio is dropped from the live row after completion/final failure; result rows expire later. SQLite reuses freed pages and may keep a larger physical database/WAL footprint. Logical deletion is not secure erasure, and backups may retain old data. The 256 MiB payload cap is not a filesystem quota. Put a filesystem quota on the volume if a strict disk ceiling is required.

The API has one owner/key: anybody holding that key can retrieve any known job ID. No multi-user isolation is claimed. There is no CORS access, no arbitrary request-supplied backend URL, no shell wake command, and no automatic shutdown of the GPU host. Protect the persistent volume as private data.

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
```

The second command also binds loopback sockets to exercise the real TCP wake adapter and HTTP forwarding. CI runs it and builds the container. Tests include forced process termination, stale attempts, transient and permanent backend failures, delayed readiness, retained requests after synchronous timeout, limits, authentication, and expiry.

The container uses Gunicorn; the dependency-free development entry point is `python3 -m asr_gateway.serve` with the same environment configuration and `ASR_DATABASE` set to a writable path. Its development HTTP server is not a production server.

The manual **Publish container** workflow tests, builds, and publishes `ghcr.io/<repository-owner>/<repository-name>:<version>`. Invoke it with a numeric version such as `0.1.0`; publishing is never triggered by a pull request. Make the GHCR package public afterward. The image build context has an allowlist, excluding runtime configuration and recordings. The first image targets the CI runner's amd64 architecture, suitable for the intended CPU host.

## Next real-hardware acceptance test

Start with the GPU host asleep. Submit synthetic/non-private audio through curl. Confirm exactly one TCP wake trigger, retained request state while booting, and the final transcription without resubmission. Repeat through Open WebUI, then restart the gateway while a job waits and retrieve that job through the asynchronous endpoint. Do not claim hardware compatibility until these checks pass.
