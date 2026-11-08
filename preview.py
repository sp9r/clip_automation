import json
from pathlib import Path
from urllib.parse import quote

from pipeline import PROJECT_ROOT, video_is_valid
from subtitle_project import (
    load_caption_project,
    load_templates,
    save_caption_project,
    write_ass_subtitles,
)


RUNS_DIR = (PROJECT_ROOT / "data" / "runs").resolve()


def _safe_run_dir(job_id: str) -> Path:
    run_dir = (RUNS_DIR / str(job_id)).resolve()
    if run_dir.parent != RUNS_DIR or not run_dir.is_dir():
        raise ValueError("runが見つかりません。")
    return run_dir


def _file_url(path: Path) -> str:
    normalized = str(path.resolve()).replace("\\", "/")
    return f"/gradio_api/file={quote(normalized, safe=':/')}"


def get_preview_payload(job_id: str) -> str:
    if not job_id:
        return json.dumps({"ready": False, "message": "runを選択してください。"}, ensure_ascii=False)
    run_dir = _safe_run_dir(job_id)
    project_path = run_dir / "caption_project.json"
    if not project_path.is_file():
        return json.dumps(
            {
                "ready": False,
                "job_id": job_id,
                "message": "字幕プロジェクトの生成を待っています。",
            },
            ensure_ascii=False,
        )
    project = load_caption_project(project_path)
    proxy_path = run_dir / "preview_proxy.mp4"
    final_path = run_dir / "final_jumpcut_video.mp4"
    source_path_raw = project.get("source_path")
    source_path = Path(source_path_raw) if source_path_raw else None

    if video_is_valid(proxy_path):
        video_path = proxy_path
    elif video_is_valid(final_path):
        video_path = final_path
    elif source_path and video_is_valid(source_path):
        video_path = source_path
    else:
        video_path = None

    if video_path is None:
        return json.dumps(
            {
                "ready": False,
                "job_id": job_id,
                "project_ready": True,
                "message": "編集用動画ファイルを準備中です。そのまま操作を続けてお待ちください。",
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "ready": True,
            "job_id": job_id,
            "video_url": _file_url(video_path),
            "project": project,
            "templates": load_templates(),
        },
        ensure_ascii=False,
    )


def save_preview_caption(
    job_id: str,
    caption_id,
    text: str,
    template_id: str,
    start,
    end,
) -> str:
    run_dir = _safe_run_dir(job_id)
    project_path = run_dir / "caption_project.json"
    project = load_caption_project(project_path)
    templates = load_templates()
    template_id = str(template_id or "").strip()
    if template_id and template_id not in templates:
        raise ValueError("指定されたテンプレートが見つかりません。")
    caption_id = int(caption_id)
    start = float(start)
    end = float(end)
    text = str(text or "").strip()
    if not text:
        raise ValueError("字幕本文を入力してください。")
    if start < 0 or end <= start:
        raise ValueError("字幕の開始・終了時刻が不正です。")
    target = next(
        (item for item in project["captions"] if int(item["id"]) == caption_id),
        None,
    )
    if target is None:
        raise ValueError("字幕IDが見つかりません。")
    target.update(
        {
            "text": text,
            "start": round(start, 3),
            "end": round(end, 3),
            "template_id": template_id,
        }
    )
    save_caption_project(project, project_path)
    write_ass_subtitles(project, run_dir / "captions.ass")
    return get_preview_payload(job_id)


PREVIEW_HTML_TEMPLATE = r"""
<div class="clip-preview-root">
  <div class="clip-preview-message"></div>
  <div class="clip-preview-stage">
    <video class="clip-preview-video" controls preload="metadata"></video>
    <div class="clip-preview-caption"></div>
  </div>
  <div class="clip-preview-controls">
    <label>字幕<select class="clip-caption-select"></select></label>
    <label>開始秒<input class="clip-start" type="number" min="0" step="0.01"></label>
    <label>終了秒<input class="clip-end" type="number" min="0" step="0.01"></label>
    <label>テンプレート<select class="clip-template-select"></select></label>
    <label class="clip-text-label">字幕本文<textarea class="clip-text" rows="2"></textarea></label>
    <div class="clip-preview-actions">
      <button class="clip-save" type="button">字幕を保存</button>
      <button class="clip-refresh" type="button">再読込</button>
      <span class="clip-save-status"></span>
    </div>
  </div>
</div>
"""


