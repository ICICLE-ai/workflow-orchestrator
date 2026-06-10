# workflow-orchestrator
An extensible workflow orchestration platform that manages the full ML lifecycle — ingesting data from HPC, cloud, and drone sources; harmonizing it into ZARR cubes; training and publishing models via PMC; and delivering inference results with uncertainty quantification.

## Project Structure

This is a monorepo containing both the React-based frontend and FastAPI-based backend.

- `frontend/`: React Router v7 & Mantine UI frontend app.
- `backend/`: FastAPI & PostgreSQL backend service.

## Setup & Running Locally

### Backend Setup

The backend uses Python 3 and FastAPI, and connects to a PostgreSQL database.

1. Navigate to the backend directory:
   ```bash
   cd backend
   ```
2. Start the PostgreSQL/PostGIS database using Docker:
   ```bash
   docker compose up -d
   ```
3. Install the required Python packages:
   ```bash
   pip install -r requirements.txt
   ```
4. Start the backend development server:
   ```bash
   python main.py
   ```
   *The backend will run on `http://localhost:8002`.*

### Frontend Setup

The frontend uses Node.js and React Router v7.

1. Navigate to the frontend directory:
   ```bash
   cd frontend
   ```
2. Install the Node modules:
   ```bash
   npm install
   ```
3. Start the Vite development server:
   ```bash
   npm run dev
   ```
   *The frontend will be accessible at `http://localhost:5173`.*
