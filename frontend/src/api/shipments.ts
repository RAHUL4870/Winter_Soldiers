/**
 * API functions for shipment endpoints.
 * Uses the shared apiClient() from T-013 — auth headers and 401 handling
 * are handled automatically by that wrapper.
 */

import { apiClient } from './client'
import type { ShipmentFilters, ShipmentListResponse } from '../types/shipment'

/**
 * Fetch paginated shipment list from GET /api/shipments.
 *
 * @param filters - Optional filter and pagination parameters
 * @returns Paginated response with shipment list items
 */
export async function fetchShipments(
  filters: ShipmentFilters
): Promise<ShipmentListResponse> {
  // Build query string from non-undefined filter values
  const params = new URLSearchParams()

  if (filters.status) params.set('status', filters.status)
  if (filters.mode) params.set('mode', filters.mode)
  if (filters.alert !== undefined) params.set('alert', String(filters.alert))
  params.set('page', String(filters.page))
  params.set('size', String(filters.size))

  const queryString = params.toString()
  const endpoint = `/api/shipments${queryString ? `?${queryString}` : ''}`

  return apiClient<ShipmentListResponse>(endpoint)
}
