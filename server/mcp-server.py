from __future__ import annotations

import html
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from typing import Annotated, Any, Literal

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp_types import ToolAnnotations
from pydantic import AnyHttpUrl, BaseModel, Field
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response


API_BASE = os.environ.get("SUCCHIA_API_BASE", "https://succhia-bobobei.onrender.com").rstrip("/")
API_TOKEN = os.environ.get("SUCCHIA_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("SUCCHIA_MCP_PUBLIC_URL", "https://bobobei-mcp-kara.onrender.com").rstrip("/")
LOGIN_USER = os.environ.get("SUCCHIA_MCP_USER", "kara").strip()
LOGIN_PASSWORD = os.environ.get("SUCCHIA_MCP_PASSWORD", "").strip()
PORT = int(os.environ.get("PORT", "8000"))

if not API_TOKEN:
    raise RuntimeError("SUCCHIA_TOKEN is required")
if not LOGIN_PASSWORD:
    raise RuntimeError("SUCCHIA_MCP_PASSWORD is required")


class PatternState(BaseModel):
    type: Literal["wave", "pulse", "climb"]
    high: int = Field(ge=0, le=100)
    low: int = Field(ge=0, le=100)
    period: int = Field(ge=500, le=60000)
    duration: int | None = Field(default=None, ge=3, le=3600)


class ChannelState(BaseModel):
    intensity: int = Field(ge=0, le=100)
    mode: int = Field(ge=1, le=4)
    pattern: PatternState | None = None


class BobobeiStatus(BaseModel):
    connected: bool
    seconds_since_phone_poll: float | None = None
    suck: ChannelState
    vibe: ChannelState
    ems: ChannelState
    message: str


class ActionResult(BaseModel):
    ok: bool
    action: str
    status: BobobeiStatus


def _api(path: str, payload: dict | None = None) -> dict:
    data = None
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}
    method = "GET"
    if payload is not None:
        method = "POST"
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(API_BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise ToolError(f"啵啵贝服务返回 HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise ToolError(f"暂时联系不上啵啵贝服务：{type(exc).__name__}") from exc


def _status_from(raw: dict) -> BobobeiStatus:
    state = raw.get("state") or {}
    patterns = state.get("patterns") or {}
    connected = bool(raw.get("page_listening"))
    return BobobeiStatus(
        connected=connected,
        seconds_since_phone_poll=raw.get("page_last_poll_sec_ago"),
        suck=ChannelState(intensity=int(state.get("suck_intensity", 0)), mode=int(state.get("suck_mode", 1)), pattern=patterns.get("suck")),
        vibe=ChannelState(intensity=int(state.get("vibe_intensity", 0)), mode=int(state.get("vibe_mode", 1)), pattern=patterns.get("vibe")),
        ems=ChannelState(intensity=int(state.get("ems_intensity", 0)), mode=int(state.get("ems_mode", 1)), pattern=patterns.get("ems")),
        message="手机控制页已连接，可以接收指令。" if connected else "手机控制页当前没有监听；请在安卓 Chrome 中打开控制页并连接 SOSEXY。",
    )


def _get_status() -> BobobeiStatus:
    return _status_from(_api("/status"))


def _require_phone() -> None:
    status = _get_status()
    if not status.connected:
        raise ToolError(status.message)


class KaraOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self) -> None:
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.auth_codes: dict[str, AuthorizationCode] = {}
        self.tokens: dict[str, AccessToken] = {}
        self.states: dict[str, dict[str, str | None]] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            raise ValueError("client_id is required")
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        state = params.state or secrets.token_urlsafe(24)
        self.states[state] = {
            "redirect_uri": str(params.redirect_uri),
            "code_challenge": params.code_challenge,
            "redirect_uri_provided_explicitly": str(params.redirect_uri_provided_explicitly),
            "client_id": client.client_id,
            "resource": params.resource,
        }
        return f"{PUBLIC_URL}/login?state={state}"

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str) -> AuthorizationCode | None:
        return self.auth_codes.get(authorization_code)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if authorization_code.code not in self.auth_codes or not client.client_id:
            raise ValueError("invalid authorization code")
        token = "bb_" + secrets.token_urlsafe(36)
        self.tokens[token] = AccessToken(
            token=token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(time.time()) + 60 * 60 * 24 * 30,
            resource=authorization_code.resource,
            subject=authorization_code.subject,
        )
        del self.auth_codes[authorization_code.code]
        return OAuthToken(access_token=token, token_type="Bearer", expires_in=60 * 60 * 24 * 30, scope=" ".join(authorization_code.scopes))

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = self.tokens.get(token)
        if access_token and (not access_token.expires_at or access_token.expires_at >= time.time()):
            return access_token
        self.tokens.pop(token, None)
        return None

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        raise NotImplementedError("refresh tokens are not supported")

    async def revoke_token(self, token: str, token_type_hint: str | None = None) -> None:  # type: ignore[override]
        self.tokens.pop(token, None)

    def issue_code(self, username: str, password: str, state: str) -> str:
        data = self.states.get(state)
        if not data:
            raise HTTPException(400, "登录链接已过期，请返回 ChatGPT 重试。")
        if not secrets.compare_digest(username, LOGIN_USER) or not secrets.compare_digest(password, LOGIN_PASSWORD):
            raise HTTPException(401, "用户名或密码不对。")
        redirect_uri = data["redirect_uri"]
        client_id = data["client_id"]
        challenge = data["code_challenge"]
        assert redirect_uri and client_id and challenge
        code = "bbc_" + secrets.token_urlsafe(24)
        self.auth_codes[code] = AuthorizationCode(
            code=code,
            client_id=client_id,
            redirect_uri=AnyHttpUrl(redirect_uri),
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"] == "True",
            expires_at=time.time() + 300,
            scopes=["bobobei"],
            code_challenge=challenge,
            resource=data.get("resource"),
            subject=username,
        )
        del self.states[state]
        return construct_redirect_uri(redirect_uri, code=code, state=state)


