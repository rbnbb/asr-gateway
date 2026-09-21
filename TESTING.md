# Manual integration tests

Run these on the Docker host, from your Compose directory, in one Bash or Zsh session. Stop at the first failing step. Commands use the Compose service name `gateway`, regardless of the container name.

Keep real addresses, credentials, and recordings outside this public document. No GPU is needed until the backend tests. The gateway image includes Python, not curl; upload commands use curl on the host.

## 0. Set shell variables

```sh
# Change this to your published HOST port, e.g. 8282.
ASR_PORT=$(docker compose exec -T gateway printenv PORT)
ASR_URL="http://127.0.0.1:$ASR_PORT"
ASR_KEY=$(docker compose exec -T gateway printenv ASR_API_KEY)
ASR_MODEL=$(docker compose exec -T gateway printenv ASR_MODEL)
ASR_AUDIO=/tmp/asr-test.wav
```

Use a 5–10 second WAV or MP3 with a known spoken sentence. Record it on a phone/laptop and copy it to the host, then update `ASR_AUDIO`. If `espeak` is already installed, alternatively:

```sh
espeak -w "$ASR_AUDIO" 'This is a test of the transcription queue.'
```

The file stays on the host: curl uploads it. An optional copy inside the container is `docker compose cp "$ASR_AUDIO" gateway:/tmp/asr-test.wav`; that temporary copy is not needed below.

## 1. Startup and internal health

```sh
docker compose config --quiet
docker compose ps
docker compose logs --tail=50 gateway
docker compose exec gateway python -c \
  'import os, urllib.request; print(urllib.request.urlopen("http://127.0.0.1:" + os.environ["PORT"] + "/health").read().decode())'
```

Set `PORT` once in `.env`; the commands read the running container's value. Expected: running/healthy and `{"status":"ok"}`. A sleeping Speaches backend does not make the gateway unhealthy.

## 2. Host access and authentication

```sh
curl -i "$ASR_URL/health"
curl -i "$ASR_URL/v1/models"
curl -i "$ASR_URL/v1/models" -H "Authorization: Bearer $ASR_KEY"
```

Expected: **200**, **401**, then **200 with the configured model**. Model listing here does not contact Speaches.

## 3. Gateway → Traefik → Speaches

Keep the GPU machine awake for this test. It uses the configured internal routes, ignores ambient HTTP proxy variables, and rejects redirects like the actual gateway.

```sh
docker compose exec -T gateway python - <<'PY'
import os
import socket
import urllib.request
import urllib.error
from urllib.parse import urlsplit

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), NoRedirect())
key = os.environ.get("ASR_BACKEND_KEY")
headers = {"Authorization": "Bearer " + key} if key else {}
checks = {
    "Speaches health": os.environ["ASR_HEALTH_URL"],
    "Speaches models": os.environ["ASR_BACKEND_URL"].rstrip("/") + "/models",
}
for label, url in checks.items():
    print("\n" + label)
    try:
        parts = urlsplit(url)
        socket.getaddrinfo(parts.hostname, parts.port or (
            443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        print("DNS OK")
        with opener.open(urllib.request.Request(url, headers=headers), timeout=10) as r:
            print("HTTP", r.status)
            print(r.read(1000).decode(errors="replace"))
    except urllib.error.HTTPError as e:
        print("HTTP", e.code)
        print(e.read(500).decode(errors="replace"))
    except (OSError, urllib.error.URLError, ValueError) as e:
        print("FAILED:", e)
PY
```

Expected: health **200** and models **200 with Speaches model data**.

| Failure | Check next |
| --- | --- |
| Name resolution | Hostname, shared Docker network, DNS alias |
| Connection refused | Listening port and entrypoint |
| 404 | Traefik router match, including Host rule, and URL path |
| 401/403 | Backend credential or proxy authentication |
| 502/503/504 | Traefik's upstream route and backend availability |
| Redirect | Configure a direct route; redirects are not followed |

After editing `.env`, run `docker compose up -d --force-recreate gateway`. Restart alone does not reload the environment. Refresh the shell variables from step 0 if you changed the key/model.

## 4. Define reusable upload and job commands

