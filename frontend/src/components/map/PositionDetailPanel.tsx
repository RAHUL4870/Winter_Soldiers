import type { Position } from '../../api/sse';

interface PositionDetailPanelProps {
  position: Position;
  onClose: () => void;
}

export default function PositionDetailPanel({
  position,
  onClose,
}: PositionDetailPanelProps) {
  const assetTypeLabel = {
    VESSEL: 'Vessel',
    AIRCRAFT: 'Aircraft',
    TRUCK: 'Truck',
  }[position.asset_type];

  const lastUpdate = new Date(position.reported_at).toLocaleTimeString();

  return (
    <div className="absolute right-0 top-0 bottom-0 w-80 bg-slate-900 border-l border-slate-700 shadow-2xl z-30 p-4 overflow-y-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-slate-100">
          {assetTypeLabel}
        </h3>
        <button
          onClick={onClose}
          className="text-slate-400 hover:text-slate-200 text-xl leading-none"
        >
          ✕
        </button>
      </div>

      {/* Details */}
      <div className="space-y-3 text-sm text-slate-300">
        <div>
          <div className="text-xs uppercase text-slate-500 font-semibold">
            Asset ID
          </div>
          <div className="text-slate-100 font-mono">{position.asset_id}</div>
        </div>

        <div>
          <div className="text-xs uppercase text-slate-500 font-semibold">
            Position
          </div>
          <div className="text-slate-100">
            {position.lat.toFixed(4)}°, {position.lon.toFixed(4)}°
          </div>
        </div>

        <div>
          <div className="text-xs uppercase text-slate-500 font-semibold">
            Speed
          </div>
          <div className="text-slate-100">
            {position.speed_knots !== null
              ? `${position.speed_knots.toFixed(1)} knots`
              : 'N/A'}
          </div>
        </div>

        <div>
          <div className="text-xs uppercase text-slate-500 font-semibold">
            Heading
          </div>
          <div className="text-slate-100">
            {position.heading_deg !== null
              ? `${position.heading_deg.toFixed(0)}°`
              : 'N/A'}
          </div>
        </div>

        <div>
          <div className="text-xs uppercase text-slate-500 font-semibold">
            Last Update
          </div>
          <div className="text-slate-100">{lastUpdate}</div>
        </div>

        <div>
          <div className="text-xs uppercase text-slate-500 font-semibold">
            Provenance
          </div>
          <div className="inline-block mt-1">
            <span className="px-2 py-1 rounded text-xs font-semibold bg-slate-800 text-slate-100">
              {position.provenance}
            </span>
          </div>
        </div>

        <div>
          <div className="text-xs uppercase text-slate-500 font-semibold">
            Source
          </div>
          <div className="text-slate-100">{position.source}</div>
        </div>
      </div>

      {/* Footer note */}
      <div className="mt-6 p-3 bg-slate-800/50 rounded border border-slate-700 text-xs text-slate-400">
        <strong>Note:</strong> Full shipment context and rerouting options available from the Alerts view.
      </div>
    </div>
  );
}
