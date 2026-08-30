/**
 * TypeScript types matching T-021's backend response shapes exactly.
 * Field names, nullability, and enum values verified against the real
 * Pydantic schema in src/nexafreight/schemas/shipment.py and
 * src/nexafreight/schemas/common.py.
 */

// Transport modes matching backend TransportMode enum (T-007)
export type TransportMode = 'SEA' | 'AIR' | 'ROAD' | 'RAIL'

// Shipment statuses matching backend ShipmentStatus enum (T-007)
export type ShipmentStatus =
  | 'PLANNED'
  | 'IN_TRANSIT'
  | 'DELIVERED'
  | 'DELAYED'
  | 'CANCELLED'

// Valid mode values for filter dropdowns — single source of truth
export const TRANSPORT_MODES: TransportMode[] = ['SEA', 'AIR', 'ROAD', 'RAIL']

// Valid status values for filter dropdowns — single source of truth
export const SHIPMENT_STATUSES: ShipmentStatus[] = [
  'PLANNED',
  'IN_TRANSIT',
  'DELIVERED',
  'DELAYED',
  'CANCELLED',
]

/**
 * One item in the paginated shipment list.
 * Matches ShipmentListItem Pydantic schema from T-021 exactly.
 *
 * Notes on nullable fields (per T-021's documented decisions):
 * - revised_eta: null until ML-based ETA prediction (T-040/T-043) exists.
 *   Frontend must render null gracefully (show "—", never "Invalid Date").
 * - strictest_sla_deadline: null if no orders are linked to this shipment yet.
 */
export interface ShipmentListItem {
  id: string
  origin: string
  destination: string
  mode: TransportMode
  status: ShipmentStatus
  strictest_sla_deadline: string | null
  revised_eta: string | null
}

/**
 * Generic pagination envelope — matches PaginatedResponse[T] from T-021's
 * common.py schema. Reusable for alerts, audit log, etc. in later tasks.
 */
export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  size: number
  total_pages: number
}

// Convenience alias for the specific paginated shipment response
export type ShipmentListResponse = PaginatedResponse<ShipmentListItem>

// Filter parameters accepted by the GET /api/shipments endpoint
export interface ShipmentFilters {
  status?: ShipmentStatus
  mode?: TransportMode
  alert?: boolean
  page: number
  size: number
}
