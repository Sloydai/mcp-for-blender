"""Sloyd generation stays behind the sidebar switch and never echoes the client secret."""
import asyncio
import logging
import socket
import sys
import types
import zlib

import pytest

from blender_mcp import generation
from test_meshy import _JsonResponse, _load_addon

CLIENT_ID = "sok_live_client-id-for-tests"
SECRET = "sloyd-super-secret-value"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 16
WEBP = b"RIFF\x10\0\0\0WEBPVP8 " + b"\0" * 8
GLB = b"glTF\x02\0\0\0" + b"\0" * 16
SLOYD_ENV = ("BLENDERMCP_SLOYD_API_KEY", "BLENDERMCP_SLOYD_CLIENT_ID", "BLENDERMCP_SLOYD_CLIENT_SECRET")


def _load(monkeypatch, **scene_attrs):
    for name in SLOYD_ENV:
        monkeypatch.delenv(name, raising=False)
    attrs = {"blendermcp_use_sloyd": False, "blendermcp_sloyd_client_id": "",
             "blendermcp_sloyd_client_secret": "", **scene_attrs}
    return _load_addon(monkeypatch, **attrs)


def _ready(monkeypatch, **scene_attrs):
    attrs = {"blendermcp_use_sloyd": True, "blendermcp_sloyd_client_id": CLIENT_ID,
             "blendermcp_sloyd_client_secret": SECRET, **scene_attrs}
    return _load(monkeypatch, **attrs)


def _capture_posts(addon, reply=None, status_code=200):
    posts = []

    def post(url, headers=None, json=None, files=None, data=None, **kwargs):
        posts.append({"url": url, "headers": headers or {}, "json": json, "files": files, "data": data})
        return _JsonResponse(reply if reply is not None else {"jobId": f"JOB{len(posts)}"}, status_code)

    addon.requests.post = post
    return posts


def _public_dns(monkeypatch, addon, address="93.184.216.34"):
    monkeypatch.setattr(addon.socket, "getaddrinfo",
                        lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])


class _Download:
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        yield self.content


def _fake_import(server):
    server._clean_imported_glb = lambda filepath, mesh_name=None: types.SimpleNamespace(
        name=mesh_name, type="MESH",
        location=types.SimpleNamespace(x=0, y=0, z=0),
        rotation_euler=types.SimpleNamespace(x=0, y=0, z=0),
        scale=types.SimpleNamespace(x=1, y=1, z=1),
    )
    server._get_aabb = lambda _obj: [[0, 0, 0], [1, 1, 1]]


# ------------------------------------------------------------------ status and credentials

def test_sloyd_status_reports_sidebar_state(monkeypatch):
    addon, _bpy, scene = _load(monkeypatch)
    server = addon.BlenderMCPServer()

    disabled = server.get_sloyd_status()
    assert disabled["enabled"] is False
    assert "check Sloyd" in disabled["message"]

    scene.blendermcp_use_sloyd = True
    missing = server.get_sloyd_status()
    assert missing["enabled"] is False
    assert "API key is not given" in missing["message"]

    scene.blendermcp_sloyd_client_id = CLIENT_ID
    assert server.get_sloyd_status()["enabled"] is False
    scene.blendermcp_sloyd_client_secret = SECRET
    ready = server.get_sloyd_status()
    assert ready["enabled"] is True
    assert SECRET not in str(ready)


