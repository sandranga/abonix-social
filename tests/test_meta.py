import httpx
import pytest
import respx

from app.publish.meta import MetaClient, MetaError

G = "https://graph.facebook.com/v23.0"


def client():
    return MetaClient("SYS", "PAGE", "IG", "v23.0", http=httpx.AsyncClient())


@respx.mock
async def test_instagram_flow_polls_until_finished():
    respx.post(f"{G}/IG/media").mock(return_value=httpx.Response(200, json={"id": "C1"}))
    status = respx.get(f"{G}/C1").mock(side_effect=[
        httpx.Response(200, json={"status_code": "IN_PROGRESS"}),
        httpx.Response(200, json={"status_code": "FINISHED"}),
    ])
    respx.post(f"{G}/IG/media_publish").mock(return_value=httpx.Response(200, json={"id": "M1"}))
    m = client()
    cid = await m.create_ig_container("https://media.abonix.net/x.jpg", "caption")
    await m.wait_ig_container(cid, every_s=0)
    assert await m.publish_ig_container(cid) == "M1"
    assert status.call_count == 2


@respx.mock
async def test_facebook_uses_page_token():
    respx.get(f"{G}/PAGE").mock(return_value=httpx.Response(200, json={"access_token": "PAGETOKEN"}))
    photos = respx.post(f"{G}/PAGE/photos").mock(return_value=httpx.Response(200, json={"id": "P1", "post_id": "PAGE_1"}))
    assert await client().publish_facebook_photo("https://media.abonix.net/x.jpg", "hi") == "PAGE_1"
    assert b"access_token=PAGETOKEN" in photos.calls[0].request.content


@respx.mock
async def test_expired_token_is_not_retryable():
    respx.get(f"{G}/me").mock(return_value=httpx.Response(400, json={
        "error": {"message": "Error validating access token", "code": 190}}))
    with pytest.raises(MetaError) as e:
        await client().check()
    assert e.value.retryable is False


@respx.mock
async def test_container_error_raises():
    respx.get(f"{G}/C1").mock(return_value=httpx.Response(200, json={"status_code": "ERROR", "status": "bad image"}))
    with pytest.raises(MetaError):
        await client().wait_ig_container("C1", every_s=0)


@respx.mock
async def test_check_without_page_id_lists_pages():
    respx.get(f"{G}/me").mock(return_value=httpx.Response(200, json={"id": "SU", "name": "abonix-social"}))
    respx.get(f"{G}/me/accounts").mock(return_value=httpx.Response(200, json={"data": [
        {"id": "P1", "name": "Abonix", "instagram_business_account": {"id": "IG1"}}]}))
    m = MetaClient("SYS", "", "", "v23.0", http=httpx.AsyncClient())
    out = await m.check()
    assert out["pages"] == [{"page_id": "P1", "name": "Abonix", "instagram_business_account": "IG1"}]
    assert "META_PAGE_ID" in out["next_step"]


@respx.mock
async def test_instagram_carousel_children_then_parent():
    from urllib.parse import parse_qs

    media = respx.post(f"{G}/IG/media").mock(side_effect=[
        httpx.Response(200, json={"id": "K1"}), httpx.Response(200, json={"id": "K2"}),
        httpx.Response(200, json={"id": "CAR"})])
    respx.get(f"{G}/K1").mock(return_value=httpx.Response(200, json={"status_code": "FINISHED"}))
    respx.get(f"{G}/K2").mock(return_value=httpx.Response(200, json={"status_code": "FINISHED"}))
    cid = await client().create_ig_carousel(["https://m/1.jpg", "https://m/2.jpg"], "caption", "alt")
    assert cid == "CAR"
    child = parse_qs(media.calls[0].request.content.decode())
    assert child["is_carousel_item"] == ["true"] and child["alt_text"] == ["alt"] and "caption" not in child
    parent = parse_qs(media.calls[2].request.content.decode())
    assert parent["media_type"] == ["CAROUSEL"] and parent["children"] == ["K1,K2"] and parent["caption"] == ["caption"]


@respx.mock
async def test_instagram_reel_container():
    from urllib.parse import parse_qs

    media = respx.post(f"{G}/IG/media").mock(return_value=httpx.Response(200, json={"id": "R1"}))
    assert await client().create_ig_reel("https://m/r.mp4", "cap") == "R1"
    sent = parse_qs(media.calls[0].request.content.decode())
    assert sent["media_type"] == ["REELS"] and sent["video_url"] == ["https://m/r.mp4"] and sent["share_to_feed"] == ["true"]


@respx.mock
async def test_facebook_multi_photo_post():
    import json
    from urllib.parse import parse_qs

    respx.get(f"{G}/PAGE").mock(return_value=httpx.Response(200, json={"access_token": "PT"}))
    photos = respx.post(f"{G}/PAGE/photos").mock(side_effect=[
        httpx.Response(200, json={"id": "F1"}), httpx.Response(200, json={"id": "F2"})])
    feed = respx.post(f"{G}/PAGE/feed").mock(return_value=httpx.Response(200, json={"id": "PAGE_9"}))
    assert await client().publish_facebook_carousel(["https://m/1.jpg", "https://m/2.jpg"], "msg") == "PAGE_9"
    assert all(parse_qs(c.request.content.decode())["published"] == ["false"] for c in photos.calls)
    sent = parse_qs(feed.calls[0].request.content.decode())
    assert json.loads(sent["attached_media"][0]) == [{"media_fbid": "F1"}, {"media_fbid": "F2"}]


@respx.mock
async def test_facebook_video_goes_to_the_video_host():
    respx.get(f"{G}/PAGE").mock(return_value=httpx.Response(200, json={"access_token": "PT"}))
    video = respx.post("https://graph-video.facebook.com/v23.0/PAGE/videos").mock(
        return_value=httpx.Response(200, json={"id": "V1"}))
    assert await client().publish_facebook_video("https://m/r.mp4", "desc") == "V1"
    assert b"file_url=https" in video.calls[0].request.content


@respx.mock
async def test_ig_insights_drops_an_unsupported_metric():
    route = respx.get(f"{G}/M1/insights").mock(side_effect=[
        httpx.Response(400, json={"error": {"message": "metric[1] must be one of ...: views is not supported", "code": 100}}),
        httpx.Response(200, json={"data": [
            {"name": "reach", "values": [{"value": 321}]},
            {"name": "saved", "values": [{"value": 7}]},
            {"name": "shares", "total_value": {"value": 2}}]}),
    ])
    out = await client().ig_insights("M1")
    assert out == {"reach": 321, "saved": 7, "shares": 2}
    assert "views" not in route.calls[1].request.url.params["metric"]


@respx.mock
async def test_fb_post_stats():
    respx.get(f"{G}/PAGE").mock(return_value=httpx.Response(200, json={"access_token": "PT"}))
    respx.get(f"{G}/PAGE_1").mock(return_value=httpx.Response(200, json={
        "reactions": {"summary": {"total_count": 12}}, "comments": {"summary": {"total_count": 3}},
        "shares": {"count": 2}}))
    assert await client().fb_post_stats("PAGE_1") == {"reactions": 12, "comments": 3, "shares": 2}