PREVIEW_CSS_TEMPLATE = r"""
.clip-preview-root { display: grid; gap: 12px; }
.clip-preview-stage { position: relative; width: 100%; background: #000; border-radius: 10px; overflow: hidden; }
.clip-preview-video { display: block; width: 100%; max-height: 62vh; background: #000; }
.clip-preview-caption { position: absolute; pointer-events: none; white-space: pre-wrap; text-align: center; line-height: 1.25; z-index: 3; }
.clip-preview-controls { display: grid; grid-template-columns: 1fr 0.7fr 0.7fr 1.2fr; gap: 10px; align-items: end; }
.clip-preview-controls label { display: grid; gap: 4px; font-size: 0.9rem; }
.clip-text-label { grid-column: 1 / -1; }
.clip-preview-controls input, .clip-preview-controls select, .clip-preview-controls textarea { width: 100%; padding: 8px; border-radius: 6px; }
.clip-preview-actions { grid-column: 1 / -1; display: flex; gap: 10px; align-items: center; }
.clip-preview-actions button { padding: 8px 14px; border-radius: 6px; }
.clip-save { background: #f05a24; color: white; font-weight: 700; }
.clip-preview-message { color: #d6a300; min-height: 1.4em; }
@media (max-width: 800px) { .clip-preview-controls { grid-template-columns: 1fr 1fr; } }
"""


