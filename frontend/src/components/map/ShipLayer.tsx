import AssetMarker from './AssetMarker';
import type { Position } from '../../api/sse';

interface ShipLayerProps {
  positions: Position[];
  onMarkerClick?: (position: Position) => void;
}

export default function ShipLayer({
  positions,
  onMarkerClick,
}: ShipLayerProps) {
  const shipPositions = positions.filter((p) => p.asset_type === 'VESSEL');

  return (
    <>
      {shipPositions.map((pos) => (
        <AssetMarker
          key={pos.asset_id}
          position={pos}
          onMarkerClick={onMarkerClick}
        />
      ))}
    </>
  );
}
