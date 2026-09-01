from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.web import render, view_context
from app.services.templates import parse_dimension_form
from auth import require_admin
from models import AiProviderCredential, RenderTask, Template, User
from render_engine.dimension_registry import default_template_config, normalize_config, seed_dimensions


router = APIRouter()


@router.get("/admin", response_class=HTMLResponse)
async def admin_home(request: Request):
    await require_admin(request)
    template_list = await Template.all()
    return render(request, "admin.html", **await view_context(request, templates=template_list))


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request):
    await require_admin(request)
    users = await User.all().order_by("-is_admin", "username")
    rows = []
    for user in users:
        task_count = await RenderTask.filter(user=user).count()
        provider_count = await AiProviderCredential.filter(user=user).count()
        rows.append(
            {
                "id": user.id,
                "username": user.username,
                "is_admin": user.is_admin,
                "created_at": user.created_at,
                "task_count": task_count,
                "provider_count": provider_count,
            }
        )
    return render(request, "admin_users.html", **await view_context(request, users=rows))


@router.get("/admin/template/new", response_class=HTMLResponse)
async def admin_template_new(request: Request):
    await require_admin(request)
    tpl = Template(name="", category="general", description="", config_schema=default_template_config())
    return render(request, "admin_template_edit.html", **await view_context(request, template=tpl, config=normalize_config(tpl.config_schema)))


@router.get("/admin/template/{tid}/edit", response_class=HTMLResponse)
async def admin_template_edit(request: Request, tid: int):
    await require_admin(request)
    tpl = await Template.get(id=tid)
    return render(request, "admin_template_edit.html", **await view_context(request, template=tpl, config=normalize_config(tpl.config_schema)))


@router.post("/admin/template/save")
async def admin_template_save(
    request: Request,
    template_id: int = Form(0),
    name: str = Form(...),
    category: str = Form(...),
    description: str = Form(""),
    sort_order: int = Form(0),
):
    await require_admin(request)
    config = parse_dimension_form(dict(await request.form()))
    data = {"name": name, "category": category, "description": description, "sort_order": sort_order, "config_schema": config}
    if template_id:
        tpl = await Template.get(id=template_id)
        await tpl.update_from_dict(data).save()
    else:
        tpl = await Template.create(**data)
    return RedirectResponse(f"/admin/template/{tpl.id}/edit", status_code=303)


@router.post("/admin/template/{tid}/toggle")
async def admin_template_toggle(request: Request, tid: int):
    await require_admin(request)
    tpl = await Template.get(id=tid)
    await tpl.update_from_dict({"is_active": not tpl.is_active}).save()
    return RedirectResponse("/admin", status_code=303)


@router.get("/admin/dimensions", response_class=HTMLResponse)
async def admin_dimensions(request: Request):
    await require_admin(request)
    return render(request, "admin_dimensions.html", **await view_context(request))


@router.post("/admin/dimensions/reseed")
async def admin_dimensions_reseed(request: Request):
    await require_admin(request)
    await seed_dimensions()
    return RedirectResponse("/admin/dimensions", status_code=303)
