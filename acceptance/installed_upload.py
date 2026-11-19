"""Exercise private file intake and playback through the installed library."""

import hashlib
import uuid
import wave
from base64 import b64encode

import requests
from playwright.sync_api import expect

from installed_cards import select_action


def check_uploads(page, base, auth, video):
    page.goto(base + "/operator/library")
    page.wait_for_function("typeof document.querySelector('#load')?.onclick === 'function'")
    page.get_by_role("button", name="Load library").click()
    expect(page.locator("#result")).to_have_text("Library loaded.")
    tree = page.get_by_role("navigation", name="Folders")
    tree.get_by_role("button", name="November meetings", exact=True).click()
    page.get_by_role("button", name="Upload", exact=True).click()
    dialog = page.get_by_role("dialog", name="Upload files")
    record = b"Document retained as uploaded.\n"
    image = page.evaluate("""() => {
        const canvas = document.createElement('canvas');
        canvas.width = 96;
        canvas.height = 64;
        canvas.getContext('2d').fillRect(0, 0, 96, 64);
        return canvas.toDataURL('image/png').split(',')[1];
    }""")
    audio = video.with_suffix(".wav")
    with wave.open(str(audio), "wb") as stream:
        stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\x00\x01" * 8000)
    picker = dialog.get_by_label("Choose files")
    picker.set_input_files([
        dict(name="Library rehearsal.mp4", mimeType="video/mp4", buffer=video.read_bytes()),
        dict(name="Library audio.wav", mimeType="audio/wav", buffer=audio.read_bytes()),
        dict(name="Unreadable clip.mp4", mimeType="video/mp4", buffer=b"invalid media"),
    ])
    dialog.get_by_role("button", name="Upload selected files").click()
    expect(dialog.get_by_role("status")).to_have_text("1 file(s) need attention.", timeout=45000)
    expect(dialog.locator("#uploadFiles li").filter(has_text="Unreadable clip")).to_contain_text("could not decode")
    dialog.get_by_role("button", name="Close").click()

    page.evaluate("""({record, image}) => {
        const transfer = new DataTransfer();
        for (const [content, filename, type] of [
            [record, 'Library notes.txt', 'text/plain'], [image, 'Library still.png', 'image/png']
        ]) {
            transfer.items.add(new File([Uint8Array.from(atob(content), ch => ch.charCodeAt(0))], filename, {type}));
        }
        document.querySelector('h1').dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true, dataTransfer: transfer}));
    }""", {"record": b64encode(record).decode(), "image": image})
    expect(dialog.get_by_label("Upload to").locator("option:checked")).to_have_text("Station programs / November meetings")
    dialog.get_by_role("button", name="Upload selected files").click()
    expect(dialog.get_by_role("status")).to_have_text("All files added.", timeout=45000)
    dialog.get_by_role("button", name="Close").click()

    def library():
        response = requests.get(base + "/api/library", headers=auth, timeout=10)
        response.raise_for_status()
        return {row["title"]: row for row in response.json()}

    works = library()
    target_folder = works["Library rehearsal"]["folder"]
    assert target_folder and "Unreadable clip" not in works
    for title, kind in [("Library rehearsal", "video"), ("Library audio", "audio"),
                        ("Library notes", "document"), ("Library still", "image")]:
        work = works[title]
        assert work["asset_type"] == kind and work["source_privacy"] == "private"
        assert work["folder"] == target_folder and work["creator"] == "operator"
        assert not work["public_eligible"] and work["delivery"] is None
        assert requests.get(base + "/watch/work/" + work["id"], timeout=10).status_code == 404
    expect(page.get_by_text("Library rehearsal", exact=True)).to_be_visible()
    type_filter = page.locator("#assetType")
    type_filter.select_option("files")
    expect(page.get_by_text("Library rehearsal", exact=True)).to_have_count(0)
    expect(page.get_by_text("Library notes", exact=True)).to_be_visible()
    type_filter.select_option("")

    details = page.locator("#workDialog")
    select_action(page, "Library rehearsal", "Video info")
    details.get_by_role("button", name="Load video preview").click()
    width = details.locator("video").evaluate("async v => {v.muted=true; await v.play(); return v.videoWidth}")
    assert width == 256
    details.get_by_role("button", name="Close").click()
    select_action(page, "Library audio", "File info")
    details.get_by_role("button", name="Load audio preview").click()
    details.locator("audio").evaluate("async a => {a.muted=true; await a.play()}")
    page.wait_for_function("document.querySelector('#workDialog audio').currentTime > 0.05")
    details.get_by_role("button", name="Close").click()
    select_action(page, "Library still", "File info")
    details.get_by_role("button", name="Load image preview").click()
    image_width = details.get_by_role("img", name="Library still").evaluate(
        "async image => {await image.decode(); return image.naturalWidth;}"
    )
    assert image_width == 96
    details.get_by_role("button", name="Close").click()
    select_action(page, "Library notes", "File info")
    expect(details.locator("audio,video,img,iframe")).to_have_count(0)
    with page.expect_download() as received:
        details.get_by_role("button", name="Download original").click()
    download = received.value
    assert download.suggested_filename == "Library notes.txt" and download.path().read_bytes() == record
    details.get_by_role("button", name="Close").click()

    endpoint = base + "/api/library/uploads"
    uid = uuid.uuid4().hex
    payload = {"file": ("Retry proof.txt", record, "text/plain")}
    metadata = {"upload_id": uid, "folder": target_folder}
    assert requests.post(endpoint, files=payload, data=metadata, timeout=10).status_code == 403
    created = requests.post(endpoint, headers=auth, files=payload, data=metadata, timeout=10)
    assert created.status_code == 201
    saved = created.json()["id"]
    requests.put(base + "/api/library/placement", headers=auth, timeout=10,
                 json={"works": [saved], "folder": None}).raise_for_status()
    retry = requests.post(endpoint, headers=auth, files=payload, data=metadata, timeout=10)
    assert retry.status_code == 200 and retry.json()["id"] == saved
    assert library()["Retry proof"]["folder"] is None
    conflict = requests.post(endpoint, headers=auth, data=metadata, timeout=10,
                             files={"file": ("Retry proof.txt", b"different bytes", "text/plain")})
    assert conflict.status_code == 409
    return dict(private_in_nested_folder=True, file_picker_and_drop=True, bad_media_rejected=True,
                signed_out_watch_denied=True, filter_videos_and_files=True,
                video_width=width, audio_played=True, image_width=image_width,
                document_sha256=hashlib.sha256(record).hexdigest(), retry_keeps_placement=True)
