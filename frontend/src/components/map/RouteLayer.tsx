import { Source, Layer } from '@vis.gl/react-maplibre';
import type { Feature } from 'geojson';

interface RouteLayerProps {
  sourceId: string;
  layerId: string;
  mode: 'SEA' | 'ROAD' | 'AIR';
  features: Feature[];
}

export default function RouteLayer({
  sourceId,
  layerId,
  mode,
  features,
}: RouteLayerProps) {
  // Style config by mode
  const paint = (() => {
    if (mode === 'SEA') {
      return {
        'line-color': '#0066cc',
        'line-width': 2.5,
        'line-opacity': 0.7,
        'line-dasharray': [5, 5],
      };
    }
    if (mode === 'AIR') {
      return {
        'line-color': '#ff9900',
        'line-width': 2.5,
        'line-opacity': 0.7,
      };
    }
    // ROAD
    return {
      'line-color': '#00cc00',
      'line-width': 2.5,
      'line-opacity': 0.7,
    };
  })();

  return (
    <>
      <Source
        id={sourceId}
        type="geojson"
        data={{
          type: 'FeatureCollection',
          features,
        }}
      />
      <Layer
        id={layerId}
        type="line"
        source={sourceId}
        paint={paint}
        layout={{
          'line-join': 'round',
          'line-cap': 'round',
        }}
      />
    </>
  );
}
