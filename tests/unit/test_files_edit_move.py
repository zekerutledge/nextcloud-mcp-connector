"""Revision-bound text editing and collision-free file move/rename behavior."""

import base64

import guard_routes
import httpx
import pytest
import respx

from mcp_connector.errors import ConflictError, ToolError
from mcp_connector.nextcloud import NcClients
from mcp_connector.nextcloud.credentials import Credentials
from mcp_connector.tools import files as files_tools

BASE = "http://nc.test"
USER = "alice"
SECRET = "app-password-test"
SOURCE = "/Docs/note.md"
DESTINATION = "/Archive/renamed.md"
FILES_ROOT = f"{BASE}/remote.php/dav/files/{USER}"
SOURCE_URL = f"{FILES_ROOT}{SOURCE}"
DESTINATION_URL = f"{FILES_ROOT}{DESTINATION}"
OLD = b"old\n"
NEW = b"# Updated\n"


def propfind(path: str, *, size: int, etag: str, fileid: str = "4711", folder: bool = False) -> str:
    resource = "<d:collection/>" if folder else ""
    href = f"/remote.php/dav/files/{USER}{path}"
    return f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:response>
<d:href>{href}</d:href><d:propstat><d:prop>
<d:getcontentlength>{size}</d:getcontentlength><d:getcontenttype>text/markdown</d:getcontenttype>
<d:getetag>{etag.replace('"', "&quot;")}</d:getetag><d:resourcetype>{resource}</d:resourcetype>
<oc:fileid>{fileid}</oc:fileid><oc:permissions>RGDNVW</oc:permissions>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"""


@pytest.fixture(autouse=True)
def no_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    guard_routes.patch_untagged(monkeypatch)


@pytest.fixture
def clients() -> NcClients:
    return NcClients(
        client=httpx.AsyncClient(follow_redirects=False),
        creds=Credentials(BASE, USER, SECRET),
    )


@pytest.mark.anyio
async def test_edit_is_etag_bound_and_requires_exact_readback(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        stat = mock.route(method="PROPFIND", url=SOURCE_URL).mock(
            side_effect=[
                httpx.Response(207, text=propfind(SOURCE, size=len(OLD), etag='"old"')),
                httpx.Response(207, text=propfind(SOURCE, size=len(NEW), etag='"new"')),
            ]
        )
        put = mock.route(method="PUT", url=SOURCE_URL).mock(return_value=httpx.Response(204))
        get = mock.route(method="GET", url=SOURCE_URL).mock(
            return_value=httpx.Response(
                206, content=NEW, headers={"Content-Range": f"bytes 0-{len(NEW) - 1}/{len(NEW)}"}
            )
        )
        result = await files_tools.edit(clients, SOURCE, NEW.decode())

    assert stat.call_count == 2
    assert get.call_count == 1
    request = put.calls[0].request
    assert request.headers["if-match"] == '"old"'
    assert request.headers["content-type"] == "text/markdown"
    assert request.content == NEW
    assert (
        request.headers["authorization"]
        == "Basic " + base64.b64encode(f"{USER}:{SECRET}".encode()).decode()
    )
    assert result == {
        "path": SOURCE,
        "etag": '"new"',
        "bytes": len(NEW),
        "updated": True,
        "verified": True,
        "attempts": 1,
    }


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/Docs/file.pdf", "/Docs/no-extension"])
async def test_edit_refuses_non_text_extensions_before_writing(
    clients: NcClients, path: str
) -> None:
    with respx.mock as mock:
        with pytest.raises(ToolError):
            await files_tools.edit(clients, path, "replacement")
        assert not mock.calls


@pytest.mark.anyio
async def test_edit_refuses_more_than_32_kib_before_network(clients: NcClients) -> None:
    with respx.mock as mock:
        with pytest.raises(ToolError, match="above"):
            await files_tools.edit(clients, SOURCE, "é" * (files_tools.HARD_EDIT_BYTES // 2 + 1))
        assert not mock.calls


@pytest.mark.anyio
async def test_edit_conflict_is_not_retried(clients: NcClients) -> None:
    changed = b"somebody else\n"
    with respx.mock as mock:
        mock.route(method="PROPFIND", url=SOURCE_URL).mock(
            side_effect=[
                httpx.Response(207, text=propfind(SOURCE, size=len(OLD), etag='"old"')),
                httpx.Response(207, text=propfind(SOURCE, size=len(changed), etag='"other"')),
            ]
        )
        put = mock.route(method="PUT", url=SOURCE_URL).mock(return_value=httpx.Response(412))
        with pytest.raises(ConflictError, match="changed"):
            await files_tools.edit(clients, SOURCE, NEW.decode())
    assert put.call_count == 1


@pytest.mark.anyio
async def test_edit_retries_once_only_after_transport_failure_proves_no_change(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="PROPFIND", url=SOURCE_URL).mock(
            side_effect=[
                httpx.Response(207, text=propfind(SOURCE, size=len(OLD), etag='"old"')),
                httpx.Response(207, text=propfind(SOURCE, size=len(OLD), etag='"old"')),
                httpx.Response(207, text=propfind(SOURCE, size=len(NEW), etag='"new"')),
            ]
        )
        put = mock.route(method="PUT", url=SOURCE_URL).mock(
            side_effect=[httpx.ReadTimeout("ambiguous"), httpx.Response(204)]
        )
        mock.route(method="GET", url=SOURCE_URL).mock(
            return_value=httpx.Response(
                206,
                content=NEW,
                headers={"Content-Range": f"bytes 0-{len(NEW) - 1}/{len(NEW)}"},
            )
        )
        result = await files_tools.edit(clients, SOURCE, NEW.decode())

    assert put.call_count == 2
    assert result["verified"] is True
    assert result["attempts"] == 2


@pytest.mark.anyio
async def test_move_is_revision_bound_collision_free_and_verified(clients: NcClients) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="PROPFIND", url=SOURCE_URL).mock(
            side_effect=[
                httpx.Response(207, text=propfind(SOURCE, size=len(OLD), etag='"old"')),
                httpx.Response(404),
            ]
        )
        mock.route(method="PROPFIND", url=DESTINATION_URL).mock(
            return_value=httpx.Response(
                207, text=propfind(DESTINATION, size=len(OLD), etag='"moved"')
            )
        )
        move = mock.route(method="MOVE", url=SOURCE_URL).mock(return_value=httpx.Response(201))
        result = await files_tools.move(clients, SOURCE, DESTINATION)

    request = move.calls[0].request
    assert request.headers["destination"] == DESTINATION_URL
    assert request.headers["overwrite"] == "F"
    assert request.headers["if-match"] == '"old"'
    assert result == {
        "source_path": SOURCE,
        "path": DESTINATION,
        "etag": '"moved"',
        "moved": True,
        "verified": True,
        "attempts": 1,
    }


@pytest.mark.anyio
async def test_move_refuses_folder_source_before_move(clients: NcClients) -> None:
    with respx.mock as mock:
        mock.route(method="PROPFIND", url=SOURCE_URL).mock(
            return_value=httpx.Response(
                207, text=propfind(SOURCE, size=0, etag='"folder"', folder=True)
            )
        )
        with pytest.raises(ToolError, match="folder"):
            await files_tools.move(clients, SOURCE, DESTINATION)
        assert [call.request.method for call in mock.calls] == ["PROPFIND"]


@pytest.mark.anyio
async def test_move_collision_is_refused_without_retry(clients: NcClients) -> None:
    with respx.mock as mock:
        mock.route(method="PROPFIND", url=SOURCE_URL).mock(
            side_effect=[
                httpx.Response(207, text=propfind(SOURCE, size=len(OLD), etag='"old"')),
                httpx.Response(207, text=propfind(SOURCE, size=len(OLD), etag='"old"')),
            ]
        )
        mock.route(method="PROPFIND", url=DESTINATION_URL).mock(
            return_value=httpx.Response(
                207,
                text=propfind(DESTINATION, size=7, etag='"occupied"', fileid="9999"),
            )
        )
        move = mock.route(method="MOVE", url=SOURCE_URL).mock(return_value=httpx.Response(412))
        with pytest.raises(ConflictError, match="already exists"):
            await files_tools.move(clients, SOURCE, DESTINATION)
    assert move.call_count == 1


@pytest.mark.anyio
async def test_move_retries_once_only_after_inspection_proves_no_change(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="PROPFIND", url=SOURCE_URL).mock(
            side_effect=[
                httpx.Response(207, text=propfind(SOURCE, size=len(OLD), etag='"old"')),
                httpx.Response(207, text=propfind(SOURCE, size=len(OLD), etag='"old"')),
                httpx.Response(404),
            ]
        )
        mock.route(method="PROPFIND", url=DESTINATION_URL).mock(
            side_effect=[
                httpx.Response(404),
                httpx.Response(207, text=propfind(DESTINATION, size=len(OLD), etag='"moved"')),
            ]
        )
        move = mock.route(method="MOVE", url=SOURCE_URL).mock(
            side_effect=[httpx.ReadTimeout("ambiguous"), httpx.Response(201)]
        )
        result = await files_tools.move(clients, SOURCE, DESTINATION)

    assert move.call_count == 2
    assert result["verified"] is True
    assert result["attempts"] == 2
