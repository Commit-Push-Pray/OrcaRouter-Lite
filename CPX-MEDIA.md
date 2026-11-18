# CPX media generation (image and video)

Added in Commit-Push-Pray's fork. Everything lives in new modules so merging upstream stays cheap:
`packages/media/`, `packages/db/models/media_job.py`, `app/routes/media.py`; plus small edits to `app/main.py`
(router + background sweeper), `app/config.py` (four settings) and `packages/db/models/__init__.py`.

## Endpoints (OpenAI-shaped, same `sk-orca-*` key auth)

| Method | Path | Notes |
| :--- | :--- | :--- |
| GET | `/v1/media/models?kind=image\|video` | Every model with its limits and price (`cost_microcents_per_unit`; 1 cent = 10 000, 1 USD = 1 000 000) |
| POST | `/v1/images/generations` | Blocking. `model`, `prompt`, `n` (1-10), `size`, `response_format` (`url`\|`b64_json`), `reference_images` |
| POST | `/v1/videos` | Creates an async job, returns **202** with the job. `model` (or `auto`), `prompt`, `seconds`, `aspect_ratio`, `resolution`, `seed`, `reference_images/videos/audios`, `first_frame`, `last_frame`, `audio` |
| GET | `/v1/videos/{id}` | Status: `queued` -> `in_progress` -> `completed` / `failed`; polls the provider (rate limited) |
| GET | `/v1/videos/{id}/content` | The finished file; **409** until ready |
| DELETE | `/v1/videos/{id}` | Cancels (best effort upstream), deletes the stored file |
| GET | `/v1/media/files/{name}` | A stored asset, only for the owning workspace |

## Behaviour

- `model="auto"` picks the cheapest video model that satisfies seconds, aspect ratio, resolution, reference
  counts, first/last frame and audio. No match gives **422**.
- A request outside the chosen model's limits gives **422** with the reason (for example "supports 2-10 seconds").
- **Budget:** before any provider call the estimated cost is checked against the key's remaining budget
  (`budget_limit_cents`); over budget gives **402** and nothing is submitted. The real cost is charged and written to
  the request log (`routing_strategy = "media"`) when the job finishes.
- **Results are copied** into `MEDIA_DIR` when a job finishes (providers' URLs expire), by the client's poll or by
  the background sweeper, whichever comes first.
- Inline `data:` references are never stored in the job row.

## Settings

| Variable | Default | Meaning |
| :--- | :--- | :--- |
| `MEDIA_DIR` | `./data/media` | Where finished assets are kept |
| `MEDIA_FAKE_PROVIDER` | `false` | Registers `fake/image-1` and `fake/video-1` (no network, no cost) for tests and client wiring |
| `MEDIA_POLL_MIN_INTERVAL_S` | `2` | Minimum gap between upstream polls of one job |
| `MEDIA_MAX_BYTES` | 512 MB | Largest result accepted from a provider |

## Adding a provider

Subclass `packages.media.provider.MediaProvider` (`models`, `generate_images`, `submit_video`, `poll_video`,
optional `cancel_video`) and register it in `packages/media/registry.py`. Real providers are planned in this
order: hosted OrcaRouter (shape to be confirmed against `api.orcarouter.ai`), Volcengine Ark (Seedance, Seedream),
MiniMax, then fal and Replicate.

## Not yet done

Real providers; speech (`/v1/audio/speech`); dashboard tab for media jobs; uploading reference files (today
references are URLs or `data:` strings the provider accepts); durable job recovery after a restart is handled by
the sweeper picking up `queued`/`running` rows, but provider-side state for the fake provider is in memory only.
