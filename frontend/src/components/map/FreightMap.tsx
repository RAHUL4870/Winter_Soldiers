import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Map, MapProvider, NavigationControl } from '@vis.gl/react-maplibre';
import 'maplibre-gl/dist/maplibre-gl.css';

import RouteLayer from './RouteLayer';
import ShipLayer from './ShipLayer';
import TruckLayer from './TruckLayer';
import FlightLayer from './FlightLayer';
import { useSSEPositions, type Position } from '../../api/sse';
import type { FeatureCollection } from 'geojson';

export default function FreightMap() {
  const [selectedPosition, setSelectedPosition] = useState<Position | null>(null);

  // Fetch static routes
  const { data: routeData, isLoading: routesLoading } = useQuery({
    queryKey: ['map', 'routes'],
    queryFn: async () => {
      const res = await fetch('http://localhost:8000/api/map/routes', {
        credentials: 'include',
      });
      return res.json() as Promise<FeatureCollection>;
    },
    staleTime: 5 * 60 * 1000,
  });

  // Stream live positions
  const { positions, isConnected, error } = useSSEPositions();

  // Filter routes by mode
  const seaFeatures =
    routeData?.features?.filter((f: any) => f.properties?.mode === 'SEA') ?? [];
  const airFeatures =
    routeData?.features?.filter((f: any) => f.properties?.mode === 'AIR') ?? [];
  const roadFeatures =
    routeData?.features?.filter((f: any) => f.properties?.mode === 'ROAD') ?? [];

  const maptilerKey =
    import.meta.env.VITE_MAPTILER_API_KEY ||
    import.meta.env.VITE_MAPTILER_KEY ||
    '';

  const isMaptilerKeyValid =
    Boolean(maptilerKey) &&
    maptilerKey !== 'your_key_here' &&
    maptilerKey !== 'your_actual_key_here' &&
    !maptilerKey.includes('YOUR_MAPTILER');

  const mapStyleUrl = isMaptilerKeyValid
    ? `https://api.maptiler.com/maps/dataviz/style.json?key=${maptilerKey}`
    : 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json';

  return (
    <MapProvider>
      <div className="relative w-full h-[calc(100vh-64px)] bg-slate-950">
        <Map
          initialViewState={{
            longitude: 0,
            latitude: 20,
            zoom: 2,
          }}
          style={{
            position: 'absolute',
            top: 0,
            bottom: 0,
            width: '100%',
            height: '100%',
          }}
          mapStyle={mapStyleUrl}
          interactiveLayerIds={['sea-layer', 'air-layer', 'road-layer']}
        >
          <NavigationControl position="top-right" />

          {/* Route layers */}
          <RouteLayer
            sourceId="sea-routes"
            layerId="sea-layer"
            mode="SEA"
            features={seaFeatures}
          />
          <RouteLayer
            sourceId="road-routes"
            layerId="road-layer"
            mode="ROAD"
            features={roadFeatures}
          />
          <RouteLayer
            sourceId="air-routes"
            layerId="air-layer"
            mode="AIR"
            features={airFeatures}
          />

          {/* Live asset markers */}
          <ShipLayer positions={positions} onMarkerClick={setSelectedPosition} />
          <TruckLayer positions={positions} onMarkerClick={setSelectedPosition} />
          <FlightLayer positions={positions} onMarkerClick={setSelectedPosition} />
        </Map>

        {/* Routes loading overlay */}
        {routesLoading && (
          <div className="absolute top-4 left-4 z-10 bg-slate-900/80 text-slate-100 px-3 py-2 rounded text-sm shadow-lg pointer-events-none">
            Loading routes…
          </div>
        )}

        {/* Connection status */}
        <div className="absolute top-4 left-4 z-10 bg-slate-900/80 text-xs text-slate-100 px-3 py-2 rounded">
          {isConnected ? '🟢 Connected' : '🔴 Disconnected'}
          {error && <p className="text-red-400 mt-1">{error}</p>}
        </div>

        {/* Selected position detail */}
        {selectedPosition && (
          <div className="absolute bottom-4 left-4 bg-slate-900 text-white p-4 rounded shadow-lg max-w-xs z-20">
            <p className="text-sm">
              <strong>{selectedPosition.asset_type}</strong>
            </p>
            <p className="text-xs text-gray-400">{selectedPosition.asset_id}</p>
            <p className="text-xs mt-2">Lat: {selectedPosition.lat.toFixed(2)}</p>
            <p className="text-xs">Lon: {selectedPosition.lon.toFixed(2)}</p>
            {selectedPosition.speed_knots !== null && (
              <p className="text-xs">Speed: {selectedPosition.speed_knots.toFixed(1)} kn</p>
            )}
            <p className="text-xs text-gray-500 mt-1">
              {selectedPosition.provenance}
            </p>
            <button
              onClick={() => setSelectedPosition(null)}
              className="mt-3 text-xs bg-slate-700 px-2 py-1 rounded hover:bg-slate-600"
            >
              Close
            </button>
          </div>
        )}
      </div>
    </MapProvider>
  );
}