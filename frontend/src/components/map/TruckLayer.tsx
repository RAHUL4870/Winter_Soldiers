import AssetMarker from './AssetMarker';
import type { Position } from '../../api/sse';

interface TruckLayerProps {
  positions: Position[];
  onMarkerClick?: (position: Position) => void;
}

export default function TruckLayer({
  positions,
  onMarkerClick,
}: TruckLayerProps) {
  const truckPositions = positions.filter((p) => p.asset_type === 'TRUCK');

  return (
    <>
      {truckPositions.map((pos) => (
        <AssetMarker
          key={pos.asset_id}
          position={pos}
          onMarkerClick={onMarkerClick}
        />
      ))}
    </>
  );
}
