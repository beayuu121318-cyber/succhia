from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


API_BASE = os.environ.get("SUCCHIA_API_BASE", "https://succhia-bobobei.onrender.com").rstrip("/")
API_TOKEN = os.environ.get("SUCCHIA_TOKEN", "").strip()
MCP_SECRET = os.environ.get("SUCCHIA_MCP_SECRET", "").strip()
PORT = int(os.environ.get("PORT", "8000"))

if not API_TOKEN:
    raise RuntimeError("SUCCHIA_TOKEN is required")
if not MCP_SECRET:
    raise RuntimeError("SUCCHIA_MCP_SECRET is required")

MCP_PATH = f"/mcp/{MCP_SECRET}"


class PatternState(BaseModel):
    type: Literal["wave", "pulse", "climb"]
    high: int = Field(ge=0, le=100)
    low: int = Field(ge=0, le=100)
    period: int = Field(ge=500, le=60000, description="Wave period in milliseconds.")
    duration: int | None = Field(default=None, ge=3, le=3600, description="Automatic stop time in seconds.")


class ChannelState(BaseModel):
    intensity: int = Field(ge=0, le=100)
    mode: int = Field(ge=1, le=4)
    pattern: PatternState | None = None


class BobobeiStatus(BaseModel):
    connected: bool = Field(description="Whether the Android control page is actively listening and connected.")
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
    url = API_BASE + path
    data = None
    headers = {"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"}
    method = "GET"
    if payload is not None:
        method = "POST"
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
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
        suck=ChannelState(
            intensity=int(state.get("suck_intensity", 0)),
            mode=int(state.get("suck_mode", 1)),
            pattern=patterns.get("suck"),
        ),
        vibe=ChannelState(
            intensity=int(state.get("vibe_intensity", 0)),
            mode=int(state.get("vibe_mode", 1)),
            pattern=patterns.get("vibe"),
        ),
        ems=ChannelState(
            intensity=int(state.get("ems_intensity", 0)),
            mode=int(state.get("ems_mode", 1)),
            pattern=patterns.get("ems"),
        ),
        message="手机控制页已连接，可以接收指令。" if connected else "手机控制页当前没有监听；请在安卓 Chrome 中打开控制页并连接 SOSEXY。",
    )


def _get_status() -> BobobeiStatus:
    return _status_from(_api("/status"))


def _require_phone() -> BobobeiStatus:
    status = _get_status()
    if not status.connected:
        raise ToolError(status.message)
    return status


mcp = MCPServer(
    "bobobei",
    version="1.0.0",
    instructions=(
        "Controls Kara's paired 啵啵贝 through her Android Chrome bridge. "
        "Read status before increasing intensity. Use conservative values unless Kara explicitly requests otherwise. "
        "Never guess consent or continue after Kara asks to stop; stop_bobobei is always available."
    ),
)


@mcp.tool(
    title="读取啵啵贝状态",
    description="查看安卓控制页是否在线，以及吮吸、震动、微电流三个通道当前的强度、模式和波形。",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=True,
)
def get_bobobei_status() -> BobobeiStatus:
    return _get_status()


@mcp.tool(
    title="设置啵啵贝单个通道",
    description=(
        "在手机控制页已连接时，设置吮吸、震动或微电流其中一个通道的固定强度。"
        "intensity 为 0-100；mode 可选 1-4。调用会关闭该通道正在运行的波形。"
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=True,
)
def set_bobobei_channel(
    channel: Annotated[Literal["suck", "vibe", "ems"], Field(description="suck=吮吸，vibe=震动，ems=微电流")],
    intensity: Annotated[int, Field(ge=0, le=100, description="目标强度，0 表示关闭该通道。")],
    mode: Annotated[int | None, Field(ge=1, le=4, description="可选模式编号 1-4。")] = None,
) -> ActionResult:
    _require_phone()
    payload: dict = {
        f"{channel}_intensity": intensity,
        "patterns": {channel: None},
    }
    if mode is not None:
        payload[f"{channel}_mode"] = mode
    _api("/set", payload)
    return ActionResult(ok=True, action=f"set_{channel}", status=_get_status())


@mcp.tool(
    title="启动啵啵贝波形",
    description=(
        "在手机控制页已连接时，为一个通道启动限时波形。"
        "pattern 可选 wave、pulse、climb；必须指定 3-600 秒的自动停止时间。"
    ),
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    structured_output=True,
)
def start_bobobei_pattern(
    channel: Annotated[Literal["suck", "vibe", "ems"], Field(description="suck=吮吸，vibe=震动，ems=微电流")],
    pattern: Annotated[Literal["wave", "pulse", "climb"], Field(description="波浪、脉冲或攀升。")],
    high: Annotated[int, Field(ge=1, le=100, description="波形高点强度。")],
    low: Annotated[int, Field(ge=0, le=99, description="波形低点强度，不得高于 high。")],
    period_ms: Annotated[int, Field(ge=500, le=60000, description="一个周期的毫秒数。")],
    duration_seconds: Annotated[int, Field(ge=3, le=600, description="自动停止时间，最多十分钟。")],
) -> ActionResult:
    _require_phone()
    if low > high:
        raise ToolError("low 不能高于 high。")
    spec = {
        "type": pattern,
        "high": high,
        "low": low,
        "period": period_ms,
        "duration": duration_seconds,
    }
    _api("/set", {f"{channel}_intensity": high, "patterns": {channel: spec}})
    return ActionResult(ok=True, action=f"pattern_{channel}_{pattern}", status=_get_status())


@mcp.tool(
    title="全部停止啵啵贝",
    description="立即把吮吸、震动、微电流全部归零，并关闭所有波形。手机暂时离线时也可以调用。",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=True,
)
def stop_bobobei() -> ActionResult:
    _api(
        "/set",
        {
            "suck_intensity": 0,
            "vibe_intensity": 0,
            "ems_intensity": 0,
            "patterns": {"suck": None, "vibe": None, "ems": None},
        },
    )
    return ActionResult(ok=True, action="stop_all", status=_get_status())


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "bobobei-mcp", "version": "1.0.0"})


mcp_http = mcp.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
)


@asynccontextmanager
async def lifespan(_: Starlette):
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Mount(MCP_PATH, app=mcp_http),
    ],
    lifespan=lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
