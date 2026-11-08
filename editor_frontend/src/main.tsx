import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { api, Caption, CaptionTemplate, CreationStatus, EditRegion, Project, ProjectPreset, Run, TranscriptWord } from './api';
import './style.css';

const uid = () => {
  const randomUUID = globalThis.crypto?.randomUUID;
  if (typeof randomUUID === 'function') return randomUUID.call(globalThis.crypto);
  return `command_${Date.now()}_${Math.random().toString(36).slice(2)}`;
};
const TRACK_HEADER_WIDTH = 104;
const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const formatTime = (seconds: number) => {
  const safe = Math.max(0, seconds);
  const minutes = Math.floor(safe / 60);
  const secs = Math.floor(safe % 60);
  const frames = Math.floor((safe % 1) * 30);
  return `${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}:${String(frames).padStart(2, '0')}`;
};

type ResizeEdge = 'start' | 'end';
type ResizeSession = { captionId: string; edge: ResizeEdge; startX: number; startBoundary: number };
type MoveSession = { captionId: string; startX: number; startOffset: number };
type WorkspaceView = 'Editing' | 'Audio' | 'Captions';
type PanelView = 'Project' | 'Effects' | 'History';
type InspectorView = 'Inspector' | 'Metadata';
type TimelineTool = 'select' | 'razor' | 'type';
type Layout = { left: number; right: number; timeline: number };
type LayoutResize = { edge: 'left' | 'right' | 'timeline'; startX: number; startY: number; layout: Layout };
const DEFAULT_LAYOUT: Layout = { left: 252, right: 284, timeline: 306 };
const loadLayout = (): Layout => {
  try {
    const saved = JSON.parse(window.localStorage.getItem('clip-automation-editor-layout') || '{}');
    return {
      left: clamp(Number(saved.left) || DEFAULT_LAYOUT.left, 190, 420),
      right: clamp(Number(saved.right) || DEFAULT_LAYOUT.right, 220, 460),
      timeline: clamp(Number(saved.timeline) || DEFAULT_LAYOUT.timeline, 220, 620),
    };
  } catch {
    return DEFAULT_LAYOUT;
  }
};

