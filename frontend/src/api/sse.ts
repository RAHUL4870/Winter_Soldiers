import { useEffect, useState, useRef } from 'react';
import { useAuthStore } from '../store/authStore';

export interface Position {
  asset_id: string;
  asset_type: 'VESSEL' | 'AIRCRAFT' | 'TRUCK';
  lat: number;
  lon: number;
  speed_knots: number | null;
  heading_deg: number | null;
  provenance: 'REAL' | 'REPLAYED' | 'SIMULATED' | 'MOCK';
  reported_at: string;
  source: string;
}

interface UseSSEPositionsReturn {
  positions: Position[];
  isConnected: boolean;
  error: string | null;
  reconnect: () => void;
}

export function useSSEPositions(): UseSSEPositionsReturn {
  const [positions, setPositions] = useState<Position[]>([]);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoffRef = useRef(1000); // Start with 1s backoff

  const getToken = () => {
    return useAuthStore.getState().token || sessionStorage.getItem('access_token') || '';
  };

  const connect = () => {
    setError(null);

    // Get current token
    const token = getToken();
    if (!token) {
      setError('Not authenticated');
      setIsConnected(false);
      return;
    }

    // EventSource does not support custom headers; use query string
    const url = `/api/map/positions/stream?token=${encodeURIComponent(token)}`;

    try {
      const es = new EventSource(url);

      es.addEventListener('POSITION_UPDATE', (event: Event) => {
        try {
          const messageEvent = event as MessageEvent;
          const rawData = JSON.parse(messageEvent.data) as any[];
          const normalized: Position[] = (Array.isArray(rawData) ? rawData : []).map((p: any) => {
            let assetType: 'VESSEL' | 'AIRCRAFT' | 'TRUCK' = 'VESSEL';
            if (p.asset_type === 'SEA' || p.asset_type === 'VESSEL') assetType = 'VESSEL';
            else if (p.asset_type === 'AIR' || p.asset_type === 'AIRCRAFT') assetType = 'AIRCRAFT';
            else if (p.asset_type === 'ROAD' || p.asset_type === 'TRUCK') assetType = 'TRUCK';

            return {
              asset_id: String(p.asset_id),
              asset_type: assetType,
              lat: Number(p.lat ?? p.latitude ?? 0),
              lon: Number(p.lon ?? p.longitude ?? 0),
              speed_knots: p.speed_knots !== undefined && p.speed_knots !== null ? Number(p.speed_knots) : null,
              heading_deg: p.heading_deg !== undefined && p.heading_deg !== null ? Number(p.heading_deg) : null,
              provenance: p.provenance || 'SIMULATED',
              reported_at: p.reported_at || p.recorded_at || new Date().toISOString(),
              source: p.source || '',
            };
          });
          setPositions(normalized);
          setIsConnected(true);
          setError(null);
          backoffRef.current = 1000; // Reset backoff on success
        } catch (parseError) {
          console.warn('Failed to parse SSE position data:', parseError);
          // Continue; do not close stream
        }
      });

      es.addEventListener('error', () => {
        console.warn('SSE connection error');
        setIsConnected(false);
        setError('Position feed disconnected');
        es.close();
        scheduleReconnect();
      });

      es.addEventListener('open', () => {
        console.log('SSE connection opened');
        setIsConnected(true);
        setError(null);
      });

      eventSourceRef.current = es;
    } catch (err) {
      console.error('Failed to create EventSource:', err);
      setError('Failed to connect to position feed');
      setIsConnected(false);
      scheduleReconnect();
    }
  };

  const scheduleReconnect = () => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
    }

    const delay = Math.min(backoffRef.current, 30000); // Cap at 30s
    backoffRef.current *= 2; // Exponential backoff

    reconnectTimeoutRef.current = setTimeout(() => {
      console.log(`Reconnecting SSE (after ${delay}ms)…`);
      connect();
    }, delay);
  };

  const reconnect = () => {
    console.log('Manual reconnect triggered');
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    backoffRef.current = 1000; // Reset backoff
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
    }
    connect();
  };

  useEffect(() => {
    connect();

    return () => {
      if (eventSourceRef.current) {
        eventSourceRef.current.close();
      }
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
    };
  }, []); // Only on mount/unmount

  return {
    positions,
    isConnected,
    error,
    reconnect,
  };
}
