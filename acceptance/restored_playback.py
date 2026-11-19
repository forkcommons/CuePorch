import argparse, json, time
from urllib.parse import urlsplit
import requests
from playwright.sync_api import sync_playwright
from browser import chromium

p = argparse.ArgumentParser()
p.add_argument("original")
p.add_argument("restored")
p.add_argument("uuid")
a = p.parse_args()
for i in range(150):
    try:
        if requests.get(a.restored + "/api/v1/config", timeout=2).ok:
            break
    except requests.RequestException:
        pass
    time.sleep(1)
else:
    raise RuntimeError("Restored host was unavailable")
path = "/api/v1/videos/" + a.uuid
video = requests.get(a.restored + path).json()
assert video["uuid"] == a.uuid
captions = requests.get(a.restored + path + "/captions").json()["data"]
chapters = requests.get(a.restored + path + "/chapters").json()["chapters"]
assert any(c["language"]["id"] == "en" for c in captions)
assert chapters[0]["title"] == "Call to order"
requests_mapped = []
with sync_playwright() as p:
    b = chromium(p)
    page = b.new_page(service_workers="block")

    def remap(route):
        url = route.request.url
        if not url.startswith(a.original + "/"):
            route.abort()
            return
        target = a.restored + url[len(a.original) :]
        response = route.fetch(url=target, timeout=30000)
        route.fulfill(response=response)
        requests_mapped.append(urlsplit(url).path)

    page.route("**/*", remap)
    page.goto(a.original + "/videos/embed/" + a.uuid, timeout=90000)
    page.get_by_role("button", name="Play Video", exact=True).click(timeout=30000)
    page.wait_for_function(
        "document.querySelector('video')?.currentTime>0", timeout=60000
    )
    width = page.locator("video").first.evaluate("v=>v.videoWidth")
    b.close()
assert width > 0 and requests_mapped
print(
    json.dumps(
        dict(
            video=a.uuid,
            restored_playback_width=width,
            caption_languages=[c["language"]["id"] for c in captions],
            chapters=chapters,
            requests_routed_to_restored=len(requests_mapped),
        ),
        indent=2,
    )
)
