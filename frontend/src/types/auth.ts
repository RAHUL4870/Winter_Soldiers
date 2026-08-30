export interface LoginRequest {
  email: string
  password: string
}

export interface UserProfile {
  email: string
  full_name: string
  role: string
  is_active: boolean
}

export interface LoginResponse {
  access_token: string
  token_type: string
  user: UserProfile
  expires_in: number
}

export interface AuthState {
  token: string | null
  user: UserProfile | null
  isAuthenticated: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => void
}
