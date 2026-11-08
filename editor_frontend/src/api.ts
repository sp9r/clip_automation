export type Caption={id:string;start_word_id:number;end_word_id:number;start:number;end:number;source_text:string;display_text:string;text_status:string;template_id:string;position_offset?:number;deleted?:boolean}
export type TranscriptWord={word_id:number;text:string;timeline_start:number;timeline_end:number;source_start?:number;source_end?:number;probability?:number|null}
export type Project={revision:number;captions:Caption[];output?:{width?:number;height?:number};default_template_id?:string}
export type CaptionTemplate={id:string;name:string;font_name:string;font_size:number;primary_color:string;outline_color:string;bold:boolean;italic:boolean;outline:number;shadow:number;shadow_color?:string;background_color?:string;background_opacity?:number;border_style?:number;underline?:boolean;strikeout?:boolean;scale_x?:number;scale_y?:number;letter_spacing?:number;angle?:number;alignment:number;margin_v:number;position_x?:number|null;position_y?:number|null;user_defined?:boolean;[key:string]:unknown}
export type Highlight={start:number;end:number;reason?:string}
export type EditRegion={id:string;start:number;end:number;mode:'keep'|'delete';default_keep?:boolean}
export type Run={run_id:string;state:'uploading'|'queued'|'not_ready'|'preparing'|'ready'|'needs_review'|'invalid'|'locked'|'failed';timeline_ready:boolean;source_ready:boolean;proxy_ready:boolean}
export type ProjectPreset={id:string;name:string;output_width:number;output_height:number;output_fps:number;max_chars:number;segmentation_mode:'llm'|'heuristic';caption_model:string;default_template_id:string;video_encoder:string}
export type CreationStatus={run_id?:string;state:string;stage?:string;progress?:number;message?:string;error?:string;project_name?:string;source_name?:string}
const API=(import.meta.env.VITE_API_URL||window.location.origin).replace(/\/$/,'')
async function call<T>(path:string,init?:RequestInit):Promise<T>{const response=await fetch(API+path,{headers:{'Content-Type':'application/json'},...init});if(!response.ok){const body=await response.json().catch(()=>({detail:response.statusText}));throw new Error(typeof body.detail==='string'?body.detail:JSON.stringify(body.detail))}return response.json()}
export const api={
  runs:()=>call<Run[]>('/api/runs'),
  presets:()=>call<ProjectPreset[]>('/api/project-presets'),
  createProject:(file:File,projectName:string,presetId:string,useLlm:boolean)=>call<{run_id:string;state:string;bytes:number}>('/api/projects',{method:'POST',headers:{'Content-Type':file.type||'application/octet-stream','X-File-Name':encodeURIComponent(file.name),'X-Project-Name':encodeURIComponent(projectName),'X-Preset-Id':presetId,'X-Use-Llm':String(useLlm)},body:file}),
  creationStatus:(id:string)=>call<CreationStatus>(`/api/runs/${id}/creation-status`),
  prepare:(id:string)=>call<{state:string;word_count:number;caption_count:number;first_mismatch?:unknown}>(`/api/runs/${id}/prepare-timeline`,{method:'POST'}),
  project:(id:string)=>call<{project:Project;words:TranscriptWord[];word_count:number;media_url:string|null;source_media_url:string|null;rendered_media_url:string|null;highlights:Highlight[];edit_regions:EditRegion[]}>(`/api/projects/${id}`),
  captionTemplates:()=>call<CaptionTemplate[]>('/api/subtitle-templates'),
  saveCaptionTemplate:(template:Partial<CaptionTemplate>)=>call<CaptionTemplate>('/api/subtitle-templates',{method:'POST',body:JSON.stringify(template)}),
  editRegions:(id:string,regions:EditRegion[])=>call<{regions:EditRegion[]}>(`/api/projects/${id}/edit-regions`,{method:'POST',body:JSON.stringify({regions})}),
  timeline:(id:string,start:number,end:number)=>call<{captions:Caption[]}>(`/api/projects/${id}/timeline?start=${start}&end=${end}`),
  command:(id:string,body:Record<string,unknown>)=>call<Project>(`/api/projects/${id}/commands`,{method:'POST',body:JSON.stringify(body)}),
  undo:(id:string)=>call<Project>(`/api/projects/${id}/undo`,{method:'POST'}),
  redo:(id:string)=>call<Project>(`/api/projects/${id}/redo`,{method:'POST'}),
  render:(id:string,start:number,end:number)=>call<{revision:number;media_url:string;source_media_url:string;start:number;end:number;rendered:boolean}>(`/api/projects/${id}/render-preview`,{method:'POST',body:JSON.stringify({start,end})}),
  export:(id:string,revision:number,filename:string)=>call<{status:string;revision:number;media_url:string;download_url?:string;filename?:string}>(`/api/projects/${id}/exports`,{method:'POST',body:JSON.stringify({revision,filename})}),
  media:(path:string)=>API+path,
}
