你是 AI 漫剧的全剧策划编辑。以下输入是按原文顺序排列的分块解析结果。

请完成全局合并：
1. 合并同一人物的姓名与别名，保留稳定的人物关系和视觉识别要点。
2. 合并重复场景、道具和事件，保持原始时间线，不新增原文不存在的事实。
3. 找出主线、支线、冲突升级、高潮、结局以及需要跨集保持的连续性规则。
4. source_start/source_end 必须沿用输入中的整部原文字符偏移。
5. 分别合并人物、场景、道具变体。每个变体必须保留 base_name、variant_type、触发原因和原文证据，不能独立成为基础资产。

只返回合法 JSON 对象，不要 Markdown，不要解释。至少包含：
{
  "story_summary": "全剧摘要",
  "genre": [],
  "worldview": "世界观",
  "main_plot": "主线",
  "subplots": [],
  "timeline": [],
  "characters": [],
  "character_variants": [],
  "scenes": [],
  "scene_variants": [],
  "props": [],
  "prop_variants": [],
  "continuity_rules": []
}

分块解析结果：
{{chunk_results_json}}
