export const formatLabel = (extension: string) => extension.replace(/^\./, '').toUpperCase() || '未知格式';
export const resolutionLabel = (width?: number, height?: number) => width && height ? `${width}×${height}` : '分辨率未知';
export const episodeLabel = ({ season, episode }: { season?: number; episode?: number }) => `${season === 0 ? '特别篇' : season != null ? `第 ${season} 季` : '季未设置'} · ${episode != null ? `第 ${episode} 集` : '集未设置'}`;

/** Byte counts shared by the cover grid, storage report and diagnostics. */
export const sizeLabel = (bytes: number) =>
  bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(2)} GB`
    : bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB`
      : `${(bytes / 1024).toFixed(1)} KB`;

/**
 * Compact size for a cover card. Returns null when the index has no usable size
 * (legacy rows scanned before the column was populated, or an offline source),
 * so callers can omit the segment instead of printing "0.0 KB".
 */
export const mediaSizeLabel = (bytes?: number | null) =>
  typeof bytes === 'number' && Number.isFinite(bytes) && bytes > 0 ? sizeLabel(bytes) : null;
