"""Deletion is one-file-only, revision-bound, non-retrying, and honest about trash fallback."""

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
PATH = "/Docs/old.txt"
URL = f"{BASE}/remote.php/dav/files/{USER}{PATH}"


def propfind(*, collection: bool = False, etag: str = '"etag-old"') -> str:
    resource = "<d:collection/>" if collection else ""
    return f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:response>
<d:href>/remote.php/dav/files/{USER}{PATH}</d:href><d:propstat><d:prop>
<d:getcontentlength>4</d:getcontentlength><d:getcontenttype>text/plain</d:getcontenttype>
<d:getetag>{etag.replace('"', "&quot;")}</d:getetag><d:resourcetype>{resource}</d:resourcetype>
<oc:fileid>4711</oc:fileid><oc:permissions>RGDNVW</oc:permissions>
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
async def test_delete_binds_exact_revision_and_reports_trash_not_guaranteed(
    clients: NcClients,
) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.route(method="PROPFIND", url=URL).mock(
            return_value=httpx.Response(207, text=propfind())
        )
        delete = mock.route(method="DELETE", url=URL).mock(return_value=httpx.Response(204))
        result = await files_tools.delete(clients, PATH)

    assert delete.calls[0].request.headers["if-match"] == '"etag-old"'
    expected = base64.b64encode(f"{USER}:{SECRET}".encode()).decode()
    assert delete.calls[0].request.headers["authorization"] == f"Basic {expected}"
    assert result["path"] == PATH
    assert result["deleted"] is True
    assert result["trash_guaranteed"] is False
    assert "permanently" in result["note"]


@pytest.mark.anyio
async def test_changed_revision_is_not_deleted_and_never_retried(clients: NcClients) -> None:
    with respx.mock as mock:
        mock.route(method="PROPFIND", url=URL).mock(
            return_value=httpx.Response(207, text=propfind())
        )
        delete = mock.route(method="DELETE", url=URL).mock(return_value=httpx.Response(412))
        with pytest.raises(ConflictError, match="changed"):
            await files_tools.delete(clients, PATH)
    assert delete.call_count == 1


@pytest.mark.anyio
async def test_folder_delete_is_refused_before_delete(clients: NcClients) -> None:
    with respx.mock as mock:
        mock.route(method="PROPFIND", url=URL).mock(
            return_value=httpx.Response(207, text=propfind(collection=True))
        )
        with pytest.raises(ToolError, match="folder"):
            await files_tools.delete(clients, PATH)
        assert [call.request.method for call in mock.calls] == ["PROPFIND"]


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/", "../escape.txt", "/Docs/../escape.txt", "/Docs\\old.txt"])
async def test_root_and_unsafe_paths_never_reach_nextcloud(clients: NcClients, path: str) -> None:
    with respx.mock as mock:
        with pytest.raises(ToolError):
            await files_tools.delete(clients, path)
        assert not mock.calls


@pytest.mark.anyio
async def test_missing_etag_refuses_without_delete(clients: NcClients) -> None:
    with respx.mock as mock:
        mock.route(method="PROPFIND", url=URL).mock(
            return_value=httpx.Response(207, text=propfind(etag=""))
        )
        with pytest.raises(ToolError, match="no ETag"):
            await files_tools.delete(clients, PATH)
        assert [call.request.method for call in mock.calls] == ["PROPFIND"]


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403, 404, 423, 500])
async def test_delete_errors_are_sanitized_and_not_retried(clients: NcClients, status: int) -> None:
    with respx.mock as mock:
        mock.route(method="PROPFIND", url=URL).mock(
            return_value=httpx.Response(207, text=propfind())
        )
        delete = mock.route(method="DELETE", url=URL).mock(
            return_value=httpx.Response(status, text="<html>private stack</html>")
        )
        with pytest.raises(ToolError) as excinfo:
            await files_tools.delete(clients, PATH)
    assert delete.call_count == 1
    text = excinfo.value.message + excinfo.value.hint
    assert "private stack" not in text
    assert SECRET not in text