PREVIEW_JS_ON_LOAD = r"""
let state = null;
const video = element.querySelector('.clip-preview-video');
const overlay = element.querySelector('.clip-preview-caption');
const message = element.querySelector('.clip-preview-message');
const captionSelect = element.querySelector('.clip-caption-select');
const templateSelect = element.querySelector('.clip-template-select');
const textInput = element.querySelector('.clip-text');
const startInput = element.querySelector('.clip-start');
const endInput = element.querySelector('.clip-end');
const saveStatus = element.querySelector('.clip-save-status');

function parseValue() {
  try { return typeof props.value === 'string' ? JSON.parse(props.value) : props.value; }
  catch (error) { return {ready: false, message: 'プレビューデータを読み込めません。'}; }
}
function currentCaption() {
  if (!state?.project?.captions) return null;
  return state.project.captions.find(c => Number(c.id) === Number(captionSelect.value)) || null;
}
function templateFor(caption, overrideId=null) {
  const id = overrideId || caption?.template_id || state?.project?.default_template_id || 'standard';
  return state?.templates?.[id] || state?.templates?.standard || null;
}
function applyPosition(template) {
  const width = Number(state.project.output.width || 1920);
  const height = Number(state.project.output.height || 1080);
  const scale = Math.max(0.25, video.clientWidth / width);
  overlay.style.fontFamily = template.font_name || 'sans-serif';
  overlay.style.fontSize = `${Math.max(10, Number(template.font_size || 54) * scale)}px`;
  overlay.style.color = template.primary_color || '#fff';
  overlay.style.fontWeight = template.bold ? '700' : '400';
  overlay.style.fontStyle = template.italic ? 'italic' : 'normal';
  overlay.style.webkitTextStroke = `${Number(template.outline || 0) * scale}px ${template.outline_color || '#000'}`;
  const shadow = Number(template.shadow || 0) * scale;
  overlay.style.textShadow = `${shadow}px ${shadow}px ${Math.max(1, shadow)}px rgba(0,0,0,.85)`;
  overlay.style.left = overlay.style.right = overlay.style.top = overlay.style.bottom = 'auto';
  overlay.style.transform = 'none';
  if (template.position_x != null && template.position_y != null) {
    overlay.style.left = `${Number(template.position_x) / width * 100}%`;
    overlay.style.top = `${Number(template.position_y) / height * 100}%`;
    overlay.style.transform = 'translate(-50%, -50%)';
    return;
  }
  const alignment = Number(template.alignment || 2);
  const margin = Number(template.margin_v || 80) / height * 100;
  if ([1,4,7].includes(alignment)) { overlay.style.left = '4%'; overlay.style.textAlign = 'left'; }
  else if ([3,6,9].includes(alignment)) { overlay.style.right = '4%'; overlay.style.textAlign = 'right'; }
  else { overlay.style.left = '50%'; overlay.style.transform = 'translateX(-50%)'; overlay.style.textAlign = 'center'; }
  if ([7,8,9].includes(alignment)) overlay.style.top = `${margin}%`;
  else if ([4,5,6].includes(alignment)) { overlay.style.top = '50%'; overlay.style.transform += ' translateY(-50%)'; }
  else overlay.style.bottom = `${margin}%`;
}
function showCaption(caption, textOverride=null, templateOverride=null) {
  if (!caption || !state?.ready) { overlay.textContent = ''; return; }
  overlay.textContent = textOverride ?? caption.text;
  const template = templateFor(caption, templateOverride);
  if (template) applyPosition(template);
}
function updateFromTime() {
  if (!state?.project?.captions) return;
  const t = video.currentTime;
  const caption = state.project.captions.find(c => t >= Number(c.start) && t < Number(c.end));
  showCaption(caption);
}
function fillEditor(caption) {
  if (!caption) return;
  textInput.value = caption.text;
  startInput.value = caption.start;
  endInput.value = caption.end;
  templateSelect.value = caption.template_id || '';
  video.currentTime = Number(caption.start || 0);
  showCaption(caption, textInput.value, templateSelect.value);
}
function loadPayload(payload) {
  state = payload;
  message.textContent = payload?.message || '';
  if (!payload?.ready) { video.removeAttribute('src'); overlay.textContent = ''; return; }
  if (video.getAttribute('src') !== payload.video_url) { video.src = payload.video_url; video.load(); }
  captionSelect.innerHTML = '';
  payload.project.captions.forEach(c => {
    const option = document.createElement('option'); option.value = c.id; option.textContent = `${c.id}: ${c.text}`; captionSelect.appendChild(option);
  });
  templateSelect.innerHTML = '<option value="">デフォルトを使用</option>';
  Object.entries(payload.templates).forEach(([id, template]) => {
    const option = document.createElement('option'); option.value = id; option.textContent = `${template.name} [${id}]`; templateSelect.appendChild(option);
  });
  if (payload.project.captions.length) fillEditor(payload.project.captions[0]);
}
video.addEventListener('timeupdate', updateFromTime);
video.addEventListener('resize', updateFromTime);
captionSelect.addEventListener('change', () => fillEditor(currentCaption()));
textInput.addEventListener('input', () => showCaption(currentCaption(), textInput.value, templateSelect.value));
templateSelect.addEventListener('change', () => showCaption(currentCaption(), textInput.value, templateSelect.value));
startInput.addEventListener('input', () => { const c=currentCaption(); if(c) video.currentTime=Number(startInput.value||0); });
element.querySelector('.clip-save').addEventListener('click', async () => {
  const caption = currentCaption(); if (!caption || !state?.job_id) return;
  saveStatus.textContent = '保存中…';
  try {
    const updated = await server.save_preview_caption(state.job_id, caption.id, textInput.value, templateSelect.value, startInput.value, endInput.value);
    loadPayload(typeof updated === 'string' ? JSON.parse(updated) : updated);
    captionSelect.value = caption.id;
    fillEditor(currentCaption());
    saveStatus.textContent = '保存しました。最終書き出しで反映されます。';
  } catch (error) { saveStatus.textContent = `保存失敗: ${error}`; }
});
element.querySelector('.clip-refresh').addEventListener('click', async () => {
  if (!state?.job_id) return;
  const updated = await server.get_preview_payload(state.job_id);
  loadPayload(typeof updated === 'string' ? JSON.parse(updated) : updated);
});
loadPayload(parseValue());
watch('value', () => loadPayload(parseValue()));
"""
