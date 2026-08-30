# Setup Instructions

## Install Dependencies
```bash
cd frontend
npm install
```

## Run Development Server
```bash
npm run dev
```
The app will be available at [http://localhost:5173](http://localhost:5173)

## Build for Production
```bash
npm run build
```

## Features
- ✅ Vite + React 18 + TypeScript
- ✅ Tailwind CSS configured
- ✅ React Router v6 with protected routes
- ✅ Zustand store for authentication (in-memory only)
- ✅ TanStack Query for API calls
- ✅ MapLibre GL for map visualization
- ✅ Recharts for analytics
- ✅ date-fns for date formatting
- ✅ Login page with error handling
- ✅ Sidebar navigation
- ✅ Modular code structure

## Folder Structure
```text
src/
├── api/          # API client functions
├── components/   # Reusable components
├── pages/        # Page components
├── store/        # Zustand stores
├── App.tsx       # Main app component
├── main.tsx      # Entry point
└── index.css     # Global styles with Tailwind
```
