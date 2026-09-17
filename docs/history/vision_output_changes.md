# 本次修改来龙去脉与结果

> 历史变更记录（MinerU 4 升级前）。以下“现状”、测试数量和回退行为指当时版本。当前视觉调用异常会使同步请求或异步任务失败，不再用 base_text 降级；现行合同见[代理说明](../../AGENTS.md)。

## 背景与触发
在 MinerU 图片识别流程中，视觉模型会基于上下文提示进行输出。现网反馈集中在三个点：

1. 图像识别结果前缀固定出现 `Image Description:`，实际希望输出直接为内容本身。
2. `chunk_type=true` 时希望标记图片识别结果为 `type="image"`，而不是沿用标题/正文标记逻辑。
3. `return_txt=true` 的纯文本输出中出现了 `[Page N]`、`[ChunkType=Body/Title]` 等提示标记（这些标记原本仅用于提示 LLM 理解上下文，不应进入最终文本）。

## 现状梳理
- `/mineru_with_images` 与两段式 `two_stage` 视觉合并逻辑，会把视觉结果拼接成 `Image Description: ...`。
- 视觉上下文中会插入 `[Page N]` 与 `[ChunkType=...]`，且部分模型会把这些标记回传到输出里。
- `chunk_type` 仅支持 `title/header/footer`，图片识别结果无单独类型标记。

## 修改内容

### 1) 去除 “Image Description:” 前缀
- 位置：
  - `src/services/mineru_with_images_service.py`
  - `src/services/two_stage_pipeline.py`
- 修改：
  - 合并逻辑中去掉 `Image Description:`，改为直接拼接视觉输出。
  - 视觉输出为空时仍保持原有降级行为（使用 base_text 或空）。

### 2) 增加图片识别结果的 `type="image"`
- 位置：
  - `src/services/mineru_with_images_service.py`
  - `src/services/two_stage_pipeline.py`
- 修改：
  - 当 `chunk_type=true` 时，图片识别的 chunk 统一标记 `type="image"`。
  - 标题/页眉/页脚仍按原规则打标，图片不再被误标为 `title`。

### 3) 清理视觉输出中的 Page/ChunkType 标记
- 位置：
  - `src/utils/text_output.py`
  - `src/services/mineru_with_images_service.py`
  - `src/services/two_stage_pipeline.py`
- 修改：
  - 新增 `sanitize_vision_text()`，清除：
    - `[Page N]`
    - `[ChunkType=...]`
    - 开头的 `Image Description:` 前缀
  - 视觉结果落库/回填前执行清理，确保最终 `txt` 输出干净。

### 4) 调整 Vision Prompt，避免模型输出标记
- 位置：
  - `src/services/vision_prompts.py`
- 修改：
  - 明确提示：上下文中的 `[Page ...]` / `[ChunkType=...]` 只用于定位，不应出现在回答中。
  - 默认提示中增加“不输出标记”的约束。

### 5) 文档同步
- 位置：
  - `AGENTS.md`
- 修改：
  - 补充 `type="image"` 行为与视觉输出清洗说明。
  - 更新视觉合并规则描述（不再添加前缀）。

## 修改结果（行为变化）

1. 视觉识别结果不再带 `Image Description:` 前缀。
2. `chunk_type=true` 时，图片块会标注 `type="image"`。
3. `return_txt=true` 时的纯文本输出不会包含 `[Page N]` 或 `[ChunkType=...]` 标记。
4. Prompt 层也明确禁止模型输出这些标记，进一步降低污染概率。

## 影响范围

- `/mineru_with_images` 同步接口
- `/mineru_with_images/task` 异步任务
- `/two_stage/task` 两段式视觉合并

非图片识别文本流程（纯 MinerU）保持不变。

## 验证结果

已执行：

- `uv run --group dev ruff check src`：通过
- `uv run --group dev pytest`：43 passed（仅出现 4 条 `thop` 的 DeprecationWarning）

## 相关文件清单

- `src/utils/text_output.py`
- `src/services/mineru_with_images_service.py`
- `src/services/two_stage_pipeline.py`
- `src/services/vision_prompts.py`
- `AGENTS.md`