```sh
asr_transcribe() {
  curl --fail-with-body --max-time 240 \
    -w '\nHTTP %{http_code}; elapsed %{time_total}s\n' \
    "$ASR_URL/v1/audio/transcriptions" \
    -H "Authorization: Bearer $ASR_KEY" \
    -F "model=$ASR_MODEL" -F "file=@$ASR_AUDIO" -F 'response_format=json'
}

asr_submit() {
  local response
  ASR_JOB=
  response=$(curl --fail-with-body --silent --show-error --max-time 30 \
    "$ASR_URL/jobs" -H "Authorization: Bearer $ASR_KEY" \
    -F "model=$ASR_MODEL" -F "file=@$ASR_AUDIO" -F 'response_format=json') || return
  ASR_JOB=$(printf '%s' "$response" | python3 -c \
    'import json,sys; print(json.load(sys.stdin)["id"])') || return
  printf 'Job: %s\n' "$ASR_JOB"
}

asr_status() {
  curl --fail-with-body --silent --show-error --max-time 10 \
    "$ASR_URL/jobs/${ASR_JOB:?Submit a job first}" \
    -H "Authorization: Bearer $ASR_KEY"
  printf '\n'
}

asr_result() {
  curl --fail-with-body --silent --show-error --max-time 10 \
    "$ASR_URL/jobs/${ASR_JOB:?Submit a job first}/result" \
    -H "Authorization: Bearer $ASR_KEY"
  printf '\n'
}
```

These functions use the current variables, so changing `ASR_URL` later switches all requests to the proxy route.

## 5. Warm transcription

With the GPU awake:

```sh
asr_transcribe
```

Expected: **200 and recognizable text**. First use may include model loading time.

## 6. Cold transcription

Put the GPU host to sleep/off using your usual method, wait until it is actually asleep, then run `asr_transcribe` once. Do not separately trigger wake first.

Expected: the machine wakes and the original request returns text. Record elapsed time. A short result alone does not prove a full cold boot occurred. The gateway's default synchronous wait is 180 seconds; curl's 240-second limit does not extend that, nor any proxy/client timeout.

Optional isolated wake diagnostic, if wake fails:

```sh
docker compose exec -T gateway python - <<'PY'
import os, socket
with socket.create_connection(
    (os.environ["ASR_WAKE_HOST"], int(os.environ["ASR_WAKE_PORT"])), timeout=5):
    pass
print("TCP connection opened and closed")
PY
```

## 7. Async submission and retrieval

```sh
asr_submit
asr_status
# Repeat asr_status until state is succeeded, then:
asr_result
```

Submission should return quickly. States: `queued` → `waking` → `transcribing` → `succeeded`, or `failed`. Polling may miss brief states. `waking` includes the backend-readiness phase and does not prove a wake packet was needed. Retrieving an unfinished result returns 409.

## 8. Recover unfinished work after restart

Start with the GPU asleep:

```sh
asr_submit && docker compose restart gateway
# Once the gateway is healthy:
asr_status
# Repeat until succeeded:
asr_result
```

Expected: the **same job ID** survives without another upload. `attempts: 2` is expected if an active first attempt was interrupted. If completion happened before the restart, this tests retained results instead of unfinished-work recovery. Keep the queue volume; never use `down -v` for this test. Repeated restarts can exhaust the default two-attempt limit.

## 9. Gateway through Traefik

```sh
ASR_DIRECT_URL=$ASR_URL
ASR_URL=https://asr.example.test  # Replace with your gateway's actual browser URL.
curl -i "$ASR_URL/health"
curl -i "$ASR_URL/v1/models" -H "Authorization: Bearer $ASR_KEY"
asr_transcribe
```

Expected: health/model responses and transcription match the direct route. Test warm first, then cold. Browser → Traefik uses HTTPS; Traefik → gateway can remain HTTP. Gateway → Speaches uses the separate configured backend URL.

If only the proxy path fails, inspect its router, target container port, body limits, authentication middleware, and timeouts. Restore direct access with `ASR_URL=$ASR_DIRECT_URL` when comparing paths. If `/health` works but multipart POSTs fail, focus on the POST-specific differences.