oauth = KaraOAuthProvider()
mcp = MCPServer(
    "bobobei",
    version="1.1.0",
    instructions=(
        "Controls Kara's paired 啵啵贝 through her Android Chrome bridge. Read status before increasing intensity. "
        "Use conservative values unless Kara explicitly requests otherwise. Never guess consent or continue after Kara asks to stop."
    ),
    auth_server_provider=oauth,
    auth=AuthSettings(
        issuer_url=AnyHttpUrl(PUBLIC_URL),
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=["bobobei"], default_scopes=["bobobei"]),
        required_scopes=["bobobei"],
        resource_server_url=None,
    ),
)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "bobobei-mcp", "version": "1.1.0", "auth": "oauth"})


@mcp.custom_route("/login", methods=["GET"])
async def login_page(request: Request) -> HTMLResponse:
    state = request.query_params.get("state")
    if not state or state not in oauth.states:
        raise HTTPException(400, "登录链接已过期，请返回 ChatGPT 重试。")
    safe_state = html.escape(state, quote=True)
    safe_user = html.escape(LOGIN_USER, quote=True)
    return HTMLResponse(f"""<!doctype html><html lang='zh-CN'><meta name='viewport' content='width=device-width,initial-scale=1'><title>连接啵啵贝</title>
<style>body{{font-family:system-ui;background:#17172b;color:#fff;display:grid;min-height:100vh;place-items:center;margin:0}}main{{width:min(88vw,420px);padding:28px;border:1px solid #5a3e83;border-radius:24px;background:#20203a}}h1{{margin-top:0}}label{{display:block;margin:16px 0 6px}}input{{box-sizing:border-box;width:100%;padding:13px;border-radius:12px;border:1px solid #777;background:#111126;color:#fff;font-size:16px}}button{{width:100%;margin-top:22px;padding:14px;border:0;border-radius:999px;background:#8b5cf6;color:#fff;font-size:17px;font-weight:700}}</style>
<main><h1>连接啵啵贝 ♡</h1><p>仅授权 Kara 的 ChatGPT 控制私人设备。</p><form method='post' action='/login/callback'><input type='hidden' name='state' value='{safe_state}'><label>用户名</label><input name='username' value='{safe_user}' autocomplete='username' required><label>密码</label><input name='password' type='password' autocomplete='current-password' required><button type='submit'>授权连接</button></form></main></html>""")


@mcp.custom_route("/login/callback", methods=["POST"])
async def login_callback(request: Request) -> Response:
    form = await request.form()
    username, password, state = form.get("username"), form.get("password"), form.get("state")
    if not all(isinstance(value, str) for value in (username, password, state)):
        raise HTTPException(400, "缺少登录信息。")
    return RedirectResponse(oauth.issue_code(username, password, state), status_code=302)


@mcp.tool(title="读取啵啵贝状态", description="查看安卓控制页是否在线，以及三个通道当前强度、模式和波形。", annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False), structured_output=True)
def get_bobobei_status() -> BobobeiStatus:
    return _get_status()


@mcp.tool(title="设置啵啵贝单个通道", description="设置吮吸、震动或微电流通道的固定强度；intensity 为 0-100，mode 可选 1-4。", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False), structured_output=True)
def set_bobobei_channel(
    channel: Annotated[Literal["suck", "vibe", "ems"], Field(description="suck=吮吸，vibe=震动，ems=微电流")],
    intensity: Annotated[int, Field(ge=0, le=100)],
    mode: Annotated[int | None, Field(ge=1, le=4)] = None,
) -> ActionResult:
    _require_phone()
    payload: dict[str, Any] = {f"{channel}_intensity": intensity, "patterns": {channel: None}}
    if mode is not None:
        payload[f"{channel}_mode"] = mode
    _api("/set", payload)
    return ActionResult(ok=True, action=f"set_{channel}", status=_get_status())


@mcp.tool(title="启动啵啵贝波形", description="为一个通道启动限时波形；pattern 可选 wave、pulse、climb，必须指定 3-600 秒自动停止。", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False), structured_output=True)
def start_bobobei_pattern(
    channel: Annotated[Literal["suck", "vibe", "ems"], Field(description="suck=吮吸，vibe=震动，ems=微电流")],
    pattern: Literal["wave", "pulse", "climb"],
    high: Annotated[int, Field(ge=1, le=100)],
    low: Annotated[int, Field(ge=0, le=99)],
    period_ms: Annotated[int, Field(ge=500, le=60000)],
    duration_seconds: Annotated[int, Field(ge=3, le=600)],
) -> ActionResult:
    _require_phone()
    if low > high:
        raise ToolError("low 不能高于 high。")
    spec = {"type": pattern, "high": high, "low": low, "period": period_ms, "duration": duration_seconds}
    _api("/set", {f"{channel}_intensity": high, "patterns": {channel: spec}})
    return ActionResult(ok=True, action=f"pattern_{channel}_{pattern}", status=_get_status())


@mcp.tool(title="全部停止啵啵贝", description="立即把吮吸、震动、微电流全部归零并关闭所有波形。", annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False), structured_output=True)
def stop_bobobei() -> ActionResult:
    _api("/set", {"suck_intensity": 0, "vibe_intensity": 0, "ems_intensity": 0, "patterns": {"suck": None, "vibe": None, "ems": None}})
    return ActionResult(ok=True, action="stop_all", status=_get_status())


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=PORT)
