/**
 * Colored pill badge for shipment status.
 * Color mapping is defined here as the single source of truth —
 * reuse this component anywhere status needs visual representation.
 */

import type { ShipmentStatus } from '../../types/shipment'

interface StatusBadgeProps {
  status: ShipmentStatus
}

const statusConfig: Record<
  ShipmentStatus,
  { label: string; className: string }
> = {
  PLANNED: {
    label: 'Planned',
    className: 'bg-blue-100 text-blue-800',
  },
  IN_TRANSIT: {
    label: 'In Transit',
    className: 'bg-indigo-100 text-indigo-800',
  },
  DELIVERED: {
    label: 'Delivered',
    className: 'bg-green-100 text-green-800',
  },
  DELAYED: {
    label: 'Delayed',
    className: 'bg-amber-100 text-amber-800',
  },
  CANCELLED: {
    label: 'Cancelled',
    className: 'bg-red-100 text-red-800',
  },
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  const config = statusConfig[status] ?? {
    label: status,
    className: 'bg-gray-100 text-gray-800',
  }

  return (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${config.className}`}
    >
      {config.label}
    </span>
  )
}
