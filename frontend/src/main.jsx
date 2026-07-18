import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import Gate from './Gate.jsx'

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <Gate />
  </StrictMode>,
)
