import { Marker } from '@vis.gl/react-maplibre';
import type { Position } from '../../api/sse';

interface AssetMarkerProps {
  position: Position;
  onMarkerClick?: (position: Position) => void;
}

export default function AssetMarker({
  position,
  onMarkerClick,
}: AssetMarkerProps) {
  // Icon emoji by asset type
  const iconMap = {
    VESSEL: '⛴️',
    AIRCRAFT: '✈️',
    TRUCK: '🚛',
  };

  // Badge styling by provenance
  const badgeConfig = {
    REAL: null, // No badge
    REPLAYED: { bg: 'bg-gray-500', text: 'text-white', label: 'R' },
    SIMULATED: { bg: 'bg-yellow-400', text: 'text-gray-800', label: 'S' },
    MOCK: { bg: 'bg-purple-300', text: 'text-gray-800', label: 'M' },
  };

  const badge = badgeConfig[position.provenance];
  const icon = iconMap[position.asset_type];

  // Heading rotation (default to 0 if null)
  const rotation = position.heading_deg ?? 0;

  return (
    <Marker
      longitude={position.lon}
      latitude={position.lat}
      anchor="center"
      onClick={(e: any) => {
        e.originalEvent.stopPropagation();
        onMarkerClick?.(position);
      }}
    >
      <div
        className="relative w-8 h-8 flex items-center justify-center cursor-pointer"
        title={`${position.asset_type} ${position.asset_id}`}
      >
        {/* Icon with heading rotation */}
        <div
          className="text-2xl transition-transform duration-500 ease-in-out select-none"
          style={{
            transform: `rotate(${rotation}deg)`,
          }}
        >
          {icon}
        </div>

        {/* Provenance badge (if applicable) */}
        {badge && (
          <span
            className={`absolute -bottom-1 -right-1 w-5 h-5 rounded-full ${badge.bg} ${badge.text} text-xs font-bold flex items-center justify-center shadow border border-gray-900`}
          >
            {badge.label}
          </span>
        )}
      </div>
    </Marker>
  );
}
