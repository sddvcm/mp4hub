import { useState } from 'react';
import { CLIENT_BUILD } from './api';
import { Button, Dialog } from './ui';
import { Icon } from './Icon';

const REPOSITORY = 'https://github.com/sddvcm/mp4hub';

/** Brand mark inlined so the About panel needs no extra static route. */
function BrandMark({ size = 64 }: { size?: number }) {
  return <svg className="about-logo" viewBox="0 0 64 64" width={size} height={size} role="img" aria-label="MP4Hub">
    <rect width="64" height="64" rx="14" fill="var(--ui-surface)"/>
    <rect x="16" y="14" width="34" height="26" rx="5" fill="var(--ui-accent-hover)" opacity="0.55"/>
    <rect x="13" y="20" width="38" height="29" rx="6" fill="var(--ui-accent)"/>
    <path d="M29 28.5v12l10-6z" fill="var(--ui-on-accent)"/>
  </svg>;
}

/**
 * About panel: project identity, version metadata and the upstream repository.
 *
 * The repository link is rendered as an anchor so the browser build can open it
 * directly; the Electron shell outsources the click through its existing
 * window-open handler, which routes external URLs to the system browser.
 */
export function AboutSettings() {
  const [copied, setCopied] = useState('');
  return <>
    <AboutBody copied={copied} changeCopied={setCopied}/>
  </>;
}

/**
 * About as a modal, opened from the titlebar. Reuses the same body so the
 * version/build/repository text cannot drift between the two entry points.
 */
export function AboutDialog({ close }: { close: () => void }) {
  const [copied, setCopied] = useState('');
  return <Dialog labelledBy="about-title" closeLabel="关闭关于" busy={false} close={close} className="modal about-modal">
    <div className="about-scroll" id="about-title">
      <AboutBody copied={copied} changeCopied={setCopied}/>
    </div>
  </Dialog>;
}

function AboutBody({ copied, changeCopied }: { copied: string; changeCopied: (value: string) => void }) {
  async function copy(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      changeCopied(label);
      window.setTimeout(() => changeCopied(''), 2000);
    } catch {
      // Clipboard permission can be denied; the link stays selectable by hand.
      changeCopied('');
    }
  }
  return <section className="settings-section about-section" aria-label="关于">
    <div className="about-identity">
      <BrandMark/>
      <div>
        <h3><Icon name="info"/>MP4Hub</h3>
        <p className="about-tagline">本地离线视频库与播放器{CLIENT_BUILD.version ? ` · 版本 ${CLIENT_BUILD.version}` : ''}</p>
      </div>
    </div>
    <dl className="about-meta">
      <div><dt>版本</dt><dd><strong className="about-version">{CLIENT_BUILD.version || '开发版'}</strong></dd></div>
      <div><dt>构建标识</dt><dd><code>{CLIENT_BUILD.build_id || '未构建'}</code></dd></div>
      <div><dt>构建时间</dt><dd>{CLIENT_BUILD.built_at ? new Date(CLIENT_BUILD.built_at).toLocaleString() : '—'}</dd></div>
      <div><dt>接口协议</dt><dd>{CLIENT_BUILD.api_protocol ?? '—'}</dd></div>
    </dl>
    <div className="about-links">
      <a className="ui-button" href={REPOSITORY} target="_blank" rel="noreferrer noopener">
        <Icon name="reveal" size={16}/>项目主页 · GitHub</a>
      <Button onClick={() => void copy(REPOSITORY, 'repo')}>{copied === 'repo' ? '已复制地址' : '复制地址'}</Button>
    </div>
    <p className="about-repo"><code>{REPOSITORY}</code></p>
    <small className="about-note">本项目基于开源代码二次修改，标注版本与构建标识便于对照问题来源。数据与视频均保存在本机，不会上传。</small>
  </section>;
}
