import AssetMarker from './AssetMarker';
import type { Position } from '../../api/sse';

interface FlightLayerProps {
  positions: Position[];
  onMarkerClick?: (position: Position) => void;
}

export default function FlightLayer({
  positions,
  onMarkerClick,
}: FlightLayerProps) {
  const flightPositions = positions.filter((p) => p.asset_type === 'AIRCRAFT');

  return (
    <>
      {flightPositions.map((pos) => (
        <AssetMarker
          key={pos.asset_id}
          position={pos}
          onMarkerClick={onMarkerClick}
        />
      ))}
    </>
  );
}
