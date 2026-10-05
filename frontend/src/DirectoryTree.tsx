import { useEffect, useMemo, useState } from 'react';
import { api, errorText, type FolderTree, type FolderTreeEntry, type FolderTreeRoot } from './api';
import { Icon } from './Icon';
import { Button, StatusMessage } from './ui';

type Node = FolderTreeEntry & { children: Node[]; mediaRoot?: boolean };

/**
 * Build the full hierarchy the sidebar shows.
 *
 * Every media directory is a level-one node named after its folder, so adding
 * 电影 and 电视剧 yields two top-level branches. Indexed sub-folders are then
 * nested by their relative path: 电影 → 国语 → 2005. Counts come straight from
 * the server and already include descendants. A media directory is modelled as
 * a folder entry with an empty relative path and depth -1, which keeps one node
 * shape for rendering and selection.
 */
function buildTree(roots: FolderTreeRoot[], folders: FolderTreeEntry[]): Node[] {
  const branches: Node[] = roots.map(root => ({
    root_id: root.id, name: root.name, folder: '', depth: -1,
    count: root.count, direct_count: root.direct_count, children: [], mediaRoot: true,
  }));
  const byId = new Map(branches.map(branch => [branch.root_id, branch]));
  // Index each folder node so a child can find its parent regardless of the
  // order the server returns rows in.
  const nodes = new Map<string, Node>();
  for (const entry of folders) {
    const node: Node = { ...entry, children: [] };
    nodes.set(`${entry.root_id}\u0000${entry.folder}`, node);
    const separator = entry.folder.lastIndexOf('/');
    const parent = separator === -1
      ? byId.get(entry.root_id)
      : nodes.get(`${entry.root_id}\u0000${entry.folder.slice(0, separator)}`);
    // A folder whose parent row is missing (should not happen) still surfaces
    // under its media directory instead of disappearing.
    (parent ?? byId.get(entry.root_id))?.children.push(node);
  }
  return branches;
}

function DirectoryNode({ node, selected, expand, toggle }: {
  node: Node; selected: (node: Node) => boolean;
  expand: (key: string, open: boolean) => void; toggle: (entry: Node) => void;
}) {
  const [open, setOpen] = useState(true);
  const key = `${node.root_id}\u0000${node.folder}`;
  useEffect(() => { expand(key, open); }, [key, open, expand]);
  const active = selected(node);
  // Media directories sit at level one; sub-folders indent by their depth.
  const level = node.mediaRoot ? 0 : node.depth + 1;
  return <li>
    <div className={`directory-node${active ? ' is-active' : ''}${node.mediaRoot ? ' is-media-root' : ''}`}
      style={{ paddingLeft: `${8 + level * 14}px` }}>
      {node.children.length
        ? <button className="directory-twist" aria-label={open ? `收起 ${node.name}` : `展开 ${node.name}`} aria-expanded={open}
            onClick={() => setOpen(value => !value)}><Icon name={open ? 'chevronDown' : 'chevronRight'} size={13}/></button>
        : <span className="directory-twist is-leaf" aria-hidden="true"/>}
      <button className="directory-label" aria-current={active ? 'true' : undefined}
        title={node.mediaRoot ? `媒体目录 · 含子目录 ${node.count} 个视频` : `${node.folder} · 含子目录 ${node.count} 个视频`}
        onClick={() => toggle(node)}>
        {node.mediaRoot && <Icon name="folder" size={13}/>}
        <span>{node.name}</span><small>{node.count}</small></button>
    </div>
    {open && node.children.length > 0 && <ul>{node.children.map(child => <DirectoryNode key={`${child.root_id}\u0000${child.folder}`}
      node={child} selected={selected} expand={expand} toggle={toggle}/>)}</ul>}
  </li>;
}

