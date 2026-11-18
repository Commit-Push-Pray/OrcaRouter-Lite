"""Hosted OrcaRouter media provider: request builders, response parsers, HTTP behaviour (mock transport)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from packages.media.providers import orcarouter_hosted as h
from packages.media.types import ImageRequest, MediaError, VideoRequest


def model(mid: str):
    return next(m for m in h.MODELS if m.id == mid)


KLING = "kling/kling-v3-omni"
SEED = "byteplus/dreamina-seedance-2-0-260128"
MINI = "minimax/minimax-h3"


# ── builders ──────────────────────────────────────────────────────────


def test_kling_body_maps_fields():
    req = VideoRequest(model=KLING, prompt="p", seconds=8, aspect_ratio="9:16", resolution="1080p",
                       first_frame="https://x/a.png", last_frame="https://x/b.png", audio=True)
    body = h.build_video_body(model(KLING), req)
    assert body == {
        "model": KLING, "prompt": "p", "image": "https://x/a.png",
        "metadata": {"image_tail": "https://x/b.png", "aspect_ratio": "9:16", "mode": "pro",
                     "duration": "8", "sound": "on"},
    }


def test_kling_duration_is_a_string_and_defaults_to_five():
    body = h.build_video_body(model(KLING), VideoRequest(model=KLING, prompt="p"))
    assert body["metadata"]["duration"] == "5"


def test_kling_o1_accepts_only_5_or_10():
    m = model("kling/kling-video-o1")
    with pytest.raises(MediaError, match="5 or 10"):
        h.build_video_body(m, VideoRequest(model=m.id, prompt="p", seconds=7))
    assert h.build_video_body(m, VideoRequest(model=m.id, prompt="p", seconds=10))["metadata"]["duration"] == "10"


def test_kling_refuses_reference_media_for_now():
    with pytest.raises(MediaError, match="reference media"):
        h.build_video_body(model(KLING), VideoRequest(model=KLING, prompt="p", reference_images=["u"]))


def test_seedance_content_roles_and_metadata():
    req = VideoRequest(model=SEED, prompt="p", seconds=6, aspect_ratio="16:9", resolution="720p", seed=7,
                       audio=True, first_frame="f", last_frame="l", reference_images=["i1", "i2"],
                       reference_videos=["v1"], reference_audios=["a1"])
    body = h.build_video_body(model(SEED), req)
    meta = body["metadata"]
    assert body["model"] == SEED and body["prompt"] == "p"
    assert [(c["type"], c["role"]) for c in meta["content"]] == [
        ("image_url", "first_frame"), ("image_url", "end_frame"),
        ("image_url", "reference_image"), ("image_url", "reference_image"),
        ("video_url", "reference_video"), ("audio_url", "reference_audio"),
    ]
    assert meta["content"][0]["image_url"] == {"url": "f"}
    assert meta["content"][4]["video_url"] == {"url": "v1"}
    assert (meta["ratio"], meta["duration"], meta["resolution"], meta["generate_audio"], meta["seed"]) == \
        ("16:9", 6, "720p", True, 7)


def test_seedance_text_only_has_no_content_and_default_duration():
    meta = h.build_video_body(model(SEED), VideoRequest(model=SEED, prompt="p"))["metadata"]
    assert "content" not in meta and meta["duration"] == 4


def test_minimax_body_single_vs_many_images_and_size():
    one = h.build_video_body(model(MINI), VideoRequest(model=MINI, prompt="p", resolution="2k",
                                                       reference_images=["i1"], aspect_ratio="1:1"))
    assert one["image"] == "i1" and "images" not in one and one["size"] == "2K" and one["duration"] == 6
    assert one["metadata"] == {"ratio": "1:1"}
    many = h.build_video_body(model(MINI), VideoRequest(model=MINI, prompt="p", seconds=9,
                                                        reference_images=["i1", "i2"],
                                                        reference_videos=["v"], reference_audios=["a"],
                                                        first_frame="f", last_frame="l"))
    assert many["images"] == ["i1", "i2"] and "image" not in many and many["duration"] == 9
    assert many["metadata"] == {"first_frame_image": "f", "last_frame_image": "l",
                                "video_urls": ["v"], "audio_urls": ["a"]}


@pytest.mark.parametrize("mid", [SEED, MINI])
def test_reference_audio_alone_is_refused(mid):
    with pytest.raises(MediaError, match="Reference audio"):
        h.build_video_body(model(mid), VideoRequest(model=mid, prompt="p", reference_audios=["a"]))


def test_unknown_family_rejected():
    with pytest.raises(MediaError):
        h.family("openai/gpt-image-1")


# ── response parsing ──────────────────────────────────────────────────


def test_parse_submit_variants():
    assert h.parse_submit({"id": "t1", "task_id": "t1", "status": "queued"}) == "t1"
    assert h.parse_submit({"id": "t2"}) == "t2"
    assert h.parse_submit({"data": {"task_id": "t3"}}) == "t3"
    with pytest.raises(MediaError):
        h.parse_submit({"status": "queued"})


@pytest.mark.parametrize("raw,state", [("NOT_START", "queued"), ("SUBMITTED", "queued"),
                                       ("IN_PROGRESS", "running"), ("queued", "queued"),
                                       ("in_progress", "running")])
def test_parse_status_pending_states(raw, state):
    st = h.parse_status({"code": "success", "data": {"status": raw, "progress": "40%"}})
    assert st.state == state and st.progress == 40 and not st.outputs


def test_parse_status_success_native_and_alias():
    native = h.parse_status({"code": "success", "data": {"status": "SUCCESS", "progress": "100%",
                                                         "result_url": "https://cdn/x.mp4"}})
    assert native.state == "succeeded" and native.outputs[0].url == "https://cdn/x.mp4"
    alias = h.parse_status({"status": "completed", "metadata": {"url": "https://cdn/y.mp4"}})
    assert alias.state == "succeeded" and alias.outputs[0].url == "https://cdn/y.mp4"


def test_parse_status_success_without_url_is_a_failure():
    st = h.parse_status({"code": "success", "data": {"status": "SUCCESS"}})
    assert st.state == "failed" and "no video URL" in st.error


def test_parse_status_failure_reason():
    st = h.parse_status({"code": "success", "data": {"status": "FAILURE", "fail_reason": "content policy"}})
    assert st.state == "failed" and st.error == "content policy"
    st2 = h.parse_status({"status": "failed", "error": {"message": "boom"}})
    assert st2.error == "boom"


def test_parse_status_unknown_or_error_envelope_is_transient():
    with pytest.raises(RuntimeError):
        h.parse_status({"code": "success", "data": {"status": "WEIRD"}})
    with pytest.raises(RuntimeError):
        h.parse_status({"code": "internal_error", "message": "try later", "data": {"status": "SUCCESS"}})


def test_parse_images_b64_and_url():
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nxx").decode()
    out = h.parse_images({"data": [{"b64_json": png}, {"url": "https://cdn/i.png"}]})
    assert out[0].data.startswith(b"\x89PNG") and out[1].url == "https://cdn/i.png"
    with pytest.raises(MediaError):
        h.parse_images({"data": []})


# ── catalog and prices ────────────────────────────────────────────────


def test_prices_are_unknown_until_overridden():
    p = h.OrcaHostedProvider("https://x/v1", "sk-orca-test")
    assert all(not m.price_known for m in p.models())
    p2 = h.OrcaHostedProvider("https://x/v1", "sk-orca-test", price_overrides='{"minimax/minimax-h3": 400000}')
    by = {m.id: m for m in p2.models()}
    assert by[MINI].price_known and by[MINI].cost_microcents_per_unit == 400_000
    assert not by[KLING].price_known


def test_bad_price_overrides_fail_loudly():
    with pytest.raises(ValueError, match="MEDIA_PRICE_OVERRIDES"):
        h.OrcaHostedProvider("https://x/v1", "k", price_overrides="not json")


# ── HTTP behaviour via mock transport ─────────────────────────────────


def provider(handler, prices: str = ""):
    return h.OrcaHostedProvider("https://api.example/v1", "sk-orca-secret", price_overrides=prices,
                                transport=httpx.MockTransport(handler))


async def test_submit_sends_auth_and_body_and_returns_task_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "task_1", "task_id": "task_1", "status": "queued"})

    p = provider(handler)
    tid = await p.submit_video(model(MINI), VideoRequest(model=MINI, prompt="lake", seconds=5))
    assert tid == "task_1"
    assert seen["url"] == "https://api.example/v1/video/generations"
    assert seen["auth"] == "Bearer sk-orca-secret"
    assert seen["body"]["model"] == MINI and seen["body"]["duration"] == 5


@pytest.mark.parametrize("status,http,etype", [(400, 422, "upstream_rejected"), (401, 502, "upstream_auth"),
                                               (403, 502, "upstream_auth"), (429, 503, "rate_limit_error"),
                                               (500, 502, "upstream_error")])
async def test_submit_error_mapping(status, http, etype):
    p = provider(lambda r: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(MediaError) as ei:
        await p.submit_video(model(MINI), VideoRequest(model=MINI, prompt="p"))
    assert ei.value.status == http and ei.value.error_type == etype


async def test_error_does_not_leak_the_api_key():
    p = provider(lambda r: httpx.Response(401, text="bad key sk-orca-secret"))
    with pytest.raises(MediaError) as ei:
        await p.submit_video(model(MINI), VideoRequest(model=MINI, prompt="p"))
    assert "sk-orca-secret" not in str(ei.value)


async def test_poll_maps_envelope_and_5xx_is_transient():
    p = provider(lambda r: httpx.Response(200, json={"code": "success", "data": {
        "status": "SUCCESS", "result_url": "https://cdn/v.mp4"}}))
    st = await p.poll_video(model(MINI), "task_1")
    assert st.state == "succeeded"
    flaky = provider(lambda r: httpx.Response(503))
    with pytest.raises(RuntimeError):
        await flaky.poll_video(model(MINI), "task_1")


async def test_poll_url_uses_task_id():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"code": "success", "data": {"status": "IN_PROGRESS", "progress": "10%"}})

    st = await provider(handler).poll_video(model(MINI), "task_9")
    assert seen["url"] == "https://api.example/v1/video/generations/task_9" and st.progress == 10


async def test_generate_images_posts_and_parses():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"url": "https://cdn/a.png"}]})

    out = await provider(handler).generate_images(
        model("openai/gpt-image-1-mini"), ImageRequest(model="openai/gpt-image-1-mini", prompt="fox", n=1,
                                                       size="1024x1024"))
    assert out[0].url == "https://cdn/a.png"
    assert seen["body"] == {"model": "openai/gpt-image-1-mini", "prompt": "fox", "n": 1, "size": "1024x1024"}


async def test_image_references_not_supported_yet():
    p = provider(lambda r: httpx.Response(200, json={}))
    with pytest.raises(MediaError, match="reference images"):
        await p.generate_images(model("openai/gpt-image-1"),
                                ImageRequest(model="openai/gpt-image-1", prompt="x", reference_images=["u"]))
