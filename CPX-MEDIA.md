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
| `MEDIA_HOSTED_ENABLED` | `true` | Use hosted OrcaRouter for media when `ORCAROUTER_API_KEY` is set |
| `MEDIA_PRICE_OVERRIDES` | empty | JSON `{model id: microcents per unit}` for models without a known price |

## Hosted OrcaRouter provider

Enabled when `ORCAROUTER_API_KEY` is set (and `MEDIA_HOSTED_ENABLED` is not `false`); base URL
`ORCAROUTER_BASE_URL`. **Built from the public docs and tested only against a mock transport, not the live API.**

- Video models: `kling/kling-v3-omni`, `kling/kling-v3`, `kling/kling-video-o1`,
  `byteplus/dreamina-seedance-2-0-260128`, `minimax/minimax-h3`. Each family gets its own request body
  (`packages/media/providers/orcarouter_hosted.py`); statuses `NOT_START/SUBMITTED` -> `queued`,
  `IN_PROGRESS` -> `running`, `SUCCESS` -> `succeeded`, `FAILURE` -> `failed`; the signed `result_url` is
  downloaded into the media store at once.
- Image models: `openai/gpt-image-1`, `-mini`, `-1.5`; Imagen 4 (fast, standard, ultra); Grok Imagine (+pro).
  Text-to-image only; reference images (needs `/images/edits`) are refused for now.
- Kling reference media (`image_list` / `video_list`) is refused until their exact field values are verified;
  first and last frame work.
- **Prices are unknown** (the hosted API publishes none). Models are flagged `price_known: false`:
  a key **with a budget cap is refused (402) for them**, `auto` ranks them last, and uncapped keys work normally.
  Supply estimates with `MEDIA_PRICE_OVERRIDES='{"minimax/minimax-h3": 400000}'` (microcents per video second or per
  image) to make them priced. The charged cost is that estimate, not the provider's settled cost.
- The key comes from the environment only; a key stored through the dashboard is not read by this provider yet.

Needs a live check: image response shape, whether Seedance accepts plain https reference URLs without its asset
library, what `POST /v1/videos` does on the hosted side, and the real cost of a task.

## Adding a provider

Subclass `packages.media.provider.MediaProvider` (`models`, `generate_images`, `submit_video`, `poll_video`,
optional `cancel_video`) and register it in `packages/media/registry.py`. Real providers are planned in this
order: hosted OrcaRouter (done, see above), Volcengine Ark (Seedance, Seedream), MiniMax, then fal and Replicate.

## Not yet done

Live verification of the hosted provider; other providers (Volcengine Ark direct, MiniMax direct, fal, Replicate); speech (`/v1/audio/speech`); dashboard tab for media jobs; uploading reference files (today
references are URLs or `data:` strings the provider accepts); durable job recovery after a restart is handled by
the sweeper picking up `queued`/`running` rows, but provider-side state for the fake provider is in memory only.