def test_sloyd_is_own_key_only(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    monkeypatch.setattr(addon, "premium_active", lambda: True)
    server = addon.BlenderMCPServer()

    status = server.get_sloyd_status()
    assert status["enabled"] is False
    assert "own Sloyd API key only" in status["message"]
    blocked = server._execute_command_internal({"type": "create_sloyd_job", "params": {"text_prompt": "a chair"}})
    assert "Unknown command type" in blocked["message"]


@pytest.mark.parametrize("env", [
    {"BLENDERMCP_SLOYD_CLIENT_ID": CLIENT_ID, "BLENDERMCP_SLOYD_CLIENT_SECRET": SECRET},
    {"BLENDERMCP_SLOYD_API_KEY": f"{CLIENT_ID}:{SECRET}"},
])
def test_sloyd_credentials_from_env_go_in_client_headers(monkeypatch, env):
    addon, _bpy, _scene = _load(monkeypatch, blendermcp_use_sloyd=True)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    posts = _capture_posts(addon)

    addon.BlenderMCPServer().create_sloyd_job(text_prompt="a chair")

    assert posts[0]["headers"]["x-client-id"] == CLIENT_ID
    assert posts[0]["headers"]["x-client-secret"] == SECRET
    assert posts[0]["headers"]["User-Agent"].startswith("mcp-for-blender/")


def test_sloyd_api_key_without_a_secret_part_is_not_a_credential(monkeypatch):
    addon, _bpy, _scene = _load(monkeypatch, blendermcp_use_sloyd=True)
    monkeypatch.setenv("BLENDERMCP_SLOYD_API_KEY", CLIENT_ID)
    posts = _capture_posts(addon)

    result = addon.BlenderMCPServer().create_sloyd_job(text_prompt="a chair")

    assert "not given" in result["error"]
    assert posts == []


# ------------------------------------------------------------------ create

def test_sloyd_text_job(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon, {"jobId": "01JZQY4YJ7KV1GQZ9G6H6H2X25"})

    result = addon.BlenderMCPServer().create_sloyd_job(text_prompt="  a low-poly pine tree ")

    assert result == {"job_id": "01JZQY4YJ7KV1GQZ9G6H6H2X25", "mode": "text"}
    assert posts[0]["url"] == "https://api.sloyd.ai/api/jobs/text-to-3d"
    assert posts[0]["json"] == {"prompt": "a low-poly pine tree"}
    assert SECRET not in str(result)


@pytest.mark.parametrize("data, mime", [(PNG, "image/png"), (JPEG, "image/jpeg"), (WEBP, "image/webp")])
def test_sloyd_local_image_is_uploaded_with_its_real_type(monkeypatch, tmp_path, data, mime):
    image = tmp_path / "ref.png"
    image.write_bytes(data)
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    result = addon.BlenderMCPServer().create_sloyd_job(image=str(image))

    assert result == {"job_id": "JOB1", "mode": "image"}
    assert posts[0]["url"] == "https://api.sloyd.ai/api/jobs/image-to-3d"
    _filename, body, sent_mime = posts[0]["files"]["file"]
    assert body == data and sent_mime == mime
    assert posts[0]["data"] is None


def test_sloyd_refuses_a_local_file_that_is_not_an_image(monkeypatch, tmp_path):
    secret_file = tmp_path / "credentials.png"
    secret_file.write_bytes(b"OPENAI_API_KEY=sk-not-an-image\n")
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    result = addon.BlenderMCPServer().create_sloyd_job(image=str(secret_file))

    assert "Not a PNG, JPEG or WebP" in result["error"]
    assert posts == []


def test_sloyd_refuses_an_image_url_on_a_private_address(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    _public_dns(monkeypatch, addon, "192.168.1.10")
    posts = _capture_posts(addon)
    addon.requests.get = lambda *_a, **_k: pytest.fail("a private address must not be fetched")

    result = addon.BlenderMCPServer().create_sloyd_job(image="http://router.local/admin.png")

    assert "local or private address" in result["error"]
    assert posts == []


def test_sloyd_public_image_url_is_fetched_then_uploaded(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    _public_dns(monkeypatch, addon)
    fetched = []

    def get(url, **kwargs):
        fetched.append((url, kwargs.get("headers") or {}))
        return _Download(JPEG)

    addon.requests.get = get
    posts = _capture_posts(addon)

    result = addon.BlenderMCPServer().create_sloyd_job(image="https://example.com/chair.jpg")

    assert result["mode"] == "image"
    assert fetched[0][0] == "https://example.com/chair.jpg"
    assert "x-client-secret" not in fetched[0][1]
    assert posts[0]["files"]["file"][1:] == (JPEG, "image/jpeg")


def test_sloyd_folder_of_views_is_one_multi_image_job(monkeypatch, tmp_path):
    (tmp_path / "Front.png").write_bytes(PNG)
    (tmp_path / "back.jpg").write_bytes(JPEG)
    (tmp_path / "left.webp").write_bytes(WEBP)
    (tmp_path / "notes.txt").write_text("ignored")
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    result = addon.BlenderMCPServer().create_sloyd_job(image=str(tmp_path))

    assert result == {"job_id": "JOB1", "mode": "multi-image"}
    assert posts[0]["url"] == "https://api.sloyd.ai/api/jobs/multi-image-to-3d"
    assert sorted(posts[0]["files"]) == ["backImage", "frontImage", "leftImage"]
    assert posts[0]["files"]["frontImage"][1] == PNG
    assert posts[0]["data"] == {"EnablePBR": "true"}


@pytest.mark.parametrize("files", [["front.png"], ["left.png", "back.png"], []])
def test_sloyd_folder_needs_front_and_one_more_view(monkeypatch, tmp_path, files):
    for name in files:
        (tmp_path / name).write_bytes(PNG)
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    result = addon.BlenderMCPServer().create_sloyd_job(image=str(tmp_path))

    assert "front.png" in result["error"]
    assert posts == []


@pytest.mark.parametrize("kwargs, needle", [
    ({"text_prompt": "a chair", "image": "https://example.com/a.png"}, "not both"),
    ({}, "not both"),
    ({"text_prompt": "x" * 4097}, "4096"),
    ({"image": "/no/such/file.png"}, "not found"),
])
def test_sloyd_rejects_bad_input_without_calling_api(monkeypatch, kwargs, needle):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    result = addon.BlenderMCPServer().create_sloyd_job(**kwargs)

    assert needle in result["error"]
    assert posts == []


# ------------------------------------------------------------------ mesh options

LOWPOLY = {"targetFaceCount": 10000, "textureResolution": "1k", "topology": "triangles"}


@pytest.mark.parametrize("quality, fields", [
    ("lowpoly", LOWPOLY),
    ("standard", {"targetFaceCount": 40000, "textureResolution": "2k", "topology": "auto"}),
    ("high", {"targetFaceCount": 100000, "textureResolution": "2k", "topology": "triangles"}),
])
def test_sloyd_quality_presets_set_text_fields(monkeypatch, quality, fields):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    addon.BlenderMCPServer().create_sloyd_job(text_prompt="a treasure chest", quality=quality)

    assert posts[0]["json"] == {"prompt": "a treasure chest", **fields}


def test_sloyd_lowpoly_image_sends_form_fields(monkeypatch, tmp_path):
    image = tmp_path / "chest.png"
    image.write_bytes(PNG)
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    addon.BlenderMCPServer().create_sloyd_job(image=str(image), quality="lowpoly")

    assert posts[0]["data"] == {"targetFaceCount": "10000", "textureResolution": "1k", "topology": "triangles"}


def test_sloyd_lowpoly_views_send_only_a_face_count(monkeypatch, tmp_path):
    (tmp_path / "front.png").write_bytes(PNG)
    (tmp_path / "back.png").write_bytes(PNG)
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    addon.BlenderMCPServer().create_sloyd_job(image=str(tmp_path), quality="lowpoly")

    assert posts[0]["data"] == {"EnablePBR": "true", "FaceCount": "10000"}


@pytest.mark.parametrize("quality, face_count, expected", [
    (None, 5000, {"targetFaceCount": 5000}),
    ("high", 5000, {"targetFaceCount": 5000, "textureResolution": "2k", "topology": "triangles"}),
    (None, 500000, {"targetFaceCount": 500000}),
    (None, 900000, {"targetFaceCount": 500000}),
])
def test_sloyd_face_count_overrides_the_preset_and_is_clamped(monkeypatch, quality, face_count, expected):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    addon.BlenderMCPServer().create_sloyd_job(text_prompt="a chest", quality=quality, face_count=face_count)

    assert posts[0]["json"] == {"prompt": "a chest", **expected}


@pytest.mark.parametrize("kwargs, needle", [
    ({"quality": "ultra"}, "quality must be one of: lowpoly, standard, high"),
    ({"face_count": 0}, "face_count must be a whole number from 1 to 500000"),
    ({"face_count": -5}, "face_count must be a whole number"),
    ({"face_count": 1.5}, "face_count must be a whole number"),
    ({"face_count": "10000"}, "face_count must be a whole number"),
    ({"face_count": True}, "face_count must be a whole number"),
    ({"pose_mode": "a-pose"}, "'t-pose' only"),
])
def test_sloyd_rejects_bad_mesh_options_without_calling_api(monkeypatch, kwargs, needle):
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)

    result = addon.BlenderMCPServer().create_sloyd_job(text_prompt="a chest", **kwargs)

    assert needle in result["error"]
    assert posts == []


def test_sloyd_t_pose_is_for_text_only(monkeypatch, tmp_path):
    image = tmp_path / "knight.png"
    image.write_bytes(PNG)
    addon, _bpy, _scene = _ready(monkeypatch)
    posts = _capture_posts(addon)
    server = addon.BlenderMCPServer()

    assert "'t-pose' only" in server.create_sloyd_job(image=str(image), pose_mode="t-pose")["error"]
    assert posts == []
    server.create_sloyd_job(text_prompt="a knight", pose_mode="t-pose")
    assert posts[0]["json"] == {"prompt": "a knight", "tPose": True}


@pytest.mark.parametrize("reply", [{}, {"jobId": "../../etc"}, {"jobId": 7}])
def test_sloyd_create_needs_a_well_formed_job_id(monkeypatch, reply):
    addon, _bpy, _scene = _ready(monkeypatch)
    _capture_posts(addon, reply)
    result = addon.BlenderMCPServer().create_sloyd_job(text_prompt="a chair")
    assert result == {"error": "Sloyd response did not include a job id"}


# ------------------------------------------------------------------ errors

@pytest.mark.parametrize("status_code, message", [
    (401, "Sloyd rejected the client ID and secret. Check them at api-dashboard.sloyd.ai"),
    (402, "Not enough Sloyd API credits. Buy prepaid credits at api-dashboard.sloyd.ai, then retry"),
    (429, "Sloyd allows 5 running jobs at once per account, including jobs started in the Sloyd web app. "
          "Wait for one to finish, then retry"),
    (500, "Sloyd request failed with status 500"),
])
def test_sloyd_errors_are_fixed_and_never_show_the_secret(monkeypatch, capsys, status_code, message):
    addon, _bpy, _scene = _ready(monkeypatch)
    # Even a reply that echoes the credentials must not reach the user.
    _capture_posts(addon, {"statusCode": status_code, "message": f"bad {CLIENT_ID}:{SECRET}"}, status_code)
    server = addon.BlenderMCPServer()

    reply = server._execute_command_internal({"type": "create_sloyd_job", "params": {"text_prompt": "a chair"}})

    assert reply["result"] == {"error": message}
    assert SECRET not in str(reply)
    assert SECRET not in capsys.readouterr().out


def test_sloyd_network_failure_never_shows_the_secret(monkeypatch, capsys):
    addon, _bpy, _scene = _ready(monkeypatch)

    def post(*_a, **kwargs):
        raise ConnectionError(f"boom x-client-secret: {kwargs['headers']['x-client-secret']}")

    addon.requests.post = post
    reply = addon.BlenderMCPServer()._execute_command_internal(
        {"type": "create_sloyd_job", "params": {"text_prompt": "a chair"}})

    assert "internet connection" in reply["result"]["error"]
    assert SECRET not in str(reply)
    assert SECRET not in capsys.readouterr().out


# ------------------------------------------------------------------ poll

@pytest.mark.parametrize("payload, expected", [
    ({"status": "success", "isSuccess": True}, {"status": "SUCCEEDED"}),
    ({"status": "completed"}, {"status": "SUCCEEDED"}),
    ({"status": "running", "fragments": [{"name": "q", "displayName": "Waiting in Queue"},
                                         {"name": "m", "displayName": "Building mesh"}]},
     {"status": "IN_PROGRESS", "stage": "Building mesh"}),
    ({"status": "pending"}, {"status": "IN_PROGRESS", "stage": None}),
    ({"status": "error", "errorMessage": "Prompt flagged"}, {"status": "FAILED", "error": "Prompt flagged"}),
    ({"status": "failed"}, {"status": "FAILED", "error": "Sloyd job failed"}),
])
def test_sloyd_poll_maps_job_statuses(monkeypatch, payload, expected):
    addon, _bpy, _scene = _ready(monkeypatch)
    seen = {}

    def get(url, headers=None, **_kwargs):
        seen["url"], seen["headers"] = url, headers
        return _JsonResponse({"id": "J1", **payload})

    addon.requests.get = get
    assert addon.BlenderMCPServer().poll_sloyd_job_status(job_id="J1") == expected
    assert seen["url"] == "https://api.sloyd.ai/api/jobs/J1"
    assert seen["headers"]["x-client-id"] == CLIENT_ID


def test_sloyd_poll_explains_a_detected_scene(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    addon.requests.get = lambda *_a, **_k: _JsonResponse({"status": "scene-detected"})
    result = addon.BlenderMCPServer().poll_sloyd_job_status(job_id="J1")
    assert result["status"] == "FAILED" and "one object" in result["error"]


@pytest.mark.parametrize("job_id", [None, "", "../users", "a/b", "x" * 129])
def test_sloyd_poll_and_import_refuse_malformed_job_ids(monkeypatch, job_id):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = addon.BlenderMCPServer()
    assert "error" in server.poll_sloyd_job_status(job_id=job_id)
    assert server.import_generated_asset_sloyd(name="X", job_id=job_id)["succeed"] is False


def test_sloyd_poll_with_a_bad_key_is_a_fixed_message(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    addon.requests.get = lambda *_a, **_k: _JsonResponse({"message": SECRET}, 401)
    result = addon.BlenderMCPServer().poll_sloyd_job_status(job_id="J1")
    assert result == {"error": "Sloyd rejected the client ID and secret. Check them at api-dashboard.sloyd.ai"}


# ------------------------------------------------------------------ import

PUBLIC_GLB = "https://storage.googleapis.com/ai-services-quality/jobs/J1.glb"


def _signed(n):
    return f"{PUBLIC_GLB}?X-Goog-Expires=300&X-Goog-Signature=sig{n}"


class _FakeSloyd:
    """Sloyd's job API and its result storage. Each finished job read hands out
    the next of `links` (the last repeats); `downloads` maps a link to its reply."""

    def __init__(self, monkeypatch, addon, statuses=("success",), links=(PUBLIC_GLB,), downloads=None,
                 field="outputs"):
        self.statuses, self.links, self.field = list(statuses), list(links), field
        self.downloads = downloads or {}
        self.calls = []
        _public_dns(monkeypatch, addon)
        addon.requests.get = self.get

    def get(self, url, headers=None, **_kwargs):
        self.calls.append((url, dict(headers or {})))
        if url.startswith("https://api.sloyd.ai/"):
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            job = {"id": "J1", "status": status}
            if status == "success" and self.links:
                link = self.links.pop(0) if len(self.links) > 1 else self.links[0]
                job.update({"outputs": {"glbUrl": link}} if self.field == "outputs" else {"glbUrl": link})
            return _JsonResponse(job)
        return self.downloads.get(url, _Download(GLB))

    @property
    def job_reads(self):
        return [url for url, _ in self.calls if url.startswith("https://api.sloyd.ai/")]

    @property
    def fetched(self):
        return [url for url, _ in self.calls if not url.startswith("https://api.sloyd.ai/")]


@pytest.mark.parametrize("field, link", [
    ("outputs", PUBLIC_GLB),       # today's public URLs, in Sloyd's MCP shape
    ("outputs", _signed(1)),
    ("top", _signed(1)),
])
def test_sloyd_import_downloads_the_url_from_the_job_without_credentials(monkeypatch, field, link):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = addon.BlenderMCPServer()
    _fake_import(server)
    sloyd = _FakeSloyd(monkeypatch, addon, links=[link], field=field)

    result = server.import_generated_asset_sloyd(name="Tree", job_id="J1")

    assert result["succeed"] is True and result["name"] == "Tree"
    assert sloyd.job_reads == ["https://api.sloyd.ai/api/jobs/J1"]
    assert sloyd.fetched == [link]
    download_headers = sloyd.calls[-1][1]
    assert not any("client" in header.lower() for header in download_headers)


# Temporary legacy fallback: today's API sends no URL field.

def test_sloyd_legacy_public_url_only_when_the_job_has_no_url_field(monkeypatch, capsys):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = addon.BlenderMCPServer()
    _fake_import(server)
    sloyd = _FakeSloyd(monkeypatch, addon, links=[])

    result = server.import_generated_asset_sloyd(name="Tree", job_id="J1")

    assert result["succeed"] is True
    assert sloyd.fetched == [PUBLIC_GLB]
    out = capsys.readouterr().out
    assert "legacy public URL" in out
    assert "storage.googleapis.com" not in out and SECRET not in out


@pytest.mark.parametrize("field", ["outputs", "top"])
def test_sloyd_url_field_wins_over_the_legacy_url(monkeypatch, capsys, field):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = addon.BlenderMCPServer()
    _fake_import(server)
    sloyd = _FakeSloyd(monkeypatch, addon, links=[_signed(1)], field=field)

    assert server.import_generated_asset_sloyd(name="Tree", job_id="J1")["succeed"] is True
    assert sloyd.fetched == [_signed(1)]
    assert "legacy" not in capsys.readouterr().out


@pytest.mark.parametrize("link", ["", "http://storage.googleapis.com/jobs/J1.glb"])
def test_sloyd_bad_url_field_is_not_replaced_by_the_legacy_url(monkeypatch, link):
    addon, _bpy, _scene = _ready(monkeypatch)
    sloyd = _FakeSloyd(monkeypatch, addon, links=[link])
    result = addon.BlenderMCPServer().import_generated_asset_sloyd(name="Tree", job_id="J1")
    assert result == {"succeed": False, "error": "Sloyd finished the job but sent no download URL"}
    assert sloyd.fetched == []


@pytest.mark.parametrize("job_id", ["../users", "J1?x=1", "a/b", "x" * 129, None])
def test_sloyd_legacy_url_needs_a_well_formed_job_id(monkeypatch, job_id):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = addon.BlenderMCPServer()
    sloyd = _FakeSloyd(monkeypatch, addon, links=[])

    url, error = server._sloyd_result_url(job_id)

    assert url is None and error == {"error": "Sloyd finished the job but sent no download URL"}
    assert server.import_generated_asset_sloyd(name="Tree", job_id=job_id)["succeed"] is False
    assert sloyd.fetched == []


def test_sloyd_legacy_url_gets_the_public_address_check(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    sloyd = _FakeSloyd(monkeypatch, addon, links=[])
    _public_dns(monkeypatch, addon, "127.0.0.1")
    result = addon.BlenderMCPServer().import_generated_asset_sloyd(name="Tree", job_id="J1")
    assert result == {"succeed": False, "error": "Sloyd's download link is not on a public address"}
    assert sloyd.fetched == []


@pytest.mark.parametrize("status_code", [400, 401, 403, 410])
def test_sloyd_expired_link_gets_one_fresh_url(monkeypatch, status_code):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = addon.BlenderMCPServer()
    _fake_import(server)
    sloyd = _FakeSloyd(monkeypatch, addon, links=[_signed(1), _signed(2)],
                       downloads={_signed(1): _Download(b"<Error>ExpiredToken</Error>", status_code)})

    result = server.import_generated_asset_sloyd(name="Tree", job_id="J1")

    assert result["succeed"] is True
    assert sloyd.fetched == [_signed(1), _signed(2)]
    assert len(sloyd.job_reads) == 2


def test_sloyd_link_refused_twice_stops_and_says_resume(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = addon.BlenderMCPServer()
    server._clean_imported_glb = lambda **_k: pytest.fail("must not import")
    refused = _Download(b"", 403)
    sloyd = _FakeSloyd(monkeypatch, addon, links=[_signed(1), _signed(2)],
                       downloads={_signed(1): refused, _signed(2): refused})

    result = server.import_generated_asset_sloyd(name="Tree", job_id="J1")

    assert result["succeed"] is False and "Resume the job" in result["error"]
    assert len(sloyd.fetched) == 2 and len(sloyd.job_reads) == 2
    assert "sig" not in result["error"]


@pytest.mark.parametrize("link, needle", [
    ("http://storage.googleapis.com/jobs/J1.glb", "sent no download URL"),
    ("file:///etc/passwd", "sent no download URL"),
])
def test_sloyd_import_needs_an_https_link(monkeypatch, link, needle):
    addon, _bpy, _scene = _ready(monkeypatch)
    sloyd = _FakeSloyd(monkeypatch, addon, links=[link])
    result = addon.BlenderMCPServer().import_generated_asset_sloyd(name="Tree", job_id="J1")
    assert needle in result["error"] and sloyd.fetched == []


def test_sloyd_import_refuses_a_link_on_a_private_address(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    sloyd = _FakeSloyd(monkeypatch, addon, links=["https://files.internal/J1.glb"])
    _public_dns(monkeypatch, addon, "10.0.0.5")
    result = addon.BlenderMCPServer().import_generated_asset_sloyd(name="Tree", job_id="J1")
    assert result == {"succeed": False, "error": "Sloyd's download link is not on a public address"}
    assert sloyd.fetched == []


def test_sloyd_import_of_an_unfinished_job_fetches_nothing(monkeypatch):
    addon, _bpy, _scene = _ready(monkeypatch)
    sloyd = _FakeSloyd(monkeypatch, addon, statuses=["running"])
    result = addon.BlenderMCPServer().import_generated_asset_sloyd(name="Tree", job_id="J1")
    assert result == {"succeed": False, "error": "Sloyd job is not finished"}
    assert sloyd.fetched == []


@pytest.mark.parametrize("download, needle", [
    (_Download(b"<html>NoSuchKey</html>"), "not a GLB"),
    (_Download(b"", 404), "Failed to import"),
])
def test_sloyd_import_refuses_what_is_not_a_glb(monkeypatch, download, needle):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = addon.BlenderMCPServer()
    server._clean_imported_glb = lambda **_k: pytest.fail("must not import")
    _FakeSloyd(monkeypatch, addon, downloads={PUBLIC_GLB: download})

    result = server.import_generated_asset_sloyd(name="Tree", job_id="J1")

    assert result["succeed"] is False and needle in result["error"]


# ------------------------------------------------------------------ dispatch

def test_sloyd_commands_stay_unregistered_until_enabled(monkeypatch):
    addon, _bpy, scene = _ready(monkeypatch, blendermcp_use_sloyd=False)
    server = addon.BlenderMCPServer()

    for command in ("create_sloyd_job", "poll_sloyd_job_status", "import_generated_asset_sloyd"):
        blocked = server._execute_command_internal({"type": command, "params": {}})
        assert "Unknown command type" in blocked["message"]
    assert server._execute_command_internal({"type": "get_sloyd_status", "params": {}})["status"] == "success"

    scene.blendermcp_use_sloyd = True
    _capture_posts(addon)
    allowed = server._execute_command_internal({"type": "create_sloyd_job", "params": {"text_prompt": "a chair"}})
    assert allowed["result"]["job_id"] == "JOB1"


# ------------------------------------------------------------------ end to end through the MCP server

class _Ctx:
    async def report_progress(self, *_):
        pass


def _wire_server(monkeypatch, addon, tmp_path):
    """The MCP server's generate_3d talking to the real addon handlers."""
    from blender_mcp import server

    blender_server = addon.BlenderMCPServer()
    _fake_import(blender_server)

    class Connection:
        def send_command(self, command, params=None, **_kw):
            reply = blender_server._execute_command_internal({"type": command, "params": params or {}})
            if reply["status"] == "error":
                logging.getLogger("BlenderMCPServer").error(f"Blender error: {reply['message']}")
                raise Exception(reply["message"])
            return reply["result"]

    monkeypatch.setattr(server, "get_blender_connection", lambda: Connection())
    monkeypatch.setattr(server, "_premium_generators", lambda _: [])
    monkeypatch.setattr(server, "_addon_protocol", lambda: 16)
    monkeypatch.setattr(server, "_pending_generations", lambda: generation.Pending(tmp_path / "pending.json"))
    monkeypatch.setattr(server, "_run_script", lambda *_a: [])

    real_wait = generation.wait_and_import

    async def no_sleep(_):
        pass

    def fast_wait(send, job, wait_seconds, progress=None, **kwargs):
        ticks = iter(range(0, 10_000, 5))
        return real_wait(send, job, wait_seconds, progress, sleep=no_sleep, clock=lambda: next(ticks), **kwargs)

    monkeypatch.setattr(generation, "wait_and_import", fast_wait)
    return server


def test_generate_3d_with_sloyd_end_to_end_never_shows_the_secret(monkeypatch, tmp_path, caplog, capsys):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = _wire_server(monkeypatch, addon, tmp_path)
    _capture_posts(addon, {"jobId": "J1"})
    sloyd = _FakeSloyd(monkeypatch, addon, statuses=["pending", "running", "success"], links=[_signed(1)])
    caplog.set_level(logging.DEBUG)

    reply = asyncio.run(server.generate_3d(_Ctx(), prompt="a pine tree", provider="sloyd", wait_seconds=45))

    assert reply.startswith("Generated and imported 'APineTree' with sloyd.")
    assert sloyd.fetched == [_signed(1)]
    # The signed link is a credential while it lasts, so it stays in the addon too.
    for output in (reply, caplog.text, capsys.readouterr().out):
        assert SECRET not in output
        assert "X-Goog-Signature" not in output


def test_generate_3d_low_poly_request_reaches_sloyd_as_low_poly_fields(monkeypatch, tmp_path):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = _wire_server(monkeypatch, addon, tmp_path)
    posts = _capture_posts(addon, {"jobId": "J1"})
    addon.requests.get = lambda *_a, **_k: _JsonResponse({"id": "J1", "status": "running"})

    reply = asyncio.run(server.generate_3d(_Ctx(), prompt="a low-poly treasure chest", quality="lowpoly",
                                           wait_seconds=10))

    assert 'job="sloyd:job:J1"' in reply
    assert posts[0]["json"] == {"prompt": "a low-poly treasure chest", **LOWPOLY}


def test_generate_3d_with_a_bad_sloyd_key_says_so_and_never_shows_it(monkeypatch, tmp_path, caplog, capsys):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = _wire_server(monkeypatch, addon, tmp_path)
    posts = _capture_posts(addon, {"message": f"Invalid API credentials {SECRET}"}, 401)
    caplog.set_level(logging.DEBUG)

    reply = asyncio.run(server.generate_3d(_Ctx(), prompt="a pine tree", provider="sloyd", wait_seconds=10))

    assert reply == "Error: Sloyd rejected the client ID and secret. Check them at api-dashboard.sloyd.ai"
    assert len(posts) == 1
    for output in (reply, caplog.text, capsys.readouterr().out):
        assert SECRET not in output


def test_generate_3d_with_sloyd_resumes_after_a_timeout_without_paying_again(monkeypatch, tmp_path):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = _wire_server(monkeypatch, addon, tmp_path)
    posts = _capture_posts(addon, {"jobId": "J1"})
    sloyd = _FakeSloyd(monkeypatch, addon, statuses=["running"], links=[_signed(1), _signed(2), _signed(3)],
                       downloads={_signed(2): _Download(b"", 403)})

    first = asyncio.run(server.generate_3d(_Ctx(), prompt="a pine tree", provider="sloyd", wait_seconds=10))
    assert first.startswith("Still generating")
    assert 'job="sloyd:job:J1"' in first

    # The same request again resumes the pending job; so does its handle.
    second = asyncio.run(server.generate_3d(_Ctx(), prompt="a pine tree", provider="sloyd", wait_seconds=10))
    assert "resumed that generation" in second
    sloyd.statuses = ["success"]
    done = asyncio.run(server.generate_3d(_Ctx(), job="sloyd:job:J1", name="Pine", wait_seconds=10))

    assert done.startswith("Generated and imported 'Pine' with sloyd.")
    assert len(posts) == 1
    # The status poll's link is not reused: the import reads the job again, and
    # when that link is refused it reads once more.
    assert sloyd.fetched == [_signed(2), _signed(3)]


def test_generate_3d_against_todays_api_uses_the_legacy_url_and_leaks_nothing(monkeypatch, tmp_path, caplog,
                                                                             capsys):
    addon, _bpy, _scene = _ready(monkeypatch)
    server = _wire_server(monkeypatch, addon, tmp_path)
    _capture_posts(addon, {"jobId": "J1"})
    sloyd = _FakeSloyd(monkeypatch, addon, statuses=["running", "success"], links=[])
    caplog.set_level(logging.DEBUG)

    reply = asyncio.run(server.generate_3d(_Ctx(), prompt="a treasure chest", provider="sloyd", wait_seconds=45))

    assert reply.startswith("Generated and imported 'ATreasureChest' with sloyd.")
    assert sloyd.fetched == [PUBLIC_GLB]
    for output in (reply, caplog.text, capsys.readouterr().out):
        assert SECRET not in output
        assert "storage.googleapis.com" not in output


def test_resume_after_a_long_wait_reads_a_fresh_link(monkeypatch, tmp_path):
    # A handle resumed long after the job finished: no link is kept from earlier polls.
    addon, _bpy, _scene = _ready(monkeypatch)
    server = _wire_server(monkeypatch, addon, tmp_path)
    sloyd = _FakeSloyd(monkeypatch, addon, links=[_signed(7)])

    done = asyncio.run(server.generate_3d(_Ctx(), job="sloyd:job:J1", name="Pine", wait_seconds=10))

    assert done.startswith("Generated and imported 'Pine' with sloyd.")
    assert sloyd.fetched == [_signed(7)]
    assert sloyd.job_reads[-1] == "https://api.sloyd.ai/api/jobs/J1"


# ------------------------------------------------------------------ sidebar logo

class _Layout:
    def __init__(self):
        self.labels = []

    def box(self):
        return self

    def row(self):
        return self

    def prop(self, *_args, **_kwargs):
        pass

    def label(self, **kwargs):
        self.labels.append(kwargs)


class _Previews:
    """Stands in for bpy.utils.previews: new(), load() and remove()."""

    def __init__(self, fail_on_new=False):
        self.fail_on_new = fail_on_new
        self.loaded = {}
        self.removed = []

    def new(self):
        if self.fail_on_new:
            raise RuntimeError("no previews in this build")
        previews = self

        class _Collection(dict):
            def load(self, name, path, kind):
                with open(path, "rb") as f:
                    previews.loaded[name] = (f.read(), kind)
                self[name] = types.SimpleNamespace(icon_id=42)

        return _Collection()

    def remove(self, collection):
        self.removed.append(collection)


def _install_previews(monkeypatch, bpy, previews):
    utils = types.ModuleType("bpy.utils")
    module = types.ModuleType("bpy.utils.previews")
    module.new, module.remove = previews.new, previews.remove
    utils.previews = module
    monkeypatch.setattr(bpy, "utils", utils, raising=False)
    monkeypatch.setitem(sys.modules, "bpy.utils", utils)
    monkeypatch.setitem(sys.modules, "bpy.utils.previews", module)


def _sloyd_row_label(addon, scene):
    layout = _Layout()
    panel = addon.BLENDERMCP_PT_Panel()
    panel._integration_header(layout, scene, "blendermcp_use_sloyd", "Sloyd", 'OUTLINER_OB_MESH',
                              icon_value=addon._sloyd_icon_id())
    panel._integration_header(layout, scene, "blendermcp_use_tripo", "Tripo", 'OUTLINER_OB_MESH')
    return layout.labels


def test_sloyd_logo_falls_back_to_the_builtin_icon_without_previews(monkeypatch, tmp_path, capsys):
    addon, _bpy, scene = _load(monkeypatch)
    monkeypatch.setattr(addon.tempfile, "tempdir", str(tmp_path))
    monkeypatch.delitem(sys.modules, "bpy.utils.previews", raising=False)

    addon._sloyd_icon_load()

    assert addon._sloyd_icon_id() == 0
    assert list(tmp_path.iterdir()) == []
    assert str(tmp_path) not in capsys.readouterr().out
    assert _sloyd_row_label(addon, scene) == [{"text": "Sloyd", "icon": 'OUTLINER_OB_MESH'},
                                              {"text": "Tripo", "icon": 'OUTLINER_OB_MESH'}]
    addon._sloyd_icon_unload()


def test_sloyd_logo_falls_back_and_cleans_up_when_previews_fail(monkeypatch, tmp_path):
    addon, bpy, scene = _load(monkeypatch)
    monkeypatch.setattr(addon.tempfile, "tempdir", str(tmp_path))
    _install_previews(monkeypatch, bpy, _Previews(fail_on_new=True))

    addon._sloyd_icon_load()

    assert addon._sloyd_icon_id() == 0
    assert list(tmp_path.iterdir()) == []
    assert _sloyd_row_label(addon, scene)[0] == {"text": "Sloyd", "icon": 'OUTLINER_OB_MESH'}


def test_sloyd_logo_is_drawn_on_the_sloyd_row_only_and_freed_on_unregister(monkeypatch, tmp_path):
    addon, bpy, scene = _load(monkeypatch)
    monkeypatch.setattr(addon.tempfile, "tempdir", str(tmp_path))
    previews = _Previews()
    _install_previews(monkeypatch, bpy, previews)

    addon._sloyd_icon_load()

    data, kind = previews.loaded["sloyd"]
    assert data.startswith(b"\x89PNG\r\n\x1a\n") and kind == 'IMAGE'
    assert addon._sloyd_icon_id() == 42
    assert _sloyd_row_label(addon, scene) == [{"text": "Sloyd", "icon_value": 42},
                                              {"text": "Tripo", "icon": 'OUTLINER_OB_MESH'}]

    addon._sloyd_icon_unload()

    assert len(previews.removed) == 1
    assert list(tmp_path.iterdir()) == []
    assert addon._sloyd_icon_id() == 0


def _rgba_pixels(png):
    """(width, height, [(r, g, b, a), ...]) of an 8-bit RGBA PNG, decoded with zlib only."""
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    pos, idat = 8, b""
    while pos < len(png):
        length = int.from_bytes(png[pos:pos + 4], "big")
        kind, body = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            width, height = int.from_bytes(body[:4], "big"), int.from_bytes(body[4:8], "big")
            assert body[8:10] == b"\x08\x06", "8-bit RGBA"
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw, stride, rows = zlib.decompress(idat), width * 4, []
    previous = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        mode, line = raw[start], bytearray(raw[start + 1:start + 1 + stride])
        for i in range(stride):
            left = line[i - 4] if i >= 4 else 0
            up, up_left = previous[i], previous[i - 4] if i >= 4 else 0
            if mode == 1:
                line[i] = (line[i] + left) & 0xFF
            elif mode == 2:
                line[i] = (line[i] + up) & 0xFF
            elif mode == 3:
                line[i] = (line[i] + (left + up) // 2) & 0xFF
            elif mode == 4:
                guess = left + up - up_left
                near = min((abs(guess - left), left), (abs(guess - up), up), (abs(guess - up_left), up_left),
                           key=lambda pair: pair[0])
                line[i] = (line[i] + near[1]) & 0xFF
        rows.append(line)
        previous = line
    pixels = [tuple(row[i:i + 4]) for row in rows for i in range(0, stride, 4)]
    return width, height, pixels


def test_sloyd_logo_is_a_white_32px_mark_like_the_builtin_icons():
    import base64

    from conftest import ROOT_ADDON

    source = ROOT_ADDON.read_text(encoding="utf-8")
    start = source.index("SLOYD_ICON_PNG = (")
    encoded = "".join(part.strip().strip('"') for part in source[start:source.index(")", start)].splitlines()[1:])

    width, height, pixels = _rgba_pixels(base64.b64decode(encoded))

    assert (width, height) == (32, 32)
    visible = [pixel for pixel in pixels if pixel[3]]
    assert len(visible) > 100
    assert all(pixel[:3] == (255, 255, 255) for pixel in visible)
    assert any(pixel[3] == 0 for pixel in pixels)
