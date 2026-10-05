import { useEffect, useState } from 'react';
import { api, errorText } from './api';
import { Icon } from './Icon';
import { Button, StatusMessage } from './ui';

export type DataLocation = { data_dir: string; app_home: string; portable: boolean; writable: boolean; source: 'portable' | 'env' | 'custom' | 'fallback' };

export function DataDirectorySettings({ busy }: { busy: boolean }) {
  const [info, setInfo] = useState<DataLocation | null>(null);
  const [notice, setNotice] = useState('');
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    void api<DataLocation>('/api/data-location').then(value => { if (active) { setInfo(value); setFailed(false); setNotice(''); } })
      .catch(error => { if (active) { setFailed(true); setNotice(errorText(error)); } });
    return () => { active = false; };
  }, []);
  const sourceLabel = info?.source === 'env' ? '由 AVHUB_DATA_DIR 指定'
    : info?.source === 'custom' ? '已在桌面端自定义'
    : info?.source === 'fallback' ? '程序目录不可写，已回退到用户目录'
    : '程序所在目录（默认）';
  return <section className="settings-section data-location-settings" aria-label="数据保存目录">
    <h3><Icon name="database"/>数据文件保存目录</h3>
    <p className="data-location-description">媒体索引、封面、设置和观看记录保存在此目录。默认与程序同目录，便于随程序一起拷贝；视频原文件始终保留在原位置。</p>
    <div className="data-location-card">
      <code title={info?.data_dir}>{info?.data_dir ?? '正在读取…'}</code>
      <span className={`data-location-badge${info && !info.writable ? ' is-warning' : ''}`}>{sourceLabel}</span>
    </div>
    <div className="data-location-actions">
      <Button icon="reveal" disabled={busy || !info} onClick={() => void api('/api/data-location/reveal', { method: 'POST' })
        .then(() => setNotice('已在资源管理器中打开数据目录'))
        .catch(error => { setFailed(true); setNotice(errorText(error)); })}>打开数据目录</Button>
      <Button icon="copy" disabled={busy || !info} onClick={() => void navigator.clipboard.writeText(info!.data_dir)
        .then(() => { setFailed(false); setNotice('数据目录路径已复制'); })
        .catch(() => { setFailed(true); setNotice('当前环境不允许复制，请手动选择文本'); })}>复制路径</Button>
    </div>
    {window.avhubDesktop
      ? <small>桌面版可直接切换目录：关闭 MP4Hub 后，把整个数据目录移动到新位置，再删除旧的 <code>portable-data.txt</code> 并重新启动；程序会优先使用同目录下的 <code>data</code>。切换前请先在“数据管理”中做一次完整备份。</small>
      : <small>浏览器版读取服务端目录。设置环境变量 <code>AVHUB_DATA_DIR</code> 可以指定其他位置，改动后需要重启服务。</small>}
    {notice && <StatusMessage kind={failed ? 'error' : 'info'}>{notice}</StatusMessage>}
  </section>;
}