export function DirectoryTree({ root, folder, select, rootId, changeRoot, revision, total }: {
  root: string; folder: string; rootId: (id: number) => void;
  select: (entry: FolderTreeEntry | null) => void; changeRoot: (root: string) => void;
  revision: number; total: number;
}) {
  const [tree, setTree] = useState<FolderTree | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [retry, setRetry] = useState(0);
  // Collapsed keys survive re-fetch so a scan or reload does not reset the view.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    void api<FolderTree>('/api/folders/tree', { signal: controller.signal }).then(value => {
      if (!controller.signal.aborted) setTree(value);
    }).catch(e => { if (!controller.signal.aborted) setError(errorText(e)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision, retry]);
  const expand = (key: string, open: boolean) => setCollapsed(current => {
    if (open === !current.has(key)) return current;
    const next = new Set(current);
    if (open) next.delete(key); else next.add(key);
    return next;
  });
  const nodes = useMemo(() => {
    const all = buildTree(tree?.roots ?? [], tree?.folders ?? []);
    if (!query.trim()) return all;
    // Keep the ancestor chain of every match so results stay reachable.
    const needle = query.trim().toLocaleLowerCase();
    const keep = (node: Node): Node | null => {
      const children = node.children.map(keep).filter((value): value is Node => value !== null);
      if (children.length || node.name.toLocaleLowerCase().includes(needle)) return { ...node, children };
      return null;
    };
    // A media directory survives when its own name matches or a descendant does.
    return all.map(branch => keep(branch)).filter((value): value is Node => value !== null);
  }, [tree, query]);
  const selectedKey = folder;
  const isSelected = (entry: FolderTreeEntry) => entry.folder === selectedKey && String(entry.root_id) === root;
  // A media directory is active when it is the scope and no folder filter is set.
  const isMediaRoot = (node: Node) => Boolean(node.mediaRoot) && !folder && String(node.root_id) === root;
  const selected = (node: Node) => node.mediaRoot ? isMediaRoot(node) : isSelected(node);
  const toggle = (entry: Node) => {
    // Selecting the already-active folder returns to 全部, which is the only way
    // back without a separate reset control. Clicking the active media directory
    // likewise clears the scope back to every root.
    if (selected(entry)) { changeRoot(''); select(null); return; }
    rootId(entry.root_id); select(entry);
  };
  return <aside className="directory-sidebar" aria-label="目录结构">
    <div className="directory-heading"><Icon name="folder" size={15}/><b>目录结构</b>
      <button aria-label="刷新目录结构" title="刷新目录结构" disabled={loading} onClick={() => setRetry(value => value + 1)}>
        <Icon name="refresh" size={14} className={loading ? 'is-spinning' : undefined}/></button></div>
    <input className="directory-search" type="search" aria-label="搜索目录" placeholder="搜索目录…"
      value={query} onChange={event => setQuery(event.target.value)}/>
    <ul className="directory-tree">
      <li><div className={`directory-node is-all${!folder && !root ? ' is-active' : ''}`}>
        <span className="directory-twist is-leaf" aria-hidden="true"/>
        <button className="directory-label" aria-current={!folder && !root ? 'true' : undefined}
          onClick={() => { changeRoot(''); select(null); }}>
          <span>全部</span><small>{tree?.total ?? total}</small></button>
      </div></li>
      {nodes.map(node => <DirectoryNode key={`${node.root_id}\u0000${node.folder}`} node={node}
        selected={selected} expand={expand} toggle={toggle}/>)}
    </ul>
    {tree && tree.roots.length > 1 && <label className="directory-root-scope">目录范围
      <select aria-label="目录范围" value={root} onChange={event => { changeRoot(event.target.value); select(null); }}>
        <option value="">全部媒体目录</option>
        {tree.roots.map(entry => <option key={entry.id} value={entry.id}>{entry.name}（{entry.count}）</option>)}
      </select></label>}
    {error ? <StatusMessage kind="error">{error}<Button icon="refresh" onClick={() => setRetry(value => value + 1)}>重试</Button></StatusMessage> :
      !loading && !tree?.roots.length ? <p className="directory-empty">还没有添加媒体目录。点击“添加媒体目录”后，这里会按层级显示每个目录及其子目录。</p> :
      <small className="directory-hint">{folder ? '再次点击当前目录可返回“全部”。' : root ? '再次点击媒体目录可返回“全部”。' : '点击媒体目录或子目录，只显示该范围（含子目录）下的视频。'}</small>}
  </aside>;
}