function AppShell() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [run, setRun] = useState('');
  const [project, setProject] = useState<Project | null>(null);
  const [words, setWords] = useState<TranscriptWord[]>([]);
  const [editRegions, setEditRegions] = useState<EditRegion[]>([]);
  const [media, setMedia] = useState<string | null>(null);
  const [mediaOffset, setMediaOffset] = useState(0);
  const [selected, setSelected] = useState('');
  const [playhead, setPlayhead] = useState(0);
  const [zoom, setZoom] = useState(80);
  const [scroll, setScroll] = useState(0);
  const [snap, setSnap] = useState(true);
  const [status, setStatus] = useState('Loading project…');
  const [error, setError] = useState('');
  const [mediaError, setMediaError] = useState('');
  const [draftText, setDraftText] = useState('');
  const [isExporting, setIsExporting] = useState(false);
  const [isResizing, setIsResizing] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [runLoadNonce, setRunLoadNonce] = useState(0);
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView>('Editing');
  const [panelView, setPanelView] = useState<PanelView>('Project');
  const [inspectorView, setInspectorView] = useState<InspectorView>('Inspector');
  const [timelineTool, setTimelineTool] = useState<TimelineTool>('select');
  const [monitorScale, setMonitorScale] = useState<'fit' | '100%'>('fit');
  const [monitorZoom, setMonitorZoom] = useState(100);
  const [splitAfterWordId, setSplitAfterWordId] = useState<number | null>(null);
  const [boundarySide, setBoundarySide] = useState<'start' | 'end'>('end');
  const [isExportDialogOpen, setIsExportDialogOpen] = useState(false);
  const [exportFilename, setExportFilename] = useState('clip_automation_export.mp4');
  const [exportUrl, setExportUrl] = useState<string | null>(null);
  const [exportBusy, setExportBusy] = useState(false);
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [dragBoundary, setDragBoundary] = useState<{ captionId: string; edge: ResizeEdge; wordId: number } | null>(null);
  const [dragMove, setDragMove] = useState<{ captionId: string; delta: number } | null>(null);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [creationFile, setCreationFile] = useState<File | null>(null);
  const [creationName, setCreationName] = useState('');
  const [creationPreset, setCreationPreset] = useState('fhd_landscape');
  const [creationUseLlm, setCreationUseLlm] = useState(true);
  const [creationRunId, setCreationRunId] = useState('');
  const [creationStatus, setCreationStatus] = useState<CreationStatus | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [presets, setPresets] = useState<ProjectPreset[]>([]);
  const [templates, setTemplates] = useState<CaptionTemplate[]>([]);
  const [templateOverrides, setTemplateOverrides] = useState<Record<string, string>>({});
  const [templateDraft, setTemplateDraft] = useState<CaptionTemplate | null>(null);
  const [templateName, setTemplateName] = useState('');
  const [monitorMode, setMonitorMode] = useState<'source' | 'rendered'>('source');
  const [sourceMedia, setSourceMedia] = useState<string | null>(null);
  const [layout, setLayout] = useState<Layout>(loadLayout);

  const videoRef = useRef<HTMLVideoElement>(null);
  const timelineRef = useRef<HTMLDivElement>(null);
  const draftTimer = useRef<number | null>(null);
  const saveInFlight = useRef(false);
  const resizeRef = useRef<ResizeSession | null>(null);
  const moveRef = useRef<MoveSession | null>(null);
  const movedRef = useRef(false);
  const layoutResizeRef = useRef<LayoutResize | null>(null);

  const selectedRun = runs.find(item => item.run_id === run);
  const activeCaptions = useMemo(() => project?.captions.filter(caption => !caption.deleted) || [], [project]);
  const selectedCaption = activeCaptions.find(caption => caption.id === selected) || null;
  const selectedIndex = activeCaptions.findIndex(caption => caption.id === selected);
  const leftCaption = selectedIndex > 0 ? activeCaptions[selectedIndex - 1] : null;
  const rightCaption = selectedIndex >= 0 && selectedIndex < activeCaptions.length - 1
    ? activeCaptions[selectedIndex + 1]
    : null;
  const duration = Math.max(activeCaptions.at(-1)?.end || 0, editRegions.at(-1)?.end || 0, 1);
  const visibleStart = Math.max(0, (scroll - TRACK_HEADER_WIDTH) / zoom);
  const viewportWidth = timelineRef.current?.clientWidth || 900;
  const visibleEnd = visibleStart + viewportWidth / zoom;
  const timelineWidth = Math.max(1100, duration * zoom + 180);
  const monitorAspect = project?.output?.width && project.output.height
    ? `${project.output.width} / ${project.output.height}`
    : '16 / 9';
  const selectedTemplateId = (selectedCaption && templateOverrides[selectedCaption.id]) || selectedCaption?.template_id || project?.default_template_id || 'standard';
  const selectedTemplateRecord = templates.find(template => template.id === selectedTemplateId) || templates[0] || null;
  const selectedTemplate = templateDraft?.id === selectedTemplateId ? templateDraft : selectedTemplateRecord;
  // The exported ASS event is absent outside its time range.  Do not keep the
  // selected inspector caption visible in the monitor after it has ended.
  const monitorCaption = activeCaptions.find(caption => playhead >= caption.start && playhead < caption.end) || null;
  const monitorTemplateId = (monitorCaption && templateOverrides[monitorCaption.id]) || monitorCaption?.template_id || project?.default_template_id || 'standard';
  const monitorTemplateRecord = templates.find(template => template.id === monitorTemplateId) || selectedTemplateRecord;
  const monitorTemplate = monitorCaption?.id === selectedCaption?.id && templateDraft
    ? templateDraft
    : monitorTemplateRecord;
  const selectedWords = selectedCaption
    ? words.filter(word => word.word_id >= selectedCaption.start_word_id && word.word_id <= selectedCaption.end_word_id)
    : [];
  const splitCandidates = selectedWords.filter(word => word.word_id < (selectedCaption?.end_word_id ?? -1));

  const orderedWords = useMemo(() => [...words].sort((a, b) => a.word_id - b.word_id), [words]);
  const captionOffset = (caption: Caption) => Number(caption.position_offset || 0);
  const wordBoundaryForTime = (time: number, caption?: Caption | null) => {
    const offset = caption ? captionOffset(caption) : 0;
    const candidates = orderedWords.flatMap((word, index) => {
      const previous = orderedWords[index - 1];
      return [
        { time: word.timeline_start + offset, afterWordId: previous?.word_id ?? word.word_id - 1 },
        { time: word.timeline_end + offset, afterWordId: word.word_id },
      ];
    }).filter(boundary => !caption || (boundary.afterWordId >= caption.start_word_id && boundary.afterWordId < caption.end_word_id));
    return candidates.reduce<{ time: number; afterWordId: number } | null>((best, boundary) => {
      if (!best || Math.abs(boundary.time - time) < Math.abs(best.time - time)) return boundary;
      return best;
    }, null);
  };

  const visibleCaptions = useMemo(
    () => activeCaptions.filter(caption => caption.end >= visibleStart - 2 && caption.start <= visibleEnd + 2),
    [activeCaptions, visibleStart, visibleEnd]
  );
  const tickStep = zoom < 45 ? 5 : zoom < 75 ? 2 : 1;
  const ticks = useMemo(() => {
    const result: number[] = [];
    for (let time = 0; time <= duration + tickStep; time += tickStep) result.push(time);
    return result;
  }, [duration, tickStep]);

  const selectRun = (nextRun: string) => {
    if (nextRun === run && project) return;
    setRun(nextRun);
    setProject(null);
    setWords([]);
    setEditRegions([]);
    setMedia(null);
    setMediaOffset(0);
    setSelected('');
    setError('');
    setMediaError('');
    setExportUrl(null);
    if (nextRun === run) setRunLoadNonce(value => value + 1);
  };

  const chooseWorkspace = (view: WorkspaceView) => { setWorkspaceView(view); setStatus(`${view} workspace selected`); };
  const choosePanel = (view: PanelView) => { setPanelView(view); setStatus(`${view} panel selected`); };
  const chooseTool = (tool: TimelineTool) => {
    setTimelineTool(tool);
    setStatus(`${tool === 'select' ? 'Select' : tool === 'razor' ? 'Razor' : 'Type'} tool selected`);
  };

  const beginLayoutResize = (event: React.PointerEvent, edge: LayoutResize['edge']) => {
    event.preventDefault();
    layoutResizeRef.current = { edge, startX: event.clientX, startY: event.clientY, layout };
    document.body.classList.add('layout-resizing');
  };

  useEffect(() => {
    const move = (event: PointerEvent) => {
      const session = layoutResizeRef.current;
      if (!session) return;
      const deltaX = event.clientX - session.startX;
      const deltaY = session.startY - event.clientY;
      setLayout(current => {
        if (session.edge === 'left') return { ...current, left: clamp(session.layout.left + deltaX, 190, 420) };
        if (session.edge === 'right') return { ...current, right: clamp(session.layout.right - deltaX, 220, 460) };
        return { ...current, timeline: clamp(session.layout.timeline + deltaY, 220, 620) };
      });
    };
    const up = () => {
      if (!layoutResizeRef.current) return;
      layoutResizeRef.current = null;
      document.body.classList.remove('layout-resizing');
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    return () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
  }, []);

  const resetLayout = () => {
    setLayout(DEFAULT_LAYOUT);
    setStatus('Editor layout reset');
  };

  useEffect(() => {
    api.runs()
      .then(nextRuns => {
        setRuns(nextRuns);
        const preferred = nextRuns.find(item => item.timeline_ready && item.source_ready && item.run_id.includes('eval'))
          || nextRuns.find(item => item.timeline_ready && item.source_ready);
        if (preferred) setRun(preferred.run_id);
        setStatus(preferred ? 'Ready to edit' : 'Select a timeline-ready run');
      })
      .catch((exception: Error) => setError(exception.message));
    api.presets().then(setPresets).catch(() => setPresets([]));
    api.captionTemplates().then(setTemplates).catch(() => setTemplates([]));
  }, []);

  useEffect(() => {
    window.localStorage.setItem('clip-automation-editor-layout', JSON.stringify(layout));
  }, [layout]);

  useEffect(() => {
    if (!run) return;
    let active = true;
    api.project(run)
      .then(result => {
        if (!active) return;
        setProject(result.project);
        setWords(result.words || []);
        setEditRegions(result.edit_regions || []);
        const sourceUrl = result.source_media_url || result.media_url;
        setMedia(sourceUrl);
        setSourceMedia(sourceUrl);
        setMonitorMode('source');
        setMediaOffset(0);
        setMediaError('');
        const first = result.project.captions.find(caption => !caption.deleted);
        setSelected(first?.id || '');
        setSplitAfterWordId(first && first.end_word_id > first.start_word_id ? first.start_word_id : null);
        setDraftText(first?.display_text || '');
        setPlayhead(0);
        setStatus(`Saved · revision ${result.project.revision}`);
      })
      .catch((exception: Error) => { if (active) setError(exception.message); });
    return () => { active = false; };
  }, [run, runLoadNonce]);

  useEffect(() => {
    if (!creationRunId) return;
    let active = true;
    const poll = async () => {
      try {
        const nextStatus = await api.creationStatus(creationRunId);
        if (!active) return;
        setCreationStatus(nextStatus);
        if (nextStatus.state === 'ready') {
          const nextRuns = await api.runs();
          if (!active) return;
          setRuns(nextRuns);
          setIsCreating(false);
          setCreationRunId('');
          setIsCreateOpen(false);
          selectRun(creationRunId);
          setStatus('New project ready to edit');
        } else if (nextStatus.state === 'failed') {
          setIsCreating(false);
          setCreationRunId('');
          setStatus('New project failed');
          setError(nextStatus.error || nextStatus.message || '新規プロジェクトの作成に失敗しました。');
        }
      } catch (exception) {
        if (active) setError((exception as Error).message);
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [creationRunId]);

  useEffect(() => {
    if (selectedCaption) setDraftText(selectedCaption.display_text);
    if (selectedCaption) setSplitAfterWordId(selectedCaption.end_word_id > selectedCaption.start_word_id ? selectedCaption.start_word_id : null);
  }, [selectedCaption?.id]);

  useEffect(() => {
    setTemplateDraft(selectedTemplateRecord ? { ...selectedTemplateRecord } : null);
  }, [selectedTemplateId, selectedTemplateRecord?.id]);

  const toggleEditRegion = async (region: EditRegion) => {
    if (!run) return;
    const nextRegions = editRegions.map(item => item.id === region.id
      ? { ...item, mode: (item.mode === 'keep' ? 'delete' : 'keep') as 'keep' | 'delete' }
      : item);
    try {
      const result = await api.editRegions(run, nextRegions);
      setEditRegions(result.regions);
      setStatus(region.mode === 'keep' ? 'Region marked for deletion' : 'Region restored to keep');
    } catch (exception) {
      setError((exception as Error).message);
    }
  };

  const applyCommand = async (body: Record<string, unknown>) => {
    if (!project) return;
    setStatus('Saving…');
    setError('');
    try {
      let commandBody = body;
      if (body.type === 'SplitCaption' && typeof body.caption_id === 'string') {
        const splitCaption = project.captions.find(caption => caption.id === body.caption_id);
        const afterWordId = Number(body.after_word_id);
        if (splitCaption && Number.isFinite(afterWordId)) {
          commandBody = { ...body, ...splitTextForCaption(splitCaption, afterWordId) };
        }
      }
      const next = await api.command(run, { command_id: uid(), base_revision: project.revision, ...commandBody });
      setProject(next);
      setSelected(current => next.captions.some(caption => caption.id === current && !caption.deleted)
        ? current
        : next.captions.find(caption => !caption.deleted)?.id || '');
      setStatus(`Saved · revision ${next.revision}`);
    } catch (exception) {
      setError((exception as Error).message);
      setStatus('Save failed');
    }
  };

  const handleHistory = async (kind: 'undo' | 'redo') => {
    if (!run) return;
    try {
      const next = await api[kind](run);
      setProject(next);
      const nextSelected = next.captions.find(caption => caption.id === selected && !caption.deleted)
        || next.captions.find(caption => !caption.deleted);
      setSelected(nextSelected?.id || '');
      setDraftText(nextSelected?.display_text || '');
      setStatus(`${kind === 'undo' ? 'Undo' : 'Redo'} · revision ${next.revision}`);
    } catch (exception) {
      setError((exception as Error).message);
    }
  };

  const seekVideo = (requestedTime: number) => {
    const mediaDuration = videoRef.current?.duration;
    const usableMediaDuration = typeof mediaDuration === 'number' && Number.isFinite(mediaDuration) && mediaDuration > 0
      ? mediaDuration
      : null;
    const maxTime = usableMediaDuration !== null
      ? Math.min(duration, mediaOffset + usableMediaDuration)
      : duration;
    const nextTime = clamp(requestedTime, mediaOffset, maxTime);
    setPlayhead(nextTime);
    if (videoRef.current) videoRef.current.currentTime = Math.max(0, nextTime - mediaOffset);
  };

  const nudgeVideo = (delta: number) => {
    const currentTime = mediaOffset + (videoRef.current?.currentTime ?? Math.max(0, playhead - mediaOffset));
    seekVideo(currentTime + delta);
  };

  const setPlayheadAt = (clientX: number) => {
    if (!timelineRef.current) return;
    const rect = timelineRef.current.getBoundingClientRect();
    const x = clientX - rect.left + timelineRef.current.scrollLeft - TRACK_HEADER_WIDTH;
    const nextTime = clamp(x / zoom, 0, duration);
    const boundary = snap ? wordBoundaryForTime(nextTime) : null;
    seekVideo(boundary?.time ?? nextTime);
    return boundary;
  };

  const seekToCaption = (caption: Caption) => {
    setSelected(caption.id);
    setDraftText(caption.display_text);
    seekVideo(caption.start);
  };

  const beginResize = (event: React.PointerEvent, caption: Caption, edge: ResizeEdge) => {
    event.preventDefault();
    event.stopPropagation();
    resizeRef.current = { captionId: caption.id, edge, startX: event.clientX, startBoundary: edge === 'end' ? caption.end_word_id : caption.start_word_id - 1 };
    setDragBoundary({ captionId: caption.id, edge, wordId: edge === 'end' ? caption.end_word_id : caption.start_word_id - 1 });
    setSelected(caption.id);
    setIsResizing(true);
  };

  const beginMove = (event: React.PointerEvent, caption: Caption) => {
    if (timelineTool !== 'select') return;
    if ((event.target as HTMLElement).closest('.clip-handle')) return;
    event.preventDefault();
    event.stopPropagation();
    moveRef.current = { captionId: caption.id, startX: event.clientX, startOffset: captionOffset(caption) };
    movedRef.current = false;
    setSelected(caption.id);
    setDraftText(caption.display_text);
  };

  useEffect(() => {
    const previewResize = (event: PointerEvent) => {
      const session = resizeRef.current;
      if (!session || !timelineRef.current || !project) return;
      const rect = timelineRef.current.getBoundingClientRect();
      const rawTime = clamp((event.clientX - rect.left + timelineRef.current.scrollLeft - TRACK_HEADER_WIDTH) / zoom, 0, duration);
      const caption = project.captions.find(item => item.id === session.captionId && !item.deleted);
      const boundary = caption ? wordBoundaryForTime(rawTime) : null;
      if (boundary) setDragBoundary({ captionId: session.captionId, edge: session.edge, wordId: boundary.afterWordId });
    };
    const finishResize = (event: PointerEvent) => {
      const session = resizeRef.current;
      if (!session || !project) return;
      resizeRef.current = null;
      setIsResizing(false);
      const caption = project.captions.find(item => item.id === session.captionId && !item.deleted);
      if (!caption) return;
      const rect = timelineRef.current?.getBoundingClientRect();
      const rawTime = rect ? clamp((event.clientX - rect.left + (timelineRef.current?.scrollLeft || 0) - TRACK_HEADER_WIDTH) / zoom, 0, duration) : caption.end;
      // Resolve neighbours from the caption that received the pointer event.
      // Using the inspector's selected-caption neighbours here meant that
      // resizing a different clip could write a boundary against the first
      // (or previously selected) clip.
      const captionIndex = activeCaptions.findIndex(item => item.id === caption.id);
      const adjacentLeft = captionIndex > 0 ? activeCaptions[captionIndex - 1] : null;
      const adjacentRight = captionIndex >= 0 && captionIndex < activeCaptions.length - 1
        ? activeCaptions[captionIndex + 1]
        : null;
      const left = session.edge === 'end' ? caption : adjacentLeft;
      const right = session.edge === 'end' ? adjacentRight : caption;
      const boundary = wordBoundaryForTime(rawTime, left && right ? undefined : caption);
      const boundaryWord = boundary?.afterWordId ?? session.startBoundary;
      setDragBoundary(null);
      if (boundaryWord === session.startBoundary) return;

      if (session.edge === 'end' && adjacentRight) {
        const nextBoundary = clamp(boundaryWord, caption.start_word_id, adjacentRight.end_word_id - 1);
        if (nextBoundary !== caption.end_word_id) {
          void applyCommand({ type: 'MoveBoundary', left_id: caption.id, right_id: adjacentRight.id, boundary_word_id: nextBoundary });
        }
      }
      if (session.edge === 'start' && adjacentLeft) {
        const nextBoundary = clamp(boundaryWord, adjacentLeft.start_word_id, caption.end_word_id - 1);
        if (nextBoundary !== caption.start_word_id - 1) {
          void applyCommand({ type: 'MoveBoundary', left_id: adjacentLeft.id, right_id: caption.id, boundary_word_id: nextBoundary });
        }
      }
    };
    window.addEventListener('pointermove', previewResize);
    window.addEventListener('pointerup', finishResize);
    return () => {
      window.removeEventListener('pointermove', previewResize);
      window.removeEventListener('pointerup', finishResize);
    };
  }, [project, activeCaptions, run, zoom, duration, orderedWords]);

  useEffect(() => {
    const previewMove = (event: PointerEvent) => {
      const session = moveRef.current;
      if (!session || !project || !timelineRef.current) return;
      const caption = project.captions.find(item => item.id === session.captionId && !item.deleted);
      if (!caption) return;
      const rawDelta = (event.clientX - session.startX) / zoom;
      const snapped = snap ? wordBoundaryForTime(caption.start + rawDelta, caption) : null;
      const delta = snapped ? snapped.time - caption.start : rawDelta;
      if (Math.abs(event.clientX - session.startX) > 2) movedRef.current = true;
      setDragMove({ captionId: caption.id, delta: Number(delta.toFixed(3)) });
    };
    const finishMove = (event: PointerEvent) => {
      const session = moveRef.current;
      if (!session || !project) return;
      moveRef.current = null;
      const caption = project.captions.find(item => item.id === session.captionId && !item.deleted);
      setDragMove(null);
      if (!caption) return;
      const pointerDistance = Math.abs(event.clientX - session.startX);
      // A click can finish with a sub-pixel clientX difference in Chromium.
      // Treat it as selection, never as a MoveCaption command.
      if (!movedRef.current && pointerDistance < 4) {
        movedRef.current = false;
        return;
      }
      const rawDelta = (event.clientX - session.startX) / zoom;
      const snapped = snap ? wordBoundaryForTime(caption.start + rawDelta, caption) : null;
      const delta = Number((snapped ? snapped.time - caption.start : rawDelta).toFixed(3));
      if (Math.abs(delta) < 0.01) return;
      movedRef.current = true;
      void applyCommand({ type: 'MoveCaption', caption_id: caption.id, delta });
    };
    window.addEventListener('pointermove', previewMove);
    window.addEventListener('pointerup', finishMove);
    return () => {
      window.removeEventListener('pointermove', previewMove);
      window.removeEventListener('pointerup', finishMove);
    };
  }, [project, zoom, snap, orderedWords]);

  const handleTextChange = (text: string) => {
    setDraftText(text);
    if (draftTimer.current) window.clearTimeout(draftTimer.current);
    draftTimer.current = window.setTimeout(() => {
      draftTimer.current = null;
      if (selectedCaption && text !== selectedCaption.display_text && !saveInFlight.current) {
        saveInFlight.current = true;
        void applyCommand({ type: 'UpdateCaptionText', caption_id: selectedCaption.id, display_text: text })
          .finally(() => { saveInFlight.current = false; });
      }
    }, 800);
  };

  const saveTextImmediate = () => {
    if (draftTimer.current) window.clearTimeout(draftTimer.current);
    draftTimer.current = null;
    if (selectedCaption && draftText !== selectedCaption.display_text && !saveInFlight.current) {
      saveInFlight.current = true;
      void applyCommand({ type: 'UpdateCaptionText', caption_id: selectedCaption.id, display_text: draftText })
        .finally(() => { saveInFlight.current = false; });
    }
  };

  const applyCaptionTemplate = (templateId: string) => {
    if (!selectedCaption) return;
    setTemplateOverrides(current => ({ ...current, [selectedCaption.id]: templateId }));
    void applyCommand({ type: 'ApplyCaptionStyle', caption_ids: [selectedCaption.id], template_id: templateId });
  };

  const updateTemplateDraft = (field: string, value: unknown) => {
    setTemplateDraft(current => current ? { ...current, [field]: value } : current);
  };

  const saveTemplateDraft = async () => {
    if (!templateDraft) return;
    try {
      const saved = await api.saveCaptionTemplate(templateDraft);
      setTemplates(await api.captionTemplates());
      setTemplateDraft(saved);
      setStatus(`Saved template: ${saved.name}`);
    } catch (exception) {
      setError((exception as Error).message);
    }
  };

  const saveTemplateAs = async () => {
    if (!templateDraft) return;
    const name = templateName.trim() || `${templateDraft.name} copy`;
    try {
      const saved = await api.saveCaptionTemplate({ ...templateDraft, id: '', name, user_defined: true });
      setTemplates(await api.captionTemplates());
      setTemplateName('');
      setTemplateOverrides(current => selectedCaption ? { ...current, [selectedCaption.id]: saved.id } : current);
      setTemplateDraft(saved);
      setStatus(`Created template: ${saved.name}`);
    } catch (exception) {
      setError((exception as Error).message);
    }
  };

  const applyTemplateToAll = () => {
    if (!templateDraft || !activeCaptions.length) return;
    setTemplateOverrides(current => Object.fromEntries(activeCaptions.map(caption => [caption.id, templateDraft.id])));
    void applyCommand({ type: 'ApplyCaptionStyle', caption_ids: activeCaptions.map(caption => caption.id), template_id: templateDraft.id });
  };

  const moveCaptionBoundary = (delta: number) => {
    if (!selectedCaption) return;
    if (boundarySide === 'end' && rightCaption) {
      const boundary = clamp(selectedCaption.end_word_id + delta, selectedCaption.start_word_id, rightCaption.end_word_id - 1);
      if (boundary !== selectedCaption.end_word_id) {
        void applyCommand({ type: 'MoveBoundary', left_id: selectedCaption.id, right_id: rightCaption.id, boundary_word_id: boundary });
      }
    } else if (boundarySide === 'start' && leftCaption) {
      const boundary = clamp(selectedCaption.start_word_id - 1 + delta, leftCaption.start_word_id, selectedCaption.end_word_id - 1);
      if (boundary !== selectedCaption.start_word_id - 1) {
        void applyCommand({ type: 'MoveBoundary', left_id: leftCaption.id, right_id: selectedCaption.id, boundary_word_id: boundary });
      }
    }
  };

  const splitSelectedCaption = () => {
    if (!selectedCaption || splitAfterWordId === null) return;
    applySplitCaption(selectedCaption, splitAfterWordId);
  };

  const splitAtPlayhead = (caption: Caption | null = selectedCaption) => {
    if (!caption || caption.start_word_id >= caption.end_word_id) return;
    const boundary = wordBoundaryForTime(playhead, caption);
    if (!boundary || boundary.afterWordId < caption.start_word_id || boundary.afterWordId >= caption.end_word_id) return;
    setSplitAfterWordId(boundary.afterWordId);
    applySplitCaption(caption, boundary.afterWordId);
  };

  const mergeSelectedCaption = () => {
    if (!selectedCaption || !rightCaption) return;
    void applyCommand({ type: 'MergeCaptions', left_id: selectedCaption.id, right_id: rightCaption.id });
  };

  const toggleEditRegionAtPlayhead = () => {
    const region = editRegions.find(item => playhead >= item.start && playhead < item.end);
    if (region) void toggleEditRegion(region);
  };

  const wordsForCaption = (caption: Caption) => orderedWords.filter(word => word.word_id >= caption.start_word_id && word.word_id <= caption.end_word_id);

  const splitTextForCaption = (caption: Caption, afterWordId: number) => {
    const captionWords = wordsForCaption(caption);
    const splitIndex = captionWords.filter(word => word.word_id <= afterWordId).length;
    const text = caption.display_text || '';
    const characterIndex = clamp(Math.round(text.length * splitIndex / Math.max(1, captionWords.length)), 1, Math.max(1, text.length - 1));
    return {
      left_display_text: text.slice(0, characterIndex).trim() || text.slice(0, characterIndex),
      right_display_text: text.slice(characterIndex).trim() || text.slice(characterIndex),
    };
  };

  const applySplitCaption = (caption: Caption, afterWordId: number) => {
    void applyCommand({
      type: 'SplitCaption',
      caption_id: caption.id,
      after_word_id: afterWordId,
      ...splitTextForCaption(caption, afterWordId),
    });
  };

  const handleCaptionClipClick = (event: React.MouseEvent<HTMLDivElement>, caption: Caption) => {
    event.stopPropagation();
    if (movedRef.current) {
      movedRef.current = false;
      return;
    }
    const rect = event.currentTarget.getBoundingClientRect();
    const localRatio = rect.width > 0 ? clamp((event.clientX - rect.left) / rect.width, 0, 1) : 0;
    const clickedTime = caption.start + (caption.end - caption.start) * localRatio;
    const boundary = wordBoundaryForTime(clickedTime, caption);
    const canSplit = timelineTool === 'razor';
    if (canSplit && boundary) {
      setSelected(caption.id);
      setDraftText(caption.display_text);
      setPlayhead(boundary.time);
      if (boundary.afterWordId >= caption.start_word_id && boundary.afterWordId < caption.end_word_id) {
        setSplitAfterWordId(boundary.afterWordId);
        applySplitCaption(caption, boundary.afterWordId);
        return;
      }
    }
    seekToCaption(caption);
  };

  const renderPreview = async () => {
    if (!run) return;
    setIsPreviewing(true);
    setStatus('Loading unrendered source preview…');
    setError('');
    try {
      // Keep the requested window strictly below the API's 30-second limit.
      // Without the small margin, binary floating-point rounding can turn
      // `start + 30` into 30.000000000000004 on the server.
      const rawStart = selectedCaption?.start ?? playhead;
      const start = Number(rawStart.toFixed(3));
      const end = Number(Math.min(duration, start + 30 - 0.001).toFixed(3));
      const result = await api.render(run, start, end);
      const sourceUrl = result.source_media_url || result.media_url;
      setMediaOffset(0);
      setMedia(sourceUrl);
      setSourceMedia(sourceUrl);
      setMonitorMode('source');
      setPlayhead(start);
      setMediaError('');
      window.setTimeout(() => {
        if (videoRef.current && Number.isFinite(start)) videoRef.current.currentTime = start;
      }, 0);
      setStatus(`Source preview ready · revision ${result.revision} · no encoding`);
    } catch (exception) {
      setError((exception as Error).message);
      setStatus('Preview failed');
    } finally {
      setIsPreviewing(false);
    }
  };

  const openExportDialog = () => {
    if (!project) return;
    setExportUrl(null);
    setIsExportDialogOpen(true);
  };

  const exportFullVideo = async () => {
    if (!run || !project) return;
    const filename = exportFilename.trim().replace(/[\\/:*?"<>|]/g, '_') || 'clip_automation_export.mp4';
    setExportBusy(true);
    setIsExporting(true);
    setStatus('Exporting full video…');
    setError('');
    try {
      const result = await api.export(run, project.revision, filename);
      setExportFilename(result.filename || filename);
      setExportUrl(api.media(result.download_url || result.media_url));
      setStatus(`Final export ready · ${result.filename || filename}`);
    } catch (exception) {
      setError((exception as Error).message);
      setStatus('Export failed');
    } finally {
      setExportBusy(false);
      setIsExporting(false);
    }
  };

  const prepareTimeline = async () => {
    if (!run) return;
    setStatus('Preparing timeline…');
    setError('');
    try {
      const report = await api.prepare(run);
      const nextRuns = await api.runs();
      setRuns(nextRuns);
      if (report.state === 'ready') {
        const loaded = await api.project(run);
        setProject(loaded.project);
        setWords(loaded.words || []);
        const sourceUrl = loaded.source_media_url || loaded.media_url;
        setMedia(sourceUrl);
        setSourceMedia(sourceUrl);
        setMonitorMode('source');
        setMediaOffset(0);
        setMediaError('');
        setSelected(loaded.project.captions.find(caption => !caption.deleted)?.id || '');
        setStatus(`${report.caption_count} captions · ${report.word_count} words ready`);
      } else {
        setProject(null);
        setStatus(`Preparation result: ${report.state}`);
        setError('This run needs review. Check its migration report before editing.');
      }
    } catch (exception) {
      setError((exception as Error).message);
      setStatus('Timeline preparation failed');
    }
  };

  const submitNewProject = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!creationFile || isCreating) return;
    setIsCreating(true);
    setError('');
    setStatus('Uploading new project…');
    try {
      const name = creationName.trim() || creationFile.name.replace(/.[^.]+$/, '');
      const result = await api.createProject(creationFile, name, creationPreset, creationUseLlm);
      setCreationRunId(result.run_id);
      setCreationStatus({ state: result.state, stage: 'upload', progress: 0, message: 'Video received.' });
    } catch (exception) {
      setIsCreating(false);
      setStatus('New project failed');
      setError((exception as Error).message);
    }
  };

  const closeCreateProject = () => {
    if (isCreating) return;
    setIsCreateOpen(false);
    setCreationFile(null);
    setCreationName('');
    setCreationStatus(null);
  };

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      const active = document.activeElement?.tagName;
      const editable = active === 'TEXTAREA' || active === 'INPUT' || active === 'SELECT';
      if ((event.ctrlKey || event.metaKey) && event.key === 'z' && !editable) {
        event.preventDefault();
        handleHistory(event.shiftKey ? 'redo' : 'undo');
      }
      if (event.code === 'Space' && !editable) {
        event.preventDefault();
        if (videoRef.current?.paused) videoRef.current.play();
        else videoRef.current?.pause();
      }
      if (!editable && event.key.toLowerCase() === 'j' && videoRef.current) {
        videoRef.current.currentTime = Math.max(0, videoRef.current.currentTime - 1);
      }
      if (!editable && event.key.toLowerCase() === 'k') videoRef.current?.pause();
      if (!editable && event.key.toLowerCase() === 'l') videoRef.current?.play();
      if (!editable && event.altKey && event.shiftKey && selectedCaption && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        event.preventDefault();
        void applyCommand({ type: 'MoveCaption', caption_id: selectedCaption.id, delta: event.key === 'ArrowLeft' ? -0.1 : 0.1 });
      }
      if (!editable && event.altKey && !event.shiftKey && selectedCaption && (event.key === 'ArrowLeft' || event.key === 'ArrowRight') && rightCaption) {
        event.preventDefault();
        const boundary = selectedCaption.end_word_id + (event.key === 'ArrowLeft' ? -1 : 1);
        applyCommand({ type: 'MoveBoundary', left_id: selectedCaption.id, right_id: rightCaption.id, boundary_word_id: boundary });
      }
      if (!editable && event.key.toLowerCase() === 's' && selectedCaption && selectedCaption.text_status === 'as_transcribed') {
        event.preventDefault();
        splitAtPlayhead();
      }
      if (!editable && event.key.toLowerCase() === 'm') {
        event.preventDefault();
        mergeSelectedCaption();
      }
      if (!editable && event.key.toLowerCase() === 'r') {
        event.preventDefault();
        chooseTool('razor');
      }
      if (!editable && event.key.toLowerCase() === 'v') {
        event.preventDefault();
        chooseTool('select');
      }
      if (!editable && (event.key === 'Delete' || event.key === 'Backspace')) {
        event.preventDefault();
        if (selectedCaption) void applyCommand({ type: 'DeleteCaption', caption_id: selectedCaption.id });
        else toggleEditRegionAtPlayhead();
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });

  const monitorWidth = project?.output?.width || 1920;
  const monitorHeight = project?.output?.height || 1080;
  const monitorAlignment = monitorTemplate?.alignment || 2;
  const monitorHorizontal = monitorAlignment % 3 === 1 ? 'left' : monitorAlignment % 3 === 0 ? 'right' : 'center';
  const monitorPositionX = monitorTemplate?.position_x != null
    ? `${clamp((monitorTemplate.position_x / monitorWidth) * 100, 0, 100)}%`
    : monitorHorizontal === 'left' ? '4%' : monitorHorizontal === 'right' ? '96%' : '50%';
  const monitorPositionY = monitorTemplate?.position_y != null
    ? `${clamp((monitorTemplate.position_y / monitorHeight) * 100, 0, 100)}%`
    : monitorAlignment <= 3 ? `${clamp(100 - ((monitorTemplate?.margin_v || 0) / monitorHeight) * 100, 0, 100)}%`
      : monitorAlignment >= 7 ? `${clamp(((monitorTemplate?.margin_v || 0) / monitorHeight) * 100, 0, 100)}%`
        : '50%';
  const monitorTransform = monitorHorizontal === 'left' ? 'translate(0,-50%)' : monitorHorizontal === 'right' ? 'translate(-100%,-50%)' : 'translate(-50%,-50%)';
  const monitorCaptionStyle = {
    ['--caption-font-family' as string]: monitorTemplate?.font_name || 'Arial',
    ['--caption-font-size' as string]: `${Math.max(12, monitorTemplate?.font_size || 42)}px`,
    ['--caption-color' as string]: monitorTemplate?.primary_color || '#ffffff',
    ['--caption-opacity' as string]: '1',
    ['--caption-outline-width' as string]: `${Math.max(0, monitorTemplate?.outline || 0)}px`,
    ['--caption-outline-color' as string]: monitorTemplate?.outline_color || '#000000',
    ['--caption-shadow-x' as string]: `${Math.max(0, monitorTemplate?.shadow || 0) / 2}px`,
    ['--caption-shadow-y' as string]: `${Math.max(0, monitorTemplate?.shadow || 0)}px`,
    ['--caption-shadow-blur' as string]: `${Math.max(0, (monitorTemplate?.shadow || 0) * 2)}px`,
    ['--caption-shadow-color' as string]: monitorTemplate?.shadow_color || '#000000',
    ['--caption-background' as string]: monitorTemplate?.background_color || '#000000',
    ['--caption-background-opacity' as string]: String((monitorTemplate?.background_opacity || 0) / 100),
    ['--caption-position-x' as string]: monitorPositionX,
    ['--caption-position-y' as string]: monitorPositionY,
    ['--caption-align' as string]: monitorHorizontal,
    ['--caption-transform' as string]: monitorTransform,
    ['--caption-scale-x' as string]: String((monitorTemplate?.scale_x || 100) / 100),
    ['--caption-scale-y' as string]: String((monitorTemplate?.scale_y || 100) / 100),
    ['--caption-rotation' as string]: `${monitorTemplate?.angle || 0}deg`,
    ['--caption-letter-spacing' as string]: `${monitorTemplate?.letter_spacing || 0}px`,
    ['--caption-font-weight' as string]: monitorTemplate?.bold ? '700' : '400',
    ['--caption-font-style' as string]: monitorTemplate?.italic ? 'italic' : 'normal',
  } as React.CSSProperties;

  return (
    <div className="app-shell" style={{ ['--left-panel-width' as string]: `${layout.left}px`, ['--right-panel-width' as string]: `${layout.right}px`, ['--timeline-height' as string]: `${layout.timeline}px` }}>
      <header className="appbar">
        <div className="brandmark">CA</div>
        <div className="brand-copy"><strong>Clip Automation</strong><span>Unified Editor</span></div>
        <div className="app-divider" />
        <label className="project-picker">
          <span>PROJECT</span>
          <select value={run} onChange={event => selectRun(event.target.value)}>
            {runs.map(item => <option key={item.run_id} value={item.run_id}>{item.run_id}{item.timeline_ready ? '' : ` · ${item.state}`}</option>)}
          </select>
        </label>
        <div className="workspace-tabs">
          {(['Editing', 'Audio', 'Captions'] as WorkspaceView[]).map(view => <button key={view} className={workspaceView === view ? 'active' : ''} aria-pressed={workspaceView === view} onClick={() => chooseWorkspace(view)}>{view}</button>)}
        </div>
        <div className="appbar-spacer" />
        <div className="status-line"><span className="status-dot" />{status}</div>
        <div className="header-actions">
          <button className="new-project-button" onClick={() => setIsCreateOpen(true)}>＋ New Project</button>
          {selectedRun && !selectedRun.timeline_ready && <button className="accent" onClick={prepareTimeline}>Prepare</button>}
          <button disabled={!project} onClick={() => handleHistory('undo')}>↶</button>
          <button disabled={!project} onClick={() => handleHistory('redo')}>↷</button>
          <button className="secondary" disabled={!media || !project || isPreviewing} onClick={renderPreview}>{isPreviewing ? 'Loading…' : 'Preview source'}</button>
          <button className="accent" disabled={!project || isExporting} onClick={openExportDialog}>{isExporting ? 'Exporting…' : 'Export final'}</button>
        </div>
      </header>

      {error && <div className="error-banner"><span>{error}</span><button onClick={() => setError('')}>Dismiss</button></div>}

      {isCreateOpen && <div className="modal-backdrop" role="presentation">
        <form className="create-modal" onSubmit={submitNewProject}>
          <div className="modal-header"><div><span className="eyebrow">PROJECT SETUP</span><h2>New Project</h2></div><button type="button" className="icon-button" onClick={closeCreateProject} disabled={isCreating}>×</button></div>
          <label className="modal-field">Video file<input type="file" accept="video/*,.mkv,.webm" onChange={event => {
            const file = event.target.files?.[0] || null;
            setCreationFile(file);
            if (file && !creationName) setCreationName(file.name.replace(/.[^.]+$/, ''));
          }} /></label>
          <label className="modal-field">Project name<input value={creationName} onChange={event => setCreationName(event.target.value)} placeholder="New Project" /></label>
          <label className="modal-field">Format<select value={creationPreset} onChange={event => setCreationPreset(event.target.value)}>
            {presets.length ? presets.map(preset => <option key={preset.id} value={preset.id}>{preset.name}</option>) : <option value="fhd_landscape">Standard Landscape FHD (1920x1080)</option>}
          </select></label>
          <label className="modal-checkbox"><input type="checkbox" checked={creationUseLlm} onChange={event => setCreationUseLlm(event.target.checked)} /> Use Ollama/LLM caption segmentation</label>
          {creationStatus && <div className={`creation-progress ${creationStatus.state === 'failed' ? 'failed' : ''}`}><div className="creation-progress-heading"><strong>{creationStatus.message || 'Preparing…'}</strong><span>{creationStatus.stage || 'upload'}</span></div><div className="progress-track"><i style={{ width: `${Math.round((creationStatus.progress || 0) * 100)}%` }} /></div><small>{creationStatus.state === 'failed' ? (creationStatus.error || 'Check the error banner above.') : (creationStatus.progress ? `${Math.round(creationStatus.progress * 100)}%` : 'Starting processing…')} · Editor will switch automatically when ready</small></div>}
          <div className="modal-actions"><button type="button" className="secondary" onClick={closeCreateProject} disabled={isCreating}>Cancel</button><button type="submit" className="accent" disabled={!creationFile || isCreating}>{isCreating ? 'Creating…' : 'Create project'}</button></div>
        </form>
      </div>}

      {isExportDialogOpen && <div className="modal-backdrop" role="presentation"><div className="create-modal export-modal"><div className="modal-header"><div><span className="eyebrow">FINAL DELIVERY</span><h2>Export final video</h2></div><button type="button" className="icon-button" onClick={() => setIsExportDialogOpen(false)} disabled={exportBusy}>×</button></div><p className="export-note">この操作で初めて最終動画をレンダーします。編集中のPreviewでは最終Exportは行いません。</p><label className="modal-field">ファイル名<input value={exportFilename} onChange={event => setExportFilename(event.target.value)} disabled={exportBusy} /></label>{exportUrl && <div className="export-ready"><strong>書き出し完了</strong><span>ノートPCへ保存するには下のボタンを押してください。ブラウザの保存先設定が使われます。</span><a className="accent download-button" href={exportUrl} download={exportFilename}>Download to notebook</a></div>}<div className="modal-actions"><button type="button" className="secondary" onClick={() => setIsExportDialogOpen(false)} disabled={exportBusy}>{exportUrl ? 'Close' : 'Cancel'}</button>{!exportUrl && <button type="button" className="accent" onClick={() => void exportFullVideo()} disabled={exportBusy}>{exportBusy ? 'Rendering final…' : 'Render final export'}</button>}</div></div></div>}

      <div className="editor-workspace">
        <aside className="media-panel">
          <div className="panel-tabs">{(['Project', 'Effects', 'History'] as PanelView[]).map(view => <button key={view} className={panelView === view ? 'active' : ''} aria-pressed={panelView === view} onClick={() => choosePanel(view)}>{view}</button>)}</div>
          <div className="panel-heading"><div><span className="eyebrow">BROWSER</span><h2>Media</h2></div><button className="icon-button">•••</button></div>
          <div className="search-field"><span>⌕</span><input placeholder="Search runs" /></div>
          <div className="bin-section"><div className="bin-title"><span>▾</span><strong>Timeline-ready</strong><small>{runs.filter(item => item.timeline_ready).length}</small></div>
            <div className="run-list">
              {runs.filter(item => item.timeline_ready).map(item => (
                <button key={item.run_id} className={item.run_id === run ? 'run-card active' : 'run-card'} onClick={() => selectRun(item.run_id)}>
                  <span className="run-thumb">▣</span><span className="run-info"><strong>{item.run_id}</strong><small>{item.run_id.includes('eval') ? 'Evaluation copy' : 'Ready to edit'}</small></span><span className="ready-pill">READY</span>
                </button>
              ))}
            </div>
          </div>
          <div className="bin-section muted-bin"><div className="bin-title"><span>▸</span><strong>Needs preparation</strong><small>{runs.filter(item => !item.timeline_ready).length}</small></div></div>
          <div className="media-footer"><span>LOCAL PROJECT</span><span>•</span><span>{runs.length} runs</span></div>
        </aside>
        <div className="workspace-resizer vertical-resizer" role="separator" aria-label="Resize media panel" onPointerDown={event => beginLayoutResize(event, 'left')} />

        <section className="monitor-panel">
          <div className="monitor-toolbar"><div className="monitor-title"><span className="live-dot" />PROGRAM MONITOR <small className="monitor-mode">{monitorMode === 'rendered' ? 'RENDERED COMPOSITION' : 'SOURCE MEDIA'}</small></div><div className="monitor-toolbar-actions"><button className={monitorScale === 'fit' ? 'active' : ''} aria-pressed={monitorScale === 'fit'} onClick={() => { setMonitorScale('fit'); setStatus('Monitor fit selected'); }}>Fit</button><button className={monitorScale === '100%' ? 'active' : ''} aria-pressed={monitorScale === '100%'} onClick={() => { setMonitorScale('100%'); setStatus('Monitor 100% selected'); }}>100%</button>{monitorMode === 'rendered' && <button onClick={() => { setMedia(sourceMedia); setMediaOffset(0); setMonitorMode('source'); setStatus('Source media selected'); }}>Source</button>}<span className="timecode">{formatTime(playhead)}</span></div></div>
          <div className="monitor-stage" style={{ ['--monitor-aspect' as string]: monitorAspect, minHeight: 0, overflow: 'hidden' }}>
            <div className="monitor-zoom-strip"><button aria-label="Zoom out" onClick={() => setMonitorZoom(value => Math.max(25, value - 10))}>−</button><input aria-label="Monitor zoom" type="range" min="25" max="200" value={monitorZoom} onChange={event => setMonitorZoom(+event.target.value)} /><button aria-label="Zoom in" onClick={() => setMonitorZoom(value => Math.min(200, value + 10))}>＋</button><span>{monitorZoom}%</span><button onClick={() => setMonitorZoom(100)}>Fit height</button></div>
            
            {media ? <video className="monitor-video" style={{ transform: `scale(${monitorZoom / 100})`, height: '100%', width: 'auto', maxHeight: '100%', maxWidth: '100%', objectFit: 'contain' }} ref={videoRef} src={api.media(media)} preload="metadata" onLoadStart={() => setMediaError('')} onPlay={() => setIsPlaying(true)} onPause={() => setIsPlaying(false)} onEnded={() => setIsPlaying(false)} onError={() => {
              setMediaError('This video cannot be decoded. Check the source media or choose another run.');
              setStatus('Media failed to load');
            }} onTimeUpdate={event => {
              const time = mediaOffset + event.currentTarget.currentTime;
              setPlayhead(time);
              const activeCaption = activeCaptions.find(caption => time >= caption.start && time < caption.end);
              if (activeCaption && activeCaption.id !== selected) setSelected(activeCaption.id);
            }} /> : <div className="no-media"><span className="no-media-icon">◈</span><strong>No source media loaded</strong><small>Caption editing is available without media.</small></div>}
            {monitorCaption && <div className="monitor-caption-preview caption-style-target" aria-live="polite" style={monitorCaptionStyle}>{monitorCaption.id === selected ? draftText : monitorCaption.display_text}</div>}
            {mediaError && <div className="media-error"><strong>Media error</strong><span>{mediaError}</span></div>}
          </div>
          <div className="transport-bar"><button onClick={() => nudgeVideo(-1)}>−1s</button><button className="play-button" aria-label={isPlaying ? 'Pause' : 'Play'} onClick={() => { if (!videoRef.current) return; if (videoRef.current.paused) void videoRef.current.play(); else videoRef.current.pause(); }}>{isPlaying ? 'Ⅱ' : '▶'}</button><button onClick={() => nudgeVideo(1)}>+1s</button><span className="transport-time">{formatTime(playhead)} / {formatTime(duration)}</span><span className="keyboard-hint">J / K / L · Space</span></div>
        </section>
        <div className="workspace-resizer vertical-resizer" role="separator" aria-label="Resize inspector panel" onPointerDown={event => beginLayoutResize(event, 'right')} />

        <aside className="inspector-panel">
          {selectedCaption && <div className="inspector-section word-edit-section"><div className="section-title">Whisper unit editing</div><div className="word-chip-list">{selectedWords.map(word => <button key={word.word_id} type="button" className={splitAfterWordId === word.word_id ? 'active' : ''} disabled={word.word_id === selectedCaption.end_word_id} onClick={() => setSplitAfterWordId(word.word_id)} title={`${word.timeline_start.toFixed(2)}s – ${word.timeline_end.toFixed(2)}s`}>{word.text}<small>{word.word_id}</small></button>)}</div><label className="compact-field">Split after<select aria-label="Split after transcript unit" value={splitAfterWordId ?? ''} onChange={event => setSplitAfterWordId(event.target.value === '' ? null : Number(event.target.value))}><option value="">Select a unit</option>{splitCandidates.map(word => <option key={word.word_id} value={word.word_id}>{word.word_id}: {word.text}</option>)}</select></label><button className="action-wide" disabled={splitAfterWordId === null} onClick={splitSelectedCaption}>Split at this Whisper unit</button><div className="boundary-controls"><label className="compact-field">Boundary<select aria-label="Boundary side" value={boundarySide} onChange={event => setBoundarySide(event.target.value as 'start' | 'end')}><option value="end">End / next caption</option><option value="start">Start / previous caption</option></select></label><div className="boundary-buttons"><button disabled={boundarySide === 'end' ? !rightCaption : !leftCaption} onClick={() => moveCaptionBoundary(-1)}>−1 unit</button><button disabled={boundarySide === 'end' ? !rightCaption : !leftCaption} onClick={() => moveCaptionBoundary(1)}>+1 unit</button></div></div></div>}
          {selectedCaption && <div className="inspector-section caption-style-section"><div className="section-title">Caption style</div><label className="compact-field">Template<select value={selectedTemplateId} onChange={event => applyCaptionTemplate(event.target.value)}>{templates.map(template => <option key={template.id} value={template.id}>{template.name}</option>)}</select></label>{templateDraft && <div className="caption-style-controls">
            <label><span>Font</span><input value={templateDraft.font_name} onChange={event => updateTemplateDraft('font_name', event.target.value)} /></label>
            <label><span>Size</span><input type="range" min="12" max="180" value={templateDraft.font_size} onChange={event => updateTemplateDraft('font_size', Number(event.target.value))} /><input className="caption-number-input" aria-label="Font size in pixels" type="number" min="12" max="180" value={templateDraft.font_size} onChange={event => updateTemplateDraft('font_size', clamp(Number(event.target.value) || 12, 12, 180))} /><output>{templateDraft.font_size}px</output></label>
            <label><span>Text color</span><input type="color" value={templateDraft.primary_color} onChange={event => updateTemplateDraft('primary_color', event.target.value)} /></label>
            <label><span>Outline</span><input type="range" min="0" max="24" value={templateDraft.outline} onChange={event => updateTemplateDraft('outline', Number(event.target.value))} /><output>{templateDraft.outline}px</output></label>
            <label><span>Outline color</span><input type="color" value={templateDraft.outline_color} onChange={event => updateTemplateDraft('outline_color', event.target.value)} /></label>
            <label><span>Shadow</span><input type="range" min="0" max="30" value={templateDraft.shadow} onChange={event => updateTemplateDraft('shadow', Number(event.target.value))} /><output>{templateDraft.shadow}px</output></label>
            <label><span>Shadow color</span><input type="color" value={templateDraft.shadow_color || '#000000'} onChange={event => updateTemplateDraft('shadow_color', event.target.value)} /></label>
            <label><span>Background</span><input type="color" value={templateDraft.background_color || '#000000'} onChange={event => updateTemplateDraft('background_color', event.target.value)} /></label>
            <label><span>Box opacity</span><input type="range" min="0" max="100" value={templateDraft.background_opacity ?? 0} onChange={event => updateTemplateDraft('background_opacity', Number(event.target.value))} /><output>{templateDraft.background_opacity ?? 0}%</output></label>
            <label><span>Letter spacing</span><input type="range" min="-20" max="50" value={templateDraft.letter_spacing ?? 0} onChange={event => updateTemplateDraft('letter_spacing', Number(event.target.value))} /><output>{templateDraft.letter_spacing ?? 0}px</output></label>
            <label><span>Scale X / Y</span><input type="range" min="50" max="200" value={templateDraft.scale_x ?? 100} onChange={event => updateTemplateDraft('scale_x', Number(event.target.value))} /><output>{templateDraft.scale_x ?? 100}%</output></label>
            <label><span>Rotation</span><input type="range" min="-180" max="180" value={templateDraft.angle ?? 0} onChange={event => updateTemplateDraft('angle', Number(event.target.value))} /><output>{templateDraft.angle ?? 0}°</output></label>
            <div className="checkbox-row"><label><input type="checkbox" checked={Boolean(templateDraft.bold)} onChange={event => updateTemplateDraft('bold', event.target.checked)} /> Bold</label><label><input type="checkbox" checked={Boolean(templateDraft.italic)} onChange={event => updateTemplateDraft('italic', event.target.checked)} /> Italic</label><label><input type="checkbox" checked={Boolean(templateDraft.underline)} onChange={event => updateTemplateDraft('underline', event.target.checked)} /> Underline</label><label><input type="checkbox" checked={Boolean(templateDraft.strikeout)} onChange={event => updateTemplateDraft('strikeout', event.target.checked)} /> Strike</label></div>
            <div className="caption-alignment-grid">{[[7, '↖'], [8, '↑'], [9, '↗'], [4, '←'], [5, '·'], [6, '→'], [1, '↙'], [2, '↓'], [3, '↘']].map(([value, label]) => <button key={value} type="button" aria-pressed={templateDraft.alignment === value} onClick={() => updateTemplateDraft('alignment', value)}>{label}</button>)}</div>
            <div className="style-summary"><span className="style-swatch" style={{ background: templateDraft.primary_color }} /><span>{templateDraft.font_name} {templateDraft.font_size}px</span></div>
            <div className="template-actions"><button type="button" onClick={() => void saveTemplateDraft()}>Save style</button><button type="button" onClick={applyTemplateToAll}>Apply to all captions</button></div>
            <div className="template-actions"><input aria-label="New template name" placeholder="New template name" value={templateName} onChange={event => setTemplateName(event.target.value)} /><button type="button" onClick={() => void saveTemplateAs()}>Save as template</button></div>
          </div>}<small className="inspector-note">Changes update this monitor immediately. Save style to use the same decoration at final render.</small></div>}
          <div className="inspector-tabs">{(['Inspector', 'Metadata'] as InspectorView[]).map(view => <button key={view} className={inspectorView === view ? 'active' : ''} aria-pressed={inspectorView === view} onClick={() => setInspectorView(view)}>{view}</button>)}</div>
          {selectedCaption && inspectorView === 'Inspector' ? <>
            {selectedCaption.text_status !== 'as_transcribed' && <button className="save-button edited-split-button" onClick={() => applySplitCaption(selectedCaption, Math.floor((selectedCaption.start_word_id + selectedCaption.end_word_id) / 2))}>Split edited caption</button>}
            <div className="inspector-header"><div><span className="eyebrow">CAPTION CLIP</span><h2>{selectedCaption.id}</h2></div><span className="status-chip">{selectedCaption.text_status}</span></div>
            <label className="field-label">Display text<textarea value={draftText} onChange={event => handleTextChange(event.target.value)} onBlur={saveTextImmediate} onKeyDown={event => { if (event.ctrlKey && event.key === 'Enter') { event.preventDefault(); saveTextImmediate(); } if (event.key === 'Escape') setDraftText(selectedCaption.display_text); }} /></label>
            <button className="save-button" onClick={saveTextImmediate}>Save caption <kbd>Ctrl ↵</kbd></button>
            <div className="inspector-section"><div className="section-title">Timing</div><div className="property-grid"><span>Start</span><strong>{selectedCaption.start.toFixed(2)}s</strong><span>End</span><strong>{selectedCaption.end.toFixed(2)}s</strong><span>Duration</span><strong>{(selectedCaption.end - selectedCaption.start).toFixed(2)}s</strong><span>Word range</span><strong>{selectedCaption.start_word_id}–{selectedCaption.end_word_id}</strong></div></div>
            <div className="inspector-section"><div className="section-title">Edit actions</div><div className="action-grid"><button disabled={selectedCaption.start_word_id === selectedCaption.end_word_id} onClick={() => applyCommand({ type: 'SplitCaption', caption_id: selected, after_word_id: Math.floor((selectedCaption.start_word_id + selectedCaption.end_word_id) / 2) })}>Split</button><button disabled={!rightCaption} onClick={() => rightCaption && applyCommand({ type: 'MergeCaptions', left_id: selected, right_id: rightCaption.id })}>Merge</button><button disabled={!rightCaption} onClick={() => rightCaption && applyCommand({ type: 'MoveBoundary', left_id: selected, right_id: rightCaption.id, boundary_word_id: selectedCaption.end_word_id - 1 })}>Trim −</button><button disabled={!rightCaption} onClick={() => rightCaption && applyCommand({ type: 'MoveBoundary', left_id: selected, right_id: rightCaption.id, boundary_word_id: selectedCaption.end_word_id + 1 })}>Trim +</button><button className="danger-action" onClick={() => void applyCommand({ type: 'DeleteCaption', caption_id: selectedCaption.id })}>Delete clip</button></div></div>
            <div className="inspector-nav"><button disabled={!leftCaption} onClick={() => leftCaption && seekToCaption(leftCaption)}>← Previous</button><button disabled={!rightCaption} onClick={() => rightCaption && seekToCaption(rightCaption)}>Next →</button></div>
          </> : selectedCaption && inspectorView === 'Metadata' ? <div className="metadata-panel"><div><span>Caption ID</span><strong>{selectedCaption.id}</strong></div><div><span>Source text</span><strong>{selectedCaption.source_text}</strong></div><div><span>Text status</span><strong>{selectedCaption.text_status}</strong></div><div><span>Word range</span><strong>{selectedCaption.start_word_id}–{selectedCaption.end_word_id}</strong></div></div> : <div className="empty-inspector"><span>◎</span><p>Select a caption clip to inspect and edit it.</p></div>}
        </aside>
      </div>

      <section className={`timeline-panel ${isResizing ? 'is-resizing' : ''}`} style={{ gridTemplateRows: 'auto 42px minmax(0, 1fr) 24px' }}>
        <div className="timeline-resizer" role="separator" aria-label="Resize timeline" onPointerDown={event => beginLayoutResize(event, 'timeline')} />
        <div className="edit-region-panel" style={{ padding: '8px 12px', borderBottom: '1px solid #343b47', background: '#171b22' }}>
          <div className="edit-region-heading" style={{ display: 'flex', gap: 12, alignItems: 'baseline', marginBottom: 6 }}><strong>Cut candidates</strong><span style={{ color: '#8995a6', fontSize: 11 }}>Highlighted ranges are kept. Other ranges start as delete areas.</span></div>
          <div className="edit-region-list" style={{ display: 'flex', gap: 6, overflowX: 'auto', paddingBottom: 2 }}>{editRegions.map(region => <button key={region.id} type="button" className={`edit-region-chip ${region.mode}`} onClick={() => void toggleEditRegion(region)} style={{ display: 'grid', gap: 2, minWidth: 150, textAlign: 'left', borderColor: region.mode === 'keep' ? '#3c855e' : '#9a4a4d', background: region.mode === 'keep' ? '#1c3828' : '#321e24' }}><strong>{region.mode === 'keep' ? 'KEEP' : 'DELETE'}</strong><span>{formatTime(region.start)} – {formatTime(region.end)}</span><small>{region.mode === 'keep' ? 'Click to mark for deletion' : 'Click to keep this range'}</small></button>)}</div>
        </div>
        <div className="timeline-toolbar"><div className="tool-cluster"><button className={`tool ${timelineTool === 'select' ? 'active' : ''}`} aria-pressed={timelineTool === 'select'} onClick={() => chooseTool('select')}>↖ Select</button><button className={`tool ${timelineTool === 'razor' ? 'active' : ''}`} aria-pressed={timelineTool === 'razor'} onClick={() => chooseTool('razor')}>✂ Razor</button><button className={`tool ${timelineTool === 'type' ? 'active' : ''}`} aria-pressed={timelineTool === 'type'} onClick={() => chooseTool('type')}>T Type</button><label className="snap-toggle"><input type="checkbox" checked={snap} onChange={event => setSnap(event.target.checked)} /> Snap</label></div><div className="sequence-name"><span className="sequence-dot" />{run || 'No sequence'} <small>· {project?.captions.length || 0} caption clips</small></div><div className="timeline-actions"><button onClick={() => setZoom(value => Math.max(24, value - 16))}>−</button><input aria-label="Timeline zoom" type="range" min="24" max="260" value={zoom} onChange={event => setZoom(+event.target.value)} /><button onClick={() => setZoom(value => Math.min(260, value + 16))}>＋</button><button onClick={() => { setZoom(Math.max(24, (timelineRef.current?.clientWidth || 900) / duration)); setScroll(0); }}>Fit</button></div></div>
        <div ref={timelineRef} className="timeline-scroll" onScroll={event => setScroll(event.currentTarget.scrollLeft)}>
          <div className="timeline-canvas" style={{ width: TRACK_HEADER_WIDTH + timelineWidth }}>
            <div className="timeline-ruler" style={{ left: TRACK_HEADER_WIDTH, width: timelineWidth }} onPointerDown={event => setPlayheadAt(event.clientX)}>{ticks.map(time => <div key={time} className="ruler-tick" style={{ left: time * zoom }}><span>{formatTime(time).slice(0, 5)}</span></div>)}</div>
            <div className="playhead-line" style={{ left: TRACK_HEADER_WIDTH + playhead * zoom }}><span className="playhead-handle" /></div>
            <div className="track-row word-track"><div className="track-header"><strong>W1</strong><span>WHISPER UNITS</span><small>word</small></div><div className="track-lane word-lane" style={{ width: timelineWidth }} onPointerDown={event => setPlayheadAt(event.clientX)}>{words.filter(word => word.timeline_end >= visibleStart - 2 && word.timeline_start <= visibleEnd + 2).map(word => { const wordCaption = activeCaptions.find(caption => word.word_id >= caption.start_word_id && word.word_id <= caption.end_word_id); const isBoundary = activeCaptions.some(caption => caption.start_word_id === word.word_id) || false; return <button key={word.word_id} type="button" className={`word-unit ${wordCaption?.id === selected ? 'in-caption' : ''} ${isBoundary ? 'caption-boundary' : ''}`} style={{ left: word.timeline_start * zoom, width: Math.max(14, (word.timeline_end - word.timeline_start) * zoom) }} onClick={event => { event.stopPropagation(); if (wordCaption) { setSelected(wordCaption.id); setDraftText(wordCaption.display_text); } seekVideo(word.timeline_start); }} title={`${word.word_id}: ${word.text} ${word.timeline_start.toFixed(2)}s–${word.timeline_end.toFixed(2)}s`}>{word.text}<i /></button>; })}</div></div>
            <div className="track-row video-track"><div className="track-header"><strong>V1</strong><span>CAPTIONS</span><small>◉ ◌</small></div><div className="track-lane" style={{ width: timelineWidth }} onPointerDown={event => setPlayheadAt(event.clientX)}>{visibleCaptions.map(caption => { const moveDelta = dragMove?.captionId === caption.id ? dragMove.delta : 0; const captionDuration = Math.max(0.001, caption.end - caption.start); const offset = captionOffset(caption); const boundaryWord = dragBoundary?.captionId === caption.id ? orderedWords.find(word => word.word_id === dragBoundary.wordId) : null; return <div key={caption.id} className={`timeline-clip ${caption.id === selected ? 'selected' : ''} ${dragMove?.captionId === caption.id ? 'dragging' : ''}`} style={{ left: (caption.start + moveDelta) * zoom, width: Math.max(18, (caption.end - caption.start) * zoom) }} onPointerDown={event => beginMove(event, caption)} onClick={event => handleCaptionClipClick(event, caption)} title={`${caption.display_text} · drag to move`}>
              {wordsForCaption(caption).map(word => { const wordStart = word.timeline_start + offset; const wordEnd = word.timeline_end + offset; return <span key={word.word_id} className="dom-word-unit" style={{ left: `${clamp((wordStart - caption.start) / captionDuration * 100, 0, 100)}%`, width: `${clamp((wordEnd - wordStart) / captionDuration * 100, 0, 100)}%` }}>{word.text}</span>; })}
              {boundaryWord && <span className="dom-drag-boundary" style={{ left: `${clamp((boundaryWord.timeline_end + offset - caption.start) / captionDuration * 100, 0, 100)}%` }} />}
              <span className="clip-handle clip-handle-start" onPointerDown={event => beginResize(event, caption, 'start')} /><span className="clip-kind">CC</span><span className="clip-label">{caption.display_text}</span><span className="clip-handle clip-handle-end" onPointerDown={event => beginResize(event, caption, 'end')} />
            </div>; })}</div></div>
            <div className="track-row audio-track"><div className="track-header"><strong>A1</strong><span>AUDIO</span><small>🔊</small></div><div className="track-lane audio-lane" style={{ width: timelineWidth }} onPointerDown={event => setPlayheadAt(event.clientX)}><div className="audio-label">SOURCE AUDIO</div><div className="waveform">{Array.from({ length: Math.ceil(timelineWidth / 18) }).map((_, index) => <i key={index} style={{ height: `${14 + ((index * 17) % 35)}%` }} />)}</div></div></div>
          </div>
        </div>
        <div className="timeline-status"><span>{visibleCaptions.length} visible clips</span><span>Selected: {selectedCaption?.display_text || 'none'}</span><span className="timeline-help">Drag clip body to move · drag edges for Whisper boundaries · Shift+Alt + ←/→ move · Alt + ←/→ boundary · Delete remove · S split</span></div>
      </section>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><AppShell /></React.StrictMode>);
