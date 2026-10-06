import { useEffect, useRef, useState } from 'react';
import { api, json, errorText, type Media, type MediaUpdate } from './api';
import { Icon } from './Icon';
import { Dialog } from './ui';

type DeleteMode = 'recycle' | 'permanent';

export function MediaActions({media,update,changed,notify}: {
  media:Media;update:(value:MediaUpdate)=>void;changed?:()=>void;notify:(message:string)=>void;
}) {
  const [open,setOpen]=useState(false);
  const [busy,setBusy]=useState(false);
  const [confirming,setConfirming]=useState(false);
  const lock=useRef(false);
  const root=useRef<HTMLDivElement>(null);
  const trigger=useRef<HTMLButtonElement>(null);
  useEffect(()=>{
    if(!open)return;
    const outside=(e:PointerEvent)=>{if(!root.current?.contains(e.target as Node))setOpen(false);};
    const escape=(e:KeyboardEvent)=>{if(e.key==='Escape'){e.stopPropagation();setOpen(false);trigger.current?.focus();}};
    document.addEventListener('pointerdown',outside);document.addEventListener('keydown',escape,true);
    root.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    return ()=>{document.removeEventListener('pointerdown',outside);document.removeEventListener('keydown',escape,true);};
  },[open]);
  async function act(action:'watched'|'auto'|'copy'|'reveal'|'open') {
    if(lock.current)return;
    if(action==='open' && !window.confirm('将用系统默认播放器打开原视频。MP4Hub 无法同步外部播放器的进度、音轨和字幕。继续？'))return;
    lock.current=true;setBusy(true);
    try {
      if(action==='watched'||action==='auto') {
        const value=await api<Media>(`/api/media/${media.id}/watched`,action==='auto'?{method:'DELETE'}:json('PUT',{watched:!media.watched}));
        update(value);changed?.();notify(action==='auto'?'已恢复自动判断观看状态':value.watched?'已标记为已看':'已标记为未看');
      } else if(action==='copy') {await navigator.clipboard.writeText(media.path);notify('视频路径已复制');}
      else {
        if(window.avhubDesktop?.mediaAction)await window.avhubDesktop.mediaAction(media.id,action);
        else await api(`/api/media/${media.id}/native/${action}`,{method:'POST'});
        notify(action==='reveal'?'已在资源管理器中定位视频':'已请求系统播放器打开原片；外部播放进度不会同步');
      }
      setOpen(false);
    } catch(e){notify(errorText(e));}
    finally {lock.current=false;setBusy(false);}
  }
  async function remove(mode:DeleteMode) {
    if(lock.current)return;
    lock.current=true;setBusy(true);
    try {
      if(window.avhubDesktop?.deleteMedia)await window.avhubDesktop.deleteMedia(media.id,mode);
      else await api(`/api/media/${media.id}/delete`,json('POST',{mode}));
      setConfirming(false);setOpen(false);
      changed?.();
      notify(mode==='permanent'?'视频已彻底删除，已从媒体库移除':'视频已移入回收站，已从媒体库移除');
    } catch(e){notify(errorText(e));}
    finally {lock.current=false;setBusy(false);}
  }
  return <div className="media-actions" ref={root}>
    <button ref={trigger} className="media-more" aria-label={`更多操作 ${media.title}`} title="更多操作" aria-haspopup="menu" aria-expanded={open} onClick={()=>setOpen(value=>!value)}>
      <Icon name="more"/>
    </button>
    {open && <div className="media-menu" role="menu" aria-label={`视频操作 ${media.title}`} onKeyDown={event=>{
      if(!['ArrowDown','ArrowUp','Home','End'].includes(event.key))return;
      event.preventDefault();
      const items=Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'));
      const index=items.indexOf(document.activeElement as HTMLButtonElement);
      const next=event.key==='Home'?0:event.key==='End'?items.length-1:(index+(event.key==='ArrowDown'?1:-1)+items.length)%items.length;
      items[next]?.focus();
    }}>
      <button role="menuitem" disabled={busy} onClick={()=>void act('watched')}><Icon name={media.watched?'eyeOff':'check'} size={16}/>{media.watched?'标记为未看':'标记为已看'}</button>
      {media.manual_watched!==null && media.manual_watched!==undefined && <button role="menuitem" disabled={busy} onClick={()=>void act('auto')}><Icon name="refresh" size={16}/>恢复自动已看判断</button>}
      <button role="menuitem" disabled={busy} onClick={()=>void act('copy')}><Icon name="copy" size={16}/>复制视频路径</button>
      <button role="menuitem" disabled={busy||Boolean(media.missing)} onClick={()=>void act('reveal')}><Icon name="reveal" size={16}/>在资源管理器中显示</button>
      <button role="menuitem" disabled={busy||Boolean(media.missing)} onClick={()=>void act('open')}><Icon name="external" size={16}/>用系统播放器打开</button>
      <button role="menuitem" className="danger" disabled={busy} onClick={()=>{setOpen(false);setConfirming(true);}}><Icon name="trash" size={16}/>删除视频…</button>
      <small>删除是唯一会改动原文件的操作</small>
    </div>}
    {confirming && <DeleteDialog media={media} busy={busy} close={()=>{if(!busy)setConfirming(false);}} onConfirm={mode=>void remove(mode)}/>}
  </div>;
}

function DeleteDialog({media,busy,close,onConfirm}: {
  media:Media;busy:boolean;close:()=>void;onConfirm:(mode:DeleteMode)=>void;
}) {
  const [mode,setMode]=useState<DeleteMode>('recycle');
  const missing=Boolean(media.missing);
  return <Dialog label="删除视频" closeLabel="取消删除" busy={busy} close={close} className="modal delete-dialog">
    <h2><Icon name="warning" size={18}/>删除视频</h2>
    <p className="delete-target">{media.title}</p>
    <p className="delete-note">{missing
      ? '该文件当前已离线，仍会从媒体库中移除这条记录。'
      : '将删除原视频文件，并从媒体库中移除这条记录。此操作无法在应用内撤销。'}</p>
    <fieldset className="delete-modes" disabled={busy}>
      <legend>选择删除方式</legend>
      <label className={mode==='recycle'?'selected':''}>
        <input type="radio" name="delete-mode" value="recycle" checked={mode==='recycle'} onChange={()=>setMode('recycle')}/>
        <span><strong>移到回收站</strong><small>{missing?'记录直接移除':'可从系统回收站还原'}</small></span>
      </label>
      <label className={mode==='permanent'?'selected':''}>
        <input type="radio" name="delete-mode" value="permanent" checked={mode==='permanent'} onChange={()=>setMode('permanent')}/>
        <span><strong>彻底删除</strong><small>不移入回收站，无法恢复</small></span>
      </label>
    </fieldset>
    <div className="delete-buttons">
      <button type="button" className="ui-button" disabled={busy} onClick={close}>取消</button>
      <button type="button" className={`ui-button ${mode==='permanent'?'danger-action':'primary'}`} disabled={busy} aria-busy={busy||undefined} onClick={()=>onConfirm(mode)}>
        {busy&&<Icon name="refresh" size={16} className="is-spinning"/>}{mode==='permanent'?'彻底删除':'移到回收站'}
      </button>
    </div>
  </Dialog>;
}
