import FreightMap from '../components/map/FreightMap';
import ShipmentList from '../components/shipments/ShipmentList';

export default function Dashboard() {
  return (
    <div className="flex h-screen bg-gray-50">
      <div className="flex-1">
        <FreightMap />
      </div>
      <div className="w-96 border-l bg-white overflow-y-auto">
        <div className="p-4">
          <h2 className="text-lg font-bold mb-4">Active Shipments</h2>
          <ShipmentList />
        </div>
      </div>
    </div>
  );
}
