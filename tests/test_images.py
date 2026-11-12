import httpx
import pytest
import respx

from app.images.providers import ImageError, PixabayProvider, credit_line

API = "https://pixabay.com/api/"


def hit(i, h=4000, url=True):
    return {"id": i, "imageHeight": h, "imageWidth": 2600, "user": f"user{i}", "pageURL": f"https://pixabay.com/p/{i}",
            "tags": "gym, training", **({"largeImageURL": f"https://cdn.pixabay.com/{i}.jpg"} if url else {})}


@respx.mock
async def test_pixabay_picks_first_fresh_tall_photo(tmp_path):
    search = respx.get(API).mock(return_value=httpx.Response(200, json={"hits": [
        hit(1),            # already used
        hit(2, h=900),     # too small
        hit(3, url=False), # no downloadable copy
        hit(4),
    ]}))
    respx.get("https://cdn.pixabay.com/4.jpg").mock(return_value=httpx.Response(200, content=b"JPEG"))

    p = PixabayProvider("KEY", http=httpx.AsyncClient())
    photo = await p.find("boxing gym", tmp_path, exclude_ids={"1"})

    assert (photo.source, photo.source_id, photo.credit) == ("pixabay", "4", "user4")
    assert photo.path.read_bytes() == b"JPEG"
    q = search.calls[0].request.url.params
    assert q["key"] == "KEY" and q["q"] == "boxing gym" and q["orientation"] == "vertical"


@respx.mock
async def test_pixabay_retries_broader_then_fails(tmp_path):
    search = respx.get(API).mock(return_value=httpx.Response(200, json={"hits": []}))
    p = PixabayProvider("KEY", http=httpx.AsyncClient())
    with pytest.raises(ImageError):
        await p.find("yoga studio sunrise", tmp_path, set())
    assert [c.request.url.params["q"] for c in search.calls] == ["yoga studio sunrise", "yoga studio"]


@respx.mock
async def test_pixabay_bad_key_is_reported(tmp_path):
    respx.get(API).mock(return_value=httpx.Response(400, text="[ERROR 400] Invalid or missing API key"))
    p = PixabayProvider("BAD", http=httpx.AsyncClient())
    with pytest.raises(ImageError, match="Pixabay 400"):
        await p.find("gym", tmp_path, set())


def test_credit_line_names_the_source():
    assert credit_line({"credit": "Ana", "source": "pixabay"}) == "Ana / Pixabay"
    assert credit_line({"credit": "Ana"}) == "Ana / Pexels"  # posts saved before Pixabay existed
    assert credit_line(None) is None
