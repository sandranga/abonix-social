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
