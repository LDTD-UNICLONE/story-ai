from fastapi import APIRouter

from app.api.v1.endpoints import (
    announcements,
    auth,
    conversations,
    health,
    models,
    points,
    project_asset_analysis,
    project_assets,
    project_chapter_processing,
    project_chapters,
    project_storyboards,
    projects,
    styles,
    task_records,
    uploads,
    users,
)
from app.api.v1.endpoints.admin import announcements as admin_announcements
from app.api.v1.endpoints.admin import models as admin_models
from app.api.v1.endpoints.admin import point_records as admin_point_records
from app.api.v1.endpoints.admin import recharges as admin_recharges
from app.api.v1.endpoints.admin import statistics as admin_statistics
from app.api.v1.endpoints.admin import styles as admin_styles
from app.api.v1.endpoints.admin import system_logs as admin_system_logs
from app.api.v1.endpoints.admin import task_records as admin_task_records
from app.api.v1.endpoints.admin import users as admin_users

api_router = APIRouter()
api_router.include_router(admin_announcements.router, tags=["Admin Announcements"])
api_router.include_router(admin_models.router, tags=["Admin Models"])
api_router.include_router(admin_point_records.router, tags=["Admin Point Records"])
api_router.include_router(admin_recharges.router, tags=["Admin Recharges"])
api_router.include_router(admin_statistics.router, tags=["Admin Statistics"])
api_router.include_router(admin_styles.router, tags=["Admin Styles"])
api_router.include_router(admin_system_logs.router, tags=["Admin System Logs"])
api_router.include_router(admin_task_records.router, tags=["Admin Task Records"])
api_router.include_router(admin_users.router, tags=["Admin Users"])
api_router.include_router(announcements.router, tags=["Announcements"])
api_router.include_router(auth.router, tags=["Auth"])
api_router.include_router(conversations.router, tags=["Conversations"])
api_router.include_router(models.router, tags=["Models"])
api_router.include_router(points.router, tags=["Points"])
api_router.include_router(project_asset_analysis.router, tags=["Project Asset Analysis"])
api_router.include_router(project_assets.router, tags=["Project Assets"])
api_router.include_router(project_chapter_processing.router, tags=["Project Chapter Processing"])
api_router.include_router(project_chapters.router, tags=["Project Chapters"])
api_router.include_router(project_storyboards.router, tags=["Project Storyboards"])
api_router.include_router(projects.router, tags=["Projects"])
api_router.include_router(styles.router, tags=["Styles"])
api_router.include_router(task_records.router, tags=["Task Records"])
api_router.include_router(uploads.router, tags=["Uploads"])
api_router.include_router(users.router, tags=["Users"])
api_router.include_router(health.router, tags=["Health"])
