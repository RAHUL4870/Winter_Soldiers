/**
 * Stub page — navigation target for clicking a shipment row.
 * Real content implemented in T-069 (ShipmentDetail full view).
 */

import { useParams } from 'react-router-dom'

export default function ShipmentDetailPage() {
  const { id } = useParams<{ id: string }>()

  return (
    <div>
      <h1 className="text-3xl font-bold text-gray-900 mb-4">Shipment Detail</h1>
      <p className="text-gray-600">
        Shipment ID: <span className="font-mono text-gray-900">{id}</span>
      </p>
      <p className="mt-4 text-gray-400 text-sm">
        Full detail view will be implemented in T-069.
      </p>
    </div>
  )
}
