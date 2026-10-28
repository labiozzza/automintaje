"""Loopback-only FastAPI routes for Motion Graphics Plus."""

from __future__ import annotations

import asyncio
import json
import ipaddress
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from .models import ApproveInput, CreateProjectInput, DesignInput, MessageInput, MirrorInput, PreviewInput, RenderInput, ScenePatchInput
from .service import MotionError


router = APIRouter(prefix="/api/motion", tags=["motion"])


def _require_loopback(request: Request) -> None:
    host = request.client.host if request.client else ""
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise HTTPException(403, "Motion Director доступен только локально")
    except ValueError as exc:
        raise HTTPException(403, "Motion Director доступен только локально") from exc


def _service(request: Request):
    value = getattr(request.app.state, "motion_service", None)
    if value is None:
        raise HTTPException(503, "Motion Graphics Plus не инициализирован")
    return value


def _translate(exc: MotionError) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


@router.get("/capabilities")
async def motion_capabilities(request: Request):
    _require_loopback(request)
    return await asyncio.to_thread(_service(request).capabilities)


@router.get("/models")
async def motion_models(request: Request):
    _require_loopback(request)
    return await asyncio.to_thread(_service(request).models)


@router.get("/projects")
async def motion_projects(request: Request, source_job_id: Optional[str] = None):
    _require_loopback(request)
    return await asyncio.to_thread(_service(request).list_projects, source_job_id)


@router.post("/projects", status_code=201)
async def motion_create_project(payload: CreateProjectInput, request: Request):
    _require_loopback(request)
    try:
        return await asyncio.to_thread(_service(request).create_project, payload)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.get("/projects/{project_id}")
async def motion_get_project(project_id: str, request: Request):
    _require_loopback(request)
    try:
        project = await asyncio.to_thread(_service(request).get_project, project_id)
    except MotionError as exc:
        raise _translate(exc) from exc
    if project is None:
        raise HTTPException(404, "Motion-проект не найден")
    return project


@router.get("/projects/{project_id}/source")
async def motion_source(project_id: str, request: Request):
    _require_loopback(request)
    service = _service(request)
    try:
        path = service.source_file(project_id)
    except MotionError as exc:
        raise _translate(exc) from exc
    if service.video_response:
        return service.video_response(path, request, "video/mp4")
    return FileResponse(str(path), media_type="video/mp4", content_disposition_type="inline")


@router.get("/projects/{project_id}/frames/{filename}")
async def motion_frame(project_id: str, filename: str, request: Request):
    _require_loopback(request)
    try:
        path = _service(request).frame_file(project_id, filename)
    except MotionError as exc:
        raise _translate(exc) from exc
    return FileResponse(str(path), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/projects/{project_id}/design", status_code=202)
async def motion_design(project_id: str, payload: DesignInput, request: Request):
    _require_loopback(request)
    try:
        return await _service(request).start_design(project_id, payload)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.post("/projects/{project_id}/messages", status_code=202)
async def motion_message(project_id: str, payload: MessageInput, request: Request):
    _require_loopback(request)
    try:
        return await _service(request).start_revision(project_id, payload)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.patch("/projects/{project_id}/scenes/{scene_id}")
async def motion_patch_scene(project_id: str, scene_id: str, payload: ScenePatchInput, request: Request):
    _require_loopback(request)
    try:
        return await asyncio.to_thread(_service(request).patch_scene, project_id, scene_id, payload)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.post("/projects/{project_id}/approve")
async def motion_approve(project_id: str, payload: ApproveInput, request: Request):
    _require_loopback(request)
    try:
        return await asyncio.to_thread(_service(request).approve, project_id, payload)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.post("/projects/{project_id}/preview", status_code=202)
async def motion_preview(project_id: str, payload: PreviewInput, request: Request):
    _require_loopback(request)
    try:
        return await _service(request).start_preview(project_id, payload)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.post("/projects/{project_id}/render", status_code=202)
async def motion_render(project_id: str, payload: RenderInput, request: Request):
    _require_loopback(request)
    try:
        return await _service(request).start_render(project_id, payload)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.post("/projects/{project_id}/cancel", status_code=202)
async def motion_cancel(project_id: str, request: Request):
    _require_loopback(request)
    try:
        return await _service(request).cancel(project_id)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.get("/projects/{project_id}/storyboard")
async def motion_storyboard(project_id: str, request: Request, revision: Optional[int] = None):
    _require_loopback(request)
    try:
        return await asyncio.to_thread(_service(request).storyboard, project_id, revision)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.get("/projects/{project_id}/outputs")
async def motion_outputs(project_id: str, request: Request):
    _require_loopback(request)
    try:
        return await asyncio.to_thread(_service(request).outputs, project_id)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.post("/projects/{project_id}/outputs/{output_job_id}/mirror", status_code=202)
async def motion_prepare_mirror(project_id: str, output_job_id: str, payload: MirrorInput, request: Request):
    _require_loopback(request)
    try:
        return await _service(request).start_mirror(project_id, output_job_id, payload.request_id)
    except MotionError as exc:
        raise _translate(exc) from exc


@router.get("/projects/{project_id}/preview/{revision}")
async def motion_preview_file(project_id: str, revision: int, request: Request):
    _require_loopback(request)
    service = _service(request)
    try:
        path = service.preview_file(project_id, revision)
    except MotionError as exc:
        raise _translate(exc) from exc
    if service.video_response:
        return service.video_response(path, request, "video/mp4")
    return FileResponse(str(path), media_type="video/mp4", content_disposition_type="inline")


@router.get("/projects/{project_id}/operations/{operation_id}")
async def motion_operation(project_id: str, operation_id: str, request: Request):
    _require_loopback(request)
    service = _service(request)
    operation = await asyncio.to_thread(service.get_operation, operation_id)
    if not operation or str(operation.get("project_id")) != project_id:
        raise HTTPException(404, "Motion-операция не найдена")
    return operation


@router.get("/projects/{project_id}/events")
async def motion_events(project_id: str, request: Request, operation_id: Optional[str] = None):
    _require_loopback(request)
    service = _service(request)

    async def stream():
        last_updated = ""
        while not await request.is_disconnected():
            if operation_id:
                operation = await asyncio.to_thread(service.get_operation, operation_id)
            else:
                operation = await asyncio.to_thread(service.repository.active_operation, project_id)
            if operation and str(operation.get("project_id")) != project_id:
                operation = None
            if operation and str(operation.get("updated_at") or "") != last_updated:
                last_updated = str(operation.get("updated_at") or "")
                payload = json.dumps(operation, ensure_ascii=False, separators=(",", ":"))
                yield "id: %s\nevent: motion\ndata: %s\n\n" % (last_updated, payload)
                if operation.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                    return
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
