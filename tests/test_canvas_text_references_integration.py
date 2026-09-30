# ruff: noqa: F811
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app.core.exceptions import AppException
from app.models.canvas_generation import CanvasGeneration
from app.models.project_canvas import CanvasNode
from app.models.task_record import UserTaskRecord
from app.services.projects import canvas_generations, canvases
from app.schemas.canvas_generation import CanvasGenerationSelect
from tests.test_canvas_api_improvements_integration import client, canvas_path  # noqa: F401
from tests.test_canvas_generations_integration import ctx, setup_node, submit, picture  # noqa: F401
from tests.test_project_canvases_integration import canvas_ctx, node, edge, patch  # noqa: F401
from tests.test_task_lifecycle_integration import lifecycle_db  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.asyncio,
              pytest.mark.skipif(os.getenv('RUN_DB_INTEGRATION_TESTS') != '1', reason='isolated PostgreSQL')]


@pytest.mark.parametrize('kind', ['text', 'image', 'video'])
async def test_pinned_text_expands_for_all_node_types_and_refreshes_explicitly(ctx, client, kind):
    target, _ = await setup_node(ctx, kind=kind)
    source = node('text', content={'text': 'Original description'})
    link = edge(source, target, input='text', text_source='input')
    target.content.text = f'Draw @{{{link.id}}}'
    await patch(ctx, 2, upsert_nodes=[source, target], upsert_edges=[link])
    before = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    await patch(ctx, before.revision, update_nodes=[{'id': source.id, 'content': {'text': 'Updated description'}}])
    path = canvas_path(ctx) + f'/nodes/{target.id}/reference-inputs'
    response = await client.get(path)
    assert response.status_code == 200
    item = response.json()['data']['items'][0]
    assert item['kind'] == 'text' and item['available'] and item['source_changed']
    assert item['text_snapshot']['text'] == 'Original description'
    assert (await client.get(canvas_path(ctx) + f'/nodes/{target.id}/image-inputs')).json()['data']['items'] == []
    record = await submit(ctx, target, revision=2)
    assert record['snapshot']['prompt'] == 'Draw Original description'
    assert record['snapshot']['inputs'][0]['text'] == 'Original description'
    if kind == 'video':
        assert record['snapshot']['model_extra']['generation_mode'] == 'text_to_video'
    assert 'image_urls' not in record['snapshot']['model_extra']
    saved = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    unchanged = await patch(ctx, saved.revision, upsert_edges=[link])
    assert next(n for n in unchanged.nodes if n.id == target.id).content_revision == 2
    link.refresh_text = True
    refreshed = await patch(ctx, unchanged.revision, upsert_edges=[link])
    assert refreshed.edges[0].text_snapshot.text == 'Updated description'
    assert next(n for n in refreshed.nodes if n.id == target.id).content_revision == 3
    again = await submit(ctx, target, revision=3)
    assert again['snapshot']['prompt'] == 'Draw Updated description'
    first = await ctx.db.get(CanvasGeneration, UUID(record['id']))
    assert first.snapshot['prompt'] == 'Draw Original description'


async def test_result_source_requires_selected_success_and_never_falls_back_to_prompt(ctx):
    source, _ = await setup_node(ctx, kind='text', text='Private source instruction')
    target = node('image')
    link = edge(source, target, input='text', text_source='result')
    with pytest.raises(AppException, match='尚无选中成功结果'):
        await patch(ctx, 2, upsert_nodes=[target], upsert_edges=[link])
    await ctx.db.rollback()
    record = await submit(ctx, source)
    generation = await ctx.db.get(CanvasGeneration, UUID(record['id']))
    task = await ctx.db.get(UserTaskRecord, UUID(record['task_record_id']))
    generation.result = {'text': 'Selected generated description', 'urls': [], 'media_ids': []}
    task.status = 'success'
    await ctx.db.commit()
    await canvas_generations.select_generation(ctx.db, ctx.pid, ctx.uid, ctx.cid, source.id, generation.id,
                                               CanvasGenerationSelect(expected_content_revision=1))
    saved = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    saved = await patch(ctx, saved.revision, upsert_nodes=[target], upsert_edges=[link])
    snapshot = saved.edges[0].text_snapshot
    assert snapshot.text == 'Selected generated description' and snapshot.generation_id == generation.id
    # A stale/corrupted selection cannot import another node's result during refresh.
    generation.node_id = uuid4()
    await ctx.db.commit()
    link.refresh_text = True
    with pytest.raises(AppException, match='尚无选中成功结果'):
        await patch(ctx, saved.revision, upsert_edges=[link])


