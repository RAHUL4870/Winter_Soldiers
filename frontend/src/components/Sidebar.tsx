import { NavLink } from 'react-router-dom'
import { useAuthStore } from '../store/authStore'

const navigation = [
  { name: 'Dashboard', href: '/dashboard', icon: '📊' },
  { name: 'Shipments', href: '/shipments', icon: '📦' },
  { name: 'Map', href: '/map', icon: '🗺️' },
  { name: 'Analytics', href: '/analytics', icon: '📈' },
  { name: 'Audit', href: '/audit', icon: '📋' },
]

export default function Sidebar() {
  const user = useAuthStore((state) => state.user)
  const logout = useAuthStore((state) => state.logout)

  return (
    <div className="flex flex-col w-64 bg-gray-800 min-h-screen">
      {/* Header */}
      <div className="flex items-center justify-center h-16 bg-gray-900">
        <h1 className="text-white text-xl font-bold">NexaFreight</h1>
      </div>

      {/* Navigation */}
      <nav className="flex-1 px-2 py-4 space-y-1">
        {navigation.map((item) => (
          <NavLink
            key={item.name}
            to={item.href}
            className={({ isActive }) =>
              `flex items-center px-4 py-3 text-sm font-medium rounded-md transition-colors ${
                isActive
                  ? 'bg-gray-900 text-white'
                  : 'text-gray-300 hover:bg-gray-700 hover:text-white'
              }`
            }
          >
            <span className="mr-3 text-xl">{item.icon}</span>
            {item.name}
          </NavLink>
        ))}
      </nav>

      {/* User section */}
      <div className="p-4 border-t border-gray-700">
        {user && (
          <div className="mb-3">
            <p className="text-xs text-gray-400">Logged in as</p>
            <p className="text-sm text-white font-medium">{user.email}</p>
            <p className="text-xs text-gray-400 capitalize">{user.role}</p>
          </div>
        )}
        <button
          onClick={logout}
          className="w-full px-4 py-2 text-sm font-medium text-white bg-red-600 rounded-md hover:bg-red-700 transition-colors"
        >
          Logout
        </button>
      </div>
    </div>
  )
}
