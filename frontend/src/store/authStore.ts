import { create } from 'zustand'
import { AuthState } from '../types/auth'
import { login as loginApi } from '../api/auth'

export const useAuthStore = create<AuthState>()((set) => ({
  token: null,
  user: null,
  isAuthenticated: false,
  login: async (email: string, password: string) => {
    const response = await loginApi(email, password)
    set({
      token: response.access_token,
      user: response.user,
      isAuthenticated: true,
    })
  },
  logout: () => {
    set({
      token: null,
      user: null,
      isAuthenticated: false,
    })
    // Redirect to login page
    window.location.href = '/login'
  },
}))
