/**
 * Real data-driven shipment list component.
 * Consumes GET /api/shipments (T-021) via TanStack Query.
 * Handles loading, error, empty, and success states explicitly.
 */

import React, { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { format, parseISO } from 'date-fns'

import { fetchShipments } from '../../api/shipments'
import type { ShipmentFilters, ShipmentStatus, TransportMode } from '../../types/shipment'
import { SHIPMENT_STATUSES, TRANSPORT_MODES } from '../../types/shipment'
import ModeIcon from './ModeIcon'
import StatusBadge from './StatusBadge'
import ShipmentSkeleton from './ShipmentSkeleton'

const PAGE_SIZE = 20

function formatDate(dateStr: string | null): string {
  if (!dateStr) return '—'
  try {
    return format(parseISO(dateStr), 'dd MMM yyyy')
  } catch {
    return '—'
  }
}

export default function ShipmentList() {
  const navigate = useNavigate()

  // Filter state — changing a filter resets page to 1
  const [statusFilter, setStatusFilter] = useState<ShipmentStatus | undefined>()
  const [modeFilter, setModeFilter] = useState<TransportMode | undefined>()
  const [page, setPage] = useState(1)

  const filters: ShipmentFilters = {
    status: statusFilter,
    mode: modeFilter,
    page,
    size: PAGE_SIZE,
  }

  const { data, isLoading, isError, error } = useQuery({
    queryKey: ['shipments', filters],
    queryFn: () => fetchShipments(filters),
    refetchInterval: 30_000, // 30-second auto-refresh (hard requirement)
    staleTime: 15_000,
  })

  // Filter change handlers — always reset to page 1
  const handleStatusChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    setStatusFilter((e.target.value as ShipmentStatus) || undefined)
    setPage(1)
  }

  const handleModeChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    setModeFilter((e.target.value as TransportMode) || undefined)
    setPage(1)
  }

  const handleRowClick = (shipmentId: string) => {
    navigate(`/shipments/${shipmentId}`)
  }

  return (
    <div>
      {/* Filter controls */}
      <div className="flex items-center gap-4 mb-6">
        <div>
          <label
            htmlFor="status-filter"
            className="block text-xs font-medium text-gray-500 mb-1"
          >
            Status
          </label>
          <select
            id="status-filter"
            value={statusFilter ?? ''}
            onChange={handleStatusChange}
            className="block w-40 rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 text-sm"
          >
            <option value="">All statuses</option>
            {SHIPMENT_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace('_', ' ')}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label
            htmlFor="mode-filter"
            className="block text-xs font-medium text-gray-500 mb-1"
          >
            Mode
          </label>
          <select
            id="mode-filter"
            value={modeFilter ?? ''}
            onChange={handleModeChange}
            className="block w-40 rounded-md border-gray-300 shadow-sm focus:border-indigo-500 focus:ring-indigo-500 text-sm"
          >
            <option value="">All modes</option>
            {TRANSPORT_MODES.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Loading state */}
      {isLoading && <ShipmentSkeleton />}

      {/* Error state */}
      {isError && (
        <div className="rounded-md bg-red-50 p-6 text-center">
          <p className="text-sm font-medium text-red-800">
            Failed to load shipments
          </p>
          <p className="text-xs text-red-600 mt-1">
            {error instanceof Error ? error.message : 'Network error — check backend'}
          </p>
        </div>
      )}

      {/* Success state */}
      {data && (
        <>
          {/* Empty state */}
          {data.items.length === 0 ? (
            <div className="rounded-md border border-dashed border-gray-300 p-12 text-center">
              <p className="text-sm font-medium text-gray-500">
                No shipments match these filters
              </p>
              <p className="text-xs text-gray-400 mt-1">
                Try adjusting the status or mode filter
              </p>
            </div>
          ) : (
            /* Table */
            <div className="overflow-hidden shadow ring-1 ring-black ring-opacity-5 rounded-lg">
              <table className="min-w-full divide-y divide-gray-300">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="py-3.5 pl-4 pr-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">
                      Shipment ID
                    </th>
                    <th className="px-3 py-3.5 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">
                      Route
                    </th>
                    <th className="px-3 py-3.5 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">
                      Mode
                    </th>
                    <th className="px-3 py-3.5 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">
                      Status
                    </th>
                    <th className="px-3 py-3.5 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">
                      SLA Deadline
                    </th>
                    <th className="px-3 py-3.5 text-left text-xs font-semibold text-gray-500 uppercase tracking-wider">
                      ETA
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200 bg-white">
                  {data.items.map((shipment) => (
                    <tr
                      key={shipment.id}
                      onClick={() => handleRowClick(shipment.id)}
                      className="hover:bg-indigo-50 cursor-pointer transition-colors"
                    >
                      <td className="whitespace-nowrap py-4 pl-4 pr-3 text-sm font-mono text-gray-900">
                        {shipment.id.slice(0, 8)}…
                      </td>
                      <td className="whitespace-nowrap px-3 py-4 text-sm text-gray-700">
                        <span className="font-medium">{shipment.origin}</span>
                        <span className="mx-1 text-gray-400">→</span>
                        <span className="font-medium">{shipment.destination}</span>
                      </td>
                      <td className="whitespace-nowrap px-3 py-4 text-sm">
                        <ModeIcon mode={shipment.mode} />
                      </td>
                      <td className="whitespace-nowrap px-3 py-4 text-sm">
                        <StatusBadge status={shipment.status} />
                      </td>
                      <td className="whitespace-nowrap px-3 py-4 text-sm text-gray-600">
                        {formatDate(shipment.strictest_sla_deadline)}
                      </td>
                      <td className="whitespace-nowrap px-3 py-4 text-sm text-gray-600">
                        {formatDate(shipment.revised_eta)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Pagination controls */}
          {data.total > 0 && (
            <div className="mt-4 flex items-center justify-between">
              <p className="text-sm text-gray-600">
                Page {data.page} of {data.total_pages} ·{' '}
                <span className="font-medium">{data.total}</span> shipments
              </p>

              <div className="flex items-center gap-2">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="inline-flex items-center px-3 py-1.5 rounded border border-gray-300 text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  ← Previous
                </button>
                <button
                  onClick={() => setPage((p) => p + 1)}
                  disabled={page >= data.total_pages}
                  className="inline-flex items-center px-3 py-1.5 rounded border border-gray-300 text-sm font-medium text-gray-700 bg-white hover:bg-gray-50 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Next →
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