async def test_unmentioned_empty_text_is_ignored_and_expansion_is_not_recursive(ctx):
    target, _ = await setup_node(ctx)
    blank = node('text')
    source = node('text', content={'text': 'Literal @{not-an-edge} and @图片1'})
    link, unused = edge(source, target, input='text', text_source='input'), edge(blank, target, input='text', text_source='input', position=1)
    target.content.text = f'Use @{{{link.id}}}'
    await patch(ctx, 2, upsert_nodes=[target, source, blank], upsert_edges=[link, unused])
    record = await submit(ctx, target, revision=2)
    assert record['snapshot']['prompt'] == 'Use Literal @{not-an-edge} and @图片1'
    assert len(record['snapshot']['inputs']) == 1
    saved = await canvases.get_canvas(ctx.db, ctx.pid, ctx.uid, ctx.cid)
    await patch(ctx, saved.revision, update_nodes=[{'id': target.id, 'content': {'text': f'Use @{{{unused.id}}}'}}])
    with pytest.raises(AppException, match='文本引用为空'):
        await submit(ctx, target, revision=3)
    assert await ctx.db.scalar(select(func.count()).select_from(UserTaskRecord)) == 1


async def test_text_and_images_keep_video_mode_and_image_binding(ctx):
    target, _ = await setup_node(ctx, kind='video')
    text_node = node('text', content={'text': 'Walk forward'})
    media = await picture(ctx)
    image_node = node('image', media_id=media.id)
    text_link = edge(text_node, target, input='text', text_source='input')
    image_link = edge(image_node, target, input='first_frame')
    last_link = edge(image_node, target, input='last_frame')
    target.content.text = f'@{{{text_link.id}}}, start from @{{{image_link.id}}}'
    await patch(ctx, 2, upsert_nodes=[target, text_node, image_node], upsert_edges=[text_link, image_link, last_link])
    record = await submit(ctx, target, revision=2)
    assert record['snapshot']['model_extra']['generation_mode'] == 'first_last_frame'
    assert record['snapshot']['model_extra']['first_frame_url'] == media.upload['url']
    assert record['snapshot']['prompt'] == 'Walk forward, start from 首帧图片'


async def test_text_expansion_limit_and_deleted_reference_block_submission(ctx):
    target, _ = await setup_node(ctx, kind='text')
    source = node('text', content={'text': 'A' * 6000})
    link = edge(source, target, input='text', text_source='input')
    target.content.text = f'@{{{link.id}}}@{{{link.id}}}'
    await patch(ctx, 2, upsert_nodes=[target, source], upsert_edges=[link])
    with pytest.raises(AppException, match='展开引用后的提示词'):
        await submit(ctx, target, revision=2)
    await ctx.db.rollback()
    await patch(ctx, 3, delete_node_ids=[source.id])
    with pytest.raises(AppException, match='引用已失效'):
        await submit(ctx, target, revision=3)
    assert await ctx.db.scalar(select(func.count()).select_from(UserTaskRecord)) == 0


async def test_text_edge_rejects_wrong_kind_and_cycle_atomically(ctx):
    target, _ = await setup_node(ctx)
    source = node('text', content={'text': 'Reference'})
    wrong = edge(target, source, input='text', text_source='input')
    with pytest.raises(AppException, match='来源必须是文本节点'):
        await patch(ctx, 2, upsert_nodes=[source], upsert_edges=[wrong])
    await ctx.db.rollback()
    other = node('text')
    with pytest.raises(AppException, match='循环'):
        await patch(ctx, 2, upsert_nodes=[source, other], upsert_edges=[
            edge(source, other, input='text', text_source='input'),
            edge(other, source, input='text', text_source='input'),
        ])
    await ctx.db.commit()
    assert await ctx.db.scalar(select(func.count()).select_from(CanvasNode)) == 1


@pytest.mark.parametrize('prompt', ['Use @文本1', 'Use @{unclosed'])
async def test_display_number_and_malformed_tag_are_rejected(ctx, prompt):
    target, _ = await setup_node(ctx, kind='text', text=prompt)
    with pytest.raises(AppException):
        await submit(ctx, target)
    assert await ctx.db.scalar(select(func.count()).select_from(UserTaskRecord)) == 0
