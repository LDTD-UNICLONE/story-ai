from fastapi import APIRouter

from app.api.v1.endpoints import (
    agent_batch_productions,
    agent_core_assets,
    agent_pilot_productions,
    agent_production_controls,
    agent_productions,
    agent_reviews,
    agent_script_packages,
    agent_storyboards,
    agent_story_bibles,
    agent_workflow_steps,
    announcements,
    auth,
    conversations,
    health,
    materials,
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
    works,
)
from app.api.v1.endpoints.admin import announcements as admin_announcements
from app.api.v1.endpoints.admin import materials as admin_materials
from app.api.v1.endpoints.admin import models as admin_models
from app.api.v1.endpoints.admin import point_records as admin_point_records
from app.api.v1.endpoints.admin import recharges as admin_recharges
from app.api.v1.endpoints.admin import statistics as admin_statistics
from app.api.v1.endpoints.admin import styles as admin_styles
from app.api.v1.endpoints.admin import system_logs as admin_system_logs
from app.api.v1.endpoints.admin import task_records as admin_task_records
from app.api.v1.endpoints.admin import users as admin_users
from app.api.v1.endpoints.admin import works as admin_works

api_router = APIRouter()
api_router.include_router(admin_announcements.router, tags=["Admin Announcements"])
api_router.include_router(admin_materials.router, tags=["Admin Materials"])
api_router.include_router(admin_models.router, tags=["Admin Models"])
api_router.include_router(admin_point_records.router, tags=["Admin Point Records"])
api_router.include_router(admin_recharges.router, tags=["Admin Recharges"])
api_router.include_router(admin_statistics.router, tags=["Admin Statistics"])
api_router.include_router(admin_styles.router, tags=["Admin Styles"])
api_router.include_router(admin_system_logs.router, tags=["Admin System Logs"])
api_router.include_router(admin_task_records.router, tags=["Admin Task Records"])
api_router.include_router(admin_users.router, tags=["Admin Users"])
api_router.include_router(admin_works.router, tags=["Admin Works"])
api_router.include_router(agent_batch_productions.router, tags=["Agent Batch Productions"])
api_router.include_router(agent_core_assets.router, tags=["Agent Core Assets"])
api_router.include_router(agent_pilot_productions.router, tags=["Agent Pilot Productions"])
api_router.include_router(agent_production_controls.router, tags=["Agent Production Controls"])
api_router.include_router(agent_productions.router, tags=["Agent Productions"])
api_router.include_router(agent_reviews.router, tags=["Agent Reviews"])
api_router.include_router(agent_script_packages.router, tags=["Agent Script Packages"])
api_router.include_router(agent_storyboards.router, tags=["Agent Storyboards"])
api_router.include_router(agent_story_bibles.router, tags=["Agent Story Bibles"])
api_router.include_router(agent_workflow_steps.router, tags=["Agent Workflow"])
api_router.include_router(announcements.router, tags=["Announcements"])
api_router.include_router(auth.router, tags=["Auth"])
api_router.include_router(conversations.router, tags=["Conversations"])
api_router.include_router(materials.router, tags=["Materials"])
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
api_router.include_router(works.router, tags=["Works"])
api_router.include_router(health.router, tags=["Health"])
