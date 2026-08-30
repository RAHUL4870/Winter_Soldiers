import { LoginRequest, LoginResponse } from '../types/auth'
import { apiClient } from './client'

export async function login(credentials: LoginRequest): Promise<LoginResponse> {
  return apiClient<LoginResponse>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify(credentials),
  })
}
