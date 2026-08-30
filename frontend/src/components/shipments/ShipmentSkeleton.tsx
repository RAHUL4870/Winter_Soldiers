/**
 * Loading skeleton for the shipment table.
 * Shows grey placeholder rows matching the table shape so the layout
 * does not jump when real data arrives.
 */

export default function ShipmentSkeleton() {
  return (
    <div className="animate-pulse">
      <div className="h-10 bg-gray-200 rounded mb-4 w-full" />
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} className="flex gap-4 py-3 border-b border-gray-100">
          <div className="h-4 bg-gray-200 rounded w-32" />
          <div className="h-4 bg-gray-200 rounded w-48" />
          <div className="h-4 bg-gray-200 rounded w-20" />
          <div className="h-4 bg-gray-200 rounded w-24" />
          <div className="h-4 bg-gray-200 rounded w-28" />
          <div className="h-4 bg-gray-200 rounded w-28" />
        </div>
      ))}
    </div>
  )
}
