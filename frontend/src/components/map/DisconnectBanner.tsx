interface DisconnectBannerProps {
  error: string | null;
  onReconnect: () => void;
}

export default function DisconnectBanner({
  error,
  onReconnect,
}: DisconnectBannerProps) {
  return (
    <div className="absolute top-4 right-4 z-20 bg-red-900/90 text-white px-4 py-3 rounded shadow-lg flex items-center gap-3">
      <span className="text-sm font-medium">{error || 'Position feed disconnected'}</span>
      <button
        onClick={onReconnect}
        className="text-xs bg-red-700 hover:bg-red-600 px-3 py-1 rounded transition-colors"
      >
        Reconnect
      </button>
    </div>
  );
}