## 10. Recorder page

1. Open the gateway's HTTPS root URL `/`.
2. Sign in with your browser username/password and optionally save them in your password manager. The configured model is selected automatically. Allow microphone access.
3. Record a short sentence, press Stop, and wait for text. Test Copy.
4. Repeat with the GPU asleep; watch for waking/transcribing status.
5. On another cold run, reload **after upload has been accepted** (e.g. the page says queued/waking). Press **Check saved job**; your browser session should still be signed in.

Expected: text without re-recording after accepted-job reload. Authentication uses a Secure, HttpOnly session cookie. The latest job ID and selected model are saved separately in localStorage. An unaccepted recording exists only in page memory and is lost on reload. A plain HTTP LAN URL does not provide the browser secure context needed for microphone access.

## 11. Open WebUI

In the Audio/STT settings, select its OpenAI-compatible provider. Set the API base URL to the gateway URL ending in `/v1`, the gateway key, and your Speaches model ID. Use an address reachable from the **Open WebUI backend container**; its localhost is not the Docker host or the gateway.

Test short speech with the GPU awake, then asleep. Expected: text appears from one recording. If curl succeeds but Open WebUI fails after a consistent duration, investigate the Open WebUI/proxy timeout. This synchronous client does not automatically retrieve a job after a disconnected request.

## Verification record

Operator-reported on 2026-09-21: container startup/health, authentication, real Speaches health/model routing, wake-triggered transcription, and async recovery of an unfinished job across gateway restart passed. The recovery run finished about 66.5 seconds after submission with two attempts and a retained result. Traefik browser ingress was subsequently exercised, but a browser transcription failed within about ten seconds. The new diagnostic/retry/login changes need another deployment check. Open WebUI remains pending. No private addresses, job IDs, credentials, or audio are recorded here.

## 12. Diagnose and recover a failed browser job

After updating the source and setting `ASR_BROWSER_USERNAME` / `ASR_BROWSER_PASSWORD` privately in `.env`:

```sh
docker compose up -d --build --force-recreate gateway
docker compose logs -f --tail=100 gateway
```

In a second terminal, inspect recent jobs without exposing recordings or transcripts:

```sh
docker compose exec gateway python -m asr_gateway.jobs --recent 5
# Or inspect the job ID displayed by the recorder:
docker compose exec gateway python -m asr_gateway.jobs JOB_ID
```

A failed job is terminal: waiting will not make it succeed. If `has_audio` is true, use **Retry transcription** on the page. If false (including older failures before retention was implemented), record again. A connection error while polling is different: **Check saved job** resumes retrieval of the same ongoing job.

To retry through curl, set `ASR_JOB` to the failed job ID. Generate `ASR_RETRY_KEY` once, and reuse it if the retry request's response is lost:

```sh
ASR_RETRY_KEY=$(python3 -c 'import uuid; print(uuid.uuid4())')
curl --fail-with-body --silent --show-error -X POST \
  "$ASR_URL/jobs/$ASR_JOB/retry" \
  -H "Authorization: Bearer $ASR_KEY" \
  -H "Idempotency-Key: $ASR_RETRY_KEY"
```

The response gives a new job ID. The old failed job remains available for diagnosis. Do not publish raw logs without review, even though application job events intentionally omit infrastructure addresses and payloads.

Browser login check: submit the normal username/password form, confirm return to `/` with the model dropdown populated, reload and confirm the session persists, then sign out and confirm protected job reads require authentication again. The password manager should be able to recognize the form; verify its actual save/fill behavior on your browser. The configured username is `owner` unless changed. If `ASR_BROWSER_PASSWORD` is empty, the browser password falls back to `ASR_API_KEY`.

If login returns `same_origin_https_required`, inspect its `reason`: `null_origin` can result from a `no-referrer` policy on the page, `missing_origin` means no header arrived, and `origin_host_mismatch` means the browser origin differs from the Host seen by the gateway. Current application responses use `Referrer-Policy: same-origin`; check that proxy middleware does not replace this with `no-referrer`. Reload the root page after upgrading before submitting the form again. Do not disable the origin check.
