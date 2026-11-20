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
`ORCAROUTER_BASE_URL`. **Verified against the live API (2026-11-18/19).**

### Video models (9)

| Model | Price | Unit | Seconds | Resolutions | Audio |
| :--- | ---: | :--- | :--- | :--- | :---: |
| `kling/kling-v2-5-turbo` | $0.042 | request | 5-10 | 720p, 1080p | — |
| `kling/kling-v2-6` | $0.042 | request | 5-10 | 720p, 1080p | — |
| `kling/kling-v3-omni` | $0.084 | request | 3-15 | 720p, 1080p, 4k | yes |
| `kling/kling-v3` | $0.084 | request | 3-15 | 720p, 1080p, 4k | yes |
| `kling/kling-video-o1` | $0.084 | request | 5 or 10 | 720p, 1080p | — |
| `kling/kling-3-turbo` | $0.112 | request | 3-15 | 720p, 1080p | yes |
| `kling/kling-v2-master` | $0.280 | request | 5-10 | 720p, 1080p, 4k | — |
| `kling/kling-v2-1-master` | $0.280 | request | 5-10 | 720p, 1080p, 4k | — |
| `minimax/minimax-h3` | $0.080/s | second | 4-15 | 768p, 2k | — |

**Pricing units:** Kling is flat per request regardless of duration (`pricing_unit="request"`); MiniMax is
per generated second (`pricing_unit="second"`). The `MediaModel.estimate_microcents(seconds)` method handles
both. `model="auto"` compares effective total cost across units.

Each family gets its own request body builder (`packages/media/providers/orcarouter_hosted.py`);
statuses `NOT_START/SUBMITTED` -> `queued`, `IN_PROGRESS` -> `running`, `SUCCESS` -> `succeeded`,
`FAILURE` -> `failed`; the signed `result_url` is downloaded into the media store at once.

Seedance (`byteplus/dreamina-*`) was removed: it is not in the live /v1/models list (returns 404).

### Image models (8)

| Model | Price | Notes |
| :--- | ---: | :--- |
| `google/imagen-4.0-fast-generate-001` | $0.020/image | |
| `google/imagen-4.0-generate-001` | $0.040/image | |
| `google/imagen-4.0-ultra-generate-001` | $0.060/image | |
| `openai/gpt-image-1` | unknown | token-based |
| `openai/gpt-image-1-mini` | unknown | token-based |
| `openai/gpt-image-1.5` | unknown | token-based |
| `grok/grok-imagine-image` | unknown | |
| `grok/grok-imagine-image-pro` | unknown | |

Text-to-image only; reference images (needs `/images/edits`) are refused for now.
All image endpoints returned 503 upstream as of 2026-11-19.

### Pricing and budgets

- All video models have **builtin prices** (`price_known: true`). Budget-capped keys work for all of them.
- Google Imagen models have builtin prices. OpenAI/Grok image models are **unpriced** (token-based pricing
  not convertible to per-image cost); a capped key is refused (402) for them.
- `MEDIA_PRICE_OVERRIDES` still works to override any builtin price or supply one for unpriced models:
  `MEDIA_PRICE_OVERRIDES='{"openai/gpt-image-1": 50000}'` (microcents per image).
- Kling reference media (`image_list` / `video_list`) is refused until their exact field values are verified;
  first and last frame work.
- The key comes from the environment only; a key stored through the dashboard is not read by this provider yet.

## Adding a provider

Subclass `packages.media.provider.MediaProvider` (`models`, `generate_images`, `submit_video`, `poll_video`,
optional `cancel_video`) and register it in `packages/media/registry.py`. Real providers are planned in this
order: hosted OrcaRouter (done, see above), Volcengine Ark (Seedance, Seedream), MiniMax, then fal and Replicate.

## CPX-GenAI integration

The generative lane script (`cpx-genai/scripts/generate_video_scenes.py`) calls the hosted OrcaRouter
video API directly (same `POST /v1/video/generations`, `GET /v1/video/generations/{task_id}` endpoints)
to produce AI-generated video clips for shots tagged as `generative` in a BRIEF.json.

The capsule-cinema fork also has an OrcaRouter tool adapter
(`lib/custom_tools/video_generation/orcarouter_video_generator_tool.py`) that wraps the same API as a
capsule-cinema engine, so the full pipeline can dispatch generative shots through capsule-cinema's
tool system.

## Not yet done

End-to-end live API test (submit + poll + download); other providers (Volcengine Ark direct, MiniMax direct,
fal, Replicate); speech (`/v1/audio/speech`); dashboard tab for media jobs; uploading reference files (today
references are URLs or `data:` strings the provider accepts); durable job recovery after a restart is handled by
the sweeper picking up `queued`/`running` rows, but provider-side state for the fake provider is in memory only.
