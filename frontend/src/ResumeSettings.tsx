import { Icon } from './Icon';
import { changeResumeMode, useResume, type ResumeMode } from './resume';

const options: { id: ResumeMode; label: string; hint: string }[] = [
  { id: 'restart', label: '从头播放', hint: '每次打开都从 0:00 开始，忽略已保存的进度' },
  { id: 'resume', label: '接上次进度', hint: '自动从上次看到的位置继续，不提示' },
  { id: 'ask', label: '每次询问', hint: '打开时提示“继续播放 / 从头开始”，由你选择' },
];

export function ResumeSettings({ busy }: { busy: boolean }) {
  const { mode } = useResume();
  return <section className="settings-section resume-settings" aria-label="续播方式设置">
    <h3><Icon name="continue"/>续播方式</h3>
    <p className="resume-description">控制已看过的视频再次打开时的起播位置。已看完的视频始终从头播放。</p>
    <div className="resume-options" role="radiogroup" aria-label="续播方式">
      {options.map(option => <label key={option.id} className={mode === option.id ? 'is-active' : ''}>
        <input type="radio" name="resume-mode" aria-label={option.label} disabled={busy}
          checked={mode === option.id} onChange={() => changeResumeMode(option.id)} />
        <span><b>{option.label}</b><small>{option.hint}</small></span>
      </label>)}
    </div>
    <small>设置自动保存。无论选择哪种方式，都可以在播放页用“重新播放”从头开始，也可以在播放信息中查看已保存的进度。</small>
  </section>;
}
